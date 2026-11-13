"""Panel del modulo de respaldos Google Drive -> D:.

A diferencia del resto de la API, TODAS las rutas exigen sesion admin
(incluidos los GET): los nombres, rutas y hashes de los respaldos
institucionales no deben ser publicos.

El backend (Render) no ve el PC con Windows: "Ejecutar ahora" y "Reintentar
fallidos" dejan una fila en backup_solicitud que el agente local toma en
menos de un minuto (BACKUPS_INTERVALO_SONDEO_SEGUNDOS)."""
import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models import (
    BackupAgente,
    BackupArchivo,
    BackupEjecucion,
    BackupEvento,
    BackupRespaldo,
    BackupServidor,
    BackupSolicitud,
)
from app.schemas.backups import (
    AgenteLatido,
    AgenteLatidoRespuesta,
    BackupAgenteRead,
    BackupArchivoRead,
    BackupEjecucionDetalle,
    BackupEjecucionRead,
    BackupEventoRead,
    BackupRespaldoRead,
    BackupServidorRead,
    BackupServidorUpdate,
    BackupSolicitudRead,
    BackupsResumen,
)
from app.services import backups_enlace
from app.services.backups_vigilancia import AGENTE_ACTIVO, diagnosticar

router = APIRouter(prefix="/api/backups", tags=["backups"], dependencies=[Depends(require_admin)])

ESTADOS_FINALES = ("VALIDADO", "SIN_HASH")
ESTADOS_ERROR = ("ERROR_INTEGRIDAD", "ERROR_TRANSFERENCIA", "ERROR_ACCESO", "SIN_ESPACIO")


def _agente(datos: dict) -> BackupAgenteRead:
    datos = dict(datos)
    config = datos.get("configuracion")
    if isinstance(config, str):
        try:
            datos["configuracion"] = json.loads(config)
        except ValueError:
            datos["configuracion"] = None
    dto = BackupAgenteRead.model_validate(datos)
    dto.activo = bool(dto.ultimo_latido and datetime.utcnow() - dto.ultimo_latido <= AGENTE_ACTIVO)
    return dto


def _agentes(filas: list[BackupAgente]) -> list[BackupAgenteRead]:
    """Lo guardado en la base + el latido mas reciente que hay en memoria
    (el agente late cada minuto contra el backend sin tocar la base)."""
    por_host = {a.host: a.model_dump() for a in filas}
    for host, vivo in backups_enlace.latidos().items():
        base = por_host.get(host, {"host": host, "estado": "INACTIVO", "origen_accesible": False})
        previo = base.get("ultimo_latido")
        if previo is None or vivo["ultimo_latido"] >= previo:
            base = {**base, **{k: v for k, v in vivo.items() if v is not None}}
        por_host[host] = base
    campos = BackupAgenteRead.model_fields
    return sorted(
        (_agente({k: d.get(k) for k in campos if k != "activo"}) for d in por_host.values()),
        key=lambda a: a.ultimo_latido or datetime.min, reverse=True,
    )


@router.post("/agente/latido", response_model=AgenteLatidoRespuesta)
async def latido_agente(body: AgenteLatido, session: AsyncSession = Depends(get_session)) -> AgenteLatidoRespuesta:
    """Lo llama el agente cada minuto. Responde desde memoria: solo consulta
    la base la primera vez tras un reinicio del backend (ver backups_enlace)."""
    if body.ack_version is not None:
        backups_enlace.confirmar_sin_solicitudes(body.ack_version)
    backups_enlace.registrar_latido(body.host, body.model_dump(exclude={"host", "ack_version"}))
    if backups_enlace.pendiente() is None:
        hay = (await session.exec(
            select(BackupSolicitud.id_solicitud).where(BackupSolicitud.estado == "PENDIENTE").limit(1)
        )).first()
        backups_enlace.fijar_pendiente(hay is not None)
    return AgenteLatidoRespuesta(solicitud_pendiente=bool(backups_enlace.pendiente()), version=backups_enlace.version())


