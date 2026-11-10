"""Tokens de sesion admin. No hay tabla de usuarios: una sola contrasena
compartida (ADMIN_PASSWORD) protege las rutas que modifican datos.

Los tokens son firmados (HMAC) en lugar de guardarse en memoria: Render
gratuito reinicia/duerme el servicio con frecuencia y antes cada reinicio
cerraba la sesion de los admins. La clave se deriva de ADMIN_PASSWORD, asi
que cambiar la contrasena invalida todas las sesiones abiertas."""

import base64
import hashlib
import hmac
import secrets
import time

from app.config import settings

TOKEN_TTL_SECONDS = 12 * 60 * 60

# Logout: tokens revocados hasta que expiren (best-effort; tras un reinicio
# un token cerrado seguiria valido hasta su vencimiento, como maximo 12 h).
_revocados: dict[str, float] = {}


def _clave() -> bytes:
    return hashlib.sha256(f"pv-admin-token:{settings.admin_password}".encode()).digest()


def _firma(payload: str) -> str:
    digest = hmac.new(_clave(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_token() -> str:
    payload = f"{int(time.time()) + TOKEN_TTL_SECONDS}.{secrets.token_urlsafe(9)}"
    return f"{payload}.{_firma(payload)}"


def revoke_token(token: str) -> None:
    if is_valid(token):
        _revocados[token] = float(token.split(".", 1)[0])


def is_valid(token: str) -> bool:
    try:
        expira, nonce, firma = token.split(".")
        vence = int(expira)
    except ValueError:
        return False
    if not hmac.compare_digest(firma, _firma(f"{expira}.{nonce}")):
        return False
    ahora = time.time()
    for t, v in list(_revocados.items()):
        if v < ahora:
            _revocados.pop(t, None)
    return vence >= ahora and token not in _revocados
