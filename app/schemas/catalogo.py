from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class TipoUnidadAcademicaCreate(BaseModel):
    nombre_tipo: str
    descripcion: Optional[str] = None


class TipoUnidadAcademicaRead(TipoUnidadAcademicaCreate):
    id_tipo_unidad: int


class TipoPlataformaCreate(BaseModel):
    nombre_tipo: str
    descripcion: Optional[str] = None


class TipoPlataformaRead(TipoPlataformaCreate):
    id_tipo_plataforma: int


class UnidadAcademicaCreate(BaseModel):
    id_tipo_unidad: int
    id_unidad_padre: Optional[int] = None
    nombre: str
    sigla: Optional[str] = None


class UnidadAcademicaUpdate(BaseModel):
    id_tipo_unidad: Optional[int] = None
    id_unidad_padre: Optional[int] = None
    nombre: Optional[str] = None
    sigla: Optional[str] = None


class UnidadAcademicaRead(BaseModel):
    id_unidad: int
    id_tipo_unidad: int
    id_unidad_padre: Optional[int]
    nombre: str
    sigla: Optional[str]
    deleted_at: Optional[datetime]


class ResponsableCreate(BaseModel):
    nombre_completo: str
    correo_institucional: str
    telefono: Optional[str] = None
    tipo_contrato: Optional[str] = None
    fecha_actualizacion: Optional[date] = None


class ResponsableUpdate(BaseModel):
    nombre_completo: Optional[str] = None
    correo_institucional: Optional[str] = None
    telefono: Optional[str] = None
    tipo_contrato: Optional[str] = None
    fecha_actualizacion: Optional[date] = None


class ResponsableRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_responsable: int
    nombre_completo: str
    correo_institucional: str
    telefono: Optional[str]
    tipo_contrato: Optional[str] = None
    fecha_actualizacion: Optional[date] = None
    deleted_at: Optional[datetime]


class ServidorCreate(BaseModel):
    hostname: str
    ip_publica: Optional[str] = None
    ip_privada: str
    vcpu: Optional[int] = None
    memoria_ram_gb: Optional[int] = None
    espacio_disco_gb: Optional[int] = None
    sistema_operativo: Optional[str] = None
    estado_servidor: Optional[str] = "Activo"
    tecnologia: Optional[str] = None


class ServidorUpdate(BaseModel):
    hostname: Optional[str] = None
    ip_publica: Optional[str] = None
    ip_privada: Optional[str] = None
    vcpu: Optional[int] = None
    memoria_ram_gb: Optional[int] = None
    espacio_disco_gb: Optional[int] = None
    sistema_operativo: Optional[str] = None
    estado_servidor: Optional[str] = None
    tecnologia: Optional[str] = None


class ServidorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_servidor: int
    hostname: str
    ip_publica: Optional[str]
    ip_privada: str
    vcpu: Optional[int]
    memoria_ram_gb: Optional[int]
    espacio_disco_gb: Optional[int]
    sistema_operativo: Optional[str]
    estado_servidor: Optional[str]
    tecnologia: Optional[str] = None
    deleted_at: Optional[datetime]
