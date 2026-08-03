"""Async replacement for the legacy ThreadPoolExecutor+requests checker
(verificar_campus.py). One httpx.AsyncClient is reused for the whole cycle;
concurrency is bounded by a semaphore instead of a thread pool size."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx
from sqlalchemy import func
from sqlmodel import select

from app.config import settings
from app.db import async_session_factory
from app.models import EjecucionMonitoreo, Plataforma, RegistroMonitoreo
from app.services import forms_service, telegram_service, tickets_service

monitor_lock = asyncio.Lock()
monitor_status: dict = {"corriendo": False, "inicio": None, "ultimo_fin": None, "ultimo_error": None}

forms_lock = asyncio.Lock()
forms_status: dict = {"corriendo": False, "ultimo_fin": None, "ultimo_resultado": None, "ultimo_error": None}

# Fallos consecutivos por plataforma. Solo cuando este contador alcanza
# settings.monitor_confirm_failures se dispara la transicion de estado y la
# notificacion de Telegram, evitando falsos positivos por timeouts transitorios.
# Se resetea a 0 en cuanto la plataforma vuelve a responder correctamente.
_consecutive_failures: dict[int, int] = {}


async def _check_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> dict:
    async with sem:
        start = time.monotonic()
        try:
            resp = await client.get(url)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            operativo = resp.status_code < 400
            return {
                "estado_http": resp.status_code,
                "tiempo_respuesta_ms": elapsed_ms,
                "esta_operativo": operativo,
                "observacion": None if operativo else f"HTTP {resp.status_code}",
            }
        except httpx.TimeoutException:
            return {
                "estado_http": None,
                "tiempo_respuesta_ms": int(settings.monitor_timeout_seconds * 1000),
                "esta_operativo": False,
                "observacion": "Tiempo de espera agotado",
            }
        except httpx.ConnectError:
            return {
                "estado_http": None,
                "tiempo_respuesta_ms": int((time.monotonic() - start) * 1000),
                "esta_operativo": False,
                "observacion": "Sin conexion / dominio no responde",
            }
        except httpx.HTTPError as e:
            return {
                "estado_http": None,
                "tiempo_respuesta_ms": int((time.monotonic() - start) * 1000),
                "esta_operativo": False,
                "observacion": str(e)[:200],
            }


def clasificar_causa(observacion: str | None, estado_http: int | None) -> str:
    """Buckets a failed check's observacion into a short, stable label so
    ticket_incidencia.categoria_error can be aggregated meaningfully (see
    /api/stats/comparative's "causas comunes")."""
    if not observacion:
        return "Desconocido"
    texto = observacion.lower()
    if "tiempo de espera" in texto:
        return "Timeout"
    if "sin conexion" in texto or "dominio no responde" in texto:
        return "Sin conexion"
    if "ssl" in texto:
        return "Error SSL"
    if estado_http and estado_http >= 500:
        return f"Error HTTP {estado_http} (servidor)"
    if estado_http and estado_http >= 400:
        return f"Error HTTP {estado_http} (cliente)"
    return observacion[:100]


async def _submit_to_forms(plataformas: list[Plataforma], checks: list[dict]) -> tuple[int, int]:
    """Posts each platform's check result to both audit forms. Returns
    (enviados, fallidos) so callers can report the outcome via forms_status
    instead of only printing to the console."""
    contextos = []
    async with async_session_factory() as session:
        for plataforma in plataformas:
            contextos.append(await forms_service.resolve_contexto(session, plataforma))

    sem = asyncio.Semaphore(settings.google_forms_max_concurrency)
    enviados = 0
    fallidos = 0

    async def _uno(plataforma: Plataforma, check: dict, contexto: tuple) -> None:
        nonlocal enviados, fallidos
        facultad, servidor, responsable = contexto
        async with sem:
            resultado = await forms_service.submit_resultado(
                client, facultad, plataforma.url_principal, check, servidor, responsable
            )
            if all(v.startswith("HTTP 2") for v in resultado.values()):
                enviados += 1
            else:
                fallidos += 1
                print(f"Google Forms: fallo en {plataforma.url_principal} -> {resultado}")

    async with httpx.AsyncClient(timeout=settings.google_forms_timeout_seconds, follow_redirects=True) as client:
        await asyncio.gather(*[_uno(p, c, ctx) for p, c, ctx in zip(plataformas, checks, contextos)])

    print(f"Google Forms: {enviados} plataformas enviadas, {fallidos} con error")
    return enviados, fallidos


async def _do_forms_submission(plataformas: list[Plataforma], checks: list[dict]) -> None:
    """Shared by the automatic post-cycle send (run_check_cycle) and the
    manual 'enviar ahora' trigger (enviar_forms_manual) so forms_status -
    previously declared but never updated by either path - actually
    reflects what happened, instead of staying frozen at its initial
    value forever."""
    if forms_lock.locked():
        return
    async with forms_lock:
        forms_status.update(corriendo=True, ultimo_error=None)
        try:
            enviados, fallidos = await _submit_to_forms(plataformas, checks)
            forms_status["ultimo_resultado"] = f"{enviados} enviadas, {fallidos} con error"
        except Exception as e:  # noqa: BLE001 - surfaced via /api/monitoreo/forms-estado
            forms_status["ultimo_error"] = str(e)[:300]
        finally:
            forms_status.update(corriendo=False, ultimo_fin=datetime.utcnow())


async def enviar_forms_manual() -> None:
    """Manual 'send to Google Forms now' trigger for the dashboard button.
    Deliberately independent of GOOGLE_FORMS_ENABLED and of the monitoring
    cycle: it reuses each platform's most recent registro_monitoreo instead
    of re-pinging all servers, so an admin can push the current known state
    to the audit forms on demand without waiting for/forcing a full check."""
    async with async_session_factory() as session:
        result = await session.exec(select(Plataforma).where(Plataforma.deleted_at.is_(None)))
        plataformas = result.all()

        subq = (
            select(RegistroMonitoreo.id_plataforma, func.max(RegistroMonitoreo.fecha_hora).label("max_fecha"))
            .group_by(RegistroMonitoreo.id_plataforma)
            .subquery()
        )
        stmt = select(RegistroMonitoreo).join(
            subq,
            (RegistroMonitoreo.id_plataforma == subq.c.id_plataforma)
            & (RegistroMonitoreo.fecha_hora == subq.c.max_fecha),
        )
        ultimos = {r.id_plataforma: r for r in (await session.exec(stmt)).all()}

    checks = [
        {"esta_operativo": ultimos[p.id_plataforma].esta_operativo, "observacion": ultimos[p.id_plataforma].observacion}
        if p.id_plataforma in ultimos
        else {"esta_operativo": False, "observacion": "Sin registros de monitoreo"}
        for p in plataformas
    ]
    await _do_forms_submission(plataformas, checks)


async def run_check_cycle(tipo: str = "Automatica") -> None:
    if monitor_lock.locked():
        return
    async with monitor_lock:
        monitor_status.update(corriendo=True, inicio=datetime.utcnow(), ultimo_error=None)
        try:
            async with async_session_factory() as session:
                result = await session.exec(select(Plataforma).where(Plataforma.deleted_at.is_(None)))
                plataformas = result.all()

            sem = asyncio.Semaphore(settings.monitor_max_concurrency)
            async with httpx.AsyncClient(
                verify=False, timeout=settings.monitor_timeout_seconds, follow_redirects=True
            ) as client:
                checks = await asyncio.gather(
                    *[_check_one(client, sem, p.url_principal) for p in plataformas]
                )

            caidas: list[tuple[Plataforma, str, dict]] = []
            recuperadas: list[tuple[Plataforma, dict]] = []
            async with async_session_factory() as session:
                ejecucion = EjecucionMonitoreo(tipo=tipo)
                session.add(ejecucion)
                await session.flush()
                for plataforma_stub, check in zip(plataformas, checks):
                    plataforma = await session.get(Plataforma, plataforma_stub.id_plataforma)
                    registro = RegistroMonitoreo(
                        id_plataforma=plataforma.id_plataforma,
                        id_ejecucion=ejecucion.id_ejecucion,
                        estado_actual_snapshot=plataforma.estado_actual,
                        **check,
                    )
                    session.add(registro)
                    await session.flush()
                    categoria = clasificar_causa(check.get("observacion"), check.get("estado_http"))

                    id_plat = plataforma.id_plataforma
                    if check["esta_operativo"]:
                        # Recuperacion: resetear contador y manejar transicion de inmediato
                        _consecutive_failures.pop(id_plat, None)
                        recien_caida, recien_recuperada = await tickets_service.handle_transition(
                            session, plataforma, True, registro.id_registro, categoria
                        )
                    else:
                        # Acumular fallos consecutivos; solo confirmar caida cuando se
                        # alcanza el umbral (evita falsos positivos por timeouts puntuales)
                        _consecutive_failures[id_plat] = _consecutive_failures.get(id_plat, 0) + 1
                        if _consecutive_failures[id_plat] >= settings.monitor_confirm_failures:
                            recien_caida, recien_recuperada = await tickets_service.handle_transition(
                                session, plataforma, False, registro.id_registro, categoria
                            )
                        else:
                            recien_caida, recien_recuperada = False, False

                    if recien_caida:
                        caidas.append((plataforma, categoria, check))
                    elif recien_recuperada:
                        recuperadas.append((plataforma, check))
                await session.commit()

            # Notificar por Telegram solo despues de que el commit anterior
            # tuvo exito - igual que con Google Forms, una caida de Telegram
            # nunca debe afectar el historial de monitoreo ya guardado.
            for plataforma, categoria, check in caidas:
                await telegram_service.notificar_caida_automatica(
                    plataforma, categoria,
                    check.get("observacion"), check.get("estado_http"), check.get("tiempo_respuesta_ms"),
                )
            for plataforma, check in recuperadas:
                await telegram_service.notificar_recuperacion_automatica(
                    plataforma, check.get("tiempo_respuesta_ms")
                )

        except Exception as e:  # noqa: BLE001 - surfaced via /api/monitoreo/estado for the admins
            monitor_status["ultimo_error"] = str(e)[:300]
        finally:
            monitor_status.update(corriendo=False, ultimo_fin=datetime.utcnow())
