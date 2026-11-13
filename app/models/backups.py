"""Tablas del modulo de respaldos (Google Drive -> disco D: de Windows).

Las escribe el agente local (app/backups/agente.py) que corre en el PC con
Windows; el backend en Render solo las lee para el panel y deja solicitudes
("Ejecutar ahora", "Reintentar fallidos") en backup_solicitud.

Todas las fechas se guardan como UTC naive, igual que el resto del sistema.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, Text
from sqlmodel import Field, SQLModel

# BIGINT en Postgres; INTEGER en SQLite (solo para pruebas locales: SQLite
# solo autoincrementa las PK declaradas como INTEGER).
_BigId = BigInteger().with_variant(Integer, "sqlite")


def _pk() -> Column:
    return Column(_BigId, primary_key=True, autoincrement=True)


def _fk(destino: str, ondelete: str = "CASCADE", nullable: bool = False) -> Column:
    return Column(_BigId, ForeignKey(destino, ondelete=ondelete), nullable=nullable, index=True)


class BackupServidor(SQLModel, table=True):
    """Carpeta de primer nivel dentro de Backupumsa2627 (una por servidor).
    Se descubre en cada ejecucion; nunca se borra aunque desaparezca."""

    __tablename__ = "backup_servidor"

    id_servidor_backup: Optional[int] = Field(default=None, sa_column=_pk())
    nombre: str = Field(max_length=255, nullable=False, unique=True)
    habilitado: bool = Field(default=True, nullable=False)
    # OK | PENDIENTE | ERROR | VACIO | NO_RECONOCIDO | NO_ENCONTRADO
    estado: str = Field(default="PENDIENTE", max_length=30, nullable=False)
    en_origen: bool = Field(default=True, nullable=False)
    detectado_en: Optional[datetime] = Field(default_factory=datetime.utcnow)
    ultimo_acceso: Optional[datetime] = Field(default=None)
    ultimo_backup_fecha: Optional[datetime] = Field(default=None)  # hora local del backup
    ultima_copia_valida: Optional[datetime] = Field(default=None)  # UTC
    observacion: Optional[str] = Field(default=None, sa_column=Column(Text))


class BackupRespaldo(SQLModel, table=True):
    """Una carpeta de respaldo (la que contiene los archivos), p. ej.
    2-CV-IQPAAB-GV-DTIC/2026-11-13_02-00-01."""

    __tablename__ = "backup_respaldo"

    id_respaldo: Optional[int] = Field(default=None, sa_column=_pk())
    id_servidor_backup: int = Field(sa_column=_fk("backup_servidor.id_servidor_backup"))
    ruta_relativa: str = Field(max_length=500, nullable=False, unique=True)
    plataforma: Optional[str] = Field(default=None, max_length=150)
    # estandar | docker_moodle | docker_cursos | desconocido
    tipo: str = Field(max_length=30, nullable=False)
    fecha_backup: Optional[datetime] = Field(default=None)  # hora local, tomada del nombre
    con_manifiesto: bool = Field(default=False, nullable=False)
    # COMPLETO | PENDIENTE | INCOMPLETO | NO_RECONOCIDO (evaluado sobre el origen)
    estado_origen: str = Field(default="PENDIENTE", max_length=30, nullable=False)
    # Estado local: DETECTADO | PENDIENTE | EN_DESCARGA | DESCARGADO | VALIDADO |
    # SIN_HASH | INCOMPLETO | NO_RECONOCIDO | ERROR_* | SIN_ESPACIO
    estado: str = Field(default="DETECTADO", max_length=30, nullable=False)
    en_origen: bool = Field(default=True, nullable=False)
    total_archivos: int = Field(default=0, nullable=False)
    tamano_total: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, default=0))
    detectado_en: Optional[datetime] = Field(default_factory=datetime.utcnow)
    publicado_en: Optional[datetime] = Field(default=None)
    observacion: Optional[str] = Field(default=None, sa_column=Column(Text))


class BackupArchivo(SQLModel, table=True):
    __tablename__ = "backup_archivo"

    id_archivo: Optional[int] = Field(default=None, sa_column=_pk())
    id_respaldo: int = Field(sa_column=_fk("backup_respaldo.id_respaldo"))
    ruta_relativa: str = Field(max_length=700, nullable=False, unique=True)
    nombre: str = Field(max_length=300, nullable=False)
    tamano_origen: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, default=0))
    mtime_origen: Optional[datetime] = Field(default=None)  # UTC
    hash_esperado: Optional[str] = Field(default=None, max_length=64)
    hash_local: Optional[str] = Field(default=None, max_length=64)
    tamano_local: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    estado: str = Field(default="DETECTADO", max_length=30, nullable=False)
    en_origen: bool = Field(default=True, nullable=False)
    intentos: int = Field(default=0, nullable=False)
    ultimo_error: Optional[str] = Field(default=None, sa_column=Column(Text))
    actualizado_en: Optional[datetime] = Field(default_factory=datetime.utcnow)
    validado_en: Optional[datetime] = Field(default=None)


class BackupEjecucion(SQLModel, table=True):
    __tablename__ = "backup_ejecucion"

    id_ejecucion: Optional[int] = Field(default=None, sa_column=_pk())
    tipo: str = Field(max_length=20, nullable=False)  # Automatica | Manual | Reintento | Inventario
    inicio: Optional[datetime] = Field(default_factory=datetime.utcnow)
    fin: Optional[datetime] = Field(default=None)
    ultimo_latido: Optional[datetime] = Field(default_factory=datetime.utcnow)
    # EN_CURSO | OK | CON_ADVERTENCIAS | ERROR | INTERRUMPIDA
    estado: str = Field(default="EN_CURSO", max_length=30, nullable=False)
    host: Optional[str] = Field(default=None, max_length=100)
    servidores: int = Field(default=0, nullable=False)
    archivos_nuevos: int = Field(default=0, nullable=False)
    archivos_descargados: int = Field(default=0, nullable=False)
    archivos_validados: int = Field(default=0, nullable=False)
    archivos_ya_existentes: int = Field(default=0, nullable=False)
    archivos_pendientes: int = Field(default=0, nullable=False)
    archivos_fallidos: int = Field(default=0, nullable=False)
    bytes_descargados: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, default=0))
    espacio_libre_destino: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    espacio_usado_backups: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    ruta_registros: Optional[str] = Field(default=None, max_length=500)
    resumen: Optional[str] = Field(default=None, sa_column=Column(Text))
    # Cola del .log de la ejecucion (el archivo completo queda en D:)
    log: Optional[str] = Field(default=None, sa_column=Column(Text))


class BackupEvento(SQLModel, table=True):
    """Advertencias, errores y transferencias de una ejecucion."""

    __tablename__ = "backup_evento"

    id_evento: Optional[int] = Field(default=None, sa_column=_pk())
    id_ejecucion: int = Field(sa_column=_fk("backup_ejecucion.id_ejecucion"))
    fecha_hora: Optional[datetime] = Field(default_factory=datetime.utcnow)
    nivel: str = Field(max_length=10, nullable=False)  # INFO | WARN | ERROR
    codigo: str = Field(max_length=40, nullable=False)
    servidor: Optional[str] = Field(default=None, max_length=255)
    ruta_relativa: Optional[str] = Field(default=None, max_length=700)
    mensaje: str = Field(sa_column=Column(Text, nullable=False))


class BackupSolicitud(SQLModel, table=True):
    """Pedidos del panel al agente local (que no es accesible desde Render)."""

    __tablename__ = "backup_solicitud"

    id_solicitud: Optional[int] = Field(default=None, sa_column=_pk())
    tipo: str = Field(max_length=30, nullable=False)  # EJECUTAR | REINTENTAR_FALLIDOS | INVENTARIO
    estado: str = Field(default="PENDIENTE", max_length=20, nullable=False)  # PENDIENTE | TOMADA | HECHA
    solicitado_en: Optional[datetime] = Field(default_factory=datetime.utcnow)
    tomada_en: Optional[datetime] = Field(default=None)
    id_ejecucion: Optional[int] = Field(
        default=None, sa_column=_fk("backup_ejecucion.id_ejecucion", ondelete="SET NULL", nullable=True)
    )


class BackupAgente(SQLModel, table=True):
    """Latido del agente local: el panel lo usa para saber si el PC con
    Windows esta encendido, cuanto espacio queda y con que configuracion
    corre (solo lectura desde el panel)."""

    __tablename__ = "backup_agente"

    host: str = Field(primary_key=True, max_length=100)
    ultimo_latido: Optional[datetime] = Field(default_factory=datetime.utcnow)
    estado: str = Field(default="INACTIVO", max_length=30, nullable=False)  # INACTIVO | EJECUTANDO
    ruta_origen: Optional[str] = Field(default=None, max_length=500)
    origen_accesible: bool = Field(default=False, nullable=False)
    ruta_destino: Optional[str] = Field(default=None, max_length=500)
    espacio_libre_destino: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    espacio_total_destino: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    espacio_usado_backups: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    espacio_libre_cache: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    proxima_ejecucion: Optional[datetime] = Field(default=None)  # hora local
    configuracion: Optional[str] = Field(default=None, sa_column=Column(Text))  # JSON
    # Ultimo conjunto de problemas avisado por Telegram desde el backend
    # (agente inactivo, sin ejecucion diaria...) para no repetir el aviso.
    alerta_clave: Optional[str] = Field(default=None, max_length=200)
