import os
import sys
from pathlib import Path

# app.config exige estas variables; las pruebas nunca se conectan a la base real.
os.environ.setdefault("DATABASE_URL", "postgresql://prueba:prueba@localhost:5432/prueba")
os.environ.setdefault("ADMIN_PASSWORD", "prueba")
os.environ["TELEGRAM_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
