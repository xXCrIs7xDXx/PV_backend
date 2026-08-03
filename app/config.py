from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    admin_password: str
    monitor_interval_hours: float = 10.0
    monitor_timeout_seconds: float = 30.0
    monitor_max_concurrency: int = 15
    # Cuantos ciclos consecutivos de fallo se requieren antes de marcar una
    # plataforma como caida y enviar la notificacion de Telegram. Un valor de
    # 2 elimina los falsos positivos por timeouts transitorios sin demorar
    # demasiado la deteccion real de una caida.
    monitor_confirm_failures: int = 2
    auto_close_tickets: bool = True
    cors_origins: str = "http://localhost:5173"

    # Off by default: each successful POST writes a permanent row into the
    # university's real audit spreadsheets. Flip to true only when ready -
    # the next monitoring cycle (scheduled or "Verificar ahora") will then
    # submit live results for every platform. See forms_service.py.
    google_forms_enabled: bool = False
    google_forms_timeout_seconds: float = 8.0
    google_forms_max_concurrency: int = 10

    # Off by default - see app/services/telegram_service.py. Requires a bot
    # token from @BotFather and the chat id that should receive alerts.
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # Cuantos meses de registro_monitoreo se conservan antes de purgarlos
    # (ver app/services/retencion_service.py). ticket_incidencia nunca se
    # purga - es la bitacora de incidencias, mucho mas liviana.
    retencion_registros_meses: int = 12

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