@router.get("/resumen", response_model=BackupsResumen)
async def resumen(session: AsyncSession = Depends(get_session)) -> BackupsResumen:
    agentes = (await session.exec(select(BackupAgente).order_by(BackupAgente.ultimo_latido.desc()))).all()
    ultima = (await session.exec(select(BackupEjecucion).order_by(BackupEjecucion.inicio.desc()).limit(1))).first()
    ultima_ok = (await session.exec(
        select(func.max(BackupEjecucion.fin)).where(BackupEjecucion.estado == "OK")
    )).first()
    servidores = (await session.exec(select(func.count()).select_from(BackupServidor))).one()
    resp = dict((await session.exec(select(BackupRespaldo.estado, func.count()).group_by(BackupRespaldo.estado))).all())
    arch = dict((await session.exec(select(BackupArchivo.estado, func.count()).group_by(BackupArchivo.estado))).all())
    copiados = (await session.exec(
        select(func.coalesce(func.sum(BackupArchivo.tamano_local), 0)).where(BackupArchivo.estado.in_(ESTADOS_FINALES))
    )).one()
    pendientes = (await session.exec(
        select(BackupSolicitud).where(BackupSolicitud.estado.in_(("PENDIENTE", "TOMADA"))).order_by(BackupSolicitud.solicitado_en)
    )).all()
    alertas = [m for _, m in await diagnosticar(session)]
    if ultima and ultima.estado == "ERROR":
        alertas.append(f"La ultima ejecucion (#{ultima.id_ejecucion}) termino con errores")
    return BackupsResumen(
        agentes=_agentes(agentes),
        ultima_ejecucion=ultima,
        ultima_ejecucion_exitosa=ultima_ok,
        servidores=servidores,
        respaldos_por_estado=resp,
        archivos_por_estado=arch,
        bytes_copiados=int(copiados or 0),
        alertas=alertas,
        solicitudes_pendientes=pendientes,
    )


@router.get("/servidores", response_model=list[BackupServidorRead])
async def list_servidores(session: AsyncSession = Depends(get_session)):
    servidores = (await session.exec(select(BackupServidor).order_by(BackupServidor.nombre))).all()
    filas = (await session.exec(
        select(BackupRespaldo.id_servidor_backup, BackupRespaldo.estado, func.count(), func.coalesce(func.sum(BackupRespaldo.tamano_total), 0))
        .group_by(BackupRespaldo.id_servidor_backup, BackupRespaldo.estado)
    )).all()
    copiados = dict((await session.exec(
        select(BackupRespaldo.id_servidor_backup, func.coalesce(func.sum(BackupArchivo.tamano_local), 0))
        .join(BackupRespaldo, BackupRespaldo.id_respaldo == BackupArchivo.id_respaldo)
        .where(BackupArchivo.estado.in_(ESTADOS_FINALES))
        .group_by(BackupRespaldo.id_servidor_backup)
    )).all())
    stats: dict[int, dict] = {}
    for id_srv, estado, n, tam in filas:
        st = stats.setdefault(id_srv, {"respaldos": 0, "validados": 0, "sin_hash": 0, "pendientes": 0, "errores": 0, "no_rec": 0, "bytes": 0})
        st["respaldos"] += n
        st["bytes"] += int(tam or 0)
        if estado == "VALIDADO":
            st["validados"] += n
        elif estado == "SIN_HASH":
            st["sin_hash"] += n
        elif estado in ESTADOS_ERROR:
            st["errores"] += n
        elif estado == "NO_RECONOCIDO":
            st["no_rec"] += n
        else:
            st["pendientes"] += n
    out = []
    for s in servidores:
        st = stats.get(s.id_servidor_backup, {})
        dto = BackupServidorRead.model_validate(s, from_attributes=True)
        dto.respaldos = st.get("respaldos", 0)
        dto.respaldos_validados = st.get("validados", 0)
        dto.respaldos_sin_hash = st.get("sin_hash", 0)
        dto.respaldos_pendientes = st.get("pendientes", 0)
        dto.respaldos_con_error = st.get("errores", 0)
        dto.respaldos_no_reconocidos = st.get("no_rec", 0)
        dto.bytes_en_origen = st.get("bytes", 0)
        dto.bytes_copiados = int(copiados.get(s.id_servidor_backup, 0) or 0)
        # Estado para el panel: OK | PENDIENTE | ERROR (+ VACIO / NO_ENCONTRADO / DESHABILITADO)
        if not s.habilitado:
            dto.estado = "DESHABILITADO"
        elif s.estado in ("VACIO", "NO_ENCONTRADO", "ERROR"):
            pass
        elif dto.respaldos_con_error:
            dto.estado = "ERROR"
        elif dto.respaldos_pendientes:
            dto.estado = "PENDIENTE"
        else:
            dto.estado = "OK"
        out.append(dto)
    return out


