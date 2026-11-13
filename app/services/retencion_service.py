"""Purga las filas viejas de registro_monitoreo para que la tabla no crezca
para siempre (~70 plataformas x una fila por verificacion). Corre despues de
cada ciclo disparado por cron-job.org (GET /api/monitoreo/forzar-ejecucion),
como tarea separada: si falla, no afecta las verificaciones ni el resto de
la app.

ticket_incidencia NUNCA se purga aqui - es la bitacora de incidencias
(mucho mas liviana, una fila por incidente real, no por chequeo) y vale la
pena conservarla indefinidamente para auditoria."""

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update

from app.config import settings
from app.db import async_session_factory
from app.models import RegistroMonitoreo, TicketIncidencia


async def purgar_registros_antiguos() -> int:
    limite = datetime.utcnow() - timedelta(days=30 * settings.retencion_registros_meses)
    async with async_session_factory() as session:
        try:
            # Los tickets apuntan al registro de la falla (FK sin ON DELETE):
            # sin esto, el primer ticket con mas de N meses bloqueaba la purga
            # entera y la tabla volvia a crecer sin limite. El ticket se
            # conserva; solo pierde el enlace al registro que se borra.
            viejos = select(RegistroMonitoreo.id_registro).where(RegistroMonitoreo.fecha_hora < limite)
            await session.execute(
                update(TicketIncidencia)
                .where(TicketIncidencia.id_registro_falla.in_(viejos))
                .values(id_registro_falla=None)
            )
            result = await session.execute(delete(RegistroMonitoreo).where(RegistroMonitoreo.fecha_hora < limite))
            await session.commit()
            borrados = result.rowcount or 0
            if borrados:
                print(f"Retencion: se purgaron {borrados} registros de monitoreo anteriores a {limite.date()}")
            return borrados
        except Exception as e:  # noqa: BLE001 - la purga nunca debe afectar al monitoreo
            await session.rollback()
            print(f"Retencion: error al purgar registros antiguos -> {e}")
            return 0
