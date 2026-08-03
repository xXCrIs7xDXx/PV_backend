from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TicketCreate(BaseModel):
    id_plataforma: int
    id_registro_falla: Optional[int] = None
    id_responsable_revision: int
    categoria_error: Optional[str] = None
    accion_realizada: Optional[str] = None


class TicketUpdate(BaseModel):
    id_responsable_revision: Optional[int] = None
    categoria_error: Optional[str] = None
    accion_realizada: Optional[str] = None
    estado_ticket: Optional[str] = None
    fecha_cierre: Optional[datetime] = None


class TicketRead(BaseModel):
    id_ticket: int
    id_plataforma: int
    id_registro_falla: Optional[int]
    id_responsable_revision: int
    fecha_apertura: datetime
    fecha_cierre: Optional[datetime]
    categoria_error: Optional[str]
    accion_realizada: Optional[str]
    estado_ticket: Optional[str]
