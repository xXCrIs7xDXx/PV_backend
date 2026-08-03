from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import Responsable
from app.schemas.catalogo import ResponsableCreate, ResponsableRead, ResponsableUpdate

router = APIRouter(prefix="/api/responsables", tags=["responsables"])


@router.get("", response_model=list[ResponsableRead])
async def list_responsables(
    include_deleted: bool = Query(default=False), session: AsyncSession = Depends(get_session)
):
    stmt = select(Responsable)
    if not include_deleted:
        stmt = stmt.where(Responsable.deleted_at.is_(None))
    result = await session.exec(stmt)
    return result.all()


@router.get("/{id_responsable}", response_model=ResponsableRead)
async def get_responsable(id_responsable: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Responsable, id_responsable)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return obj


@router.post("", response_model=ResponsableRead, dependencies=[Depends(require_admin)])
async def create_responsable(body: ResponsableCreate, session: AsyncSession = Depends(get_session)):
    obj = Responsable(**body.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.put("/{id_responsable}", response_model=ResponsableRead, dependencies=[Depends(require_admin)])
async def update_responsable(
    id_responsable: int, body: ResponsableUpdate, session: AsyncSession = Depends(get_session)
):
    obj = await session.get(Responsable, id_responsable)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.delete("/{id_responsable}", dependencies=[Depends(require_admin)])
async def delete_responsable(id_responsable: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(Responsable, id_responsable)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    obj.deleted_at = datetime.utcnow()
    session.add(obj)
    await session.commit()
    return {"ok": True}
