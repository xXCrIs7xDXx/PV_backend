"""Informe PDF de monitoreo (hoja horizontal) a pedido del admin.

Flujo pensado para el frontend:
  1. GET  /api/informes/datos -> filas con descripcion automatica; solo las
     plataformas "En Revision" vienen con editable=True.
  2. El admin edita esas descripciones y pide la previsualizacion.
  3. POST /api/informes/pdf  -> devuelve el PDF (inline) que el frontend
     muestra en un iframe y, si el admin confirma, descarga tal cual.

No se guarda nada en la base de datos: las descripciones escritas viven
solo en esa peticion. La construccion del PDF es sincrona (reportlab), asi
que el router la ejecuta en un threadpool para no bloquear el event loop
mientras corre un ciclo de monitoreo.
"""
from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta, timezone
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas as rl_canvas
from fastapi import HTTPException, status
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Plataforma, RegistroMonitoreo, Responsable, Servidor, TicketIncidencia
from app.schemas.informe import (
    DESCRIPCION_MAX_LEN,
    InformeDatos,
    InformeFila,
    InformeIncidencia,
    InformeResumen,
)

BOLIVIA_TZ = timezone(timedelta(hours=-4))

ESTADO_OPERATIVO = "Operativo"
ESTADO_MANTENIMIENTO = "En Mantenimiento"
ESTADO_REVISION = "En Revision"

# Orden de las filas en el informe: primero lo que requiere atencion
# (En Revision, Error, Fuera de Servicio), luego mantenimiento y al final las
# operativas - igual que la ventana de edicion del frontend. Dentro de cada
# grupo, orden alfabetico.
_ORDEN_ESTADO = {
    ESTADO_REVISION: 0,
    "Error": 1,
    "Fuera de Servicio": 2,
    ESTADO_MANTENIMIENTO: 3,
    ESTADO_OPERATIVO: 4,
}

_ETIQUETA_ESTADO = {ESTADO_REVISION: "En Revisión"}

# Estados que el admin puede asignar a mano en un informe (mismos que la app)
ESTADOS_INFORME = {ESTADO_OPERATIVO, ESTADO_REVISION, ESTADO_MANTENIMIENTO, "Error", "Fuera de Servicio"}

# Umbral a partir del cual una respuesta correcta se reporta como lenta.
RESPUESTA_LENTA_MS = 2000

_HTTP_DESCRIPCION = {
    400: "Solicitud incorrecta",
    401: "Acceso no autorizado",
    403: "Acceso prohibido",
    404: "Página no encontrada",
    500: "Error interno del servidor",
    502: "Puerta de enlace incorrecta",
    503: "Servicio no disponible",
    504: "Tiempo de espera de la puerta de enlace agotado",
}


# --------------------------------------------------------------------------
# Datos
# --------------------------------------------------------------------------

def ahora_bolivia() -> datetime:
    return datetime.now(BOLIVIA_TZ).replace(tzinfo=None)


def nombre_archivo(fecha: datetime) -> str:
    # Windows no admite ":" en nombres de archivo, por eso la hora va con punto.
    return f"Monitorizacion {fecha:%d-%m-%Y %H.%M}.pdf"


def _segundos(ms: int) -> str:
    s = ms / 1000
    texto = f"{s:.1f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def _problema(reg: RegistroMonitoreo) -> str:
    """Describe en lenguaje claro por que fallo una verificacion."""
    if reg.estado_http and reg.estado_http >= 400:
        etiqueta = _HTTP_DESCRIPCION.get(reg.estado_http, "Error del servidor web")
        return f"{etiqueta} (HTTP {reg.estado_http})"
    obs = (reg.observacion or "").strip()
    texto = obs.lower()
    if "tiempo de espera" in texto:
        if reg.tiempo_respuesta_ms:
            return f"No responde: tiempo de espera agotado ({_segundos(reg.tiempo_respuesta_ms)} s)"
        return "No responde: tiempo de espera agotado"
    if "sin conexion" in texto or "dominio no responde" in texto:
        return "Sin conexión: el dominio no responde"
    return obs or "Falla no especificada"


