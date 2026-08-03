from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import UnidadAcademica
from app.schemas.catalogo import UnidadAcademicaCreate, UnidadAcademicaRead, UnidadAcademicaUpdate

router = APIRouter(prefix="/api/unidades", tags=["unidades"])


@router.get("", response_model=list[UnidadAcademicaRead])
async def list_unidades(
    id_padre: int | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(UnidadAcademica)
    if not include_deleted:
        stmt = stmt.where(UnidadAcademica.deleted_at.is_(None))
    if id_padre is not None:
        stmt = stmt.where(UnidadAcademica.id_unidad_padre == id_padre)
    result = await session.exec(stmt)
    return result.all()


@router.get("/{id_unidad}", response_model=UnidadAcademicaRead)
async def get_unidad(id_unidad: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(UnidadAcademica, id_unidad)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return obj


@router.post("", response_model=UnidadAcademicaRead, dependencies=[Depends(require_admin)])
async def create_unidad(body: UnidadAcademicaCreate, session: AsyncSession = Depends(get_session)):
    obj = UnidadAcademica(**body.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.put("/{id_unidad}", response_model=UnidadAcademicaRead, dependencies=[Depends(require_admin)])
async def update_unidad(
    id_unidad: int, body: UnidadAcademicaUpdate, session: AsyncSession = Depends(get_session)
):
    obj = await session.get(UnidadAcademica, id_unidad)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.delete("/{id_unidad}", dependencies=[Depends(require_admin)])
async def delete_unidad(id_unidad: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(UnidadAcademica, id_unidad)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    obj.deleted_at = datetime.utcnow()
    session.add(obj)
    await session.commit()
    return {"ok": True}
