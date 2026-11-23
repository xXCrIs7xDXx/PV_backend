from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

DESCRIPCION_MAX_LEN = 500


class InformeFila(BaseModel):
    """Una fila del informe tal como se mostrara en la ventana de edicion
    del frontend (y luego en el PDF)."""

    nro: int
    id_plataforma: int
    nombre_plataforma: str
    url_principal: str
    servidor: str
    servidor_ip: Optional[str] = None
    responsable: str
    responsable_telefono: Optional[str] = None
    estado: str
    descripcion: str
    # Solo las plataformas "En Revision" admiten una descripcion escrita por
    # el admin; el resto siempre usa la descripcion automatica.
    editable: bool


class InformeResumen(BaseModel):
    total: int
    operativos: int
    mantenimiento: int
    en_revision: int
    otros: int


class InformeIncidencia(BaseModel):
    """Una caida (racha de verificaciones fallidas seguidas) ocurrida en el
    dia del informe. Horas en hora de Bolivia."""

    id_plataforma: int
    nombre_plataforma: str
    servidor: str
    # None = la racha ya venia del dia anterior
    inicio: Optional[datetime] = None
    # Primera verificacion correcta despues de la falla (None = sigue caida)
    fin: Optional[datetime] = None
    duracion_min: Optional[int] = None
    causa: str
    resultado: str  # "Recuperada" | "Sigue caída" | "Falla aislada"
    verificaciones_fallidas: int
    id_ticket: Optional[int] = None


class InformeDatos(BaseModel):
    fecha_generacion: datetime  # hora local de Bolivia (UTC-4)
    # Dia del historial pedido (None = estado actual de las plataformas)
    fecha_informe: Optional[date] = None
    # Hora (Bolivia) de la verificacion en la que se basan los datos
    fecha_datos: Optional[datetime] = None
    nombre_archivo: str
    resumen: InformeResumen
    filas: list[InformeFila]
    # Caidas del dia del informe (hoy hasta ahora, o el dia del historial)
    incidencias: list[InformeIncidencia] = Field(default_factory=list)
    # Plataformas que siguieron caidas todo el dia (falla de dias anteriores)
    caidas_persistentes: list[str] = Field(default_factory=list)


class InformePdfRequest(BaseModel):
    # id_plataforma -> descripcion escrita por el admin. Las claves de
    # plataformas que no estan "En Revision" se ignoran en el servidor.
    descripciones: dict[int, str] = Field(default_factory=dict)
    # Dia del historial (hora de Bolivia). Sin fecha = estado actual.
    fecha: Optional[date] = None
    # id_plataforma -> estado a mostrar SOLO en este informe (no se guarda en
    # la base). Valores validos: ESTADOS_INFORME; los demas se ignoran.
    estados: dict[int, str] = Field(default_factory=dict)
