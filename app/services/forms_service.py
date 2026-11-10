"""Reemplaza los scripts de PowerShell que llenaban a mano los dos Google
Forms de auditoria de la universidad. Solo se dispara con el boton "Enviar a
Google Forms" del panel (POST /api/monitoreo/enviar-forms): cada envio
escribe filas REALES y permanentes en las planillas de produccion, no es un
entorno de pruebas.

Field mapping (entry.* ids) was supplied by the DTIC and must match the
forms' real fields exactly:

Formulario 1 (principal): Facultad, URL, IP, Funciona ("Si"/"No"),
Observacion, Nombre del responsable, Celular, Correo, Actualizacion
(always sent as "" per the DTIC's own legacy script behaviour).

Formulario 2 (secundario): Facultad, URL, Funciona, Observacion.
"""

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Plataforma, Responsable, Servidor, UnidadAcademica

FORM1_URL = "https://docs.google.com/forms/d/e/1FAIpQLSdVQ64T-DwPklarms4u7xCyyxAT3mxsdip_bjXr6jEIJLaNig/formResponse"
FORM2_URL = "https://docs.google.com/forms/d/e/1FAIpQLSe5w9NWj6s9Ejccc11i_wNaa0moYbaQuk1EQGF4Uc64nv3Xnw/formResponse"

# Google silently treats requests without a browser-like User-Agent as bot
# traffic on some forms (the request still gets a 200 back, but the
# response is never recorded) - verified against the real forms' field
# structure (entry ids, types, required flags all match) on 2026-06-17, so
# this header is the most likely fix for "POST succeeds but nothing shows
# up in the sheet".
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


async def get_facultad_nombre(session: AsyncSession, id_unidad: int) -> str:
    """Walks id_unidad_padre up to the root academic unit - in our schema
    that root is what the audit forms call "Facultad"."""
    unidad = await session.get(UnidadAcademica, id_unidad)
    while unidad and unidad.id_unidad_padre is not None:
        unidad = await session.get(UnidadAcademica, unidad.id_unidad_padre)
    return unidad.nombre if unidad else "Desconocido"


async def resolve_contexto(
    session: AsyncSession, plataforma: Plataforma
) -> tuple[str, Servidor | None, Responsable | None]:
    """Sequential DB reads done once per platform before the concurrent
    POST phase - AsyncSession is not safe to share across concurrent
    coroutines, so all session access happens here, not in submit_resultado."""
    facultad = await get_facultad_nombre(session, plataforma.id_unidad)
    servidor = await session.get(Servidor, plataforma.id_servidor)
    responsable = await session.get(Responsable, plataforma.id_responsable)
    return facultad, servidor, responsable


def _build_form1_payload(
    facultad: str, url: str, ip: str | None, funciona: str, observacion: str | None, responsable: Responsable | None
) -> dict:
    return {
        "entry.591184759": facultad,
        "entry.597854097": url,
        "entry.1032511652": ip or "",
        "entry.664926929": funciona,
        "entry.2128549437": observacion or "",
        "entry.1898741510": responsable.nombre_completo if responsable else "",
        "entry.1650155913": responsable.telefono or "" if responsable else "",
        "entry.1025622666": responsable.correo_institucional if responsable else "",
        "entry.313481142": "",
    }


def _build_form2_payload(facultad: str, url: str, funciona: str, observacion: str | None) -> dict:
    return {
        "entry.1328385488": facultad,
        "entry.1376873309": url,
        "entry.900937645": funciona,
        "entry.2055124839": observacion or "",
    }


async def submit_resultado(
    client: httpx.AsyncClient,
    facultad: str,
    url_principal: str,
    check: dict,
    servidor: Servidor | None,
    responsable: Responsable | None,
) -> dict[str, str]:
    """Posts the check result to both audit forms. Each form POST is
    independently try/except'd - a Google Forms outage or rejected payload
    must never break the monitoring cycle or its Postgres writes (which
    have already committed by the time this runs, see monitor_engine.py).
    Returns a small status report per form for logging/diagnostics."""
    ip = (servidor.ip_publica or servidor.ip_privada) if servidor else None
    funciona = "Si" if check["esta_operativo"] else "No"
    observacion = check.get("observacion")

    payloads = (
        ("form1", FORM1_URL, _build_form1_payload(facultad, url_principal, ip, funciona, observacion, responsable)),
        ("form2", FORM2_URL, _build_form2_payload(facultad, url_principal, funciona, observacion)),
    )
    resultado: dict[str, str] = {}
    for nombre, form_url, payload in payloads:
        try:
            resp = await client.post(form_url, data=payload, headers=REQUEST_HEADERS)
            resultado[nombre] = f"HTTP {resp.status_code}"
        except httpx.HTTPError as e:
            resultado[nombre] = f"ERROR {e}"
    return resultado
