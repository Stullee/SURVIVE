"""HTTP routes: the dashboard, its JSON API and the owner's actions.

Routes are plain functions (FastAPI runs them in a worker thread), because the
database layer is synchronous and must never run on the event loop.
"""

from __future__ import annotations

import contextlib
import html
from typing import Annotated, Any

from fastapi import APIRouter, Body, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import diagnostics
from .db import utcnow
from .economy.ledger import OWNER_KINDS
from .economy.service import Economy, Reply
from .logging_setup import printable
from .paths import WEB_DIR
from .security import ingress_base_href
from .state import AppState
from .version import app_version, build_id

router = APIRouter()

_INDEX_TEMPLATE = (WEB_DIR / "index.html").read_text(encoding="utf-8")

# What arrives with a later phase: the owner's buttons on approvals, inbox and upgrades (phase 4).
COMING_IN_PHASE = {"owner_actions": 4}
UNAVAILABLE = JSONResponse({"error": "the economy is not available, see the system log"}, status_code=503)


def _state(request: Request) -> AppState:
    return request.app.state.ember


def _economy(request: Request) -> Economy | None:
    return _state(request).economy


def _owner(request: Request) -> str | None:
    """The Home Assistant user behind an Ingress request, as the Supervisor's proxy reports it.

    Only a label for the audit trail, never an authorization: the headers can be
    forged by any Home Assistant user. They arrive as UTF-8 bytes that the server
    decodes as Latin-1, so non-ASCII names are decoded again here.
    """
    name = request.headers.get("x-remote-user-display-name") or request.headers.get("x-remote-user-name")
    if not name:
        return None
    with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
        name = name.encode("latin-1").decode("utf-8")
    return printable(name, 60)


def _reply(reply: Reply) -> JSONResponse:
    return JSONResponse(reply.body, status_code=reply.status)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> HTMLResponse:
    base = ingress_base_href(request.headers.get("x-ingress-path"))
    page = (
        _INDEX_TEMPLATE.replace("__BASE_HREF__", html.escape(base, quote=True))
        .replace("__VERSION__", html.escape(app_version(), quote=True))
        .replace("__BUILD__", html.escape(build_id(), quote=True))
    )
    return HTMLResponse(page)


@router.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    state = _state(request)
    return {"status": "ok", "version": app_version(), "database": "ok" if state.db_error is None else "error"}


@router.get("/api/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    state = _state(request)
    economy = state.economy
    payload: dict[str, Any] = {
        "generated_at": utcnow(),
        "mode": "dry_run" if state.loaded.settings.dry_run else "live",
        "coming_in_phase": COMING_IN_PHASE,
        "now": None,
        "projects": [],
        "activity": [],
        "approvals": [],
        "inbox": [],
        "upgrades": [],
        "mind": None,
    }
    if economy is not None:
        payload.update(economy.dashboard())
        agent = state.agent
        if agent is not None and payload.get("agent"):
            parts = agent.dashboard()
            will = parts.pop("last_will")
            payload.update(parts)
            payload["agent"].update(agent.agent_fields())
            payload["agent"]["cycle_running"] = agent.running_cycle or bool(parts["now"] and parts["now"]["running"])
            if payload.get("memorial"):
                payload["memorial"]["last_will"] = will["text"] if will else None
                payload["memorial"]["last_will_cut_off"] = bool(will and will["cut_off"])
        elif payload.get("agent"):
            payload["agent"].update(
                {
                    "next_wake_at": None,
                    "next_wake_reason": None,
                    "can_wake": False,
                    "cycles_enabled": False,
                    "wake_blocked_reason": f"The agent could not start: {state.agent_error}",
                }
            )
    else:
        payload.update(
            {"agent": None, "economy": None, "ledger": None, "memorial": None, "lives": [], "transitions": []}
        )
    payload["system"] = state.system_info()
    payload["events"] = state.recent_events()
    return payload


@router.get("/api/sensors")
def sensors(request: Request) -> JSONResponse:
    """Small JSON document for a Home Assistant REST sensor (see DOCS.md)."""
    state = _state(request)
    economy = state.economy
    data: dict[str, Any]
    if economy is None:
        data = {
            "name": state.loaded.settings.agent_name,
            "state": "unknown",
            "mode": "dry_run" if state.loaded.settings.dry_run else "live",
            "safe_mode": state.loaded.safe_mode,
            "balance_usd": None,
            "runway_days": None,
            "runway_known": False,
            "today_api_spend_usd": None,
            "daily_cap_usd": state.loaded.settings.daily_spend_cap_usd,
        }
    else:
        data = economy.sensors()
    data["dry_run"] = data["mode"] == "dry_run"  # kept from 0.1.x for existing sensor setups
    data["updated_at"] = utcnow()
    return JSONResponse(data)


@router.get("/api/events")
def events(request: Request, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    return _state(request).recent_events(limit=limit)


@router.get("/api/ledger")
def ledger(request: Request, limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
    economy = _economy(request)
    if economy is None:
        return UNAVAILABLE
    return JSONResponse({"mode": economy.mode, "entries": economy.books.entries(economy.life.scope(), limit)})


@router.post("/api/ledger/{kind}")
def add_entry(
    request: Request, kind: Annotated[str, Path(max_length=20)], body: Annotated[Any, Body()] = None
) -> JSONResponse:
    economy = _economy(request)
    if economy is None:
        return UNAVAILABLE
    if kind not in OWNER_KINDS:
        return JSONResponse({"error": "unknown kind of entry", "field": "kind"}, status_code=404)
    reply = economy.record(kind, body, _owner(request))
    if reply.status == 201:
        _poke(request)  # money in may revive the agent or end its starvation
    return _reply(reply)


@router.post("/api/ledger/{entry_id}/correct")
def correct_entry(
    request: Request, entry_id: Annotated[int, Path(ge=1, le=2**62)], body: Annotated[Any, Body()] = None
) -> JSONResponse:
    economy = _economy(request)
    if economy is None:
        return UNAVAILABLE
    return _reply(economy.correct(entry_id, body, _owner(request)))


@router.post("/api/control/pause")
def pause(request: Request) -> JSONResponse:
    return _control(request, paused=True)


@router.post("/api/control/resume")
def resume(request: Request) -> JSONResponse:
    return _control(request, paused=False)


def _control(request: Request, paused: bool) -> JSONResponse:
    economy = _economy(request)
    if economy is None:
        return UNAVAILABLE
    status = economy.set_paused(paused, _owner(request))
    _poke(request)
    return JSONResponse({"state": status.state, "paused": paused})


def _poke(request: Request) -> None:
    scheduler = _state(request).scheduler
    if scheduler is not None:
        scheduler.poke()


@router.post("/api/control/wake")
def wake(request: Request) -> JSONResponse:
    state = _state(request)
    if state.agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    status, body = state.agent.request_wake()
    if status == 202 and state.scheduler is not None:
        state.scheduler.poke()
    return JSONResponse(body, status_code=status)


@router.get("/api/cycles/{cycle_id}")
def cycle(request: Request, cycle_id: Annotated[int, Path(ge=1, le=2**62)]) -> JSONResponse:
    agent = _state(request).agent
    detail = agent.cycle_detail(cycle_id) if agent is not None else None
    if detail is None:
        return JSONResponse({"error": "no such wake cycle"}, status_code=404)
    return JSONResponse(detail)


@router.get("/api/diagnostics")
def diagnostics_report(request: Request) -> PlainTextResponse:
    """A text report of the whole system for troubleshooting (never contains secrets)."""
    return PlainTextResponse(diagnostics.report(_state(request)))
