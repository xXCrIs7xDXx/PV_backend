# Backend — Monitoreo Campus Virtuales UMSA

FastAPI + SQLModel + asyncpg + Alembic + APScheduler. Ver la documentación
completa del proyecto en [`../README.md`](../README.md) y la guía paso a
paso (entorno, `.env`, Alembic, siembra, Google Forms) en
[`../GUIA_DE_USO.md`](../GUIA_DE_USO.md).

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # y completa tus valores
alembic stamp head
uvicorn app.main:app --reload --port 8000
```