def descripcion_automatica(estado: str, reg: RegistroMonitoreo | None) -> str:
    if reg is None:
        return "Recién agregada al monitor; aún no tiene verificaciones."

    falla = not reg.esta_operativo

    if estado == ESTADO_OPERATIVO:
        if falla:
            return f"{_problema(reg)} en la última verificación."
        if reg.tiempo_respuesta_ms and reg.tiempo_respuesta_ms >= RESPUESTA_LENTA_MS:
            return f"Respuesta lenta ({_segundos(reg.tiempo_respuesta_ms)} s)."
        return "Sin problemas."

    if estado == ESTADO_REVISION:
        if falla:
            return f"{_problema(reg)}."
        return "Respondió correctamente en la última verificación."

    if estado == ESTADO_MANTENIMIENTO:
        base = "Puesta en mantenimiento por un administrador"
    else:
        base = f"Marcada como «{estado}» por un administrador"
    if falla:
        return f"{base}; última verificación: {_problema(reg)}."
    return f"{base}; responde correctamente."


def _servidor_display(srv: Servidor | None) -> tuple[str, str | None]:
    if srv is None:
        return "—", None
    ip = srv.ip_publica or srv.ip_privada
    if ip == "N/A":
        ip = None
    nombre = "Externo" if srv.hostname.lower() == "externo" else srv.hostname
    return nombre, ip


def _utc_a_bolivia(fecha_utc: datetime) -> datetime:
    """fecha_hora se guarda como UTC sin zona (ver app/models/monitoreo.py)."""
    return fecha_utc - timedelta(hours=4)


def _estado_en_registro(plat: Plataforma, reg: RegistroMonitoreo | None, historico: bool) -> str:
    if not historico or reg is None:
        return plat.estado_actual or ESTADO_OPERATIVO
    # Estado que tenia la plataforma en esa verificacion. Los registros
    # anteriores a la columna snapshot no lo tienen: se deduce del resultado.
    if reg.estado_actual_snapshot:
        return reg.estado_actual_snapshot
    return ESTADO_OPERATIVO if reg.esta_operativo else ESTADO_REVISION


RESULTADO_RECUPERADA = "Recuperada"
RESULTADO_SIGUE_CAIDA = "Sigue caída"
RESULTADO_AISLADA = "Falla aislada"

# Estados que maneja el monitor solo. Las fallas de plataformas puestas a
# mano en Mantenimiento / Error / Fuera de Servicio no son "caidas del dia":
# el admin ya sabe de ellas.
_ESTADOS_AUTOMATICOS = {None, ESTADO_OPERATIVO, ESTADO_REVISION}


