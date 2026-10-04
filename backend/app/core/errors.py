"""Uniform error envelope: every failure leaves the API as {code, message, detail}.

`code` is a stable dotted identifier the console localizes (`apiErrors.<code>`);
`message` is operator-facing English; `detail` carries structured context.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    status: int = 400

    def __init__(self, code: str, message: str, *, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        if status is not None:
            self.status = status


class NotFound(AppError):
    status = 404


class Conflict(AppError):
    status = 409


class Unauthorized(AppError):
    status = 401


def _envelope(status: int, code: str, message: str, detail: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"code": code, "message": message, "detail": detail}
    )


def aws_error_detail(err: ClientError) -> dict[str, Any]:
    e = err.response.get("Error", {})
    return {
        "aws_code": e.get("Code"),
        "aws_message": e.get("Message"),
        "operation": err.operation_name,
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _envelope(exc.status, exc.code, exc.message, exc.detail)

    @app.exception_handler(ClientError)
    async def _aws_error(_: Request, exc: ClientError):
        d = aws_error_detail(exc)
        status = 403 if d["aws_code"] in {"AccessDenied", "AccessDeniedException"} else 502
        return _envelope(status, "aws.client_error", f"{d['operation']}: {d['aws_message']}", d)

    @app.exception_handler(BotoCoreError)
    async def _boto_error(_: Request, exc: BotoCoreError):
        return _envelope(502, "aws.unavailable", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        from fastapi.encoders import jsonable_encoder

        # errors() may carry the raised exception in `ctx` (model validators): stringify it
        detail = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        return _envelope(422, "request.invalid", "request validation failed", detail)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return _envelope(exc.status_code, f"http.{exc.status_code}", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return _envelope(500, "internal", "internal error", {"type": type(exc).__name__})
