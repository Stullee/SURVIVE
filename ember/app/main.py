"""FastAPI application: dashboard, JSON API, and startup/shutdown.

Startup never fails because of bad options or a broken database: the problem
is logged, shown in the dashboard, and the web UI keeps running so the owner
can see what is wrong.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db as dbmod
from . import events, paths
from .config import LoadedSettings, load_settings
from .logging_setup import register_secret, setup_logging
from .security import AccessPolicy, SecurityMiddleware
from .state import AppState
from .version import app_version
from .web import router

log = logging.getLogger(__name__)


def dev_mode_enabled() -> bool:
    """Local development outside Home Assistant (docker-compose): no Ingress IP filter."""
    return os.environ.get("EMBER_DEV_MODE", "").strip().lower() in {"1", "true", "yes"}


def create_app(loaded: LoadedSettings | None = None, *, dev_mode: bool | None = None) -> FastAPI:
    loaded = loaded or load_settings()
    dev_mode = dev_mode_enabled() if dev_mode is None else dev_mode
    register_secret(loaded.settings.anthropic_api_key.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = _start(loaded, dev_mode)
        app.state.ember = state
        handler = events.install(state.db) if state.db_error is None else None
        try:
            yield
        finally:
            if handler is not None:
                events.uninstall(handler)
            _record(state, "info", "system", "Stopped")

    app = FastAPI(title="Ember", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SecurityMiddleware, policy=AccessPolicy(dev_mode=dev_mode))
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=paths.WEB_DIR / "static"), name="static")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"error": "internal error, see the system log"}, status_code=500)

    return app


def _start(loaded: LoadedSettings, dev_mode: bool) -> AppState:
    database = dbmod.Database(paths.db_path())
    state = AppState(loaded=loaded, db=database, dev_mode=dev_mode)
    try:
        applied = dbmod.migrate(database.path)
        if applied:
            log.info("Database schema is now at version %d", max(applied))
    except Exception as exc:  # noqa: BLE001 - the dashboard must still come up
        state.db_error = str(exc)
        log.error("Database unavailable: %s", exc)
        return state
    state.born_at = database.set_meta_if_missing("born_at", state.started_at)
    database.set_meta("last_started_at", state.started_at)
    _record(
        state,
        "info",
        "system",
        f"Started version {app_version()}",
        {"dry_run": loaded.settings.dry_run, "dev_mode": dev_mode, "config": loaded.source},
    )
    for error in loaded.errors:
        _record(state, "error", "config", f"Invalid option: {error}")
    if loaded.safe_mode:
        _record(state, "warning", "config", "Safe mode: built-in defaults are used and dry-run is forced on.")
    return state


def _record(state: AppState, level: str, kind: str, message: str, details: dict | None = None) -> None:
    if state.db_error is not None:
        return
    try:
        state.db.add_event(level, kind, message, details)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record event: %s", exc)


def app_factory() -> FastAPI:
    """Build the app from the environment (``uvicorn --factory app.main:app_factory``)."""
    loaded = load_settings()
    setup_logging(loaded.settings.log_level)
    return create_app(loaded)
