from dataclasses import dataclass
from typing import Optional

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import UnidadAcademica


async def build_unidad_display_names(session: AsyncSession) -> dict[int, str]:
    """Maps id_unidad -> "Facultad - Carrera" for child units, or just the
    unit's own name when it has no parent (top-level facultades)."""
    rows = (await session.exec(select(UnidadAcademica))).all()
    by_id = {u.id_unidad: u for u in rows}
    names: dict[int, str] = {}
    for u in rows:
        padre = by_id.get(u.id_unidad_padre) if u.id_unidad_padre else None
        names[u.id_unidad] = f"{padre.nombre} - {u.nombre}" if padre else u.nombre
    return names


@dataclass
class UnidadInfo:
    display: str  # "Facultad - Carrera" (igual que build_unidad_display_names)
    id_facultad: int  # unidad raiz de la jerarquia
    nombre_facultad: str
    nombre_subunidad: Optional[str]  # None si la plataforma cuelga de la facultad


async def build_unidad_info(session: AsyncSession) -> dict[int, UnidadInfo]:
    """id_unidad -> facultad raiz + sub-unidad, para agrupar por facultad."""
    rows = (await session.exec(select(UnidadAcademica))).all()
    by_id = {u.id_unidad: u for u in rows}
    out: dict[int, UnidadInfo] = {}
    for u in rows:
        raiz = u
        visitados = {u.id_unidad}
        while raiz.id_unidad_padre and raiz.id_unidad_padre in by_id and raiz.id_unidad_padre not in visitados:
            raiz = by_id[raiz.id_unidad_padre]
            visitados.add(raiz.id_unidad)
        padre = by_id.get(u.id_unidad_padre) if u.id_unidad_padre else None
        out[u.id_unidad] = UnidadInfo(
            display=f"{padre.nombre} - {u.nombre}" if padre else u.nombre,
            id_facultad=raiz.id_unidad,
            nombre_facultad=raiz.nombre,
            nombre_subunidad=None if raiz is u else u.nombre,
        )
    return out
