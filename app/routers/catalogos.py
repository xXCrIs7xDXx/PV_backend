from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import TipoPlataforma, TipoUnidadAcademica
from app.schemas.catalogo import (
    TipoPlataformaCreate,
    TipoPlataformaRead,
    TipoUnidadAcademicaCreate,
    TipoUnidadAcademicaRead,
)

router = APIRouter(prefix="/api", tags=["catalogos"])


@router.get("/tipos-unidad", response_model=list[TipoUnidadAcademicaRead])
async def list_tipos_unidad(session: AsyncSession = Depends(get_session)):
    result = await session.exec(select(TipoUnidadAcademica))
    return result.all()


@router.post("/tipos-unidad", response_model=TipoUnidadAcademicaRead, dependencies=[Depends(require_admin)])
async def create_tipo_unidad(body: TipoUnidadAcademicaCreate, session: AsyncSession = Depends(get_session)):
    obj = TipoUnidadAcademica(**body.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.delete("/tipos-unidad/{id_tipo_unidad}", dependencies=[Depends(require_admin)])
async def delete_tipo_unidad(id_tipo_unidad: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(TipoUnidadAcademica, id_tipo_unidad)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    try:
        await session.delete(obj)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Tipo en uso por unidades academicas existentes")
    return {"ok": True}


@router.get("/tipos-plataforma", response_model=list[TipoPlataformaRead])
async def list_tipos_plataforma(session: AsyncSession = Depends(get_session)):
    result = await session.exec(select(TipoPlataforma))
    return result.all()


@router.post("/tipos-plataforma", response_model=TipoPlataformaRead, dependencies=[Depends(require_admin)])
async def create_tipo_plataforma(body: TipoPlataformaCreate, session: AsyncSession = Depends(get_session)):
    obj = TipoPlataforma(**body.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.delete("/tipos-plataforma/{id_tipo_plataforma}", dependencies=[Depends(require_admin)])
async def delete_tipo_plataforma(id_tipo_plataforma: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(TipoPlataforma, id_tipo_plataforma)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    try:
        await session.delete(obj)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Tipo en uso por plataformas existentes")
    return {"ok": True}
