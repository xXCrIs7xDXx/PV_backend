"""Hoja de calculo de registro para compartir con el equipo.

Al final de cada ejecucion se regenera un .xlsx DENTRO de la unidad de
Google Drive (BACKUPS_HOJA_RUTA, p. ej. G:\\Mi unidad\\Respaldos DTIC\\...).
Drive para escritorio lo sube solo; en drive.google.com se abre con Google
Sheets y se comparte por enlace. El archivo se sobrescribe en el mismo lugar
(no se borra y recrea), asi Drive conserva el mismo enlace y solo agrega una
version nueva.

No requiere credenciales ni la API de Google. Limitacion: es un archivo de
Excel abierto con Sheets, no una hoja nativa; lo que alguien edite a mano se
pierde en la siguiente ejecucion (es un reporte, no un formulario).

Pestanas:
  Resumen            estado actual: agente, ultima ejecucion, disco, servidores
  Registro diario    una fila por ejecucion (tambien las que no trajeron nada)
  Copias por dia     que se copio cada dia, por servidor
  Respaldos          inventario: una fila por respaldo
  Archivos copiados  una fila por archivo copiado (con su SHA-256)
  Avisos             advertencias y errores recientes
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from app.backups.config import BackupSettings, ruta_extendida, ruta_visible
from app.models import BackupArchivo, BackupEjecucion, BackupEvento, BackupRespaldo, BackupServidor

FINALES = ("VALIDADO", "SIN_HASH")
ETIQUETAS = {
    "VALIDADO": "Validado (SHA-256)",
    "SIN_HASH": "Copiado (verificado por tamaño)",
    "DESCARGADO": "Descargado, sin validar",
    "PENDIENTE": "Pendiente",
    "DETECTADO": "Detectado",
    "INCOMPLETO": "Incompleto en Drive",
    "NO_RECONOCIDO": "No reconocido (no se copia)",
    "EN_DESCARGA": "En descarga",
    "ERROR_INTEGRIDAD": "ERROR de integridad",
    "ERROR_TRANSFERENCIA": "ERROR de transferencia",
    "ERROR_ACCESO": "ERROR de acceso",
    "SIN_ESPACIO": "ERROR: sin espacio",
    "OK": "OK",
    "CON_ADVERTENCIAS": "Con advertencias",
    "ERROR": "ERROR",
    "EN_CURSO": "En curso",
    "INTERRUMPIDA": "Interrumpida",
    "VACIO": "Sin respaldos en Drive",
    "NO_ENCONTRADO": "Ya no está en Drive",
    "DESHABILITADO": "Deshabilitado",
}
VERDE, AMBAR, ROJO, GRIS = "C6EFCE", "FFEB9C", "FFC7CE", "E7E6E6"


def _color(estado: str) -> str | None:
    if estado in ("VALIDADO", "SIN_HASH", "OK"):
        return VERDE
    if estado.startswith("ERROR") or estado in ("SIN_ESPACIO",):
        return ROJO
    if estado in ("PENDIENTE", "DETECTADO", "INCOMPLETO", "CON_ADVERTENCIAS", "DESCARGADO", "EN_DESCARGA",
                  "EN_CURSO", "INTERRUMPIDA"):
        return AMBAR
    if estado in ("NO_RECONOCIDO", "VACIO", "NO_ENCONTRADO", "DESHABILITADO"):
        return GRIS
    return None


def _local(utc: datetime | None) -> datetime | None:
    """UTC naive (como se guarda en la base) -> hora local de la PC (Bolivia)."""
    if utc is None:
        return None
    return utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None, microsecond=0)


def _gb(n: int | None) -> float | None:
    return round(n / 1024**3, 3) if n is not None else None


async def _datos(sf, cfg: BackupSettings) -> dict:
    async with sf() as s:
        servidores = (await s.exec(select(BackupServidor).order_by(BackupServidor.nombre))).all()
        nombre_srv = {x.id_servidor_backup: x.nombre for x in servidores}
        respaldos = (await s.exec(
            select(BackupRespaldo).order_by(BackupRespaldo.fecha_backup.desc().nulls_last(), BackupRespaldo.ruta_relativa)
        )).all()
        por_respaldo = {r.id_respaldo: r for r in respaldos}
        archivos = (await s.exec(
            select(BackupArchivo).where(BackupArchivo.estado.in_(FINALES)).order_by(BackupArchivo.validado_en.desc())
        )).all()
        ejecuciones = (await s.exec(select(BackupEjecucion).order_by(BackupEjecucion.inicio.desc()).limit(730))).all()
        eventos = (await s.exec(
            select(BackupEvento).where(BackupEvento.nivel.in_(("WARN", "ERROR"))).order_by(BackupEvento.id_evento.desc()).limit(1000)
        )).all()
    return {"servidores": servidores, "nombre_srv": nombre_srv, "respaldos": respaldos, "por_respaldo": por_respaldo,
            "archivos": archivos, "ejecuciones": ejecuciones, "eventos": eventos}


def _escribir(d: dict, cfg: BackupSettings, agente: dict | None, destino_tmp: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    negrita = Font(bold=True)
    cabecera = PatternFill("solid", fgColor="1F3864")
    blanco = Font(bold=True, color="FFFFFF")
    F_FECHA, F_GB = "dd/mm/yyyy hh:mm", "#,##0.00"

    def tabla(ws, columnas: list[tuple[str, int]], filas: list[list], fila_cab: int = 1, col_estado: int | None = None,
              formatos: dict[int, str] | None = None) -> None:
        for i, (titulo, ancho) in enumerate(columnas, start=1):
            c = ws.cell(row=fila_cab, column=i, value=titulo)
            c.font, c.fill = blanco, cabecera
            c.alignment = Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = ancho
        for n, fila in enumerate(filas, start=fila_cab + 1):
            for i, valor in enumerate(fila, start=1):
                c = ws.cell(row=n, column=i, value=valor)
                if formatos and i in formatos and valor is not None:
                    c.number_format = formatos[i]
                elif isinstance(valor, datetime):
                    c.number_format = F_FECHA
            if col_estado:
                crudo = fila[col_estado - 1]
                color = _color(crudo) if isinstance(crudo, str) else None
                c = ws.cell(row=n, column=col_estado, value=ETIQUETAS.get(crudo, crudo))
                if color:
                    c.fill = PatternFill("solid", fgColor=color)
        ws.freeze_panes = ws.cell(row=fila_cab + 1, column=1)
        if filas:
            ws.auto_filter.ref = f"A{fila_cab}:{get_column_letter(len(columnas))}{fila_cab + len(filas)}"

    nombre_srv, por_respaldo = d["nombre_srv"], d["por_respaldo"]

    # ----------------------------------------------------------- Resumen
    ws = wb.active
    ws.title = "Resumen"
    ultima = d["ejecuciones"][0] if d["ejecuciones"] else None
    bytes_copiados = sum(a.tamano_local or 0 for a in d["archivos"])
    ws["A1"] = "Registro de respaldos: Google Drive (Backupumsa2627) → disco D:"
    ws["A1"].font = Font(bold=True, size=14)
    pares = [
        ("Actualizado", datetime.now().replace(microsecond=0)),
        ("Generado por", "Agente de respaldos del sistema de monitoreo DTIC (se regenera en cada ejecución; no editar a mano)"),
        ("Origen (Drive)", (agente or {}).get("ruta_origen") or cfg.carpeta_compartida),
        ("Destino (PC)", ruta_visible(cfg.destino_backups)),
        ("Ejecución diaria", f"{cfg.hora_diaria} (hora de Bolivia)"),
        ("Última ejecución", _local(ultima.inicio) if ultima else "sin ejecuciones"),
        ("Resultado", ETIQUETAS.get(ultima.estado, ultima.estado) if ultima else ""),
        ("Detalle", ultima.resumen if ultima else ""),
        ("Total copiado en D: (GB)", _gb(bytes_copiados)),
        ("Espacio libre en D: (GB)", _gb((agente or {}).get("espacio_libre_destino"))),
    ]
    for n, (k, v) in enumerate(pares, start=3):
        ws.cell(row=n, column=1, value=k).font = negrita
        c = ws.cell(row=n, column=2, value=v)
        if isinstance(v, datetime):
            c.number_format = F_FECHA
            c.alignment = Alignment(horizontal="left")
        elif isinstance(v, float):
            c.number_format = F_GB
            c.alignment = Alignment(horizontal="left")
    if ultima:
        color = _color(ultima.estado)
        if color:
            ws.cell(row=9, column=2).fill = PatternFill("solid", fgColor=color)

    stats: dict[int, dict[str, int]] = {}
    for r in d["respaldos"]:
        st = stats.setdefault(r.id_servidor_backup, {"total": 0, "sha": 0, "tam": 0, "pend": 0, "err": 0, "bytes": 0})
        st["total"] += 1
        st["bytes"] += r.tamano_total or 0
        if r.estado == "VALIDADO":
            st["sha"] += 1
        elif r.estado == "SIN_HASH":
            st["tam"] += 1
        elif r.estado.startswith("ERROR") or r.estado == "SIN_ESPACIO":
            st["err"] += 1
        elif r.estado != "NO_RECONOCIDO":
            st["pend"] += 1
    filas = []
    for sv in d["servidores"]:
        st = stats.get(sv.id_servidor_backup, {})
        if not sv.habilitado:
            estado = "DESHABILITADO"
        elif sv.estado in ("VACIO", "NO_ENCONTRADO", "ERROR"):
            estado = sv.estado
        elif st.get("err"):
            estado = "ERROR"
        elif st.get("pend"):
            estado = "PENDIENTE"
        else:
            estado = "OK"
        filas.append([sv.nombre, estado, sv.ultimo_backup_fecha, _local(sv.ultima_copia_valida), st.get("total", 0),
                      st.get("sha", 0), st.get("tam", 0), st.get("pend", 0), st.get("err", 0), _gb(st.get("bytes", 0)),
                      sv.observacion])
    cab = len(pares) + 5
    ws.cell(row=cab - 1, column=1, value="Servidores").font = Font(bold=True, size=12)
    tabla(ws, [("Servidor", 44), ("Estado", 60), ("Último respaldo en Drive", 22), ("Última copia en D:", 20),
               ("Respaldos", 11), ("Validados SHA-256", 12), ("Solo tamaño", 10), ("Pendientes", 11), ("Con error", 10),
               ("GB en Drive", 12), ("Observación", 60)], filas, fila_cab=cab, col_estado=2, formatos={10: F_GB})
    ws.freeze_panes = None
    ws.auto_filter.ref = None
    ws.column_dimensions["B"].width = 60

    # --------------------------------------------------- Registro diario
    ws = wb.create_sheet("Registro diario")
    filas = [[e.id_ejecucion, _local(e.inicio), _local(e.fin), e.tipo, e.estado, e.servidores, e.archivos_nuevos,
              e.archivos_descargados, _gb(e.bytes_descargados), e.archivos_validados, e.archivos_ya_existentes,
              e.archivos_pendientes, e.archivos_fallidos, _gb(e.espacio_libre_destino), e.resumen]
             for e in d["ejecuciones"]]
    tabla(ws, [("N.º", 7), ("Inicio", 18), ("Fin", 18), ("Tipo", 12), ("Resultado", 20), ("Servidores", 11),
               ("Archivos nuevos en Drive", 14), ("Archivos copiados", 12), ("GB copiados", 12),
               ("Validados SHA-256", 12), ("Ya estaban en D:", 12), ("Pendientes", 11), ("Fallidos", 9),
               ("GB libres en D:", 13), ("Resumen", 110)], filas, col_estado=5, formatos={9: F_GB, 14: F_GB})

    # ----------------------------------------------------- Copias por dia
    ws = wb.create_sheet("Copias por día")
    dias: dict[tuple, dict] = {}
    for a in d["archivos"]:
        r = por_respaldo.get(a.id_respaldo)
        cuando = _local(a.validado_en)
        if r is None or cuando is None:
            continue
        g = dias.setdefault((cuando.date(), nombre_srv.get(r.id_servidor_backup, "?")),
                            {"respaldos": set(), "archivos": 0, "sha": 0, "tam": 0, "bytes": 0})
        g["respaldos"].add(a.id_respaldo)
        g["archivos"] += 1
        g["bytes"] += a.tamano_local or 0
        g["sha" if a.estado == "VALIDADO" else "tam"] += 1
    filas = [[datetime(dia.year, dia.month, dia.day), srv, len(g["respaldos"]), g["archivos"], _gb(g["bytes"]), g["sha"], g["tam"]]
             for (dia, srv), g in sorted(dias.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=True)]
    tabla(ws, [("Día de la copia", 16), ("Servidor", 44), ("Respaldos copiados", 12), ("Archivos", 10),
               ("GB copiados", 12), ("Archivos validados SHA-256", 14), ("Archivos verificados solo por tamaño", 16)],
          filas, formatos={1: "dd/mm/yyyy", 5: F_GB})

    # ---------------------------------------------------------- Respaldos
    ws = wb.create_sheet("Respaldos")
    filas = [[nombre_srv.get(r.id_servidor_backup, "?"), r.plataforma, r.fecha_backup, r.ruta_relativa, r.estado,
              r.total_archivos, _gb(r.tamano_total), "Sí" if r.con_manifiesto else "No", _local(r.publicado_en),
              "Sí" if r.en_origen else "No (se conserva en D:)", r.observacion] for r in d["respaldos"]]
    tabla(ws, [("Servidor", 40), ("Plataforma", 22), ("Fecha del respaldo", 18), ("Carpeta en Drive", 70),
               ("Estado de la copia", 32), ("Archivos", 9), ("GB", 10), ("Trae manifiesto SHA-256", 12),
               ("Copiado a D: el", 18), ("Sigue en Drive", 20), ("Observación", 70)], filas, col_estado=5, formatos={7: F_GB})

    # --------------------------------------------------- Archivos copiados
    ws = wb.create_sheet("Archivos copiados")
    filas = []
    for a in d["archivos"][: cfg.hoja_max_archivos]:
        r = por_respaldo.get(a.id_respaldo)
        filas.append([_local(a.validado_en), nombre_srv.get(r.id_servidor_backup, "?") if r else "?",
                      r.plataforma if r else None, r.fecha_backup if r else None, a.nombre, a.tamano_local,
                      a.estado, a.hash_local, a.ruta_relativa])
    tabla(ws, [("Copiado el", 18), ("Servidor", 40), ("Plataforma", 22), ("Fecha del respaldo", 18), ("Archivo", 60),
               ("Tamaño (bytes)", 16), ("Verificación", 32), ("SHA-256 de la copia en D:", 68), ("Ruta", 90)],
          filas, col_estado=7, formatos={6: "#,##0"})

    # -------------------------------------------------------------- Avisos
    ws = wb.create_sheet("Avisos")
    filas = [[_local(e.fecha_hora), e.id_ejecucion, "Error" if e.nivel == "ERROR" else "Advertencia", e.codigo,
              e.servidor, e.ruta_relativa, e.mensaje] for e in d["eventos"]]
    tabla(ws, [("Fecha", 18), ("Ejecución", 10), ("Nivel", 13), ("Código", 24), ("Servidor", 36), ("Ruta", 60),
               ("Mensaje", 120)], filas)

    destino_tmp.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino_tmp)


async def generar(sf, cfg: BackupSettings, agente: dict | None = None) -> str | None:
    """Regenera la hoja. Devuelve la ruta visible, o None si no esta configurada.
    Lanza OSError si no se puede escribir (Drive cerrado, archivo abierto...)."""
    if not cfg.hoja_ruta:
        return None
    destino = ruta_extendida(Path(cfg.hoja_ruta))
    datos = await _datos(sf, cfg)
    tmp = cfg.destino_temporal / ".hoja_registro.xlsx"

    def _trabajo() -> None:
        _escribir(datos, cfg, agente, tmp)
        destino.parent.mkdir(parents=True, exist_ok=True)
        # Se sobrescribe el contenido del MISMO archivo: Drive mantiene el enlace
        with open(tmp, "rb") as origen, open(destino, "wb") as salida:
            shutil.copyfileobj(origen, salida)
        tmp.unlink(missing_ok=True)

    await asyncio.to_thread(_trabajo)
    return ruta_visible(destino)
