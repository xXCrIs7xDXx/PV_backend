import asyncio
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import EjecucionMonitoreo, Plataforma, RegistroMonitoreo
from app.schemas.monitoreo import (
    EjecucionDetalle,
    EjecucionPlataformaDetalle,
    EjecucionResumen,
    FormsEstado,
    MonitorEstado,
    RegistroMonitoreoRead,
)
from app.services import monitor_engine
from app.services.unidades_display import build_unidad_display_names

router = APIRouter(prefix="/api/monitoreo", tags=["monitoreo"])


@router.get("/estado", response_model=MonitorEstado)
async def estado() -> MonitorEstado:
    s = monitor_engine.monitor_status
    return MonitorEstado(
        corriendo=s["corriendo"], inicio=s["inicio"], ultimo_fin=s["ultimo_fin"], ultimo_error=s["ultimo_error"]
    )


@router.post("/ejecutar", dependencies=[Depends(require_admin)])
async def ejecutar() -> dict:
    if monitor_engine.monitor_lock.locked():
        return {"ok": False, "error": "Ya hay una verificacion en curso"}
    asyncio.create_task(monitor_engine.run_check_cycle(tipo="Manual"))
    return {"ok": True}


@router.get("/forms-estado", response_model=FormsEstado)
async def forms_estado() -> FormsEstado:
    s = monitor_engine.forms_status
    return FormsEstado(
        corriendo=s["corriendo"],
        ultimo_fin=s["ultimo_fin"],
        ultimo_resultado=s["ultimo_resultado"],
        ultimo_error=s["ultimo_error"],
    )


@router.post("/enviar-forms", dependencies=[Depends(require_admin)])
async def enviar_forms() -> dict:
    if monitor_engine.forms_lock.locked():
        return {"ok": False, "error": "Ya hay un envio a Google Forms en curso"}
    asyncio.create_task(monitor_engine.enviar_forms_manual())
    return {"ok": True}


@router.get("/ejecuciones", response_model=list[EjecucionResumen])
async def list_ejecuciones(
    limit: int = Query(default=50, le=500),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(EjecucionMonitoreo).order_by(EjecucionMonitoreo.fecha_hora.desc()).limit(limit)
    ejecuciones = (await session.exec(stmt)).all()
    if not ejecuciones:
        return []

    ids = [e.id_ejecucion for e in ejecuciones]
    registros = (
        await session.exec(select(RegistroMonitoreo).where(RegistroMonitoreo.id_ejecucion.in_(ids)))
    ).all()
    by_ejecucion: dict[int, list[RegistroMonitoreo]] = {}
    for r in registros:
        by_ejecucion.setdefault(r.id_ejecucion, []).append(r)

    out = []
    for e in ejecuciones:
        regs = by_ejecucion.get(e.id_ejecucion, [])
        operativos = en_revision = mantenimiento = 0
        for r in regs:
            if r.estado_actual_snapshot == "En Mantenimiento":
                mantenimiento += 1
            elif r.esta_operativo:
                operativos += 1
            else:
                en_revision += 1
        out.append(
            EjecucionResumen(
                id_ejecucion=e.id_ejecucion,
                fecha_hora=e.fecha_hora,
                tipo=e.tipo,
                operativos=operativos,
                en_revision=en_revision,
                mantenimiento=mantenimiento,
                total=len(regs),
            )
        )
    return out


@router.get("/ejecuciones/{id_ejecucion}", response_model=EjecucionDetalle)
async def get_ejecucion_detalle(id_ejecucion: int, session: AsyncSession = Depends(get_session)):
    ejecucion = await session.get(EjecucionMonitoreo, id_ejecucion)
    if not ejecucion:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")

    unidad_display = await build_unidad_display_names(session)
    stmt = (
        select(RegistroMonitoreo, Plataforma.nombre_plataforma, Plataforma.id_unidad)
        .join(Plataforma, Plataforma.id_plataforma == RegistroMonitoreo.id_plataforma)
        .where(RegistroMonitoreo.id_ejecucion == id_ejecucion)
        .order_by(Plataforma.nombre_plataforma)
    )
    rows = (await session.exec(stmt)).all()

    plataformas = [
        EjecucionPlataformaDetalle(
            id_plataforma=registro.id_plataforma,
            nombre_plataforma=nombre,
            nombre_unidad=unidad_display.get(id_unidad, "—"),
            estado_actual_snapshot=registro.estado_actual_snapshot,
            esta_operativo=registro.esta_operativo,
            estado_http=registro.estado_http,
            tiempo_respuesta_ms=registro.tiempo_respuesta_ms,
            observacion=registro.observacion,
        )
        for registro, nombre, id_unidad in rows
    ]

    return EjecucionDetalle(
        id_ejecucion=ejecucion.id_ejecucion,
        fecha_hora=ejecucion.fecha_hora,
        tipo=ejecucion.tipo,
        plataformas=plataformas,
    )


@router.get("/registros", response_model=list[RegistroMonitoreoRead])
async def list_registros(
    id_plataforma: int | None = Query(default=None),
    limit: int = Query(default=200, le=2000),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(RegistroMonitoreo).order_by(RegistroMonitoreo.fecha_hora.desc()).limit(limit)
    if id_plataforma is not None:
        stmt = stmt.where(RegistroMonitoreo.id_plataforma == id_plataforma)
    result = await session.exec(stmt)
    return result.all()


@router.get("/exportar/csv")
async def exportar_csv(session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    stmt = (
        select(Plataforma.nombre_plataforma, Plataforma.url_principal, RegistroMonitoreo)
        .join(RegistroMonitoreo, RegistroMonitoreo.id_plataforma == Plataforma.id_plataforma)
        .order_by(RegistroMonitoreo.fecha_hora.desc())
        .limit(5000)
    )
    rows = (await session.exec(stmt)).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Plataforma", "URL", "Fecha", "Operativo", "Codigo HTTP", "Tiempo (ms)", "Observacion"])
    for nombre, url, reg in rows:
        writer.writerow(
            [
                nombre,
                url,
                reg.fecha_hora,
                "Si" if reg.esta_operativo else "No",
                reg.estado_http or "",
                reg.tiempo_respuesta_ms or "",
                reg.observacion or "",
            ]
        )
    return StreamingResponse(
        iter(["﻿" + out.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=monitoreo_umsa.csv"},
    )
