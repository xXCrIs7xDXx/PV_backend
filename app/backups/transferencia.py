"""Copia en streaming por bloques con SHA-256 al vuelo y reanudacion.

- Nunca carga un archivo completo en memoria (bloques de BACKUPS_TAMANO_BLOQUE_MB).
- Escribe en <archivo>.part dentro de D:\\Backups\\_TEMPORAL; si la copia se
  corta, la siguiente vez continua desde donde quedo (re-hasheando lo ya
  copiado), en vez de volver a bajar 50 GB. Drive para escritorio permite
  leer desde un desplazamiento (seek) aunque el archivo este en streaming.
- Comprueba que el archivo de origen no cambio durante la copia.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.backups.config import BackupSettings
from app.backups.fuente import Fuente


class ErrorTransferencia(Exception):
    pass


class SinEspacio(Exception):
    pass


@dataclass
class ResultadoCopia:
    sha256: str
    tamano: int
    bytes_transferidos: int  # sin contar lo reanudado
    reanudado_desde: int


def espacio_libre(ruta: Path) -> int:
    p = ruta
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def espacio_total(ruta: Path) -> int:
    p = ruta
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).total


def tamano_directorio(ruta: Path) -> int:
    total = 0
    if not ruta.exists():
        return 0
    for base, _dirs, archivos in os.walk(ruta):
        for a in archivos:
            try:
                total += os.path.getsize(os.path.join(base, a))
            except OSError:
                pass
    return total


def sha256_archivo(ruta: Path, bloque: int = 8 * 1024 * 1024, hasta: int | None = None) -> str:
    return _hash_local(ruta, bloque, hasta).hexdigest()


def _hash_local(ruta: Path, bloque: int, hasta: int | None = None) -> "hashlib._Hash":
    h = hashlib.sha256()
    restante = hasta
    with open(ruta, "rb") as f:
        while True:
            n = bloque if restante is None else min(bloque, restante)
            if n == 0:
                break
            datos = f.read(n)
            if not datos:
                break
            h.update(datos)
            if restante is not None:
                restante -= len(datos)
    return h


def comprobar_espacio(destino: Path, necesario: int, cfg: BackupSettings, cache: Path | None = None) -> None:
    margen = int(cfg.margen_minimo_libre_gb * 1024**3)
    libre = espacio_libre(destino)
    if libre - necesario < margen:
        raise SinEspacio(
            f"espacio insuficiente en {destino.anchor or destino}: libres {libre / 1024**3:.1f} GB, "
            f"se necesitan {necesario / 1024**3:.1f} GB + margen de {cfg.margen_minimo_libre_gb:g} GB"
        )
    if cache is not None and cache.exists():
        # Drive para escritorio guarda en su cache lo que se lee en streaming;
        # se exige al menos el margen libre en ese volumen.
        libre_cache = espacio_libre(cache)
        if libre_cache < margen:
            raise SinEspacio(
                f"espacio insuficiente en el volumen de la cache de Drive ({cache.anchor}): "
                f"libres {libre_cache / 1024**3:.1f} GB (margen {cfg.margen_minimo_libre_gb:g} GB)"
            )


def copiar(
    fuente: Fuente,
    ruta_relativa: str,
    destino_part: Path,
    tamano_esperado: int,
    cfg: BackupSettings,
    progreso: Callable[[int], None] | None = None,
) -> ResultadoCopia:
    """Copia ruta_relativa del origen a destino_part. Reanuda si el .part existe."""
    bloque = max(1, cfg.tamano_bloque_mb) * 1024 * 1024
    destino_part.parent.mkdir(parents=True, exist_ok=True)

    info_inicial = fuente.info(ruta_relativa)
    if info_inicial is None:
        raise ErrorTransferencia("el archivo ya no existe en el origen")
    if info_inicial.tamano != tamano_esperado:
        destino_part.unlink(missing_ok=True)  # lo reanudable ya no corresponde al origen
        raise ErrorTransferencia(
            f"el tamano en el origen cambio ({tamano_esperado} -> {info_inicial.tamano} bytes)"
        )

    inicio = 0
    if destino_part.exists():
        actual = destino_part.stat().st_size
        if 0 < actual <= tamano_esperado:
            inicio = actual
        else:
            destino_part.unlink()  # temporal propio corrupto o mas grande que el origen

    h = _hash_local(destino_part, bloque) if inicio else hashlib.sha256()
    transferidos = 0
    limite_bps = cfg.limite_mbps * 1024 * 1024 / 8 if cfg.limite_mbps > 0 else 0
    t0 = time.monotonic()

    try:
        with fuente.abrir(ruta_relativa) as src, open(destino_part, "ab" if inicio else "wb") as dst:
            if inicio:
                src.seek(inicio)
            while True:
                datos = src.read(bloque)
                if not datos:
                    break
                dst.write(datos)
                h.update(datos)
                transferidos += len(datos)
                if progreso:
                    progreso(len(datos))
                if limite_bps:
                    esperado = transferidos / limite_bps
                    transcurrido = time.monotonic() - t0
                    if esperado > transcurrido:
                        time.sleep(esperado - transcurrido)
            dst.flush()
            os.fsync(dst.fileno())
    except OSError as e:
        raise ErrorTransferencia(f"lectura/escritura interrumpida tras {inicio + transferidos} bytes: {e}") from e

    tamano = destino_part.stat().st_size
    if tamano != tamano_esperado:
        raise ErrorTransferencia(f"tamano copiado {tamano} distinto del esperado {tamano_esperado}")

    info_final = fuente.info(ruta_relativa)
    if info_final is None or info_final.tamano != info_inicial.tamano or info_final.mtime != info_inicial.mtime:
        destino_part.unlink(missing_ok=True)
        raise ErrorTransferencia("el archivo de origen cambio durante la copia")

    return ResultadoCopia(h.hexdigest(), tamano, transferidos, inicio)
