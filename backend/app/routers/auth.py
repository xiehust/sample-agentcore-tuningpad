from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from ..core.auth import COOKIE_NAME, check_password, is_authenticated, issue_token
from ..core.config import get_settings
from ..core.errors import Unauthorized

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str


@router.get("/status")
def status(request: Request):
    s = get_settings()
    return {"auth_required": s.auth_required, "authenticated": is_authenticated(request)}


@router.post("/login")
def login(body: LoginBody, response: Response):
    s = get_settings()
    if not s.auth_required:
        return {"ok": True}
    if not check_password(s, body.password):
        raise Unauthorized("auth.invalid_password", "invalid password")
    response.set_cookie(
        COOKIE_NAME,
        issue_token(s),
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
