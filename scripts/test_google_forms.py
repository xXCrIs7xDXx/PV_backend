"""Sends exactly ONE real, clearly-marked test submission to both audit
Google Forms, independent of GOOGLE_FORMS_ENABLED and the monitoring cycle.

This writes one permanent row to each of the real production audit sheets
(observacion is tagged "PRUEBA - puedes borrar esta fila" so it's easy to
find and delete afterwards). Only run this when you intend to verify the
integration end-to-end.

Usage:
    python -m scripts.test_google_forms
"""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import forms_service  # noqa: E402


async def main() -> None:
    check = {"esta_operativo": True, "observacion": "PRUEBA - puedes borrar esta fila"}
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        resultado = await forms_service.submit_resultado(
            client,
            facultad="PRUEBA DTIC",
            url_principal="https://ejemplo-prueba.umsa.bo/",
            check=check,
            servidor=None,
            responsable=None,
        )
    print("Resultado:", resultado)
    if all(v.startswith("HTTP 2") for v in resultado.values()):
        print("Ambos forms respondieron 2xx. Revisa la planilla de respuestas para confirmar que la fila aparecio.")
    else:
        print("Al menos un form fallo - revisa el detalle arriba.")


if __name__ == "__main__":
    asyncio.run(main())
