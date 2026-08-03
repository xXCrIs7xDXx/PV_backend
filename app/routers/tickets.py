from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import TicketIncidencia
from app.schemas.tickets import TicketCreate, TicketRead, TicketUpdate

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketRead])
async def list_tickets(
    estado: str | None = Query(default=None),
    id_plataforma: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(TicketIncidencia).order_by(TicketIncidencia.fecha_apertura.desc())
    if estado is not None:
        stmt = stmt.where(TicketIncidencia.estado_ticket == estado)
    if id_plataforma is not None:
        stmt = stmt.where(TicketIncidencia.id_plataforma == id_plataforma)
    result = await session.exec(stmt)
    return result.all()


@router.get("/{id_ticket}", response_model=TicketRead)
async def get_ticket(id_ticket: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(TicketIncidencia, id_ticket)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return obj


@router.post("", response_model=TicketRead, dependencies=[Depends(require_admin)])
async def create_ticket(body: TicketCreate, session: AsyncSession = Depends(get_session)):
    obj = TicketIncidencia(**body.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.patch("/{id_ticket}", response_model=TicketRead, dependencies=[Depends(require_admin)])
async def update_ticket(id_ticket: int, body: TicketUpdate, session: AsyncSession = Depends(get_session)):
    obj = await session.get(TicketIncidencia, id_ticket)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj
