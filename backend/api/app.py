"""Local FastAPI application (SPEC section 89).

Binds to loopback. There is no authentication because there is no network
surface: the API exists so the UI can talk to the pipeline on the same machine
(§50, §51).

Routers are thin -- they validate input, call a service and shape the response.
Business rules live in the services.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.dependencies import AppState, build_state
from backend.api.routers import (
    editing,
    files,
    health,
    interaction,
    jobs,
    media,
    profiles,
    projects,
)
from backend.config.schema import AppConfig
from backend.core.errors import ErrorCode, GamingEditorError, http_status_for
from backend.core.logging import LogChannel, get_logger
from backend.core.versions import APPLICATION_VERSION

logger = get_logger("api.app", LogChannel.APPLICATION)

API_PREFIX = "/api"


def create_app(
    *,
    config: AppConfig | None = None,
    config_dir: Path | None = None,
    data_root: Path | None = None,
    state: AppState | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        config, config_dir, data_root: forwarded to :func:`build_state`.
        state: a pre-built state, used by tests to inject temporary storage.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = state or build_state(
            config=config, config_dir=config_dir, data_root=data_root
        )
        app.state.services = resolved
        # A crash leaves jobs marked RUNNING; nothing is executing now, so
        # re-queue them and resume from where the pipeline stopped (§47).
        recovered = resolved.jobs.recover()
        logger.info(
            "API started",
            extra={
                "version": APPLICATION_VERSION,
                "data_root": str(resolved.paths.data_root),
                "recovered_jobs": len(recovered),
            },
        )
        try:
            yield
        finally:
            resolved.close()
            logger.info("API stopped")

    app = FastAPI(
        title="AI Gaming Video Editor",
        version=APPLICATION_VERSION,
        description=(
            "Local API for the gaming video editor. Turns long gameplay "
            "recordings into 10-60 minute YouTube videos, entirely on this machine."
        ),
        lifespan=lifespan,
    )

    origins = (
        state.config.application.api.cors_origins
        if state is not None
        else (config.application.api.cors_origins if config is not None else [])
    )
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(GamingEditorError)
    async def handle_domain_error(
        _request: Request, exc: GamingEditorError
    ) -> JSONResponse:
        """Return the typed error code so the UI can react to it (§81)."""
        status = http_status_for(exc.code)
        if status >= 500:
            logger.error(
                exc.message,
                extra={"error_code": exc.code, **exc.details},
                exc_info=True,
            )
        else:
            logger.warning(exc.message, extra={"error_code": exc.code})
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Give schema rejections the same typed shape as domain errors.

        FastAPI's default body is ``{"detail": [...]}``, which the UI would have
        to special-case. Every failure from this API carries an ``error_code``
        instead, and a violated duration bound reports the specific code rather
        than a generic one.
        """
        errors = exc.errors()
        code = (
            ErrorCode.INVALID_TARGET_DURATION
            if any("target_duration_seconds" in str(error.get("loc", ())) for error in errors)
            else ErrorCode.SCHEMA_VALIDATION_FAILED
        )
        return JSONResponse(
            status_code=http_status_for(code),
            content={
                "error_code": code.value,
                "message": "Request failed validation.",
                "details": {"errors": _validation_details(errors)},
                "recoverable": False,
            },
        )

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(projects.router, prefix=API_PREFIX)
    app.include_router(media.router, prefix=API_PREFIX)
    app.include_router(jobs.router, prefix=API_PREFIX)
    app.include_router(editing.router, prefix=API_PREFIX)
    app.include_router(files.router, prefix=API_PREFIX)
    app.include_router(interaction.router, prefix=API_PREFIX)
    app.include_router(interaction.presets_router, prefix=API_PREFIX)
    app.include_router(profiles.router, prefix=API_PREFIX)

    return app


def _validation_details(errors: list[dict]) -> list[dict[str, str]]:
    """Reduce Pydantic's error list to what the UI can show a user."""
    return [
        {
            "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
            "message": str(error.get("msg", "")),
        }
        for error in errors
    ]


__all__ = ["API_PREFIX", "create_app"]
