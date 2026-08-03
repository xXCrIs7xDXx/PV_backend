"""Notifica por Telegram cuando una plataforma deja de estar Operativa -
tanto si lo detecta el monitor automatico (Operativo -> En Revision) como
si lo hace un admin manualmente (Operativo -> En Mantenimiento/Error/Fuera
de Servicio). Apagado por defecto (TELEGRAM_ENABLED=false); si Telegram
falla o no esta configurado, esto nunca debe romper el ciclo de monitoreo
ni el endpoint que lo dispara - mismo criterio de "fail-soft" que
forms_service.py.

Importante para evitar saturar el chat: esta funcion se llama una sola vez
por transicion real hacia un estado no operativo, nunca en cada ciclo
mientras la plataforma sigue caida (ver el guard en
tickets_service.handle_transition y en routers/plataformas.py, que solo
disparan la notificacion cuando el estado ANTERIOR era "Operativo")."""
from __future__ import annotations

import httpx

from app.config import settings
from app.models import Plataforma

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def _enviar_mensaje(texto: str) -> None:
    if not settings.telegram_enabled or not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    url = TELEGRAM_API_URL.format(token=settings.telegram_bot_token)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                data={"chat_id": settings.telegram_chat_id, "text": texto, "parse_mode": "Markdown"},
            )
            if resp.status_code != 200:
                print(f"Telegram: fallo al enviar notificacion -> HTTP {resp.status_code} {resp.text[:200]}")
    except httpx.HTTPError as e:
        print(f"Telegram: error al enviar notificacion -> {e}")


async def notificar_caida_automatica(
    plataforma: Plataforma,
    categoria: str,
    observacion: str | None = None,
    estado_http: int | None = None,
    tiempo_ms: int | None = None,
) -> None:
    lineas = [f"\U0001F534 *{plataforma.nombre_plataforma}* no responde", ""]
    if estado_http:
        lineas.append(f"📋 Error: HTTP {estado_http} — {categoria}")
    elif observacion:
        lineas.append(f"📋 Error: {observacion}")
    else:
        lineas.append(f"📋 Error: {categoria}")
    if tiempo_ms is not None:
        lineas.append(f"⏱ Tiempo: {tiempo_ms:,}ms")
    lineas.append(f"🌐 URL: {plataforma.url_principal}")
    await _enviar_mensaje("\n".join(lineas))


async def notificar_recuperacion_automatica(
    plataforma: Plataforma,
    tiempo_ms: int | None = None,
) -> None:
    lineas = [f"✅ *{plataforma.nombre_plataforma}* volvió a estar Operativo", ""]
    if tiempo_ms is not None:
        lineas.append(f"⏱ Respuesta: {tiempo_ms:,}ms")
    lineas.append(f"🌐 URL: {plataforma.url_principal}")
    await _enviar_mensaje("\n".join(lineas))


async def notificar_cambio_manual(plataforma: Plataforma, estado_nuevo: str) -> None:
    texto = (
        f"⚠️ *{plataforma.nombre_plataforma}* fue cambiada manualmente a {estado_nuevo}.\n"
        f"🌐 URL: {plataforma.url_principal}"
    )
    await _enviar_mensaje(texto)
