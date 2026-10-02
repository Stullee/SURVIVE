"""Life states, runway, death and revival.

Each mode (live, dry run) has its own lives; the current life is the newest row
in ``lives``. The state is derived from the books and two owner switches, in
this order of precedence:

    dead > killed > paused > unfunded > critical > alive

* dead: the settled balance reached zero after the agent had spent money, or
  it starved (see :meth:`Life.starve`). A dead life never changes again; an
  owner grant large enough for a fresh start begins a new life ("revival").
* killed / paused: the owner's switches (the kill switch UI arrives in phase 4).
  0.15.0: Ember's code also pauses the agent when a fact from Etsy's numbers
  (a refund, a listing fee) leaves it without money; while it stays paused
  that isn't death, so the owner decides.
* unfunded: nothing to spend and nothing spent yet; the agent waits for money.
* critical: less than 2 days of runway. It ends only when runway is back to 4
  days or more *and* money came in since it started, so the state doesn't
  flicker around the threshold. The first time a life turns critical, it is
  due to write its last will (phase 3 does the writing).

``dormant`` and ``ended`` are bookkeeping states: a live life sleeps while the
app runs in dry run, and a dry-run life ends with its test session.

Evaluation is read-only (:meth:`Life.evaluate`) or atomic and write-on-change
(:meth:`Life.evaluate_and_persist`), which records every state change in
``life_transitions``; runway measures only the time the agent was active.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .. import events
from ..config import Settings
from ..db import Database
from .clock import Clock, from_iso, to_iso
from .costs import MICROS_PER_USD, micros_to_usd
from .ledger import Books, Scope
from .pricing import last_will_reserve, opening_cost

log = logging.getLogger(__name__)

ACTIVE_STATES = frozenset({"alive", "critical"})
CRITICAL_ENTER_DAYS = 2.0
CRITICAL_LEAVE_DAYS = 4.0
RUNWAY_WINDOW = timedelta(days=7)
RUNWAY_CAP_DAYS = 365.0
LAST_MODE_KEY = "economy.last_mode"
SESSION_KEY = "economy.dry_run.session_mark"  # newest ledger id when the dry-run session began
SESSION_NO_KEY = "economy.dry_run.session_no"  # counts dry-run sessions (the agent's records are kept per session)
PAUSED_KEY = "control.paused"
KILLED_KEY = "control.killed"
# 0.15.0: why Ember's code paused the agent: a fact from Etsy's numbers (a refund, a listing fee) left it without money.
# While it stays paused, that is not death: the owner decides (a grant, or resuming it, which clears this).
MONEY_PAUSE_KEY = "control.paused_for_money"

REASONS = {
    "alive": "Running normally",
    "paused": "Paused by the owner",
    "killed": "Stopped with the kill switch",
    "unfunded": "Waiting for money: grant funds to start",
    "dormant": "Sleeping while the app runs in dry run",
}


def mode_of(settings: Settings) -> str:
    return "dry_run" if settings.dry_run else "live"


@dataclass(frozen=True)
class Runway:
    """How long the balance lasts at the API spending of the last 7 active days (``days``; the life's states go by
    it), and, 0.12.0, at that spending less the revenue and plus the expenses of the same days (``net_days``: None
    while it earns at least what it spends, see ``net_note``)."""

    days: float | None
    note: str | None
    window_spend: int = 0
    active_days: float = 0.0
    net_days: float | None = None
    net_note: str | None = None
    window_net_in: int = 0  # revenue less expenses in the same days (their corrections included)


@dataclass
class LifeStatus:
    """Everything the dashboard, the sensors and the budget guard need to know."""

    mode: str
    life_id: int | None
    state: str
    reason: str
    born_at: str | None = None
    ended_at: str | None = None
    critical_since: str | None = None
    critical_mark: int | None = None
    last_will_due: bool = False
    last_will_at: str | None = None
    balance: int = 0
    settled_balance: int = 0
    pending: int = 0
    spent_in_life: bool = False
    runway: Runway = field(default_factory=lambda: Runway(None, None))
    revive_needed: int | None = None
    revive_suggested: int | None = None
    money_in_id: int = 0  # 0.16.2: the newest ledger row that brought money in (burn.settle)

    @property
    def critical(self) -> bool:
        return self.critical_since is not None

    @property
    def can_run(self) -> bool:
        return self.state in ACTIVE_STATES


@dataclass(frozen=True)
class _Revival:
    grant_id: int
    grant_micros: int


class Life:
    def __init__(self, db: Database, settings: Settings, clock: Clock, books: Books, mode: str) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.books = books
        self.mode = mode

    # --- helpers ---

    def scope(self) -> Scope:
        if self.mode == "live":
            return Scope("live")
        mark = self.db.get_meta(SESSION_KEY)
        return Scope("dry_run", int(mark) if mark and mark.isdigit() else 0)

    def session(self) -> int:
        """The dry-run session number (0 in live mode): the agent keeps its projects, files and journal per session."""
        if self.mode == "live":
            return 0
        value = self.db.get_meta(SESSION_NO_KEY)
        return int(value) if value and value.isdigit() else 0

    def current(self) -> sqlite3.Row | None:
        with self.db.connection() as conn:
            return conn.execute("SELECT * FROM lives WHERE mode = ? ORDER BY id DESC LIMIT 1", (self.mode,)).fetchone()

    def previous_lives(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, born_at, ended_at, end_reason, state FROM lives WHERE mode = ? AND ended_at IS NOT NULL"
                " ORDER BY id DESC LIMIT ?",
                (self.mode, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "born_at": r["born_at"],
                "died_at": r["ended_at"],
                "reason": r["end_reason"],
                "state": r["state"],
            }
            for r in rows
        ]

    def flag(self, key: str) -> bool:
        return self.db.get_meta(key) == "1"

    def revive_threshold(self) -> int | None:
        """Balance a new life needs: one planning call plus the last-will reserve."""
        opening = opening_cost(self.settings, self.db, self.mode)
        reserve = last_will_reserve(self.settings, self.db, self.mode)
        if opening is None or reserve is None:
            return None
        return opening + reserve

    # --- startup ---

    def start(self) -> LifeStatus:
        """Called once at startup: begin lives and dry-run sessions as the mode requires."""
        now = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            last_mode = self.db.get_meta(LAST_MODE_KEY)
            live = _current(conn, "live")
            dry = _current(conn, "dry_run")
            if self.mode == "dry_run":
                if last_mode != "dry_run" or dry is None:
                    if dry is not None and dry["ended_at"] is None:
                        self._end(conn, dry, "ended", "dry-run session ended", now)
                    self.db.set_meta(SESSION_KEY, str(self.books.last_id()))
                    self.db.set_meta(SESSION_NO_KEY, str(self.session() + 1))
                    self._birth(conn, "dry_run", now, "new dry-run session")
                if live is not None and live["ended_at"] is None and live["state"] != "dormant":
                    self._transition(conn, live, "dormant", REASONS["dormant"], now)
            else:
                if dry is not None and dry["ended_at"] is None:
                    self._end(conn, dry, "ended", "dry run switched off", now)
                if live is None:
                    self._birth(conn, "live", now, "first live start")
            self.db.set_meta(LAST_MODE_KEY, self.mode)
            return self.evaluate_and_persist()

    def _birth(
        self,
        conn: sqlite3.Connection,
        mode: str,
        now: str,
        reason: str,
        revived_from: int | None = None,
        revived_by: int | None = None,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO lives (mode, born_at, born_mark, started_reason, state, state_reason, revived_from_life_id,"
            " revived_by_ledger_id) VALUES (?, ?, ?, ?, 'new', ?, ?, ?)",
            (mode, now, self.books.last_id(), reason, reason, revived_from, revived_by),
        )
        life_id = int(cursor.lastrowid)
        events.record(self.db, "info", "life", f"A new {_mode_label(mode)} life began: {reason}", {"life_id": life_id})
        return life_id

    def _end(self, conn: sqlite3.Connection, life: sqlite3.Row, state: str, reason: str, now: str) -> None:
        conn.execute(
            "UPDATE lives SET state = ?, state_reason = ?, ended_at = ?, end_reason = ?, ended_mark = ?,"
            " last_will_due = 0 WHERE id = ?",
            (state, reason, now, reason, self.books.last_id(), life["id"]),
        )
        _add_transition(conn, life, state, reason, now)

    def _transition(self, conn: sqlite3.Connection, life: sqlite3.Row, state: str, reason: str, now: str) -> None:
        conn.execute("UPDATE lives SET state = ?, state_reason = ? WHERE id = ?", (state, reason, life["id"]))
        _add_transition(conn, life, state, reason, now)

    # --- evaluation ---

    def evaluate(self) -> LifeStatus:
        """The current state, computed from the books; writes nothing."""
        life = self.current()
        if life is None:
            return LifeStatus(self.mode, None, "unknown", "No life has started yet")
        return self._assess(life)[0]

    def evaluate_and_persist(self) -> LifeStatus:
        """Evaluate and store the result atomically; writes only when something changed."""
        with self.db.transaction() as conn:
            life = _current(conn, self.mode)
            if life is None:
                return LifeStatus(self.mode, None, "unknown", "No life has started yet")
            status, revival = self._assess(life)
            now = to_iso(self.clock.now())
            if revival is not None:
                amount = micros_to_usd(revival.grant_micros)
                reason = f"revived by owner grant #{revival.grant_id} of ${amount:.2f}"
                new_id = self._birth(conn, self.mode, now, reason, life["id"], revival.grant_id)
                events.record(self.db, "warning", "life", f"{self.settings.agent_name} was {reason}")
                life = _current(conn, self.mode)
                assert life is not None and life["id"] == new_id
                status, _ = self._assess(life)
            self._persist(conn, life, status, now)
            return status

    def _persist(self, conn: sqlite3.Connection, life: sqlite3.Row, status: LifeStatus, now: str) -> None:
        if life["ended_at"] is not None:
            return
        if status.state == "dead":
            self._end(conn, life, "dead", status.reason, now)
            events.record(
                self.db,
                "error",
                "life",
                f"{self.settings.agent_name} died: {status.reason}",
                {"life_id": life["id"], "mode": self.mode},
            )
            return
        due = 1 if status.last_will_due else 0
        if status.critical_since != life["critical_since"] or due != life["last_will_due"]:
            conn.execute(
                "UPDATE lives SET critical_since = ?, critical_mark = ?, last_will_due = ? WHERE id = ?",
                (status.critical_since, status.critical_mark, due, life["id"]),
            )
            if status.critical_since and not life["critical_since"]:
                events.record(self.db, "warning", "life", f"Critical: {status.reason}", {"life_id": life["id"]})
        if status.state != life["state"]:
            self._transition(conn, life, status.state, status.reason, now)
            level = "warning" if status.state in ("critical", "unfunded", "killed") else "info"
            events.record(
                self.db,
                level,
                "life",
                f"State changed from {life['state']} to {status.state}: {status.reason}",
                {"life_id": life["id"], "mode": self.mode},
            )

    def _assess(self, life: sqlite3.Row) -> tuple[LifeStatus, _Revival | None]:
        scope = self.scope()
        now = self.clock.now()
        balance = self.books.balance(scope)
        pending = self.books.pending(scope)
        settled = balance + self.books.provisional_excess(scope)
        spent = self.books.first_api_cost_after(scope, life["born_mark"]) is not None
        base = LifeStatus(
            mode=self.mode,
            life_id=life["id"],
            state=life["state"],
            reason=life["state_reason"] or "",
            born_at=life["born_at"],
            ended_at=life["ended_at"],
            critical_since=life["critical_since"],
            critical_mark=life["critical_mark"],
            last_will_due=bool(life["last_will_due"]),
            last_will_at=life["last_will_at"],
            balance=balance,
            settled_balance=settled,
            pending=pending,
            spent_in_life=spent,
            money_in_id=self.books.last_money_in(scope),
        )

        if life["ended_at"] is not None:
            base.reason = life["end_reason"]
            base.runway = Runway(None, "The agent is dead" if life["state"] == "dead" else "This session has ended")
            if life["state"] != "dead":
                return base, None
            return self._revival(base, scope, life)

        # 0.15.0: paused by Ember's code for a fact that left it without money: the owner decides, not the balance
        held = self.mode == "live" and self.flag(PAUSED_KEY) and bool(self.db.get_meta(MONEY_PAUSE_KEY))
        if spent and settled <= 0 and not held:
            reason = f"ran out of money (balance ${micros_to_usd(balance):.2f})"
            return replace(base, state="dead", reason=reason, runway=Runway(0.0, "Out of money")), None

        runway = self._runway(life, scope, balance, now)
        critical_since, critical_mark = life["critical_since"], life["critical_mark"]
        reason = REASONS["alive"]
        if critical_since is None:
            if runway.days is not None and runway.days < CRITICAL_ENTER_DAYS:
                critical_since, critical_mark = to_iso(now), self.books.last_id()
        else:
            recovered = runway.days is None or runway.days >= CRITICAL_LEAVE_DAYS
            # It must also be able to plan again (a starving agent often has no runway figure at all).
            threshold = self.revive_threshold() or 0
            affordable = balance - pending >= threshold
            if recovered and affordable and self.books.money_in_after(scope, critical_mark or 0):
                critical_since = critical_mark = None
        if critical_since is not None:
            reason = life["state_reason"] if life["state"] == "critical" else "Runway is under 2 days"
        last_will_due = critical_since is not None and life["last_will_at"] is None

        if self.flag(KILLED_KEY):
            state, reason = "killed", REASONS["killed"]
        elif self.flag(PAUSED_KEY):
            why = self.db.get_meta(MONEY_PAUSE_KEY) if self.mode == "live" else None  # only live entries set it
            state, reason = "paused", (why or REASONS["paused"])
        elif not spent and balance <= 0:
            state, reason = "unfunded", REASONS["unfunded"]
        elif critical_since is not None:
            state = "critical"
        else:
            state = "alive"
        return (
            replace(
                base,
                state=state,
                reason=reason,
                critical_since=critical_since,
                critical_mark=critical_mark,
                last_will_due=last_will_due,
                runway=runway,
            ),
            None,
        )

    def _revival(self, base: LifeStatus, scope: Scope, life: sqlite3.Row) -> tuple[LifeStatus, _Revival | None]:
        threshold = self.revive_threshold()
        if threshold is None:
            return base, None
        available = base.balance - base.pending
        grant = self.books.latest_grant_after(scope, life["ended_mark"] or 0)
        if grant is not None and available >= threshold:
            return base, _Revival(int(grant["id"]), int(grant["amount_micros"]))
        needed = max(0, threshold - available)
        needed = _ceil_cents(needed)
        week = int(self.settings.daily_spend_cap_usd * 7 * MICROS_PER_USD)
        suggested = max(_ceil_dollars(needed), _ceil_dollars(week))
        return replace(base, revive_needed=needed, revive_suggested=suggested), None

    def _runway(self, life: sqlite3.Row, scope: Scope, balance: int, now: datetime) -> Runway:
        first_cost = self.books.first_api_cost_after(scope, life["born_mark"])
        if first_cost is None:
            return Runway(None, "No spending yet")
        start = max(now - RUNWAY_WINDOW, from_iso(life["born_at"]), from_iso(first_cost))
        spend = self.books.api_spend_between(scope, start, now, after_id=life["born_mark"])
        active = self._active_seconds(life["id"], start, now)
        days = max(1.0, active / 86_400)
        if spend <= 0:
            note = "No spending in the last 7 days"
            return Runway(None, note, 0, days, net_note=note)
        runway = max(0, balance) / (spend / days)
        # 0.12.0: the net runway counts what came in and what else went out in the same days (it counted neither).
        net_in = self.books.net_revenue_between(scope, start, now)
        burn = spend - net_in
        if burn <= 0:
            net_days, net_note = None, "Earning at least what it spends (last 7 days)"
        else:
            net_days, net_note = min(max(0, balance) / (burn / days), RUNWAY_CAP_DAYS), None
        return Runway(min(runway, RUNWAY_CAP_DAYS), None, spend, days, net_days, net_note, net_in)

    def _active_seconds(self, life_id: int, start: datetime, end: datetime) -> float:
        """Seconds within [start, end] the life spent alive or critical."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT ts, to_state FROM life_transitions WHERE life_id = ? ORDER BY id", (life_id,)
            ).fetchall()
        total = 0.0
        for index, row in enumerate(rows):
            if row["to_state"] not in ACTIVE_STATES:
                continue
            begin = from_iso(row["ts"])
            finish = from_iso(rows[index + 1]["ts"]) if index + 1 < len(rows) else end
            overlap = (min(finish, end) - max(begin, start)).total_seconds()
            total += max(0.0, overlap)
        return total

    def persist_if_dead(self) -> LifeStatus:
        """Record a death at once without otherwise waking the life (used for the dormant live life)."""
        with self.db.transaction() as conn:
            life = _current(conn, self.mode)
            if life is None:
                return LifeStatus(self.mode, None, "unknown", "No life has started yet")
            status, _ = self._assess(life)
            if status.state == "dead" and life["ended_at"] is None:
                self._persist(conn, life, status, to_iso(self.clock.now()))
            return status

    # --- events from the budget guard ---

    def starve(self, purpose: str, needed: int) -> str:
        """The guard refused a cycle-opening call for lack of money. Returns the new state.

        The first time, the agent turns critical and is due to write its last
        will. If the will is already written, or the last-will call itself
        can't be paid, the agent dies. Call inside the guard's transaction.
        """
        with self.db.transaction() as conn:
            life = _current(conn, self.mode)
            if life is None or life["ended_at"] is not None:
                return "dead" if life is not None else "unknown"
            now = to_iso(self.clock.now())
            cost = f"${micros_to_usd(needed):.2f}"
            if purpose == "last_will" or life["last_will_at"] is not None:
                status = LifeStatus(self.mode, life["id"], "dead", f"starved: can't afford a {cost} call")
                self._persist(conn, life, status, now)
                return "dead"
            reason = f"Starving: can't afford a {cost} planning call"
            conn.execute(
                "UPDATE lives SET critical_since = COALESCE(critical_since, ?),"
                " critical_mark = COALESCE(critical_mark, ?), last_will_due = 1, state_reason = ? WHERE id = ?",
                (now, self.books.last_id(), reason, life["id"]),
            )
            if life["state"] in ("alive", "critical"):
                if life["state"] == "alive":
                    _add_transition(conn, life, "critical", reason, now)
                conn.execute("UPDATE lives SET state = 'critical' WHERE id = ?", (life["id"],))
            events.record(self.db, "warning", "life", reason, {"life_id": life["id"]})
            return "critical"

    def record_last_will(self, conn: sqlite3.Connection, life_id: int, at: str) -> bool:
        """Mark the will as written (inside the caller's transaction). A life that already ended is left alone."""
        updated = conn.execute(
            "UPDATE lives SET last_will_at = ?, last_will_due = 0"
            " WHERE id = ? AND ended_at IS NULL AND last_will_at IS NULL",
            (at, life_id),
        ).rowcount
        return updated == 1

    # --- the owner's switches ---

    def set_switch(self, key: str, on: bool) -> LifeStatus:
        with self.db.transaction():
            self.db.set_meta(key, "1" if on else "0")
            if key == PAUSED_KEY and not on:
                self.db.set_meta(MONEY_PAUSE_KEY, "")  # 0.15.0: resumed, the money decides again
            return self.evaluate_and_persist()


def _current(conn: sqlite3.Connection, mode: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM lives WHERE mode = ? ORDER BY id DESC LIMIT 1", (mode,)).fetchone()


def _add_transition(conn: sqlite3.Connection, life: sqlite3.Row, state: str, reason: str, now: str) -> None:
    conn.execute(
        "INSERT INTO life_transitions (ts, life_id, mode, from_state, to_state, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (now, life["id"], life["mode"], life["state"], state, reason),
    )


def _mode_label(mode: str) -> str:
    return "dry-run" if mode == "dry_run" else "live"


def _ceil_cents(micros: int) -> int:
    step = MICROS_PER_USD // 100
    return math.ceil(micros / step) * step


def _ceil_dollars(micros: int) -> int:
    return math.ceil(micros / MICROS_PER_USD) * MICROS_PER_USD
