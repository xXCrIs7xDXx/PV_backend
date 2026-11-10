from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Plataforma, RegistroMonitoreo
from app.schemas.monitoreo import DashboardResumen

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/resumen", response_model=DashboardResumen)
async def resumen(session: AsyncSession = Depends(get_session)) -> DashboardResumen:
    latest_sub = (
        select(RegistroMonitoreo.id_plataforma, func.max(RegistroMonitoreo.id_registro).label("max_id"))
        .group_by(RegistroMonitoreo.id_plataforma)
        .subquery()
    )
    # Solo plataformas vigentes: las eliminadas conservan registros viejos
    stmt = (
        select(RegistroMonitoreo)
        .join(latest_sub, RegistroMonitoreo.id_registro == latest_sub.c.max_id)
        .join(Plataforma, Plataforma.id_plataforma == RegistroMonitoreo.id_plataforma)
        .where(Plataforma.deleted_at.is_(None))
    )
    rows = (await session.exec(stmt)).all()

    operativos = sum(1 for r in rows if r.esta_operativo)
    en_revision = sum(1 for r in rows if not r.esta_operativo)
    tiempos = [r.tiempo_respuesta_ms for r in rows if r.tiempo_respuesta_ms is not None]
    promedio = int(sum(tiempos) / len(tiempos)) if tiempos else None
    ultima_fecha = max((r.fecha_hora for r in rows), default=None)

    return DashboardResumen(
        fecha=ultima_fecha, operativos=operativos, en_revision=en_revision, total=len(rows), tiempo_promedio_ms=promedio
    )

