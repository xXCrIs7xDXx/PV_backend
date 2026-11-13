"""Pruebas del modulo de respaldos con un origen falso (carpetas locales) y
SQLite temporal: no tocan Neon, Google Drive ni el disco D:.

    pip install pytest aiosqlite
    python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app import models  # noqa: F401 - registra las tablas
from app.backups.config import BackupSettings
from app.backups.descubrimiento import clasificar, descubrir
from app.backups.fuente import DriveEscritorioFuente, Entrada
from app.backups.motor import BloqueoArchivo, MotorRespaldos
from app.backups.verificacion import ManifiestoInvalido, parsear_manifiesto
from app.models import BackupArchivo, BackupEjecucion, BackupEvento, BackupRespaldo, BackupServidor

HACE_UN_DIA = time.time() - 86400


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def escribir(p: Path, datos: bytes, mtime: float = HACE_UN_DIA) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(datos)
    os.utime(p, (mtime, mtime))


def respaldo_estandar(base: Path, plat: str, ts: str, *, hash_db_malo: bool = False, data: bytes | None = None) -> dict[str, bytes]:
    archivos = {
        f"codigo_{plat}_{ts}.tar.gz": os.urandom(3000) + b"codigo",
        f"db_{plat}_{ts}.sql.gz": os.urandom(5000) + b"db",
    }
    if data is not None:
        archivos[f"data_{plat}_{ts}.tar.gz"] = data
    lineas = []
    for n, b in archivos.items():
        h = sha(b)
        if hash_db_malo and n.startswith("db_"):
            h = "0" * 64
        lineas.append(f"{h}  {n}")
    for n, b in archivos.items():
        escribir(base / ts / n, b)
    escribir(base / ts / "hash_verificacion.txt", ("\n".join(lineas) + "\n").encode())
    escribir(base / ts / "desktop.ini", b"[.ShellClassInfo]")  # lo crea Drive; se ignora
    return archivos


@pytest.fixture
def entorno(tmp_path: Path):
    origen = tmp_path / "G" / "Backupumsa2627"
    destino = tmp_path / "D" / "Backups"

    # Servidor con estructura <servidor>/<fecha>/ y manifiesto
    a = respaldo_estandar(origen / "2-SRV-A", "piqpaa", "2026-11-10_02-00-01", data=os.urandom(20000))
    respaldo_estandar(origen / "2-SRV-A", "piqpaa", "2026-11-11_02-00-01", hash_db_malo=True)
    # Respaldo recien subido (mtime = ahora) -> PENDIENTE
    for n, b in respaldo_estandar(origen / "2-SRV-A", "piqpaa", "2026-11-12_02-00-01").items():
        os.utime(origen / "2-SRV-A" / "2026-11-12_02-00-01" / n, None)
    # Servidor con <servidor>/<plataforma>/<fecha>/ SIN manifiesto
    escribir(origen / "6-SRV-B" / "cvpfo" / "2026-11-10_03-00-01" / "codigo_cvpfo_2026-11-10_03-00-01.tar.gz", b"c" * 100)
    escribir(origen / "6-SRV-B" / "cvpfo" / "2026-11-10_03-00-01" / "db_cvpfo_2026-11-10_03-00-01.sql.gz", b"d" * 100)
    # Docker: moodle completo y cursos incompleto
    escribir(origen / "14-SRV-C" / "cvx_moodle_08-11-2026-020001" / "cvx_db_backup.sql", b"sql" * 50)
    escribir(origen / "14-SRV-C" / "cvx_moodle_08-11-2026-020001" / "cvx_moodledata.tar.gz", b"md" * 50)
    escribir(origen / "14-SRV-C" / "cvx_cursos_08-11-2026-020028" / "cursos.txt",
             "ID_CURSO | NOMBRE_CORTO | NOMBRE\n1 | A | Curso A\n2 | B | Curso B\n".encode())
    escribir(origen / "14-SRV-C" / "cvx_cursos_08-11-2026-020028" / "copia-curso-1.mbz", b"mbz1")
    # Archivo suelto en la carpeta del servidor -> NO_RECONOCIDO
    escribir(origen / "14-SRV-C" / "config_backup_06-04-2026", b"PLATFORM_DIR=/opt/x")
    # Servidor vacio
    (origen / "4-SRV-VACIO").mkdir(parents=True)
    escribir(origen / "4-SRV-VACIO" / "desktop.ini", b"x")

    db = tmp_path / "estado.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")

    async def crear():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    asyncio.run(crear())
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    alertas: list[str] = []

    async def notificar(texto: str) -> None:
        alertas.append(texto)

    def motor(**cfg_extra) -> MotorRespaldos:
        valores = dict(
            ruta_origen=str(origen), destino_raiz=destino, segundos_entre_sondeos=0,
            reintentos=2, espera_reintento_segundos=0, margen_minimo_libre_gb=0, alertas_telegram=False,
        )
        valores.update(cfg_extra)
        cfg = BackupSettings(_env_file=None, **valores)
        return MotorRespaldos(cfg, session_factory=sf, notificar=notificar)

    def consulta(stmt):
        async def _q():
            async with sf() as s:
                return (await s.exec(stmt)).all()
        return asyncio.run(_q())

    yield {"origen": origen, "destino": destino, "motor": motor, "q": consulta, "alertas": alertas, "datos_a": a}
    asyncio.run(engine.dispose())


def correr(m: MotorRespaldos, **kw) -> str:
    return asyncio.run(m.ejecutar(**kw))


# --------------------------------------------------------------------- unitarias
def test_manifiesto_formato_sha256sum():
    h = "a" * 64
    assert parsear_manifiesto(f"{h}  x.tar.gz\r\n{h.upper()} *y.sql.gz\n\n") == {"x.tar.gz": h, "y.sql.gz": h}
    with pytest.raises(ManifiestoInvalido):
        parsear_manifiesto("no es un hash  x")
    with pytest.raises(ManifiestoInvalido):
        parsear_manifiesto("")


def test_clasificacion_de_estructuras_reales():
    from datetime import datetime

    f = [Entrada("db_piqpaa_2026-11-13_02-00-01.sql.gz", "x", False, 1, datetime(2026, 1, 1))]
    r = clasificar("2-CV", "2-CV/2026-11-13_02-00-01", f)
    assert (r.tipo, r.plataforma, r.fecha_backup) == ("estandar", "piqpaa", datetime(2026, 11, 13, 2, 0, 1))
    r = clasificar("6-CV", "6-CV/cvpfo/2026-11-13_03-00-01", [])
    assert (r.tipo, r.plataforma) == ("estandar", "cvpfo")
    r = clasificar("16-CV", "16-CV/cvmicroflex_moodle_07-11-2026-020001", [])
    assert (r.tipo, r.plataforma, r.fecha_backup) == ("docker_moodle", "cvmicroflex", datetime(2026, 11, 7, 2, 0, 1))
    r = clasificar("19-P", "19-P/electronica/cvelectronica_cursos_08-11-2026-020028", [])
    assert (r.tipo, r.plataforma) == ("docker_cursos", "cvelectronica")
    assert clasificar("14-CV", "14-CV", []).tipo == "desconocido"
    assert clasificar("14-CV", "14-CV/otra_cosa", []).tipo == "desconocido"


def test_descubrimiento_dinamico_ignora_desktop_ini(entorno):
    inv = descubrir(DriveEscritorioFuente(entorno["origen"]))
    nombres = {s.nombre for s in inv.servidores}
    assert nombres == {"2-SRV-A", "6-SRV-B", "14-SRV-C", "4-SRV-VACIO"}
    vacio = next(s for s in inv.servidores if s.nombre == "4-SRV-VACIO")
    assert vacio.vacio
    a = next(s for s in inv.servidores if s.nombre == "2-SRV-A")
    assert all("desktop.ini" not in {f.nombre for f in r.archivos} for r in a.respaldos)


# ----------------------------------------------------------------- integracion
def test_primera_ejecucion_completa(entorno):
    m, q, d = entorno["motor"](), entorno["q"], entorno["destino"] / "Backupumsa2627"
    assert correr(m) == "ERROR"  # hay un hash incorrecto (caso 9)

    # Respaldo con manifiesto correcto: publicado y VALIDADO
    ok = d / "2-SRV-A" / "2026-11-10_02-00-01"
    for n, b in entorno["datos_a"].items():
        assert (ok / n).read_bytes() == b
    assert (ok / "hash_verificacion.txt").exists()
    assert not (ok / "desktop.ini").exists()
    estados = {r.ruta_relativa: r.estado for r in q(select(BackupRespaldo))}
    assert estados["2-SRV-A/2026-11-10_02-00-01"] == "VALIDADO"
    # Hash incorrecto: NO se publica (caso 9)
    assert estados["2-SRV-A/2026-11-11_02-00-01"] == "ERROR_INTEGRIDAD"
    assert not (d / "2-SRV-A" / "2026-11-11_02-00-01" / "db_piqpaa_2026-11-11_02-00-01.sql.gz").exists()
    # Respaldo recien modificado: PENDIENTE, no se copia (caso 7)
    assert estados["2-SRV-A/2026-11-12_02-00-01"] == "PENDIENTE"
    assert not (d / "2-SRV-A" / "2026-11-12_02-00-01").exists()
    # Sin manifiesto: copiado pero solo SIN_HASH (nunca "VALIDADO")
    assert estados["6-SRV-B/cvpfo/2026-11-10_03-00-01"] == "SIN_HASH"
    assert estados["14-SRV-C/cvx_moodle_08-11-2026-020001"] == "SIN_HASH"
    # cursos.txt lista 2 cursos y hay 1 .mbz
    assert estados["14-SRV-C/cvx_cursos_08-11-2026-020028"] == "INCOMPLETO"
    # Estructura desconocida: registrada, no descargada (caso 3)
    assert estados["14-SRV-C"] == "NO_RECONOCIDO"
    assert not (d / "14-SRV-C" / "config_backup_06-04-2026").exists()
    srv = {s.nombre: s.estado for s in q(select(BackupServidor))}
    assert srv["4-SRV-VACIO"] == "VACIO"

    # Registro diario (caso 12)
    dias = list((entorno["destino"] / "Registros").iterdir())
    assert len(dias) == 1
    for f in ("ejecucion.log", "resumen_diario.csv", "detalle_archivos.csv", "resumen_diario.json"):
        assert (dias[0] / f).exists(), f
    with open(dias[0] / "resumen_diario.csv", encoding="utf-8-sig") as f:
        filas = {r["servidor"]: r for r in csv.DictReader(f)}
    assert set(filas) == {"2-SRV-A", "6-SRV-B", "14-SRV-C", "4-SRV-VACIO"}
    assert filas["2-SRV-A"]["estado_general"] == "ERROR"
    assert filas["4-SRV-VACIO"]["estado_general"] == "VACIO"
    with open(dias[0] / "detalle_archivos.csv", encoding="utf-8-sig") as f:
        detalle = list(csv.DictReader(f))
    validados = [r for r in detalle if r["estado"] == "VALIDADO"]
    assert validados and all(r["hash_esperado_sha256"] == r["hash_obtenido_sha256"] for r in validados)
    assert any(r["estado"] == "SIN_HASH" and r["verificacion"] == "LIMITADA" for r in detalle)
    # Temporales limpios (solo queda lo del respaldo con error para reintentar)
    restos = [p for p in (entorno["destino"] / "_TEMPORAL").rglob("*") if p.is_file() and not p.name.startswith(".")]
    assert all("2026-11-11_02-00-01" in str(p) for p in restos)
    # Alerta enviada (nunca un "exito" con fallos)
    assert entorno["alertas"] and "ERROR" in entorno["alertas"][0]


def test_reejecucion_es_idempotente(entorno):
    q = entorno["q"]
    correr(entorno["motor"]())
    n_arch = len(q(select(BackupArchivo)))
    m2 = entorno["motor"]()
    correr(m2)
    e = q(select(BackupEjecucion).order_by(BackupEjecucion.id_ejecucion.desc()))[0]
    assert e.archivos_descargados == 0  # el respaldo con hash malo se reintenta pero vuelve a fallar
    assert e.archivos_nuevos == 0
    # 2-SRV-A/10 (4 archivos) + 6-SRV-B (2) + 14-SRV-C moodle (2)
    assert e.archivos_ya_existentes == 8
    assert len(q(select(BackupArchivo))) == n_arch  # sin duplicados (caso 5)


def test_borrado_en_drive_conserva_historico(entorno):
    correr(entorno["motor"]())
    ruta = entorno["origen"] / "6-SRV-B" / "cvpfo" / "2026-11-10_03-00-01" / "db_cvpfo_2026-11-10_03-00-01.sql.gz"
    ruta.unlink()
    correr(entorno["motor"]())
    local = entorno["destino"] / "Backupumsa2627" / "6-SRV-B" / "cvpfo" / "2026-11-10_03-00-01" / ruta.name
    assert local.exists()  # caso 6
    a = entorno["q"](select(BackupArchivo).where(BackupArchivo.nombre == ruta.name))[0]
    assert a.en_origen is False and a.estado == "SIN_HASH"


def test_no_sobrescribe_archivo_local_distinto(entorno):
    final = entorno["destino"] / "Backupumsa2627" / "2-SRV-A" / "2026-11-10_02-00-01" / "codigo_piqpaa_2026-11-10_02-00-01.tar.gz"
    escribir(final, b"contenido distinto que alguien dejo a mano")
    correr(entorno["motor"]())
    assert final.read_bytes() == b"contenido distinto que alguien dejo a mano"
    a = entorno["q"](select(BackupArchivo).where(BackupArchivo.nombre == final.name))[0]
    assert a.estado == "ERROR_INTEGRIDAD"
    eventos = entorno["q"](select(BackupEvento).where(BackupEvento.codigo == "CONFLICTO_LOCAL"))
    assert eventos


def test_reanuda_descarga_interrumpida(entorno):
    nombre = "data_piqpaa_2026-11-10_02-00-01.tar.gz"
    datos = entorno["datos_a"][nombre]
    part = entorno["destino"] / "_TEMPORAL" / "2-SRV-A" / "2026-11-10_02-00-01" / (nombre + ".part")
    escribir(part, datos[:7000])  # simula un corte a mitad (caso 8)
    correr(entorno["motor"]())
    final = entorno["destino"] / "Backupumsa2627" / "2-SRV-A" / "2026-11-10_02-00-01" / nombre
    assert final.read_bytes() == datos
    log = entorno["q"](select(BackupEjecucion))[0].log
    assert "reanudando desde" in log


def test_sin_espacio_no_borra_nada(entorno):
    correr(entorno["motor"](margen_minimo_libre_gb=10**9))  # caso 10
    e = entorno["q"](select(BackupEjecucion))[0]
    assert e.estado == "ERROR"
    assert entorno["q"](select(BackupArchivo).where(BackupArchivo.estado == "SIN_ESPACIO"))
    assert not any((entorno["destino"] / "Backupumsa2627").rglob("*.gz"))
    assert entorno["origen"].exists() and any(entorno["origen"].rglob("*.gz"))


def test_origen_inaccesible_registra_fallo(entorno, tmp_path):
    m = entorno["motor"](ruta_origen=str(tmp_path / "no-existe"))
    assert correr(m) == "ERROR"  # caso 11
    e = entorno["q"](select(BackupEjecucion))[0]
    assert e.estado == "ERROR" and "BACKUPS_RUTA_ORIGEN" in e.resumen
    assert not entorno["q"](select(BackupServidor))  # no se inventa nada
    dia = next((entorno["destino"] / "Registros").iterdir())
    datos = json.loads((dia / "resumen_diario.json").read_text(encoding="utf-8"))
    assert datos["ejecuciones"][0]["estado"] == "ERROR"


def test_ejecuciones_simultaneas_se_rechazan(entorno):
    m = entorno["motor"]()
    m.cfg.destino_temporal.mkdir(parents=True, exist_ok=True)
    with BloqueoArchivo(m.cfg.destino_temporal / ".ejecucion.lock"):
        assert correr(m) == "OCUPADO"  # caso 13
    assert not entorno["q"](select(BackupEjecucion))


def test_servidor_nuevo_se_detecta_sin_cambiar_codigo(entorno):
    correr(entorno["motor"]())
    respaldo_estandar(entorno["origen"] / "99-SRV-NUEVO", "nueva", "2026-11-10_01-00-01")
    correr(entorno["motor"]())
    srv = {s.nombre: s for s in entorno["q"](select(BackupServidor))}
    assert "99-SRV-NUEVO" in srv  # caso 2
    assert (entorno["destino"] / "Backupumsa2627" / "99-SRV-NUEVO" / "2026-11-10_01-00-01" / "hash_verificacion.txt").exists()


def test_limite_por_ejecucion_hace_lotes(entorno):
    correr(entorno["motor"](max_gb_por_ejecucion=0.000001))  # ~1 KB: un respaldo por vez (caso 4)
    e = entorno["q"](select(BackupEjecucion))[0]
    assert e.archivos_descargados >= 1
    pendientes = entorno["q"](select(BackupRespaldo).where(BackupRespaldo.observacion.like("%limite%")))
    assert pendientes


def test_api_panel_y_solicitud_al_agente(entorno):
    """Panel (Render) -> backup_solicitud -> agente local -> resultado visible en el panel."""
    from fastapi.testclient import TestClient

    from app import auth_state
    from app.backups.agente import ServicioRespaldos
    from app.db import get_session
    from app.main import app

    m = entorno["motor"]()

    async def sesion():
        async with m.sf() as s:
            yield s

    app.dependency_overrides[get_session] = sesion
    try:
        cliente = TestClient(app)
        assert cliente.get("/api/backups/resumen").status_code == 401  # nada publico
        h = {"Authorization": f"Bearer {auth_state.issue_token()}"}
        r = cliente.post("/api/backups/ejecutar", headers=h)
        assert r.status_code == 200 and r.json()["estado"] == "PENDIENTE"
        assert cliente.post("/api/backups/ejecutar", headers=h).status_code == 409  # sin duplicar

        asyncio.run(ServicioRespaldos(m).ciclo())  # el agente toma la solicitud

        sol = cliente.get("/api/backups/solicitudes", headers=h).json()[0]
        assert sol["estado"] == "HECHA" and sol["id_ejecucion"]
        res = cliente.get("/api/backups/resumen", headers=h).json()
        assert res["ultima_ejecucion"]["tipo"] == "Manual"
        assert res["respaldos_por_estado"]["VALIDADO"] == 1
        assert res["agentes"] and res["agentes"][0]["configuracion"]["sobrescribir_validados"] is False
        srv = {s["nombre"]: s for s in cliente.get("/api/backups/servidores", headers=h).json()}
        assert srv["2-SRV-A"]["estado"] == "ERROR" and srv["6-SRV-B"]["estado"] == "OK"
        assert srv["4-SRV-VACIO"]["estado"] == "VACIO"
        det = cliente.get(f"/api/backups/ejecuciones/{sol['id_ejecucion']}", headers=h).json()
        assert any(e["codigo"] == "ERROR_INTEGRIDAD" for e in det["eventos"])
        assert "Inicio de ejecucion" in cliente.get(f"/api/backups/ejecuciones/{sol['id_ejecucion']}/log", headers=h).text
        assert cliente.get("/api/backups/respaldos?estado=ERRORES", headers=h).json()
        inv = cliente.get("/api/backups/exportar/inventario.csv", headers=h)
        assert inv.status_code == 200 and "hash_esperado_sha256" in inv.text
        id_srv = srv["6-SRV-B"]["id_servidor_backup"]
        assert cliente.patch(f"/api/backups/servidores/{id_srv}", json={"habilitado": False}, headers=h).json()["habilitado"] is False
    finally:
        app.dependency_overrides.clear()


def test_hoja_compartida_se_regenera_en_el_mismo_archivo(entorno, tmp_path):
    from openpyxl import load_workbook

    ruta = tmp_path / "G" / "Mi unidad" / "Respaldos DTIC" / "Registro de respaldos.xlsx"
    correr(entorno["motor"](hoja_ruta=str(ruta)))
    wb = load_workbook(ruta)
    assert wb.sheetnames == ["Resumen", "Registro diario", "Copias por día", "Respaldos", "Archivos copiados", "Avisos"]
    diario = list(wb["Registro diario"].iter_rows(values_only=True))
    assert diario[0][0] == "N.º" and len(diario) == 2 and diario[1][4] == "ERROR"
    archivos = list(wb["Archivos copiados"].iter_rows(values_only=True))
    assert len(archivos) == 1 + 8  # 4 validados SHA-256 + 4 verificados por tamano
    validados = [f for f in archivos[1:] if f[6] == "Validado (SHA-256)"]
    assert len(validados) == 4 and all(len(f[7]) == 64 for f in validados)
    copias = list(wb["Copias por día"].iter_rows(values_only=True))
    assert {f[1] for f in copias[1:]} == {"2-SRV-A", "6-SRV-B", "14-SRV-C"}
    estados = {f[3]: f[4] for f in wb["Respaldos"].iter_rows(min_row=2, values_only=True)}
    assert estados["2-SRV-A/2026-11-11_02-00-01"] == "ERROR de integridad"
    assert any(f[3] == "ERROR_INTEGRIDAD" for f in wb["Avisos"].iter_rows(min_row=2, values_only=True))

    # Segunda ejecucion: mismo archivo (mismo enlace en Drive), una fila mas en el registro
    correr(entorno["motor"](hoja_ruta=str(ruta)))
    assert len(list(load_workbook(ruta)["Registro diario"].iter_rows(values_only=True))) == 3
    assert [p.name for p in ruta.parent.iterdir()] == [ruta.name]


def test_hoja_que_no_se_puede_escribir_no_rompe_la_ejecucion(entorno, tmp_path):
    bloqueada = tmp_path / "es_una_carpeta.xlsx"
    bloqueada.mkdir()
    correr(entorno["motor"](hoja_ruta=str(bloqueada)))
    e = entorno["q"](select(BackupEjecucion))[0]
    assert "hoja compartida" in e.resumen
    assert entorno["q"](select(BackupRespaldo).where(BackupRespaldo.estado == "VALIDADO"))  # la copia si se hizo


def test_latido_en_memoria_no_consulta_la_base_en_reposo(entorno, monkeypatch):
    """El agente late por HTTP; el backend responde desde memoria y solo se
    va a la base cuando el panel pidio algo (cuidado del computo de Neon)."""
    from fastapi.testclient import TestClient

    from app import auth_state
    from app.db import get_session
    from app.main import app
    from app.services import backups_enlace, backups_vigilancia

    m = entorno["motor"]()
    consultas = {"n": 0}

    async def sesion():
        async with m.sf() as s:
            original = s.exec

            async def contar(*a, **k):
                consultas["n"] += 1
                return await original(*a, **k)

            s.exec = contar
            yield s

    monkeypatch.setattr(backups_enlace, "_pendiente", None)
    monkeypatch.setattr(backups_enlace, "_version", 0)
    monkeypatch.setattr(backups_enlace, "_latidos", {})
    app.dependency_overrides[get_session] = sesion
    try:
        c = TestClient(app)
        h = {"Authorization": f"Bearer {auth_state.issue_token()}"}
        cuerpo = {"host": "PC-DTIC", "estado": "INACTIVO", "origen_accesible": True, "espacio_libre_destino": 10**12,
                  "configuracion": {"hora_diaria": "10:00"}}
        assert c.post("/api/backups/agente/latido", json=cuerpo).status_code == 401
        r = c.post("/api/backups/agente/latido", json=cuerpo, headers=h).json()
        assert r == {"solicitud_pendiente": False, "version": 0}
        assert consultas["n"] == 1  # solo la primera vez tras arrancar
        for _ in range(5):
            c.post("/api/backups/agente/latido", json=cuerpo, headers=h)
        assert consultas["n"] == 1  # en reposo: cero consultas

        c.post("/api/backups/ejecutar", headers=h)
        r = c.post("/api/backups/agente/latido", json=cuerpo, headers=h).json()
        assert r == {"solicitud_pendiente": True, "version": 1}
        # El agente ya la atendio y confirma la version que vio
        r = c.post("/api/backups/agente/latido", json={**cuerpo, "ack_version": 1}, headers=h).json()
        assert r["solicitud_pendiente"] is False

        # El panel ve al agente activo aunque nunca escribio en la base...
        ag = c.get("/api/backups/resumen", headers=h).json()["agentes"][0]
        assert ag["host"] == "PC-DTIC" and ag["activo"] and ag["configuracion"]["hora_diaria"] == "10:00"
        assert not entorno["q"](select(models.BackupAgente))
        # ...y el ciclo horario lo guarda para que sobreviva a un reinicio de Render
        monkeypatch.setattr(backups_vigilancia, "async_session_factory", m.sf)
        asyncio.run(backups_vigilancia.revisar())
        guardado = entorno["q"](select(models.BackupAgente))[0]
        assert guardado.host == "PC-DTIC" and guardado.espacio_libre_destino == 10**12
    finally:
        app.dependency_overrides.clear()


def test_agente_usa_el_latido_http_y_reintenta_si_falla(entorno):
    from app.backups.agente import ServicioRespaldos

    srv = ServicioRespaldos(entorno["motor"](backend_url="http://backend.invalido", hora_diaria="23:59"))
    respuestas = iter([{"solicitud_pendiente": False, "version": 0}, None, {"solicitud_pendiente": True, "version": 4}])
    tomadas = []

    async def latido():
        return next(respuestas)

    async def tomar():
        tomadas.append(1)
        return None

    srv.latido_http, srv.tomar_solicitud = latido, tomar
    asyncio.run(srv.ciclo())  # backend dice "nada": no se toca la base
    assert tomadas == []
    asyncio.run(srv.ciclo())  # backend caido: se cae al sondeo lento (1.a vez consulta)
    assert tomadas == [1]
    asyncio.run(srv.ciclo())  # backend dice "hay": se consulta y se confirma la version
    assert tomadas == [1, 1] and srv.ack_version == 4


def test_rutas_de_mas_de_260_caracteres(entorno):
    """Los .mbz de Moodle tienen nombres largos; con rutas profundas pasan el
    limite MAX_PATH de Windows (lo detecto la prueba real del 13/11/2026)."""
    carpeta = "cvlargo_cursos_08-11-2026-020028"
    nombre = "copia_de_seguridad-moodle2-course-2-" + "derechos_estudiantiles_umsa_" * 6 + "20261011-0900.mbz"
    base = entorno["origen"] / ("19-" + "P" * 60) / ("sub" * 20)
    from app.backups.config import ruta_extendida

    escribir(ruta_extendida(base / carpeta / "cursos.txt"), b"ID | X | Y\n1 | A | B\n")
    escribir(ruta_extendida(base / carpeta / nombre), b"mbz-largo")
    correr(entorno["motor"]())
    final = ruta_extendida(entorno["destino"] / "Backupumsa2627" / ("19-" + "P" * 60) / ("sub" * 20) / carpeta / nombre)
    assert len(str(final)) > 300
    assert final.read_bytes() == b"mbz-largo"


def test_inventario_no_descarga(entorno):
    assert correr(entorno["motor"](), tipo="Inventario", modo="inventario") in ("OK", "CON_ADVERTENCIAS", "ERROR")
    assert not any((entorno["destino"] / "Backupumsa2627").rglob("*.gz"))
    assert entorno["q"](select(BackupArchivo))