async def obtener_incidencias(
    session: AsyncSession, dia: date
) -> tuple[list[InformeIncidencia], list[str]]:
    """Caidas ocurridas en `dia` (hora de Bolivia): cada racha de
    verificaciones fallidas seguidas de una plataforma es una incidencia.
    Devuelve (incidencias, persistentes): las rachas que ya venian del dia
    anterior y no se recuperaron no se listan como incidencia, solo por
    nombre, porque no "pasaron" ese dia."""
    inicio_utc = datetime.combine(dia, time.min) + timedelta(hours=4)
    fin_utc = inicio_utc + timedelta(days=1)
    en_el_dia = (RegistroMonitoreo.fecha_hora >= inicio_utc, RegistroMonitoreo.fecha_hora < fin_utc)

    con_fallas = (
        select(RegistroMonitoreo.id_plataforma)
        .where(*en_el_dia, RegistroMonitoreo.esta_operativo.is_(False))
        .distinct()
    )
    rows = (
        await session.exec(
            select(RegistroMonitoreo, Plataforma, Servidor)
            .join(Plataforma, Plataforma.id_plataforma == RegistroMonitoreo.id_plataforma)
            .outerjoin(Servidor, Plataforma.id_servidor == Servidor.id_servidor)
            .where(*en_el_dia, RegistroMonitoreo.id_plataforma.in_(con_fallas))
            .order_by(RegistroMonitoreo.id_plataforma, RegistroMonitoreo.id_registro)
        )
    ).all()
    if not rows:
        return [], []

    ids = {r[0].id_plataforma for r in rows}
    # Ultimo resultado ANTES del dia: dice si la primera falla del dia
    # empezo ese dia o venia de antes.
    previo_sub = (
        select(RegistroMonitoreo.id_plataforma, func.max(RegistroMonitoreo.id_registro).label("max_id"))
        .where(RegistroMonitoreo.id_plataforma.in_(ids), RegistroMonitoreo.fecha_hora < inicio_utc)
        .group_by(RegistroMonitoreo.id_plataforma)
        .subquery()
    )
    previo_ok = {
        r.id_plataforma: r.esta_operativo
        for r in (
            await session.exec(
                select(RegistroMonitoreo).join(previo_sub, RegistroMonitoreo.id_registro == previo_sub.c.max_id)
            )
        ).all()
    }

    ids_falla = [r[0].id_registro for r in rows if not r[0].esta_operativo]
    ticket_de = {
        t.id_registro_falla: t.id_ticket
        for t in (
            await session.exec(select(TicketIncidencia).where(TicketIncidencia.id_registro_falla.in_(ids_falla)))
        ).all()
    }

    incidencias: list[InformeIncidencia] = []
    persistentes: list[str] = []

    def cerrar(plat: Plataforma, srv: Servidor | None, fallas: list[RegistroMonitoreo],
               recuperacion: RegistroMonitoreo | None, venia: bool) -> None:
        primera = fallas[0]
        if primera.estado_actual_snapshot not in _ESTADOS_AUTOMATICOS:
            return
        if venia and recuperacion is None:
            persistentes.append(plat.nombre_plataforma)
            return
        if recuperacion is None:
            resultado = RESULTADO_SIGUE_CAIDA
        elif len(fallas) == 1 and not venia:
            resultado = RESULTADO_AISLADA
        else:
            resultado = RESULTADO_RECUPERADA
        inicio = None if venia else _utc_a_bolivia(primera.fecha_hora)
        fin = _utc_a_bolivia(recuperacion.fecha_hora) if recuperacion else None
        duracion = None
        if inicio is not None and resultado != RESULTADO_AISLADA:
            hasta = fin or _utc_a_bolivia(fallas[-1].fecha_hora)
            duracion = round((hasta - inicio).total_seconds() / 60)
        incidencias.append(
            InformeIncidencia(
                id_plataforma=plat.id_plataforma,
                nombre_plataforma=plat.nombre_plataforma,
                servidor=_servidor_display(srv)[0],
                inicio=inicio,
                fin=fin,
                duracion_min=duracion,
                causa=_problema(primera),
                resultado=resultado,
                verificaciones_fallidas=len(fallas),
                id_ticket=next((ticket_de[f.id_registro] for f in fallas if f.id_registro in ticket_de), None),
            )
        )

    actual: int | None = None
    plat = srv = None
    fallas: list[RegistroMonitoreo] = []
    venia = False
    for reg, p, s in rows + [(None, None, None)]:  # centinela para cerrar la ultima racha
        if reg is None or reg.id_plataforma != actual:
            if fallas:
                cerrar(plat, srv, fallas, None, venia)
            if reg is None:
                break
            actual, plat, srv, fallas = reg.id_plataforma, p, s, []
            venia = previo_ok.get(reg.id_plataforma) is False
        if not reg.esta_operativo:
            fallas.append(reg)
        else:
            if fallas:
                cerrar(plat, srv, fallas, reg, venia)
                fallas = []
            # Tras una verificacion correcta, la siguiente falla ya es del dia
            venia = False

    incidencias.sort(key=lambda i: (i.inicio is not None, i.inicio or datetime.min, i.nombre_plataforma))
    persistentes.sort(key=str.casefold)
    return incidencias, persistentes


