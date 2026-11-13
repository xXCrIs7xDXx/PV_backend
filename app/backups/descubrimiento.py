"""Descubre servidores y carpetas de respaldo en el origen, sin asumir una
estructura fija.

Regla general: un "respaldo" es cualquier carpeta que contiene archivos. Su
tipo se reconoce por el nombre de la carpeta (estructuras vistas en
Backupumsa2627 el 13/11/2026):

  estandar       <servidor>[/<plataforma>]/2026-11-13_02-00-01/
                   codigo_<p>_<ts>.tar.gz, db_<p>_<ts>.sql.gz,
                   [data_<p>_<ts>.tar.gz (semanal)], hash_verificacion.txt
  docker_moodle  <servidor>[/<carpeta>]/<p>_moodle_08-11-2026-020001/
                   <x>_db_backup.sql, <x>_moodledata.tar.gz   (sin manifiesto)
  docker_cursos  <servidor>[/<carpeta>]/<p>_cursos_08-11-2026-020028/
                   cursos.txt + copia_de_seguridad-*.mbz       (sin manifiesto)

Cualquier otra cosa se marca "desconocido" (NO_RECONOCIDO): se registra y
se alerta, pero no se inventa su estructura.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.backups.fuente import Entrada, Fuente, FuenteNoDisponible

MANIFIESTO = "hash_verificacion.txt"
PROFUNDIDAD_MAXIMA = 5

_RE_ESTANDAR = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$")
_RE_DOCKER = re.compile(
    r"^(?P<p>.+?)_(?P<k>moodle|cursos)_(?P<d>\d{2})-(?P<m>\d{2})-(?P<a>\d{4})-(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})$"
)
_RE_ARCHIVO_ESTANDAR = re.compile(
    r"^(?P<k>codigo|db|data)_(?P<p>.+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.(tar\.gz|sql\.gz|sql|tgz|zip)$"
)


@dataclass
class RespaldoOrigen:
    servidor: str
    ruta_relativa: str
    nombre: str
    tipo: str  # estandar | docker_moodle | docker_cursos | desconocido
    plataforma: str | None
    fecha_backup: datetime | None  # hora local (la del nombre de la carpeta)
    archivos: list[Entrada]

    @property
    def manifiesto(self) -> Entrada | None:
        return next((a for a in self.archivos if a.nombre == MANIFIESTO), None)

    @property
    def tamano_total(self) -> int:
        return sum(a.tamano for a in self.archivos)

    @property
    def ultima_modificacion(self) -> datetime:
        return max(a.mtime for a in self.archivos)

    def firma(self) -> tuple:
        """Huella de tamanos/fechas para comprobar estabilidad entre sondeos."""
        return tuple(sorted((a.nombre, a.tamano, a.mtime.isoformat()) for a in self.archivos))


@dataclass
class ServidorOrigen:
    nombre: str
    respaldos: list[RespaldoOrigen] = field(default_factory=list)
    error: str | None = None

    @property
    def vacio(self) -> bool:
        return self.error is None and not self.respaldos


@dataclass
class Inventario:
    servidores: list[ServidorOrigen]
    # Archivos sueltos en la raiz de la carpeta compartida (no pertenecen a un servidor)
    sueltos_raiz: list[Entrada]


def clasificar(servidor: str, ruta_relativa: str, archivos: list[Entrada]) -> RespaldoOrigen:
    partes = ruta_relativa.split("/")
    nombre = partes[-1]
    padre = partes[-2] if len(partes) >= 3 else None  # carpeta intermedia (no el servidor)

    if len(partes) == 1:
        # Archivos sueltos directamente en la carpeta del servidor
        return RespaldoOrigen(servidor, ruta_relativa, nombre, "desconocido", None, None, archivos)

    m = _RE_ESTANDAR.match(nombre)
    if m:
        a, mes, d, H, M, S = (int(x) for x in m.groups())
        plataforma = None
        for f in archivos:
            mf = _RE_ARCHIVO_ESTANDAR.match(f.nombre)
            if mf:
                plataforma = mf.group("p")
                break
        return RespaldoOrigen(
            servidor, ruta_relativa, nombre, "estandar", plataforma or padre, _fecha(a, mes, d, H, M, S), archivos
        )

    m = _RE_DOCKER.match(nombre)
    if m:
        g = m.groupdict()
        fecha = _fecha(int(g["a"]), int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]), int(g["S"]))
        return RespaldoOrigen(
            servidor, ruta_relativa, nombre, f"docker_{g['k']}", g["p"], fecha, archivos
        )

    return RespaldoOrigen(servidor, ruta_relativa, nombre, "desconocido", padre, None, archivos)


def _fecha(a: int, m: int, d: int, H: int, M: int, S: int) -> datetime | None:
    try:
        return datetime(a, m, d, H, M, S)
    except ValueError:
        return None


def _recorrer(fuente: Fuente, servidor: str, ruta: str, nivel: int, out: list[RespaldoOrigen]) -> None:
    entradas = fuente.listar(ruta)
    archivos = [e for e in entradas if not e.es_carpeta]
    if archivos:
        out.append(clasificar(servidor, ruta, archivos))
    if nivel >= PROFUNDIDAD_MAXIMA:
        return
    for e in entradas:
        if e.es_carpeta:
            _recorrer(fuente, servidor, e.ruta_relativa, nivel + 1, out)


def descubrir(fuente: Fuente, incluir: set[str] | None = None, excluir: set[str] | None = None) -> Inventario:
    """Lista TODAS las carpetas de primer nivel (servidores) y sus respaldos.
    Si falla la lectura de la raiz lanza FuenteNoDisponible; si falla un
    servidor concreto, se registra el error en ese servidor y se sigue."""
    raiz = fuente.listar("")
    servidores: list[ServidorOrigen] = []
    for e in raiz:
        if not e.es_carpeta:
            continue
        if incluir and e.nombre not in incluir:
            continue
        if excluir and e.nombre in excluir:
            continue
        srv = ServidorOrigen(e.nombre)
        try:
            _recorrer(fuente, e.nombre, e.nombre, 1, srv.respaldos)
        except FuenteNoDisponible as err:
            srv.error = str(err)
        servidores.append(srv)
    return Inventario(servidores, [e for e in raiz if not e.es_carpeta])


def resondear(fuente: Fuente, respaldo: RespaldoOrigen) -> RespaldoOrigen:
    """Vuelve a listar los archivos del respaldo (segundo sondeo)."""
    archivos = [e for e in fuente.listar(respaldo.ruta_relativa) if not e.es_carpeta]
    return RespaldoOrigen(
        respaldo.servidor, respaldo.ruta_relativa, respaldo.nombre, respaldo.tipo,
        respaldo.plataforma, respaldo.fecha_backup, archivos,
    )
