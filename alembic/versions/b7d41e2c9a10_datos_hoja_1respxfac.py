"""datos de la hoja 1RespXFac (tecnologia, contrato, notas)

Solo agrega columnas opcionales y amplia dos VARCHAR: el codigo que ya esta
en produccion sigue funcionando antes y despues de aplicarla.

Revision ID: b7d41e2c9a10
Revises: eeabe07d2aca
Create Date: 2026-11-10 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d41e2c9a10"
down_revision: Union[str, Sequence[str], None] = "eeabe07d2aca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("servidor", sa.Column("tecnologia", sa.String(length=50), nullable=True))

    # Varios responsables comparten una plataforma (nombres y celulares
    # concatenados): 150/20 se quedaban cortos y se truncaban.
    op.alter_column("responsable", "nombre_completo", type_=sa.String(length=255), existing_nullable=False)
    op.alter_column("responsable", "telefono", type_=sa.String(length=50), existing_nullable=True)
    op.add_column("responsable", sa.Column("tipo_contrato", sa.String(length=50), nullable=True))
    op.add_column("responsable", sa.Column("fecha_actualizacion", sa.Date(), nullable=True))

    op.add_column("plataforma", sa.Column("notas_credenciales", sa.Text(), nullable=True))
    op.add_column("plataforma", sa.Column("notas_migracion", sa.Text(), nullable=True))
    op.add_column("plataforma", sa.Column("responsable_anterior", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plataforma", "responsable_anterior")
    op.drop_column("plataforma", "notas_migracion")
    op.drop_column("plataforma", "notas_credenciales")
    op.drop_column("responsable", "fecha_actualizacion")
    op.drop_column("responsable", "tipo_contrato")
    op.alter_column("responsable", "telefono", type_=sa.String(length=20), existing_nullable=True)
    op.alter_column("responsable", "nombre_completo", type_=sa.String(length=150), existing_nullable=False)
    op.drop_column("servidor", "tecnologia")