@router.patch("/servidores/{id_servidor_backup}", response_model=BackupServidorRead)
async def update_servidor(id_servidor_backup: int, body: BackupServidorUpdate, session: AsyncSession = Depends(get_session)):
    obj = await session.get(BackupServidor, id_servidor_backup)
    if not obj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    obj.habilitado = body.habilitado
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.get("/respaldos", response_model=list[BackupRespaldoRead])
async def list_respaldos(
    id_servidor_backup: int | None = Query(default=None),
    estado: str | None = Query(default=None, description="Estado o 'ERRORES' para todos los de error"),
    limit: int = Query(default=200, le=2000),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(BackupRespaldo).order_by(BackupRespaldo.fecha_backup.desc().nulls_last(), BackupRespaldo.ruta_relativa).limit(limit)
    if id_servidor_backup is not None:
        stmt = stmt.where(BackupRespaldo.id_servidor_backup == id_servidor_backup)
    if estado == "ERRORES":
        stmt = stmt.where(BackupRespaldo.estado.in_(ESTADOS_ERROR))
    elif estado:
        stmt = stmt.where(BackupRespaldo.estado == estado)
    return (await session.exec(stmt)).all()


@router.get("/respaldos/{id_respaldo}/archivos", response_model=list[BackupArchivoRead])
async def list_archivos_respaldo(id_respaldo: int, session: AsyncSession = Depends(get_session)):
    return (await session.exec(
        select(BackupArchivo).where(BackupArchivo.id_respaldo == id_respaldo).order_by(BackupArchivo.nombre)
    )).all()


@router.get("/archivos", response_model=list[BackupArchivoRead])
async def list_archivos(
    estado: str | None = Query(default=None, description="Estado o 'ERRORES'"),
    limit: int = Query(default=200, le=2000),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(BackupArchivo).order_by(BackupArchivo.actualizado_en.desc()).limit(limit)
    if estado == "ERRORES":
        stmt = stmt.where(BackupArchivo.estado.in_(ESTADOS_ERROR))
    elif estado:
        stmt = stmt.where(BackupArchivo.estado == estado)
    return (await session.exec(stmt)).all()


@router.get("/ejecuciones", response_model=list[BackupEjecucionRead])
async def list_ejecuciones(limit: int = Query(default=60, le=500), session: AsyncSession = Depends(get_session)):
    return (await session.exec(select(BackupEjecucion).order_by(BackupEjecucion.inicio.desc()).limit(limit))).all()


@router.get("/ejecuciones/{id_ejecucion}", response_model=BackupEjecucionDetalle)
async def get_ejecucion(id_ejecucion: int, session: AsyncSession = Depends(get_session)):
    e = await session.get(BackupEjecucion, id_ejecucion)
    if not e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    eventos = (await session.exec(
        select(BackupEvento).where(BackupEvento.id_ejecucion == id_ejecucion).order_by(BackupEvento.id_evento)
    )).all()
    dto = BackupEjecucionDetalle.model_validate({**BackupEjecucionRead.model_validate(e).model_dump(), "eventos": []})
    dto.eventos = [BackupEventoRead.model_validate(ev) for ev in eventos]
    return dto


@router.get("/ejecuciones/{id_ejecucion}/log", response_class=PlainTextResponse)
async def get_log(id_ejecucion: int, session: AsyncSession = Depends(get_session)):
    e = await session.get(BackupEjecucion, id_ejecucion)
    if not e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return PlainTextResponse(e.log or "(sin log)", headers={"Cache-Control": "no-store"})


def _csv(nombre: str, columnas: list[str], filas: list[list]) -> StreamingResponse:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(columnas)
    w.writerows(filas)
    return StreamingResponse(
        iter(["﻿" + out.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )


@router.get("/exportar/ejecuciones.csv")
async def exportar_ejecuciones(session: AsyncSession = Depends(get_session)):
    filas = (await session.exec(select(BackupEjecucion).order_by(BackupEjecucion.inicio.desc()).limit(5000))).all()
    cols = ["id_ejecucion", "tipo", "inicio_utc", "fin_utc", "estado", "servidores", "archivos_nuevos",
            "archivos_descargados", "archivos_validados", "archivos_ya_existentes", "archivos_pendientes",
            "archivos_fallidos", "bytes_descargados", "espacio_libre_destino", "resumen"]
    return _csv("respaldos_ejecuciones.csv", cols, [
        [e.id_ejecucion, e.tipo, e.inicio, e.fin, e.estado, e.servidores, e.archivos_nuevos, e.archivos_descargados,
         e.archivos_validados, e.archivos_ya_existentes, e.archivos_pendientes, e.archivos_fallidos,
         e.bytes_descargados, e.espacio_libre_destino, e.resumen] for e in filas
    ])


@router.get("/exportar/inventario.csv")
async def exportar_inventario(session: AsyncSession = Depends(get_session)):
    filas = (await session.exec(
        select(BackupServidor.nombre, BackupRespaldo, BackupArchivo)
        .join(BackupRespaldo, BackupRespaldo.id_servidor_backup == BackupServidor.id_servidor_backup)
        .join(BackupArchivo, BackupArchivo.id_respaldo == BackupRespaldo.id_respaldo)
        .order_by(BackupArchivo.ruta_relativa)
    )).all()
    cols = ["servidor", "plataforma", "backup_id", "fecha_backup", "ruta_relativa", "tamano_bytes", "estado",
            "verificacion", "hash_esperado_sha256", "hash_local_sha256", "en_drive", "validado_en_utc", "ultimo_error"]
    return _csv("respaldos_inventario.csv", cols, [
        [srv, r.plataforma, r.ruta_relativa, r.fecha_backup, a.ruta_relativa, a.tamano_origen, a.estado,
         "SHA256" if a.estado == "VALIDADO" else ("LIMITADA" if a.estado == "SIN_HASH" else ""),
         a.hash_esperado, a.hash_local, "Si" if a.en_origen else "No", a.validado_en, a.ultimo_error]
        for srv, r, a in filas
    ])


async def _solicitar(session: AsyncSession, tipo: str) -> BackupSolicitud:
    abierta = (await session.exec(
        select(BackupSolicitud).where(BackupSolicitud.tipo == tipo, BackupSolicitud.estado.in_(("PENDIENTE", "TOMADA")))
    )).first()
    if abierta:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya hay una solicitud igual en curso")
    sol = BackupSolicitud(tipo=tipo)
    session.add(sol)
    await session.commit()
    await session.refresh(sol)
    backups_enlace.nueva_solicitud()  # el agente la vera en su proximo latido
    return sol


@router.post("/ejecutar", response_model=BackupSolicitudRead)
async def ejecutar(session: AsyncSession = Depends(get_session)):
    return await _solicitar(session, "EJECUTAR")


@router.post("/reintentar-fallidos", response_model=BackupSolicitudRead)
async def reintentar(session: AsyncSession = Depends(get_session)):
    return await _solicitar(session, "REINTENTAR_FALLIDOS")


@router.post("/inventario", response_model=BackupSolicitudRead)
async def inventario(session: AsyncSession = Depends(get_session)):
    return await _solicitar(session, "INVENTARIO")


@router.get("/solicitudes", response_model=list[BackupSolicitudRead])
async def list_solicitudes(limit: int = Query(default=20, le=200), session: AsyncSession = Depends(get_session)):
    return (await session.exec(select(BackupSolicitud).order_by(BackupSolicitud.solicitado_en.desc()).limit(limit))).all()
