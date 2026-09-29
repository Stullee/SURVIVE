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
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol

from .. import events
from ..config import ModelPrice, Settings
from ..db import Database
from ..version import app_version
from .clock import Clock, from_iso, to_iso
from .costs import MICROS_PER_USD, Usage, container_micros, cost_micros, micros_to_usd
from .estimate import CONTAINER_MINIMUM_MINUTES, Plan, Unpriceable, plan_request, worst_case_micros
from .ledger import Books
from .life import Life, LifeStatus
from .pricing import (
    US_INFERENCE_MULTIPLIER,
    geo_multiplier,
    last_will_reserve,
    mark_us_inference,
    note_accurate_call,
    raise_safety_factor,
    safety_factor,
)

log = logging.getLogger(__name__)

# How a wake cycle can end ("stopped" is also set by the guard after an overrun).
CYCLE_END_STATUSES = frozenset({"completed", "idle", "refused", "failed", "stopped", "interrupted"})
# Calls that open a wake cycle; refusing one of them for lack of money is starvation.
OPENING_PURPOSES = frozenset({"plan", "last_will"})
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
REVIEW = "review"
STUDY = "study"  # 0.12.0: Ember studying its owner's library, within the owner's daily study budget
OUTSIDE_CYCLE_CAP = (WORKSHOP, REVIEW, STUDY)
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
    estimate: int
    container_price: Decimal = Decimal(0)  # USD per hour of a code execution container
    started: datetime | None = None


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
        status = self.life.evaluate_and_persist()
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
                        " session, app_version) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                        (
                            status.life_id,
                            self.boot_id,
                            to_iso(self.clock.now()),
                            trigger,
                            1 if self.simulated else 0,
                            usd_cap_to_micros(self.settings.cycle_spend_cap_usd),
                            self.life.session(),
                            app_version()[:40],
                        ),
                    ).lastrowid
                )
        if refusal is not None:
            raise CallRefused(refusal[0], refusal[1], state=status.state)
        return cycle_id

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

    def call(self, cycle_id: int, purpose: str, request: Mapping[str, Any]) -> CallResult:
        """Reserve, send and settle one model request. Raises CallRefused or CallFailed."""
        with self.db.connection() as conn:
            if conn.in_transaction:
                # The reservation must be committed before the request is sent, and nothing may hold the
                # database while waiting for the API.
                raise RuntimeError("model calls must not run inside a database transaction")
        # The request that is priced is exactly the request that is sent.
        frozen = copy.deepcopy(dict(request))
        reservation = self.reserve(cycle_id, purpose, frozen)
        try:
            outcome = self.transport.send(frozen)
        except Exception as exc:  # noqa: BLE001 - a transport bug must not lose the reservation
            log.exception("Model transport failed")
            outcome = Interrupted(f"transport error: {type(exc).__name__}")
        result = self.finalize(reservation, outcome)
        if result.status != "ok":
            raise CallFailed(result)
        return result

    def quote(self, request: Mapping[str, Any], purpose: str = "work") -> int:
        """The worst case the guard would reserve for ``request`` as a ``purpose`` call now (reads only). Raises
        Unpriceable."""
        try:
            input_tokens = self.transport.count_tokens(request)
        except Exception:  # noqa: BLE001 - same fallback as reserve()
            input_tokens = rough_token_count(request)
        plan = plan_request(request, input_tokens)
        price = self.settings.price_for(plan.model)
        if price is None:
            raise Unpriceable(f"model {plan.model!r} has no entry in the price table")
        return self._estimate(
            plan,
            price,
            Decimal(str(self.settings.web_search_usd_per_1000)),
            geo_multiplier(self.db),
            Decimal(str(self.settings.code_execution_usd_per_hour)),
            purpose,
        )

    def headroom(self, cycle_id: int, purpose: str = "work", keep: int = 0) -> int:
        """How much the next call of ``purpose`` in this cycle may cost: the tightest of the cycle cap, the daily
        cap and the balance (keeping the last-will reserve unless the will is written or this is the will), less
        ``keep`` of each that also limits a later call (0.12.0: the reflection's reserve)."""
        status = self.life.evaluate()
        scope = self.life.scope()
        with self.db.connection() as conn:
            cycle = conn.execute("SELECT cap_micros FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if cycle is None:
            return 0
        spent, reserved = self.books.cycle_spend(cycle_id, outside_cap=False)
        pending = self.books.pending(scope)
        daily_cap = usd_cap_to_micros(self.settings.daily_spend_cap_usd)
        today = self.books.cap_spend_on(scope, self.clock.today())
        if purpose == WORKSHOP:
            own_cap = usd_cap_to_micros(self.settings.workshop_run_cap_usd)
        elif purpose in (REVIEW, STUDY):
            own_cap = daily_cap  # only the daily cap and the balance limit it (a study also its own budget: loop.py)
        else:
            own_cap = cycle["cap_micros"] - spent - reserved
        in_cap = keep if purpose not in OUTSIDE_CYCLE_CAP else 0  # a workshop run's own cap isn't the reflection's
        room = min(own_cap - in_cap, daily_cap - today - pending - keep, status.balance - pending - keep)
        if purpose != "last_will" and status.last_will_at is None:
            room = min(
                room, status.balance - pending - (last_will_reserve(self.settings, self.db, self.life.mode) or 0)
            )
        return max(0, room)

    def reserve(self, cycle_id: int, purpose: str, request: Mapping[str, Any]) -> Reservation:
        """Check and record a call before it is sent. Raises CallRefused after committing the refusal."""
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
            refusal = refusal or _state_refusal(status) or self._cycle_refusal(cycle, status)
            if refusal is None and problem is not None:
                refusal = (f"the request can't be priced: {problem}", "request")
            if refusal is None:
                assert plan is not None
                price = self.settings.price_for(plan.model)
                if price is None:
                    refusal = (f"model {plan.model!r} has no entry in the price table", "request")
            if refusal is None:
                assert plan is not None and price is not None
                estimate = self._estimate(plan, price, search_price, geo, container_price, purpose)
                refusal, starving = self._money_refusal(cycle, status, purpose, estimate)
            if refusal is not None:
                call_id = None
                if cycle is not None:
                    call_id = self._insert_call(
                        conn, cycle_id, purpose, model, "refused", now, estimate, plan, None, refusal[0]
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
                )
        if refusal is not None:
            raise CallRefused(refusal[0], refusal[1], call_id, state)
        assert plan is not None and price is not None and call_id is not None
        return Reservation(
            call_id, cycle_id, plan.model, plan, price, search_price, geo, estimate, container_price, now
        )

    def _system_refusal(self) -> tuple[str, str] | None:
        if not self.health.lock_held:
            return ("another Ember process is using the data folder", "system")
        if self.health.broken:
            return (f"spending is stopped after a bookkeeping error ({self.health.broken}); restart the app", "system")
        return None

    def _cycle_refusal(self, cycle: Any, status: LifeStatus) -> tuple[str, str] | None:
        if cycle is None:
            return None
        if cycle["status"] != "running":
            return (f"wake cycle #{cycle['id']} is {cycle['status']}", "request")
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
        purpose: str,
    ) -> int:
        base = worst_case_micros(plan, price, search_price, geo, container_price)
        factor = safety_factor(self.db, plan.model, self.life.mode, purpose)
        return int((Decimal(base) * factor).to_integral_value(rounding=ROUND_CEILING))

    def _money_refusal(
        self, cycle: Any, status: LifeStatus, purpose: str, estimate: int
    ) -> tuple[tuple[str, str] | None, bool]:
        """(refusal, is it starvation) for the caps, the balance and the last-will reserve."""
        scope = self.life.scope()
        if purpose == WORKSHOP:
            run_cap = usd_cap_to_micros(self.settings.workshop_run_cap_usd)
            if estimate > run_cap:
                return (
                    f"a workshop run may cost at most ${micros_to_usd(run_cap):.2f}"
                    f" (this call up to ${micros_to_usd(estimate):.4f})",
                    "cap",
                ), False
        elif purpose not in OUTSIDE_CYCLE_CAP:
            spent, reserved = self.books.cycle_spend(cycle["id"], outside_cap=False)
            if spent + reserved + estimate > cycle["cap_micros"]:
                return (
                    f"the cycle cap of ${micros_to_usd(cycle['cap_micros']):.2f} would be exceeded"
                    f" (spent ${micros_to_usd(spent + reserved):.4f}, this call up to ${micros_to_usd(estimate):.4f})",
                    "cap",
                ), False
        daily_cap = usd_cap_to_micros(self.settings.daily_spend_cap_usd)
        today = self.books.cap_spend_on(scope, self.clock.today())
        pending = self.books.pending(scope)
        if today + pending + estimate > daily_cap:
            return (
                f"the daily cap of ${micros_to_usd(daily_cap):.2f} would be exceeded"
                f" (today ${micros_to_usd(today + pending):.4f}, this call up to ${micros_to_usd(estimate):.4f})",
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
        if purpose != "last_will" and status.last_will_at is None:
            reserve = last_will_reserve(self.settings, self.db, self.life.mode) or 0
            if available - estimate < reserve:
                return (
                    f"this call would dip into the ${micros_to_usd(reserve):.4f} kept back for the last will{held}",
                    "balance",
                ), opening and settled - estimate < reserve
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
    ) -> int:
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
            " estimate_micros, guard_reason, price_snapshot, plan, app_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                overrun=known > res.estimate,
                expected=res.estimate,
            )

        response = outcome.response
        raw = response.get("usage")
        notes: list[str] = []
        if not isinstance(raw, Mapping) or not raw.get("input_tokens") or "output_tokens" not in raw:
            # Every answer reports its usage; without it the bill is unknown (a transport bug).
            notes.append("usage missing or incomplete")
            raw = raw if isinstance(raw, Mapping) else {}
        usage = Usage.from_api(raw, _remainder_ttl(res.plan))
        iterations = raw.get("iterations")
        if isinstance(iterations, list) and iterations:
            summed = Usage()
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
        expected = int((Decimal(res.estimate) * multiplier / res.geo).to_integral_value(rounding=ROUND_CEILING))
        uncertain = bool(notes)
        cost = max(known, res.estimate) if uncertain else known
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
                " inference_geo = ?, error = ?, usage_raw = ?, request_id = ? WHERE id = ? AND status = 'pending'",
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
            if s.overrun:
                factor = raise_safety_factor(self.db, res.model, s.floor, s.expected, self.life.mode, purpose)
                conn.execute(
                    "UPDATE cycles SET status = 'stopped', ended_at = ?, note = ? WHERE id = ? AND status = 'running'",
                    (to_iso(now), "a call cost more than its worst-case estimate", res.cycle_id),
                )
                events.record(
                    self.db,
                    "error",
                    "economy",
                    f"Call #{res.call_id} cost ${micros_to_usd(s.floor):.4f}, more than its worst-case estimate"
                    f" ${micros_to_usd(s.expected):.4f}. The wake cycle was stopped and estimates for {purpose}"
                    f" calls on {res.model} are now scaled by {factor}.",
                )
            elif s.status == "ok" and not s.uncertain:
                # 0.12.0: a raised factor comes down again after calls that didn't need it.
                factor = safety_factor(self.db, res.model, self.life.mode, purpose)
                if s.floor * factor <= s.expected:
                    lowered = note_accurate_call(self.db, res.model, self.life.mode, purpose)
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
