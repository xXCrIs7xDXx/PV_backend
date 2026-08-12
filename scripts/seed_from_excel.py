"""One-time, idempotent seed of the catalog tables (tipo_unidad_academica,
unidad_academica, tipo_plataforma, responsable, servidor, plataforma) from
"Moddles de toda la Universidad Codigo.xlsx".

Re-runnable safely: every write is an application-level "select by natural
key, then insert-or-update" (the immutable DDL only has a UNIQUE constraint
on plataforma.url_principal, so this is done in Python rather than via
Postgres ON CONFLICT for the other tables). Running this script twice never
duplicates rows.

Usage:
    python -m scripts.seed_from_excel --file "../Moddles de toda la Universidad Codigo.xlsx"

See the implementation plan for the exact normalization rules (sections on
tipo_unidad_academica/tipo_plataforma normalization, responsable 2026-over-
2025 precedence + email placeholders, servidor inventory + orphan-IP
placeholders, and plataforma URL dedupe).
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

import openpyxl
from sqlmodel import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import async_session_factory  # noqa: E402
from app.models import (  # noqa: E402
    Plataforma,
    Responsable,
    Servidor,
    TipoPlataforma,
    TipoUnidadAcademica,
    UnidadAcademica,
)

# --- Normalization tables (rule #1) -----------------------------------------

RAW_TO_TIPO_UNIDAD = {
    "Facultad": "Facultad",
    None: "Otro",
    "Carrera": "Carrera",
    "Carreras": "Carrera",
    "Postgrado": "Postgrado",
    "Postgrado e institutos": "Postgrado",
    "Instituto": "Instituto",
    "Institutos": "Instituto",
    "Investigacion": "Instituto",
    "Pre Universitario": "Preuniversitario",
    "CETI": "Institucional",
    "Vicerrectorado": "Institucional",
    "DINA": "Institucional",
    "DIPGIS": "Institucional",
    "PPCAD": "Institucional",
    "Recursos Humanos Administrativos": "Institucional",
    "DTIC": "Institucional",
    "CEPIES": "Institucional",
    "STUMSA": "Institucional",
}

RAW_TO_TIPO_PLATAFORMA = {
    "Facultad": "Pregrado",
    None: "Institucional",
    "Carrera": "Pregrado",
    "Carreras": "Pregrado",
    "Postgrado": "Postgrado",
    "Postgrado e institutos": "Postgrado",
    "Instituto": "Instituto",
    "Institutos": "Instituto",
    "Investigacion": "Instituto",
    "Pre Universitario": "Preuniversitario",
    "CETI": "Institucional",
    "Vicerrectorado": "Institucional",
    "DINA": "Institucional",
    "DIPGIS": "Institucional",
    "PPCAD": "Institucional",
    "Recursos Humanos Administrativos": "Institucional",
    "DTIC": "Institucional",
    "CEPIES": "Institucional",
    "STUMSA": "Institucional",
}

HONORIFICS = ("dr.", "dra.", "lic.", "ing.")
MEM_RE = re.compile(r"\(GB\s*(\d+)\)")


def clean_str(value, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s


def clean_phone(value) -> str | None:
    s = clean_str(value)
    if s is None:
        return None
    if s.endswith(".0"):
        s = s[:-2]
    return s or None


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "sin-dato"


def normalize_name_key(name: str) -> str:
    n = " ".join(name.split()).strip().lower()
    for h in HONORIFICS:
        if n.startswith(h):
            n = n[len(h):].strip()
            break
    return n


def normalize_url(url: str) -> str:
    u = url.strip()
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    return u


def normalize_url_key(url: str) -> str:
    u = normalize_url(url).lower().rstrip("/")
    return u


async def upsert(session, model, lookup: dict, values: dict):
    """Select-by-natural-key then insert-or-update (see module docstring:
    the immutable DDL doesn't have UNIQUE constraints on these natural keys,
    so we can't use Postgres ON CONFLICT here)."""
    stmt = select(model).filter_by(**lookup)
    existing = (await session.exec(stmt)).first()
    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        session.add(existing)
        await session.flush()
        return existing
    obj = model(**lookup, **values)
    session.add(obj)
    await session.flush()
    return obj


def load_rows(ws, min_row=2):
    return [r for r in ws.iter_rows(min_row=min_row, values_only=True) if r[0] is not None]


async def seed(xlsx_path: Path) -> None:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    resp_rows = load_rows(wb["Responsables"])
    srv_rows = load_rows(wb["Resumen servidores Redes"])

    print(f"Responsables: {len(resp_rows)} filas | Resumen servidores Redes: {len(srv_rows)} filas")

    async with async_session_factory() as session:
        # --- Step 1: tipo_unidad_academica / tipo_plataforma -----------------
        tipo_unidad_id: dict[str, int] = {}
        for nombre in sorted(set(RAW_TO_TIPO_UNIDAD.values())):
            obj = await upsert(session, TipoUnidadAcademica, {"nombre_tipo": nombre}, {"descripcion": None})
            tipo_unidad_id[nombre] = obj.id_tipo_unidad

        tipo_plataforma_id: dict[str, int] = {}
        for nombre in sorted(set(RAW_TO_TIPO_PLATAFORMA.values())):
            obj = await upsert(session, TipoPlataforma, {"nombre_tipo": nombre}, {"descripcion": None})
            tipo_plataforma_id[nombre] = obj.id_tipo_plataforma

        # --- Step 2: parent unidad_academica per Facultad --------------------
        facultades = []
        seen_fac = set()
        for r in resp_rows:
            fac = clean_str(r[3])
            if fac and fac not in seen_fac:
                seen_fac.add(fac)
                facultades.append(fac)

        facultad_id: dict[str, int] = {}
        for fac in facultades:
            obj = await upsert(
                session,
                UnidadAcademica,
                {"nombre": fac, "id_unidad_padre": None},
                {"id_tipo_unidad": tipo_unidad_id["Facultad"]},
            )
            facultad_id[fac] = obj.id_unidad

        # --- Step 3: child unidad_academica for Tipo not in (None, "Facultad") --
        child_id: dict[tuple[str, str], int] = {}
        for r in resp_rows:
            fac = clean_str(r[3])
            tipo_raw = clean_str(r[4])
            carrera = clean_str(r[5])
            if not fac or tipo_raw is None or tipo_raw == "Facultad":
                continue
            child_name = carrera or tipo_raw
            key = (fac, child_name)
            if key in child_id:
                continue
            obj = await upsert(
                session,
                UnidadAcademica,
                {"nombre": child_name, "id_unidad_padre": facultad_id[fac]},
                {"id_tipo_unidad": tipo_unidad_id[RAW_TO_TIPO_UNIDAD[tipo_raw]]},
            )
            child_id[key] = obj.id_unidad

        # --- Step 4: servidor from "Resumen servidores Redes" -----------------
        servidor_by_ip: dict[str, int] = {}
        servidor_by_hostname: dict[str, int] = {}
        skipped_servers = []
        for r in srv_rows:
            hostname = clean_str(r[1], 100)
            ip_publica = clean_str(r[2], 45)
            ip_privada = clean_str(r[3], 45)
            if not hostname:
                continue
            if not ip_privada and not ip_publica:
                skipped_servers.append(hostname)
                continue
            ip_privada = ip_privada or ip_publica

            mem_match = MEM_RE.search(str(r[7])) if r[7] else None
            vcpu = int(float(r[6])) if r[6] is not None else None
            memoria_ram_gb = int(mem_match.group(1)) if mem_match else None
            espacio_disco_gb = int(float(r[8])) if r[8] is not None else None
            sistema_operativo = clean_str(r[5], 50)

            obj = await upsert(
                session,
                Servidor,
                {"hostname": hostname},
                {
                    "ip_publica": ip_publica,
                    "ip_privada": ip_privada,
                    "vcpu": vcpu,
                    "memoria_ram_gb": memoria_ram_gb,
                    "espacio_disco_gb": espacio_disco_gb,
                    "sistema_operativo": sistema_operativo,
                    "estado_servidor": "Activo",
                },
            )
            servidor_by_hostname[hostname] = obj.id_servidor
            if ip_publica:
                servidor_by_ip[ip_publica] = obj.id_servidor

        # --- Step 5: "Externo" servidores for orphan IPs ----------
        orphan_ips = []
        seen_orphan = set()
        for r in resp_rows:
            ip = clean_str(r[7])
            if ip and ip not in servidor_by_ip and ip not in seen_orphan:
                seen_orphan.add(ip)
                orphan_ips.append(ip)

        for ip in orphan_ips:
            obj = await upsert(
                session,
                Servidor,
                {"hostname": "Externo", "ip_privada": ip},
                {
                    "ip_publica": ip,
                    "vcpu": None,
                    "memoria_ram_gb": None,
                    "espacio_disco_gb": None,
                    "sistema_operativo": None,
                    "estado_servidor": "Desconocido",
                },
            )
            servidor_by_ip[ip] = obj.id_servidor

        # --- Step 6: responsable (2026 overrides 2025, dedupe, placeholders) --
        responsable_id_by_row: list[int] = []
        placeholder_emails = []
        for r in resp_rows:
            fac = clean_str(r[3])
            nombre = clean_str(r[14], 150) or clean_str(r[10], 150)
            telefono = clean_phone(r[15]) or clean_phone(r[11])
            telefono = telefono[:20] if telefono else None
            correo = clean_str(r[16], 100) or clean_str(r[12], 100)

            if correo:
                dedupe_key = correo.strip().lower()
            elif nombre:
                dedupe_key = normalize_name_key(nombre)
            else:
                dedupe_key = f"sin-responsable-{slug(fac or 'desconocido')}"
                nombre = f"Responsable no asignado - {fac or 'Desconocido'}"

            correo_final = (correo or f"sin-correo+{slug(dedupe_key)}@umsa.bo")[:100]
            if not correo:
                placeholder_emails.append((r[0], nombre, fac))

            obj = await upsert(
                session,
                Responsable,
                {"correo_institucional": correo_final},
                {"nombre_completo": nombre, "telefono": telefono},
            )
            responsable_id_by_row.append(obj.id_responsable)

        # --- Step 7: plataforma (dedupe by url_principal, keep first) --------
        seen_urls: dict[str, int] = {}  # normalized url -> id_plataforma
        dup_report = []
        created_count = 0
        for idx, r in enumerate(resp_rows):
            fac = clean_str(r[3])
            tipo_raw = clean_str(r[4])
            carrera = clean_str(r[5])
            link = clean_str(r[6])
            ip = clean_str(r[7])
            if not link or not fac:
                continue

            url_key = normalize_url_key(link)
            if url_key in seen_urls:
                dup_report.append((r[0], fac, tipo_raw, carrera, link))
                continue

            if tipo_raw is None or tipo_raw == "Facultad":
                id_unidad = facultad_id[fac]
            else:
                id_unidad = child_id[(fac, carrera or tipo_raw)]

            id_servidor = servidor_by_ip.get(ip)
            if id_servidor is None:
                # Si no hay IP en el excel, omitimos o asignamos nulo (aunque la BD requiere un servidor).
                # Para evitar fallos, saltamos la fila si no se puede resolver un servidor válido.
                dup_report.append((r[0], fac, tipo_raw, carrera, f"{link} (sin IP / servidor)"))
                continue

            nombre_plataforma = f"{fac} - {carrera or tipo_raw}" if tipo_raw and tipo_raw != "Facultad" else fac
            nombre_plataforma = nombre_plataforma[:150]
            id_tipo_plataforma = tipo_plataforma_id[RAW_TO_TIPO_PLATAFORMA[tipo_raw]]

            obj = await upsert(
                session,
                Plataforma,
                {"url_principal": normalize_url(link)[:255]},
                {
                    "id_unidad": id_unidad,
                    "id_servidor": id_servidor,
                    "id_responsable": responsable_id_by_row[idx],
                    "id_tipo_plataforma": id_tipo_plataforma,
                    "nombre_plataforma": nombre_plataforma,
                },
            )
            seen_urls[url_key] = obj.id_plataforma
            created_count += 1

        await session.commit()

    # --- Validation summary (step 8) ----------------------------------------
    print("\n=== Resumen de siembra ===")
    print(f"Tipos de unidad academica: {len(tipo_unidad_id)}")
    print(f"Tipos de plataforma: {len(tipo_plataforma_id)}")
    print(f"Unidades academicas (facultades): {len(facultad_id)}")
    print(f"Unidades academicas (hijas): {len(child_id)}")
    print(f"Servidores del inventario: {len(servidor_by_hostname)}")
    print(f"Servidores externos creados desde IPs huérfanas: {len(orphan_ips)}")
    if skipped_servers:
        print(f"Servidores omitidos (sin IP publica ni privada): {skipped_servers}")
    print(f"Responsables (filas procesadas, tras deduplicar por correo/nombre): {len(set(responsable_id_by_row))}")
    if placeholder_emails:
        print(f"\nResponsables sin correo real ({len(placeholder_emails)} filas) - completar manualmente desde el dashboard:")
        for row_id, nombre, fac in placeholder_emails:
            print(f"  - fila {row_id}: {nombre} ({fac})")
    print(f"\nPlataformas creadas/actualizadas: {created_count}")
    if dup_report:
        print(f"\nFilas omitidas por URL duplicada o sin servidor resoluble ({len(dup_report)}) - reconciliar manualmente:")
        for row_id, fac, tipo_raw, carrera, link in dup_report:
            print(f"  - fila {row_id}: {fac} / {tipo_raw} / {carrera} -> {link}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        default="../../Moddles de toda la Universidad Codigo.xlsx",
        help="Ruta al Excel maestro (relativa a backend/scripts o absoluta)",
    )
    args = parser.parse_args()
    xlsx_path = (Path(__file__).resolve().parent / args.file).resolve()
    if not xlsx_path.exists():
        xlsx_path = Path(args.file).resolve()
    if not xlsx_path.exists():
        raise SystemExit(f"No se encontro el archivo: {args.file}")
    asyncio.run(seed(xlsx_path))


if __name__ == "__main__":
    main()
