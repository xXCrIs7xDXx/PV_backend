"""Quitar modulo de respaldos Drive -> D:

Borra las 7 tablas backup_* que creo c3f8a1d2b4e6. Los respaldos ya copiados
en D:\\Backups no se tocan (no estan en la base).

Revision ID: d9e2b7c4a1f3
Revises: c3f8a1d2b4e6
Create Date: 2026-11-17
"""
import importlib.util
from pathlib import Path
from typing import Sequence, Union


revision: str = "d9e2b7c4a1f3"
down_revision: Union[str, Sequence[str], None] = "c3f8a1d2b4e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _migracion_original():
    ruta = Path(__file__).with_name("c3f8a1d2b4e6_modulo_respaldos_drive.py")
    spec = importlib.util.spec_from_file_location("c3f8a1d2b4e6", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def upgrade() -> None:
    # Mismo orden que el downgrade de c3f8a1d2b4e6 (respeta las FK)
    _migracion_original().downgrade()


def downgrade() -> None:
    # Vuelve a crear las tablas vacias (los datos borrados no se recuperan)
    _migracion_original().upgrade()
