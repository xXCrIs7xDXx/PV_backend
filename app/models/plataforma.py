from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Plataforma(SQLModel, table=True):
    __tablename__ = "plataforma"

    id_plataforma: Optional[int] = Field(default=None, primary_key=True)
    id_unidad: int = Field(foreign_key="unidad_academica.id_unidad", nullable=False)
    id_servidor: int = Field(foreign_key="servidor.id_servidor", nullable=False)
    id_responsable: int = Field(foreign_key="responsable.id_responsable", nullable=False)
    id_tipo_plataforma: int = Field(foreign_key="tipo_plataforma.id_tipo_plataforma", nullable=False)
    nombre_plataforma: str = Field(max_length=150, nullable=False)
    url_principal: str = Field(max_length=255, nullable=False, unique=True)
    estado_actual: Optional[str] = Field(default="Operativo", max_length=50)
    deleted_at: Optional[datetime] = Field(default=None)
