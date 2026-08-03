from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from app import auth_state
from app.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    if body.password != settings.admin_password:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Contrasena incorrecta")
    return LoginResponse(token=auth_state.issue_token())


@router.post("/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    if authorization and authorization.startswith("Bearer "):
        auth_state.revoke_token(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}
