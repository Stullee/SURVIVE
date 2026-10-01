"""The agent as the app sees it: when to wake, running a cycle, recovery, dashboard data.

``decide()`` is the single place that says whether a wake cycle runs now (and
why not, when it doesn't); the scheduler only calls it and waits. All times come
from the injected clock, so tests drive the agent's day with a fake clock.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .. import events, paths, privacy
from ..config import LoadedSettings, Settings
from ..db import Database
from ..economy import burn
from ..economy.clock import from_iso, to_iso
from ..economy.costs import micros_to_usd
from ..economy.metering import (
    EVENT_RESERVE_HOUR,
    MeteredModel,
    OfflineTransport,
    Transport,
    event_reserve,
    usd_cap_to_micros,
)
from ..economy.pricing import opening_cost, working_cycle_cost
from ..economy.service import Economy
from ..integrations import (
    etsy,
    etsy_publisher,
    etsy_revenue,
    mailstore,
    pinterest,
    pinterest_publisher,
    printify_publisher,
)
from ..integrations import executor as email_executor
from ..integrations.etsy_connection import EtsyConnection
from ..integrations.etsy_publisher import Publisher
from ..integrations.mail import Mailbox, select_mailbox
from ..integrations.pinterest_connection import PinterestConnection
from ..integrations.printify_connection import PrintifyConnection
from ..products import site
from . import agenda, audit, metrics, netguard, news, policy, store, ventures, website
from .loop import NO_STEP, CycleEnd, CycleRunner, recover_records
from .memory import CAPS, Memory
from .sandbox import Jail, SandboxError, kind_of
from .store import AgentScope

log = logging.getLogger(__name__)

FIRST_WAKE_DELAY = timedelta(minutes=2)
BOOT_GRACE = timedelta(seconds=60)
CRASH_LOOP = 3
MAX_WILL_ATTEMPTS = 3
WAKE_NOW_MIN_GAP = timedelta(seconds=60)
# 0.14.0: the owner's messages and decisions wake one cycle for all of them, this long after the last one (two
# approvals 83 seconds apart started two cycles: $2.57 in 8 minutes). Wake now stays immediate.
OWNER_QUIET = timedelta(minutes=5)
OWNER_QUIET_MAX = timedelta(minutes=15)  # and at most this long after the first, however often the owner clicks
OWNER_GAP = timedelta(minutes=30)  # between two such cycles (nine messages 7 minutes apart started nine cycles)
# 0.14.0: while a request waits for the owner, the longest sleep (or the default sleep, if longer). It was the default
# sleep alone: 60 minutes at the owner's options, not the 240 the notes said.
WAITING_SLEEP_MINUTES = 240
SLEEP_REASON_CHARS = 200  # of the agent's reason for its sleep, in the next wake's reason
SHOP_RETRY = timedelta(minutes=etsy_publisher.SYNC_MINUTES)  # after a failed check of the Etsy shop
CHECK_HOUR = 8  # the owner's hour a waiting milestone's check wakes the agent, on its day (0.12.0)


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
        # The owner wrote (or, 0.12.0, decided): wake for it once one can, if the agent hasn't seen it by then (a cycle
        # running, the minute between wake-ups, and 0.14.0, OWNER_QUIET after the owner's last one: quiet_until).
        # waiting_for: which of the two. 0.14.0: kept in meta too (owner_wake_*), so a restart keeps the promised wake.
        self.message_waiting = False
        self.waiting_for = "message"
        self.quiet_until: datetime | None = None
        self.quiet_by: datetime | None = None  # 0.14.0: OWNER_QUIET_MAX after the first message or decision
        self.running_cycle = False
        self._lock = threading.Lock()  # one cycle at a time in this process
        # Ember's mailbox: the fake one in dry run (its inbox grows with the session's wake cycles), the
        # configured one live, or none. Only Ember's code holds it; the tools get its address.
        self.mailbox: Mailbox | None = select_mailbox(
            self.mode, self.settings, economy.life.session(), self._dry_run_wakes
        )
        self.executor = email_executor.Executor(db, self.clock, self.settings, self.scope, self.mailbox)
        # The Etsy shop: the fake one in dry run, the owner's once connected. Only Ember's code reaches it.
        self.etsy = EtsyConnection(
            db,
            self.clock,
            self.settings,
            self.mode,
            economy.life.session(),
            etsy.TokenFile(paths.etsy_dir() / "tokens.json"),
            etsy.TaxonomyFile(paths.etsy_dir() / "categories.json"),
        )
        self.publisher = Publisher(
            db, self.clock, self.settings, self.scope, self.etsy.shop, lambda: self.roots()[0], self._etsy_numbers
        )
        # 0.13.0 (Phase E2): the owner's Pinterest account (the fake one in dry run), switched on in the options. Only
        # Ember's code reaches it; it makes the pins the owner approved (and deletes the ones they undo).
        self.pinterest = PinterestConnection(
            db,
            self.clock,
            self.settings,
            self.mode,
            economy.life.session(),
            pinterest.TokenFile(paths.pinterest_dir() / "tokens.json"),
        )
        self.pins = pinterest_publisher.Publisher(
            db, self.clock, self.settings, self.scope, self.pinterest.account, lambda: self.roots()[0]
        )
        # 0.13.0 (Phase E4): the owner's Printify account (the fake one in dry run), switched on in the options: the
        # products it makes on order are sold in the Etsy shop. Only Ember's code reaches it.
        self.printify = PrintifyConnection(db, self.clock, self.settings, self.mode, economy.life.session())
        self.pod = printify_publisher.Publisher(
            db,
            self.clock,
            self.settings,
            self.scope,
            self.printify.account,
            self.printify.shop_id,
            lambda: self.roots()[0],
        )
        self._shop_failed_at: datetime | None = None  # the last check of the shop that failed (sync_shop)
        self._mail_checked_at: datetime | None = None  # 0.13.0: the last read of the mailbox between cycles

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
            store.keep_act_words(conn)  # 0.14.0: requests stored before NEVER read normalised text
        try:  # 0.14.0: a cycle the app died in gets its journal and digest, as one that ends gets them
            with self.db.transaction() as conn:
                recover_records(conn, self.scope(), to_iso(now))
        except Exception:  # noqa: BLE001 - the records must never keep the agent from starting
            log.exception("Could not write the records of an interrupted cycle")
        if self.economy.health.lock_held:  # (another process holding the data folder may be sending right now)
            self.executor.recover()  # an email that was being sent may have gone out: it is never sent again
            self.publisher.recover()  # a listing that was being created may exist: it is never created again
            self.pins.recover()  # so may a pin (0.13.0)
            self.pod.recover()  # and a Printify product
        if self.mode == "dry_run":
            self._rotate_dry_run_folders()
        workspace, memory_root = self.roots()
        for jail in (workspace, memory_root):
            jail.ensure_root()
            jail.remove_temporary_files()
        scope = self.scope()
        with self.db.transaction() as conn:
            self.memory(scope).ensure(conn, to_iso(now))
        try:
            with self.db.transaction() as conn:
                ventures.seed(conn, scope, to_iso(now))  # the tree's first ideas (0.10.0), once
        except Exception:  # noqa: BLE001 - the ventures must never keep the agent from starting
            log.exception("Could not plant the first ventures")
        etsy_revenue.audit(self.db, self.clock, self.settings)  # 0.12.0: the owner turned it on or off
        self.take_back_while_off()  # 0.14.0: no owner_user_ids, or safe mode
        wake = self._meta_time("next_wake_at")
        if wake is not None and wake < now + BOOT_GRACE:
            # Give the owner a minute to pause after an update or restart.
            self._set_time("next_wake_at", now + BOOT_GRACE)
        quiet = self._meta_time("owner_wake_at")  # 0.14.0: a wake for the owner's news a restart would have dropped
        # (unless the owner switched that wake off: changing an option restarts the app)
        on = {"message": self.settings.wake_on_message, "decision": self.settings.wake_on_decision}
        if quiet is not None and not on.get(self.db.get_meta(self._key("owner_wake_for")) or "message", True):
            self._owner_wake_done()
        elif quiet is not None:
            self.message_waiting = True
            self.waiting_for = self.db.get_meta(self._key("owner_wake_for")) or "message"
            self.quiet_until = max(quiet, now + BOOT_GRACE)
            self.quiet_by = self._meta_time("owner_wake_by")

    def _etsy_numbers(self, scope: AgentScope, settings: Settings) -> None:
        """0.12.0: the orders' revenue, Etsy's fees and refunds in the ledger after a sync, when the owner turned that
        on (etsy_revenue)."""
        etsy_revenue.record(self.db, self.clock, self.economy, scope, settings)

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
        mode = burn.current(self.db, status).mode
        # 0.12.0: dormant, no model calls until money comes in; only the owner's Wake now runs a cycle
        if mode == burn.DORMANT and not self.wake_requested:
            return Decision(False, reason=f"Dormant: {burn.MEANING[burn.DORMANT]}; Wake now runs a cycle")
        ready = None
        if self.message_waiting and not self.wake_requested:
            ready = self._wake_for_waiting_message(now)
        if self.wake_requested:
            return Decision(True, "owner", "woken by the owner")
        decision = self._decide_schedule(now, mode)
        # 0.14.0: the owner's wake is one more reason to wake, not a hold: a cycle that comes first reads their news
        if ready is not None and not decision.run and (decision.wait_until is None or ready < decision.wait_until):
            why = "read the owner's message" if self.waiting_for == "message" else "act on the owner's decision"
            return Decision(False, reason=f"Waking up to {why} soon", wait_until=ready)
        return decision

    def _decide_schedule(self, now: datetime, mode: str) -> Decision:
        """For ``decide``: an event's or the schedule's wake (the owner's aside)."""
        no_room = self._no_room_for_work()
        if no_room:
            return Decision(False, reason=no_room)
        event = self._event_wake(now)  # 0.13.0: a reply, an inquiry or a milestone's last day
        if event is not None:
            return event
        wake = self._meta_time("next_wake_at")
        if wake is None:
            wake = now + FIRST_WAKE_DELAY
            self._set_time("next_wake_at", wake)
            self.db.set_meta(self._key("next_wake_reason"), "first wake-up")
        held = self._maintenance_day(wake) if mode == burn.MAINTENANCE else None
        if held is not None:
            wake = held
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
        held = event_reserve(self.settings, self.clock, "schedule", working)  # 0.13.0: kept for events until 20:00
        if daily - today - held < needed <= daily - today:
            evening = self.clock.at(self.clock.today(), EVENT_RESERVE_HOUR)  # 0.14.0: 20:00 on DST days too
            reason = f"the rest of the daily cap is kept for event wake-ups until {EVENT_RESERVE_HOUR}:00"
            self._set_time("next_wake_at", evening)
            self.db.set_meta(self._key("next_wake_reason"), reason)
            return Decision(False, reason=reason[0].upper() + reason[1:], wait_until=evening)
        if daily - today < needed:
            tomorrow = self._next_local_midnight(now) + timedelta(minutes=5)
            self._set_time("next_wake_at", tomorrow)
            self.db.set_meta(self._key("next_wake_reason"), "waiting for the daily cap to reset")
            return Decision(False, reason="Waiting for the daily cap to reset", wait_until=tomorrow)
        return Decision(True, "schedule", "scheduled wake-up")

    def _event_wake(self, now: datetime) -> Decision | None:
        """0.13.0: an urgent event in the agenda (a reply to Ember's email, an inquiry, a milestone's last day) wakes
        the agent for a reactive cycle: at most agenda.EVENT_WAKES a day, agenda.MIN_GAP apart, when the day's cap
        covers a cycle (the events' share included). Otherwise the event waits for the next cycle's plan. 0.14.0: only
        with the owner's wake_on_events option, and behind the schedule's guards: not in a crash loop, nor while the
        agent backs off after a failed, stopped or refused cycle (backoff_until, set by ``_after``)."""
        if not self.settings.wake_on_events:
            return None
        with self.db.connection() as conn:
            waiting = agenda.waking(conn, self.scope())
            if not waiting:
                return None
            woken, last = agenda.wakes(conn, self.scope(), self.clock)
        if woken >= agenda.EVENT_WAKES or (last is not None and now - last < agenda.MIN_GAP):
            return None
        backoff = self._meta_time("backoff_until")
        if (backoff is not None and now < backoff) or self._crash_loop():
            return None
        daily = usd_cap_to_micros(self.settings.daily_spend_cap_usd)
        today = self.economy.books.cap_spend_on(self.economy.life.scope(), self.clock.today())
        if daily - today < (opening_cost(self.settings, self.db, self.mode) or 0):
            return None
        with self.db.transaction() as conn:
            agenda.mark_woke(conn, [int(r["id"]) for r in waiting], to_iso(now))
        more = f" and {len(waiting) - 1} more" if len(waiting) > 1 else ""
        return Decision(True, "event", f"woken by an event: {waiting[0]['text']}{more}"[:300])

    def check_events(self) -> None:
        """0.13.0: read the mailbox every agenda.MAIL_MINUTES between cycles (0.14.0: less often while reading it
        fails, mailstore.wait_minutes), then note in the agenda what happened since the last look (orders, replies,
        favorites, milestones due today). The scheduler calls this every round, after the Etsy sync; like reading the
        shop, it goes on while the agent sleeps, is paused or dormant."""
        if self.sync_blocked() or self.running_cycle:
            return
        now = self.clock.now()
        scope = self.scope()
        every = mailstore.wait_minutes(self.db, self.mode, agenda.MAIL_MINUTES)  # 0.14.0: longer while it fails
        if self.mailbox is not None and (
            self._mail_checked_at is None or now - self._mail_checked_at >= timedelta(minutes=every)
        ):
            self._mail_checked_at = now
            try:  # the fake mailbox of a dry run needs no network; the real one is Ember's own code
                with netguard.sealed() if self.mailbox.simulated else contextlib.nullcontext():
                    mailstore.fetch(self.db, self.clock, scope, self.mailbox)
            except Exception:  # noqa: BLE001 - the mailbox must not stop the scheduler
                log.exception("Reading the mailbox between cycles failed")
        key = agenda.SINCE_KEY.format(mode=self.mode)
        since = self.db.get_meta(key)
        first = not since
        if first:
            since = to_iso(now)
            self.db.set_meta(key, since)
        with self.db.transaction() as conn:
            noted = agenda.note(conn, scope, self.clock, str(since), first=first)
        for text in noted:
            events.record(self.db, "info", "agent", f"Agenda: {text}"[:300])

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

    def _maintenance_day(self, wake: datetime) -> datetime | None:
        """0.14.0: in the maintenance burn mode, one cycle a day after a failed, stopped or refused cycle too (its
        back-off woke the agent 30 minutes later): no scheduled wake before a day after the last cycle began. Any cycle
        counts (the owner's and events' too), as a completed one's sleep always did (``_after``). Returns that moment
        (kept as the next wake) when it is later than ``wake``; None otherwise."""
        with self.db.connection() as conn:
            last = conn.execute(
                "SELECT MAX(started_at) FROM cycles WHERE simulated = ? AND session = ?",
                (1 if self.mode == "dry_run" else 0, self.economy.life.session()),
            ).fetchone()[0]
        if last is None:
            return None
        day = min(burn.MAINTENANCE_SLEEP_MINUTES, self.settings.max_sleep_minutes)
        held = from_iso(last) + timedelta(minutes=day)
        if held <= wake:
            return None
        self._set_time("next_wake_at", held)
        self.db.set_meta(self._key("next_wake_reason"), "the burn mode is maintenance: one cycle a day")
        return held

    def _next_local_midnight(self, now: datetime) -> datetime:
        local = now.astimezone(self.clock.tz)
        return self.clock.day_start(local.date() + timedelta(days=1))

    def request_wake(self, by_message: bool = False, by_decision: bool = False) -> tuple[int, dict[str, Any]]:
        """The owner pressed Wake now, sent a message (``by_message``, the wake_on_message option) or decided on one of
        the agent's requests or proposals (``by_decision``, the wake_on_decision option): (HTTP status, body). All
        share the minute between wake-ups."""
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
        self._owner_wake_done()  # the cycle this wakes reads every message the agent hasn't seen
        woke = "message" if by_message else "decision" if by_decision else ""
        said = f"The owner's {woke} woke the agent" if woke else "The owner woke the agent"
        events.record(self.db, "info", "agent", said)
        return 202, {"queued": True}

    def wake_for_message(self) -> str | None:
        """The owner sent a message (with the wake_on_message option): wake the agent to read it.

        "now" if a wake is on its way (Wake now); "soon", or "after_cycle" while a cycle runs: 0.14.0, it wakes for the
        message OWNER_QUIET after the owner's last message or decision (and OWNER_GAP after the last such wake), once
        the cycle has ended and the minute has passed (``decide``), unless a cycle saw them all by then; None if it
        can't run (paused, dormant, dead, ...): then it reads the message at its next wake.
        """
        return self._wake_for_owner("message")

    def wake_for_decision(self) -> str | None:
        """0.12.0: the owner decided on one of the agent's requests, ventures or milestones (with the wake_on_decision
        option): wake the agent to act on it, like a message (the same answers). It slept up to 12 hours with a
        decision it could have acted on."""
        return self._wake_for_owner("decision")

    def _wake_for_owner(self, what: str) -> str | None:
        if self.wake_requested:  # woken and not started yet: that cycle sees it
            return "now"
        # 0.14.0: one cycle for the owner's messages and decisions, OWNER_QUIET after the last one (each started a
        # cycle of its own: nine messages, nine cycles in an hour). Dormant, only Wake now runs a cycle.
        if self.blocked_reason() or burn.peek(self.db, self.economy.life.evaluate()).mode == burn.DORMANT:
            return None
        now = self.clock.now()
        if not self.message_waiting or what == "message":  # a message waiting says so first
            self.waiting_for = what
        if not self.message_waiting or self.quiet_by is None:
            self.quiet_by = now + OWNER_QUIET_MAX
        self.message_waiting = True
        self.quiet_until = min(now + OWNER_QUIET, self.quiet_by)
        self._set_time("owner_wake_at", self.quiet_until)
        self._set_time("owner_wake_by", self.quiet_by)
        self.db.set_meta(self._key("owner_wake_for"), self.waiting_for)
        return "after_cycle" if self.running_cycle else "soon"

    def _owner_wake_done(self) -> None:
        """0.14.0: no wake waits for the owner's messages and decisions any more (in memory and in meta)."""
        self.message_waiting = False
        self.quiet_until = self.quiet_by = None
        self._set_time("owner_wake_at", None)
        self._set_time("owner_wake_by", None)

    def _wake_for_waiting_message(self, now: datetime) -> datetime | None:
        """For ``decide``: wake for the owner's messages and decisions (0.14.0: once they have been quiet for
        OWNER_QUIET). Returns when that can be while it is still too soon; None once the agent is woken, or when the
        agent has seen all of the owner's news (a cycle that ran meanwhile saw it: no second cycle for it)."""
        with self.db.connection() as conn:
            unread = store.unseen(conn, "messages", self.scope(), 1) or news.decided_unseen(conn, self.scope())
        if not unread:
            self._owner_wake_done()
            return None
        if self.quiet_until is not None and now < self.quiet_until:
            return self.quiet_until
        last = self._meta_time("owner_woke_at")  # 0.14.0: OWNER_GAP between two cycles for the owner's news
        if last is not None and now < last + OWNER_GAP:
            return last + OWNER_GAP
        status, body = self.request_wake(
            by_message=self.waiting_for == "message", by_decision=self.waiting_for == "decision"
        )
        if status == 429 and self.last_wake_request is not None:
            return self.last_wake_request + WAKE_NOW_MIN_GAP
        if status == 202:
            self._set_time("owner_woke_at", now)
        if body.get("code") != "cycle_running":  # woken (or it can't be: the message waits for the next wake)
            self._owner_wake_done()
        return None

    # --- running ---

    def run_cycle(self, trigger: str) -> CycleEnd:
        if not self._lock.acquire(blocking=False):
            return CycleEnd("skipped", "a cycle is already running", skipped=True)
        self.running_cycle = True
        try:
            # 0.14.0: whatever its trigger, the cycle starting now is the one a Wake now asked for (it reads every
            # message). The last will's cycle left it standing, so the will ran again at once, past its retry time.
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
                self.etsy,
                self.publisher,
                self.pinterest,
                self.pins,
                self.printify,
                self.pod,
                unlocks_off=self.unlocks_off(),
            )
            end = runner.run(trigger)
            try:
                self._after(trigger, end)
            except Exception:  # noqa: BLE001 - 0.12.0: it left the last wake time standing, so the agent woke again
                log.exception("Scheduling the next wake failed")
                self._fallback_wake()
            self._scrub_quietly()  # what this cycle wrote after the owner removed a message
            return end
        finally:
            self.running_cycle = False
            self._lock.release()

    # --- the words the owner removed (0.11.2) ---

    def scrub_removed(self) -> bool:
        """Replace the words the owner removed (privacy.register) in what the agent keeps and reads again: its memory
        files, its open projects and its workspace's text files. Runs now unless a cycle is running, and at the end
        of every cycle otherwise (so it never races a cycle's own writes). The history (journal, replies, tool calls)
        can't change: what shows it redacts it. Returns whether it ran now."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._scrub_quietly()
        finally:
            self._lock.release()
        return True

    def _scrub_quietly(self) -> None:
        try:
            changed = self._scrub()
        except Exception:  # noqa: BLE001 - a failed scrub must not end a cycle; the next one tries again
            log.exception("Scrubbing the words the owner removed failed")
            return
        if changed:
            events.record(self.db, "info", "owner", f"Removed the words of a removed message from {changed} texts")

    def _scrub(self) -> int:
        with self.db.connection() as conn:
            redactor = privacy.load(conn)
        if not redactor:
            return 0
        scope = self.scope()
        workspace, memory_root = self.roots()
        now = to_iso(self.clock.now())
        where, params = scope.where()
        with self.db.transaction() as conn:
            changed = Memory(self.db, memory_root, scope).scrub(conn, redactor.apply, now)
            open_projects = conn.execute(
                f"SELECT id, hypothesis, next_step, notes FROM projects WHERE {where}"
                " AND status NOT IN ('succeeded', 'failed', 'abandoned')",
                params,
            ).fetchall()
            for row in open_projects:
                hypothesis, next_step, notes = (redactor.apply(row[k]) for k in ("hypothesis", "next_step", "notes"))
                if (hypothesis, next_step, notes) != (row["hypothesis"], row["next_step"], row["notes"]):
                    conn.execute(  # within the columns' limits; notes are a log, so their newest end is kept
                        "UPDATE projects SET hypothesis = ?, next_step = ?, notes = ? WHERE id = ?",
                        (hypothesis[:400], next_step[:200], notes[-2000:], row["id"]),
                    )
                    changed += 1
        for entry in workspace.walk(workspace.limits.max_entries).entries:
            if entry.is_dir or kind_of(entry.path) != "text":
                continue
            try:
                text = workspace.read(entry.path)
                clean = redactor.apply(text)
                if clean != text:
                    workspace.write(entry.path, clean)
                    changed += 1
            except SandboxError:  # gone or unreadable meanwhile
                continue
        return changed

    def _next_check(self, now: datetime) -> tuple[datetime, int] | None:
        """0.12.0: the next morning (CHECK_HOUR, the owner's time) a waiting milestone's check is due, and its number;
        None if none waits. The checks of a day share its morning: one wake-up a day at most."""
        with self.db.connection() as conn:
            where, params = self.scope().where()
            rows = conn.execute(
                f"SELECT id, check_at FROM milestones WHERE {where} AND status = 'open' AND wait_for IS NOT NULL"
                " ORDER BY check_at, id",
                params,
            ).fetchall()
        for row in rows:
            moment = self.clock.at(date.fromisoformat(row["check_at"]), CHECK_HOUR)  # 0.14.0: 08:00 on DST days too
            if moment > now:
                return moment, int(row["id"])
        return None

    def _overran(self, end: CycleEnd) -> bool:
        """0.14.0: whether a call's overrun stopped or cut short a cycle the agent chose a sleep for (the budget guard
        stopped it, or refused further calls of that kind)."""
        if end.status not in ("stopped", "refused") or not end.sleep_minutes or end.cycle_id is None:
            return False
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM llm_calls WHERE cycle_id = ? AND overrun = 1 LIMIT 1", (end.cycle_id,)
            ).fetchone()
        return row is not None

    def _fallback_wake(self) -> None:
        """The next wake when working it out failed (0.12.0): the default interval from now, never the last one."""
        minutes = max(self.settings.min_sleep_minutes, self.settings.wake_interval_minutes)
        with contextlib.suppress(Exception):
            self._set_time("next_wake_at", self.clock.now() + timedelta(minutes=minutes))
            self.db.set_meta(
                self._key("next_wake_reason"),
                f"working out the next wake failed (see the System log); in {minutes} min",
            )
            events.record(self.db, "error", "agent", "Working out the next wake failed; waking by the default interval")

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
            self._set_time("backoff_until", None)  # 0.14.0: no room holds events; an older back-off would outlive it
            self._set_time("next_wake_at", None)
            self.db.set_meta(self._key("next_wake_reason"), no_room)
            return
        # 0.14.0: a cycle a call's overrun stopped sleeps as the agent chose, as a completed one would, but no sooner
        # than the back-off: its money was spent already, and backing off sooner (live, 30 minutes instead of 420)
        # only brought the next paid cycle sooner
        overran = self._overran(end)
        if end.status in ("completed", "idle") or overran:
            minutes = end.sleep_minutes or self.settings.wake_interval_minutes
            minutes = max(self.settings.min_sleep_minutes, min(self.settings.max_sleep_minutes, minutes))
            if not overran:
                self.db.set_meta(self._key("failures"), "0")
            reason = "scheduled"
            if end.sleep_minutes:
                reason = f"{self.settings.agent_name} chose {minutes} min"
                if end.sleep_reason:  # the agent's words, quoted (the dashboard shows them as text)
                    reason += f": {json.dumps(end.sleep_reason[:SLEEP_REASON_CHARS], ensure_ascii=False)}"
                # 0.12.0: waiting for the owner is no reason to sleep long (it slept 12 hours for an approval): while
                # its requests wait, it wakes within WAITING_SLEEP_MINUTES (0.14.0; or the default interval, if longer)
                # at the latest, to work on something else.
                cap = max(self.settings.min_sleep_minutes, self.settings.wake_interval_minutes, WAITING_SLEEP_MINUTES)
                with self.db.connection() as conn:
                    waiting = store.count_rows(conn, "approvals", self.scope(), "status = 'pending'")
                if waiting and minutes > cap:
                    minutes = cap
                    reason += (
                        f"; cut to {cap} min: {waiting} request{'s wait' if waiting != 1 else ' waits'} for your"
                        " decision, and it works on something else meanwhile"
                    )
            check = self._next_check(now)  # 0.12.0: a milestone's check wakes it that morning (once a day at most)
            if check is not None and now + timedelta(minutes=minutes) > check[0]:
                minutes = max(self.settings.min_sleep_minutes, math.ceil((check[0] - now).total_seconds() / 60))
                reason += f"; waking for the check of milestone #{check[1]}"
            daily = min(burn.MAINTENANCE_SLEEP_MINUTES, self.settings.max_sleep_minutes)
            if minutes < daily and burn.current(self.db, self.economy.life.evaluate()).mode == burn.MAINTENANCE:
                minutes, reason = daily, "the burn mode is maintenance: one cycle a day"  # 0.12.0
        if end.status not in ("completed", "idle"):
            failures += 1
            self.db.set_meta(self._key("failures"), str(failures))
            backoff = min(self.settings.max_sleep_minutes, self.settings.min_sleep_minutes * 2 ** (failures - 1))
            if not overran or minutes < backoff:
                minutes, reason = backoff, f"after a {end.status} cycle, backing off"
            else:
                reason += f" (the cycle was {end.status} after a call cost more than its worst case)"
        self._set_time("next_wake_at", now + timedelta(minutes=minutes))
        self.db.set_meta(self._key("next_wake_reason"), reason)
        # 0.14.0: an event waits out the back-off too (decide may hold the schedule longer: maintenance, the reserve)
        backoff = None if end.status in ("completed", "idle") else self._meta_time("next_wake_at")
        self._set_time("backoff_until", backoff)

    # --- approved actions Ember carries out itself ---

    def executor_blocked(self) -> str | None:
        """Why approved emails aren't sent now (None if they may be): only while the agent itself could run."""
        if not self.cycles_enabled:
            return "Wake cycles are switched off (EMBER_SCHEDULER=off)"
        if not self.economy.health.lock_held:
            return "Another Ember process is using the data folder"
        state = self.economy.life.evaluate().state
        return None if state in ("alive", "critical") else f"The agent is {state}"

    def sync_blocked(self) -> str | None:
        """0.12.0: why the Etsy shop isn't read now (None if it may be). Unlike sending, reading spends nothing and
        goes on while the agent is paused, dormant or waiting for money, so a sale is still seen and recorded; not
        once the kill switch is on or the life is over."""
        if not self.cycles_enabled:
            return "Wake cycles are switched off (EMBER_SCHEDULER=off)"
        if not self.economy.health.lock_held:
            return "Another Ember process is using the data folder"
        state = self.economy.life.evaluate().state
        return None if state in ("alive", "critical", "paused", "unfunded") else f"The agent is {state}"

    def unlocks_off(self) -> str:
        """0.14.0: why the owner's unlocks don't act now ("" when they do): no owner_user_ids, or safe mode."""
        return policy.off(self.settings.owner_user_ids, self.loaded.safe_mode)

    def take_back_while_off(self) -> None:
        """0.14.0: while unlocks are off, Ember's code takes back every unlock that stands, in every mode and session,
        as the kill switch does. None acts again once they are back on: a veto window that passed meanwhile approves
        nothing. The owner grants again."""
        off = self.unlocks_off()
        if not off:
            return
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            taken = policy.revoke_everywhere(conn, now, by=policy.REVOKED_BY, why=f"unlocks are off while {off}")
        if taken:
            message = f"Ember's code took back every unlock ({len(taken)} in all): unlocks are off while {off}"
            events.record(self.db, "warning", "control", message)

    def run_policy(self) -> None:
        """0.13.0: the owner's unlocks (policy.py): revoke the unlocks an unclear result, a spent budget, a missed
        milestone or a veto ended, then approve the requests whose veto window passed (one held by an unlock taken
        back waits for the owner; 0.14.0: none while unlocks are off, and they are taken back). Before the approved
        actions are carried out, in the scheduler's round. Then the owner's daily digest of the day before, once
        (audit.py)."""
        if self.executor_blocked():
            return
        self.take_back_while_off()
        scope = self.scope()
        with self.db.transaction() as conn:
            happened = policy.keep(conn, scope, self.clock) + policy.run_due(
                conn, scope, self.clock, self.unlocks_off()
            )
            digests = audit.write_due(conn, scope, self.clock)
        for line in happened:
            events.record(self.db, "info", "control", line[:300])
        for text in digests:
            events.record(self.db, "info", "control", f"Daily digest: {text}"[:300])

    def undo_blocked(self) -> str | None:
        """0.14.0: why the owner's Undo isn't carried out now (None if it may be): also while the agent is paused or
        waits for money (an Undo costs no API money, and the owner asked for it); not once the kill switch is on."""
        if not self.cycles_enabled:
            return "Wake cycles are switched off (EMBER_SCHEDULER=off)"
        if not self.economy.health.lock_held:
            return "Another Ember process is using the data folder"
        state = self.economy.life.evaluate().state
        return None if state in audit.UNDO_WHILE else f"The agent is {state}"

    def execute_approved(self) -> list[tuple[int, str]]:
        """Send the approved emails, create the approved Etsy listings and (0.13.0) pins and Printify products that are
        due (the scheduler calls this before every decision). 0.14.0: while the agent is paused or waits for money,
        only the owner's Undo."""
        if self.executor_blocked():
            if self.undo_blocked():
                return []
            return self.publisher.run(undos=True) + self.pins.run(undos=True) + self._pod_run(undos=True)
        done = self.executor.run() if self.mailbox is not None else []
        return done + self.publisher.run() + self.pins.run() + self._pod_run()

    def _pod_run(self, undos: bool = False) -> list[tuple[int, str]]:
        """The approved Printify products (the fake account of a dry run needs no network)."""
        account = self.printify.account()
        if account is None:
            return []
        with netguard.sealed() if account.simulated else contextlib.nullcontext():
            return self.pod.run(undos)

    def sync_shop(self) -> None:
        """Read the Etsy shop's listings and orders (at most hourly) and its categories (daily) while Ember runs, not
        only when the agent wakes: Etsy's API terms allow showing listings for 6 hours after they were read, its
        other content for a day. The scheduler calls this every round (between checks it only looks at the time of
        the last one); after a failure (raised, or kept for the dashboard) the next try waits an hour."""
        now = self.clock.now()
        if self.sync_blocked():
            return
        self._sync_pins()
        self._sync_pod()
        if not self.publisher.due():
            return
        if self._shop_failed_at and now - self._shop_failed_at < SHOP_RETRY:
            return
        shop = self.etsy.shop()
        if shop is None:
            return
        self._shop_failed_at = now  # until the check worked
        error = self.publisher.sync()  # first: failing categories must not keep the listings' numbers old
        if error is None:  # 0.12.0: Ember's code checks the milestones with a metric against the fresh numbers
            metrics.grade_all(
                self.db, self.scope(), self.economy.life.scope(), self.clock, self.settings.etsy_stats_history
            )
        # The fake shop of a dry run needs no network; the owner's is reached by Ember's code only.
        with netguard.sealed() if shop.simulated else contextlib.nullcontext():
            self.etsy.refresh_categories(shop)
        if error is None:
            self._shop_failed_at = None

    def _sync_pins(self) -> None:
        """0.13.0 (Phase E2): the pins' numbers, at most every pinterest_publisher.SYNC_HOURS (the fake account of a
        dry run needs no network)."""
        account = self.pinterest.account()
        if account is None:
            return
        try:
            with netguard.sealed() if account.simulated else contextlib.nullcontext():
                self.pins.sync()
        except Exception:  # noqa: BLE001 - Pinterest must not keep the shop from being checked
            log.exception("Checking the pins on Pinterest failed")

    def _sync_pod(self) -> None:
        """0.13.0 (Phase E4): the Printify products and their orders, at most every printify_publisher.SYNC_MINUTES."""
        account = self.printify.account()
        if account is None or not self.pod.due():
            return
        try:
            with netguard.sealed() if account.simulated else contextlib.nullcontext():
                self.pod.sync()
        except Exception:  # noqa: BLE001 - Printify must not keep the shop from being checked
            log.exception("Checking the products at Printify failed")

    # --- dashboard ---

    def integrations(self) -> dict[str, Any]:
        scope = self.scope()
        shop = self.etsy.describe()
        with self.db.connection() as conn:
            shop.update(
                listings=etsy_publisher.listings_json(conn, scope),
                orders=etsy_publisher.orders_json(conn, scope),
                created_today=etsy_publisher.created_today(conn, self.clock, scope),
                waiting=etsy_publisher.waiting(conn, scope),
            )
            home = website.describe(conn, scope, self.settings)  # 0.13.0 (Phase E3)
        return {
            "email": email_executor.integration(self.db, self.clock, self.settings, self.mode, scope, self.mailbox),
            "etsy": shop,
            "pinterest": self.pinterest.describe(scope),  # 0.13.0 (Phase E2)
            "printify": self.printify.describe(scope),  # 0.13.0 (Phase E4)
            "site": home,
        }

    def agent_fields(self) -> dict[str, Any]:
        blocked = self.blocked_reason()
        wake = self._meta_time("next_wake_at")
        reason = self.db.get_meta(self._key("next_wake_reason")) or None
        if self.message_waiting:  # it wakes for the owner's message as soon as it can (decide)
            now = self.clock.now()
            soon = max(now, self.last_wake_request + WAKE_NOW_MIN_GAP) if self.last_wake_request else now
            soon = max(soon, self.quiet_until or soon)  # 0.14.0: once the owner has been quiet for a few minutes
            last = self._meta_time("owner_woke_at")  # and OWNER_GAP after the last cycle for their news
            soon = max(soon, last + OWNER_GAP) if last else soon
            if wake is None or soon < wake:
                why = "to read your message" if self.waiting_for == "message" else "to act on your decision"
                wake, reason = soon, why
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
            agenda_open = agenda.open_count(conn, scope)  # 0.13.0
            event_wakes = agenda.wakes(conn, scope, self.clock)[0]
            digest = audit.latest(conn, scope)
            unlocks = sum(1 for g in policy.grants(conn, scope) if g["level"] != "manual")
        wake = self._meta_time("next_wake_at") if self.blocked_reason() is None else None
        return {
            **counts,
            "email_unread": email_unread,
            "email_waiting": email_waiting,
            # 0.13.0: everything waiting for the owner in one number, and the agenda
            "waiting_on_you": sum(counts[name] for name in views.WAITING_ON_YOU),
            "agenda_open": agenda_open,
            "event_wakes_today": event_wakes,
            # 0.13.0: the unlocks that stand, and the newest daily digest (a notification can follow digest_day)
            "unlocks": unlocks,
            "digest_day": digest["day"] if digest else None,
            "digest": digest["text"] if digest else None,
            "next_wake_at": to_iso(wake) if wake else None,
            "cycle_running": self.running_cycle,
        }

    def inbox_page(self, before: int, limit: int) -> dict[str, Any]:
        from . import views

        return views.inbox_page(self, before, limit)

    def ventures(self) -> dict[str, Any]:
        from . import views

        return views.ventures_view(self)

    def roadmap(self) -> dict[str, Any]:
        from . import views

        return views.roadmap_view(self)

    def library(self) -> dict[str, Any]:
        from . import views

        return views.library_view(self)

    def library_document(self, document_id: int) -> dict[str, Any] | None:
        from . import views

        return views.library_document(self, document_id)

    def planner_preview(self) -> str:
        """What the next wake cycle's plan would see, built now for the diagnostics report (the daily review, new mail
        and the shop's latest numbers come in when the cycle runs)."""
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
            self.etsy,
            self.publisher,
            self.pinterest,
            self.pins,
            self.printify,
            self.pod,
        )
        with self.db.connection() as conn:
            spent, ventured = ventures.day_spend(conn, scope, self.clock.today())
        venture = ventures.venture_turn(self.settings.venture_share, spent, ventured)
        kind = "a venture cycle" if venture else "an ordinary cycle"
        return f"(the next cycle is {kind})\n{runner.planner_preview(venture)}"

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

    def upgrade_script(self, upgrade_id: int) -> tuple[str, str] | None:
        """(file name, text) of the workshop script an upgrade request carries, or None."""
        from . import views

        return views.upgrade_script(self, upgrade_id)

    def site_files(self) -> dict[str, bytes]:
        """The owner's website as Ember's code builds it now (0.13.0, Phase E3). Raises site.SiteError with why it
        can't be built."""
        if not self.settings.site_enabled:
            raise site.SiteError("the website is off: switch it on in the app's options (site_enabled)")
        with self.db.connection() as conn:
            return website.built(conn, self.scope(), website.owner(self.settings))

    def site_download(self) -> bytes:
        """The website as a zip for the owner to publish; the download is recorded, so the plan and the dashboard can
        say what changed since. Raises site.SiteError."""
        files = self.site_files()
        with self.db.transaction() as conn:
            website.record_download(conn, self.scope(), files, to_iso(self.clock.now()))
        return site.archive(files)

    def workspace_product(self, path: str) -> tuple[str, bytes, str]:
        """(file name, bytes, content type) of a PDF, Word, Excel or PNG file; raises views.WorkspaceFileError."""
        from . import views

        return views.workspace_product(self, path)
