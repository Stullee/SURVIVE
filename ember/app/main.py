"""FastAPI application: dashboard, JSON API, and startup/shutdown.

Startup never fails because of bad options or a broken database: the problem
is logged, shown in the dashboard, and the web UI keeps running so the owner
can see what is wrong.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db as dbmod
from . import events, paths
from .agent.owner import apply_kill_switch_reset
from .agent.scheduler import Scheduler
from .agent.service import Agent
from .config import LoadedSettings, load_settings
from .economy.metering import ProcessLock, lock_path
from .economy.service import Economy
from .logging_setup import printable, redact, register_secret, setup_logging
from .security import AccessPolicy, ASGIApp, Message, Receive, Scope, SecurityMiddleware, Send
from .state import AppState
from .version import app_version
from .web import router

log = logging.getLogger(__name__)


def dev_mode_enabled() -> bool:
    """Local development outside Home Assistant (docker-compose): no Ingress IP filter."""
    return os.environ.get("EMBER_DEV_MODE", "").strip().lower() in {"1", "true", "yes"}


class CatchAllMiddleware:
    """Turn any unhandled error into a logged 500 response.

    It sits inside SecurityMiddleware, so error responses get the same security
    headers as every other response, and the error is logged exactly once.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            log.exception("Unhandled error on %s %s", scope["method"], printable(scope["path"], 200))
            if started:
                raise
            body = b'{"error":"internal error, see the system log"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})


def create_app(loaded: LoadedSettings | None = None, *, dev_mode: bool | None = None) -> FastAPI:
    loaded = loaded or load_settings()
    dev_mode = dev_mode_enabled() if dev_mode is None else dev_mode
    register_secret(loaded.settings.anthropic_api_key.get_secret_value())
    register_secret(loaded.settings.email_password.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Database work never runs on the event loop (see db._refuse_on_event_loop).
        state = await asyncio.to_thread(_start, loaded, dev_mode)
        app.state.ember = state
        if state.db_error is None:
            state.log_handler = events.install(state.db)
        if state.economy is not None:
            # One task re-evaluates the economy and wakes the agent (see agent/scheduler.py).
            state.scheduler = Scheduler(state.db, state.economy, state.agent)
            state.scheduler.start()
        try:
            yield
        finally:
            if state.scheduler is not None:
                await state.scheduler.stop()
            await asyncio.to_thread(_stop, state)

    # redirect_slashes=False: a redirect's absolute Location would leave the Ingress path.
    app = FastAPI(
        title="Ember", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan, redirect_slashes=False
    )
    # The middleware added last runs first: SecurityMiddleware wraps CatchAllMiddleware.
    app.add_middleware(CatchAllMiddleware)
    app.add_middleware(SecurityMiddleware, policy=AccessPolicy(dev_mode=dev_mode))
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=paths.WEB_DIR / "static"), name="static")
    return app


def _start(loaded: LoadedSettings, dev_mode: bool) -> AppState:
    database = dbmod.Database(paths.db_path())
    state = AppState(loaded=loaded, db=database, dev_mode=dev_mode)
    try:
        applied = dbmod.migrate(database.path)
        if applied:
            log.info("Database schema is now at version %d", max(applied))
        database.prune_events(events.KEEP_EVENTS)
        # Phase 1 stored the install time as "born_at"; a life's birth is in the lives table now.
        state.installed_at = database.set_meta_if_missing("born_at", state.started_at)
        database.set_meta("last_started_at", state.started_at)
    except Exception as exc:  # noqa: BLE001 - the dashboard must still come up
        # Anything from a missing file to a damaged page or a stuck lock ends up
        # here; the dashboard shows it and the agent stays stopped.
        state.db_error = redact(str(exc))
        log.error("Database unavailable: %s", exc)
        database.close()
        return state
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
    economy: Economy | None = None
    try:
        economy = Economy(database, loaded, lock=ProcessLock(lock_path(paths.data_dir())))
        economy.start()
        apply_kill_switch_reset(database, economy, loaded.settings.kill_switch_reset)
    except Exception as exc:  # noqa: BLE001 - the dashboard must still come up
        state.economy_error = redact(f"{type(exc).__name__}: {exc}")
        log.exception("The economy could not start; model calls are disabled")
        if economy is not None:
            economy.stop()
        return state
    state.economy = economy
    try:
        agent = Agent(database, loaded, economy)
        agent.recover()
        state.agent = agent
    except Exception as exc:  # noqa: BLE001 - the dashboard and the economy must still come up
        state.agent_error = redact(f"{type(exc).__name__}: {exc}")
        log.exception("The agent could not start; no wake cycles will run")
    return state


def _stop(state: AppState) -> None:
    if state.economy is not None:
        state.economy.stop()
    if state.log_handler is not None:
        events.uninstall(state.log_handler)
        state.log_handler = None
    _record(state, "info", "system", "Stopped")
    if state.agent is not None and state.agent.running_cycle:
        # A cycle thread is still finishing; leave the connection to it. The next start
        # charges any unfinished call at its worst case.
        return
    state.db.close()


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
