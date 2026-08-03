"""In-memory admin session tokens. No user table — a single shared admin
password gates write/delete routes for the 2 DTIC admins (see plan)."""

import secrets
from datetime import datetime, timedelta

TOKEN_TTL = timedelta(hours=12)

_active_tokens: dict[str, datetime] = {}


def issue_token() -> str:
    token = secrets.token_urlsafe(32)
    _active_tokens[token] = datetime.utcnow() + TOKEN_TTL
    return token


def revoke_token(token: str) -> None:
    _active_tokens.pop(token, None)


def is_valid(token: str) -> bool:
    expires_at = _active_tokens.get(token)
    if expires_at is None:
        return False
    if expires_at < datetime.utcnow():
        _active_tokens.pop(token, None)
        return False
    return True
