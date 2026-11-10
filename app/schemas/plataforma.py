from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.catalogo import ResponsableRead, ServidorRead
from app.schemas.monitoreo import RegistroMonitoreoRead


def _limpiar_url(v: Optional[str]) -> Optional[str]:
    # Quita tabs/espacios/saltos de linea (pasa al copiar desde Excel)
    return "".join(v.split()) if isinstance(v, str) else v


class PlataformaCreate(BaseModel):
    _url = field_validator("url_principal", mode="before")(_limpiar_url)

    id_unidad: int
    id_servidor: int = 0
    ip_publica: Optional[str] = None
    id_responsable: int
    id_tipo_plataforma: int
    nombre_plataforma: str
    url_principal: str
    estado_actual: Optional[str] = "Operativo"
    notas_credenciales: Optional[str] = None
    notas_migracion: Optional[str] = None
    responsable_anterior: Optional[str] = None


class PlataformaUpdate(BaseModel):
    _url = field_validator("url_principal", mode="before")(_limpiar_url)

    id_unidad: Optional[int] = None
    id_servidor: Optional[int] = None
    id_responsable: Optional[int] = None
    id_tipo_plataforma: Optional[int] = None
    nombre_plataforma: Optional[str] = None
    url_principal: Optional[str] = None
    estado_actual: Optional[str] = None
    notas_credenciales: Optional[str] = None
    notas_migracion: Optional[str] = None
    responsable_anterior: Optional[str] = None


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
    notas_credenciales: Optional[str] = None
    notas_migracion: Optional[str] = None
    responsable_anterior: Optional[str] = None


class PlataformaReadExpanded(PlataformaRead):
    """Same as PlataformaRead plus human-readable joined names for display."""

    nombre_unidad: Optional[str] = None
    # Facultad (unidad raiz) y sub-unidad (carrera/postgrado/instituto),
    # para agrupar por facultad en el frontend
    id_facultad: Optional[int] = None
    nombre_facultad: Optional[str] = None
    nombre_subunidad: Optional[str] = None
    nombre_servidor: Optional[str] = None
    tecnologia_servidor: Optional[str] = None
    ip_publica: Optional[str] = None
    ip_privada: Optional[str] = None
    nombre_responsable: Optional[str] = None
    telefono_responsable: Optional[str] = None
    correo_responsable: Optional[str] = None
    tipo_contrato_responsable: Optional[str] = None
    fecha_actualizacion_responsable: Optional[date] = None
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
