"""Ciclo de monitoreo: verifica todas las plataformas con un solo
httpx.AsyncClient (concurrencia limitada por un semaforo), guarda una fila en
registro_monitoreo por plataforma y abre/cierra tickets y notifica por
Telegram cuando cambia el estado."""
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


async def _fallos_previos(session, ids: list[int], cuantos: int) -> dict[int, int]:
    """Fallos consecutivos que cada plataforma ya acumulaba ANTES de este
    ciclo, leidos del historial (registro_monitoreo). Antes se llevaba en
    memoria y se perdia cada vez que Render reiniciaba el servicio, con lo
    que una caida real podia no confirmarse nunca."""
    if cuantos <= 0 or not ids:
        return {}
    rn = (
        func.row_number()
        .over(partition_by=RegistroMonitoreo.id_plataforma, order_by=RegistroMonitoreo.id_registro.desc())
        .label("rn")
    )
    sub = (
        select(RegistroMonitoreo.id_plataforma, RegistroMonitoreo.esta_operativo, rn)
        .where(RegistroMonitoreo.id_plataforma.in_(ids))
        .subquery()
    )
    filas = (
        await session.exec(
            select(sub.c.id_plataforma, sub.c.esta_operativo).where(sub.c.rn <= cuantos).order_by(sub.c.rn)
        )
    ).all()
    racha: dict[int, int] = {}
    cortada: set[int] = set()
    for id_plat, operativo in filas:  # del mas reciente al mas antiguo
        if id_plat in cortada:
            continue
        if operativo:
            cortada.add(id_plat)
        else:
            racha[id_plat] = racha.get(id_plat, 0) + 1
    return racha


async def _check_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> dict:
    async with sem:
        start = time.monotonic()
        try:
            # strip(): una URL guardada con un tab/espacio al final (copiada de
            # Excel) hacia fallar el ciclo entero el 09/11/2026
            resp = await client.get(url.strip())
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
        except Exception as e:  # noqa: BLE001 - una URL invalida no debe tirar el ciclo de TODAS
            return {
                "estado_http": None,
                "tiempo_respuesta_ms": int((time.monotonic() - start) * 1000),
                "esta_operativo": False,
                "observacion": str(e)[:200] or type(e).__name__,
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
    """Envia y deja el resultado en forms_status (lo muestra el panel)."""
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
    """Boton "Enviar a Google Forms" del panel. Usa el ultimo
    registro_monitoreo de cada plataforma (no vuelve a verificar los
    servidores), asi el admin envia el ultimo estado conocido cuando quiera."""
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
                # Se lee ANTES de insertar los registros de este ciclo
                previos = await _fallos_previos(
                    session, [p.id_plataforma for p in plataformas], settings.monitor_confirm_failures - 1
                )
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

                    if check["esta_operativo"]:
                        # Recuperacion: se maneja de inmediato
                        recien_caida, recien_recuperada = await tickets_service.handle_transition(
                            session, plataforma, True, registro.id_registro, categoria
                        )
                    else:
                        # Solo se confirma la caida tras MONITOR_CONFIRM_FAILURES
                        # fallos seguidos (evita falsos positivos por timeouts puntuales)
                        consecutivos = previos.get(plataforma.id_plataforma, 0) + 1
                        if consecutivos >= settings.monitor_confirm_failures:
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
