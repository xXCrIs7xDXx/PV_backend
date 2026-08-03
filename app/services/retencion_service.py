"""Purga las filas viejas de registro_monitoreo para que la tabla no crezca
para siempre (~68 plataformas x una fila por verificacion). Corre como un
job diario de APScheduler (ver app/main.py), totalmente separado del ciclo
de monitoreo: si falla, no debe afectar las verificaciones ni el resto de
la app.

ticket_incidencia NUNCA se purga aqui - es la bitacora de incidencias
(mucho mas liviana, una fila por incidente real, no por chequeo) y vale la
pena conservarla indefinidamente para auditoria."""

from datetime import datetime, timedelta

from sqlalchemy import delete

from app.config import settings
from app.db import async_session_factory
from app.models import RegistroMonitoreo


async def purgar_registros_antiguos() -> int:
    limite = datetime.utcnow() - timedelta(days=30 * settings.retencion_registros_meses)
    async with async_session_factory() as session:
        try:
            result = await session.execute(delete(RegistroMonitoreo).where(RegistroMonitoreo.fecha_hora < limite))
            await session.commit()
            borrados = result.rowcount or 0
            if borrados:
                print(f"Retencion: se purgaron {borrados} registros de monitoreo anteriores a {limite.date()}")
            return borrados
        except Exception as e:  # noqa: BLE001 - este job nunca debe tirar abajo el scheduler
            await session.rollback()
            print(f"Retencion: error al purgar registros antiguos -> {e}")
            return 0
