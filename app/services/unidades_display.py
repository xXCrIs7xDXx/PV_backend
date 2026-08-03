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
