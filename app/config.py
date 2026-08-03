from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    admin_password: str

    @field_validator("database_url", mode="after")
    @classmethod
    def clean_database_url(cls, v: str) -> str:
        if not v:
            return v
        url = v.strip()
        # Asegura el scheme adecuado para asyncpg
        for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://", "postgresql+psycopg2://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix):]
                break

        # Filtra parametros no soportados por asyncpg (como channel_binding)
        parts = urlsplit(url)
        if parts.query:
            query_params = parse_qsl(parts.query, keep_blank_values=True)
            filtered: list[tuple[str, str]] = []
            for key, val in query_params:
                k_lower = key.lower()
                if k_lower in ("channel_binding",):
                    continue
                if k_lower == "sslmode":
                    filtered.append(("ssl", val))
                else:
                    filtered.append((key, val))
            new_query = urlencode(filtered)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

        return url
    monitor_interval_hours: float = 10.0
    monitor_timeout_seconds: float = 30.0
    monitor_max_concurrency: int = 15
    # Cuantos ciclos consecutivos de fallo se requieren antes de marcar una
    # plataforma como caida y enviar la notificacion de Telegram. Un valor de
    # 2 elimina los falsos positivos por timeouts transitorios sin demorar
    # demasiado la deteccion real de una caida.
    monitor_confirm_failures: int = 2
    auto_close_tickets: bool = True
    cors_origins: str = "*"

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
        if not self.cors_origins or self.cors_origins.strip() == "*":
            return ["*"]
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return origins if origins else ["*"]


settings = Settings()
