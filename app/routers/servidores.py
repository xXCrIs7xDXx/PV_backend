from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import Servidor
from app.schemas.catalogo import ServidorCreate, ServidorRead, ServidorUpdate

router = APIRouter(prefix="/api/servidores", tags=["servidores"])


@router.get("", response_model=list[ServidorRead])
async def list_servidores(
    include_deleted: bool = Query(default=False), session: AsyncSession = Depends(get_session)
):
    stmt = select(Servidor)
    if not include_deleted:
        stmt = stmt.where(Servidor.deleted_at.is_(None))
    result = await session.exec(stmt)
    return result.all()


@router.get("/{id_servidor}", response_model=ServidorRead)
async def get_servidor(id_servidor: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Servidor, id_servidor)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return obj


@router.post("", response_model=ServidorRead, dependencies=[Depends(require_admin)])
async def create_servidor(body: ServidorCreate, session: AsyncSession = Depends(get_session)):
    obj = Servidor(**body.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.put("/{id_servidor}", response_model=ServidorRead, dependencies=[Depends(require_admin)])
async def update_servidor(
    id_servidor: int, body: ServidorUpdate, session: AsyncSession = Depends(get_session)
):
    obj = await session.get(Servidor, id_servidor)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.delete("/{id_servidor}", dependencies=[Depends(require_admin)])
async def delete_servidor(id_servidor: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Servidor, id_servidor)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    obj.deleted_at = datetime.utcnow()
    session.add(obj)
    await session.commit()
    return {"ok": True}
