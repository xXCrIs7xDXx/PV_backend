"""Enlace en memoria entre el backend (Render) y el agente de respaldos.

Por que existe: Neon (plan gratuito) suspende el computo a los 5 minutos sin
consultas y solo cobra las horas activas. Si el agente preguntara a la base
cada minuto "¿hay solicitudes del panel?" y guardara ahi su latido, Neon
quedaria despierto las 24 horas. En su lugar el agente llama cada minuto a
POST /api/backups/agente/latido, que responde DESDE MEMORIA (sin tocar la
base), y solo consulta la base cuando de verdad hay una solicitud.

De paso, esa llamada por minuto mantiene despierto a Render mientras la PC
esta encendida (igual que el keep-alive de cron-job.org).

Todo esto se pierde cuando Render reinicia el servicio; por eso:
  - `pendiente` arranca en None ("no se sabe") y la primera vez se consulta la base;
  - el latido se guarda en la base una vez por hora, aprovechando que el
    ciclo de monitoreo ya la desperto (ver backups_vigilancia.revisar)."""
from __future__ import annotations

from datetime import datetime

# None = desconocido (recien arrancado) | True = hay solicitudes | False = no hay
_pendiente: bool | None = None
# Sube con cada solicitud nueva: el agente confirma "no queda nada" indicando
# la version que vio, asi no se pisa una solicitud que llego en el medio.
_version: int = 0
# host -> ultimo latido recibido (campos de BackupAgente)
_latidos: dict[str, dict] = {}


def nueva_solicitud() -> None:
    global _pendiente, _version
    _pendiente = True
    _version += 1


def pendiente() -> bool | None:
    return _pendiente


def version() -> int:
    return _version


def fijar_pendiente(valor: bool) -> None:
    global _pendiente
    _pendiente = valor


def confirmar_sin_solicitudes(version_vista: int) -> None:
    global _pendiente
    if version_vista == _version:
        _pendiente = False


def registrar_latido(host: str, datos: dict) -> None:
    _latidos[host] = {**datos, "host": host, "ultimo_latido": datetime.utcnow()}


def latidos() -> dict[str, dict]:
    return dict(_latidos)
