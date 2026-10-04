"""Single-operator auth.

* `TUNINGPAD_PASSWORD` set  → signed session cookie required on every /api route
  except the auth endpoints and health.
* no password               → login disabled, but only loopback clients are served
  (the platform holds powerful AWS credentials; it must never be open on a LAN).
"""

from __future__ import annotations

import hmac
import ipaddress

from fastapi import Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings, get_settings

COOKIE_NAME = "tuningpad_session"
OPERATOR = "operator"
PUBLIC_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret, salt="tuningpad-session")


def issue_token(settings: Settings) -> str:
    return _serializer(settings).dumps({"u": OPERATOR})


def verify_token(settings: Settings, token: str | None) -> bool:
    if not token:
        return False
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_ttl_hours * 3600)
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and data.get("u") == OPERATOR


def check_password(settings: Settings, candidate: str) -> bool:
    return bool(settings.password) and hmac.compare_digest(
        settings.password.encode(), candidate.encode()
    )


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_authenticated(request: Request) -> bool:
    settings = get_settings()
    if not settings.auth_required:
        return True
    return verify_token(settings, request.cookies.get(COOKIE_NAME))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        settings = get_settings()
        if not settings.auth_required:
            client = request.client.host if request.client else None
            if not is_loopback(client):
                return JSONResponse(
                    status_code=403,
                    content={
                        "code": "auth.loopback_only",
                        "message": "login is disabled; only loopback clients are served "
                        "(set TUNINGPAD_PASSWORD to enable remote access)",
                        "detail": None,
                    },
                )
            return await call_next(request)
        if path in PUBLIC_PATHS or is_authenticated(request):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"code": "auth.required", "message": "sign in required", "detail": None},
        )


def validate_startup(settings: Settings) -> None:
    """Refuse a prod start that would expose the console without a password."""
    if settings.run_mode == "prod" and not settings.password:
        raise RuntimeError("run_mode=prod requires TUNINGPAD_PASSWORD")
    if not settings.password and not is_loopback(settings.host):
        raise RuntimeError(
            f"binding {settings.host} without TUNINGPAD_PASSWORD is refused; "
            "set a password or bind 127.0.0.1"
        )
