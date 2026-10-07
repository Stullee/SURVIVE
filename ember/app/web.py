"""HTTP routes: the dashboard, its JSON API and the owner's actions.

Routes are plain functions (FastAPI runs them in a worker thread), because the
database layer is synchronous and must never run on the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Body, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import diagnostics
from . import events as event_log
from .agent import owner as owner_side
from .agent.views import WorkspaceFileError
from .db import utcnow
from .economy.ledger import OWNER_KINDS
from .economy.life import KILLED_KEY
from .economy.service import Economy, Reply
from .integrations import executor as email_executor
from .integrations.etsy import EtsyError
from .integrations.pinterest import PinterestError
from .integrations.sftp import SftpError
from .logging_setup import printable
from .paths import WEB_DIR
from .products import blog, live, site
from .products.blog import BlogError
from .products.site import SiteError
from .security import USER_ID_HEADER, ingress_base_href
from .state import AppState
from .version import app_version, build_id

router = APIRouter()
# 0.26.0: the workspace's thumbnails are drawn here, one at a time (api/workspace/thumb).
_THUMBS_DRAWN = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thumbnails")
log = logging.getLogger(__name__)

_INDEX_TEMPLATE = (WEB_DIR / "index.html").read_text(encoding="utf-8")

# Sections still waiting for a later phase (none since 0.3.0; kept for pages loaded from older versions).
COMING_IN_PHASE: dict[str, int] = {}
UNAVAILABLE = JSONResponse({"error": "the economy is not available, see the system log"}, status_code=503)


def _state(request: Request) -> AppState:
    return request.app.state.ember


def _economy(request: Request) -> Economy | None:
    return _state(request).economy


def _owner(request: Request) -> str | None:
    """The Home Assistant user behind an Ingress request, for the audit trail: their name and their user ID.

    The Supervisor's proxy names the signed-in user in the first ``X-Remote-User-Id`` header, which a browser can't
    set: that ID is what counts (SecurityMiddleware checks it against owner_user_ids, 0.11.2). The display name is
    only a label, which users can change, so the ID stays next to it: "Stefan (8f14…)". The names arrive as UTF-8
    bytes that the server decodes as Latin-1, so non-ASCII names are decoded again here.
    """
    name = _display_name(request)
    user_id = _user_id(request)
    if user_id is None:
        return name
    shown = f"{name[: 57 - len(user_id)].rstrip()} ({user_id})" if name else user_id  # the columns hold 60
    return shown[:60]


def _display_name(request: Request) -> str | None:
    name = request.headers.get("x-remote-user-display-name") or request.headers.get("x-remote-user-name")
    if not name:
        return None
    with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
        name = name.encode("latin-1").decode("utf-8")
    return printable(name, 60)


def _user_id(request: Request) -> str | None:
    """The signed-in Home Assistant user's ID: the first X-Remote-User-Id header (the Supervisor's own)."""
    values = request.headers.getlist(USER_ID_HEADER)
    return printable(values[0].strip(), 40) if values and values[0].strip() else None


def _owner_status(request: Request) -> dict[str, Any]:
    """Who Ember answers (0.11.2): whether owner_user_ids names the owner, and who is asking, so the dashboard can
    say which ID to put there."""
    state = _state(request)
    return {
        "ids_set": bool(state.loaded.settings.owner_user_ids),
        "dev_mode": state.dev_mode,
        "user_id": _user_id(request),
        "user_name": _display_name(request),
    }


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
        "inbox_before": None,
        "promises_open": [],
        "upgrades": [],
        "instructions": None,
        "mind": None,
        "badges": None,
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
    payload["integrations"] = _integrations(state)
    payload["system"] = {**state.system_info(), "owner": _owner_status(request)}
    payload["events"] = state.recent_events()
    return payload


def _integrations(state: AppState) -> dict[str, Any]:
    """Ember's integrations as the dashboard shows them (never a password)."""
    if state.agent is not None:
        return state.agent.integrations()
    settings = state.loaded.settings
    email = email_executor.integration(None, None, settings, "dry_run" if settings.dry_run else "live", None, None)
    if email["status"] == "ok":  # it would work, but without the agent there is no mailbox
        email.update(status="error", reason="The agent is not running, see the system log")
    return {"email": email}


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
            "net_runway_days": None,
            "burn_mode": None,
            "today_api_spend_usd": None,
            "daily_cap_usd": state.loaded.settings.daily_spend_cap_usd,
        }
    else:
        data = economy.sensors()
        data["kill_switch_engaged"] = state.db.get_meta(KILLED_KEY) == "1"
        if state.agent is not None:
            data.update(state.agent.sensor_fields())
    data["dry_run"] = data["mode"] == "dry_run"  # kept from 0.1.x for existing sensor setups
    data["updated_at"] = utcnow()
    return JSONResponse(data)


