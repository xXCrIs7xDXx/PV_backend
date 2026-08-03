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
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_check_cycle,
        "interval",
        hours=settings.monitor_interval_hours,
        id="monitor_cycle",
        next_run_time=datetime.now(),
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        purgar_registros_antiguos,
        "interval",
        hours=24,
        id="purga_registros",
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)


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


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