async def obtener_datos(
    session: AsyncSession, fecha: date | None = None, estados: dict[int, str] | None = None
) -> InformeDatos:
    """Sin `fecha`: estado actual de cada plataforma + su ultima verificacion.
    Con `fecha` (dia de Bolivia): la ultima verificacion de cada plataforma
    registrada ese dia, con el estado que tenia en ese momento."""
    historico = fecha is not None
    # Estados cambiados por el admin solo para este informe (no se guardan)
    cambios = {k: v for k, v in (estados or {}).items() if v in ESTADOS_INFORME}

    def estado_de(plat: Plataforma, reg: RegistroMonitoreo | None) -> str:
        return cambios.get(plat.id_plataforma) or _estado_en_registro(plat, reg, historico)

    latest_sub = select(RegistroMonitoreo.id_plataforma, func.max(RegistroMonitoreo.id_registro).label("max_id"))
    if historico:
        inicio_utc = datetime.combine(fecha, time.min) + timedelta(hours=4)
        fin_utc = inicio_utc + timedelta(days=1)
        latest_sub = latest_sub.where(
            RegistroMonitoreo.fecha_hora >= inicio_utc, RegistroMonitoreo.fecha_hora < fin_utc
        )
    latest_sub = latest_sub.group_by(RegistroMonitoreo.id_plataforma).subquery()

    stmt = (
        select(Plataforma, Servidor, Responsable, RegistroMonitoreo)
        .outerjoin(Servidor, Plataforma.id_servidor == Servidor.id_servidor)
        .outerjoin(Responsable, Plataforma.id_responsable == Responsable.id_responsable)
    )
    if historico:
        # Solo las plataformas verificadas ese dia (incluye las que se
        # eliminaron despues: existian en esa fecha).
        stmt = (
            stmt.join(latest_sub, latest_sub.c.id_plataforma == Plataforma.id_plataforma)
            .join(RegistroMonitoreo, RegistroMonitoreo.id_registro == latest_sub.c.max_id)
            .where(or_(Plataforma.deleted_at.is_(None), Plataforma.deleted_at >= inicio_utc))
        )
    else:
        stmt = (
            stmt.outerjoin(latest_sub, latest_sub.c.id_plataforma == Plataforma.id_plataforma)
            .outerjoin(RegistroMonitoreo, RegistroMonitoreo.id_registro == latest_sub.c.max_id)
            .where(Plataforma.deleted_at.is_(None))
        )
    rows = (await session.exec(stmt)).all()
    if historico and not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No hay verificaciones registradas el {fecha:%d/%m/%Y}."
        )
    rows = sorted(
        rows,
        key=lambda r: (
            _ORDEN_ESTADO.get(estado_de(r[0], r[3]), 2),
            r[0].nombre_plataforma.casefold(),
        ),
    )

    ultima = max((r[3].fecha_hora for r in rows if r[3] is not None and r[3].fecha_hora), default=None)
    fecha_datos = _utc_a_bolivia(ultima) if ultima else None

    filas: list[InformeFila] = []
    operativos = mantenimiento = en_revision = otros = 0
    for i, (plat, srv, resp, reg) in enumerate(rows, start=1):
        estado = estado_de(plat, reg)
        if estado == ESTADO_OPERATIVO:
            operativos += 1
        elif estado == ESTADO_MANTENIMIENTO:
            mantenimiento += 1
        elif estado == ESTADO_REVISION:
            en_revision += 1
        else:
            otros += 1

        servidor, servidor_ip = _servidor_display(srv)
        filas.append(
            InformeFila(
                nro=i,
                id_plataforma=plat.id_plataforma,
                nombre_plataforma=plat.nombre_plataforma,
                url_principal=plat.url_principal,
                servidor=servidor,
                servidor_ip=servidor_ip,
                responsable=resp.nombre_completo if resp else "Sin responsable",
                responsable_telefono=resp.telefono if resp else None,
                estado=estado,
                descripcion=descripcion_automatica(estado, reg),
                editable=estado == ESTADO_REVISION,
            )
        )

    ahora = ahora_bolivia()
    incidencias, persistentes = await obtener_incidencias(session, fecha or ahora.date())
    return InformeDatos(
        fecha_generacion=ahora,
        fecha_informe=fecha,
        fecha_datos=fecha_datos,
        # Historico: el nombre lleva la fecha/hora de la verificacion usada
        nombre_archivo=nombre_archivo(fecha_datos if historico and fecha_datos else ahora),
        resumen=InformeResumen(
            total=len(filas),
            operativos=operativos,
            mantenimiento=mantenimiento,
            en_revision=en_revision,
            otros=otros,
        ),
        filas=filas,
        incidencias=incidencias,
        caidas_persistentes=persistentes,
    )


