"""The agent as the app sees it: when to wake, running a cycle, recovery, dashboard data.

``decide()`` is the single place that says whether a wake cycle runs now (and
why not, when it doesn't); the scheduler only calls it and waits. All times come
from the injected clock, so tests drive the agent's day with a fake clock.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .. import events, paths
from ..config import LoadedSettings
from ..db import Database
from ..economy.clock import from_iso, to_iso
from ..economy.costs import micros_to_usd
from ..economy.metering import MeteredModel, OfflineTransport, Transport, usd_cap_to_micros
from ..economy.pricing import opening_cost, working_cycle_cost
from ..economy.service import Economy
from ..integrations import executor as email_executor
from ..integrations import mailstore
from ..integrations.mail import Mailbox, select_mailbox
from . import store
from .loop import NO_STEP, CycleEnd, CycleRunner
from .memory import CAPS, Memory
from .sandbox import Jail
from .store import AgentScope

log = logging.getLogger(__name__)

FIRST_WAKE_DELAY = timedelta(minutes=2)
BOOT_GRACE = timedelta(seconds=60)
CRASH_LOOP = 3
MAX_WILL_ATTEMPTS = 3
WAKE_NOW_MIN_GAP = timedelta(seconds=60)
SLEEP_REASON_CHARS = 200  # of the agent's reason for its sleep, in the next wake's reason


def cycles_enabled_by_env() -> bool:
    return os.environ.get("EMBER_SCHEDULER", "").strip().lower() not in ("off", "0", "false", "no")


def select_transport(mode: str, session: int, stop: threading.Event | None = None, api_key: str = "") -> Transport:
    """The only place a model transport is chosen: the fake in dry run, the Anthropic API live."""
    if mode == "dry_run":
        from .fake_llm import FakeTransport

        scenario = os.environ.get("EMBER_FAKE_SCENARIO", "founder").strip() or "founder"
        delay = int(os.environ.get("EMBER_FAKE_DELAY_MS", "800") or 0)
        return FakeTransport(seed=session, scenario=scenario, delay_ms=delay, stop=stop)
    if not api_key:
        return OfflineTransport(simulated=False)
    from ..economy.anthropic_transport import AnthropicTransport

    return AnthropicTransport(api_key, stop)


@dataclass(frozen=True)
class Decision:
    run: bool
    trigger: str | None = None
    reason: str = ""
    wait_until: datetime | None = None


class Agent:
    def __init__(
        self,
        db: Database,
        loaded: LoadedSettings,
        economy: Economy,
        transport: Transport | None = None,
        cycles_enabled: bool | None = None,
    ) -> None:
        self.db = db
        self.loaded = loaded
        self.settings = loaded.settings
        self.economy = economy
        self.clock = economy.clock
        self.mode = economy.mode
        self.stop = threading.Event()
        self.cycles_enabled = cycles_enabled_by_env() if cycles_enabled is None else cycles_enabled
        self.transport = transport or select_transport(
            self.mode, economy.life.session(), self.stop, self.settings.anthropic_api_key.get_secret_value()
        )
        self.meter: MeteredModel = economy.metered(self.transport)
        self.wake_requested = False
        self.last_wake_request: datetime | None = None
        # The owner wrote while no wake could start (a cycle running, or the minute between wake-ups): wake for it
        # once one can, if the message is still unread then.
        self.message_waiting = False
        self.running_cycle = False
        self._lock = threading.Lock()  # one cycle at a time in this process
        # Ember's mailbox: the fake one in dry run (its inbox grows with the session's wake cycles), the
        # configured one live, or none. Only Ember's code holds it; the tools get its address.
        self.mailbox: Mailbox | None = select_mailbox(
            self.mode, self.settings, economy.life.session(), self._dry_run_wakes
        )
        self.executor = email_executor.Executor(db, self.clock, self.settings, self.scope, self.mailbox)

    # --- where things live ---

    def scope(self) -> AgentScope:
        status = self.economy.life.evaluate()
        return AgentScope(self.mode, self.economy.life.session(), status.life_id or 0)

    def roots(self) -> tuple[Jail, Jail]:
        base = paths.data_dir() / "dry_run" if self.mode == "dry_run" else paths.data_dir()
        return Jail(base / "workspace"), Jail(base / "memory")

    def memory(self, scope: AgentScope | None = None) -> Memory:
        return Memory(self.db, self.roots()[1], scope or self.scope())

    def _key(self, name: str) -> str:
        return f"agent.{self.mode}.{name}"

    def _dry_run_wakes(self) -> int:
        """How many wake cycles this dry-run session has opened (the fake mailbox's clock)."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM cycles WHERE simulated = 1 AND session = ?", (self.economy.life.session(),)
            ).fetchone()
        return int(row[0])

    def _meta_time(self, name: str) -> datetime | None:
        value = self.db.get_meta(self._key(name))
        try:
            return from_iso(value) if value else None
        except ValueError:
            return None

    def _set_time(self, name: str, moment: datetime | None) -> None:
        self.db.set_meta(self._key(name), to_iso(moment) if moment else "")

    # --- startup ---

    def recover(self) -> None:
        """Tidy up after a crash or restart; prepare the dry-run folders and memory files."""
        now = self.clock.now()
        with self.db.transaction() as conn:
            store.interrupt_open_tool_calls(conn, to_iso(now))
        if self.economy.health.lock_held:  # (another process holding the data folder may be sending right now)
            self.executor.recover()  # an email that was being sent may have gone out: it is never sent again
        if self.mode == "dry_run":
            self._rotate_dry_run_folders()
        workspace, memory_root = self.roots()
        for jail in (workspace, memory_root):
            jail.ensure_root()
            jail.remove_temporary_files()
        scope = self.scope()
        with self.db.transaction() as conn:
            self.memory(scope).ensure(conn, to_iso(now))
        wake = self._meta_time("next_wake_at")
        if wake is not None and wake < now + BOOT_GRACE:
            # Give the owner a minute to pause after an update or restart.
            self._set_time("next_wake_at", now + BOOT_GRACE)

    def _rotate_dry_run_folders(self) -> None:
        session = str(self.economy.life.session())
        key = "agent.dry_run.folder_session"
        if self.db.get_meta(key) == session:
            return
        base = paths.data_dir() / "dry_run"
        for name in ("workspace", "memory"):
            current, previous = base / name, base / f"{name}.prev"
            if current.exists():
                shutil.rmtree(previous, ignore_errors=True)
                current.rename(previous)
        self.db.set_meta(key, session)

    # --- deciding ---

    def blocked_reason(self) -> str | None:
        """Why no cycle can run at all right now (None if one could)."""
        if not self.cycles_enabled:
            return "Wake cycles are switched off (EMBER_SCHEDULER=off)"
        if self.mode == "live" and not self.settings.anthropic_api_key.get_secret_value():
            return "Live mode needs the Anthropic API key in the app options"
        api_blocked = getattr(self.transport, "blocked", None)
        if api_blocked:
            return api_blocked
        if not self.economy.health.lock_held:
            return "Another Ember process is using the data folder"
        if self.economy.health.broken:
            return "Spending is stopped after a bookkeeping error; restart the app"
        status = self.economy.life.evaluate()
        if status.state not in ("alive", "critical"):
            reasons = {
                "paused": "The agent is paused",
                "killed": "The agent was stopped with the kill switch",
                "dead": "The agent is dead",
                "unfunded": "The agent has no money yet",
            }
            return reasons.get(status.state, f"The agent is {status.state}")
        return None

    def decide(self, now: datetime | None = None) -> Decision:
        now = now or self.clock.now()
        blocked = self.blocked_reason()
        if blocked:
            return Decision(False, reason=blocked)
        status = self.economy.life.evaluate()
        if status.last_will_due and not self._gave_up_will(status.life_id):
            retry = self._meta_time("will_retry_at")
            if retry is None or now >= retry or self.wake_requested:
                return Decision(True, "last_will", "the last will is due")
            return Decision(False, reason="The last will is due; retrying later", wait_until=retry)
        if self.message_waiting and not self.wake_requested:
            ready = self._wake_for_waiting_message()
            if ready is not None:
                return Decision(False, reason="Waking up to read the owner's message in a moment", wait_until=ready)
        if self.wake_requested:
            return Decision(True, "owner", "woken by the owner")
        no_room = self._no_room_for_work()
        if no_room:
            return Decision(False, reason=no_room)
        wake = self._meta_time("next_wake_at")
        if wake is None:
            wake = now + FIRST_WAKE_DELAY
            self._set_time("next_wake_at", wake)
            self.db.set_meta(self._key("next_wake_reason"), "first wake-up")
        if now < wake:
            return Decision(
                False, reason=self.db.get_meta(self._key("next_wake_reason")) or "sleeping", wait_until=wake
            )
        if self._crash_loop():
            return Decision(False, reason="The last cycles were all interrupted; press Wake now to try again")
        # A cycle that can plan but not afford one work step and its reflection would only pay for the plan. A daily
        # cap below that worst case gets a cycle with all of it: the loop checks what each step really costs.
        daily = usd_cap_to_micros(self.settings.daily_spend_cap_usd)
        working = working_cycle_cost(self.settings, self.db, self.mode) or 0
        needed = max(opening_cost(self.settings, self.db, self.mode) or 0, min(working, daily))
        scope = self.economy.life.scope()
        today = self.economy.books.cap_spend_on(scope, self.clock.today())
        if daily - today < needed:
            tomorrow = self._next_local_midnight(now) + timedelta(minutes=5)
            self._set_time("next_wake_at", tomorrow)
            self.db.set_meta(self._key("next_wake_reason"), "waiting for the daily cap to reset")
            return Decision(False, reason="Waiting for the daily cap to reset", wait_until=tomorrow)
        return Decision(True, "schedule", "scheduled wake-up")

    def _gave_up_will(self, life_id: int | None) -> bool:
        return life_id is not None and self.db.get_meta(self._key("will_given_up")) == str(life_id)

    def _no_room_for_work(self) -> str | None:
        """Why scheduled wakes wait for the owner: the last cycle paid for its plan but had no money left for a work
        step, and the cycle spend cap is below what a working cycle can cost, so the next one would likely do the same.
        """
        working = working_cycle_cost(self.settings, self.db, self.mode)
        cap = self.settings.cycle_spend_cap_usd
        if working is None or usd_cap_to_micros(cap) >= working:
            return None
        with self.db.connection() as conn:
            last = conn.execute(
                "SELECT status, note FROM cycles WHERE simulated = ? AND session = ? AND status <> 'running'"
                " ORDER BY id DESC LIMIT 1",
                (1 if self.mode == "dry_run" else 0, self.economy.life.session()),
            ).fetchone()
        if last is None or (last["status"], last["note"]) != ("refused", NO_STEP):
            return None
        return (
            f"The last cycle had no money left for a work step after its plan: the cycle spend cap (${cap:.2f}) is"
            f" below what a working cycle can cost (up to ${micros_to_usd(working):.2f}). Raise it, or press Wake now"
        )

    def _crash_loop(self) -> bool:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT status FROM cycles WHERE simulated = ? AND session = ? ORDER BY id DESC LIMIT ?",
                (1 if self.mode == "dry_run" else 0, self.economy.life.session(), CRASH_LOOP),
            ).fetchall()
        return len(rows) == CRASH_LOOP and all(r["status"] == "interrupted" for r in rows)

    def _next_local_midnight(self, now: datetime) -> datetime:
        local = now.astimezone(self.clock.tz)
        return self.clock.day_start(local.date() + timedelta(days=1))

    def request_wake(self, by_message: bool = False) -> tuple[int, dict[str, Any]]:
        """The owner pressed Wake now, or sent a message (``by_message``, the wake_on_message option): (HTTP status,
        body). Both share the minute between wake-ups."""
        now = self.clock.now()
        if self.running_cycle:
            return 409, {"code": "cycle_running", "error": "a wake cycle is already running"}
        blocked = self.blocked_reason()
        if blocked:
            return 409, {"code": "not_runnable", "error": blocked}
        if self.last_wake_request and now - self.last_wake_request < WAKE_NOW_MIN_GAP:
            return 429, {"code": "too_soon", "error": "wait a minute between wake-ups"}
        self.last_wake_request = now
        self.wake_requested = True
        self.message_waiting = False  # the cycle this wakes reads every message the agent hasn't seen
        events.record(
            self.db, "info", "agent", "The owner's message woke the agent" if by_message else "The owner woke the agent"
        )
        return 202, {"queued": True}

    def wake_for_message(self) -> str | None:
        """The owner sent a message (with the wake_on_message option): wake the agent to read it.

        "now" if a wake is on its way; "after_cycle" or "soon" while a cycle runs or the agent woke less than a minute
        ago: it wakes for the message once the cycle has ended and the minute has passed (``decide``); None if it
        can't run (paused, dead, ...): then it reads the message at its next wake.
        """
        if self.wake_requested:  # woken and not started yet: that cycle reads the message
            return "now"
        status, body = self.request_wake(by_message=True)
        if status == 202:
            return "now"
        code = body.get("code")
        if code in ("cycle_running", "too_soon"):
            self.message_waiting = True
            return "after_cycle" if code == "cycle_running" else "soon"
        return None

    def _wake_for_waiting_message(self) -> datetime | None:
        """For ``decide``: wake for a message that couldn't wake the agent when it came. Returns when that can be while
        it is still too soon; None once the agent is woken, or when no message is left unread (the cycle that was
        running read it: no second cycle for it)."""
        with self.db.connection() as conn:
            unread = store.unseen(conn, "messages", self.scope(), 1)
        if not unread:
            self.message_waiting = False
            return None
        status, body = self.request_wake(by_message=True)
        if status == 429 and self.last_wake_request is not None:
            return self.last_wake_request + WAKE_NOW_MIN_GAP
        if body.get("code") != "cycle_running":  # woken (or it can't be: the message waits for the next wake)
            self.message_waiting = False
        return None

    # --- running ---

    def run_cycle(self, trigger: str) -> CycleEnd:
        if not self._lock.acquire(blocking=False):
            return CycleEnd("skipped", "a cycle is already running", skipped=True)
        self.running_cycle = True
        try:
            if trigger == "owner":
                self.wake_requested = False
            scope = self.scope()
            workspace, memory_root = self.roots()
            runner = CycleRunner(
                self.db,
                self.settings,
                self.clock,
                self.economy,
                self.meter,
                scope,
                workspace,
                Memory(self.db, memory_root, scope),
                self.stop,
                self.mailbox,
            )
            end = runner.run(trigger)
            self._after(trigger, end)
            return end
        finally:
            self.running_cycle = False
            self._lock.release()

    def _after(self, trigger: str, end: CycleEnd) -> None:
        now = self.clock.now()
        failures = int(self.db.get_meta(self._key("failures")) or 0)
        if trigger == "last_will":
            if end.status == "completed":
                self._set_time("will_retry_at", None)
                self.db.set_meta(self._key("will_attempts"), "0")
            elif end.status == "refused":
                self._set_time("will_retry_at", self._next_local_midnight(now) + timedelta(minutes=5))
            else:
                attempts = int(self.db.get_meta(self._key("will_attempts")) or 0) + 1
                self.db.set_meta(self._key("will_attempts"), str(attempts))
                if attempts >= MAX_WILL_ATTEMPTS:
                    # Each attempt costs money; stop trying rather than spend the rest on a failing call.
                    life_id = self.economy.life.evaluate().life_id
                    self.db.set_meta(self._key("will_given_up"), str(life_id))
                    events.record(
                        self.db, "error", "agent", f"Gave up on the last will after {attempts} failed attempts"
                    )
                    return
                minutes = min(self.settings.max_sleep_minutes, 30 * 2 ** (attempts - 1))
                self._set_time("will_retry_at", now + timedelta(minutes=minutes))
            return
        if end.status == "interrupted":
            return
        no_room = self._no_room_for_work()
        if no_room:  # no scheduled wake until the owner wakes the agent or raises the cap (decide)
            self._set_time("next_wake_at", None)
            self.db.set_meta(self._key("next_wake_reason"), no_room)
            return
        if end.status in ("completed", "idle"):
            minutes = end.sleep_minutes or self.settings.wake_interval_minutes
            minutes = max(self.settings.min_sleep_minutes, min(self.settings.max_sleep_minutes, minutes))
            self.db.set_meta(self._key("failures"), "0")
            reason = "scheduled"
            if end.sleep_minutes:
                reason = f"{self.settings.agent_name} chose {minutes} min"
                if end.sleep_reason:  # the agent's words, quoted (the dashboard shows them as text)
                    reason += f": {json.dumps(end.sleep_reason[:SLEEP_REASON_CHARS], ensure_ascii=False)}"
        else:
            failures += 1
            self.db.set_meta(self._key("failures"), str(failures))
            minutes = min(self.settings.max_sleep_minutes, self.settings.min_sleep_minutes * 2 ** (failures - 1))
            reason = f"after a {end.status} cycle, backing off"
        self._set_time("next_wake_at", now + timedelta(minutes=minutes))
        self.db.set_meta(self._key("next_wake_reason"), reason)

    # --- approved actions Ember carries out itself ---

    def executor_blocked(self) -> str | None:
        """Why approved emails aren't sent now (None if they may be): only while the agent itself could run."""
        if not self.cycles_enabled:
            return "Wake cycles are switched off (EMBER_SCHEDULER=off)"
        if not self.economy.health.lock_held:
            return "Another Ember process is using the data folder"
        state = self.economy.life.evaluate().state
        return None if state in ("alive", "critical") else f"The agent is {state}"

    def execute_approved(self) -> list[tuple[int, str]]:
        """Send the approved emails that are due (the scheduler calls this before every decision)."""
        if self.mailbox is None or self.executor_blocked():
            return []
        return self.executor.run()

    # --- dashboard ---

    def integrations(self) -> dict[str, Any]:
        return {
            "email": email_executor.integration(
                self.db, self.clock, self.settings, self.mode, self.scope(), self.mailbox
            )
        }

    def agent_fields(self) -> dict[str, Any]:
        blocked = self.blocked_reason()
        wake = self._meta_time("next_wake_at")
        reason = self.db.get_meta(self._key("next_wake_reason")) or None
        if self.message_waiting:  # it wakes for the owner's message as soon as it can (decide)
            now = self.clock.now()
            soon = max(now, self.last_wake_request + WAKE_NOW_MIN_GAP) if self.last_wake_request else now
            if wake is None or soon < wake:
                wake, reason = soon, "to read your message"
        can_wake = blocked is None and not self.running_cycle
        return {
            "next_wake_at": to_iso(wake) if wake and blocked is None else None,
            "next_wake_reason": reason if blocked is None else None,
            "can_wake": can_wake,
            "wake_blocked_reason": blocked or ("A wake cycle is running" if self.running_cycle else None),
            "cycles_enabled": self.cycles_enabled,
        }

    def dashboard(self) -> dict[str, Any]:
        from . import views

        return views.dashboard(self)

    def sensor_fields(self) -> dict[str, Any]:
        from . import views

        scope = self.scope()
        with self.db.connection() as conn:
            counts = views.badges(conn, scope)
            email_unread = mailstore.unread(conn, scope, 0)[0] if self.mailbox is not None else 0
            email_waiting = email_executor.waiting(conn, scope)
        wake = self._meta_time("next_wake_at") if self.blocked_reason() is None else None
        return {
            **counts,
            "email_unread": email_unread,
            "email_waiting": email_waiting,
            "next_wake_at": to_iso(wake) if wake else None,
            "cycle_running": self.running_cycle,
        }

    def cycle_detail(self, cycle_id: int) -> dict[str, Any] | None:
        from . import views

        return views.cycle_detail(self, cycle_id)

    def memory_files(self) -> dict[str, str]:
        memory = self.memory()
        return {name: memory.read(name) for name in CAPS}

    def workspace(self) -> dict[str, Any]:
        from . import views

        return views.workspace(self)

    def workspace_file(self, path: str) -> tuple[str, str]:
        """(file name, text); raises views.WorkspaceFileError with a message for the owner."""
        from . import views

        return views.workspace_file(self, path)
