from fastapi import Header, HTTPException, status

from app import auth_state


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Dependency for mutating routes. Expects 'Authorization: Bearer <token>'
    issued by POST /api/auth/login. Read (GET) routes never use this."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesion admin requerida")
    token = authorization.removeprefix("Bearer ").strip()
    if not auth_state.is_valid(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesion admin invalida o expirada")
