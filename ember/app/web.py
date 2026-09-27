"""HTTP routes for the dashboard and the read-only JSON API."""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import mock
from .db import utcnow
from .paths import WEB_DIR
from .security import ingress_base_href
from .state import AppState
from .version import app_version

router = APIRouter()

_INDEX_TEMPLATE = (WEB_DIR / "index.html").read_text(encoding="utf-8")


def _state(request: Request) -> AppState:
    return request.app.state.ember


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> HTMLResponse:
    base = ingress_base_href(request.headers.get("x-ingress-path"))
    page = _INDEX_TEMPLATE.replace("__BASE_HREF__", html.escape(base, quote=True)).replace(
        "__VERSION__", html.escape(app_version(), quote=True)
    )
    return HTMLResponse(page)


@router.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    state = _state(request)
    return {"status": "ok", "version": app_version(), "database": "ok" if state.db_error is None else "error"}


@router.get("/api/dashboard")
def dashboard(request: Request, scenario: str = Query("alive", max_length=20)) -> dict[str, Any]:
    state = _state(request)
    settings = state.loaded.settings
    payload = mock.dashboard(settings.agent_name, settings.daily_spend_cap_usd, settings.cycle_spend_cap_usd, scenario)
    payload["generated_at"] = utcnow()
    payload["system"] = state.system_info()
    payload["events"] = state.recent_events()
    return payload


@router.get("/api/sensors")
def sensors(request: Request) -> JSONResponse:
    """Small JSON document for a Home Assistant REST sensor (see README)."""
    state = _state(request)
    data = mock.sensors()
    data["name"] = state.loaded.settings.agent_name
    data["dry_run"] = state.loaded.settings.dry_run
    data["updated_at"] = utcnow()
    return JSONResponse(data)


@router.get("/api/events")
def events(request: Request, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    return _state(request).recent_events(limit=limit)
