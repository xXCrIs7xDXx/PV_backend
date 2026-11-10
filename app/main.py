import asyncio
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# psycopg/asyncpg en Windows necesitan el event loop "selector" (solo afecta
# al desarrollo local; Render corre en Linux).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]

from app.config import settings  # noqa: E402
from app.routers import (  # noqa: E402
    auth,
    catalogos,
    dashboard,
    informes,
    monitoreo,
    plataformas,
    responsables,
    servidores,
    stats,
    tickets,
    unidades,
)

# No hay scheduler interno: Render (plan gratuito) duerme el servicio, asi que
# el ciclo de monitoreo lo dispara cron-job.org llamando a
# GET /api/monitoreo/forzar-ejecucion cada hora (ver README).
app = FastAPI(title="Monitoreo Campus Virtuales UMSA")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
    # Para que el frontend pueda leer el nombre del PDF del informe
    # ("Monitorizacion dd-mm-aaaa hh.mm.pdf") al descargarlo.
    expose_headers=["Content-Disposition"],
)

for _router in (
    auth.router,
    dashboard.router,
    catalogos.router,
    unidades.router,
    responsables.router,
    servidores.router,
    plataformas.router,
    monitoreo.router,
    tickets.router,
    stats.router,
    informes.router,
):
    app.include_router(_router)


@app.get("/")
@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    """Keep-alive de cron-job.org (cada 10 min) para que Render no duerma."""
    return {"status": "ok", "message": "Backend DTIC activo"}
