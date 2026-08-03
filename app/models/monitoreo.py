from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, ForeignKey, Text
from sqlmodel import Field, SQLModel


class EjecucionMonitoreo(SQLModel, table=True):
    """One row per monitoring run/cycle (manual "Verificar ahora" or the
    scheduled job). registro_monitoreo rows reference this via id_ejecucion
    so the UI can group "una fila por revision" instead of one row per
    plataforma per cycle."""

    __tablename__ = "ejecucion_monitoreo"

    id_ejecucion: Optional[int] = Field(default=None, sa_column=Column(BigInteger, primary_key=True))
    fecha_hora: Optional[datetime] = Field(default_factory=datetime.utcnow)
    tipo: str = Field(max_length=20, nullable=False)  # "Manual" | "Automatica"


class RegistroMonitoreo(SQLModel, table=True):
    __tablename__ = "registro_monitoreo"

    id_registro: Optional[int] = Field(default=None, sa_column=Column(BigInteger, primary_key=True))
    id_plataforma: int = Field(
        sa_column=Column(
            "id_plataforma",
            ForeignKey("plataforma.id_plataforma", ondelete="CASCADE"),
            nullable=False,
        )
    )
    id_ejecucion: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, ForeignKey("ejecucion_monitoreo.id_ejecucion", ondelete="CASCADE"), nullable=True),
    )
    fecha_hora: Optional[datetime] = Field(default_factory=datetime.utcnow)
    estado_http: Optional[int] = Field(default=None)
    tiempo_respuesta_ms: Optional[int] = Field(default=None)
    esta_operativo: bool = Field(nullable=False)
    observacion: Optional[str] = Field(default=None, sa_column=Column(Text))
    estado_actual_snapshot: Optional[str] = Field(default=None, max_length=50)


class TicketIncidencia(SQLModel, table=True):
    __tablename__ = "ticket_incidencia"

    id_ticket: Optional[int] = Field(default=None, sa_column=Column(BigInteger, primary_key=True))
    id_plataforma: int = Field(foreign_key="plataforma.id_plataforma", nullable=False)
    id_registro_falla: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, ForeignKey("registro_monitoreo.id_registro"), nullable=True),
    )
    id_responsable_revision: int = Field(foreign_key="responsable.id_responsable", nullable=False)
    fecha_apertura: Optional[datetime] = Field(default_factory=datetime.utcnow)
    fecha_cierre: Optional[datetime] = Field(default=None)
    categoria_error: Optional[str] = Field(default=None, max_length=100)
    accion_realizada: Optional[str] = Field(default=None, sa_column=Column(Text))
    estado_ticket: Optional[str] = Field(default="Abierto", max_length=50)
