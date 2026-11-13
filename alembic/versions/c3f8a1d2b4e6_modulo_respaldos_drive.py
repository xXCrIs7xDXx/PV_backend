"""modulo de respaldos Google Drive -> D: (tablas backup_*)

Solo CREA tablas nuevas; no toca ninguna existente. El codigo en produccion
sigue funcionando antes y despues de aplicarla.

Revision ID: c3f8a1d2b4e6
Revises: b7d41e2c9a10
Create Date: 2026-11-13 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3f8a1d2b4e6"
down_revision: Union[str, Sequence[str], None] = "b7d41e2c9a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "backup_servidor",
        sa.Column("id_servidor_backup", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("nombre", sa.String(255), nullable=False, unique=True),
        sa.Column("habilitado", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("estado", sa.String(30), nullable=False, server_default="PENDIENTE"),
        sa.Column("en_origen", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("detectado_en", sa.DateTime(), nullable=True),
        sa.Column("ultimo_acceso", sa.DateTime(), nullable=True),
        sa.Column("ultimo_backup_fecha", sa.DateTime(), nullable=True),
        sa.Column("ultima_copia_valida", sa.DateTime(), nullable=True),
        sa.Column("observacion", sa.Text(), nullable=True),
    )
    op.create_table(
        "backup_respaldo",
        sa.Column("id_respaldo", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "id_servidor_backup", sa.BigInteger(),
            sa.ForeignKey("backup_servidor.id_servidor_backup", ondelete="CASCADE"), nullable=False, index=True,
        ),
        sa.Column("ruta_relativa", sa.String(500), nullable=False, unique=True),
        sa.Column("plataforma", sa.String(150), nullable=True),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("fecha_backup", sa.DateTime(), nullable=True),
        sa.Column("con_manifiesto", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("estado_origen", sa.String(30), nullable=False, server_default="PENDIENTE"),
        sa.Column("estado", sa.String(30), nullable=False, server_default="DETECTADO"),
        sa.Column("en_origen", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("total_archivos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tamano_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("detectado_en", sa.DateTime(), nullable=True),
        sa.Column("publicado_en", sa.DateTime(), nullable=True),
        sa.Column("observacion", sa.Text(), nullable=True),
    )
    op.create_table(
        "backup_archivo",
        sa.Column("id_archivo", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "id_respaldo", sa.BigInteger(),
            sa.ForeignKey("backup_respaldo.id_respaldo", ondelete="CASCADE"), nullable=False, index=True,
        ),
        sa.Column("ruta_relativa", sa.String(700), nullable=False, unique=True),
        sa.Column("nombre", sa.String(300), nullable=False),
        sa.Column("tamano_origen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mtime_origen", sa.DateTime(), nullable=True),
        sa.Column("hash_esperado", sa.String(64), nullable=True),
        sa.Column("hash_local", sa.String(64), nullable=True),
        sa.Column("tamano_local", sa.BigInteger(), nullable=True),
        sa.Column("estado", sa.String(30), nullable=False, server_default="DETECTADO"),
        sa.Column("en_origen", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ultimo_error", sa.Text(), nullable=True),
        sa.Column("actualizado_en", sa.DateTime(), nullable=True),
        sa.Column("validado_en", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_backup_archivo_estado", "backup_archivo", ["estado"])
    op.create_table(
        "backup_ejecucion",
        sa.Column("id_ejecucion", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("inicio", sa.DateTime(), nullable=True),
        sa.Column("fin", sa.DateTime(), nullable=True),
        sa.Column("ultimo_latido", sa.DateTime(), nullable=True),
        sa.Column("estado", sa.String(30), nullable=False, server_default="EN_CURSO"),
        sa.Column("host", sa.String(100), nullable=True),
        sa.Column("servidores", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archivos_nuevos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archivos_descargados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archivos_validados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archivos_ya_existentes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archivos_pendientes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archivos_fallidos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_descargados", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("espacio_libre_destino", sa.BigInteger(), nullable=True),
        sa.Column("espacio_usado_backups", sa.BigInteger(), nullable=True),
        sa.Column("ruta_registros", sa.String(500), nullable=True),
        sa.Column("resumen", sa.Text(), nullable=True),
        sa.Column("log", sa.Text(), nullable=True),
    )
    op.create_index("ix_backup_ejecucion_inicio", "backup_ejecucion", ["inicio"])
    op.create_table(
        "backup_evento",
        sa.Column("id_evento", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "id_ejecucion", sa.BigInteger(),
            sa.ForeignKey("backup_ejecucion.id_ejecucion", ondelete="CASCADE"), nullable=False, index=True,
        ),
        sa.Column("fecha_hora", sa.DateTime(), nullable=True),
        sa.Column("nivel", sa.String(10), nullable=False),
        sa.Column("codigo", sa.String(40), nullable=False),
        sa.Column("servidor", sa.String(255), nullable=True),
        sa.Column("ruta_relativa", sa.String(700), nullable=True),
        sa.Column("mensaje", sa.Text(), nullable=False),
    )
    op.create_table(
        "backup_solicitud",
        sa.Column("id_solicitud", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="PENDIENTE"),
        sa.Column("solicitado_en", sa.DateTime(), nullable=True),
        sa.Column("tomada_en", sa.DateTime(), nullable=True),
        sa.Column(
            "id_ejecucion", sa.BigInteger(),
            sa.ForeignKey("backup_ejecucion.id_ejecucion", ondelete="SET NULL"), nullable=True, index=True,
        ),
    )
    op.create_table(
        "backup_agente",
        sa.Column("host", sa.String(100), primary_key=True),
        sa.Column("ultimo_latido", sa.DateTime(), nullable=True),
        sa.Column("estado", sa.String(30), nullable=False, server_default="INACTIVO"),
        sa.Column("ruta_origen", sa.String(500), nullable=True),
        sa.Column("origen_accesible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ruta_destino", sa.String(500), nullable=True),
        sa.Column("espacio_libre_destino", sa.BigInteger(), nullable=True),
        sa.Column("espacio_total_destino", sa.BigInteger(), nullable=True),
        sa.Column("espacio_usado_backups", sa.BigInteger(), nullable=True),
        sa.Column("espacio_libre_cache", sa.BigInteger(), nullable=True),
        sa.Column("proxima_ejecucion", sa.DateTime(), nullable=True),
        sa.Column("configuracion", sa.Text(), nullable=True),
        sa.Column("alerta_clave", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("backup_agente")
    op.drop_table("backup_solicitud")
    op.drop_table("backup_evento")
    op.drop_index("ix_backup_ejecucion_inicio", table_name="backup_ejecucion")
    op.drop_table("backup_ejecucion")
    op.drop_index("ix_backup_archivo_estado", table_name="backup_archivo")
    op.drop_table("backup_archivo")
    op.drop_table("backup_respaldo")
    op.drop_table("backup_servidor")
