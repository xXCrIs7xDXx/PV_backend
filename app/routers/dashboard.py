from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import RegistroMonitoreo
from app.schemas.monitoreo import DashboardResumen, HistorialPunto

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/resumen", response_model=DashboardResumen)
async def resumen(session: AsyncSession = Depends(get_session)) -> DashboardResumen:
    latest_sub = (
        select(RegistroMonitoreo.id_plataforma, func.max(RegistroMonitoreo.id_registro).label("max_id"))
        .group_by(RegistroMonitoreo.id_plataforma)
        .subquery()
    )
    stmt = select(RegistroMonitoreo).join(latest_sub, RegistroMonitoreo.id_registro == latest_sub.c.max_id)
    rows = (await session.exec(stmt)).all()

    operativos = sum(1 for r in rows if r.esta_operativo)
    en_revision = sum(1 for r in rows if not r.esta_operativo)
    tiempos = [r.tiempo_respuesta_ms for r in rows if r.tiempo_respuesta_ms is not None]
    promedio = int(sum(tiempos) / len(tiempos)) if tiempos else None
    ultima_fecha = max((r.fecha_hora for r in rows), default=None)

    return DashboardResumen(
        fecha=ultima_fecha, operativos=operativos, en_revision=en_revision, total=len(rows), tiempo_promedio_ms=promedio
    )


@router.get("/historial", response_model=list[HistorialPunto])
async def historial(
    dias: int = Query(default=30, le=365), session: AsyncSession = Depends(get_session)
) -> list[HistorialPunto]:
    desde = datetime.utcnow() - timedelta(days=dias)
    stmt = select(RegistroMonitoreo).where(RegistroMonitoreo.fecha_hora >= desde)
    rows = (await session.exec(stmt)).all()

    buckets: dict[datetime, dict[str, int]] = {}
    for r in rows:
        bucket = r.fecha_hora.replace(minute=0, second=0, microsecond=0)
        b = buckets.setdefault(bucket, {"operativos": 0, "en_revision": 0})
        b["operativos" if r.esta_operativo else "en_revision"] += 1

    return [
        HistorialPunto(fecha_hora=k, operativos=v["operativos"], en_revision=v["en_revision"])
        for k, v in sorted(buckets.items())
    ]
