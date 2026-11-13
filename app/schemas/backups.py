from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BackupAgenteRead(_Orm):
    host: str
    ultimo_latido: Optional[datetime]
    activo: bool = False  # latido reciente
    estado: str
    ruta_origen: Optional[str]
    origen_accesible: bool
    ruta_destino: Optional[str]
    espacio_libre_destino: Optional[int]
    espacio_total_destino: Optional[int]
    espacio_usado_backups: Optional[int]
    espacio_libre_cache: Optional[int]
    proxima_ejecucion: Optional[datetime]
    configuracion: Optional[dict[str, Any]] = None


class AgenteLatido(BaseModel):
    """Lo que el agente envia cada minuto (se guarda en memoria, no en la base)."""

    host: str
    estado: str = "INACTIVO"
    ruta_origen: Optional[str] = None
    origen_accesible: Optional[bool] = None
    ruta_destino: Optional[str] = None
    espacio_libre_destino: Optional[int] = None
    espacio_total_destino: Optional[int] = None
    espacio_usado_backups: Optional[int] = None
    espacio_libre_cache: Optional[int] = None
    proxima_ejecucion: Optional[datetime] = None
    configuracion: Optional[dict[str, Any]] = None
    # Version de solicitudes que el agente ya reviso en la base sin encontrar nada
    ack_version: Optional[int] = None


class AgenteLatidoRespuesta(BaseModel):
    solicitud_pendiente: bool
    version: int


class BackupEjecucionRead(_Orm):
    id_ejecucion: int
    tipo: str
    inicio: Optional[datetime]
    fin: Optional[datetime]
    ultimo_latido: Optional[datetime]
    estado: str
    host: Optional[str]
    servidores: int
    archivos_nuevos: int
    archivos_descargados: int
    archivos_validados: int
    archivos_ya_existentes: int
    archivos_pendientes: int
    archivos_fallidos: int
    bytes_descargados: int
    espacio_libre_destino: Optional[int]
    espacio_usado_backups: Optional[int]
    ruta_registros: Optional[str]
    resumen: Optional[str]


class BackupEventoRead(_Orm):
    id_evento: int
    fecha_hora: Optional[datetime]
    nivel: str
    codigo: str
    servidor: Optional[str]
    ruta_relativa: Optional[str]
    mensaje: str


class BackupEjecucionDetalle(BackupEjecucionRead):
    eventos: list[BackupEventoRead]


class BackupServidorRead(_Orm):
    id_servidor_backup: int
    nombre: str
    habilitado: bool
    estado: str
    en_origen: bool
    detectado_en: Optional[datetime]
    ultimo_acceso: Optional[datetime]
    ultimo_backup_fecha: Optional[datetime]
    ultima_copia_valida: Optional[datetime]
    observacion: Optional[str]
    respaldos: int = 0
    respaldos_validados: int = 0  # con SHA-256
    respaldos_sin_hash: int = 0  # copiados, verificacion limitada
    respaldos_pendientes: int = 0
    respaldos_con_error: int = 0
    respaldos_no_reconocidos: int = 0
    bytes_en_origen: int = 0
    bytes_copiados: int = 0


class BackupServidorUpdate(BaseModel):
    habilitado: bool


class BackupRespaldoRead(_Orm):
    id_respaldo: int
    id_servidor_backup: int
    ruta_relativa: str
    plataforma: Optional[str]
    tipo: str
    fecha_backup: Optional[datetime]
    con_manifiesto: bool
    estado_origen: str
    estado: str
    en_origen: bool
    total_archivos: int
    tamano_total: int
    detectado_en: Optional[datetime]
    publicado_en: Optional[datetime]
    observacion: Optional[str]


class BackupArchivoRead(_Orm):
    id_archivo: int
    id_respaldo: int
    ruta_relativa: str
    nombre: str
    tamano_origen: int
    mtime_origen: Optional[datetime]
    hash_esperado: Optional[str]
    hash_local: Optional[str]
    tamano_local: Optional[int]
    estado: str
    en_origen: bool
    intentos: int
    ultimo_error: Optional[str]
    actualizado_en: Optional[datetime]
    validado_en: Optional[datetime]


class BackupSolicitudRead(_Orm):
    id_solicitud: int
    tipo: str
    estado: str
    solicitado_en: Optional[datetime]
    tomada_en: Optional[datetime]
    id_ejecucion: Optional[int]


class BackupsResumen(BaseModel):
    agentes: list[BackupAgenteRead]
    ultima_ejecucion: Optional[BackupEjecucionRead]
    ultima_ejecucion_exitosa: Optional[datetime]
    servidores: int
    respaldos_por_estado: dict[str, int]
    archivos_por_estado: dict[str, int]
    bytes_copiados: int
    # Problemas detectados desde el backend (agente caido, sin ejecucion diaria...)
    alertas: list[str]
    solicitudes_pendientes: list[BackupSolicitudRead]
