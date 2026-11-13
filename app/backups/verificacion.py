"""Politica de finalizacion (¿el respaldo en Drive ya esta completo?) y
lectura de manifiestos sha256sum.

Hoy los scripts Linux NO publican un marcador de finalizacion
(BACKUP_COMPLETO.ok), asi que se usa una politica conservadora:
  1. Antiguedad minima desde la ultima modificacion en Drive (ventana).
  2. Estabilidad: tamanos y fechas iguales en dos sondeos separados.
  3. Conjunto esperado de archivos segun el tipo de respaldo.
  4. Si hay hash_verificacion.txt: debe ser legible, listar exactamente los
     archivos presentes e incluir al menos codigo_ y db_.
Esto reduce el riesgo pero NO garantiza que el respaldo de origen sea
restaurable; por eso un respaldo sin manifiesto queda como SIN_HASH
(verificacion limitada) y nunca como VALIDADO.
Cuando los scripts Linux publiquen un BACKUP_COMPLETO.ok al terminar, conviene
exigirlo aqui (hoy solo se tolera si aparece: no cuenta como archivo de datos).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.backups.config import BackupSettings
from app.backups.descubrimiento import MANIFIESTO, RespaldoOrigen
from app.backups.fuente import Fuente

MARCADOR_FIN = "BACKUP_COMPLETO.ok"
MAX_BYTES_MANIFIESTO = 256 * 1024
_RE_LINEA_SHA = re.compile(r"^(?P<h>[0-9a-fA-F]{64}) [ *](?P<n>.+)$")
_RE_LINEA_CURSO = re.compile(r"^\s*\d+\s*\|")


class ManifiestoInvalido(Exception):
    pass


def parsear_manifiesto(texto: str) -> dict[str, str]:
    """Formato de sha256sum: '<64 hex><espacio><espacio o *><nombre>'.
    Devuelve nombre_de_archivo -> hash (minusculas). Lanza ManifiestoInvalido
    si hay lineas que no cumplen el formato o nombres repetidos."""
    hashes: dict[str, str] = {}
    for n, linea in enumerate(texto.splitlines(), start=1):
        linea = linea.strip("\r﻿")
        if not linea.strip():
            continue
        m = _RE_LINEA_SHA.match(linea)
        if not m:
            raise ManifiestoInvalido(f"linea {n} con formato invalido")
        nombre = m.group("n").strip().replace("\\", "/").split("/")[-1]
        if nombre in hashes:
            raise ManifiestoInvalido(f"'{nombre}' aparece dos veces")
        hashes[nombre] = m.group("h").lower()
    if not hashes:
        raise ManifiestoInvalido("manifiesto vacio")
    return hashes


def leer_texto(fuente: Fuente, ruta_relativa: str) -> tuple[str, str]:
    """Devuelve (texto, sha256 de los bytes leidos)."""
    with fuente.abrir(ruta_relativa) as f:
        datos = f.read(MAX_BYTES_MANIFIESTO + 1)
    if len(datos) > MAX_BYTES_MANIFIESTO:
        raise ManifiestoInvalido("archivo de control demasiado grande")
    return datos.decode("utf-8", errors="replace"), hashlib.sha256(datos).hexdigest()


@dataclass
class Evaluacion:
    estado_origen: str  # COMPLETO | PENDIENTE | INCOMPLETO | NO_RECONOCIDO
    motivo: str = ""
    # nombre -> sha256 esperado. Incluye el propio manifiesto / cursos.txt con
    # el hash de los bytes que se evaluaron: si la copia local difiere, el
    # archivo de control cambio entre la evaluacion y la descarga.
    hashes: dict[str, str] = field(default_factory=dict)
    # True solo si TODOS los archivos de datos tienen un SHA-256 esperado
    verificacion_sha: bool = False

    @property
    def completo(self) -> bool:
        return self.estado_origen == "COMPLETO"


def _tipo_estandar(nombre: str) -> str | None:
    for k in ("codigo_", "db_", "data_"):
        if nombre.startswith(k):
            return k[:-1]
    return None


def evaluar(
    respaldo: RespaldoOrigen,
    fuente: Fuente,
    cfg: BackupSettings,
    ahora_utc: datetime,
    estable: bool,
) -> Evaluacion:
    if respaldo.tipo == "desconocido":
        return Evaluacion("NO_RECONOCIDO", "estructura de carpeta no reconocida")

    nombres = {a.nombre for a in respaldo.archivos}
    vacios = [a.nombre for a in respaldo.archivos if a.tamano == 0 and a.nombre != MARCADOR_FIN]
    if vacios:
        return Evaluacion("INCOMPLETO", f"archivos de 0 bytes: {', '.join(vacios)}")

    manifiesto = respaldo.manifiesto
    ventana = cfg.ventana_minutos_con_manifiesto if manifiesto else cfg.ventana_minutos_sin_manifiesto
    edad = ahora_utc - respaldo.ultima_modificacion
    if edad < timedelta(minutes=ventana):
        minutos = max(0, int(edad.total_seconds() // 60))
        return Evaluacion("PENDIENTE", f"modificado hace {minutos} min (ventana {ventana} min)")
    if not estable:
        return Evaluacion("PENDIENTE", "los tamanos/fechas cambiaron entre dos sondeos (subida en curso)")

    if respaldo.tipo == "estandar":
        tipos = {_tipo_estandar(n) for n in nombres}
        faltan = [k for k in ("codigo", "db") if k not in tipos]
        if faltan:
            return Evaluacion("INCOMPLETO", f"faltan archivos esperados: {', '.join(f + '_*' for f in faltan)}")
        if manifiesto is None:
            return Evaluacion("COMPLETO", "sin manifiesto: verificacion limitada a tamano")
        try:
            texto, sha_manifiesto = leer_texto(fuente, manifiesto.ruta_relativa)
            hashes = parsear_manifiesto(texto)
        except ManifiestoInvalido as e:
            return Evaluacion("INCOMPLETO", f"manifiesto invalido: {e}")
        except OSError as e:
            return Evaluacion("PENDIENTE", f"no se pudo leer el manifiesto: {e}")
        datos = nombres - {MANIFIESTO, MARCADOR_FIN}
        no_estan = sorted(set(hashes) - datos)
        if no_estan:
            return Evaluacion("INCOMPLETO", f"el manifiesto lista archivos que no estan en Drive: {', '.join(no_estan)}")
        sin_hash = sorted(datos - set(hashes))
        if sin_hash:
            return Evaluacion("INCOMPLETO", f"archivos que no figuran en el manifiesto: {', '.join(sin_hash)}")
        tipos_manifiesto = {_tipo_estandar(n) for n in hashes}
        if not {"codigo", "db"} <= tipos_manifiesto:
            return Evaluacion("INCOMPLETO", "el manifiesto no incluye codigo_ y db_")
        return Evaluacion(
            "COMPLETO", "manifiesto SHA-256 completo", {**hashes, MANIFIESTO: sha_manifiesto}, verificacion_sha=True
        )

    if respaldo.tipo == "docker_moodle":
        bajo = [n.lower() for n in nombres]
        if not any(n.endswith((".sql", ".sql.gz")) for n in bajo):
            return Evaluacion("INCOMPLETO", "falta el volcado de la base de datos (*.sql)")
        if not any("moodledata" in n and n.endswith((".tar.gz", ".tgz")) for n in bajo):
            return Evaluacion("INCOMPLETO", "falta el archivo moodledata (*.tar.gz)")
        return Evaluacion("COMPLETO", "sin manifiesto: verificacion limitada a tamano")

    if respaldo.tipo == "docker_cursos":
        if "cursos.txt" not in nombres:
            return Evaluacion("INCOMPLETO", "falta cursos.txt")
        try:
            lista, sha_lista = leer_texto(fuente, f"{respaldo.ruta_relativa}/cursos.txt")
        except (OSError, ManifiestoInvalido) as e:
            return Evaluacion("INCOMPLETO", f"no se pudo leer cursos.txt: {e}")
        esperados = sum(1 for linea in lista.splitlines() if _RE_LINEA_CURSO.match(linea))
        mbz = sum(1 for n in nombres if n.lower().endswith(".mbz"))
        if mbz < esperados:
            return Evaluacion("INCOMPLETO", f"cursos.txt lista {esperados} cursos pero hay {mbz} archivos .mbz")
        return Evaluacion(
            "COMPLETO", f"{mbz} cursos (.mbz) segun cursos.txt; verificacion limitada a tamano",
            {"cursos.txt": sha_lista},
        )

    return Evaluacion("NO_RECONOCIDO", f"tipo {respaldo.tipo} sin politica definida")
