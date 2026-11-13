"""Configuracion del agente de respaldos (variables BACKUPS_* del .env).

Solo la usa el agente local en Windows; el backend en Render no necesita
ninguna de estas variables."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PREFIJO = "\\\\?\\"


def ruta_extendida(p: Path) -> Path:
    """En Windows antepone \\\\?\\ para superar el limite de 260 caracteres
    (los .mbz de Moodle tienen nombres muy largos). Probado tambien sobre la
    unidad virtual G: de Drive para escritorio."""
    if sys.platform != "win32":
        return p
    s = str(p)
    if s.startswith(_PREFIJO):
        return p
    s = os.path.abspath(s)
    if s.startswith("\\\\"):  # ruta de red \\servidor\recurso
        return Path(_PREFIJO + "UNC\\" + s[2:])
    return Path(_PREFIJO + s)


def ruta_visible(p: Path | str) -> str:
    """La ruta tal como la escribe una persona (sin el prefijo \\\\?\\)."""
    s = str(p)
    if s.startswith(_PREFIJO + "UNC\\"):
        return "\\\\" + s[len(_PREFIJO) + 4:]
    return s.removeprefix(_PREFIJO)


class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="BACKUPS_", extra="ignore"
    )

    # --- Origen ---------------------------------------------------------------
    # Nombre de la carpeta compartida en Drive.
    carpeta_compartida: str = "Backupumsa2627"
    # Unidad de Google Drive para escritorio.
    unidad_drive: str = "G:"
    # Ruta ya resuelta (opcional). Vacio = descubrirla resolviendo el acceso
    # directo .lnk / .shortcut-targets-by-id. Siempre se verifica que se pueda leer.
    ruta_origen: str | None = None

    # --- Destino (siempre en el mismo volumen, para publicar con os.replace) --
    destino_raiz: Path = Path(r"D:\Backups")

    # --- Programacion ---------------------------------------------------------
    hora_diaria: str = "10:00"  # hora local de Windows
    # Si el PC estaba apagado a la hora programada, ejecutar al encender.
    recuperar_ejecucion_perdida: bool = True
    # URL publica del backend (https://<servicio>.onrender.com). Con ella el
    # agente late cada `intervalo_sondeo_segundos` contra el backend, que
    # responde desde memoria: la base (Neon) NO se despierta mientras no haya
    # nada que hacer, y Render se mantiene despierto mientras la PC este encendida.
    backend_url: str | None = None
    intervalo_sondeo_segundos: int = 60
    # Sin backend_url el agente tiene que preguntar a la base directamente;
    # se hace cada 15 min para no tener a Neon despierto todo el dia (los
    # botones del panel tardan hasta ese tiempo en atenderse).
    intervalo_sondeo_bd_segundos: int = 900

    # --- Hoja de calculo para compartir ---------------------------------------
    # Ruta de un .xlsx dentro de la unidad de Google Drive (p. ej.
    # G:\Mi unidad\Respaldos DTIC\Registro de respaldos.xlsx). Se regenera al
    # final de cada ejecucion; se abre con Google Sheets y se comparte por
    # enlace. Vacio = no se genera.
    hoja_ruta: str | None = None
    hoja_max_archivos: int = 20000  # filas de la pestana "Archivos copiados"

    # --- Servidores (vacio = todos) -------------------------------------------
    incluir: str = ""  # nombres separados por coma
    excluir: str = ""

    # --- Transferencia --------------------------------------------------------
    reintentos: int = 5
    espera_reintento_segundos: int = 30  # se duplica en cada intento (max 15 min)
    margen_minimo_libre_gb: float = 20.0
    # Limite de volumen por ejecucion (la primera trae ~150 GB de historico).
    # 0 = sin limite. Lo que no entra queda PENDIENTE para la siguiente.
    max_gb_por_ejecucion: float = 0.0
    # 0 = sin limite de ancho de banda
    limite_mbps: float = 0.0
    tamano_bloque_mb: int = 8
    # Volumen donde Drive para escritorio guarda su cache (los archivos en
    # streaming pasan por ahi). Vacio = %LOCALAPPDATA%\Google\DriveFS.
    ruta_cache_drive: str | None = None

    # --- Politica de finalizacion ---------------------------------------------
    # Minutos que deben pasar desde la ultima modificacion del respaldo en Drive.
    ventana_minutos_con_manifiesto: int = 30
    ventana_minutos_sin_manifiesto: int = 180
    # Segundos entre dos sondeos de tamano dentro de la misma ejecucion.
    segundos_entre_sondeos: int = 20
    # Las carpetas con estructura no reconocida se registran pero no se descargan.
    descargar_no_reconocidos: bool = False

    # --- Alertas --------------------------------------------------------------
    alertas_telegram: bool = True
    # Alertar si un servidor lleva mas de estas horas sin respaldo nuevo
    # (minimo; si el servidor respalda semanalmente se usa su propio ritmo).
    horas_sin_backup_alerta: int = 30
    # Se considera bloqueada una ejecucion sin latido por mas de estos minutos.
    minutos_ejecucion_bloqueada: int = 30

    @field_validator("destino_raiz", mode="before")
    @classmethod
    def _ruta(cls, v):
        return Path(str(v).strip()) if v else v

    # --- Derivados ------------------------------------------------------------
    @property
    def destino_backups(self) -> Path:
        return ruta_extendida(self.destino_raiz) / self.carpeta_compartida

    @property
    def destino_temporal(self) -> Path:
        return ruta_extendida(self.destino_raiz) / "_TEMPORAL"

    @property
    def destino_registros(self) -> Path:
        return ruta_extendida(self.destino_raiz) / "Registros"

    @property
    def lista_incluir(self) -> set[str]:
        return {s.strip() for s in self.incluir.split(",") if s.strip()}

    @property
    def lista_excluir(self) -> set[str]:
        return {s.strip() for s in self.excluir.split(",") if s.strip()}

    def para_panel(self) -> dict:
        """Configuracion efectiva que se muestra (solo lectura) en el panel."""
        return {
            "carpeta_compartida": self.carpeta_compartida,
            "unidad_drive": self.unidad_drive,
            "destino": ruta_visible(self.destino_backups),
            "temporales": ruta_visible(self.destino_temporal),
            "registros": ruta_visible(self.destino_registros),
            "hora_diaria": self.hora_diaria,
            "hoja_compartida": self.hoja_ruta or "(no configurada)",
            "enlace_backend": "latido HTTP (sin despertar la base)" if self.backend_url else
                              f"sondeo directo a la base cada {self.intervalo_sondeo_bd_segundos // 60} min",
            "incluir": sorted(self.lista_incluir),
            "excluir": sorted(self.lista_excluir),
            "reintentos": self.reintentos,
            "margen_minimo_libre_gb": self.margen_minimo_libre_gb,
            "max_gb_por_ejecucion": self.max_gb_por_ejecucion,
            "limite_mbps": self.limite_mbps,
            "ventana_minutos_con_manifiesto": self.ventana_minutos_con_manifiesto,
            "ventana_minutos_sin_manifiesto": self.ventana_minutos_sin_manifiesto,
            "descargar_no_reconocidos": self.descargar_no_reconocidos,
            "eliminar_del_origen": False,
            "sobrescribir_validados": False,
        }


backup_settings = BackupSettings()
