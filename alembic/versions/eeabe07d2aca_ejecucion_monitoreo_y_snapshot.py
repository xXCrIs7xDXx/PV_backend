"""ejecucion_monitoreo y snapshot

Revision ID: eeabe07d2aca
Revises: 57609e34123e
Create Date: 2026-06-18 15:37:07.002151

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eeabe07d2aca'
down_revision: Union[str, Sequence[str], None] = '57609e34123e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ejecucion_monitoreo",
        sa.Column("id_ejecucion", sa.BigInteger(), primary_key=True),
        sa.Column("fecha_hora", sa.DateTime(), nullable=True),
        sa.Column("tipo", sa.String(length=20), nullable=False),
    )
    op.add_column(
        "registro_monitoreo",
        sa.Column("id_ejecucion", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_registro_monitoreo_id_ejecucion",
        "registro_monitoreo",
        "ejecucion_monitoreo",
        ["id_ejecucion"],
        ["id_ejecucion"],
        ondelete="CASCADE",
    )
    op.add_column(
        "registro_monitoreo",
        sa.Column("estado_actual_snapshot", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("registro_monitoreo", "estado_actual_snapshot")
    op.drop_constraint("fk_registro_monitoreo_id_ejecucion", "registro_monitoreo", type_="foreignkey")
    op.drop_column("registro_monitoreo", "id_ejecucion")
    op.drop_table("ejecucion_monitoreo")
