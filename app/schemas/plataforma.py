from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.catalogo import ResponsableRead, ServidorRead
from app.schemas.monitoreo import RegistroMonitoreoRead


class PlataformaCreate(BaseModel):
    id_unidad: int
    id_servidor: int
    id_responsable: int
    id_tipo_plataforma: int
    nombre_plataforma: str
    url_principal: str
    estado_actual: Optional[str] = "Operativo"


class PlataformaUpdate(BaseModel):
    id_unidad: Optional[int] = None
    id_servidor: Optional[int] = None
    id_responsable: Optional[int] = None
    id_tipo_plataforma: Optional[int] = None
    nombre_plataforma: Optional[str] = None
    url_principal: Optional[str] = None
    estado_actual: Optional[str] = None


class PlataformaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_plataforma: int
    id_unidad: int
    id_servidor: int
    id_responsable: int
    id_tipo_plataforma: int
    nombre_plataforma: str
    url_principal: str
    estado_actual: Optional[str]
    deleted_at: Optional[datetime]


class PlataformaReadExpanded(PlataformaRead):
    """Same as PlataformaRead plus human-readable joined names for display."""

    nombre_unidad: Optional[str] = None
    nombre_servidor: Optional[str] = None
    nombre_responsable: Optional[str] = None
    nombre_tipo_plataforma: Optional[str] = None
    ultimo_estado_http: Optional[int] = None
    ultimo_tiempo_respuesta_ms: Optional[int] = None
    ultima_verificacion: Optional[datetime] = None


class PlataformaDetalle(BaseModel):
    """Full relational view for the dashboard's per-platform detail modal."""

    plataforma: PlataformaRead
    nombre_unidad: str
    servidor: ServidorRead
    responsable: ResponsableRead
    historial: list[RegistroMonitoreoRead]
