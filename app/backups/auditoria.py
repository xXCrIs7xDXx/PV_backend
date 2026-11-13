"""Registro diario en D:\\Backups\\Registros\\<AAAA-MM-DD>\\:

  ejecucion.log         texto legible (se agregan todas las ejecuciones del dia)
  resumen_diario.csv    una fila por servidor y ejecucion
  detalle_archivos.csv  una fila por archivo revisado en la ejecucion
  resumen_diario.json   lista de ejecuciones del dia con su resumen y detalle

Se escribe SIEMPRE, aunque no haya novedades o el origen no este accesible.
La fecha de la carpeta es la hora local de Windows (Bolivia, UTC-4).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

COLUMNAS_RESUMEN = [
    "fecha_revision", "servidor", "carpetas_detectadas", "archivos_nuevos", "archivos_descargados",
    "archivos_ya_existentes", "archivos_pendientes", "archivos_validados", "archivos_fallidos",
    "bytes_descargados", "ultimo_backup_fecha", "ultima_copia_valida", "estado_general",
    "duracion_segundos", "observaciones", "id_ejecucion",
]
COLUMNAS_DETALLE = [
    "fecha_revision", "servidor", "plataforma", "backup_id", "ruta_relativa", "nombre_archivo",
    "tamano_bytes", "fecha_modificacion", "hash_esperado_sha256", "hash_obtenido_sha256", "estado",
    "verificacion", "fecha_inicio", "fecha_fin", "mensaje_error", "id_ejecucion",
]


@dataclass
class FilaDetalle:
    servidor: str
    plataforma: str | None
    backup_id: str
    ruta_relativa: str
    nombre_archivo: str
    tamano_bytes: int
    fecha_modificacion: str
    estado: str
    verificacion: str  # SHA256 | LIMITADA | NINGUNA
    hash_esperado_sha256: str = ""
    hash_obtenido_sha256: str = ""
    fecha_inicio: str = ""
    fecha_fin: str = ""
    mensaje_error: str = ""


@dataclass
class ResumenServidor:
    servidor: str
    carpetas_detectadas: int = 0
    archivos_nuevos: int = 0
    archivos_descargados: int = 0
    archivos_ya_existentes: int = 0
    archivos_pendientes: int = 0
    archivos_validados: int = 0
    archivos_fallidos: int = 0
    bytes_descargados: int = 0
    ultimo_backup_fecha: str = ""
    ultima_copia_valida: str = ""
    estado_general: str = "OK"
    duracion_segundos: float = 0.0
    observaciones: list[str] = field(default_factory=list)


def ahora_local() -> datetime:
    return datetime.now().replace(microsecond=0)


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


class _ColaLog(logging.Handler):
    """Guarda las ultimas lineas del log para subirlas a la base (panel)."""

    def __init__(self, maximo: int = 2000):
        super().__init__()
        self.lineas: deque[str] = deque(maxlen=maximo)

    def emit(self, record: logging.LogRecord) -> None:
        self.lineas.append(self.format(record))


class RegistroDiario:
    def __init__(self, raiz_registros: Path, id_ejecucion: int | None, tipo: str):
        self.inicio_local = ahora_local()
        self.fecha = self.inicio_local.strftime("%Y-%m-%d")
        self.carpeta = raiz_registros / self.fecha
        self.id_ejecucion = id_ejecucion
        self.tipo = tipo
        self.detalle: list[FilaDetalle] = []
        self.resumenes: dict[str, ResumenServidor] = {}
        self.carpeta.mkdir(parents=True, exist_ok=True)

        self.log = logging.getLogger(f"backups.ejecucion.{id_ejecucion or 'x'}.{id(self)}")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        formato = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        self._archivo = logging.FileHandler(self.carpeta / "ejecucion.log", encoding="utf-8")
        self._archivo.setFormatter(formato)
        self._cola = _ColaLog()
        self._cola.setFormatter(formato)
        handlers: list[logging.Handler] = [self._archivo, self._cola]
        if sys.stderr is not None:  # con pythonw (servicio sin consola) no hay stderr
            consola = logging.StreamHandler()
            consola.setFormatter(formato)
            handlers.append(consola)
        for h in handlers:
            self.log.addHandler(h)
        self.log.info("=" * 70)
        self.log.info("Inicio de ejecucion #%s (%s)", id_ejecucion if id_ejecucion else "-", tipo)

    def resumen(self, servidor: str) -> ResumenServidor:
        if servidor not in self.resumenes:
            self.resumenes[servidor] = ResumenServidor(servidor)
        return self.resumenes[servidor]

    def agregar(self, fila: FilaDetalle) -> None:
        self.detalle.append(fila)

    def texto_log(self) -> str:
        return "\n".join(self._cola.lineas)

    def cerrar(self, estado: str, resumen_general: dict) -> Path:
        """Escribe CSV/JSON del dia (agregando a lo ya escrito ese dia)."""
        fecha_revision = fmt(self.inicio_local)
        id_ej = self.id_ejecucion or ""

        filas_resumen = []
        for r in sorted(self.resumenes.values(), key=lambda r: r.servidor):
            d = asdict(r)
            d["observaciones"] = "; ".join(r.observaciones)
            d["duracion_segundos"] = round(r.duracion_segundos, 1)
            d["fecha_revision"] = fecha_revision
            d["id_ejecucion"] = id_ej
            filas_resumen.append(d)
        filas_detalle = []
        for f in self.detalle:
            d = asdict(f)
            d["fecha_revision"] = fecha_revision
            d["id_ejecucion"] = id_ej
            filas_detalle.append(d)

        _agregar_csv(self.carpeta / "resumen_diario.csv", COLUMNAS_RESUMEN, filas_resumen)
        _agregar_csv(self.carpeta / "detalle_archivos.csv", COLUMNAS_DETALLE, filas_detalle)

        ruta_json = self.carpeta / "resumen_diario.json"
        try:
            previo = json.loads(ruta_json.read_text(encoding="utf-8")) if ruta_json.exists() else {}
        except (OSError, ValueError):
            previo = {}
        ejecuciones = previo.get("ejecuciones", []) if isinstance(previo, dict) else []
        ejecuciones.append(
            {
                "id_ejecucion": self.id_ejecucion,
                "tipo": self.tipo,
                "inicio": fecha_revision,
                "fin": fmt(ahora_local()),
                "estado": estado,
                "resumen": resumen_general,
                "servidores": filas_resumen,
                "archivos": filas_detalle,
            }
        )
        tmp = ruta_json.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"fecha": self.fecha, "ejecuciones": ejecuciones}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, ruta_json)

        self.log.info("Fin de ejecucion #%s: %s", self.id_ejecucion if self.id_ejecucion else "-", estado)
        for h in list(self.log.handlers):
            if h is not self._cola:
                h.close()
                self.log.removeHandler(h)
        return self.carpeta


def _agregar_csv(ruta: Path, columnas: list[str], filas: list[dict]) -> None:
    nuevo = not ruta.exists()
    # utf-8-sig: Excel abre bien los acentos (igual que el CSV del monitoreo)
    with open(ruta, "a", newline="", encoding="utf-8-sig" if nuevo else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerows(filas)
