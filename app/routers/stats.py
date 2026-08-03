from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Plataforma, RegistroMonitoreo
from app.schemas.monitoreo import ComparacionDiasResponse, ComparacionPlataforma, DiaStats
from app.services.unidades_display import build_unidad_display_names

router = APIRouter(prefix="/api/stats", tags=["stats"])

# fecha_hora is stored as naive UTC (see app/models/monitoreo.py). Bolivia is
# fixed at UTC-4 with no DST, so "calendar day" for the comparativa/fechas
# endpoints must be computed in that offset, not in raw UTC - otherwise a
# check made e.g. at 02:00 UTC (22:00 the previous day in Bolivia) gets
# bucketed under the wrong day for a Bolivian admin.
BOLIVIA_UTC_OFFSET = timedelta(hours=4)


async def _ultimo_registro_por_plataforma(session: AsyncSession, dia: date) -> dict[int, RegistroMonitoreo]:
    """Latest registro per plataforma within the given Bolivia calendar day -
    same "latest per group" pattern as /api/dashboard/resumen, scoped to one
    day."""
    inicio = datetime.combine(dia, time.min) + BOLIVIA_UTC_OFFSET
    fin = inicio + timedelta(days=1)
    latest_sub = (
        select(RegistroMonitoreo.id_plataforma, func.max(RegistroMonitoreo.id_registro).label("max_id"))
        .where(RegistroMonitoreo.fecha_hora >= inicio, RegistroMonitoreo.fecha_hora < fin)
        .group_by(RegistroMonitoreo.id_plataforma)
        .subquery()
    )
    stmt = select(RegistroMonitoreo).join(latest_sub, RegistroMonitoreo.id_registro == latest_sub.c.max_id)
    rows = (await session.exec(stmt)).all()
    return {r.id_plataforma: r for r in rows}


def _resumen(snapshot: dict[int, RegistroMonitoreo]) -> DiaStats:
    operativos = sum(1 for r in snapshot.values() if r.esta_operativo)
    en_revision = sum(1 for r in snapshot.values() if not r.esta_operativo)
    return DiaStats(operativos=operativos, en_revision=en_revision)


@router.get("/fechas-disponibles", response_model=list[str])
async def fechas_disponibles(session: AsyncSession = Depends(get_session)) -> list[str]:
    fecha_local = func.date(RegistroMonitoreo.fecha_hora - BOLIVIA_UTC_OFFSET)
    stmt = select(fecha_local).distinct().order_by(fecha_local.desc())
    rows = (await session.exec(stmt)).all()
    return [str(r[0] if isinstance(r, (tuple, list)) else r) for r in rows]


@router.get("/comparar", response_model=ComparacionDiasResponse)
async def comparar(
    fecha_a: date = Query(...),
    fecha_b: date = Query(...),
    session: AsyncSession = Depends(get_session),
) -> ComparacionDiasResponse:
    snap_a = await _ultimo_registro_por_plataforma(session, fecha_a)
    snap_b = await _ultimo_registro_por_plataforma(session, fecha_b)
    unidad_display = await build_unidad_display_names(session)

    plataformas = (
        await session.exec(
            select(Plataforma).where(Plataforma.deleted_at.is_(None)).order_by(Plataforma.nombre_plataforma)
        )
    ).all()

    items: list[ComparacionPlataforma] = []
    for p in plataformas:
        ra = snap_a.get(p.id_plataforma)
        rb = snap_b.get(p.id_plataforma)
        if ra is None and rb is None:
            continue
        operativo_a = ra.esta_operativo if ra else None
        operativo_b = rb.esta_operativo if rb else None
        items.append(
            ComparacionPlataforma(
                id_plataforma=p.id_plataforma,
                nombre_plataforma=p.nombre_plataforma,
                nombre_unidad=unidad_display.get(p.id_unidad, "—"),
                operativo_a=operativo_a,
                operativo_b=operativo_b,
                tiempo_a=ra.tiempo_respuesta_ms if ra else None,
                tiempo_b=rb.tiempo_respuesta_ms if rb else None,
                cambio=operativo_a is not None and operativo_b is not None and operativo_a != operativo_b,
            )
        )

    return ComparacionDiasResponse(
        fecha_a=fecha_a,
        fecha_b=fecha_b,
        resumen_a=_resumen(snap_a),
        resumen_b=_resumen(snap_b),
        plataformas=items,
    )
