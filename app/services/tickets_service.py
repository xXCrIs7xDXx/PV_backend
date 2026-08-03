"""Opens/closes ticket_incidencia automatically on plataforma state transitions.
Operativo->En Revision opens a ticket (unless one is already open); the
reverse transition closes it when AUTO_CLOSE_TICKETS is enabled (see
config.py). "En Revision" (not "Caido") is intentional: a failed check is
often a transient error or a maintenance window that needs a human look,
not necessarily a hard outage."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models import Plataforma, TicketIncidencia

ESTADO_OPERATIVO = "Operativo"
ESTADO_EN_REVISION = "En Revision"
AUTO_MANAGED_STATES = {ESTADO_OPERATIVO, ESTADO_EN_REVISION}

# Manual overrides an admin can set from the dashboard (PUT /api/plataformas/{id}).
# Keep this in sync with frontend/src/lib/estados.ts. Any estado_actual value
# outside AUTO_MANAGED_STATES pauses automatic status flips and ticket
# open/close for that platform (see the guard in handle_transition) until an
# admin reverts it back to "Operativo" or "En Revision" - that's the whole
# point of marking something "En Mantenimiento": stop the noise while it's
# being worked on.
ESTADOS_MANUALES = ["En Mantenimiento", "Error", "Fuera de Servicio"]


async def handle_transition(
    session: AsyncSession,
    plataforma: Plataforma,
    now_operativo: bool,
    id_registro: int,
    categoria: str = "Desconocido",
) -> tuple[bool, bool]:
    """Devuelve (recien_caida, recien_recuperada) para que el llamador
    pueda notificar por Telegram *despues* de que el commit de esta
    transaccion haya tenido exito - ver run_check_cycle en
    monitor_engine.py). En cualquier otro caso (ya estaba caida, o hay 
    un override manual activo) devuelve (False, False), así que
    nunca se notifica mas de una vez por la misma caida o recuperacion."""
    if plataforma.estado_actual not in AUTO_MANAGED_STATES:
        # Admin override in effect (e.g. "En Mantenimiento") - the ping was
        # still recorded in registro_monitoreo by the caller, but we don't
        # touch estado_actual or tickets until the admin clears the override.
        return False, False

    was_operativo = plataforma.estado_actual != ESTADO_EN_REVISION
    plataforma.estado_actual = ESTADO_OPERATIVO if now_operativo else ESTADO_EN_REVISION
    session.add(plataforma)

    if was_operativo and not now_operativo:
        existing = await session.exec(
            select(TicketIncidencia).where(
                TicketIncidencia.id_plataforma == plataforma.id_plataforma,
                TicketIncidencia.estado_ticket == "Abierto",
            )
        )
        if existing.first() is None:
            session.add(
                TicketIncidencia(
                    id_plataforma=plataforma.id_plataforma,
                    id_registro_falla=id_registro,
                    id_responsable_revision=plataforma.id_responsable,
                    categoria_error=categoria,
                    estado_ticket="Abierto",
                )
            )
        return True, False
    elif not was_operativo and now_operativo:
        if settings.auto_close_tickets:
            result = await session.exec(
                select(TicketIncidencia).where(
                    TicketIncidencia.id_plataforma == plataforma.id_plataforma,
                    TicketIncidencia.estado_ticket == "Abierto",
                )
            )
            for ticket in result.all():
                ticket.estado_ticket = "Cerrado"
                ticket.fecha_cierre = datetime.utcnow()
                ticket.accion_realizada = ticket.accion_realizada or "Recuperado automaticamente por el monitor"
                session.add(ticket)
        return False, True
    return False, False
