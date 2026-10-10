"""Metered model calls: the budget guard.

Every model call goes through :meth:`MeteredModel.call`, and one metered call
is exactly one HTTP request (the client never retries on its own; a retry or a
``pause_turn`` continuation is a new metered call). A call has three steps:

1. reserve: in one transaction, check everything that could forbid the call
   (life state, spending caps, balance, the last-will reserve), price the
   request's worst case and record it as a pending reservation. A refusal is
   recorded too, and only raised after the transaction has committed.
2. send: outside any lock, through the transport.
3. finalize: record what the call actually cost, once, together with its
   ``api_cost`` ledger row.

The transport is picked from the mode, so a dry run can never reach the real
API: the fake model (agent/fake_llm.py) is ``simulated``, the Anthropic
transport (anthropic_transport.py, the only module that imports the
``anthropic`` package) is not, and every row records which one ran.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol

from .. import events
from ..config import ModelPrice, Settings
from ..db import Database
from ..version import app_version
from . import burn
from .clock import Clock, from_iso, to_iso
from .costs import MICROS_PER_USD, Usage, container_micros, cost_micros, micros_to_usd
from .estimate import (
    CONTAINER_MINIMUM_MINUTES,
    Plan,
    Unpriceable,
    expected_micros,
    plan_request,
    worst_case_micros,
)
from .ledger import OUTSIDE_CYCLE_CAP, Books
from .life import Life, LifeStatus
from .pricing import (
    SAFETY_FADE_DAYS,
    US_INFERENCE_MULTIPLIER,
    fade_safety_factors,
    geo_multiplier,
    last_will_reserve,
    mark_us_inference,
    note_accurate_call,
    raise_safety_factor,
    safety_factor,
    working_cycle_cost,
)

log = logging.getLogger(__name__)

# How a wake cycle can end ("stopped" is also set by the guard after an overrun).
CYCLE_END_STATUSES = frozenset({"completed", "idle", "refused", "failed", "stopped", "interrupted"})
# Calls that open a wake cycle; refusing one of them for lack of money is starvation.
OPENING_PURPOSES = frozenset({"plan", "last_will"})
# 0.12.0: the calls that do a cycle's work, charged to the venture and milestone they serve (the cycle's focus, or the
# venture a research call names); the rest (plans, reviews, critiques, brainstorms, library study, the last will) is
# overhead.
WORK_PURPOSES = ("work", "reflect", "research", "workshop", "draft")  # 0.12.0: a draft is work too
_PURPOSE = re.compile(r"^[a-z_]{1,32}$")
_TRIGGER = re.compile(r"^[a-z_]{1,32}$")
CLOCK_TOLERANCE_SECONDS = 60
_KNOWN_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_creation",
        "server_tool_use",
        "service_tier",
        "inference_geo",
        "iterations",
        "output_tokens_details",  # e.g. thinking_tokens: already part of output_tokens
    }
)
_KNOWN_SERVER_TOOLS = frozenset({"web_search_requests", "web_fetch_requests", "code_execution_requests"})
# Workshop calls (code execution, 0.7.0) have their own cap per run and count toward the daily cap and the balance,
# but not toward the cycle cap: one run can cost more than a whole wake cycle may. The daily review (0.7.1), once a
# day before the first plan, doesn't count toward the cycle cap either, so the cycle it opens can still do its work.
WORKSHOP = "workshop"
RESEARCH = "research"
REVIEW = "review"
STUDY = "study"  # 0.12.0: Ember studying its owner's library, within the owner's daily study budget
CONSOLIDATE = "consolidate"  # 0.12.0: the lessons' consolidation after the daily review
RESEARCH_CHECK = "research_check"  # 0.12.0: the research model's check, on the agent's research questions
CRITIC = "critic"  # 0.13.0: the independent critic of a proposed venture's case, before the plan
# 0.13.0: until EVENT_RESERVE_HOUR (the owner's time), EVENT_RESERVE_SHARE of the daily cap is kept for the agenda's
# event wake-ups (agent/agenda.py): a scheduled cycle's cap leaves it, and a scheduled wake that would need it waits.
# 0.30.3: only while the owner's wake_on_events is on (event_reserve); 0.31.0, and a kind of event under it.
EVENT_RESERVE_SHARE = 0.20
EVENT_RESERVE_HOUR = 20
# The purposes outside the cycle cap (WORKSHOP, REVIEW, STUDY, CONSOLIDATE, CRITIC) are ledger.OUTSIDE_CYCLE_CAP, one
# list for the guard and the books (0.15.0: the books counted the critic and the consolidation toward the cycle cap).
# 0.15.0: they still keep the burn mode and the event reserve: a maintenance cycle's cap bounds every call in it, and
# until EVENT_RESERVE_HOUR a scheduled cycle's calls, all of them, leave the events' share of the day.
# 0.12.0: the cycle cap counts what a call is expected to cost (the daily cap, the balance and the last-will reserve
# still count its worst case). A call with server tools (research) is expected to cost EXPECTED_FACTOR times the 95th
# percentile of the last EXPECTED_WINDOW ones on its model, once there are EXPECTED_SAMPLES; so is a reflection at
# least. A conversation's cache entry older than CACHE_FRESH_SECONDS (of its 5 minutes) counts as missed.
EXPECTED_FACTOR = Decimal("1.5")
EXPECTED_WINDOW = 20
EXPECTED_SAMPLES = 5
# 0.15.0: a workshop call holds at least what the workshop calls of the last WORKSHOP_TAIL_DAYS cost (see
# workshop_reservation). 0.21.0: a research call too. 0.37.3: research and the research model's check
# (RESEARCH_PURPOSES) send the same request (prompts.research_request), so each holds what either cost on its model: the
# check what research on the research model cost, and research, once the research model took over, what its check cost.
WORKSHOP_TAIL_DAYS = 14
RESEARCH_PURPOSES = (RESEARCH, RESEARCH_CHECK)
# 0.21.0 (analysis 0.20.1, FIX NOW 6): a call with server tools can cost more than it holds: a workshop run admitted
# with its hold just above the last will's reserve, booked at what live call #423 used, cost $1.89 on a $1.00 balance,
# and Ember died $0.89 below zero without a last will (the owner's Anthropic account paid the rest). A workshop or
# research call is admitted only while what is left above that reserve is SERVER_TOOL_ROOM times its hold, or the most
# such a call cost of its hold in the last WORKSHOP_TAIL_DAYS if that is more (server_tool_room).
# 0.37.3 (analysis 0.37.0): the research model's check too. It held only its worst case and needed only that left above
# the reserve, though a search can cost several times its worst case (live research did): near the bottom of the
# balance a check whose quote fit could still end Ember below zero without its last will.
SERVER_TOOL_PURPOSES = (WORKSHOP, RESEARCH, RESEARCH_CHECK)
SERVER_TOOL_ROOM = Decimal(5)
# 0.15.0: an overrun stops the cycle only when its call counts toward the cycle cap and it is more than
# OVERRUN_TOLERANCE of the estimate or more than OVERRUN_TOLERANCE_MICROS. Otherwise the cycle goes on without further
# calls of that purpose. Live, a workshop run $0.0025 (0.7%) over its estimate stopped a cycle mid-plan, and the
# reflection with it; stopping saves nothing once the money is spent.
OVERRUN_TOLERANCE = Decimal("0.10")
OVERRUN_TOLERANCE_MICROS = 20_000
OVERRUN_STOP = "a call cost more than its worst-case estimate"  # the note of a cycle the guard stopped
STALE_NOTE = "closing it failed; closed when the next cycle began"  # 0.22.0: close_stale
CACHE_FRESH_SECONDS = {"5m": 240, "1h": 3_540}
CONVERSATION_PURPOSES = ("work", "reflect")
_STANDARD_GEOS = frozenset({"global", "not_available"})
_SNAPSHOT_SUFFIX = re.compile(r"^-\d{8}$")


# --- transports ---


@dataclass(frozen=True)
class Completed:
    """The API answered with a message (any stop reason, including refusals)."""

    response: dict[str, Any]
    request_id: str | None = None


@dataclass(frozen=True)
class NotSent:
    """The request never reached the API (e.g. no connection); nothing is billed."""

    error: str


@dataclass(frozen=True)
class Rejected:
    """The API refused the request before generating anything (HTTP error); nothing is billed."""

    status: int
    error: str
    request_id: str | None = None


@dataclass(frozen=True)
class Interrupted:
    """The request was sent but the answer broke off; the cost is unknown."""

    error: str
    partial_usage: dict[str, Any] | None = None
    request_id: str | None = None


Outcome = Completed | NotSent | Rejected | Interrupted


class FilesError(RuntimeError):
    """A Files API operation (the workshop's inputs and outputs) failed; the message is safe to show."""


class Transport(Protocol):
    simulated: bool

    def count_tokens(self, request: Mapping[str, Any]) -> int: ...

    def send(self, request: Mapping[str, Any]) -> Outcome: ...

    # The Files API, for the workshop (free, so not metered). Each raises FilesError.
    def upload_file(self, name: str, data: bytes, mime: str) -> str: ...

    def file_info(self, file_id: str) -> dict[str, Any]: ...

    def download_file(self, file_id: str, limit: int) -> bytes: ...

    def delete_file(self, file_id: str) -> None: ...


# A picture whose size can't be read: more than the API bills for any picture it accepts.
IMAGE_TOKEN_ALLOWANCE = 5_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def rough_token_count(request: Mapping[str, Any]) -> int:
    """A generous prompt size: half a token per UTF-8 byte plus overhead for the request framing.

    A base64 picture is billed by its pixels, not its bytes, so it counts as ``picture_tokens`` instead.
    """
    pictures: list[int] = []
    size = len(json.dumps(_without_pictures(request, pictures), ensure_ascii=False, default=str).encode("utf-8"))
    return math.ceil(size / 2) + 600 + sum(pictures)


def picture_tokens(data: str) -> int:
    """A generous token count for a base64 picture: a quarter more than width x height / 750 (the API's rule)."""
    size = picture_size(data)
    if size is None:
        return IMAGE_TOKEN_ALLOWANCE
    width, height = size
    return min(IMAGE_TOKEN_ALLOWANCE, math.ceil(width * height / 750 * 1.25) + 100)


def picture_size(data: str) -> tuple[int, int] | None:
    """(width, height) of a base64 PNG, read from its header; None for anything else."""
    try:
        head = base64.b64decode(data[:32], validate=True)
    except ValueError:
        return None
    if len(head) < 24 or not head.startswith(_PNG_SIGNATURE) or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _without_pictures(node: Any, found: list[int]) -> Any:
    if isinstance(node, Mapping):
        source = node.get("source")
        if node.get("type") == "image" and isinstance(source, Mapping) and source.get("type") == "base64":
            data = source.get("data")
            found.append(picture_tokens(data) if isinstance(data, str) else IMAGE_TOKEN_ALLOWANCE)
            return {"type": "image"}
        return {key: _without_pictures(value, found) for key, value in node.items()}
    if isinstance(node, list | tuple):
        return [_without_pictures(value, found) for value in node]
    return node


class OfflineTransport:
    """Used until the model transports exist: counts tokens, never sends anything."""

    def __init__(self, simulated: bool) -> None:
        self.simulated = simulated

    def count_tokens(self, request: Mapping[str, Any]) -> int:
        return rough_token_count(request)

    def upload_file(self, name: str, data: bytes, mime: str) -> str:
        raise FilesError("files can't be sent without a model transport")

    def file_info(self, file_id: str) -> dict[str, Any]:
        raise FilesError("files can't be read without a model transport")

    def download_file(self, file_id: str, limit: int) -> bytes:
        raise FilesError("files can't be read without a model transport")

    def delete_file(self, file_id: str) -> None:
        raise FilesError("files can't be deleted without a model transport")

    def send(self, request: Mapping[str, Any]) -> Outcome:
        return NotSent("model calls are not available in this version yet")


# --- results ---


class CallRefused(Exception):
    """The budget guard refused a call or a cycle (nothing was sent)."""

    def __init__(self, reason: str, category: str, call_id: int | None = None, state: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.category = category
        self.call_id = call_id
        self.state = state


@dataclass(frozen=True)
class CallResult:
    call_id: int
    status: str
    cost_micros: int
    estimate_micros: int
    billing_uncertain: bool
    overrun: bool
    response: dict[str, Any] | None = None
    error: str | None = None


class CallFailed(Exception):
    """The call was sent (or tried) but produced no usable answer."""

    def __init__(self, result: CallResult) -> None:
        super().__init__(result.error or result.status)
        self.result = result


@dataclass
class MeterHealth:
    """Shared flags the guard checks before every call (and the dashboard shows)."""

    lock_held: bool = False
    broken: str | None = None


@dataclass(frozen=True)
class Reservation:
    call_id: int
    cycle_id: int
    model: str
    plan: Plan
    price: ModelPrice
    search_price: Decimal
    geo: Decimal
    estimate: int  # what is held of the daily cap and the balance (0.15.0: a workshop call's reservation)
    container_price: Decimal = Decimal(0)  # USD per hour of a code execution container
    started: datetime | None = None
    quote: int | None = None  # 0.15.0: the priced worst case, if less than ``estimate``: an overrun is judged by it


@dataclass(frozen=True)
class Allowance:
    """What a call may go over, in micros (MeteredModel._allowance: a reflection's, so that it always runs): the cycle
    cap, the daily cap and the event wake-ups' share of the day. The balance and the last-will reserve allow nothing."""

    cycle: int = 0
    day: int = 0
    events: int = 0


@dataclass
class _Settlement:
    status: str
    cost: int
    floor: int
    uncertain: bool
    usage: Usage = field(default_factory=Usage)
    usage_raw: dict[str, Any] | None = None
    response_model: str | None = None
    stop_reason: str | None = None
    message_id: str | None = None
    service_tier: str | None = None
    inference_geo: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    overrun: bool = False
    us_inference: bool = False
    expected: int = 0  # the estimate, adjusted for a price multiplier learned from this response
    request_id: str | None = None
    iterations: int | None = None  # 0.15.0: the samplings of a server tool's loop, if the answer says


# --- the process lock ---


class ProcessLock:
    """An exclusive lock on /data/ember.lock: only one Ember process may spend money."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def usd_cap_to_micros(amount: float) -> int:
    return int((Decimal(str(amount)) * MICROS_PER_USD).to_integral_value(rounding=ROUND_DOWN))


def recover_interrupted(db: Database, clock: Clock, boot_id: str) -> int:
    """At startup: calls and cycles an earlier process left unfinished.

    A reservation still pending after a restart may or may not have reached the
    API, so it is charged at its worst-case estimate and marked uncertain.
    Returns how many calls were recovered.
    """
    now = to_iso(clock.now())
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, estimate_micros, simulated, local_day FROM llm_calls WHERE status = 'pending' AND boot_id <> ?",
            (boot_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE llm_calls SET status = 'interrupted', cost_micros = estimate_micros, floor_micros = 0,"
                " billing_uncertain = 1, finished_at = ?, error = 'the app stopped during this call'"
                " WHERE id = ? AND status = 'pending'",
                (now, row["id"]),
            )
            conn.execute(
                "INSERT INTO ledger (ts, occurred_on, type, amount_micros, simulated, llm_call_id, note, created_by)"
                " VALUES (?, ?, 'api_cost', ?, ?, ?, 'interrupted by a restart; charged at the worst case', 'system')",
                (now, row["local_day"], row["estimate_micros"], row["simulated"], row["id"]),
            )
        cycles = conn.execute(
            "UPDATE cycles SET status = 'interrupted', ended_at = ?, note = 'the app stopped during this cycle'"
            " WHERE status = 'running' AND boot_id <> ?",
            (now, boot_id),
        ).rowcount
        if rows or cycles:
            total = sum(row["estimate_micros"] for row in rows)
            events.record(
                db,
                "warning",
                "economy",
                f"Recovered after a restart: {len(rows)} unfinished call(s) charged at their worst case"
                f" (${micros_to_usd(total):.4f}), {cycles} cycle(s) marked interrupted",
            )
    return len(rows)


def event_reserve(settings: Settings, clock: Clock, trigger: str, working: int = 0) -> int:
    """0.13.0: what a ``trigger``'s cycle leaves of the daily cap for the agenda's event wake-ups: EVENT_RESERVE_SHARE
    of it for a scheduled cycle until EVENT_RESERVE_HOUR (the owner's time), nothing for any other, nor when the rest
    of the cap couldn't pay for a working cycle (``working``: what one needs) anyway. 0.30.3: nor with the owner's
    wake_on_events off: no event can wake the agent to spend it, and a scheduled wake waited until 20:00 for nothing.
    0.31.0: nor while each kind of event is switched off (Settings.waking_events)."""
    if not settings.waking_events():
        return 0
    if trigger != "schedule" or clock.now().astimezone(clock.tz).hour >= EVENT_RESERVE_HOUR:
        return 0
    daily = usd_cap_to_micros(settings.daily_spend_cap_usd)
    held = int(daily * EVENT_RESERVE_SHARE)
    return held if daily - held >= working else 0


def workshop_reservation(
    db: Database, settings: Settings, clock: Clock, simulated: bool, model: str, quote: int
) -> int:
    """0.15.0: what a workshop call on ``model`` holds of the daily cap and the balance: its worst case (``quote``),
    the owner's cap per run, or EXPECTED_FACTOR times the costliest of the workshop calls on ``model`` in the last
    WORKSHOP_TAIL_DAYS days (``_workshop_tail``), whichever is most.

    A code execution call's worst case is a price under assumptions nothing in the request enforces (estimate.py):
    live, a run admitted under a $0.35 estimate cost $1.84, and at the default caps it would have taken the day to
    $2.94 against a $1.50 cap. Holding what runs were seen to cost keeps the daily cap and the balance hard for runs
    like those; a run can still cost more than anything seen, and that is booked when it happens. The tail forgets a
    costly run after WORKSHOP_TAIL_DAYS, so it can't refuse the workshop for good.

    0.16.2: the costliest call, not the 95th percentile (of 20 calls, that left the costliest out); and the owner's
    Reset estimates no longer clears the tail: one click on it dropped the hold after a run like #423 from $2.76 to
    the $1.50 cap per run, which the same run would have broken the daily cap with. 0.33.0: a run holds at most what
    the day has left (MeteredModel.reservation's room), and at least its worst case: live, a $2.76 hold refused every
    run once the day's spending passed about $3 (four refusals in two days, two cycles of a book's edits lost), while
    each of those runs was priced below what was left. A run that costs more than it held is booked as it happens."""
    return max(quote, usd_cap_to_micros(settings.workshop_run_cap_usd), _tail_hold(db, clock, simulated, model))


def _tail_hold(db: Database, clock: Clock, simulated: bool, model: str, purposes: tuple[str, ...] = (WORKSHOP,)) -> int:
    """EXPECTED_FACTOR times the costliest recent call of ``purposes`` on ``model`` (``_workshop_tail``; 0 without
    one)."""
    costs = [cost for _, cost in _workshop_tail(db, clock, simulated, model, purposes)]
    return int((Decimal(max(costs)) * EXPECTED_FACTOR).to_integral_value(rounding=ROUND_CEILING)) if costs else 0


def server_tool_room(db: Database, clock: Clock, simulated: bool) -> Decimal:
    """0.21.0: how many times its hold a workshop or research call (0.37.3: or the research model's check) needs above
    the last will's reserve: SERVER_TOOL_ROOM, or the most such a call cost of what it held in the last
    WORKSHOP_TAIL_DAYS if that is more."""
    since = to_iso(clock.now() - timedelta(days=WORKSHOP_TAIL_DAYS))
    marks = ", ".join("?" for _ in SERVER_TOOL_PURPOSES)
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT floor_micros, estimate_micros FROM llm_calls WHERE purpose IN ({marks}) AND simulated = ?"
            " AND status IN ('ok', 'interrupted') AND estimate_micros > 0 AND floor_micros > estimate_micros"
            " AND ts >= ?",
            (*SERVER_TOOL_PURPOSES, 1 if simulated else 0, since),
        ).fetchall()
    return max([SERVER_TOOL_ROOM, *(Decimal(int(r[0])) / Decimal(int(r[1])) for r in rows)])


def workshop_tail_ends(db: Database, clock: Clock, simulated: bool, model: str, above: int) -> datetime | None:
    """0.15.0: when the last of the recent workshop calls on ``model`` that alone make a hold of more than ``above``
    leaves the tail of workshop_reservation (None if none does)."""
    costly = [ts for ts, cost in _workshop_tail(db, clock, simulated, model) if cost * EXPECTED_FACTOR > above]
    return from_iso(max(costly)) + timedelta(days=WORKSHOP_TAIL_DAYS) if costly else None


def _workshop_tail(
    db: Database, clock: Clock, simulated: bool, model: str, purposes: tuple[str, ...] = (WORKSHOP,)
) -> list[tuple[str, int]]:
    """(when, what it is known to cost) of the last EXPECTED_WINDOW calls of ``purposes`` (the workshop's; research's
    and its check's together: 0.37.3) on ``model`` in the last WORKSHOP_TAIL_DAYS days that are known to have cost
    something. What they are known to cost, not what they were booked at: an uncertain call booked at its hold would
    raise the next hold with every call. 0.16.2: an interrupted call too (a broken stream reports what the run had used
    so far; one cut by a restart knows nothing, so it adds nothing)."""
    since = to_iso(clock.now() - timedelta(days=WORKSHOP_TAIL_DAYS))
    marks = ", ".join("?" for _ in purposes)
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT ts, floor_micros FROM llm_calls WHERE model = ? AND purpose IN ({marks}) AND simulated = ?"
            " AND status IN ('ok', 'interrupted') AND floor_micros > 0 AND ts >= ? ORDER BY id DESC LIMIT ?",
            (model, *purposes, 1 if simulated else 0, since, EXPECTED_WINDOW),
        ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


class MeteredModel:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        clock: Clock,
        books: Books,
        life: Life,
        transport: Transport,
        boot_id: str,
        health: MeterHealth,
    ) -> None:
        if bool(transport.simulated) != (life.mode == "dry_run"):
            raise ValueError("the transport doesn't match the mode (dry run must be simulated, live must be real)")
        self.db = db
        self.settings = settings
        self.clock = clock
        self.books = books
        self.life = life
        self.transport = transport
        self.boot_id = boot_id
        self.health = health

    @property
    def simulated(self) -> bool:
        return bool(self.transport.simulated)

    # --- cycles ---

    def open_cycle(self, trigger: str) -> int:
        """Start a wake cycle with this cycle's spending cap. Raises CallRefused."""
        if not _TRIGGER.match(trigger):
            raise ValueError("bad trigger name")
        self._fade_factors()  # 0.15.0
        status = self.life.evaluate_and_persist()
        # 0.12.0: a maintenance cycle has at most its burn mode's cap (kept before the transaction: it writes meta)
        mode = burn.current(self.db, status)
        cap = self.opening_cap(trigger, mode)
        refusal: tuple[str, str] | None = None
        cycle_id = 0
        with self.db.transaction() as conn:
            refusal = self._system_refusal() or _state_refusal(status)
            if refusal is None and conn.execute("SELECT 1 FROM cycles WHERE status = 'running'").fetchone():
                refusal = ("another wake cycle is still running", "request")
            if refusal is None:
                cycle_id = int(
                    conn.execute(
                        "INSERT INTO cycles (life_id, boot_id, started_at, status, trigger, simulated, cap_micros,"
                        " session, app_version, burn_mode) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                        (
                            status.life_id,
                            self.boot_id,
                            to_iso(self.clock.now()),
                            trigger,
                            1 if self.simulated else 0,
                            cap,
                            self.life.session(),
                            app_version()[:40],
                            mode.mode,  # 0.15.0: the guard keeps the mode the cycle opened in
                        ),
                    ).lastrowid
                )
        if refusal is not None:
            raise CallRefused(refusal[0], refusal[1], state=status.state)
        return cycle_id

    def close_stale(self) -> list[int]:
        """0.22.0 (analysis 0.20.1, FIX NOW 17): the cycles of this boot still marked running, closed as failed. Call it
        only where no cycle can be running (the agent runs one at a time): closing one failed (its report or its
        close raised), and every later cycle was refused as "another wake cycle is still running" until a restart,
        Wake now included, without an event saying why. Returns their numbers."""
        now = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            stale = [
                int(r[0])
                for r in conn.execute("SELECT id FROM cycles WHERE status = 'running' AND boot_id = ?", (self.boot_id,))
            ]
            for cycle_id in stale:
                conn.execute(
                    "UPDATE cycles SET status = 'failed', ended_at = ?, note = COALESCE(note, ?)"
                    " WHERE id = ? AND status = 'running'",
                    (now, STALE_NOTE, cycle_id),
                )
        return stale

    def _fade_factors(self) -> None:
        """0.15.0: raised safety factors unchanged for SAFETY_FADE_DAYS come down (pricing.fade_safety_factors)."""
        for purpose, model, factor in fade_safety_factors(self.db, self.life.mode, self.clock.today()):
            events.record(
                self.db,
                "info",
                "economy",
                f"Estimates for {purpose} calls on {model} are now scaled by {factor}: {SAFETY_FADE_DAYS} days"
                " without a change.",
            )

    def opening_cap(self, trigger: str, mode: burn.Burn) -> int:
        """What a ``trigger``'s cycle opened now in burn ``mode`` may spend (reads only): the options' cycle cap, at
        most the mode's (maintenance), and for a scheduled cycle until EVENT_RESERVE_HOUR at most the day's rest less
        the events' share (0.13.0)."""
        cap = mode.cycle_cap(usd_cap_to_micros(self.settings.cycle_spend_cap_usd))
        held = self.held_for_events(trigger)  # 0.13.0: the events' share of the day
        if held:
            today = self.books.cap_spend_on(self.life.scope(), self.clock.today())
            cap = min(cap, max(0, usd_cap_to_micros(self.settings.daily_spend_cap_usd) - today - held))
        return cap

    def held_for_events(self, trigger: str) -> int:
        """What a ``trigger``'s cycle leaves of the day for event wake-ups now (``event_reserve``); 0.15.0: every call
        of a scheduled cycle leaves it, not only its cap."""
        if trigger != "schedule":
            return 0
        working = working_cycle_cost(self.settings, self.db, self.life.mode) or 0
        return event_reserve(self.settings, self.clock, trigger, working)

    def cycle_room(self, cycle_id: int | None, mode: burn.Burn) -> tuple[int, str]:
        """0.15.0: (what the cycle may still spend under the cap in force, as its plan is judged; why that is below
        the options' cycle cap: "maintenance" for the burn mode's cap, "events" for the event reserve, or ""). With no
        cycle (the diagnostics' preview), a scheduled cycle's, opened now in burn ``mode``."""
        if cycle_id is None:
            trigger, opened, room = "schedule", mode.mode, self.opening_cap("schedule", mode)
        else:
            with self.db.connection() as conn:
                cycle = conn.execute("SELECT trigger, burn_mode FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
            if cycle is None:
                return 0, ""
            trigger, opened, room = cycle["trigger"], cycle["burn_mode"], self.rooms(cycle_id, "plan")[0]
        if opened == burn.MAINTENANCE:
            return room, "maintenance"
        cut = self.held_for_events(trigger) and room < usd_cap_to_micros(self.settings.cycle_spend_cap_usd)
        return room, "events" if cut else ""

    def close_cycle(self, cycle_id: int, status: str = "completed", note: str | None = None) -> bool:
        """End a running cycle. Returns False if it had already ended (e.g. stopped after an overrun)."""
        if status not in CYCLE_END_STATUSES:
            raise ValueError(f"unknown cycle status {status!r}")
        with self.db.connection() as conn:
            updated = conn.execute(
                "UPDATE cycles SET status = ?, ended_at = ?, note = COALESCE(?, note)"
                " WHERE id = ? AND status = 'running'",
                (status, to_iso(self.clock.now()), note, cycle_id),
            ).rowcount
        return updated == 1

    # --- calls ---

    def call(
        self,
        cycle_id: int,
        purpose: str,
        request: Mapping[str, Any],
        venture_id: int | None = None,
        hold: int | None = None,
    ) -> CallResult:
        """Reserve, send and settle one model request (``venture_id``: the venture it serves, if not the cycle's;
        ``hold``: 0.33.0, what a workshop call holds, reservation with its room). Raises CallRefused or CallFailed."""
        with self.db.connection() as conn:
            if conn.in_transaction:
                # The reservation must be committed before the request is sent, and nothing may hold the
                # database while waiting for the API.
                raise RuntimeError("model calls must not run inside a database transaction")
        # The request that is priced is exactly the request that is sent.
        frozen = copy.deepcopy(dict(request))
        reservation = self.reserve(cycle_id, purpose, frozen, venture_id, hold)
        try:
            outcome = self.transport.send(frozen)
        except Exception as exc:  # noqa: BLE001 - a transport bug must not lose the reservation
            log.exception("Model transport failed")
            outcome = Interrupted(f"transport error: {type(exc).__name__}")
        result = self.finalize(reservation, outcome)
        if result.status != "ok":
            raise CallFailed(result)
        return result

    def quote(
        self, request: Mapping[str, Any], purpose: str = "work", extra_tokens: int = 0, *, scaled: bool = True
    ) -> int:
        """The worst case the guard would price ``request`` at as a ``purpose`` call now (reads only), with
        ``extra_tokens`` more of prompt (what the conversation may still grow by); ``scaled=False``: without its safety
        factor, what a workshop run's cap is checked against (0.15.0). Raises Unpriceable."""
        plan, price = self._plan(request, extra_tokens)
        return self._estimate(
            plan,
            price,
            Decimal(str(self.settings.web_search_usd_per_1000)),
            geo_multiplier(self.db),
            Decimal(str(self.settings.code_execution_usd_per_hour)),
            purpose if scaled else None,
        )

    def reservation(self, request: Mapping[str, Any], purpose: str = "work", room: int | None = None) -> int:
        """0.15.0: what the guard would hold of the daily cap and the balance for ``request`` as a ``purpose`` call now
        (reads only): its worst case, a workshop call's at least its cap per run and what recent runs cost
        (workshop_reservation). 0.33.0: with ``room`` (what is left of the day and the balance), a workshop call
        holds no more than that, and never less than its worst case. Raises Unpriceable."""
        quote = self.quote(request, purpose)
        held = self._held(purpose, str(request.get("model") or ""), quote)
        return held if room is None or purpose != WORKSHOP else max(quote, min(held, room))

    def _held(self, purpose: str, model: str, estimate: int) -> int:
        # 0.21.0: what recent research calls cost, like a workshop call's; 0.37.3: the research model's check too, and
        # either holds what both cost on its model (RESEARCH_PURPOSES)
        if purpose in RESEARCH_PURPOSES:
            return max(estimate, _tail_hold(self.db, self.clock, self.simulated, model, RESEARCH_PURPOSES))
        if purpose != WORKSHOP:
            return estimate
        return workshop_reservation(self.db, self.settings, self.clock, self.simulated, model, estimate)

    def expected(
        self,
        request: Mapping[str, Any],
        purpose: str,
        cycle_id: int,
        extra_tokens: int = 0,
        cached: int | None = None,
    ) -> int:
        """0.12.0: what the cycle cap counts for ``request`` as a ``purpose`` call in cycle ``cycle_id`` now (reads
        only): its expected cost, at most its worst case; a call outside the cycle cap, its worst case. ``cached``:
        the prompt tokens the cache will hold by then, if not what the cycle's last call cached (a reflection after
        a step reads what the step cached). Raises Unpriceable."""
        worst = self.quote(request, purpose, extra_tokens)
        if purpose in OUTSIDE_CYCLE_CAP:
            return worst
        plan, price = self._plan(request, extra_tokens)
        return min(worst, self._expected(plan, price, purpose, cycle_id, worst, cached)[0])

    def prompt_tokens(self, request: Mapping[str, Any]) -> int:
        """The prompt's tokens as the guard counts them (reads only). Raises Unpriceable."""
        return self._plan(request)[0].input_tokens

    def affordable(
        self,
        request: Mapping[str, Any],
        purpose: str,
        cycle_id: int,
        keep: int = 0,
        keep_money: int | None = None,
        extra_tokens: int = 0,
    ) -> tuple[bool, int, int]:
        """0.12.0: (whether ``request`` fits now as a ``purpose`` call, its expected cost, its worst case), as the guard
        will judge it: its expected cost under its own cap (the cycle cap) less ``keep``, its worst case under the
        daily cap and the balance less ``keep_money`` (``keep`` if not given). A reflection may go over the caps by
        what ``_allowance`` allows. Raises Unpriceable."""
        worst = self.quote(request, purpose, extra_tokens)
        expected, allowance = worst, Allowance()
        if purpose not in OUTSIDE_CYCLE_CAP:
            plan, price = self._plan(request, extra_tokens)
            expected, miss = self._expected(plan, price, purpose, cycle_id, worst)
            expected = min(expected, worst)
            allowance = self._allowance(cycle_id, purpose, miss)
        cycle_room, money_room = self.rooms(cycle_id, purpose, keep, keep_money, allowance)
        own = expected if purpose not in OUTSIDE_CYCLE_CAP else worst
        # 0.23.0: what the call holds of the day and the balance, as the guard counts it (research and, 0.37.3, its
        # check: their tail too)
        held = self._held(purpose, str(request.get("model") or ""), worst)
        return own <= cycle_room and held <= money_room, expected, worst

    def reflection_reserve(self, expected: int, model: str) -> int:
        """0.12.0: what a cycle keeps for its reflection under the cycle cap: 1.5 times the 95th percentile of the
        recent reflections on ``model``, and at least what this one is expected to cost."""
        history = self._history(model, "reflect")
        return max(expected, history or 0)

    def _plan(self, request: Mapping[str, Any], extra_tokens: int = 0) -> tuple[Plan, ModelPrice]:
        try:
            input_tokens = self.transport.count_tokens(request)
        except Exception:  # noqa: BLE001 - same fallback as reserve()
            input_tokens = rough_token_count(request)
        plan = plan_request(request, input_tokens + max(extra_tokens, 0))
        price = self.settings.price_for(plan.model)
        if price is None:
            raise Unpriceable(f"model {plan.model!r} has no entry in the price table")
        return plan, price

    def _expected(
        self, plan: Plan, price: ModelPrice, purpose: str, cycle_id: int, worst: int, cached: int | None = None
    ) -> tuple[int, int]:
        """(the expected cost of a ``purpose`` call, what one cache miss would add to it) in micros, at the safety
        factor of its model and purpose: a call with server tools by its history (the worst case without one), a
        conversation's call by what the calls before it in the cycle cached (or ``cached``)."""
        if plan.tool_uses or plan.code_runs:
            history = self._history(plan.model, purpose)
            return (worst if history is None else min(history, worst)), 0
        if purpose not in CONVERSATION_PURPOSES or not plan.cache_ttls:
            cached = 0
        elif cached is None:
            cached = self._cached(cycle_id, plan.model, plan.cache_ttls)
        output = self._history(plan.model, purpose, "output_tokens")
        base, miss = expected_micros(plan, price, cached, geo_multiplier(self.db), output)
        factor = safety_factor(self.db, plan.model, self.life.mode, purpose)
        scaled = [int((Decimal(n) * factor).to_integral_value(rounding=ROUND_CEILING)) for n in (base, miss)]
        return scaled[0], scaled[1]

    def _history(self, model: str, purpose: str, column: str = "cost_micros") -> int | None:
        """EXPECTED_FACTOR times the 95th percentile of ``column`` (their cost, or their output tokens) of the last
        EXPECTED_WINDOW calls of ``purpose`` on ``model`` in this mode that cost something, or None while there are
        fewer than EXPECTED_SAMPLES."""
        if column not in ("cost_micros", "output_tokens"):
            raise ValueError(column)
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT {column} FROM llm_calls WHERE model = ? AND purpose = ? AND simulated = ? AND status = 'ok'"
                " AND cost_micros > 0 ORDER BY id DESC LIMIT ?",
                (model, purpose, 1 if self.simulated else 0, EXPECTED_WINDOW),
            ).fetchall()
        costs = sorted(int(r[0]) for r in rows)
        if len(costs) < EXPECTED_SAMPLES:
            return None
        p95 = costs[min(len(costs) - 1, math.ceil(0.95 * len(costs)) - 1)]
        return int((Decimal(p95) * EXPECTED_FACTOR).to_integral_value(rounding=ROUND_CEILING))

    def _cached(self, cycle_id: int, model: str, ttls: tuple[str, ...]) -> int:
        """The prompt tokens the conversation's last call in this cycle cached (0 if its entry may be gone), counted as
        the guard counted that call's prompt, so they compare with this one's."""
        if not ttls:
            return 0
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT json_extract(plan, '$.input_tokens'), finished_at FROM llm_calls WHERE cycle_id = ?"
                " AND model = ? AND status = 'ok' AND purpose IN ('work', 'reflect') ORDER BY id DESC LIMIT 1",
                (cycle_id, model),
            ).fetchone()
        if row is None or row[1] is None:
            return 0
        fresh = max(CACHE_FRESH_SECONDS.get(ttl, 0) for ttl in ttls)
        if (self.clock.now() - from_iso(row[1])).total_seconds() > fresh:
            return 0
        return int(row[0] or 0)

    def headroom(self, cycle_id: int, purpose: str = "work", keep: int = 0) -> int:
        """How much the next call of ``purpose`` in this cycle may cost: the tightest of the cycle cap, the daily
        cap and the balance (keeping the last-will reserve unless the will is written or this is the will), less
        ``keep`` of each that also limits a later call (0.12.0: the reflection's reserve)."""
        return min(self.rooms(cycle_id, purpose, keep))

    def rooms(
        self,
        cycle_id: int,
        purpose: str = "work",
        keep: int = 0,
        keep_money: int | None = None,
        allowance: Allowance | None = None,
    ) -> tuple[int, int]:
        """0.12.0: (the room under the call's own cap: the cycle cap, a workshop run's cap, or the daily cap for the
        other calls outside the cycle cap, less ``keep`` where it limits a later call too; the room under the daily
        cap and the balance, keeping the last-will reserve, less ``keep_money``, ``keep`` if not given). The cycle cap
        counts expected costs, the rest worst cases. 0.15.0: in a maintenance cycle every call's own room is also the
        cycle cap's, and until EVENT_RESERVE_HOUR a scheduled cycle's calls leave the event reserve (``_money_refusal``
        says how). ``allowance``: what the call may go over the caps by (a reflection's, ``_allowance``)."""
        over = allowance or Allowance()
        keep_money = keep if keep_money is None else keep_money
        status = self.life.evaluate()
        scope = self.life.scope()
        with self.db.connection() as conn:
            cycle = conn.execute(
                "SELECT cap_micros, trigger, burn_mode FROM cycles WHERE id = ?", (cycle_id,)
            ).fetchone()
        if cycle is None:
            return 0, 0
        every = cycle["burn_mode"] == burn.MAINTENANCE  # 0.15.0: its cap bounds every call in it
        spent, reserved = self.books.cycle_spend(cycle_id, outside_cap=False, every_purpose=every)
        pending = self.books.pending(scope)
        daily_cap = usd_cap_to_micros(self.settings.daily_spend_cap_usd)
        today = self.books.cap_spend_on(scope, self.clock.today())
        held = self.held_for_events(cycle["trigger"])  # 0.15.0
        in_cycle_cap = purpose not in OUTSIDE_CYCLE_CAP or every
        cycle_room = cycle["cap_micros"] - spent - reserved + over.cycle
        if purpose not in OUTSIDE_CYCLE_CAP:
            # 0.15.0: what the calls outside the cycle cap spent before comes out of the day's rest less the reserve
            own_cap = min(cycle_room, daily_cap - held - today - pending + over.events) if held else cycle_room
        elif purpose == WORKSHOP:
            own_cap = usd_cap_to_micros(self.settings.workshop_run_cap_usd)
        else:
            own_cap = daily_cap  # only the daily cap and the balance limit it (a study also its own budget: loop.py)
        if every:
            own_cap = min(own_cap, cycle_room)
        in_cap = keep if in_cycle_cap else 0  # a workshop run's own cap isn't the reflection's
        outside = held if purpose in OUTSIDE_CYCLE_CAP else 0  # they count their worst case against the reserve
        money = min(
            daily_cap - outside - today - pending - keep_money + over.day, status.balance - pending - keep_money
        )
        reserve = 0
        if purpose != "last_will" and status.last_will_at is None:
            reserve = last_will_reserve(self.settings, self.db, self.life.mode) or 0
            money = min(money, status.balance - pending - reserve)
        if purpose in SERVER_TOOL_PURPOSES:  # 0.23.0: as _money_refusal judges it, SERVER_TOOL_ROOM times the hold
            room = server_tool_room(self.db, self.clock, self.simulated)
            money = min(money, int(Decimal(status.balance - pending - reserve) / room))
        return max(0, own_cap - in_cap), max(0, money)

    def reserve(
        self,
        cycle_id: int,
        purpose: str,
        request: Mapping[str, Any],
        venture_id: int | None = None,
        hold: int | None = None,
    ) -> Reservation:
        """Check and record a call before it is sent. Raises CallRefused after committing the refusal. ``hold``:
        0.33.0, what a workshop call holds (reservation with its room), never less than its worst case."""
        if not _PURPOSE.match(purpose):
            raise ValueError("bad purpose name")
        model = str(request.get("model") or "")
        plan: Plan | None = None
        problem: str | None = None
        try:
            try:
                input_tokens = self.transport.count_tokens(request)
            except Exception:  # noqa: BLE001 - e.g. the counting endpoint is rate limited
                log.warning("Counting the prompt's tokens failed; using a generous rough count", exc_info=True)
                input_tokens = rough_token_count(request)
            plan = plan_request(request, input_tokens)
        except Unpriceable as exc:
            problem = str(exc)
        except Exception as exc:  # noqa: BLE001 - a request that can't be sized is refused
            problem = f"could not size the request ({type(exc).__name__})"

        refusal: tuple[str, str] | None = None
        state: str | None = None
        with self.db.transaction() as conn:
            now = self.clock.now()
            cycle = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
            status = self.life.evaluate_and_persist()
            state = status.state
            estimate = 0
            price: ModelPrice | None = None
            geo = geo_multiplier(self.db)
            search_price = Decimal(str(self.settings.web_search_usd_per_1000))
            container_price = Decimal(str(self.settings.code_execution_usd_per_hour))
            starving = False
            refusal = self._system_refusal()
            if refusal is None and cycle is None:
                refusal = ("unknown wake cycle", "request")
            if refusal is None:
                latest = conn.execute("SELECT MAX(ts) FROM llm_calls").fetchone()[0]
                if latest and (from_iso(latest) - now).total_seconds() > CLOCK_TOLERANCE_SECONDS:
                    refusal = ("the system clock went backwards; waiting until it is past the last call", "system")
            refusal = refusal or _state_refusal(status) or self._cycle_refusal(conn, cycle, status, purpose)
            if refusal is None and problem is not None:
                refusal = (f"the request can't be priced: {problem}", "request")
            if refusal is None:
                assert plan is not None
                price = self.settings.price_for(plan.model)
                if price is None:
                    refusal = (f"model {plan.model!r} has no entry in the price table", "request")
            quote = 0
            if refusal is None:
                assert plan is not None and price is not None
                priced = self._estimate(plan, price, search_price, geo, container_price, None)
                quote = self._estimate(plan, price, search_price, geo, container_price, purpose)
                estimate = self._held(purpose, plan.model, quote)  # 0.15.0: a workshop call holds more
                if hold is not None and purpose == WORKSHOP:  # 0.33.0: as much as the day has left
                    estimate = max(quote, min(estimate, hold))
                # 0.12.0: the cycle cap counts the expected cost (a reflection may go over the caps: _allowance)
                expected, allowance = quote, Allowance()
                if purpose not in OUTSIDE_CYCLE_CAP:
                    expected, miss = self._expected(plan, price, purpose, cycle_id, quote)
                    expected = min(expected, quote)
                    allowance = self._allowance(cycle_id, purpose, miss)
                refusal, starving = self._money_refusal(cycle, status, purpose, estimate, expected, allowance, priced)
            if refusal is not None:
                call_id = None
                if cycle is not None:
                    call_id = self._insert_call(
                        conn,
                        cycle_id,
                        purpose,
                        model,
                        "refused",
                        now,
                        estimate,
                        plan,
                        None,
                        refusal[0],
                        venture_id=venture_id,
                    )
                if starving:
                    state = self.life.starve(purpose, estimate)
                events.record(
                    self.db,
                    "warning",
                    "guard",
                    f"Refused a {purpose} call: {refusal[0]}",
                    {"call_id": call_id, "cycle_id": cycle_id, "category": refusal[1]},
                )
            else:
                assert plan is not None and price is not None
                call_id = self._insert_call(
                    conn,
                    cycle_id,
                    purpose,
                    plan.model,
                    "pending",
                    now,
                    estimate,
                    plan,
                    price,
                    None,
                    search_price,
                    geo,
                    container_price,
                    venture_id=venture_id,
                )
        if refusal is not None:
            raise CallRefused(refusal[0], refusal[1], call_id, state)
        assert plan is not None and price is not None and call_id is not None
        return Reservation(
            call_id, cycle_id, plan.model, plan, price, search_price, geo, estimate, container_price, now, quote
        )

    def _system_refusal(self) -> tuple[str, str] | None:
        if not self.health.lock_held:
            return ("another Ember process is using the data folder", "system")
        if self.health.broken:
            return (f"spending is stopped after a bookkeeping error ({self.health.broken}); restart the app", "system")
        return None

    def _cycle_refusal(self, conn: Any, cycle: Any, status: LifeStatus, purpose: str) -> tuple[str, str] | None:
        if cycle is None:
            return None
        if cycle["status"] == "stopped" and cycle["note"] == OVERRUN_STOP:
            # 0.15.0: the reflection is never refused for an overrun (its worst case is a ceiling of its own, and the
            # caps and the balance still count it); the rest is, as a cap ends the work, so the cycle still reflects.
            if purpose != "reflect":
                return (f"wake cycle #{cycle['id']} was stopped: {OVERRUN_STOP}", "cap")
        elif cycle["status"] != "running":
            return (f"wake cycle #{cycle['id']} is {cycle['status']}", "request")
        if purpose != "reflect":
            # 0.15.0: a purpose that cost more than its worst case makes no more calls in this cycle
            overran = conn.execute(
                "SELECT id FROM llm_calls WHERE cycle_id = ? AND purpose = ? AND overrun = 1 LIMIT 1",
                (cycle["id"], purpose),
            ).fetchone()
            if overran is not None:
                return (
                    f"{purpose} call #{overran['id']} of this cycle cost more than its worst-case estimate; no more"
                    f" {purpose} calls until the next cycle",
                    "cap",
                )
        if bool(cycle["simulated"]) != self.simulated:
            return ("the wake cycle and the model transport are in different modes", "system")
        if cycle["life_id"] != status.life_id:
            return ("the wake cycle belongs to an earlier life", "request")
        return None

    def _estimate(
        self,
        plan: Plan,
        price: ModelPrice,
        search_price: Decimal,
        geo: Decimal,
        container_price: Decimal,
        purpose: str | None,
    ) -> int:
        """The priced worst case, at the safety factor of ``purpose`` (None: unscaled)."""
        base = worst_case_micros(plan, price, search_price, geo, container_price)
        if purpose is None:
            return base
        factor = safety_factor(self.db, plan.model, self.life.mode, purpose)
        return int((Decimal(base) * factor).to_integral_value(rounding=ROUND_CEILING))

    def _allowance(self, cycle_id: int, purpose: str, miss: int) -> Allowance:
        """What a ``purpose`` call in cycle ``cycle_id`` may go over the caps by (reads only): nothing, but a
        reflection, which always runs within the balance and the last-will reserve, the cycle cap by one cache miss
        (``miss``: 0.12.0) and by what the calls under that cap cost beyond their worst case (0.15.0), and the daily
        cap and the event reserve by what the cycle's calls of today cost beyond what they held (0.16.2: after a
        workshop run cost more than its hold, the daily cap refused the reflection); the event reserve counts the
        reflection's expected cost, so it also allows the cache miss."""
        if purpose != "reflect":
            return Allowance()
        own, today = self._overrun_excess(cycle_id)
        return Allowance(cycle=miss + own, day=today, events=miss + today)

    def _overrun_excess(self, cycle_id: int) -> tuple[int, int]:
        """(what the calls of the cycle that count toward its cap cost beyond their worst case (0.15.0; in maintenance
        every call counts toward it), what all its calls booked today cost beyond what they held (0.16.2: a workshop
        call holds more than its worst case))."""
        outside = ", ".join("?" for _ in OUTSIDE_CYCLE_CAP)
        excess = "MAX(c.floor_micros - c.estimate_micros, 0)"
        with self.db.connection() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(CASE WHEN c.purpose NOT IN ({outside}) OR y.burn_mode = ? THEN {excess} END), 0),"
                f" COALESCE(SUM(CASE WHEN c.local_day = ? THEN {excess} END), 0)"
                " FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id WHERE c.cycle_id = ? AND c.overrun = 1",
                (*OUTSIDE_CYCLE_CAP, burn.MAINTENANCE, self.clock.today().isoformat(), cycle_id),
            ).fetchone()
        return int(row[0]), int(row[1])

    def _money_refusal(
        self,
        cycle: Any,
        status: LifeStatus,
        purpose: str,
        estimate: int,
        expected: int,
        allowance: Allowance | None = None,
        priced: int | None = None,
    ) -> tuple[tuple[str, str] | None, bool]:
        """(refusal, is it starvation) for the caps, the balance and the last-will reserve: the cycle cap counts the
        call's ``expected`` cost, the rest its worst case, what it holds (``estimate``), and a reflection may go over
        the caps by its ``allowance`` (``_allowance``). 0.15.0: a workshop run's cap is checked against the request as
        priced (``priced``, without a raised safety factor: that locked the workshop at its cap after one overrun);
        the factor and what recent runs cost make it hold more of the day instead.

        0.15.0: a maintenance cycle's cap bounds every call in it, those outside the cycle cap by their worst case
        (a workshop run, the daily review, a study). Until EVENT_RESERVE_HOUR a scheduled cycle's calls leave the
        events' share of the day: the calls under the cycle cap by their expected cost, like the cycle cap that left
        it when the cycle opened, and the others by their worst case."""
        scope = self.life.scope()
        over = allowance or Allowance()
        every = cycle["burn_mode"] == burn.MAINTENANCE
        if purpose == WORKSHOP:
            run_cap = usd_cap_to_micros(self.settings.workshop_run_cap_usd)
            own = estimate if priced is None else priced
            if own > run_cap:
                return (
                    f"a workshop run may cost at most ${micros_to_usd(run_cap):.2f}"
                    f" (this call up to ${micros_to_usd(own):.4f})",
                    "cap",
                ), False
        if purpose not in OUTSIDE_CYCLE_CAP or every:
            spent, reserved = self.books.cycle_spend(cycle["id"], outside_cap=False, every_purpose=every)
            if spent + reserved + expected > cycle["cap_micros"] + over.cycle:
                return (
                    f"the cycle cap of ${micros_to_usd(cycle['cap_micros']):.2f}"
                    + (" (maintenance: every call counts)" if every else "")
                    + f" would be exceeded (spent ${micros_to_usd(spent + reserved):.4f}, this call about"
                    f" ${micros_to_usd(expected):.4f})",
                    "cap",
                ), False
        daily_cap = usd_cap_to_micros(self.settings.daily_spend_cap_usd)
        today = self.books.cap_spend_on(scope, self.clock.today())
        pending = self.books.pending(scope)
        if today + pending + estimate > daily_cap + over.day:
            return (
                f"the daily cap of ${micros_to_usd(daily_cap):.2f} would be exceeded"
                f" (today ${micros_to_usd(today + pending):.4f}, this call up to ${micros_to_usd(estimate):.4f})",
                "cap",
            ), False
        for_events = self.held_for_events(cycle["trigger"])
        outside = purpose in OUTSIDE_CYCLE_CAP
        counted, extra = (estimate, 0) if outside else (expected, over.events)
        if for_events and today + pending + counted > daily_cap - for_events + extra:
            return (
                f"${micros_to_usd(for_events):.2f} of the daily cap is kept for event wake-ups until"
                f" {EVENT_RESERVE_HOUR}:00 (today ${micros_to_usd(today + pending):.4f} of"
                f" ${micros_to_usd(daily_cap):.2f}, this call {'up to' if outside else 'about'}"
                f" ${micros_to_usd(counted):.4f})",
                "cap",
            ), False
        available = status.balance - pending
        # Uncertain charges were booked at their worst case; judged on what they really cost, the agent may
        # still afford the call. Then it is refused (the money isn't proven), but it doesn't starve.
        settled = status.settled_balance - pending
        opening = purpose in OPENING_PURPOSES
        held = (
            f"; ${micros_to_usd(settled - available):.4f} of uncertain charges may still be refunded"
            if (settled > available)
            else ""
        )
        if estimate > available:
            return (
                f"not enough money: this call can cost up to ${micros_to_usd(estimate):.4f},"
                f" ${micros_to_usd(max(available, 0)):.4f} is available{held}",
                "balance",
            ), opening and estimate > settled
        reserve = 0
        if purpose != "last_will" and status.last_will_at is None:
            reserve = last_will_reserve(self.settings, self.db, self.life.mode) or 0
            if available - estimate < reserve:
                return (
                    f"this call would dip into the ${micros_to_usd(reserve):.4f} kept back for the last will{held}",
                    "balance",
                ), opening and settled - estimate < reserve
        if purpose in SERVER_TOOL_PURPOSES:  # 0.21.0: it can cost more than it holds
            room = server_tool_room(self.db, self.clock, self.simulated)
            needed = int((Decimal(estimate) * room).to_integral_value(rounding=ROUND_CEILING))
            if available - reserve < needed:
                return (
                    f"a {purpose} call can cost more than it holds (${micros_to_usd(estimate):.4f}), so it needs"
                    f" {room:.1f} times that left above the last will's reserve;"
                    f" ${micros_to_usd(max(available, 0)):.4f} is available{held}",
                    "balance",
                ), False
        return None, False

    def _insert_call(
        self,
        conn: Any,
        cycle_id: int,
        purpose: str,
        model: str,
        status: str,
        now: Any,
        estimate: int,
        plan: Plan | None,
        price: ModelPrice | None,
        guard_reason: str | None,
        search_price: Decimal | None = None,
        geo: Decimal | None = None,
        container_price: Decimal | None = None,
        venture_id: int | None = None,
    ) -> int:
        # 0.12.0: what the call serves: its cycle's focus milestone, and the venture it names or the cycle's (its focus,
        # or its project's); overhead serves none.
        venture = milestone = None
        if purpose in WORK_PURPOSES:
            focus = conn.execute(
                "SELECT y.milestone_id, COALESCE(y.venture_id, p.venture_id) FROM cycles y"
                " LEFT JOIN projects p ON p.id = y.project_id WHERE y.id = ?",
                (cycle_id,),
            ).fetchone()
            if focus is not None:
                milestone, venture = focus[0], focus[1]
            venture = venture_id if venture_id is not None else venture
        snapshot = None
        if price is not None:
            snapshot = json.dumps(
                {
                    **{k: str(Decimal(str(v))) for k, v in price.model_dump().items() if k != "model"},
                    "web_search_usd_per_1000": str(search_price),
                    "code_execution_usd_per_hour": str(container_price),
                    "geo_multiplier": str(geo),
                    "safety_factor": str(safety_factor(self.db, price.model, self.life.mode, purpose)),
                }
            )
        cursor = conn.execute(
            "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day,"
            " estimate_micros, guard_reason, price_snapshot, plan, app_version, venture_id, milestone_id, overhead)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.boot_id,
                cycle_id,
                purpose,
                model[:100] or "?",
                1 if self.simulated else 0,
                status,
                to_iso(now),
                now.astimezone(self.clock.tz).date().isoformat(),
                estimate,
                guard_reason,
                snapshot,
                json.dumps(plan.to_json()) if plan is not None else None,
                app_version()[:40],
                venture,
                milestone,
                0 if purpose in WORK_PURPOSES else 1,
            ),
        )
        return int(cursor.lastrowid)

    # --- settling ---

    def finalize(self, reservation: Reservation, outcome: Outcome) -> CallResult:
        """Record what the call cost. Never raises; a bookkeeping failure stops all spending."""
        try:
            settled = self._settle(reservation, outcome)
        except Exception as exc:  # noqa: BLE001 - charge the worst case rather than guess
            log.exception("Could not work out the cost of call #%d", reservation.call_id)
            settled = _Settlement(
                status="interrupted",
                cost=reservation.estimate,
                floor=0,
                uncertain=True,
                error=f"cost could not be computed ({type(exc).__name__}); charged the worst case",
            )
        settled.request_id = getattr(outcome, "request_id", None)
        try:
            self._store(reservation, settled)
        except Exception as exc:  # noqa: BLE001 - spending stops until a restart
            self.health.broken = f"call #{reservation.call_id} could not be recorded: {type(exc).__name__}"
            log.exception("Could not record the cost of call #%d; model calls are stopped", reservation.call_id)
        response = outcome.response if isinstance(outcome, Completed) else None
        return CallResult(
            call_id=reservation.call_id,
            status=settled.status,
            cost_micros=settled.cost,
            estimate_micros=reservation.estimate,
            billing_uncertain=settled.uncertain,
            overrun=settled.overrun,
            response=response,
            error=settled.error,
        )

    def _settle(self, res: Reservation, outcome: Outcome) -> _Settlement:
        quote = res.estimate if res.quote is None else res.quote  # an overrun is judged by the priced worst case
        if isinstance(outcome, NotSent):
            return _Settlement("failed", 0, 0, False, error=outcome.error[:500])
        if isinstance(outcome, Rejected):
            return _Settlement("failed", 0, 0, False, error=f"HTTP {outcome.status}: {outcome.error[:480]}")
        if isinstance(outcome, Interrupted):
            partial = Usage.from_api(outcome.partial_usage or {}, _remainder_ttl(res.plan))
            if partial == Usage():
                partial = Usage(input_tokens=res.plan.input_tokens)
            known = cost_micros(partial, res.price, res.search_price, res.geo)
            # Charge the worst case, or what is already known to be billed if that is more (then the
            # estimate was wrong: handled like any other overrun).
            return _Settlement(
                "interrupted",
                max(res.estimate, known),
                known,
                True,
                usage=partial,
                usage_raw=outcome.partial_usage,
                error=outcome.error[:500],
                overrun=known > quote,
                expected=quote,
            )

        response = outcome.response
        raw = response.get("usage")
        notes: list[str] = []
        usage_read = isinstance(raw, Mapping) and bool(raw.get("input_tokens")) and "output_tokens" in raw
        if not usage_read:
            # Every answer reports its usage; without it the bill is unknown (a transport bug).
            notes.append("usage missing or incomplete")
            raw = raw if isinstance(raw, Mapping) else {}
        usage = Usage.from_api(raw, _remainder_ttl(res.plan))
        iterations = raw.get("iterations")
        samplings = None
        if isinstance(iterations, list) and iterations:
            summed = Usage()
            # 0.15.0: how many times the API sampled the model in the call (a server tool's loop), kept with the call.
            # The SDK (1.8.0) passes them on as a list of entries; a sampling's has type "message".
            samplings = sum(1 for i in iterations if isinstance(i, Mapping) and i.get("type") in (None, "message"))
            for item in iterations:
                if not isinstance(item, Mapping):
                    notes.append("unreadable usage iteration")
                    continue
                if item.get("type") not in (None, "message") or item.get("model") not in (None, res.model):
                    notes.append(f"usage iteration of type {item.get('type')!r} on {item.get('model') or res.model}")
                summed = summed.plus(Usage.from_api(item, _remainder_ttl(res.plan)))
            usage = Usage(**{k: max(v, getattr(summed, k)) for k, v in asdict(usage).items()})
        for key, value in raw.items():
            if key not in _KNOWN_USAGE_KEYS and value not in (None, 0, {}, [], ""):
                notes.append(f"unknown usage field {key}")
        for key, value in (raw.get("server_tool_use") or {}).items():
            if key not in _KNOWN_SERVER_TOOLS and value:
                notes.append(f"unpriced server tool use {key}")
        tier = raw.get("service_tier")
        if tier not in (None, "standard"):
            notes.append(f"service tier {tier}")
        geo_value = raw.get("inference_geo")
        multiplier = res.geo
        us_inference = False
        if geo_value == "us":
            multiplier = US_INFERENCE_MULTIPLIER
            us_inference = True
        elif geo_value not in (None, "", *_STANDARD_GEOS):
            multiplier = US_INFERENCE_MULTIPLIER
            notes.append(f"inference region {geo_value}")
        response_model = response.get("model")
        if isinstance(response_model, str) and response_model and not _same_model(response_model, res.model):
            notes.append(f"answered by {response_model} instead of {res.model}")

        known = cost_micros(usage, res.price, res.search_price, multiplier)
        if usage.code_execution_requests or isinstance(response.get("container"), Mapping):
            known += container_micros(self._container_minutes(res), res.container_price)
        # A multiplier first learned from this answer (US-only inference) isn't an estimation error.
        expected = int((Decimal(quote) * multiplier / res.geo).to_integral_value(rounding=ROUND_CEILING))
        uncertain = bool(notes)
        # 0.15.0: an uncertain answer (an unknown usage field, say) is charged at its worst case; one without its usage
        # at what it held, as a workshop run's worst case is no ceiling
        cost = max(known, quote if usage_read else res.estimate) if uncertain else known
        return _Settlement(
            "ok",
            cost,
            min(known, cost),
            uncertain,
            usage=usage,
            usage_raw=raw,
            response_model=response_model if isinstance(response_model, str) else None,
            stop_reason=str(response.get("stop_reason") or "")[:50] or None,
            message_id=str(response.get("id") or "")[:100] or None,
            service_tier=str(tier)[:30] if tier else None,
            inference_geo=str(geo_value)[:30] if geo_value else None,
            notes=notes,
            overrun=known > expected,
            us_inference=us_inference,
            expected=expected,
            iterations=samplings,
        )

    def _container_minutes(self, res: Reservation) -> int:
        """Container time to book for a call that ran code: how long the call took, at least the 5 minutes
        Anthropic bills at least (the first 1,550 hours a month are free, so this is an upper bound)."""
        elapsed = (self.clock.now() - res.started).total_seconds() if res.started is not None else 0
        return max(CONTAINER_MINIMUM_MINUTES, math.ceil(elapsed / 60))

    def _store(self, res: Reservation, s: _Settlement) -> None:
        now = self.clock.now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT local_day, status, purpose FROM llm_calls WHERE id = ?", (res.call_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise RuntimeError(f"call #{res.call_id} is not pending")
            error = s.error or ("; ".join(s.notes) if s.notes else None)
            conn.execute(
                "UPDATE llm_calls SET status = ?, finished_at = ?, cost_micros = ?, floor_micros = ?,"
                " billing_uncertain = ?, input_tokens = ?, output_tokens = ?, cache_write_5m_tokens = ?,"
                " cache_write_1h_tokens = ?, cache_read_tokens = ?, web_search_requests = ?,"
                " web_fetch_requests = ?, response_model = ?, stop_reason = ?, message_id = ?, service_tier = ?,"
                " inference_geo = ?, error = ?, usage_raw = ?, request_id = ?, iterations = ?, overrun = ?"
                " WHERE id = ? AND status = 'pending'",
                (
                    s.status,
                    to_iso(now),
                    s.cost,
                    s.floor,
                    1 if s.uncertain else 0,
                    s.usage.input_tokens,
                    s.usage.output_tokens,
                    s.usage.cache_write_5m_tokens,
                    s.usage.cache_write_1h_tokens,
                    s.usage.cache_read_tokens,
                    s.usage.web_search_requests,
                    s.usage.web_fetch_requests,
                    s.response_model,
                    s.stop_reason,
                    s.message_id,
                    s.service_tier,
                    s.inference_geo,
                    error,
                    json.dumps(s.usage_raw, default=str) if s.usage_raw is not None else None,
                    (s.request_id or "")[:100] or None,
                    s.iterations,
                    1 if s.overrun else 0,
                    res.call_id,
                ),
            )
            if s.status in ("ok", "interrupted"):
                conn.execute(
                    "INSERT INTO ledger (ts, occurred_on, type, amount_micros, simulated, llm_call_id, created_by)"
                    " VALUES (?, ?, 'api_cost', ?, ?, ?, 'system')",
                    (to_iso(now), row["local_day"], s.cost, 1 if self.simulated else 0, res.call_id),
                )
            if s.status in ("failed", "interrupted"):
                charged = f"; charged ${micros_to_usd(s.cost):.4f} (worst case)" if s.cost else "; nothing charged"
                events.record(
                    self.db,
                    "warning",
                    "economy",
                    f"Call #{res.call_id} ({res.model}) {s.status}: {s.error or 'no answer'}{charged}",
                    {"call_id": res.call_id, "request_id": s.request_id},
                )
            if s.us_inference and mark_us_inference(self.db):
                events.record(
                    self.db,
                    "warning",
                    "economy",
                    "The Anthropic account runs US-only inference (1.1x token prices); costs and limits now include it",
                )
            if s.notes:
                events.record(
                    self.db,
                    "warning",
                    "economy",
                    f"Call #{res.call_id} was charged at its worst case because its bill is uncertain: "
                    + "; ".join(s.notes),
                )
            purpose = row["purpose"]
            today = self.clock.today()
            if s.overrun:
                factor = raise_safety_factor(self.db, res.model, s.floor, s.expected, self.life.mode, purpose, today)
                over = f"Call #{res.call_id} cost ${micros_to_usd(s.floor):.4f}, more than its worst-case estimate"
                over += f" ${micros_to_usd(s.expected):.4f}"
                scaled = f"estimates for {purpose} calls on {res.model} are now scaled by {factor}"
                excess = s.floor - s.expected
                small = excess <= s.expected * OVERRUN_TOLERANCE and excess <= OVERRUN_TOLERANCE_MICROS
                if purpose in OUTSIDE_CYCLE_CAP or small:  # 0.15.0: the cycle goes on
                    events.record(
                        self.db,
                        "warning",
                        "economy",
                        f"{over}. The wake cycle goes on without further {purpose} calls, and {scaled}.",
                    )
                else:
                    conn.execute(
                        "UPDATE cycles SET status = 'stopped', ended_at = ?, note = ?"
                        " WHERE id = ? AND status = 'running'",
                        (to_iso(now), OVERRUN_STOP, res.cycle_id),
                    )
                    events.record(
                        self.db,
                        "error",
                        "economy",
                        f"{over}. The wake cycle was stopped (its reflection may still run) and {scaled}.",
                    )
            elif s.status == "ok" and not s.uncertain:
                # 0.12.0: a raised factor comes down again after calls that didn't need it.
                factor = safety_factor(self.db, res.model, self.life.mode, purpose)
                if s.floor * factor <= s.expected:
                    lowered = note_accurate_call(self.db, res.model, self.life.mode, purpose, today)
                    if lowered is not None:
                        events.record(
                            self.db,
                            "info",
                            "economy",
                            f"Estimates for {purpose} calls on {res.model} are now scaled by {lowered}: the last"
                            " calls cost no more than their unscaled estimate.",
                        )
            self.life.evaluate_and_persist()


def _state_refusal(status: LifeStatus) -> tuple[str, str] | None:
    if status.can_run:
        return None
    return (f"the agent is {status.state}", "state")


def _same_model(answered: str, requested: str) -> bool:
    """An alias (claude-haiku-4-5) is answered by its dated snapshot (claude-haiku-4-5-20251001)."""
    return answered == requested or (
        answered.startswith(requested) and bool(_SNAPSHOT_SUFFIX.match(answered[len(requested) :]))
    )


def _remainder_ttl(plan: Plan) -> str:
    return "1h" if "1h" in plan.cache_ttls else "5m"


def lock_path(data_dir: Path) -> Path:
    return data_dir / "ember.lock"
