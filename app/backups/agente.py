"""Agente local de respaldos (corre en el PC con Windows que tiene G: y D:).

Uso (desde la carpeta PV, con el venv activado):

  python -m app.backups.agente diagnostico   Resuelve el origen y muestra servidores/espacio. No escribe nada.
  python -m app.backups.agente inventario    Registra en la BD lo que hay en Drive, SIN descargar.
  python -m app.backups.agente ejecutar      Una ejecucion completa ahora (tipo Manual).
  python -m app.backups.agente reintentar    Solo los respaldos que quedaron con error.
  python -m app.backups.agente servicio      Queda corriendo: ejecucion diaria a BACKUPS_HORA_DIARIA,
                                             recupera la del dia si el PC estaba apagado y atiende los
                                             botones del panel ("Ejecutar ahora", "Reintentar fallidos").

El servicio lo arranca el Programador de tareas de Windows al iniciar sesion
(scripts/instalar_tarea_respaldos.ps1): necesita la sesion del usuario porque
Google Drive para escritorio solo monta G: dentro de ella.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import date, datetime, timezone

from sqlalchemy import update
from sqlmodel import select

from app.backups.config import BackupSettings, backup_settings
from app.backups.motor import BloqueoArchivo, BloqueoOcupado, MotorRespaldos, proxima_ejecucion, utcnow
from app.models import BackupEjecucion, BackupSolicitud

INTENTOS_DIARIOS = 3
ESPERA_REINTENTO_DIARIO = 30 * 60  # segundos

SOLICITUDES = {
    "EJECUTAR": ("Manual", "normal"),
    "REINTENTAR_FALLIDOS": ("Reintento", "reintentar"),
    "INVENTARIO": ("Inventario", "inventario"),
}


def _a_utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def aviso(cfg: BackupSettings, mensaje: str) -> None:
    """Mensajes del servicio: consola (si hay) + Registros\\agente.log
    (con pythonw, que es como lo arranca el Programador de tareas, no hay consola)."""
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S} {mensaje}"
    if sys.stdout is not None:
        print(linea, flush=True)
    try:
        cfg.destino_registros.mkdir(parents=True, exist_ok=True)
        with open(cfg.destino_registros / "agente.log", "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass


class ServicioRespaldos:
    def __init__(self, motor: MotorRespaldos):
        self.motor = motor
        self.cfg: BackupSettings = motor.cfg
        self.ultimo_intento_diario: date | None = None
        self.intentos_hoy = 0
        self.reintentar_en: float | None = None
        self.ack_version: int | None = None
        self.backend_caido = False
        self.ultimo_sondeo_bd = float("-inf")  # el primer ciclo si consulta la base

    async def tomar_solicitud(self) -> BackupSolicitud | None:
        async with self.motor.sf() as s:
            sol = (await s.exec(
                select(BackupSolicitud).where(BackupSolicitud.estado == "PENDIENTE").order_by(BackupSolicitud.solicitado_en).limit(1)
            )).first()
            if sol is None:
                return None
            res = await s.exec(
                update(BackupSolicitud)
                .where(BackupSolicitud.id_solicitud == sol.id_solicitud, BackupSolicitud.estado == "PENDIENTE")
                .values(estado="TOMADA", tomada_en=utcnow())
            )
            await s.commit()
            return sol if res.rowcount else None

    async def cerrar_solicitud(self, id_solicitud: int, estado: str) -> None:
        async with self.motor.sf() as s:
            sol = await s.get(BackupSolicitud, id_solicitud)
            if sol:
                sol.estado = estado
                s.add(sol)
                await s.commit()

    async def toca_diaria(self) -> bool:
        ahora = datetime.now()
        hoy = ahora.date()
        if self.ultimo_intento_diario == hoy:
            # Ya se intento hoy: solo se repite si termino en ERROR (ver ciclo)
            return self.reintentar_en is not None and time.monotonic() >= self.reintentar_en
        h, m = (int(x) for x in self.cfg.hora_diaria.split(":"))
        slot = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
        if ahora < slot:
            return False
        if not self.cfg.recuperar_ejecucion_perdida and (ahora - slot).total_seconds() > 15 * 60:
            return False
        try:
            async with self.motor.sf() as s:
                hecha = (await s.exec(
                    select(BackupEjecucion.id_ejecucion).where(
                        BackupEjecucion.tipo == "Automatica", BackupEjecucion.inicio >= _a_utc(slot)
                    ).limit(1)
                )).first()
        except Exception:  # noqa: BLE001 - sin BD igual se intenta (y se registra el error del dia)
            hecha = None
        if hecha is not None:
            self.ultimo_intento_diario = hoy
            return False
        return True

    async def latido_http(self) -> dict | None:
        """Late contra el backend (que responde desde memoria, sin despertar la
        base) y devuelve {"solicitud_pendiente", "version"}; None si no responde
        (Render arrancando o sin Internet): se reintenta en el siguiente ciclo."""
        import httpx

        from app import auth_state

        datos = self.motor.estado_agente("INACTIVO", proxima=proxima_ejecucion(self.cfg))
        if datos["proxima_ejecucion"] is not None:
            datos["proxima_ejecucion"] = datos["proxima_ejecucion"].isoformat()
        datos["ack_version"] = self.ack_version
        url = self.cfg.backend_url.rstrip("/") + "/api/backups/agente/latido"
        try:
            # 90 s: un Render gratuito dormido tarda ~1 min en despertar
            async with httpx.AsyncClient(timeout=90) as cliente:
                r = await cliente.post(url, json=datos, headers={"Authorization": f"Bearer {auth_state.issue_token()}"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            self.ack_version = None
            if self.backend_caido:
                aviso(self.cfg, "el backend volvio a responder")
                self.backend_caido = False
            return r.json()
        except Exception as e:  # noqa: BLE001
            if not self.backend_caido:  # se avisa una vez, no cada minuto
                aviso(self.cfg, f"el backend no responde ({type(e).__name__}: {str(e)[:120]}); se sigue intentando")
                self.backend_caido = True
            return None

    async def hay_que_mirar_solicitudes(self) -> tuple[bool, int | None]:
        """(¿consultar la base?, version de solicitudes informada por el backend)."""
        if self.cfg.backend_url:
            r = await self.latido_http()
            if r is not None:
                return bool(r.get("solicitud_pendiente")), r.get("version")
            # Backend caido: no se sabe; se cae al sondeo lento de la base
        ahora = time.monotonic()
        if ahora - self.ultimo_sondeo_bd >= self.cfg.intervalo_sondeo_bd_segundos:
            self.ultimo_sondeo_bd = ahora
            return True, None
        return False, None

    async def ciclo(self) -> None:
        sol = None
        mirar, version = await self.hay_que_mirar_solicitudes()
        if mirar:
            try:
                sol = await self.tomar_solicitud()
                if sol is None and version is not None:
                    self.ack_version = version  # se confirma en el proximo latido
            except Exception as e:  # noqa: BLE001
                aviso(self.cfg, f"no se pudieron leer las solicitudes del panel: {e}")
        if sol is not None:
            tipo, modo = SOLICITUDES.get(sol.tipo, ("Manual", "normal"))
            aviso(self.cfg, f"solicitud #{sol.id_solicitud} del panel: {sol.tipo}")
            estado = await self.motor.ejecutar(tipo=tipo, modo=modo, id_solicitud=sol.id_solicitud)
            await self.cerrar_solicitud(sol.id_solicitud, "PENDIENTE" if estado == "OCUPADO" else "HECHA")
            aviso(self.cfg, f"solicitud #{sol.id_solicitud} terminada: {estado}")
            return
        if await self.toca_diaria():
            aviso(self.cfg, "inicia la ejecucion diaria")
            estado = await self.motor.ejecutar(tipo="Automatica")
            if estado != "OCUPADO":
                hoy = datetime.now().date()
                self.intentos_hoy = self.intentos_hoy + 1 if self.ultimo_intento_diario == hoy else 1
                self.ultimo_intento_diario = hoy
                # Drive sin montar al encender, corte de Internet...: hasta 3 intentos por dia
                repetir = estado == "ERROR" and self.intentos_hoy < INTENTOS_DIARIOS
                self.reintentar_en = time.monotonic() + ESPERA_REINTENTO_DIARIO if repetir else None
                aviso(self.cfg, f"ejecucion diaria terminada: {estado}"
                      + (f"; se reintenta en {ESPERA_REINTENTO_DIARIO // 60} min" if repetir else ""))
            return
        # En reposo NO se escribe en la base (la mantendria despierta todo el
        # dia): el latido va por HTTP. Sin backend_url, se guarda en la base
        # solo cuando ya se la consulto por el sondeo lento.
        if mirar and not self.cfg.backend_url:
            try:
                await self.motor.latido_agente("INACTIVO", None, accesible=True, proxima=proxima_ejecucion(self.cfg))
            except Exception as e:  # noqa: BLE001
                aviso(self.cfg, f"no se pudo registrar el latido: {e}")

    async def correr(self) -> None:
        self.cfg.destino_temporal.mkdir(parents=True, exist_ok=True)
        with BloqueoArchivo(self.cfg.destino_temporal / ".servicio.lock"):
            enlace = f"latido HTTP a {self.cfg.backend_url}" if self.cfg.backend_url else "sondeo directo a la base (falta BACKUPS_BACKEND_URL)"
            aviso(self.cfg, f"servicio iniciado; ejecucion diaria a las {self.cfg.hora_diaria}; {enlace}")
            while True:
                try:
                    await self.ciclo()
                except Exception as e:  # noqa: BLE001 - el servicio no debe morir
                    aviso(self.cfg, f"error en el ciclo del servicio: {type(e).__name__}: {e}")
                await asyncio.sleep(self.cfg.intervalo_sondeo_segundos)


def diagnostico(cfg: BackupSettings) -> int:
    from app.backups import transferencia as tr
    from app.backups.descubrimiento import descubrir
    from app.backups.fuente import DriveEscritorioFuente, FuenteNoDisponible

    try:
        fuente = DriveEscritorioFuente.desde_config(cfg)
        fuente.verificar()
    except FuenteNoDisponible as e:
        print(f"ORIGEN NO ACCESIBLE: {e}")
        return 1
    print(f"Origen: {fuente.ruta}")
    inv = descubrir(fuente, cfg.lista_incluir, cfg.lista_excluir)
    total = 0
    for srv in inv.servidores:
        tam = sum(r.tamano_total for r in srv.respaldos)
        total += tam
        tipos: dict[str, int] = {}
        for r in srv.respaldos:
            tipos[r.tipo] = tipos.get(r.tipo, 0) + 1
        estado = srv.error or ("VACIO" if srv.vacio else ", ".join(f"{k}: {v}" for k, v in sorted(tipos.items())))
        print(f"  {srv.nombre:45s} {len(srv.respaldos):4d} carpetas {tam / 1024**3:9.2f} GB  [{estado}]")
    for e in inv.sueltos_raiz:
        print(f"  (archivo suelto en la raiz: {e.nombre})")
    print(f"Total en origen: {total / 1024**3:.2f} GB")
    try:
        print(f"Libre en destino {cfg.destino_raiz}: {tr.espacio_libre(cfg.destino_raiz) / 1024**3:.2f} GB")
    except OSError as e:
        print(f"Destino {cfg.destino_raiz} no disponible: {e}")
    return 0


def main(argv: list[str]) -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    comando = argv[1] if len(argv) > 1 else "ayuda"
    cfg = backup_settings
    if comando == "diagnostico":
        return diagnostico(cfg)
    motor = MotorRespaldos(cfg)
    if comando == "servicio":
        try:
            asyncio.run(ServicioRespaldos(motor).correr())
        except BloqueoOcupado:
            print("Respaldos: el servicio ya esta corriendo en este PC")
            return 1
        return 0
    modos = {"ejecutar": ("Manual", "normal"), "reintentar": ("Reintento", "reintentar"), "inventario": ("Inventario", "inventario")}
    if comando in modos:
        tipo, modo = modos[comando]
        estado = asyncio.run(motor.ejecutar(tipo=tipo, modo=modo))
        print(f"Resultado: {estado}")
        return 0 if estado in ("OK", "CON_ADVERTENCIAS") else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
