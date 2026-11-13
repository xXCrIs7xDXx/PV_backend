"""Vigila desde el backend (Render) al agente de respaldos que corre en el PC
con Windows. El agente no puede avisar si el PC esta apagado o si se colgo,
asi que el ciclo horario de monitoreo (GET /api/monitoreo/forzar-ejecucion)
llama a revisar() y avisa por Telegram UNA vez por cada problema nuevo.

Si las tablas backup_* todavia no existen (migracion sin aplicar) o el
modulo no se instalo (sin agentes registrados), no hace nada."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html import escape

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import async_session_factory
from app.models import BackupAgente, BackupEjecucion
from app.services import backups_enlace

AGENTE_ACTIVO = timedelta(minutes=5)  # el servicio late cada minuto
AGENTE_CAIDO = timedelta(hours=2)
SIN_EJECUCION = timedelta(hours=26)
BLOQUEADA = timedelta(minutes=30)
_BOLIVIA = timezone(timedelta(hours=-4))


def _local(utc: datetime | None) -> str:
    if utc is None:
        return "nunca"
    return utc.replace(tzinfo=timezone.utc).astimezone(_BOLIVIA).strftime("%d/%m/%Y %H:%M")


async def diagnosticar(session: AsyncSession) -> list[tuple[str, str]]:
    """Lista de (clave, mensaje) con los problemas actuales del modulo."""
    ahora = datetime.utcnow()
    agentes = (await session.exec(select(BackupAgente))).all()
    if not agentes:
        return []
    vivos = backups_enlace.latidos()
    problemas: list[tuple[str, str]] = []
    for ag in agentes:
        # El latido por minuto vive en memoria; la base solo se actualiza cada hora
        latido, accesible = ag.ultimo_latido, ag.origen_accesible
        vivo = vivos.get(ag.host)
        if vivo and (latido is None or vivo["ultimo_latido"] >= latido):
            latido = vivo["ultimo_latido"]
            if vivo.get("origen_accesible") is not None:
                accesible = vivo["origen_accesible"]
        if latido is None or ahora - latido > AGENTE_CAIDO:
            problemas.append((
                f"caido:{ag.host}",
                f"El agente de respaldos en {ag.host} no da senales desde {_local(latido)} "
                "(¿PC apagado, sesion cerrada o servicio detenido?)",
            ))
        elif not accesible:
            problemas.append((f"origen:{ag.host}", f"En {ag.host} la carpeta de Google Drive no esta accesible"))
    ultima = (await session.exec(select(BackupEjecucion).order_by(BackupEjecucion.inicio.desc()).limit(1))).first()
    if ultima is None or (ultima.inicio and ahora - ultima.inicio > SIN_EJECUCION):
        problemas.append(("sin_ejecucion", f"No hay ejecuciones de respaldo desde {_local(ultima.inicio if ultima else None)}"))
    elif ultima.estado == "EN_CURSO" and ultima.ultimo_latido and ahora - ultima.ultimo_latido > BLOQUEADA:
        problemas.append((
            f"bloqueada:{ultima.id_ejecucion}",
            f"La ejecucion de respaldo #{ultima.id_ejecucion} no avanza desde {_local(ultima.ultimo_latido)}",
        ))
    return problemas


async def _guardar_latidos(session: AsyncSession) -> None:
    """Pasa a la base los latidos que estan en memoria (una vez por hora,
    cuando el ciclo de monitoreo ya desperto a Neon): asi sobreviven a un
    reinicio de Render sin que el agente escriba en la base cada minuto."""
    for host, vivo in backups_enlace.latidos().items():
        ag = await session.get(BackupAgente, host)
        if ag is None:
            ag = BackupAgente(host=host)
        elif ag.ultimo_latido and ag.ultimo_latido >= vivo["ultimo_latido"]:
            continue
        for campo, valor in vivo.items():
            if valor is None or campo == "host":
                continue
            setattr(ag, campo, json.dumps(valor, ensure_ascii=False) if campo == "configuracion" else valor)
        session.add(ag)
    await session.commit()


async def revisar() -> None:
    try:
        async with async_session_factory() as session:
            await _guardar_latidos(session)
            problemas = await diagnosticar(session)
            agentes = (await session.exec(select(BackupAgente).order_by(BackupAgente.ultimo_latido.desc()))).all()
            if not agentes:
                return
            ag = agentes[0]
            clave = "|".join(sorted(k for k, _ in problemas))[:200]
            if clave == (ag.alerta_clave or ""):
                return
            avisados = set((ag.alerta_clave or "").split("|"))
            ag.alerta_clave = clave
            session.add(ag)
            await session.commit()
        nuevos = [m for k, m in problemas if k not in avisados]
        if nuevos:
            from app.services import telegram_service

            texto = "\U0001F534 <b>Respaldos Drive → D:</b>\n" + "\n".join("• " + escape(m) for m in nuevos)
            await telegram_service.enviar_alerta(texto)
    except Exception as e:  # noqa: BLE001 - nunca afecta al monitoreo de plataformas
        print(f"Vigilancia de respaldos: {type(e).__name__}: {str(e)[:200]}")
