"""Acceso de SOLO LECTURA a la carpeta compartida de respaldos.

Proveedor implementado: Google Drive para escritorio (unidad G:). La carpeta
Backupumsa2627 pertenece a otra cuenta y aparece en "Mi unidad" como un
acceso directo .lnk que no se puede recorrer; Drive para escritorio expone el
destino real en G:\\.shortcut-targets-by-id\\<id>\\Backupumsa2627, que si es
una carpeta legible. Aqui se resuelve esa ruta y se comprueba listandola.

Si algun dia la ruta deja de ser recorrible (p. ej. el agente corre como
servicio sin sesion y G: no esta montada), se puede agregar otro proveedor
(Google Drive API/OAuth) que implemente la misma interfaz `Fuente`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Protocol

from app.backups.config import BackupSettings, ruta_extendida, ruta_visible

# Archivos que Drive para escritorio / Windows crean y que no son parte del respaldo
IGNORADOS = {"desktop.ini", "thumbs.db", ".ds_store"}


class FuenteNoDisponible(Exception):
    """Drive no esta montado, la carpeta compartida no aparece o no se puede leer."""


@dataclass(frozen=True)
class Entrada:
    nombre: str
    ruta_relativa: str  # con "/" como separador, relativa a la carpeta compartida
    es_carpeta: bool
    tamano: int
    mtime: datetime  # UTC naive


class Fuente(Protocol):
    descripcion: str

    def verificar(self) -> None: ...
    def listar(self, ruta_relativa: str = "") -> list[Entrada]: ...
    def info(self, ruta_relativa: str) -> Entrada | None: ...
    def abrir(self, ruta_relativa: str) -> BinaryIO: ...


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _destino_lnk(lnk: Path) -> Path | None:
    """Lee el destino de un acceso directo de Windows (sin dependencias extra)."""
    if sys.platform != "win32" or not lnk.is_file():
        return None
    script = f"(New-Object -ComObject WScript.Shell).CreateShortcut('{str(lnk).replace(chr(39), chr(39) * 2)}').TargetPath"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    destino = out.stdout.strip()
    return Path(destino) if destino else None


def resolver_ruta_origen(cfg: BackupSettings) -> Path:
    """Devuelve una ruta de carpeta que existe Y se puede listar. Nunca
    devuelve el .lnk ni una ruta sin probar."""
    def legible(p: Path) -> bool:
        try:
            if not p.is_dir():
                return False
            with os.scandir(p) as it:
                next(it, None)
            return True
        except OSError:
            return False

    if cfg.ruta_origen:
        p = Path(cfg.ruta_origen)
        if legible(p):
            return p
        raise FuenteNoDisponible(f"BACKUPS_RUTA_ORIGEN no es una carpeta legible: {p}")

    unidad = Path(cfg.unidad_drive.rstrip("\\/") + "\\")
    if not unidad.exists():
        raise FuenteNoDisponible(
            f"La unidad {cfg.unidad_drive} no esta montada (¿Google Drive para escritorio esta abierto?)"
        )

    nombre = cfg.carpeta_compartida
    candidatos: list[Path] = []
    desde_lnk: list[Path] = []
    # 1) Acceso directo en "Mi unidad" (el caso actual): se resuelve su destino
    for mi_unidad in ("Mi unidad", "My Drive"):
        base = unidad / mi_unidad
        destino = _destino_lnk(base / f"{nombre}.lnk")
        if destino is not None:
            candidatos.append(destino)
            desde_lnk.append(destino)
        candidatos.append(base / nombre)  # por si algun dia es una carpeta normal
    # 2) Destinos de accesos directos que Drive expone en una carpeta oculta
    try:
        ocultos = unidad / ".shortcut-targets-by-id"
        if ocultos.is_dir():
            candidatos.extend(p / nombre for p in ocultos.iterdir())
    except OSError:
        pass
    # 3) Unidades compartidas
    for compartidas in ("Unidades compartidas", "Shared drives"):
        try:
            base = unidad / compartidas
            if base.is_dir():
                candidatos.extend(p / nombre for p in base.iterdir())
        except OSError:
            pass

    encontrados: list[Path] = []
    for c in candidatos:
        if legible(c) and all(os.path.normcase(str(c)) != os.path.normcase(str(e)) for e in encontrados):
            encontrados.append(c)

    if not encontrados:
        raise FuenteNoDisponible(
            f"No se encontro una carpeta legible '{nombre}' en {unidad} "
            "(¿se revoco el acceso compartido o cambio el nombre?)"
        )
    if len(encontrados) > 1:
        # Si el .lnk resolvio a una de ellas, esa manda
        claves = {os.path.normcase(str(e)) for e in encontrados}
        lnk = next((c for c in desde_lnk if os.path.normcase(str(c)) in claves), None)
        if lnk is not None:
            return lnk
        rutas = "; ".join(str(e) for e in encontrados)
        raise FuenteNoDisponible(
            f"Hay varias carpetas '{nombre}' accesibles ({rutas}). Define BACKUPS_RUTA_ORIGEN con la correcta."
        )
    return encontrados[0]


class DriveEscritorioFuente:
    """Lee la carpeta compartida a traves de la unidad de Drive para escritorio."""

    def __init__(self, raiz: Path):
        self.raiz = ruta_extendida(raiz)  # nombres de .mbz muy largos
        self.ruta = ruta_visible(self.raiz)
        self.descripcion = f"Google Drive para escritorio: {self.ruta}"

    @classmethod
    def desde_config(cls, cfg: BackupSettings) -> "DriveEscritorioFuente":
        return cls(resolver_ruta_origen(cfg))

    def _ruta(self, rel: str) -> Path:
        return self.raiz.joinpath(*[p for p in rel.split("/") if p]) if rel else self.raiz

    def verificar(self) -> None:
        try:
            if not self.raiz.is_dir():
                raise FuenteNoDisponible(f"La carpeta de origen ya no existe: {self.raiz}")
            with os.scandir(self.raiz) as it:
                next(it, None)
        except OSError as e:
            raise FuenteNoDisponible(f"No se puede leer {self.raiz}: {e}") from e

    def listar(self, ruta_relativa: str = "") -> list[Entrada]:
        base = self._ruta(ruta_relativa)
        out: list[Entrada] = []
        try:
            with os.scandir(base) as it:
                for d in it:
                    if d.name.lower() in IGNORADOS:
                        continue
                    st = d.stat()
                    es_carpeta = d.is_dir()
                    out.append(
                        Entrada(
                            nombre=d.name,
                            ruta_relativa=f"{ruta_relativa}/{d.name}" if ruta_relativa else d.name,
                            es_carpeta=es_carpeta,
                            tamano=0 if es_carpeta else st.st_size,
                            mtime=_utc(st.st_mtime),
                        )
                    )
        except OSError as e:
            raise FuenteNoDisponible(f"No se puede listar {base}: {e}") from e
        return sorted(out, key=lambda e: e.nombre)

    def info(self, ruta_relativa: str) -> Entrada | None:
        p = self._ruta(ruta_relativa)
        try:
            st = p.stat()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise FuenteNoDisponible(f"No se puede leer {p}: {e}") from e
        es_carpeta = p.is_dir()
        return Entrada(p.name, ruta_relativa, es_carpeta, 0 if es_carpeta else st.st_size, _utc(st.st_mtime))

    def abrir(self, ruta_relativa: str) -> BinaryIO:
        # Solo lectura: el origen nunca se modifica, mueve ni borra.
        return open(self._ruta(ruta_relativa), "rb")
