from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.deps import require_admin
from app.schemas.informe import InformeDatos, InformePdfRequest
from app.services import informe_service

router = APIRouter(prefix="/api/informes", tags=["informes"])


@router.get("/datos", response_model=InformeDatos)
async def datos_informe(
    fecha: date | None = Query(
        default=None, description="Dia del historial (YYYY-MM-DD, hora de Bolivia). Sin fecha = estado actual."
    ),
    session: AsyncSession = Depends(get_session),
) -> InformeDatos:
    """Filas del informe con su descripcion automatica. Solo las plataformas
    'En Revision' vienen con editable=true para que el frontend permita
    modificar su descripcion antes de generar el PDF. Con `fecha` se usa la
    ultima verificacion registrada ese dia (historial)."""
    return await informe_service.obtener_datos(session, fecha)


@router.post("/pdf", dependencies=[Depends(require_admin)])
async def generar_pdf(body: InformePdfRequest, session: AsyncSession = Depends(get_session)) -> Response:
    """Genera el PDF con los datos actuales y las descripciones escritas por
    el admin (solo se aplican a plataformas 'En Revision'). Se devuelve
    'inline' para que el frontend lo previsualice en un iframe y luego lo
    descargue con el nombre de Content-Disposition. No guarda nada."""
    datos = await informe_service.obtener_datos(session, body.fecha, body.estados)
    informe_service.aplicar_descripciones(datos, body.descripciones)
    pdf = await run_in_threadpool(informe_service.construir_pdf, datos)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{datos.nombre_archivo}"',
            "Cache-Control": "no-store",
        },
    )
