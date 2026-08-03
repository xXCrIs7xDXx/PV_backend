from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import Plataforma, RegistroMonitoreo, Responsable, Servidor, TipoPlataforma, UnidadAcademica
from app.schemas.plataforma import (
    PlataformaCreate,
    PlataformaDetalle,
    PlataformaRead,
    PlataformaReadExpanded,
    PlataformaUpdate,
)
from app.services import telegram_service
from app.services.unidades_display import build_unidad_display_names

router = APIRouter(prefix="/api/plataformas", tags=["plataformas"])


@router.get("", response_model=list[PlataformaReadExpanded])
async def list_plataformas(
    id_unidad: int | None = Query(default=None),
    id_servidor: int | None = Query(default=None),
    estado_actual: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    latest_sub = (
        select(
            RegistroMonitoreo.id_plataforma,
            func.max(RegistroMonitoreo.id_registro).label("max_id"),
        )
        .group_by(RegistroMonitoreo.id_plataforma)
        .subquery()
    )

    stmt = (
        select(
            Plataforma,
            UnidadAcademica.nombre,
            Servidor.hostname,
            Responsable.nombre_completo,
            TipoPlataforma.nombre_tipo,
            RegistroMonitoreo.estado_http,
            RegistroMonitoreo.tiempo_respuesta_ms,
            RegistroMonitoreo.fecha_hora,
        )
        .join(UnidadAcademica, Plataforma.id_unidad == UnidadAcademica.id_unidad)
        .join(Servidor, Plataforma.id_servidor == Servidor.id_servidor)
        .join(Responsable, Plataforma.id_responsable == Responsable.id_responsable)
        .join(TipoPlataforma, Plataforma.id_tipo_plataforma == TipoPlataforma.id_tipo_plataforma)
        .outerjoin(latest_sub, latest_sub.c.id_plataforma == Plataforma.id_plataforma)
        .outerjoin(RegistroMonitoreo, RegistroMonitoreo.id_registro == latest_sub.c.max_id)
    )
    if not include_deleted:
        stmt = stmt.where(Plataforma.deleted_at.is_(None))
    if id_unidad is not None:
        stmt = stmt.where(Plataforma.id_unidad == id_unidad)
    if id_servidor is not None:
        stmt = stmt.where(Plataforma.id_servidor == id_servidor)
    if estado_actual is not None:
        stmt = stmt.where(Plataforma.estado_actual == estado_actual)
    stmt = stmt.order_by(Plataforma.nombre_plataforma)

    rows = (await session.exec(stmt)).all()
    unidad_display = await build_unidad_display_names(session)
    out = []
    for plat, nombre_unidad, hostname, nombre_responsable, nombre_tipo, estado_http, tiempo_ms, fecha in rows:
        out.append(
            PlataformaReadExpanded(
                **plat.model_dump(),
                nombre_unidad=unidad_display.get(plat.id_unidad, nombre_unidad),
                nombre_servidor=hostname,
                nombre_responsable=nombre_responsable,
                nombre_tipo_plataforma=nombre_tipo,
                ultimo_estado_http=estado_http,
                ultimo_tiempo_respuesta_ms=tiempo_ms,
                ultima_verificacion=fecha,
            )
        )
    return out


@router.get("/{id_plataforma}/detalle", response_model=PlataformaDetalle)
async def get_plataforma_detalle(id_plataforma: int, session: AsyncSession = Depends(get_session)):
    plataforma = await session.get(Plataforma, id_plataforma)
    if not plataforma:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")

    unidad = await session.get(UnidadAcademica, plataforma.id_unidad)
    servidor = await session.get(Servidor, plataforma.id_servidor)
    responsable = await session.get(Responsable, plataforma.id_responsable)
    unidad_display = await build_unidad_display_names(session)

    historial_stmt = (
        select(RegistroMonitoreo)
        .where(RegistroMonitoreo.id_plataforma == id_plataforma)
        .order_by(RegistroMonitoreo.fecha_hora.desc())
        .limit(20)
    )
    historial = (await session.exec(historial_stmt)).all()

    return PlataformaDetalle(
        plataforma=plataforma,
        nombre_unidad=unidad_display.get(plataforma.id_unidad, unidad.nombre if unidad else "—"),
        servidor=servidor,
        responsable=responsable,
        historial=historial,
    )


@router.get("/{id_plataforma}", response_model=PlataformaRead)
async def get_plataforma(id_plataforma: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Plataforma, id_plataforma)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return obj


@router.post("", response_model=PlataformaRead, dependencies=[Depends(require_admin)])
async def create_plataforma(body: PlataformaCreate, session: AsyncSession = Depends(get_session)):
    obj = Plataforma(**body.model_dump())
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Esa URL ya esta registrada en otra plataforma")
    await session.refresh(obj)
    return obj


@router.put("/{id_plataforma}", response_model=PlataformaRead, dependencies=[Depends(require_admin)])
async def update_plataforma(
    id_plataforma: int, body: PlataformaUpdate, session: AsyncSession = Depends(get_session)
):
    obj = await session.get(Plataforma, id_plataforma)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    estado_anterior = obj.estado_actual
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Esa URL ya esta registrada en otra plataforma")
    await session.refresh(obj)

    # Notificar por Telegram solo cuando un admin saca manualmente una
    # plataforma de "Operativo" - igual que la caida automatica, esto no
    # debe disparar nada si ya estaba en cualquier otro estado (evita
    # notificar de nuevo cada vez que se edita una plataforma ya caida).
    if estado_anterior == "Operativo" and obj.estado_actual not in (None, "Operativo"):
        await telegram_service.notificar_cambio_manual(obj, obj.estado_actual)

    return obj


@router.delete("/{id_plataforma}", dependencies=[Depends(require_admin)])
async def delete_plataforma(id_plataforma: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Plataforma, id_plataforma)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    obj.deleted_at = datetime.utcnow()
    session.add(obj)
    await session.commit()
    return {"ok": True}
