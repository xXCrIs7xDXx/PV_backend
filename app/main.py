import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore

from app.config import settings
from app.routers import (
    auth,
    catalogos,
    dashboard,
    monitoreo,
    plataformas,
    responsables,
    servidores,
    stats,
    tickets,
    unidades,
)
from app.services.monitor_engine import run_check_cycle
from app.services.retencion_service import purgar_registros_antiguos


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ya no usamos APScheduler interno, el monitoreo se dispara externamente
    yield


app = FastAPI(title="Monitoreo Campus Virtuales UMSA", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
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
):
    app.include_router(_router)


@app.get("/")
@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "message": "Backend DTIC activo"}