def aplicar_descripciones(datos: InformeDatos, descripciones: dict[int, str]) -> None:
    """Reemplaza la descripcion solo en las filas editables (En Revision).
    Cualquier otra clave se ignora: la regla se valida aqui, no solo en el
    frontend. Un texto vacio conserva la descripcion automatica."""
    for fila in datos.filas:
        if not fila.editable:
            continue
        texto = (descripciones.get(fila.id_plataforma) or "").strip()
        if texto:
            fila.descripcion = texto[:DESCRIPCION_MAX_LEN]


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

PAGE_SIZE = landscape(letter)  # 792 x 612 pt
MARGEN_X = 36
MARGEN_SUP = 84
MARGEN_INF = 46
BANDA_ALTO = 58

AZUL = colors.HexColor("#1B2A4A")
AZUL_ACENTO = colors.HexColor("#2E75B6")
GRIS_TEXTO = colors.HexColor("#5B6475")
GRIS_LINEA = colors.HexColor("#D5DBE5")
FONDO_ZEBRA = colors.HexColor("#F5F7FA")
FONDO_REVISION = colors.HexColor("#FDEDED")

COLOR_ESTADO = {
    ESTADO_OPERATIVO: (colors.HexColor("#1E7B34"), colors.HexColor("#E6F4EA")),
    ESTADO_MANTENIMIENTO: (colors.HexColor("#A15C00"), colors.HexColor("#FFF3E0")),
    ESTADO_REVISION: (colors.HexColor("#C62828"), colors.HexColor("#FDE2E2")),
}
COLOR_OTRO = (colors.HexColor("#6B21A8"), colors.HexColor("#F3E8FF"))

_st_titulo = ParagraphStyle("titulo", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=AZUL)
_st_sub = ParagraphStyle("sub", fontName="Helvetica", fontSize=9, leading=12, textColor=GRIS_TEXTO)
_st_seccion = ParagraphStyle("seccion", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=AZUL)
_st_head = ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white)
_st_head_c = ParagraphStyle("head_c", parent=_st_head, alignment=TA_CENTER)
_st_cel = ParagraphStyle("cel", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=colors.black)
_st_cel_c = ParagraphStyle("cel_c", parent=_st_cel, alignment=TA_CENTER)
_st_cel_b = ParagraphStyle("cel_b", parent=_st_cel, fontName="Helvetica-Bold")
_st_url = ParagraphStyle("url", parent=_st_cel, wordWrap="CJK")
_st_nota = ParagraphStyle("nota", fontName="Helvetica", fontSize=7.5, leading=10, textColor=GRIS_TEXTO)
_st_card_num = ParagraphStyle("card_num", fontName="Helvetica-Bold", fontSize=20, leading=22, alignment=TA_LEFT)
_st_card_lbl = ParagraphStyle("card_lbl", fontName="Helvetica", fontSize=8, leading=10, textColor=GRIS_TEXTO)

# Nro, Nombre, URL, Servidor, Responsable, Estado, Descripcion -> 720 pt
_ANCHOS = [24, 136, 136, 92, 104, 76, 152]


def _e(texto: str | None) -> str:
    return escape(texto or "", {'"': "&quot;"})


def _fecha_larga(fecha: datetime) -> str:
    sufijo = "a. m." if fecha.hour < 12 else "p. m."
    return f"{fecha:%d/%m/%Y}, {fecha:%I:%M} {sufijo}"


