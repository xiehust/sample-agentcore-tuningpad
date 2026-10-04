from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.auth import AuthMiddleware, validate_startup
from .core.config import get_settings
from .core.db import init_db
from .core.errors import register_error_handlers
from .jobs.engine import get_engine
from .routers import auth, jobs

log = logging.getLogger("tuningpad")


def _load_modules() -> None:
    """Import pipeline/reconciler modules for their registration side effects."""
    from . import pipelines  # noqa: F401


def create_app(*, start_background: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        validate_startup(settings)
        init_db()
        _load_modules()
        if start_background:
            engine = get_engine()
            resumed = engine.resume_pending()
            if resumed:
                log.info("resumed %d unfinished jobs", len(resumed))
            engine.start_reconcilers()
        yield
        if start_background:
            get_engine().shutdown()

    app = FastAPI(
        title="TuningPad",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health():
        return {"ok": True}

    app.include_router(auth.router)
    app.include_router(jobs.router)
    from .routers import register_all

    register_all(app)
    return app


app = create_app()
