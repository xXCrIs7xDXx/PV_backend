from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class TipoUnidadAcademica(SQLModel, table=True):
    __tablename__ = "tipo_unidad_academica"

    id_tipo_unidad: Optional[int] = Field(default=None, primary_key=True)
    nombre_tipo: str = Field(max_length=50, nullable=False)
    descripcion: Optional[str] = Field(default=None, max_length=150)


class UnidadAcademica(SQLModel, table=True):
    __tablename__ = "unidad_academica"

    id_unidad: Optional[int] = Field(default=None, primary_key=True)
    id_tipo_unidad: int = Field(foreign_key="tipo_unidad_academica.id_tipo_unidad", nullable=False)
    id_unidad_padre: Optional[int] = Field(default=None, foreign_key="unidad_academica.id_unidad")
    nombre: str = Field(max_length=150, nullable=False)
    sigla: Optional[str] = Field(default=None, max_length=20)
    deleted_at: Optional[datetime] = Field(default=None)


class TipoPlataforma(SQLModel, table=True):
    __tablename__ = "tipo_plataforma"

    id_tipo_plataforma: Optional[int] = Field(default=None, primary_key=True)
    nombre_tipo: str = Field(max_length=50, nullable=False)
    descripcion: Optional[str] = Field(default=None, max_length=150)


class Responsable(SQLModel, table=True):
    __tablename__ = "responsable"

    id_responsable: Optional[int] = Field(default=None, primary_key=True)
    nombre_completo: str = Field(max_length=150, nullable=False)
    correo_institucional: str = Field(max_length=100, nullable=False)
    telefono: Optional[str] = Field(default=None, max_length=20)
    deleted_at: Optional[datetime] = Field(default=None)


class Servidor(SQLModel, table=True):
    __tablename__ = "servidor"

    id_servidor: Optional[int] = Field(default=None, primary_key=True)
    hostname: str = Field(max_length=100, nullable=False)
    ip_publica: Optional[str] = Field(default=None, max_length=45)
    ip_privada: str = Field(max_length=45, nullable=False)
    vcpu: Optional[int] = Field(default=None)
    memoria_ram_gb: Optional[int] = Field(default=None)
    espacio_disco_gb: Optional[int] = Field(default=None)
    sistema_operativo: Optional[str] = Field(default=None, max_length=50)
    estado_servidor: Optional[str] = Field(default="Activo", max_length=50)
    deleted_at: Optional[datetime] = Field(default=None)