def _make_canvas(fecha: datetime):
    fecha_txt = _fecha_larga(fecha)

    class _Canvas(rl_canvas.Canvas):
        """Dibuja encabezado y pie en cada pagina; guarda las paginas para
        poder escribir "Pagina X de Y" cuando ya se conoce el total."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._paginas: list[dict] = []

        def showPage(self):  # noqa: N802 - API de reportlab
            self._paginas.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._paginas)
            for estado in self._paginas:
                self.__dict__.update(estado)
                self._decorar(total)
                super().showPage()
            super().save()

        def _decorar(self, total: int) -> None:
            ancho, alto = PAGE_SIZE
            self.saveState()
            # Banda superior
            self.setFillColor(AZUL)
            self.rect(0, alto - BANDA_ALTO, ancho, BANDA_ALTO, stroke=0, fill=1)
            self.setFillColor(AZUL_ACENTO)
            self.rect(0, alto - BANDA_ALTO - 3, ancho, 3, stroke=0, fill=1)

            self.setFillColor(colors.white)
            self.setFont("Helvetica-Bold", 14)
            self.drawString(MARGEN_X, alto - 26, "Universidad Mayor de San Andrés")
            self.setFont("Helvetica", 8.5)
            self.drawString(MARGEN_X, alto - 41, "DEPARTAMENTO DE TECNOLOGÍAS DE INFORMACIÓN Y COMUNICACIÓN")

            self.setFont("Helvetica-Bold", 10)
            self.drawRightString(ancho - MARGEN_X, alto - 26, "Monitoreo de Plataformas Virtuales")
            self.setFont("Helvetica", 8.5)
            self.drawRightString(ancho - MARGEN_X, alto - 41, fecha_txt)

            # Pie
            self.setStrokeColor(GRIS_LINEA)
            self.setLineWidth(0.6)
            self.line(MARGEN_X, 32, ancho - MARGEN_X, 32)
            self.setFillColor(GRIS_TEXTO)
            self.setFont("Helvetica", 7.5)
            self.drawString(
                MARGEN_X, 20, "DTIC · Plataformas de Educación Virtual – Informática Educativa"
            )
            self.drawRightString(ancho - MARGEN_X, 20, f"Página {self._pageNumber} de {total}")
            self.restoreState()

    return _Canvas


def _tarjetas(resumen: InformeResumen) -> Table:
    items = [
        ("Plataformas monitoreadas", resumen.total, AZUL, colors.HexColor("#EEF2F8")),
        ("En funcionamiento", resumen.operativos, *COLOR_ESTADO[ESTADO_OPERATIVO]),
        ("En mantenimiento", resumen.mantenimiento, *COLOR_ESTADO[ESTADO_MANTENIMIENTO]),
        ("En revisión", resumen.en_revision, *COLOR_ESTADO[ESTADO_REVISION]),
    ]
    if resumen.otros:
        items.append(("Otros estados", resumen.otros, *COLOR_OTRO))

    gap = 10
    ancho_total = PAGE_SIZE[0] - 2 * MARGEN_X
    ancho_card = (ancho_total - gap * (len(items) - 1)) / len(items)

    celdas, anchos, estilos = [], [], []
    for idx, (etiqueta, valor, color, fondo) in enumerate(items):
        col = idx * 2
        num = ParagraphStyle(f"n{idx}", parent=_st_card_num, textColor=color)
        celdas.append([Paragraph(str(valor), num), Paragraph(_e(etiqueta), _st_card_lbl)])
        anchos.append(ancho_card)
        estilos += [
            ("BACKGROUND", (col, 0), (col, 0), fondo),
            ("LINEBEFORE", (col, 0), (col, 0), 3.5, color),
        ]
        if idx < len(items) - 1:
            celdas.append("")
            anchos.append(gap)

    tabla = Table([celdas], colWidths=anchos)
    tabla.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                *estilos,
            ]
        )
    )
    return tabla


def _tabla_plataformas(filas: list[InformeFila]) -> Table:
    encabezados = ["Nro", "Nombre", "URL", "Servidor", "Responsable", "Estado", "Descripción del problema"]
    data = [
        [Paragraph(h, _st_head_c if i in (0, 5) else _st_head) for i, h in enumerate(encabezados)]
    ]
    estilos = [
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, GRIS_LINEA),
        ("BOX", (0, 0), (-1, -1), 0.6, GRIS_LINEA),
    ]

    for i, f in enumerate(filas, start=1):
        color, fondo = COLOR_ESTADO.get(f.estado, COLOR_OTRO)
        etiqueta = _ETIQUETA_ESTADO.get(f.estado, f.estado)
        st_estado = ParagraphStyle(
            f"estado{i}", parent=_st_cel_c, fontName="Helvetica-Bold", textColor=color
        )

        servidor = _e(f.servidor)
        if f.servidor_ip:
            servidor += f'<br/><font color="#5B6475" size="6.5">{_e(f.servidor_ip)}</font>'
        responsable = _e(f.responsable)
        if f.responsable_telefono:
            responsable += f'<br/><font color="#5B6475" size="6.5">Cel. {_e(f.responsable_telefono)}</font>'
        url = f'<link href="{_e(f.url_principal)}"><font color="#2E75B6">{_e(f.url_principal)}</font></link>'

        data.append(
            [
                Paragraph(str(f.nro), _st_cel_c),
                Paragraph(_e(f.nombre_plataforma), _st_cel_b),
                Paragraph(url, _st_url),
                Paragraph(servidor, _st_cel),
                Paragraph(responsable, _st_cel),
                Paragraph(_e(etiqueta), st_estado),
                Paragraph(_e(f.descripcion).replace("\n", "<br/>"), _st_cel),
            ]
        )
        if f.estado == ESTADO_REVISION:
            estilos.append(("BACKGROUND", (0, i), (-1, i), FONDO_REVISION))
        elif i % 2 == 0:
            estilos.append(("BACKGROUND", (0, i), (-1, i), FONDO_ZEBRA))
        estilos.append(("BACKGROUND", (5, i), (5, i), fondo))

    tabla = Table(data, colWidths=_ANCHOS, repeatRows=1)
    tabla.setStyle(TableStyle(estilos))
    return tabla


COLOR_RESULTADO = {
    RESULTADO_RECUPERADA: COLOR_ESTADO[ESTADO_OPERATIVO],
    RESULTADO_SIGUE_CAIDA: COLOR_ESTADO[ESTADO_REVISION],
    RESULTADO_AISLADA: COLOR_ESTADO[ESTADO_MANTENIMIENTO],
}

# Nro, Plataforma, Servidor, Detectada, Recuperada, Duracion, Causa, Resultado -> 720 pt
_ANCHOS_INC = [24, 160, 90, 58, 62, 64, 166, 96]


def _duracion(minutos: int | None) -> str:
    if minutos is None:
        return "—"
    h, m = divmod(minutos, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def _seccion_incidencias(datos: InformeDatos, fecha: datetime, historico: bool) -> list:
    incidencias = datos.incidencias
    if historico:
        titulo = f"Caídas e incidencias del día ({fecha:%d/%m/%Y})"
    else:
        titulo = f"Caídas e incidencias de hoy (hasta las {datos.fecha_generacion:%H:%M})"
    if incidencias:
        titulo += f" &nbsp;·&nbsp; {len(incidencias)} registrada{'s' if len(incidencias) != 1 else ''}"
    partes: list = [Paragraph(titulo, _st_seccion), Spacer(1, 6)]

    if not incidencias:
        aviso = Table(
            [[Paragraph("Sin caídas nuevas: ninguna plataforma dejó de responder durante el día.", _st_cel_b)]],
            colWidths=[PAGE_SIZE[0] - 2 * MARGEN_X],
        )
        verde, fondo_verde = COLOR_ESTADO[ESTADO_OPERATIVO]
        aviso.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), fondo_verde),
                    ("LINEBEFORE", (0, 0), (0, 0), 3.5, verde),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        partes.append(aviso)
    else:
        encabezados = ["Nro", "Plataforma", "Servidor", "Detectada", "Recuperada", "Duración aprox.", "Causa", "Resultado"]
        data = [[Paragraph(h, _st_head_c if i in (0, 3, 4, 5, 7) else _st_head) for i, h in enumerate(encabezados)]]
        estilos = [
            ("BACKGROUND", (0, 0), (-1, 0), AZUL),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, GRIS_LINEA),
            ("BOX", (0, 0), (-1, -1), 0.6, GRIS_LINEA),
        ]
        for i, inc in enumerate(incidencias, start=1):
            color, fondo = COLOR_RESULTADO.get(inc.resultado, COLOR_OTRO)
            st_res = ParagraphStyle(f"res{i}", parent=_st_cel_c, fontName="Helvetica-Bold", textColor=color)
            resultado = _e(inc.resultado)
            if inc.id_ticket:
                resultado += f'<br/><font name="Helvetica" color="#5B6475" size="6.5">Ticket #{inc.id_ticket}</font>'
            detectada = f"{inc.inicio:%H:%M}" if inc.inicio else "Día anterior"
            recuperada = f"{inc.fin:%H:%M}" if inc.fin else "—"
            if inc.resultado == RESULTADO_AISLADA:
                duracion = "Puntual"
            else:
                duracion = _duracion(inc.duracion_min)
                if inc.fin is None and inc.duracion_min is not None:
                    duracion = "Más de " + duracion
            causa = _e(inc.causa)
            if inc.verificaciones_fallidas > 1:
                causa += (
                    f'<br/><font color="#5B6475" size="6.5">'
                    f"{inc.verificaciones_fallidas} verificaciones fallidas seguidas</font>"
                )
            data.append(
                [
                    Paragraph(str(i), _st_cel_c),
                    Paragraph(_e(inc.nombre_plataforma), _st_cel_b),
                    Paragraph(_e(inc.servidor), _st_cel),
                    Paragraph(detectada, _st_cel_c),
                    Paragraph(recuperada, _st_cel_c),
                    Paragraph(duracion, _st_cel_c),
                    Paragraph(causa, _st_cel),
                    Paragraph(resultado, st_res),
                ]
            )
            if i % 2 == 0:
                estilos.append(("BACKGROUND", (0, i), (-1, i), FONDO_ZEBRA))
            estilos.append(("BACKGROUND", (7, i), (7, i), fondo))
        tabla = Table(data, colWidths=_ANCHOS_INC, repeatRows=1)
        tabla.setStyle(TableStyle(estilos))
        partes.append(tabla)

    notas = []
    if datos.caidas_persistentes:
        notas.append(
            "<b>Sin responder todo el día</b> (la falla viene de días anteriores): "
            + ", ".join(_e(n) for n in datos.caidas_persistentes)
            + "."
        )
    if incidencias:
        notas.append(
            "El monitor verifica cada hora: <b>Detectada</b> es la primera verificación fallida y "
            "<b>Recuperada</b> la primera que volvió a responder, por lo que la duración es aproximada."
            + (
                " <b>Falla aislada</b>: una sola verificación fallida que el monitor no llegó a confirmar como caída."
                if any(i.resultado == RESULTADO_AISLADA for i in incidencias)
                else ""
            )
        )
    if notas:
        partes += [Spacer(1, 5), Paragraph("<br/>".join(notas), _st_nota)]
    return partes


def construir_pdf(datos: InformeDatos) -> bytes:
    historico = datos.fecha_informe is not None and datos.fecha_datos is not None
    # En un informe del historial la fecha que manda es la de la verificacion
    fecha = datos.fecha_datos if historico else datos.fecha_generacion
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=PAGE_SIZE,
        leftMargin=MARGEN_X,
        rightMargin=MARGEN_X,
        topMargin=MARGEN_SUP,
        bottomMargin=MARGEN_INF,
        title=datos.nombre_archivo.removesuffix(".pdf"),
        author="DTIC – Universidad Mayor de San Andrés",
        subject="Monitoreo de Plataformas Virtuales",
    )

    story = [
        Paragraph("Informe de Monitoreo de Plataformas Virtuales", _st_titulo),
        Spacer(1, 2),
        Paragraph(
            f"Plataformas de Educación Virtual – Informática Educativa &nbsp;·&nbsp; "
            f"Fecha: <b>{_fecha_larga(fecha)}</b>"
            + (
                "<br/>Datos del historial: última verificación registrada ese día "
                f"&nbsp;·&nbsp; Generado el {_fecha_larga(datos.fecha_generacion)}"
                if historico
                else ""
            ),
            _st_sub,
        ),
        Spacer(1, 12),
        _tarjetas(datos.resumen),
        Spacer(1, 16),
        *_seccion_incidencias(datos, fecha, historico),
        Spacer(1, 16),
        Paragraph(f"{fecha:%d-%m-%Y} – Monitoreo Plataformas Virtuales", _st_seccion),
        Spacer(1, 6),
        _tabla_plataformas(datos.filas),
        Spacer(1, 10),
        Paragraph(
            "<b>Estados:</b> <b>Operativo</b>: la plataforma responde correctamente. "
            "<b>En Mantenimiento</b>: estado asignado por un administrador mientras se trabaja en ella. "
            "<b>En Revisión</b>: el monitor confirmó fallas consecutivas y la plataforma requiere atención. "
            "La descripción de las plataformas operativas y en mantenimiento se genera a partir de la "
            "última verificación automática.",
            _st_nota,
        ),
    ]

    doc.build(story, canvasmaker=_make_canvas(fecha))
    return buf.getvalue()
