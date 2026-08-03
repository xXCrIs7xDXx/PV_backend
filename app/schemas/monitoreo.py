from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class RegistroMonitoreoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_registro: int
    id_plataforma: int
    fecha_hora: datetime
    estado_http: Optional[int]
    tiempo_respuesta_ms: Optional[int]
    esta_operativo: bool
    observacion: Optional[str]


class MonitorEstado(BaseModel):
    corriendo: bool
    inicio: Optional[datetime] = None
    ultimo_fin: Optional[datetime] = None
    ultimo_error: Optional[str] = None


class FormsEstado(BaseModel):
    corriendo: bool
    ultimo_fin: Optional[datetime] = None
    ultimo_resultado: Optional[str] = None
    ultimo_error: Optional[str] = None


class DashboardResumen(BaseModel):
    fecha: Optional[datetime]
    operativos: int
    en_revision: int
    total: int
    tiempo_promedio_ms: Optional[int]


class HistorialPunto(BaseModel):
    fecha_hora: datetime
    operativos: int
    en_revision: int


class DiaStats(BaseModel):
    operativos: int
    en_revision: int


class ComparacionPlataforma(BaseModel):
    id_plataforma: int
    nombre_plataforma: str
    nombre_unidad: str
    operativo_a: Optional[bool]
    operativo_b: Optional[bool]
    tiempo_a: Optional[int]
    tiempo_b: Optional[int]
    cambio: bool


class ComparacionDiasResponse(BaseModel):
    fecha_a: date
    fecha_b: date
    resumen_a: DiaStats
    resumen_b: DiaStats
    plataformas: list[ComparacionPlataforma]


class EjecucionResumen(BaseModel):
    id_ejecucion: int
    fecha_hora: datetime
    tipo: str
    operativos: int
    en_revision: int
    mantenimiento: int
    total: int


class EjecucionPlataformaDetalle(BaseModel):
    id_plataforma: int
    nombre_plataforma: str
    nombre_unidad: str
    estado_actual_snapshot: Optional[str]
    esta_operativo: bool
    estado_http: Optional[int]
    tiempo_respuesta_ms: Optional[int]
    observacion: Optional[str]


class EjecucionDetalle(BaseModel):
    id_ejecucion: int
    fecha_hora: datetime
    tipo: str
    plataformas: list[EjecucionPlataformaDetalle]