@router.get("/api/events")
def events(request: Request, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    return _state(request).recent_events(limit=limit)


@router.get("/api/ledger")
def ledger(
    request: Request, limit: int = Query(50, ge=1, le=500), before: int | None = Query(None, ge=1)
) -> JSONResponse:
    """The ledger's entries, newest first; ``before``: those older than that entry (0.12.0: the tab's older rows)."""
    economy = _economy(request)
    if economy is None:
        return UNAVAILABLE
    entries = economy.books.entries(economy.life.scope(), limit, before)
    return JSONResponse({"mode": economy.mode, "entries": entries})


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


@router.post("/api/economy/estimates/reset")
def reset_estimates(request: Request) -> JSONResponse:
    """0.12.0: the owner puts every scaled-up cost estimate back to 1."""
    economy = _economy(request)
    if economy is None:
        return UNAVAILABLE
    reply = economy.reset_estimates(_owner(request))
    _poke(request)  # scheduled wakes held for want of room may run again
    return _reply(reply)


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


@router.post("/api/control/kill")
def kill(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    state = _state(request)
    if state.economy is None:
        return UNAVAILABLE
    reply = owner_side.kill(state.db, state.economy, state.loaded.settings.agent_name, body, _owner(request))
    _poke(request)
    return _reply(reply)


# --- Etsy: connecting the owner's shop (0.8.0) ---


@router.post("/api/etsy/connect")
def etsy_connect(request: Request) -> JSONResponse:
    """Start connecting: Etsy's page where the owner allows Ember's access (opened in the owner's browser)."""
    agent = _state(request).agent
    if agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    try:
        url = agent.etsy.start()
    except EtsyError as exc:
        return JSONResponse({"error": str(exc), "field": "etsy"}, status_code=422)
    event_log.record(_state(request).db, "info", "etsy", f"{_owner(request) or 'The owner'} started connecting Etsy")
    return JSONResponse({"authorize_url": url})


@router.post("/api/etsy/finish")
def etsy_finish(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    """Finish connecting with the address Etsy sent the owner to."""
    agent = _state(request).agent
    if agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    pasted = body.get("address") if isinstance(body, dict) else None
    if not isinstance(pasted, str) or not pasted.strip() or len(pasted) > 4_000:
        return JSONResponse({"error": "paste the address Etsy sent you to", "field": "address"}, status_code=422)
    try:
        info = agent.etsy.finish(pasted)
    except EtsyError as exc:
        return JSONResponse({"error": str(exc), "field": "address"}, status_code=422)
    event_log.record(_state(request).db, "info", "etsy", f"Connected the Etsy shop {info.name}")
    _poke(request)
    return JSONResponse({"shop_name": info.name, "currency": info.currency})


@router.post("/api/etsy/disconnect")
def etsy_disconnect(request: Request) -> JSONResponse:
    agent = _state(request).agent
    if agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    agent.etsy.disconnect()
    event_log.record(_state(request).db, "info", "etsy", f"{_owner(request) or 'The owner'} disconnected the Etsy shop")
    return JSONResponse({"disconnected": True})


# --- Pinterest: connecting the owner's account (0.13.0, Phase E2) ---


@router.post("/api/pinterest/connect")
def pinterest_connect(request: Request) -> JSONResponse:
    """Start connecting: Pinterest's page where the owner allows Ember's access (opened in the owner's browser)."""
    agent = _state(request).agent
    if agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    try:
        url = agent.pinterest.start()
    except PinterestError as exc:
        return JSONResponse({"error": str(exc), "field": "pinterest"}, status_code=422)
    who = _owner(request) or "The owner"
    event_log.record(_state(request).db, "info", "pinterest", f"{who} started connecting Pinterest")
    return JSONResponse({"authorize_url": url})


@router.post("/api/pinterest/finish")
def pinterest_finish(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    """Finish connecting with the address Pinterest sent the owner to."""
    agent = _state(request).agent
    if agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    pasted = body.get("address") if isinstance(body, dict) else None
    if not isinstance(pasted, str) or not pasted.strip() or len(pasted) > 4_000:
        return JSONResponse({"error": "paste the address Pinterest sent you to", "field": "address"}, status_code=422)
    try:
        info = agent.pinterest.finish(pasted)
    except PinterestError as exc:
        return JSONResponse({"error": str(exc), "field": "address"}, status_code=422)
    event_log.record(_state(request).db, "info", "pinterest", f"Connected the Pinterest account {info.username}")
    _poke(request)
    return JSONResponse({"username": info.username, "profile_url": info.url})


@router.post("/api/pinterest/disconnect")
def pinterest_disconnect(request: Request) -> JSONResponse:
    agent = _state(request).agent
    if agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    agent.pinterest.disconnect()
    who = _owner(request) or "The owner"
    event_log.record(_state(request).db, "info", "pinterest", f"{who} disconnected the Pinterest account")
    return JSONResponse({"disconnected": True})


def _owner_actions(request: Request) -> owner_side.Owner | None:
    state = _state(request)
    if state.economy is None or state.agent is None:
        return None
    return owner_side.Owner(
        state.db,
        state.economy.clock,
        state.economy,
        state.agent.scope(),
        state.loaded.settings.agent_name,
        unlocks_off=state.agent.unlocks_off(),  # 0.15.0: no unlock without owner_user_ids, or in safe mode
        ready=state.agent.channels_ready(),  # 0.15.0: a channel venture's first test waits for its channel
    )


NO_AGENT = JSONResponse({"error": "the agent is not available, see the system log"}, status_code=503)
ItemId = Annotated[int, Path(ge=1, le=2**62)]


@router.post("/api/approvals/{approval_id}/decide")
def decide_approval(request: Request, approval_id: ItemId, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.decide(approval_id, body, _owner(request))
    _poke_executor(request, reply)
    _wake_for_decision(request, reply)
    return _reply(reply)


@router.post("/api/approvals/{approval_id}/close")
def close_approval(request: Request, approval_id: ItemId, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.close(approval_id, body, _owner(request))
    _poke_executor(request, reply)
    _wake_for_decision(request, reply)
    return _reply(reply)


def _poke_executor(request: Request, reply: Reply) -> None:
    """An approved email is sent by the scheduler's next round: start it now rather than within the minute."""
    approval = reply.body.get("approval") if reply.status == 200 else None
    if isinstance(approval, dict) and approval.get("executor"):
        _poke(request)


@router.get("/api/inbox")
def inbox(request: Request, before: int = Query(ge=1, le=2**62), limit: int = Query(30, ge=1, le=200)) -> JSONResponse:
    """0.15.0: the Inbox's messages older than message ``before``, newest first (the dashboard brings the newest, and
    every message of the owner's still waiting for an answer)."""
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    return JSONResponse(agent.inbox_page(before, limit))


@router.post("/api/inbox")
def send_message(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.send_message(body, _owner(request))
    if reply.status == 201:
        reply.body["wake"] = _wake_for_message(request)
    return _reply(reply)


def _wake_for_message(request: Request) -> str | None:
    """The owner wrote: wake the agent to read it (the wake_on_message option). "now" if a wake is on its way (Wake
    now); "soon", or "after_cycle" while a cycle runs, if it follows (0.15.0: one cycle a few minutes after the owner's
    last message or decision, Agent.wake_for_message); None if none follows (the option is off, the agent is paused,
    ...): the message waits for the next wake."""
    state = _state(request)
    agent = state.agent
    if agent is None or not state.loaded.settings.wake_on_message:
        return None
    if agent.wake_requested:  # already woken and not started yet: that cycle reads the message
        return "now"
    wake = agent.wake_for_message()
    if wake in ("now", "soon") and state.scheduler is not None:
        state.scheduler.poke()  # decide again: sleep only until the owner has been quiet for a few minutes
    return wake


def _wake_for_decision(request: Request, reply: Any) -> None:
    """0.12.0: the owner decided on one of the agent's requests, ventures or milestones: wake it to act on it (the
    wake_on_decision option), like a message wakes it (the reply says when, as ``wake``: see ``_wake_for_message``)."""
    state = _state(request)
    agent = state.agent
    if reply.status == 200 and agent is not None:
        agent.lift_sleep_cut()  # 0.15.0: no request waits any more: the sleep the agent chose stands
    if reply.status != 200 or agent is None or not state.loaded.settings.wake_on_decision:
        return
    wake = agent.wake_for_decision()
    if wake in ("now", "soon") and state.scheduler is not None:
        state.scheduler.poke()
    if isinstance(reply.body, dict):
        reply.body["wake"] = wake


@router.post("/api/instructions")
def set_instructions(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.set_instructions(body, _owner(request)))


@router.post("/api/lessons/pins")
def pin_lesson(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    """0.12.0: the owner pins one of the agent's lessons."""
    actions, agent = _owner_actions(request), _state(request).agent
    if actions is None or agent is None:
        return NO_AGENT
    return _reply(actions.pin_lesson(body, _owner(request), agent.memory().read("lessons")))


@router.post("/api/lessons/pins/{pin_id}/unpin")
def unpin_lesson(request: Request, pin_id: ItemId) -> JSONResponse:
    """0.12.0: the owner unpins a lesson."""
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.unpin_lesson(pin_id, _owner(request)))


@router.post("/api/email/suppressions")
def suppress_email(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    """0.12.0: the owner adds an address Ember never emails."""
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.suppress_email(body, _owner(request)))


@router.post("/api/inbox/{message_id}/remove")
def remove_message(request: Request, message_id: ItemId) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.remove_message(message_id, _owner(request))
    agent = _state(request).agent
    if reply.status == 200 and agent is not None:
        agent.scrub_removed()  # its words out of the agent's memory, projects and files (now, or when a cycle ends)
    return _reply(reply)


@router.post("/api/inbox/read")
def mark_read(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.mark_read(body))


@router.get("/api/ventures")
def ventures(request: Request) -> JSONResponse:
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    return JSONResponse(agent.ventures())


@router.post("/api/ventures")
def add_venture(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.add_venture(body, _owner(request)))


@router.post("/api/ventures/{venture_id}/decide")
def decide_venture(request: Request, venture_id: ItemId, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.decide_venture(venture_id, body, _owner(request))
    _wake_for_decision(request, reply)
    return _reply(reply)


@router.post("/api/ventures/{venture_id}/knockouts")
def override_knockout(request: Request, venture_id: ItemId, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.override_knockout(venture_id, body, _owner(request))
    _wake_for_decision(request, reply)
    return _reply(reply)


@router.post("/api/milestones/{milestone_id}/autonomy")
def set_autonomy(request: Request, milestone_id: ItemId, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.set_autonomy(milestone_id, body, _owner(request))
    _wake_for_decision(request, reply)
    return _reply(reply)


@router.post("/api/autonomy/take_back")
def take_back_unlocks(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    """0.13.0: the owner's switch that takes back every unlock at once."""
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.take_back_unlocks(body, _owner(request))
    _wake_for_decision(request, reply)
    return _reply(reply)


@router.post("/api/actions/{journal_id}/undo")
def undo_action(request: Request, journal_id: ItemId) -> JSONResponse:
    """0.13.0: the owner's Undo of an action of Ember's code (carried out in the scheduler's next round)."""
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.undo(journal_id, _owner(request))
    if reply.status == 200:
        _poke(request)
    return _reply(reply)


@router.get("/api/roadmap")
def roadmap(request: Request) -> JSONResponse:
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    return JSONResponse(agent.roadmap())


@router.post("/api/roadmap")
def add_milestone(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.add_milestone(body, _owner(request)))


@router.post("/api/roadmap/goal")
def set_goal(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    """0.29.0: the owner's goal at the roadmap's root; the agent wakes to plan toward it (like a decision)."""
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.set_goal(body, _owner(request))
    _wake_for_decision(request, reply)
    return _reply(reply)


@router.post("/api/roadmap/{milestone_id}/decide")
def decide_milestone(request: Request, milestone_id: ItemId, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    reply = actions.decide_milestone(milestone_id, body, _owner(request))
    _wake_for_decision(request, reply)
    return _reply(reply)


@router.get("/api/library")
def library_view(request: Request) -> JSONResponse:
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    return JSONResponse(agent.library())


@router.get("/api/library/{document_id}")
def library_document(request: Request, document_id: ItemId) -> JSONResponse:
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    document = agent.library_document(document_id)
    if document is None:
        return JSONResponse({"error": "no such document", "field": "id"}, status_code=404)
    return JSONResponse(document)


@router.post("/api/library")
def add_document(request: Request, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.add_document(body, _owner(request)))


@router.post("/api/library/{document_id}/remove")
def remove_document(request: Request, document_id: ItemId) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.remove_document(document_id, _owner(request)))


@router.post("/api/library/{document_id}/study")
def study_document_again(request: Request, document_id: ItemId) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.study_document_again(document_id, _owner(request)))


@router.post("/api/upgrades/{upgrade_id}")
def update_upgrade(request: Request, upgrade_id: ItemId, body: Annotated[Any, Body()] = None) -> JSONResponse:
    actions = _owner_actions(request)
    if actions is None:
        return NO_AGENT
    return _reply(actions.update_upgrade(upgrade_id, body, _owner(request)))


@router.get("/api/upgrades/{upgrade_id}/script")
def upgrade_script(request: Request, upgrade_id: ItemId) -> Response:
    """The workshop script an upgrade request carries, as plain text that always downloads (like workspace files)."""
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    found = agent.upgrade_script(upgrade_id)
    if found is None:
        return JSONResponse({"error": "this upgrade request has no script"}, status_code=404)
    name, text = found
    return PlainTextResponse(
        text,
        headers={
            "content-disposition": _attachment(name),
            "content-security-policy": "sandbox; default-src 'none'",
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


@router.get("/api/cycles/{cycle_id}")
def cycle(request: Request, cycle_id: Annotated[int, Path(ge=1, le=2**62)]) -> JSONResponse:
    agent = _state(request).agent
    detail = agent.cycle_detail(cycle_id) if agent is not None else None
    if detail is None:
        return JSONResponse({"error": "no such wake cycle"}, status_code=404)
    return JSONResponse(detail)


@router.get("/api/workspace")
def workspace(request: Request) -> JSONResponse:
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    return JSONResponse(agent.workspace())


@router.get("/api/workspace/file")
def workspace_file(request: Request, path: str = "") -> Response:
    """One file the agent wrote, as plain text that browsers always download and never render.

    The dashboard shares Home Assistant's origin, so agent-written HTML or SVG must never be
    rendered here: the file is an attachment in a sandbox without any sources, and not sniffed.
    """
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    try:
        name, text = agent.workspace_file(path)
    except WorkspaceFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return PlainTextResponse(
        text,
        headers={
            "content-disposition": _attachment(name),
            "content-security-policy": "sandbox; default-src 'none'",
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


@router.get("/api/workspace/product")
def workspace_product(request: Request, path: str = "", inline: bool = False) -> Response:
    """A PDF, Word, Excel or PNG file Ember's code made in the workspace, with its exact content type.

    Files are downloads; only a picture (PNG, JPEG) may be shown inline, for the dashboard's previews. The sandbox
    policy and nosniff keep a browser from ever running anything in them, as for the text files.
    """
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    try:
        name, data, content_type = agent.workspace_product(path)
    except WorkspaceFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    shown = inline and content_type in ("image/png", "image/jpeg")
    return Response(
        data,
        media_type=content_type,
        headers={
            "content-disposition": _attachment(name, inline=shown),
            "content-security-policy": "sandbox; default-src 'none'",
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


@router.get("/api/workspace/thumb")
async def workspace_thumb(request: Request, path: str = "") -> Response:
    """0.26.0: a small JPEG of a PNG or JPEG in the workspace, for the dashboard's lists (a listing photo has MBs).

    One drawn before comes from memory; a new one is drawn on a thread of its own, one at a time (a print-size poster
    takes seconds on a Raspberry Pi), and a request waiting for its turn holds no worker thread: a list asking for many
    at once never holds up the dashboard. The dashboard asks with the file's time and size in the address, so a browser
    may keep it a day: a changed picture has another address. Shown inline, with the same sandbox policy and nosniff
    as the files themselves.
    """
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    try:
        name, data = await asyncio.to_thread(agent.workspace_thumb, path, False)
        if data is None:
            name, data = await asyncio.get_running_loop().run_in_executor(_THUMBS_DRAWN, agent.workspace_thumb, path)
    except WorkspaceFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            "content-disposition": _attachment(name, inline=True),
            "content-security-policy": "sandbox; default-src 'none'",
            "x-content-type-options": "nosniff",
            "cache-control": "private, max-age=86400",
        },
    )


def _attachment(name: str, inline: bool = False) -> str:
    """A Content-Disposition value with a plain ASCII name and the exact one (RFC 6266, RFC 5987)."""
    clean = "".join(c for c in name if c.isprintable() and c not in "\"'\\/;%").strip() or "file.txt"
    fallback = clean.encode("ascii", "replace").decode("ascii").replace("?", "_")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(clean, safe='')}"


# 0.13.0 (Phase E3): the owner's website, as Ember's code builds it: its files' names and their types.
_SITE_FILE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?\.(html|txt|xml)$")
_SITE_TYPES = {"html": "text/html; charset=utf-8", "txt": "text/plain; charset=utf-8", "xml": "application/xml"}
# The preview runs nothing on the dashboard's origin: a sandbox without scripts, forms or popups (the same origin only,
# so its links keep the owner's session), and its own stylesheet the only thing it may load.
SITE_PREVIEW_POLICY = f"sandbox allow-same-origin; {site.CSP}; frame-ancestors 'none'"


@router.get("/api/site/preview/{name}")
def site_preview(request: Request, name: Annotated[str, Path(max_length=60)]) -> Response:
    """A file of the owner's website as it would be published: the dashboard's Preview opens index.html, and its
    links open the others."""
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    if not _SITE_FILE.match(name):
        return PlainTextResponse("There is no such page.", status_code=404)
    try:
        files = agent.site_files()
    except SiteError as exc:
        return PlainTextResponse(f"The website can't be built yet: {exc}.", status_code=409)
    data = files.get(name)
    if data is None:
        return PlainTextResponse("There is no such page.", status_code=404)
    return Response(
        data,
        media_type=_SITE_TYPES[name.rsplit(".", 1)[1]],
        headers={
            "content-security-policy": SITE_PREVIEW_POLICY,
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


@router.get("/api/site/download")
def site_download(request: Request) -> Response:
    """The website as one zip for the owner to upload to their host; the download is recorded (what changed since
    is what the owner hasn't published)."""
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    try:
        data = agent.site_download()
    except SiteError as exc:
        return JSONResponse({"error": f"The website can't be built yet: {exc}."}, status_code=409)
    return Response(
        data,
        media_type="application/zip",
        headers={
            "content-disposition": _attachment("website.zip"),
            "content-security-policy": "sandbox; default-src 'none'",
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


# 0.14.0: the blog on the owner's website: the page an upload request carries (the owner previews exactly what goes
# up, in a sandbox that runs nothing and loads only the site's own stylesheet, font and pictures from the site), and
# their check of the SFTP login.
BLOG_PREVIEW_POLICY = "sandbox; default-src 'none'; frame-ancestors 'none'"


@router.get("/api/blog/preview/{approval_id}")
def blog_preview(request: Request, approval_id: ItemId) -> Response:
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    found = agent.blog_page(approval_id)
    if found is None:
        return PlainTextResponse("There is no such page.", status_code=404)
    data, policy = found[1], BLOG_PREVIEW_POLICY
    site_url = urlsplit(agent.settings.site_url.strip())
    if site_url.scheme == "https" and site_url.hostname and not site_url.username:  # the site's look, from the site
        origin = f"https://{site_url.netloc}"
        data, policy = blog.preview(data, origin), blog.preview_policy(origin)
    return Response(
        data,
        media_type="text/html; charset=utf-8",
        headers={
            "content-security-policy": policy,
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        },
    )


# 0.16.0: Ember live on the owner's website: its files as they would go up now. The page loads the site's stylesheet,
# font and pictures from the site, and its chart from here; nothing runs.
_LIVE_NAMES = {
    "live.html": live.PAGE,
    "banner.svg": live.BANNER,
    "balance.svg": live.CHART,
    "live-en.html": live.PAGE_EN,
    "banner-en.svg": live.BANNER_EN,
    "balance-en.svg": live.CHART_EN,
}


@router.get("/api/live/preview/{name}")
def live_preview(request: Request, name: Annotated[str, Path(max_length=20)]) -> Response:
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    path = _LIVE_NAMES.get(name)
    if path is None:
        return PlainTextResponse("There is no such file.", status_code=404)
    data = agent.live_files().get(path)
    if data is None:
        return PlainTextResponse("This file isn't shown (see the live options).", status_code=404)
    headers = {"x-content-type-options": "nosniff", "cache-control": "no-store"}
    if path.endswith(".svg"):
        headers["content-security-policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return Response(data, media_type="image/svg+xml", headers=headers)
    for chart in (live.CHART, live.CHART_EN):  # the chart from here: the site has the last uploaded one
        data = data.replace(f'src="/{chart}"'.encode(), f'src="{chart.rsplit("/", 1)[1]}"'.encode())
    policy = "default-src 'none'; img-src 'self'"
    site_url = urlsplit(agent.settings.site_url.strip())
    if site_url.scheme == "https" and site_url.hostname and not site_url.username:  # the site's look, from the site
        origin = f"https://{site_url.netloc}"
        data = blog.preview(data, origin)
        policy = f"default-src 'none'; style-src {origin}; font-src {origin}; img-src {origin} 'self'"
    data = re.sub(rb'<meta http-equiv="(?:Content-Security-Policy|refresh)"[^>]*>\n', b"", data)
    headers["content-security-policy"] = (
        f"sandbox allow-same-origin; {policy}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return Response(data, media_type="text/html; charset=utf-8", headers=headers)


@router.post("/api/live/titles/{text_id}")
def live_title(
    request: Request,
    text_id: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    body: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """The owner's word on one of the agent's titles for the live page: {"show": true} shows it from the next upload
    on (at once), {"show": false} keeps it off. Only a title the page would show now."""
    agent = _state(request).agent
    if agent is None:
        return NO_AGENT
    if not isinstance(body, dict) or set(body) != {"show"} or not isinstance(body["show"], bool):
        return JSONResponse({"error": 'send {"show": true} or {"show": false}', "field": "show"}, status_code=422)
    try:
        card = agent.decide_live_title(text_id, body["show"], _owner(request))
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse({"live": card})


@router.post("/api/blog/check")
def blog_check(request: Request) -> JSONResponse:
    """Log in to the owner's server (pinning its key the first time) and read the blog's list."""
    agent = _state(request).agent
    if agent is None:
        return JSONResponse({"code": "not_runnable", "error": "the agent is not running"}, status_code=409)
    try:
        result = agent.blog_check()
    except (SftpError, BlogError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:  # noqa: BLE001 - 0.16.1: the owner hears what went wrong, never a bare 500
        log.exception("Checking the SFTP connection failed")
        return JSONResponse({"error": f"the check failed ({type(exc).__name__}): see the app's log"}, status_code=502)
    who = _owner(request) or "The owner"
    found = f"{result['posts']} posts in the blog's list" if result["index"] else "no blog list yet"
    event_log.record(_state(request).db, "info", "website", f"{who} checked the SFTP connection: it works ({found})")
    return JSONResponse(result)


@router.get("/api/diagnostics")
def diagnostics_report(request: Request, full: bool = False) -> PlainTextResponse:
    """A text report of the whole system for troubleshooting (never contains secrets): shareable, or with other
    people's text (emails, web pages) when ``full``."""
    return PlainTextResponse(diagnostics.report(_state(request), full=full), headers={"cache-control": "no-store"})
