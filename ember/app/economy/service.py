"""The economy as the web routes and the scheduler see it.

Owner entries are validated, checked for replays and for surprises (an
unusually large amount, or a change that would make the agent critical or kill
it), and only then written, together with the life-state evaluation, in one
transaction. The ledger's writers are this module (owner entries) and the
budget guard (API costs); nothing else writes money.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .. import events
from ..config import LoadedSettings
from ..db import Database
from ..logging_setup import printable
from .clock import Clock, from_iso
from .costs import micros_to_usd
from .ledger import (
    STATE_SEVERITY,
    Books,
    DuplicateKeyMismatch,
    EntryError,
    PreparedEntry,
    Scope,
    confirmations,
)
from .life import KILLED_KEY, PAUSED_KEY, RUNWAY_CAP_DAYS, Life, LifeStatus, mode_of
from .metering import MeteredModel, MeterHealth, ProcessLock, Transport, recover_interrupted, usd_cap_to_micros
from .pricing import opening_cost

log = logging.getLogger(__name__)

TYPE_LABELS = {
    "owner_grant": "a grant",
    "revenue": "revenue",
    "expense": "an expense",
    "adjustment": "an adjustment",
    "api_cost_correction": "an API cost correction",
}


@dataclass(frozen=True)
class Reply:
    """An HTTP status and JSON body for an owner request."""

    status: int
    body: dict[str, Any]


class Economy:
    def __init__(
        self,
        db: Database,
        loaded: LoadedSettings,
        clock: Clock | None = None,
        lock: ProcessLock | None = None,
        boot_id: str | None = None,
    ) -> None:
        self.db = db
        self.loaded = loaded
        self.settings = loaded.settings
        self.clock = clock or Clock()
        self.mode = mode_of(self.settings)
        self.books = Books(db, self.clock)
        self.life = Life(db, self.settings, self.clock, self.books, self.mode)
        self.lock = lock
        self.boot_id = boot_id or uuid.uuid4().hex
        self.health = MeterHealth()

    # --- startup and the periodic check ---

    def start(self) -> LifeStatus:
        self.health.lock_held = self.lock.acquire() if self.lock is not None else True
        if not self.health.lock_held:
            events.record(
                self.db,
                "error",
                "economy",
                "Another Ember process is using the data folder; model calls are refused until it stops",
            )
        else:
            recover_interrupted(self.db, self.clock, self.boot_id)
        if self.loaded.safe_mode:
            log.info("Safe mode: the starting balance is not recorded until the options are valid")
        else:
            entry_id = self.books.record_starting_grant(self.settings.starting_balance_usd)
            if entry_id is not None:
                events.record(
                    self.db,
                    "info",
                    "ledger",
                    f"Recorded the starting balance of ${self.settings.starting_balance_usd:.2f} from the options",
                    {"entry_id": entry_id},
                )
        for warning in self.settings.price_warnings():
            events.record(self.db, "warning", "config", f"Price looks too low: {warning}")
        return self.life.start()

    def stop(self) -> None:
        self.health.lock_held = False  # nothing in this process may spend once the lock is given up
        if self.lock is not None:
            self.lock.release()

    def tick(self) -> LifeStatus:
        """Re-evaluate the life state (runway changes with time, not only with money)."""
        return self.life.evaluate_and_persist()

    def metered(self, transport: Transport) -> MeteredModel:
        return MeteredModel(
            self.db, self.settings, self.clock, self.books, self.life, transport, self.boot_id, self.health
        )

    # --- owner requests ---

    def record(self, kind: str, body: Any, entered_by: str | None = None) -> Reply:
        try:
            prepared = self.books.prepare(kind, body, self.mode, entered_by)
            confirm_state, confirm_large = confirmations(body)
        except EntryError as exc:
            return Reply(422, {"error": str(exc), "field": exc.field})
        return self._write(prepared, confirm_state, confirm_large)

    def correct(self, entry_id: int, body: Any, entered_by: str | None = None) -> Reply:
        try:
            prepared = self.books.prepare_correction(entry_id, body, self.life.scope(), entered_by)
            confirm_state, confirm_large = confirmations(body)
        except EntryError as exc:
            status = 404 if exc.field == "id" and str(exc) == "no such entry" else 422
            return Reply(status, {"error": str(exc), "field": exc.field})
        return self._write(prepared, confirm_state, confirm_large)

    def _write(self, prepared: PreparedEntry, accepted: str | None, confirm_large: bool) -> Reply:
        # In dry run, a real (not test-money) entry also moves the sleeping live agent's balance.
        lives = [self.life]
        if self.mode == "dry_run" and not prepared.simulated:
            lives.append(Life(self.db, self.settings, self.clock, self.books, "live"))
        with self.db.transaction() as conn:
            try:
                existing = self.books.existing(prepared)
            except DuplicateKeyMismatch as exc:
                return Reply(409, {"error": str(exc), "code": "duplicate_key_mismatch"})
            if existing is not None:
                return Reply(200, {"entry": existing, "economy": self._summary(self.life.evaluate()), "replay": True})
            if not confirm_large:
                large, typical = self.books.unusually_large(prepared)
                if large:
                    return Reply(
                        409,
                        {
                            "error": "this amount is much larger than usual; please confirm",
                            "code": "unusually_large",
                            "amount_usd": micros_to_usd(abs(prepared.amount_micros)),
                            "typical_usd": micros_to_usd(typical) if typical is not None else None,
                        },
                    )
            befores = [life.evaluate() for life in lives]
            conn.execute("SAVEPOINT owner_entry")
            try:
                entry_id = self.books.insert(conn, prepared)
            except sqlite3.IntegrityError:
                # Another request changed the same entry in the meantime (the database refused this one).
                conn.execute("ROLLBACK TO owner_entry")
                conn.execute("RELEASE owner_entry")
                changed = {"error": "the entry was changed meanwhile; reload and try again", "code": "conflict"}
                return Reply(409, changed)
            afters = [life.evaluate() for life in lives]
            for life, before, after in zip(lives, befores, afters, strict=True):
                worse = _worse_state(before, after)
                if worse is None or (accepted is not None and STATE_SEVERITY[accepted] >= STATE_SEVERITY[worse]):
                    continue
                conn.execute("ROLLBACK TO owner_entry")
                conn.execute("RELEASE owner_entry")
                whose = "the live agent's" if life is not self.life else "the agent's"
                return Reply(
                    409,
                    {
                        "error": f"this would change {whose} state from {before.state} to {worse}",
                        "code": "would_change_state",
                        "mode": life.mode,
                        "state_before": before.state,
                        "state_after": worse,
                        "balance_after_usd": micros_to_usd(after.balance),
                    },
                )
            conn.execute("RELEASE owner_entry")
            events.record(self.db, "info", "ledger", _describe(prepared, entry_id), {"entry_id": entry_id})
            status = self.life.evaluate_and_persist()
            for life in lives[1:]:
                life.persist_if_dead()  # a confirmed death of the sleeping live agent is recorded now, not later
        return Reply(201, {"entry": self.books.entry(entry_id), "economy": self._summary(status)})

    def set_paused(self, paused: bool, who: str | None = None) -> LifeStatus:
        status = self.life.set_switch(PAUSED_KEY, paused)
        events.record(self.db, "info", "control", f"{who or 'The owner'} {'paused' if paused else 'resumed'} the agent")
        return status

    # --- read models ---

    def status(self) -> LifeStatus:
        return self.life.evaluate()

    def _summary(self, status: LifeStatus) -> dict[str, Any]:
        return {
            "state": status.state,
            "balance_usd": micros_to_usd(status.balance),
            "runway_days": _round(status.runway.days),
        }

    def warnings(self) -> list[str]:
        result: list[str] = []
        if not self.health.lock_held:
            result.append("Another Ember process is using the data folder; model calls are refused.")
        if self.health.broken:
            result.append(f"Spending is stopped after a bookkeeping error: {self.health.broken}. Restart the app.")
        status = self.life.evaluate()
        held = status.settled_balance - status.balance
        if held > 0 and self.mode == "live":  # simulated charges have no Console to check against
            result.append(
                f"${micros_to_usd(held):.2f} of interrupted calls was charged at the worst case. Check the real cost in"
                " the Anthropic Console and record the difference as an API cost correction (decrease)."
            )
        opening = opening_cost(self.settings, self.db, self.mode)
        if opening is not None:
            for label, cap in (
                ("cycle spend cap", self.settings.cycle_spend_cap_usd),
                ("daily spend cap", self.settings.daily_spend_cap_usd),
            ):
                if usd_cap_to_micros(cap) < opening:
                    result.append(
                        f"The {label} (${cap:.2f}) is below one planning call (up to ${micros_to_usd(opening):.2f}),"
                        " so the agent can't start a wake cycle."
                    )
        return result

    def dashboard(self) -> dict[str, Any]:
        status = self.life.evaluate()
        scope = self.life.scope()
        now = self.clock.now()
        live_balance = self.books.balance(Scope("live")) if self.mode == "dry_run" else status.balance
        with self.db.connection() as conn:
            running = conn.execute("SELECT 1 FROM cycles WHERE status = 'running' LIMIT 1").fetchone() is not None
            last_wake = conn.execute(
                "SELECT MAX(started_at) FROM cycles WHERE life_id = ?", (status.life_id or 0,)
            ).fetchone()[0]
            transitions = conn.execute(
                "SELECT ts, mode, life_id, from_state, to_state, reason FROM life_transitions WHERE mode = ?"
                " ORDER BY id DESC LIMIT 20",
                (self.mode,),
            ).fetchall()
        revive = None
        if status.revive_needed is not None:
            revive = {
                "needed_usd": micros_to_usd(status.revive_needed),
                "suggested_usd": micros_to_usd(status.revive_suggested or status.revive_needed),
            }
        agent = {
            "name": self.settings.agent_name,
            "mode": self.mode,
            "life_id": status.life_id,
            "state": status.state,
            "state_reason": status.reason,
            "critical": status.critical,
            "born_at": status.born_at,
            "age_days": _age_days(status.born_at, status.ended_at, now),
            "paused": self.life.flag(PAUSED_KEY),
            "killed": self.life.flag(KILLED_KEY),
            "balance_usd": micros_to_usd(status.balance),
            "real_balance_usd": micros_to_usd(live_balance),
            "pending_usd": micros_to_usd(status.pending),
            "runway_days": _round(status.runway.days),
            "runway_note": status.runway.note,
            "today_spend_usd": micros_to_usd(self.books.cap_spend_on(scope, self.clock.today())),
            "daily_cap_usd": self.settings.daily_spend_cap_usd,
            "cycle_cap_usd": self.settings.cycle_spend_cap_usd,
            "last_wake_at": last_wake,
            "next_wake_at": None,
            "cycle_running": running,
            "last_will_due": status.last_will_due,
            "revive": revive,
            "warnings": self.warnings(),
        }
        totals = self.books.totals(scope)
        live_totals = self.books.totals(Scope("live"))
        economy = {
            "days": self.books.day_series(scope),
            "totals": {f"{k}_usd": micros_to_usd(v) for k, v in totals.items()},
            "self_sufficiency_ratio": _ratio(live_totals),
            "simulated_self_sufficiency_ratio": _ratio(totals) if self.mode == "dry_run" else None,
            "simulated_note": (
                "Dry run: simulated API costs and test money from this session are included; the live balance is"
                " unchanged."
                if self.mode == "dry_run"
                else None
            ),
        }
        return {
            "agent": agent,
            "economy": economy,
            "ledger": {"entries": self.books.entries(scope, 20)},
            "memorial": self._memorial(status, scope) if status.state == "dead" else None,
            "lives": self.life.previous_lives(),
            "transitions": [dict(row) for row in transitions],
        }

    def _memorial(self, status: LifeStatus, scope: Scope) -> dict[str, Any]:
        with self.db.connection() as conn:
            cycles = conn.execute("SELECT COUNT(*) FROM cycles WHERE life_id = ?", (status.life_id,)).fetchone()[0]
        totals = self.books.totals(scope, since=status.born_at, until=status.ended_at)
        return {
            "life_id": status.life_id,
            "mode": self.mode,
            "simulated": self.mode == "dry_run",
            "name": self.settings.agent_name,
            "born_at": status.born_at,
            "died_at": status.ended_at,
            "lifespan_days": _age_days(status.born_at, status.ended_at, self.clock.now()),
            "reason": status.reason,
            "cycles": int(cycles),
            "total_cost_usd": micros_to_usd(totals["api_cost"] + totals["expense"]),
            "total_revenue_usd": micros_to_usd(totals["revenue"]),
            "last_will": None,
        }

    def sensors(self) -> dict[str, Any]:
        status = self.life.evaluate()
        runway = status.runway.days
        if status.state == "dead":
            runway = 0.0
        return {
            "name": self.settings.agent_name,
            "state": status.state,
            "mode": self.mode,
            "safe_mode": self.loaded.safe_mode,
            "balance_usd": round(micros_to_usd(status.balance), 2),
            "runway_days": round(min(runway, RUNWAY_CAP_DAYS), 1) if runway is not None else RUNWAY_CAP_DAYS,
            "runway_known": runway is not None,
            "today_api_spend_usd": round(
                micros_to_usd(self.books.cap_spend_on(self.life.scope(), self.clock.today())), 2
            ),
            "daily_cap_usd": self.settings.daily_spend_cap_usd,
        }


def _worse_state(before: LifeStatus, after: LifeStatus) -> str | None:
    """The worse state an entry would bring (dead, unfunded or critical), or None if it wouldn't."""
    if after.state == "dead" and before.state != "dead":
        return "dead"
    if after.state == "unfunded" and before.state not in ("unfunded", "dead"):
        return "unfunded"
    if after.critical and not before.critical and after.state != "dead":
        return "critical"
    return None


def _describe(prepared: PreparedEntry, entry_id: int) -> str:
    who = printable(prepared.entered_by, 60) if prepared.entered_by else "The owner"
    amount = f"${micros_to_usd(abs(prepared.amount_micros)):.2f}"
    if prepared.corrects_id is not None:
        text = f"{who} corrected entry #{prepared.corrects_id} by {amount}"
    else:
        label = TYPE_LABELS.get(prepared.type, prepared.type)
        sign = "" if prepared.amount_micros > 0 else "negative "
        text = f"{who} recorded {sign}{label} of {amount}"
    if prepared.simulated:
        text += " (test money)"
    return f"{text} (entry #{entry_id})"


def _age_days(born_at: str | None, ended_at: str | None, now: datetime) -> float | None:
    if not born_at:
        return None
    end = from_iso(ended_at) if ended_at else now
    return round(max(0.0, (end - from_iso(born_at)).total_seconds() / 86_400), 2)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _ratio(totals: dict[str, int]) -> float | None:
    cost = totals["api_cost"] + totals["expense"]
    return round(totals["revenue"] / cost, 3) if cost > 0 else None
