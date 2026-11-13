"""Orquesta una ejecucion del modulo de respaldos (algoritmo de la seccion
6.2 de la especificacion):

  bloqueo -> base de datos -> origen (Drive) -> descubrir servidores ->
  sincronizar inventario -> segundo sondeo -> politica de finalizacion ->
  descargar a D:\\Backups\\_TEMPORAL -> verificar SHA-256/tamano ->
  publicar en D:\\Backups\\Backupumsa2627 -> registro diario + alertas

Reglas que no se rompen:
  - El origen solo se lee (nunca se mueve, renombra ni borra nada en Drive).
  - Nada se borra ni se sobrescribe en la carpeta final: si un archivo ya
    existe con contenido distinto, se registra el conflicto y se deja.
  - Un archivo solo cuenta como correcto con evidencia en la base de datos
    (VALIDADO con SHA-256, o SIN_HASH = verificacion limitada a tamano).
  - El registro diario se escribe siempre, aunque falle todo lo demas.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from sqlalchemy import delete, update
from sqlmodel import select

from app.backups import transferencia as tr
from app.backups.auditoria import FilaDetalle, RegistroDiario, ResumenServidor, fmt
from app.backups.config import BackupSettings, backup_settings, ruta_visible
from app.backups.descubrimiento import RespaldoOrigen, ServidorOrigen, descubrir, resondear
from app.backups.fuente import DriveEscritorioFuente, Entrada, Fuente, FuenteNoDisponible
from app.backups.verificacion import Evaluacion, evaluar
from app.models import (
    BackupAgente,
    BackupArchivo,
    BackupEjecucion,
    BackupEvento,
    BackupRespaldo,
    BackupServidor,
    BackupSolicitud,
)

HOST = socket.gethostname()[:100]
ESTADOS_FINALES = {"VALIDADO", "SIN_HASH"}
ESTADOS_ERROR = {"ERROR_INTEGRIDAD", "ERROR_TRANSFERENCIA", "ERROR_ACCESO", "SIN_ESPACIO"}
UMBRAL_GRANDE = 10 * 1024**3  # los respaldos grandes se dejan para el final de la cola
MAX_FALLOS_INTEGRIDAD = 2  # no volver a bajar 50 GB cinco veces si el hash no coincide
MAX_LOG_EN_BD = 60_000  # caracteres del log que se suben a la base por ejecucion
LOGS_EN_BD = 30  # ejecuciones que conservan su log en la base
DIAS_EVENTOS = 180


def utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


class BloqueoOcupado(Exception):
    pass


class BloqueoArchivo:
    """Bloqueo exclusivo entre procesos (msvcrt en Windows, flock en Linux).
    El sistema operativo lo libera solo si el proceso muere."""

    def __init__(self, ruta: Path):
        self.ruta = ruta
        self._f = None

    def __enter__(self) -> "BloqueoArchivo":
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.ruta, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            raise BloqueoOcupado(f"ya hay otra ejecucion en curso ({self.ruta})") from e
        self._f = f
        return self

    def __exit__(self, *exc) -> None:
        if self._f is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._f.close()
        self._f = None


@dataclass
class Corrida:
    """Estado en memoria de una ejecucion."""

    tipo: str
    modo: str  # normal | inventario | reintentar
    reg: RegistroDiario
    id_ejecucion: int | None
    inicio: float = field(default_factory=time.monotonic)
    archivos_nuevos: int = 0
    archivos_descargados: int = 0
    archivos_validados: int = 0
    archivos_ya_existentes: int = 0
    archivos_pendientes: int = 0
    archivos_fallidos: int = 0
    bytes_descargados: int = 0
    servidores: int = 0
    errores: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    alertas: list[str] = field(default_factory=list)  # lo que merece Telegram
    eventos: list[BackupEvento] = field(default_factory=list)
    abortar: bool = False
    progreso: list = field(default_factory=lambda: ["", 0, 0])  # ruta, bytes, total
    ruta_origen: str | None = None

    @property
    def log(self):
        return self.reg.log

    def evento(self, nivel: str, codigo: str, mensaje: str, servidor: str | None = None, ruta: str | None = None):
        getattr(self.log, {"INFO": "info", "WARN": "warning", "ERROR": "error"}[nivel])(
            "[%s] %s%s", codigo, f"{ruta}: " if ruta else (f"{servidor}: " if servidor else ""), mensaje
        )
        if self.id_ejecucion is not None:
            self.eventos.append(
                BackupEvento(
                    id_ejecucion=self.id_ejecucion, nivel=nivel, codigo=codigo[:40],
                    servidor=servidor, ruta_relativa=(ruta or None) and ruta[:700], mensaje=mensaje,
                )
            )
        if nivel == "ERROR":
            self.errores.append(mensaje)
        elif nivel == "WARN":
            self.advertencias.append(mensaje)


def _sf_por_defecto():
    from app.db import async_session_factory

    return async_session_factory


async def _telegram(texto: str) -> None:
    from app.services import telegram_service

    await telegram_service.enviar_alerta(texto)


class MotorRespaldos:
    def __init__(
        self,
        cfg: BackupSettings = backup_settings,
        session_factory=None,
        fuente_factory: Callable[[], Fuente] | None = None,
        notificar: Callable[[str], object] | None = None,
    ):
        self.cfg = cfg
        self.sf = session_factory or _sf_por_defecto()
        self.fuente_factory = fuente_factory or (lambda: DriveEscritorioFuente.desde_config(cfg))
        self.notificar = notificar if notificar is not None else (_telegram if cfg.alertas_telegram else None)
        # Lo ultimo que se supo, para el latido HTTP entre ejecuciones
        self.ultimo_uso: int | None = None
        self.ultimo_origen: tuple[str | None, bool | None] = (None, None)

    # ------------------------------------------------------------------ rutas
    def _final(self, rel: str) -> Path:
        return self.cfg.destino_backups.joinpath(*rel.split("/"))

    def _staged(self, rel: str) -> Path:
        return self.cfg.destino_temporal.joinpath(*rel.split("/"))

    def _cache(self) -> Path | None:
        if self.cfg.ruta_cache_drive:
            return Path(self.cfg.ruta_cache_drive)
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "Google" / "DriveFS" if base else None

    # --------------------------------------------------------------- ejecucion
    async def ejecutar(self, tipo: str = "Automatica", modo: str = "normal", id_solicitud: int | None = None) -> str:
        """Corre una ejecucion completa. Devuelve el estado final
        (OK | CON_ADVERTENCIAS | ERROR | OCUPADO)."""
        cfg = self.cfg
        destino_ok = True
        error_destino = None
        try:
            for p in (cfg.destino_backups, cfg.destino_temporal, cfg.destino_registros):
                p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            destino_ok = False
            error_destino = f"no se puede usar el destino {cfg.destino_raiz}: {e}"

        bloqueo = BloqueoArchivo(cfg.destino_temporal / ".ejecucion.lock") if destino_ok else None
        try:
            if bloqueo:
                bloqueo.__enter__()
        except BloqueoOcupado as e:
            print(f"Respaldos: {e}")
            return "OCUPADO"

        try:
            id_ej, error_bd = None, None
            try:
                id_ej = await self._crear_ejecucion(tipo, id_solicitud)
            except Exception as e:  # noqa: BLE001 - se registra en el log del dia
                error_bd = f"base de datos no accesible: {type(e).__name__}: {str(e)[:300]}"

            raiz_registros = cfg.destino_registros if destino_ok else (
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PV-Respaldos" / "Registros"
            )
            reg = RegistroDiario(raiz_registros, id_ej, tipo if modo == "normal" else f"{tipo}/{modo}")
            c = Corrida(tipo=tipo, modo=modo, reg=reg, id_ejecucion=id_ej)
            latidos = asyncio.create_task(self._latidos(c)) if id_ej else None

            try:
                if error_destino:
                    c.evento("ERROR", "ERROR_DESTINO", error_destino)
                    c.alertas.append(error_destino)
                elif error_bd:
                    c.evento("ERROR", "ERROR_BD", error_bd + " (sin BD no hay evidencia de lo ya validado: no se descarga nada)")
                    c.alertas.append(error_bd)
                else:
                    await self._procesar(c)
            except Exception as e:  # noqa: BLE001 - nunca dejar la ejecucion sin cerrar
                c.evento("ERROR", "ERROR_INTERNO", f"{type(e).__name__}: {e}")
                c.log.error(traceback.format_exc())
                c.alertas.append(f"error interno: {type(e).__name__}: {str(e)[:200]}")
            finally:
                if latidos:
                    latidos.cancel()
            return await self._cerrar(c)
        finally:
            if bloqueo:
                bloqueo.__exit__(None, None, None)

    # ------------------------------------------------------------------ pasos
    async def _procesar(self, c: Corrida) -> None:
        cfg = self.cfg
        await self._marcar_interrumpidas(c)

        try:
            fuente = await asyncio.to_thread(self.fuente_factory)
            await asyncio.to_thread(fuente.verificar)
            c.ruta_origen = getattr(fuente, "ruta", None) or fuente.descripcion
            c.log.info("Origen: %s", fuente.descripcion)
            t_descubrimiento = time.monotonic()
            inv = await asyncio.to_thread(descubrir, fuente, cfg.lista_incluir, cfg.lista_excluir)
        except FuenteNoDisponible as e:
            c.evento("ERROR", "ERROR_ACCESO", str(e))
            c.alertas.append(f"Drive no accesible: {e}")
            return

        c.log.info(
            "Descubiertos %d servidores, %d carpetas de respaldo",
            len(inv.servidores), sum(len(s.respaldos) for s in inv.servidores),
        )
        for e in inv.sueltos_raiz:
            c.evento("WARN", "NO_RECONOCIDO", "archivo suelto en la raiz de la carpeta compartida (no se descarga)", ruta=e.ruta_relativa)

        servidores_db = await self._sincronizar_servidores(c, inv.servidores)

        # Inventario: alta/actualizacion de respaldos y archivos; se separa lo
        # que ya esta terminado de lo que hay que evaluar/descargar.
        candidatos: list[tuple[ServidorOrigen, RespaldoOrigen]] = []
        for srv in inv.servidores:
            sdb = servidores_db[srv.nombre]
            res = c.reg.resumen(srv.nombre)
            if not sdb.habilitado:
                res.estado_general = "DESHABILITADO"
                res.observaciones.append("servidor deshabilitado en el panel")
                continue
            c.servidores += 1
            res.carpetas_detectadas = len(srv.respaldos)
            if srv.error:
                c.evento("ERROR", "ERROR_ACCESO", srv.error, servidor=srv.nombre)
                res.estado_general = "ERROR"
                res.observaciones.append(srv.error)
                continue
            if srv.vacio:
                res.estado_general = "VACIO"
                res.observaciones.append("sin respaldos en Drive")
            for r in srv.respaldos:
                if await self._sincronizar_respaldo(c, sdb, r, res):
                    candidatos.append((srv, r))

        # Segundo sondeo: una sola espera global, no una por respaldo
        if candidatos:
            espera = cfg.segundos_entre_sondeos - (time.monotonic() - t_descubrimiento)
            if espera > 0:
                c.log.info("Esperando %.0f s para el segundo sondeo de %d respaldos", espera, len(candidatos))
                await asyncio.sleep(espera)

        cola: list[tuple[RespaldoOrigen, Evaluacion]] = []
        for srv, r in candidatos:
            try:
                r2 = await asyncio.to_thread(resondear, fuente, r)
                if not r2.archivos:
                    raise FuenteNoDisponible("la carpeta quedo vacia o desaparecio entre sondeos")
                ev = await asyncio.to_thread(evaluar, r2, fuente, cfg, utcnow(), r2.firma() == r.firma())
            except FuenteNoDisponible as e:
                c.evento("ERROR", "ERROR_ACCESO", str(e), servidor=srv.nombre, ruta=r.ruta_relativa)
                c.reg.resumen(srv.nombre).estado_general = "ERROR"
                continue
            if r2.firma() != r.firma():
                await self._sincronizar_respaldo(c, servidores_db[srv.nombre], r2, c.reg.resumen(srv.nombre), contar=False)
            if ev.estado_origen == "NO_RECONOCIDO" and cfg.descargar_no_reconocidos:
                ev = Evaluacion("COMPLETO", "estructura no reconocida; copia sin verificacion de contenido")
            if ev.completo:
                cola.append((r2, ev))
            else:
                await self._marcar_no_listo(c, r2, ev)

        if c.modo == "inventario":
            for r, ev in cola:
                await self._actualizar_respaldo(r.ruta_relativa, estado_origen="COMPLETO", observacion=ev.motivo)
                for a in r.archivos:
                    self._fila(c, r, a, "DETECTADO", "SHA256" if a.nombre in ev.hashes else "LIMITADA",
                               esperado=ev.hashes.get(a.nombre, ""), mensaje="inventario: completo en origen, no se descarga")
            return

        # Cola: primero lo pequeno y antiguo; los respaldos de 50 GB al final
        cola.sort(key=lambda x: (x[0].tamano_total >= UMBRAL_GRANDE, x[0].fecha_backup or datetime.min, x[0].ruta_relativa))
        presupuesto = int(cfg.max_gb_por_ejecucion * 1024**3) if cfg.max_gb_por_ejecucion > 0 else None
        for r, ev in cola:
            if c.abortar:
                await self._marcar_no_listo(c, r, Evaluacion("PENDIENTE", "ejecucion detenida antes de llegar a este respaldo"))
                continue
            faltan = await self._bytes_pendientes(r)
            if presupuesto is not None and faltan > presupuesto and c.bytes_descargados > 0:
                await self._marcar_no_listo(
                    c, r, Evaluacion("PENDIENTE", f"limite de {cfg.max_gb_por_ejecucion:g} GB por ejecucion alcanzado; sigue en la proxima")
                )
                continue
            t0 = time.monotonic()
            antes = c.bytes_descargados
            await self._procesar_respaldo(c, fuente, r, ev)
            c.reg.resumen(r.servidor).duracion_segundos += time.monotonic() - t0
            if presupuesto is not None:
                presupuesto -= c.bytes_descargados - antes

        await self._revisar_faltantes(c, [s for s in servidores_db.values() if s.habilitado])

    # ------------------------------------------------------------- inventario
    async def _sincronizar_servidores(self, c: Corrida, servidores: list[ServidorOrigen]) -> dict[str, BackupServidor]:
        ahora = utcnow()
        async with self.sf() as s:
            existentes = {x.nombre: x for x in (await s.exec(select(BackupServidor))).all()}
            vistos = set()
            for srv in servidores:
                vistos.add(srv.nombre)
                sdb = existentes.get(srv.nombre)
                if sdb is None:
                    sdb = BackupServidor(nombre=srv.nombre)
                    s.add(sdb)
                    existentes[srv.nombre] = sdb
                    msg = "servidor nuevo detectado en Drive"
                    if srv.vacio:
                        msg += " (todavia sin respaldos)"
                    c.evento("WARN" if srv.vacio else "INFO", "SERVIDOR_NUEVO", msg, servidor=srv.nombre)
                    c.alertas.append(f"{srv.nombre}: {msg}")
                elif not sdb.en_origen:
                    c.evento("INFO", "SERVIDOR_REAPARECE", "la carpeta del servidor volvio a aparecer en Drive", servidor=srv.nombre)
                sdb.en_origen = True
                sdb.ultimo_acceso = ahora
                fechas = [r.fecha_backup for r in srv.respaldos if r.fecha_backup]
                if fechas:
                    sdb.ultimo_backup_fecha = max(fechas)
                if srv.error:
                    sdb.estado, sdb.observacion = "ERROR", srv.error
                elif srv.vacio:
                    sdb.estado, sdb.observacion = "VACIO", "sin respaldos en Drive"
                s.add(sdb)
            # Solo se marca "no encontrado" si se recorrio todo (sin filtro incluir)
            if not self.cfg.lista_incluir:
                for nombre, sdb in existentes.items():
                    if nombre not in vistos and nombre not in self.cfg.lista_excluir and sdb.en_origen:
                        sdb.en_origen = False
                        sdb.estado = "NO_ENCONTRADO"
                        sdb.observacion = "la carpeta ya no aparece en Drive; la copia local se conserva"
                        s.add(sdb)
                        c.evento("WARN", "SERVIDOR_DESAPARECE", sdb.observacion, servidor=nombre)
                        c.alertas.append(f"{nombre}: {sdb.observacion}")
            await s.commit()
            for sdb in existentes.values():
                await s.refresh(sdb)
            return existentes

    async def _sincronizar_respaldo(
        self, c: Corrida, sdb: BackupServidor, r: RespaldoOrigen, res: ResumenServidor, contar: bool = True
    ) -> bool:
        """Da de alta/actualiza el respaldo y sus archivos. Devuelve True si
        hay que evaluarlo/descargarlo; False si ya esta terminado (en ese
        caso agrega las filas YA_EXISTENTE al detalle)."""
        ahora = utcnow()
        async with self.sf() as s:
            rdb = (await s.exec(select(BackupRespaldo).where(BackupRespaldo.ruta_relativa == r.ruta_relativa))).first()
            nuevo_respaldo = rdb is None
            if rdb is None:
                rdb = BackupRespaldo(id_servidor_backup=sdb.id_servidor_backup, ruta_relativa=r.ruta_relativa, tipo=r.tipo)
                s.add(rdb)
                await s.flush()
            rdb.tipo = r.tipo
            rdb.plataforma = r.plataforma
            rdb.fecha_backup = r.fecha_backup
            rdb.con_manifiesto = r.manifiesto is not None
            rdb.total_archivos = len(r.archivos)
            rdb.tamano_total = r.tamano_total
            rdb.en_origen = True
            s.add(rdb)

            existentes = {
                a.ruta_relativa: a
                for a in (await s.exec(select(BackupArchivo).where(BackupArchivo.id_respaldo == rdb.id_respaldo))).all()
            }
            terminado = rdb.estado in ESTADOS_FINALES
            ya_existentes: list[tuple[Entrada, BackupArchivo]] = []
            for a in r.archivos:
                adb = existentes.get(a.ruta_relativa)
                if adb is None:
                    adb = BackupArchivo(
                        id_respaldo=rdb.id_respaldo, ruta_relativa=a.ruta_relativa, nombre=a.nombre,
                        tamano_origen=a.tamano, mtime_origen=a.mtime, estado="DETECTADO",
                    )
                    s.add(adb)
                    if contar:
                        c.archivos_nuevos += 1
                        res.archivos_nuevos += 1
                    terminado = False
                    continue
                adb.en_origen = True
                if adb.estado in ESTADOS_FINALES:
                    if adb.tamano_origen != a.tamano or adb.mtime_origen != a.mtime:
                        # Se conserva la copia validada; no se sobrescribe
                        msg = (f"el archivo cambio en Drive despues de copiarse "
                               f"({adb.tamano_origen} -> {a.tamano} bytes); se conserva la copia local validada")
                        if adb.ultimo_error != msg:
                            c.evento("WARN", "CONFLICTO_ORIGEN", msg, servidor=r.servidor, ruta=a.ruta_relativa)
                            c.alertas.append(f"{a.ruta_relativa}: {msg}")
                            adb.ultimo_error = msg
                    if not self._final(a.ruta_relativa).exists():
                        c.evento("WARN", "FALTA_LOCAL", "estaba validado pero ya no existe en D:; se vuelve a descargar",
                                 servidor=r.servidor, ruta=a.ruta_relativa)
                        adb.estado = "PENDIENTE"
                        adb.validado_en = None
                        terminado = False
                    else:
                        ya_existentes.append((a, adb))
                else:
                    adb.tamano_origen = a.tamano
                    adb.mtime_origen = a.mtime
                    terminado = False
                s.add(adb)
            # Archivos que estaban en la BD y ya no estan en Drive (se conservan en D:)
            vistos = {a.ruta_relativa for a in r.archivos}
            for ruta, adb in existentes.items():
                if ruta not in vistos and adb.en_origen:
                    adb.en_origen = False
                    s.add(adb)
            if terminado:
                rdb.estado_origen = "COMPLETO"
            await s.commit()

        if r.fecha_backup and (not res.ultimo_backup_fecha or fmt(r.fecha_backup) > res.ultimo_backup_fecha):
            res.ultimo_backup_fecha = fmt(r.fecha_backup)
        if not terminado:
            if nuevo_respaldo and contar:
                c.log.info("Nuevo respaldo: %s (%s, %d archivos, %.2f GB)", r.ruta_relativa, r.tipo, len(r.archivos), r.tamano_total / 1024**3)
            # "Reintentar fallidos" solo toca lo que quedo con error
            return c.modo != "reintentar" or rdb.estado in ESTADOS_ERROR

        for a, adb in ya_existentes:
            c.archivos_ya_existentes += 1
            res.archivos_ya_existentes += 1
            self._fila(c, r, a, "YA_EXISTENTE", "SHA256" if adb.estado == "VALIDADO" else "LIMITADA",
                       esperado=adb.hash_esperado or "", obtenido=adb.hash_local or "",
                       mensaje="copia local previamente " + ("validada con SHA-256" if adb.estado == "VALIDADO" else "verificada solo por tamano"))
        return False

    async def _marcar_interrumpidas(self, c: Corrida) -> None:
        limite = utcnow() - timedelta(minutes=self.cfg.minutos_ejecucion_bloqueada)
        async with self.sf() as s:
            viejas = (await s.exec(
                select(BackupEjecucion).where(
                    BackupEjecucion.estado == "EN_CURSO",
                    BackupEjecucion.id_ejecucion != c.id_ejecucion,
                    BackupEjecucion.ultimo_latido < limite,
                )
            )).all()
            for e in viejas:
                e.estado = "INTERRUMPIDA"
                e.fin = e.ultimo_latido
                e.resumen = (e.resumen or "") + " La ejecucion se interrumpio (PC apagado, suspendido o proceso cerrado)."
                s.add(e)
                c.evento("WARN", "EJECUCION_INTERRUMPIDA", f"la ejecucion #{e.id_ejecucion} quedo sin terminar; lo pendiente se retoma ahora")
            # Archivos que quedaron "EN_DESCARGA" por un corte: se reanudan desde el .part
            for a in (await s.exec(select(BackupArchivo).where(BackupArchivo.estado == "EN_DESCARGA"))).all():
                a.estado = "PENDIENTE"
                s.add(a)
            await s.commit()

    # ---------------------------------------------------------- no listos
    async def _marcar_no_listo(self, c: Corrida, r: RespaldoOrigen, ev: Evaluacion) -> None:
        estado = ev.estado_origen  # PENDIENTE | INCOMPLETO | NO_RECONOCIDO
        res = c.reg.resumen(r.servidor)
        nuevo = False
        async with self.sf() as s:
            rdb = (await s.exec(select(BackupRespaldo).where(BackupRespaldo.ruta_relativa == r.ruta_relativa))).first()
            if rdb is not None:
                nuevo = rdb.estado == "DETECTADO"
                anterior = rdb.observacion
                rdb.estado_origen = estado
                if rdb.estado not in ESTADOS_ERROR:
                    rdb.estado = estado
                rdb.observacion = ev.motivo
                s.add(rdb)
                for adb in (await s.exec(select(BackupArchivo).where(BackupArchivo.id_respaldo == rdb.id_respaldo))).all():
                    if adb.estado not in ESTADOS_FINALES | ESTADOS_ERROR | {"DESCARGADO"}:
                        adb.estado = "PENDIENTE" if estado == "PENDIENTE" else estado
                        s.add(adb)
                await s.commit()
            else:
                anterior = None

        for a in r.archivos:
            self._fila(c, r, a, "PENDIENTE" if estado == "PENDIENTE" else estado, "NINGUNA", mensaje=ev.motivo)
        c.archivos_pendientes += len(r.archivos)
        res.archivos_pendientes += len(r.archivos)

        if estado == "NO_RECONOCIDO":
            # Solo se alerta la primera vez; despues queda en el reporte
            if nuevo:
                c.evento("WARN", "NO_RECONOCIDO", f"{ev.motivo}; se registra pero no se descarga", servidor=r.servidor, ruta=r.ruta_relativa)
                c.alertas.append(f"{r.ruta_relativa}: estructura no reconocida")
            else:
                c.log.info("[NO_RECONOCIDO] %s (ya informado antes)", r.ruta_relativa)
            # No degrada el estado del servidor: queda visible en el reporte
            res.observaciones.append(f"{r.ruta_relativa}: no reconocido")
            return
        if estado == "INCOMPLETO":
            c.evento("WARN", "INCOMPLETO", ev.motivo, servidor=r.servidor, ruta=r.ruta_relativa)
            if anterior != ev.motivo:
                c.alertas.append(f"{r.ruta_relativa}: incompleto ({ev.motivo})")
        else:
            c.evento("INFO", "PENDIENTE", ev.motivo, servidor=r.servidor, ruta=r.ruta_relativa)
            c.advertencias.append(f"{r.ruta_relativa}: pendiente")
        if res.estado_general in ("OK", "VACIO"):
            res.estado_general = "PENDIENTE"
        res.observaciones.append(f"{r.ruta_relativa}: {ev.motivo}")

    # ---------------------------------------------------------- descarga
    async def _bytes_pendientes(self, r: RespaldoOrigen) -> int:
        async with self.sf() as s:
            finales = {
                a.ruta_relativa
                for a in (await s.exec(
                    select(BackupArchivo).where(BackupArchivo.ruta_relativa.in_([a.ruta_relativa for a in r.archivos]))
                )).all()
                if a.estado in ESTADOS_FINALES | {"DESCARGADO"}
            }
        return sum(a.tamano for a in r.archivos if a.ruta_relativa not in finales)

    async def _procesar_respaldo(self, c: Corrida, fuente: Fuente, r: RespaldoOrigen, ev: Evaluacion) -> None:
        res = c.reg.resumen(r.servidor)
        listos: list[tuple[Entrada, Path | None]] = []  # (archivo, staged o None si ya esta en la carpeta final)
        fallo: str | None = None
        c.log.info("Procesando %s (%d archivos, %.2f GB): %s", r.ruta_relativa, len(r.archivos), r.tamano_total / 1024**3, ev.motivo)

        ordenados = sorted(r.archivos, key=lambda x: x.tamano)
        for i, a in enumerate(ordenados):
            if c.abortar:
                fallo = fallo or "PENDIENTE"
                for resto in ordenados[i:]:
                    self._fila(c, r, resto, "PENDIENTE", "NINGUNA", mensaje="ejecucion detenida antes de copiarlo")
                c.archivos_pendientes += len(ordenados) - i
                res.archivos_pendientes += len(ordenados) - i
                break
            esperado = ev.hashes.get(a.nombre)
            verif = "SHA256" if esperado else "LIMITADA"
            final, staged = self._final(a.ruta_relativa), self._staged(a.ruta_relativa)
            part = staged.with_name(staged.name + ".part")
            adb = await self._archivo(a.ruta_relativa)
            await self._actualizar_archivo(a.ruta_relativa, hash_esperado=esperado)

            if adb.estado in ESTADOS_FINALES and final.exists():
                c.archivos_ya_existentes += 1
                res.archivos_ya_existentes += 1
                self._fila(c, r, a, "YA_EXISTENTE", verif, esperado or "", adb.hash_local or "", mensaje="copia local previamente verificada")
                listos.append((a, None))
                continue

            if final.exists():
                # Existe en D: pero sin evidencia en la BD: se verifica, nunca se sobrescribe
                h = await asyncio.to_thread(tr.sha256_archivo, final)
                ok = (h == esperado) if esperado else (final.stat().st_size == a.tamano)
                if ok:
                    await self._actualizar_archivo(
                        a.ruta_relativa, estado="VALIDADO" if esperado else "SIN_HASH", hash_local=h,
                        tamano_local=final.stat().st_size, validado_en=utcnow(), ultimo_error=None,
                    )
                    c.archivos_ya_existentes += 1
                    res.archivos_ya_existentes += 1
                    self._fila(c, r, a, "YA_EXISTENTE", verif, esperado or "", h, mensaje="ya existia en D:; se verifico y se adopta")
                    listos.append((a, None))
                else:
                    msg = "ya existe en D: con contenido distinto al de Drive; no se sobrescribe (revisar a mano)"
                    await self._actualizar_archivo(a.ruta_relativa, estado="ERROR_INTEGRIDAD", hash_local=h, ultimo_error=msg)
                    c.evento("ERROR", "CONFLICTO_LOCAL", msg, servidor=r.servidor, ruta=a.ruta_relativa)
                    c.alertas.append(f"{a.ruta_relativa}: {msg}")
                    c.archivos_fallidos += 1
                    res.archivos_fallidos += 1
                    self._fila(c, r, a, "ERROR_INTEGRIDAD", verif, esperado or "", h, mensaje=msg)
                    fallo = "ERROR_INTEGRIDAD"
                continue

            if (adb.estado == "DESCARGADO" and adb.hash_local and staged.exists()
                    and staged.stat().st_size == a.tamano and (not esperado or adb.hash_local == esperado)):
                self._fila(c, r, a, "DESCARGADO", verif, esperado or "", adb.hash_local,
                           mensaje="descargado en una ejecucion anterior; pendiente de publicar")
                listos.append((a, staged))
                continue

            inicio = datetime.now()
            resultado = await self._descargar(c, fuente, r, a, part, esperado)
            fin = datetime.now()
            if resultado is None:
                adb = await self._archivo(a.ruta_relativa)
                fallo = adb.estado if adb.estado in ESTADOS_ERROR else (fallo or "PENDIENTE")
                self._fila(c, r, a, adb.estado, verif, esperado or "", "", fmt(inicio), fmt(fin), adb.ultimo_error or "")
                continue
            os.replace(part, staged)
            await self._actualizar_archivo(
                a.ruta_relativa, estado="DESCARGADO", hash_local=resultado.sha256,
                tamano_local=resultado.tamano, ultimo_error=None,
            )
            c.archivos_descargados += 1
            res.archivos_descargados += 1
            res.bytes_descargados += resultado.bytes_transferidos
            nota = f"reanudado desde {resultado.reanudado_desde} bytes" if resultado.reanudado_desde else ""
            self._fila(c, r, a, "DESCARGADO", verif, esperado or "", resultado.sha256, fmt(inicio), fmt(fin), nota)
            listos.append((a, staged))

        if fallo or len(listos) != len(r.archivos):
            # Lo ya descargado queda en _TEMPORAL y se publica en la proxima ejecucion
            await self._actualizar_respaldo(r.ruta_relativa, estado=fallo or "PENDIENTE", estado_origen="COMPLETO")
            if res.estado_general in ("OK", "VACIO"):
                res.estado_general = "PENDIENTE"
            return

        # Publicacion: si la carpeta final no existe, se mueve la carpeta entera
        # (atomico dentro de D:); si existe, archivo por archivo sin sobrescribir.
        a_publicar = [(a, st) for a, st in listos if st is not None]
        try:
            carpeta_staged = self._staged(r.ruta_relativa)
            carpeta_final = self._final(r.ruta_relativa)
            todos_staged = len(a_publicar) == len(r.archivos) and r.ruta_relativa != r.servidor
            restos = [p for p in carpeta_staged.iterdir()] if carpeta_staged.exists() else []
            if todos_staged and not carpeta_final.exists() and len(restos) == len(a_publicar):
                carpeta_final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(carpeta_staged, carpeta_final)
            else:
                for a, st in a_publicar:
                    destino = self._final(a.ruta_relativa)
                    if destino.exists():
                        raise FileExistsError(f"{a.ruta_relativa} aparecio en D: antes de publicarse")
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(st, destino)
        except OSError as e:
            msg = f"no se pudo publicar en la carpeta final: {e}"
            c.evento("ERROR", "ERROR_PUBLICACION", msg, servidor=r.servidor, ruta=r.ruta_relativa)
            c.alertas.append(f"{r.ruta_relativa}: {msg}")
            await self._actualizar_respaldo(r.ruta_relativa, estado="ERROR_TRANSFERENCIA", observacion=msg)
            return

        ahora = utcnow()
        for a, st in a_publicar:
            sha = a.nombre in ev.hashes
            await self._actualizar_archivo(a.ruta_relativa, estado="VALIDADO" if sha else "SIN_HASH", validado_en=ahora)
            if sha:
                c.archivos_validados += 1
                res.archivos_validados += 1
        # Las filas del detalle pasan a su estado final
        for fila in c.reg.detalle:
            if fila.backup_id == r.ruta_relativa and fila.estado == "DESCARGADO":
                fila.estado = "VALIDADO" if fila.verificacion == "SHA256" else "SIN_HASH"
        estado = "VALIDADO" if ev.verificacion_sha else "SIN_HASH"
        await self._actualizar_respaldo(r.ruta_relativa, estado=estado, estado_origen="COMPLETO",
                                        publicado_en=ahora, observacion=ev.motivo)
        async with self.sf() as s:
            sdb = (await s.exec(select(BackupServidor).where(BackupServidor.nombre == r.servidor))).first()
            if sdb:
                sdb.ultima_copia_valida = ahora
                s.add(sdb)
                await s.commit()
        res.ultima_copia_valida = fmt(datetime.now().replace(microsecond=0))
        c.log.info("Publicado %s (%s)", r.ruta_relativa, estado)
        _limpiar_vacias(self._staged(r.ruta_relativa), self.cfg.destino_temporal)

    async def _descargar(self, c: Corrida, fuente: Fuente, r: RespaldoOrigen, a: Entrada, part: Path, esperado: str | None):
        cfg = self.cfg
        res = c.reg.resumen(r.servidor)
        intentos = max(1, cfg.reintentos)
        fallos_integridad = 0
        estado, error = "ERROR_TRANSFERENCIA", ""
        for intento in range(1, intentos + 1):
            ya = part.stat().st_size if part.exists() else 0
            try:
                await asyncio.to_thread(tr.comprobar_espacio, cfg.destino_raiz, max(0, a.tamano - ya), cfg, self._cache())
            except tr.SinEspacio as e:
                await self._actualizar_archivo(a.ruta_relativa, estado="SIN_ESPACIO", ultimo_error=str(e))
                c.evento("ERROR", "SIN_ESPACIO", f"{e}; se detienen las descargas (no se borra nada)", servidor=r.servidor, ruta=a.ruta_relativa)
                c.alertas.append(f"Sin espacio: {e}")
                c.archivos_fallidos += 1
                res.archivos_fallidos += 1
                res.estado_general = "ERROR"
                c.abortar = True
                return None

            await self._actualizar_archivo(a.ruta_relativa, estado="EN_DESCARGA", incrementar_intentos=True)
            c.progreso = [a.ruta_relativa, ya, a.tamano]
            c.log.info("Descargando %s (%.2f GB)%s, intento %d/%d", a.ruta_relativa, a.tamano / 1024**3,
                       f", reanudando desde {ya / 1024**3:.2f} GB" if ya else "", intento, intentos)

            def avance(n: int) -> None:
                c.progreso[1] += n

            try:
                resultado = await asyncio.to_thread(tr.copiar, fuente, a.ruta_relativa, part, a.tamano, cfg, avance)
            except tr.ErrorTransferencia as e:
                estado, error = "ERROR_TRANSFERENCIA", str(e)
            except FuenteNoDisponible as e:
                estado, error = "ERROR_ACCESO", str(e)
            else:
                c.bytes_descargados += resultado.bytes_transferidos
                if esperado and resultado.sha256 != esperado:
                    part.unlink(missing_ok=True)  # nunca se publica; se vuelve a bajar
                    fallos_integridad += 1
                    estado = "ERROR_INTEGRIDAD"
                    error = f"SHA-256 no coincide con el manifiesto (esperado {esperado}, obtenido {resultado.sha256})"
                else:
                    c.progreso = ["", 0, 0]
                    return resultado
            finally:
                c.progreso[0] = ""

            c.log.warning("Intento %d/%d fallido para %s: %s", intento, intentos, a.ruta_relativa, error)
            if estado == "ERROR_INTEGRIDAD" and fallos_integridad >= MAX_FALLOS_INTEGRIDAD:
                break
            if intento < intentos:
                try:
                    await asyncio.to_thread(fuente.verificar)
                except FuenteNoDisponible as e:
                    estado, error = "ERROR_ACCESO", str(e)
                    break
                await asyncio.sleep(min(cfg.espera_reintento_segundos * 2 ** (intento - 1), 900))

        await self._actualizar_archivo(a.ruta_relativa, estado=estado, ultimo_error=error)
        c.evento("ERROR", estado, error, servidor=r.servidor, ruta=a.ruta_relativa)
        c.alertas.append(f"{a.ruta_relativa}: {estado} - {error[:150]}")
        c.archivos_fallidos += 1
        res.archivos_fallidos += 1
        res.estado_general = "ERROR"
        if estado == "ERROR_ACCESO":
            c.abortar = True  # sin Drive no tiene sentido seguir
        return None

    # ------------------------------------------------------------- faltantes
    async def _revisar_faltantes(self, c: Corrida, servidores: list[BackupServidor]) -> None:
        """Alerta si un (servidor, plataforma, tipo) no tiene respaldo nuevo
        dentro de su ritmo habitual (diario, semanal...), con un minimo de
        BACKUPS_HORAS_SIN_BACKUP_ALERTA horas."""
        ids = {s.id_servidor_backup: s.nombre for s in servidores}
        if not ids:
            return
        async with self.sf() as s:
            filas = (await s.exec(
                select(BackupRespaldo.id_servidor_backup, BackupRespaldo.plataforma, BackupRespaldo.tipo, BackupRespaldo.fecha_backup)
                .where(BackupRespaldo.id_servidor_backup.in_(list(ids)), BackupRespaldo.fecha_backup.is_not(None),
                       BackupRespaldo.en_origen.is_(True), BackupRespaldo.tipo != "desconocido")
            )).all()
        grupos: dict[tuple, list[datetime]] = {}
        for id_srv, plat, tipo, fecha in filas:
            grupos.setdefault((ids[id_srv], plat or "-", tipo), []).append(fecha)
        ahora = datetime.now()
        minimo = timedelta(hours=self.cfg.horas_sin_backup_alerta)
        for (servidor, plat, tipo), fechas in sorted(grupos.items()):
            fechas.sort()
            # Se ignoran las repeticiones del mismo dia (p. ej. corridas manuales
            # o 02:00 y 04:00); con menos de 3 huecos no hay ritmo confiable y se
            # asume semanal, para no alertar a diario por copias puntuales.
            huecos = [b - a for a, b in zip(fechas, fechas[1:]) if b - a >= timedelta(hours=20)]
            ritmo = statistics.median(huecos) if len(huecos) >= 3 else timedelta(days=7)
            umbral = max(minimo, ritmo * 1.5)
            atraso = ahora - fechas[-1]
            if atraso > umbral:
                msg = (f"sin respaldo nuevo de '{plat}' ({tipo}) desde {fechas[-1]:%d/%m/%Y %H:%M} "
                       f"(ritmo habitual: cada {_duracion(ritmo)})")
                c.evento("WARN", "BACKUP_FALTANTE", msg, servidor=servidor)
                c.alertas.append(f"{servidor}: {msg}")
                res = c.reg.resumen(servidor)
                res.observaciones.append(msg)
                if res.estado_general == "OK":
                    res.estado_general = "PENDIENTE"

    # ---------------------------------------------------------------- cierre
    async def _cerrar(self, c: Corrida) -> str:
        if c.errores:
            estado = "ERROR"
        elif c.advertencias or c.alertas:
            estado = "CON_ADVERTENCIAS"
        else:
            estado = "OK"

        uso = libre = total = None
        try:
            uso = await asyncio.to_thread(tr.tamano_directorio, self.cfg.destino_backups)
            libre = tr.espacio_libre(self.cfg.destino_raiz)
            total = tr.espacio_total(self.cfg.destino_raiz)
        except OSError:
            pass

        resumen = {
            "estado": estado,
            "servidores": c.servidores,
            "archivos_nuevos": c.archivos_nuevos,
            "archivos_descargados": c.archivos_descargados,
            "archivos_validados": c.archivos_validados,
            "archivos_ya_existentes": c.archivos_ya_existentes,
            "archivos_pendientes": c.archivos_pendientes,
            "archivos_fallidos": c.archivos_fallidos,
            "bytes_descargados": c.bytes_descargados,
            "errores": len(c.errores),
            "advertencias": len(c.advertencias),
            "espacio_libre_destino": libre,
            "espacio_usado_backups": uso,
            "duracion_segundos": round(time.monotonic() - c.inicio, 1),
        }
        c.log.info(
            "Resumen: %s | nuevos %d, descargados %d (%.2f GB), validados %d, ya existentes %d, pendientes %d, fallidos %d",
            estado, c.archivos_nuevos, c.archivos_descargados, c.bytes_descargados / 1024**3,
            c.archivos_validados, c.archivos_ya_existentes, c.archivos_pendientes, c.archivos_fallidos,
        )
        try:
            carpeta = c.reg.cerrar(estado, resumen)
        except OSError as e:
            carpeta = None
            print(f"Respaldos: no se pudo escribir el registro diario: {e}")

        if c.id_ejecucion is not None:
            try:
                async with self.sf() as s:
                    e = await s.get(BackupEjecucion, c.id_ejecucion)
                    if e:
                        e.fin = utcnow()
                        e.estado = estado
                        for k in ("servidores", "archivos_nuevos", "archivos_descargados", "archivos_validados",
                                  "archivos_ya_existentes", "archivos_pendientes", "archivos_fallidos", "bytes_descargados"):
                            setattr(e, k, getattr(c, k))
                        e.espacio_libre_destino = libre
                        e.espacio_usado_backups = uso
                        e.ruta_registros = ruta_visible(carpeta) if carpeta else None
                        e.resumen = _texto_resumen(c, estado)
                        e.log = c.reg.texto_log()[-MAX_LOG_EN_BD:]
                        s.add(e)
                    for ev in c.eventos:
                        s.add(ev)
                    await s.commit()
                self.ultimo_uso = uso
                self.ultimo_origen = (c.ruta_origen, c.ruta_origen is not None)
                await self.latido_agente("INACTIVO", c.ruta_origen, accesible=c.ruta_origen is not None, usado=uso)
                await self._retencion()
            except Exception as ex:  # noqa: BLE001
                print(f"Respaldos: no se pudo guardar el resultado en la base: {ex}")

            # Hoja compartida en Google Drive (nunca rompe la ejecucion)
            try:
                from app.backups import hoja

                ruta = await hoja.generar(self.sf, self.cfg, self.estado_agente("INACTIVO", usado=uso))
                if ruta:
                    print(f"Respaldos: hoja actualizada en {ruta}")
            except Exception as ex:  # noqa: BLE001
                aviso = f"no se pudo actualizar la hoja compartida ({type(ex).__name__}: {str(ex)[:200]})"
                print(f"Respaldos: {aviso}")
                c.alertas.append(aviso)
                try:
                    async with self.sf() as s:
                        e = await s.get(BackupEjecucion, c.id_ejecucion)
                        if e:
                            e.resumen = f"{e.resumen or ''} AVISO: {aviso}."
                            s.add(e)
                            await s.commit()
                except Exception:  # noqa: BLE001
                    pass

        if self.notificar and (estado == "ERROR" or c.alertas):
            try:
                await self.notificar(_texto_telegram(c, estado))
            except Exception as ex:  # noqa: BLE001 - Telegram nunca rompe la ejecucion
                print(f"Respaldos: no se pudo enviar la alerta: {ex}")
        return estado

    # --------------------------------------------------------------- BD util
    async def _crear_ejecucion(self, tipo: str, id_solicitud: int | None) -> int:
        async with self.sf() as s:
            e = BackupEjecucion(tipo=tipo, host=HOST)
            s.add(e)
            await s.flush()
            if id_solicitud:
                sol = await s.get(BackupSolicitud, id_solicitud)
                if sol:
                    sol.id_ejecucion = e.id_ejecucion
                    s.add(sol)
            await s.commit()
            return e.id_ejecucion

    async def _archivo(self, ruta: str) -> BackupArchivo:
        async with self.sf() as s:
            return (await s.exec(select(BackupArchivo).where(BackupArchivo.ruta_relativa == ruta))).one()

    async def _actualizar_archivo(self, ruta: str, incrementar_intentos: bool = False, **campos) -> None:
        async with self.sf() as s:
            a = (await s.exec(select(BackupArchivo).where(BackupArchivo.ruta_relativa == ruta))).one()
            for k, v in campos.items():
                if k == "hash_esperado" and v is None:
                    continue
                setattr(a, k, v)
            if incrementar_intentos:
                a.intentos += 1
            a.actualizado_en = utcnow()
            s.add(a)
            await s.commit()

    async def _actualizar_respaldo(self, ruta: str, **campos) -> None:
        async with self.sf() as s:
            r = (await s.exec(select(BackupRespaldo).where(BackupRespaldo.ruta_relativa == ruta))).first()
            if r is None:
                return
            for k, v in campos.items():
                setattr(r, k, v)
            s.add(r)
            await s.commit()

    def _fila(self, c: Corrida, r: RespaldoOrigen, a: Entrada, estado: str, verificacion: str,
              esperado: str = "", obtenido: str = "", inicio: str = "", fin: str = "", mensaje: str = "") -> None:
        c.reg.agregar(FilaDetalle(
            servidor=r.servidor, plataforma=r.plataforma, backup_id=r.ruta_relativa,
            ruta_relativa=a.ruta_relativa, nombre_archivo=a.nombre, tamano_bytes=a.tamano,
            fecha_modificacion=fmt(_a_local(a.mtime)), estado=estado, verificacion=verificacion,
            hash_esperado_sha256=esperado, hash_obtenido_sha256=obtenido,
            fecha_inicio=inicio, fecha_fin=fin, mensaje_error=mensaje,
        ))

    async def _latidos(self, c: Corrida) -> None:
        ultimo_log = time.monotonic()
        while True:
            await asyncio.sleep(60)
            try:
                async with self.sf() as s:
                    e = await s.get(BackupEjecucion, c.id_ejecucion)
                    if e:
                        e.ultimo_latido = utcnow()
                        s.add(e)
                        await s.commit()
                await self.latido_agente("EJECUTANDO", c.ruta_origen, accesible=c.ruta_origen is not None)
            except Exception:  # noqa: BLE001 - el latido es best-effort
                pass
            ruta, hechos, total = c.progreso
            if ruta and time.monotonic() - ultimo_log >= 300:
                ultimo_log = time.monotonic()
                c.log.info("  ... %s: %.2f / %.2f GB", ruta, hechos / 1024**3, total / 1024**3)

    def estado_agente(self, estado: str, usado: int | None = None, proxima: datetime | None = None) -> dict:
        """Datos del latido (espacio en disco, config...). No toca la base."""
        cfg = self.cfg
        libre = total = libre_cache = None
        try:
            libre = tr.espacio_libre(cfg.destino_raiz)
            total = tr.espacio_total(cfg.destino_raiz)
            cache = self._cache()
            if cache and cache.exists():
                libre_cache = tr.espacio_libre(cache)
        except OSError:
            pass
        ruta_origen, accesible = self.ultimo_origen
        return {
            "host": HOST,
            "estado": estado,
            "ruta_origen": ruta_origen,
            "origen_accesible": accesible,
            "ruta_destino": ruta_visible(cfg.destino_backups),
            "espacio_libre_destino": libre,
            "espacio_total_destino": total,
            "espacio_usado_backups": usado if usado is not None else self.ultimo_uso,
            "espacio_libre_cache": libre_cache,
            "proxima_ejecucion": proxima,
            "configuracion": cfg.para_panel(),
        }

    async def latido_agente(self, estado: str, ruta_origen: str | None, accesible: bool,
                            usado: int | None = None, proxima: datetime | None = None) -> None:
        """Guarda el latido en la base. Solo se usa al empezar/terminar y durante
        una ejecucion (la base ya esta despierta); en reposo el agente late por
        HTTP contra el backend para no despertar a Neon (ver agente.py)."""
        datos = self.estado_agente(estado, usado, proxima)
        async with self.sf() as s:
            ag = await s.get(BackupAgente, HOST)
            if ag is None:
                ag = BackupAgente(host=HOST)
            ag.ultimo_latido = utcnow()
            ag.estado = estado
            if ruta_origen is not None or not accesible:
                ag.ruta_origen = ruta_origen or ag.ruta_origen
                ag.origen_accesible = accesible
            for campo in ("ruta_destino", "espacio_libre_destino", "espacio_total_destino", "espacio_libre_cache"):
                setattr(ag, campo, datos[campo])
            if usado is not None:
                ag.espacio_usado_backups = usado
            if proxima is not None:
                ag.proxima_ejecucion = proxima
            ag.configuracion = json.dumps(datos["configuracion"], ensure_ascii=False)
            s.add(ag)
            await s.commit()

    async def _retencion(self) -> None:
        """Mantiene chica la base (Neon gratuito: 0,5 GB): el texto del log solo
        se conserva para las ultimas ejecuciones y los eventos por unos meses.
        Los registros completos siguen en D:\\Backups\\Registros."""
        async with self.sf() as s:
            ids = (await s.exec(
                select(BackupEjecucion.id_ejecucion).order_by(BackupEjecucion.id_ejecucion.desc()).limit(LOGS_EN_BD)
            )).all()
            if len(ids) == LOGS_EN_BD:
                await s.exec(
                    update(BackupEjecucion)
                    .where(BackupEjecucion.id_ejecucion < min(ids), BackupEjecucion.log.is_not(None))
                    .values(log=None)
                )
            await s.exec(delete(BackupEvento).where(BackupEvento.fecha_hora < utcnow() - timedelta(days=DIAS_EVENTOS)))
            await s.commit()


def _a_local(utc: datetime) -> datetime:
    from datetime import timezone

    return utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _duracion(td: timedelta) -> str:
    horas = td.total_seconds() / 3600
    return f"{horas / 24:.1f} dias" if horas >= 36 else f"{horas:.0f} h"


def _limpiar_vacias(carpeta: Path, tope: Path) -> None:
    """Borra carpetas temporales VACIAS que quedaron tras publicar (nunca archivos)."""
    p = carpeta
    try:
        while p != tope and tope in p.parents:
            if p.exists():
                if any(p.iterdir()):
                    break
                p.rmdir()
            p = p.parent
    except OSError:
        pass


def _texto_resumen(c: Corrida, estado: str) -> str:
    partes = [
        f"{estado}: {c.servidores} servidores, {c.archivos_nuevos} archivos nuevos, "
        f"{c.archivos_descargados} descargados ({c.bytes_descargados / 1024**3:.2f} GB), "
        f"{c.archivos_validados} validados SHA-256, {c.archivos_ya_existentes} ya existentes, "
        f"{c.archivos_pendientes} pendientes, {c.archivos_fallidos} fallidos."
    ]
    if c.errores:
        partes.append("Errores: " + " | ".join(c.errores[:5]))
    return " ".join(partes)


def _texto_telegram(c: Corrida, estado: str) -> str:
    icono = "\U0001F534" if estado == "ERROR" else "⚠️"
    lineas = [
        f"{icono} <b>Respaldos Drive → D:</b> ejecucion #{c.id_ejecucion or '-'} ({escape(c.tipo)}): <b>{estado}</b>",
        escape(f"Descargados {c.archivos_descargados} ({c.bytes_descargados / 1024**3:.2f} GB), "
               f"pendientes {c.archivos_pendientes}, fallidos {c.archivos_fallidos}"),
        "",
    ]
    for a in c.alertas[:12]:
        lineas.append("• " + escape(a[:250]))
    if len(c.alertas) > 12:
        lineas.append(f"… y {len(c.alertas) - 12} mas (ver el panel / registro del dia)")
    return "\n".join(lineas)


def proxima_ejecucion(cfg: BackupSettings, ahora: datetime | None = None) -> datetime:
    ahora = ahora or datetime.now()
    h, m = (int(x) for x in cfg.hora_diaria.split(":"))
    hoy = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
    return hoy if ahora < hoy else hoy + timedelta(days=1)
