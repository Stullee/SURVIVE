"""One wake cycle: plan, act with tools, reflect (or write the last will).

Every model call goes through the budget guard (``MeteredModel``), which can
refuse it; the runner treats a refusal as the end of what it was doing, never
as an error to retry. The conversation of the act phase is kept exactly as the
API returned it, and every ``tool_use`` is answered by a ``tool_result`` in
the next message, in order (the fake model checks this in tests).

In dry run the whole cycle runs with the network and other programs blocked
(``netguard.sealed``). A cycle starts by fetching new mail when Ember has a
mailbox (the fake one in dry run, sealed too); in live mode that happens before
anything is sealed, and only Ember's own code talks to the mail server.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from .. import events
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, to_iso
from ..economy.costs import micros_to_usd
from ..economy.estimate import Unpriceable
from ..economy.metering import (
    REVIEW,
    STUDY,
    CallFailed,
    CallRefused,
    CallResult,
    MeteredModel,
    picture_size,
    usd_cap_to_micros,
)
from ..economy.pricing import LAST_WILL, PLANNER_OPENING, REVIEW_CALL, working_cycle_cost
from ..economy.service import Economy
from ..integrations import etsy_publisher, mailstore
from ..integrations.etsy_connection import EtsyConnection
from ..integrations.etsy_publisher import Publisher
from ..integrations.mail import Mailbox
from ..version import app_version
from . import context, library, netguard, news, prompts, review, roadmap, store, tools, ventures
from .memory import Memory
from .sandbox import Jail, SandboxError
from .store import AgentScope
from .workshop import Workshop, WorkshopError
from .workshop import report as workshop_report

log = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_TURN = 4
# The work conversation's size limit (bytes of JSON, pictures counted as below). At 24,000 (until 0.9.0) it ended every
# cycle that made a product and looked at its pictures after 5 to 10 tool steps. Money is checked before each step.
MAX_CONVERSATION_BYTES = 64_000
MAX_EMPTY_NUDGES = 1
RETRY_DELAY_SECONDS = 5.0
NUDGE = "Continue with the plan, or reply with a short report of what you did."
CUT_OFF = "Your reply was cut off at the length limit. Continue in shorter parts, or use a tool."
# Why a call of a reply cut off by max_tokens didn't run (only the reply's last block can be incomplete): 0.11.1 says
# how to fit, as "write in smaller parts" didn't (a cycle repeated the same too-long write five times).
CUT_CALL = (
    "your reply was cut off at its length limit before this call was complete, so nothing changed. Write a long"
    f" file in parts of at most {tools.WRITE_CHARS:,} characters (create, then append, one part per reply) and keep"
    " other texts shorter"
)
STEP_CHARS = 200  # a plan step's length (prompts.PLANNER_RULES tells the planner)
STEP_GROWTH_BYTES = 20_000  # the most one step can add (4 tool results and a reply): the REFLECT profile's room
# 0.12.0: what the next step may add, as the reflection after it is priced: the largest step of this cycle so far, 1.5
# times, and at least STEP_GROWTH_TOKENS. A fixed 20,000 bytes of "x" counted as about 19,000 tokens live, while real
# steps added 400 to 3,700: cycles stopped with a third of their money left.
STEP_GROWTH_TOKENS = 4_000
STEP_GROWTH_FACTOR = 1.5
# A picture the model looks at counts like text in proportion to its pixels: the largest a look shows (LOOK_PIXELS
# square, about 1,300 tokens) like this much, a 1000 x 750 listing photo like 3,750 bytes and a wide spreadsheet picture
# (1000 x 180) like 900. A picture whose size can't be read counts as the largest.
IMAGE_EQUIVALENT_BYTES = 5_000
PLANNER_SCALES = (1.0, 0.75, 0.5, 0.3)  # the planner's context budgets, until the request fits its profile
NO_STEP = "not enough money left in this cycle for a work step and the reflection"


class Stopping(Exception):
    """The app is shutting down: end the cycle as interrupted."""


class EndCycle(Exception):
    """Something the cycle can't continue after (the guard refused for state or system reasons)."""

    def __init__(self, status: str, note: str) -> None:
        super().__init__(note)
        self.status = status
        self.note = note


@dataclass
class CycleEnd:
    status: str
    note: str | None = None
    rerun: bool = False  # decide again at once (e.g. starvation made the last will due)
    sleep_minutes: int | None = None
    sleep_reason: str | None = None  # the agent's own words for sleep_minutes (set_sleep), if it gave any
    cycle_id: int | None = None
    skipped: bool = False  # the cycle never opened


@dataclass
class Plan:
    assessment: str
    goal: str
    focus_project_id: int | None
    steps: list[str]
    sleep_minutes: int | None
    money_path: str = ""  # how the goal leads to income (or what a learning experiment would show)
    focus_venture_id: int | None = None  # 0.10.0
    focus_milestone_id: int | None = None  # 0.11.0

    def to_json(self) -> dict[str, Any]:
        return {
            "assessment": self.assessment,
            "goal": self.goal,
            "money_path": self.money_path,
            "focus_project_id": self.focus_project_id,
            "focus_venture_id": self.focus_venture_id,
            "focus_milestone_id": self.focus_milestone_id,
            "steps": self.steps,
            "sleep_minutes": self.sleep_minutes,
        }


@dataclass
class _Act:
    turns: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)  # tool_result blocks not sent yet
    report: str = ""
    end_reason: str = ""
    steps: int = 0  # work calls the model answered


class CycleRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        clock: Clock,
        economy: Economy,
        meter: MeteredModel,
        scope: AgentScope,
        workspace: Jail,
        memory: Memory,
        stop: threading.Event | None = None,
        mailbox: Mailbox | None = None,
        etsy: EtsyConnection | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.economy = economy
        self.meter = meter
        self.scope = scope
        self.workspace = workspace
        self.memory = memory
        self.stop = stop or threading.Event()
        self.dry_run = scope.mode == "dry_run"
        self.mailbox = mailbox
        self.mail = mailbox is not None  # the email tools and the MAIL section, for every cycle of this run
        self.etsy = etsy
        self.publisher = publisher
        self.etsy_on = False  # the Etsy tools and the ETSY SHOP section: set once the cycle found a shop
        self.library_on = False  # the library's tools (0.12.0): set when a cycle starts with documents in it

    # --- the cycle ---

    def planner_preview(self, venture: bool) -> str:
        """The planner's context as a wake cycle would build it now (the diagnostics report shows it): nothing is
        fetched, synced, marked or spent."""
        self.etsy_on = self.etsy is not None and self.publisher is not None and self.etsy.shop() is not None
        snap = self._snapshot(venture)
        planner = ""
        for scale in PLANNER_SCALES:
            planner, _ = context.planner_context(snap, self.dry_run, scale)
            if context.fits(
                prompts.plan_request(self.settings, planner, venture=venture), PLANNER_OPENING.input_tokens
            ):
                break
        return planner

    def run(self, trigger: str) -> CycleEnd:
        try:
            cycle_id = self.meter.open_cycle(trigger)
        except CallRefused as exc:
            return CycleEnd("skipped", exc.reason, skipped=True)
        state = tools.CycleTools()
        ctx = tools.ToolContext(
            db=self.db,
            clock=self.clock,
            scope=self.scope,
            cycle_id=cycle_id,
            workspace=self.workspace,
            memory=self.memory,
            min_sleep=self.settings.min_sleep_minutes,
            max_sleep=self.settings.max_sleep_minutes,
            state=state,
            # A fetched PDF has no size limit, so reading pages costs real money only if the owner allows it.
            allow_fetch=self.dry_run or self.settings.web_fetch,
            mail=tools.MailAccess(self.mailbox.address, self.settings.email_daily_limit) if self.mailbox else None,
        )
        ctx.research = self._research_fn(ctx)
        ctx.workshop = self._workshop_fn(ctx) if prompts.workshop_on(self.settings) else None
        end = CycleEnd("failed", "the cycle ended unexpectedly")
        try:
            if trigger != "last_will":
                ctx.venture = self._venture_cycle(cycle_id)
                ctx.brainstorm = self._brainstorm_fn(ctx) if ctx.venture else None
                with self.db.connection() as conn:
                    self.library_on = ctx.library = library.totals(conn, self.scope)[0] > 0
                self._fetch_mail(cycle_id)
                self._sync_etsy(cycle_id, ctx)
                self._expire_requests()
            with netguard.sealed() if self.dry_run else contextlib.nullcontext():
                end = self._last_will(cycle_id) if trigger == "last_will" else self._plan_act_reflect(cycle_id, ctx)
        except Stopping:
            end = CycleEnd("interrupted", "the app was stopping")
        except EndCycle as exc:
            end = CycleEnd(exc.status, exc.note)
        except Exception as exc:  # noqa: BLE001 - a bug ends the cycle, never the app
            log.exception("Wake cycle #%d failed", cycle_id)
            end = CycleEnd("failed", f"internal error ({type(exc).__name__})")
        end.cycle_id = cycle_id
        if end.sleep_minutes is None:
            end.sleep_minutes, end.sleep_reason = state.sleep_minutes, state.sleep_reason or None
        self._close(cycle_id, end, state)
        return end

    def _close(self, cycle_id: int, end: CycleEnd, state: tools.CycleTools) -> None:
        try:
            self._write_report(cycle_id, end)
        except Exception:  # noqa: BLE001 - the cycle must still be closed, or no later cycle could open
            log.exception("Could not write the report of cycle #%d", cycle_id)
        try:
            closed = self.meter.close_cycle(cycle_id, end.status if end.status != "skipped" else "failed", end.note)
        except Exception:  # noqa: BLE001
            log.exception("Could not close cycle #%d", cycle_id)
            return
        try:
            self._announce(cycle_id, end, closed)
        except Exception:  # noqa: BLE001
            log.exception("Could not record the end of cycle #%d", cycle_id)

    def _write_report(self, cycle_id: int, end: CycleEnd) -> None:
        now = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            store.interrupt_open_tool_calls(conn, now, cycle_id)
            if not store.has_journal(conn, cycle_id):
                summary = f"Cycle ended {end.status}" + (f": {end.note}" if end.note else "")
                store.write_journal(conn, self.scope, cycle_id, "system", summary, _code_journal(conn, cycle_id), now)
            if end.sleep_minutes is not None:
                store.update_cycle(conn, cycle_id, sleep_minutes=end.sleep_minutes)
            store.update_cycle(conn, cycle_id, phase=None, current_action=None)

    def _announce(self, cycle_id: int, end: CycleEnd, closed: bool) -> None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT status FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        final = row["status"] if row else end.status
        if not closed and final != end.status:
            end.status = final  # the guard already stopped it (an overrun)
        spent, _ = self.economy.books.cycle_spend(cycle_id)
        tail = f"; next sleep {end.sleep_minutes} min" if end.sleep_minutes else ""
        level = "info" if final in ("completed", "idle") else "warning"
        events.record(
            self.db,
            level,
            "agent",
            f"Cycle #{cycle_id} {final}: ${micros_to_usd(spent):.4f}" + (f" ({end.note})" if end.note else "") + tail,
            {"cycle_id": cycle_id},
        )

    def _venture_cycle(self, cycle_id: int) -> bool:
        """Whether this is a venture cycle: venture cycles have had less than the owner's share of the day's spending
        (``ventures.venture_turn``). Recorded on the cycle; an empty venture tree gets its first ideas first."""
        with self.db.transaction() as conn:
            ventures.seed(conn, self.scope, to_iso(self.clock.now()))
            spent, ventured = ventures.day_spend(conn, self.scope, self.clock.today())
            turn = ventures.venture_turn(self.settings.venture_share, spent, ventured)
            if turn:
                store.update_cycle(conn, cycle_id, venture=1)
        return turn

    def _expire_requests(self) -> None:
        """0.12.0: the requests the owner didn't decide within their type's days expire (news for the agent)."""
        with self.db.transaction() as conn:
            expired = store.expire_requests(conn, self.scope, to_iso(self.clock.now()))
        for r in expired:
            days = store.REQUEST_DAYS[r["type"]]
            events.record(self.db, "info", "agent", f"Request #{r['id']} expired: no decision in {days} days")

    def _fetch_mail(self, cycle_id: int) -> None:
        """New mail before the plan (errors are recorded and shown, and never stop the cycle)."""
        if self.mailbox is None or self.stop.is_set():
            return
        self._progress(cycle_id, current_action="Checking the mailbox")
        try:
            # The fake mailbox of a dry run needs no network, so it is sealed; the real one is Ember's own code.
            with netguard.sealed() if self.mailbox.simulated else contextlib.nullcontext():
                mailstore.fetch(self.db, self.clock, self.scope, self.mailbox)
        except Exception:  # noqa: BLE001 - mail must never end a cycle
            log.exception("Checking the mailbox failed")

    def _sync_etsy(self, cycle_id: int, ctx: tools.ToolContext) -> None:
        """The shop's numbers before the plan, and what the tools know of it (errors are recorded and shown, and
        never stop the cycle)."""
        if self.etsy is None or self.publisher is None:
            return
        shop = self.etsy.shop()
        if shop is None:
            return
        if not self.stop.is_set():
            self._progress(cycle_id, current_action="Checking the Etsy shop")
            try:
                # The fake shop of a dry run needs no network; the owner's is reached by Ember's code only.
                with netguard.sealed() if shop.simulated else contextlib.nullcontext():
                    self.etsy.refresh_categories(shop)
                self.publisher.sync()
            except Exception:  # noqa: BLE001 - the shop must never end a cycle
                log.exception("Checking the Etsy shop failed")
        ctx.etsy = tools.EtsyAccess(
            shop_name=self.etsy.shop_name() or "your shop",
            currency=self.etsy.currency() or "USD",
            daily_limit=self.settings.etsy_listings_per_day,
            categories=tuple(self.etsy.categories()),
        )
        self.etsy_on = True

    def _progress(self, cycle_id: int, **columns: Any) -> None:
        with self.db.transaction() as conn:
            store.update_cycle(conn, cycle_id, **columns)

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Stopping

    def _snapshot(self, venture: bool = False) -> context.Snapshot:
        status = self.economy.life.evaluate()
        scope = self.economy.life.scope()
        today = self.economy.books.cap_spend_on(scope, self.clock.today())
        local = self.clock.now().astimezone(self.clock.tz).strftime("%A %Y-%m-%d %H:%M %Z")
        with self.db.connection() as conn:
            fresh = news.collect(conn, self.db, self.scope, app_version())
            shop = ""
            if self.etsy_on and self.etsy is not None:
                name = self.etsy.shop_name() or "your shop"
                shop = etsy_publisher.shop_text(conn, self.scope, self.clock, name, self.settings.etsy_listings_per_day)
            return context.snapshot(
                conn,
                self.scope,
                status,
                self.memory,
                self.workspace,
                local_time=local,
                version=app_version(),
                agent_name=self.settings.agent_name,
                today_spend=today,
                daily_cap=self.settings.daily_spend_cap_usd,
                cycle_cap=self.settings.cycle_spend_cap_usd,
                news=fresh,
                mail_address=self.mailbox.address if self.mailbox else None,
                today=self.clock.today(),
                etsy=shop,
                venture=venture,
                venture_share=self.settings.venture_share,
                shelf=library.shelf(conn, self.scope),
                decision_wakes=self.settings.wake_on_decision,
            )

    def _call(self, cycle_id: int, purpose: str, request: dict[str, Any]) -> CallResult:
        """One metered call; a failure that cost nothing (no connection, overloaded) is retried once, but never a
        request the API rejected as it is (0.10.1: a search limited to a blocked site was sent twice)."""
        self._check_stop()
        try:
            return self.meter.call(cycle_id, purpose, request)
        except CallFailed as exc:
            if exc.result.status != "failed" or exc.result.cost_micros or _rejected(exc.result.error):
                raise
            log.info("Call #%d failed without cost (%s); retrying once", exc.result.call_id, exc.result.error)
            if self.stop.wait(RETRY_DELAY_SECONDS):
                raise Stopping from exc
            return self.meter.call(cycle_id, purpose, request)

    # --- plan, act, reflect ---

    def _plan_act_reflect(self, cycle_id: int, ctx: tools.ToolContext) -> CycleEnd:
        with self.db.connection() as conn:
            review_due = review.due(conn, self.scope, self.clock)
        if review_due:
            self._review(cycle_id)
        if self.library_on:
            self._study(cycle_id)
        snap = self._snapshot(ctx.venture)
        action = "Planning this venture cycle" if ctx.venture else "Planning this cycle"
        self._progress(cycle_id, phase="plan", current_action=action)
        request = None
        for scale in PLANNER_SCALES:
            planner, planned = context.planner_context(snap, self.dry_run, scale)
            request = prompts.plan_request(self.settings, planner, venture=ctx.venture)
            if context.fits(request, PLANNER_OPENING.input_tokens):
                break
        else:
            return CycleEnd("failed", "the planning prompt doesn't fit its budget")
        try:
            result = self._call(cycle_id, "plan", request)
        except CallRefused as exc:
            return CycleEnd("refused", f"planning refused: {exc.reason}", rerun=exc.state == "critical")
        except CallFailed as exc:
            return CycleEnd("failed", f"planning failed: {exc.result.error or exc.result.status}")
        response = result.response or {}
        text = _text_of(response)
        self._save_text(result.call_id, text, response)
        stop = response.get("stop_reason")
        if stop == "refusal":
            return CycleEnd("stopped", "the model refused to plan")
        if stop != "end_turn":
            return CycleEnd("failed", f"the plan was cut off ({stop})")
        plan = self._parse_plan(text)
        if plan is None:
            return CycleEnd("failed", "the plan wasn't valid JSON")
        if snap.library is not None and snap.library.new:
            shown = [i for item in snap.library.new if library.studied_line(item) in planner for i in item.ids]
            with self.db.transaction() as conn:
                library.mark_seen(conn, cycle_id, shown)  # the documents this plan listed as newly studied
        if planned.changelog:
            news.mark_changelog_seen(self.db, self.scope, snap.news)
        focus = None
        venture_focus = milestone_focus = ""
        with self.db.connection() as conn:
            if plan.focus_project_id is not None:
                focus = store.project(conn, self.scope, plan.focus_project_id)
                if focus is None or focus["status"] not in store.OPEN_STATUSES:
                    plan.focus_project_id, focus = None, None
            if plan.focus_venture_id is not None:
                venture = ventures.get(conn, self.scope, plan.focus_venture_id)
                if venture is None or venture["stage"] not in ventures.OPEN_STAGES:
                    plan.focus_venture_id = None
                else:
                    venture_focus = self._venture_focus(conn, venture)
            if plan.focus_milestone_id is not None:
                milestone = roadmap.get(conn, self.scope, plan.focus_milestone_id)
                if milestone is None or milestone["status"] != "open":
                    plan.focus_milestone_id = None
                else:
                    parent = roadmap.get(conn, self.scope, milestone["parent_id"]) if milestone["parent_id"] else None
                    milestone_focus = roadmap.focus_text(milestone, self.clock.today(), parent)
        ctx.state.focus_project_id = plan.focus_project_id
        ctx.state.focus_venture_id = plan.focus_venture_id
        self._progress(
            cycle_id,
            plan=json.dumps(plan.to_json(), ensure_ascii=False),
            project_id=plan.focus_project_id,
            venture_id=plan.focus_venture_id,
            milestone_id=plan.focus_milestone_id,
            current_action=plan.goal[:300] or None,
        )
        if not plan.steps:
            with self.db.transaction() as conn:
                # No brief follows: what the plan showed in full was all the agent needed to see of it.
                news.mark_seen(conn, cycle_id, planned.items)
                store.write_journal(
                    conn,
                    self.scope,
                    cycle_id,
                    "agent",
                    "Nothing worth doing this cycle",
                    plan.assessment,
                    to_iso(self.clock.now()),
                )
            return CycleEnd("idle", "nothing to do", sleep_minutes=plan.sleep_minutes)

        brief, briefed = context.brief(
            snap,
            self.dry_run,
            plan.to_json(),
            focus,
            self.settings.max_tool_steps,
            venture_focus=venture_focus,
            milestone_focus=milestone_focus,
            knowledge=self._knowledge(plan),
        )
        act = self._act(cycle_id, ctx, brief, planned.listed & briefed.items)
        if act.end_reason == "refusal":
            return CycleEnd("stopped", "the model refused to continue")
        if not act.steps:
            # Nothing ran, so there is nothing to reflect on: the system's journal entry says why (_write_report).
            self._progress(cycle_id, act_end_reason=act.end_reason[:300] or None)
            status = "failed" if act.end_reason.startswith("failed") else "refused"
            return CycleEnd(status, act.end_reason.removeprefix(f"{status}: ") or None)
        # A journal written during the work is the reflection: the separate reflect call would only be refused.
        reflected = ctx.state.journal_written or self._reflect(cycle_id, ctx, brief, act)
        status = "completed"
        note = act.end_reason if act.end_reason not in ("", "done") else None
        if act.end_reason.startswith("refused"):
            status = "refused"
        elif act.end_reason.startswith("failed"):
            status = "failed"
        if not reflected and status == "completed":
            note = note or "no money left for reflecting"
        if ctx.state.sleep_minutes:  # set_sleep, the last call winning (the reflection's after the act phase's)
            return CycleEnd(status, note, sleep_minutes=ctx.state.sleep_minutes, sleep_reason=ctx.state.sleep_reason)
        return CycleEnd(status, note, sleep_minutes=plan.sleep_minutes)

    def _venture_focus(self, conn: Any, row: Any) -> str:
        """The brief's FOCUS for the plan's venture: its record, money, projects and knowledge file."""
        paid = ventures.money(conn, self.scope).get(row["id"], ventures.Money())
        parts = ventures.knowledge_parts(self.workspace, row["id"], row["title"])
        try:
            size = self.workspace.size_of(parts[-1], "text") if parts else None
        except SandboxError:
            size = None
        return ventures.focus_text(row, paid, size, ventures.projects_of(conn, row["id"]), parts)

    def _review(self, cycle_id: int) -> None:
        """The daily review, before the first plan of the day. It never ends the cycle: a review the budget can't
        cover now is tried at the next cycle, and a failed one is recorded (at most review.MAX_ATTEMPTS a day)."""
        self._progress(cycle_id, phase="review", current_action="Reviewing the last 7 days")
        status = self.economy.life.evaluate()
        with self.db.connection() as conn:
            card = review.scorecard(
                conn,
                self.scope,
                self.clock,
                self.economy.books,
                self.economy.life.scope(),
                status,
                dry_run=self.dry_run,
            )
        request = prompts.review_request(self.settings, card.text)
        if not context.fits(request, REVIEW_CALL.input_tokens):
            log.warning("The daily review doesn't fit its budget; skipped")
            return
        try:
            quote = self.meter.quote(request, REVIEW)
        except Unpriceable as exc:
            log.warning("The daily review can't be priced (%s); skipped", exc)
            return
        if quote > self.meter.headroom(cycle_id, REVIEW):
            log.info("The daily review can't be afforded now; it is tried at the next cycle")
            return
        try:
            result = self._call(cycle_id, REVIEW, request)
        except CallRefused:
            return  # the plan meets the same refusal and ends the cycle, or a later cycle tries again
        except CallFailed as exc:
            self._save_review(cycle_id, card, None, f"the review call failed: {exc.result.error or exc.result.status}")
            return
        response = result.response or {}
        text = _text_of(response)
        self._save_text(result.call_id, text, response)
        stop = response.get("stop_reason")
        parsed = review.parse(text, card.project_ids) if stop == "end_turn" else None
        note = None
        if parsed is None:
            note = "the review wasn't valid JSON" if stop == "end_turn" else f"the review was cut off ({stop})"
        self._save_review(cycle_id, card, parsed, note)

    def _study(self, cycle_id: int) -> None:
        """0.12.0: study the owner's library before the plan: the next parts of the documents waiting, a few calls a
        cycle, within the owner's daily study budget (counted toward the daily cap, not the cycle cap). What is
        learned is kept, so a text is read once. It never ends the cycle: what the money can't cover now waits."""
        budget = usd_cap_to_micros(self.settings.library_study_usd_per_day)
        for _ in range(library.STUDY_CALLS if budget > 0 else 0):
            self._check_stop()
            with self.db.connection() as conn:
                document = library.next_to_study(conn, self.scope)
                if document is None:
                    return
                parts = library.next_parts(conn, document)
                known = library.learnings_of(conn, document["id"])
                spent = library.study_spent(conn, self.scope, self.clock.today())
            if not parts:  # its text is gone: nothing left to read
                return
            context_text = library.study_context(document, parts, known, secrets.token_hex(3))
            request = prompts.study_request(self.settings, context_text)
            try:
                quote = self.meter.quote(request, STUDY)
            except Unpriceable as exc:
                log.warning("A study of the library can't be priced (%s); skipped", exc)
                return
            # The study leaves what the cycle needs to work after it: its plan, a work step and the reflection.
            working = working_cycle_cost(self.settings, self.db, self.economy.life.mode) or 0
            if quote > min(self.meter.headroom(cycle_id, STUDY, keep=working), budget - spent):
                log.info("The library's study waits: today's study budget or the money left can't cover it")
                return
            first, last = parts[0]["part"], parts[-1]["part"]
            self._progress(
                cycle_id,
                phase="study",
                current_action=f"Studying {document['title'][:120]} (part {first}-{last} of {document['parts']})",
            )
            try:
                result = self._call(cycle_id, STUDY, request)
            except CallRefused:
                return
            except CallFailed as exc:
                self._study_failed(document, f"the call failed: {exc.result.error or exc.result.status}", exc.result)
                return
            response = result.response or {}
            text = _text_of(response)
            self._save_text(result.call_id, text, response)
            stop = response.get("stop_reason")
            parsed = library.parse_study(text, first, last) if stop == "end_turn" else None
            if parsed is None:
                why = "the answer wasn't valid JSON" if stop == "end_turn" else f"the answer was cut off ({stop})"
                self._study_failed(document, why, result)
                return
            with self.db.transaction() as conn:
                library.save_study(
                    conn,
                    self.scope,
                    document,
                    parsed,
                    last,
                    cost=result.cost_micros,
                    now=to_iso(self.clock.now()),
                    cycle_id=cycle_id,
                    llm_call_id=result.call_id,
                )

    def _study_failed(self, document: Any, why: str, result: CallResult) -> None:
        with self.db.transaction() as conn:
            stopped = library.study_failed(conn, document["id"], why, result.cost_micros)
        if stopped:
            events.record(
                self.db,
                "warning",
                "agent",
                f"The study of library document #{document['id']} stopped after {library.STUDY_FAILURES} tries: {why}",
            )

    def _knowledge(self, plan: Plan) -> str:
        """0.12.0: the learnings from the owner's library that match the plan, picked by Ember's code, for the brief."""
        if not self.library_on:
            return ""
        query = " ".join([plan.goal, plan.money_path, *plan.steps])
        with self.db.connection() as conn:
            rows = library.relevant(conn, self.scope, query, plan.focus_venture_id, plan.focus_project_id)
        return "\n".join(library.learning_line(r) for r in rows)

    def _save_review(
        self, cycle_id: int, card: review.Scorecard, parsed: review.Review | None, note: str | None
    ) -> None:
        with self.db.transaction() as conn:
            review.save(conn, self.scope, cycle_id, to_iso(self.clock.now()), self.clock.today(), card, parsed, note)
        if parsed is None:
            events.record(self.db, "warning", "agent", f"The daily review failed: {note}")

    def _parse_plan(self, text: str) -> Plan | None:
        data: Any = None
        for candidate in (text, _first_object(text)):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
                break
            except ValueError:
                continue
        if not isinstance(data, dict):
            return None
        steps = data.get("steps")
        steps = [_step(s) for s in steps if isinstance(s, str) and s.strip()][:6] if isinstance(steps, list) else []
        focus = data.get("focus_project_id")
        venture = data.get("focus_venture_id")
        milestone = data.get("focus_milestone_id")
        sleep = data.get("sleep_minutes")
        return Plan(
            assessment=str(data.get("assessment") or "")[:600],
            goal=str(data.get("goal") or "")[:300],
            money_path=str(data.get("money_path") or "")[:300],
            focus_project_id=focus if isinstance(focus, int) and not isinstance(focus, bool) else None,
            focus_venture_id=venture if isinstance(venture, int) and not isinstance(venture, bool) else None,
            focus_milestone_id=milestone if isinstance(milestone, int) and not isinstance(milestone, bool) else None,
            steps=steps,
            sleep_minutes=self._clamp_sleep(sleep) if isinstance(sleep, int) and not isinstance(sleep, bool) else None,
        )

    def _clamp_sleep(self, minutes: int) -> int:
        return max(self.settings.min_sleep_minutes, min(self.settings.max_sleep_minutes, minutes))

    def _act(self, cycle_id: int, ctx: tools.ToolContext, brief: str, seen: frozenset[news.Item]) -> _Act:
        """The work steps; ``seen`` (the owner's items the plan listed and the brief showed in full) is marked once one
        is answered."""
        act = _Act()
        max_steps = self.settings.max_tool_steps
        self._progress(cycle_id, phase="act", max_steps=max_steps, step=0)
        nudged = 0
        for step in range(1, max_steps + 1):
            self._check_stop()
            if ctx.state.strikes >= tools.SANDBOX_STRIKES:
                act.end_reason = "too many refused file operations"
                break
            turns = self._with_pending(act)
            if _size(turns) > MAX_CONVERSATION_BYTES:
                act.end_reason = "the conversation got too long"
                break
            final = step == max_steps
            request = prompts.work_request(
                self.settings,
                brief,
                turns,
                final=final,
                mail=self.mail,
                etsy=self.etsy_on,
                venture=ctx.venture,
                library=self.library_on,
            )
            if not self._affordable(cycle_id, request, brief, turns, ctx):
                act.end_reason = "the budget left in this cycle is kept for reflecting" if act.steps else NO_STEP
                break
            self._progress(cycle_id, step=step, current_action=f"Working (tool step {step}, at most {max_steps})")
            try:
                result = self._call(cycle_id, "work", request)
            except CallRefused as exc:
                if exc.category in ("cap", "balance"):
                    act.end_reason = f"refused: {exc.reason}"
                    break
                raise EndCycle("stopped", f"refused: {exc.reason}") from exc
            except CallFailed as exc:
                act.end_reason = f"failed: {exc.result.error or exc.result.status}"
                break
            act.steps += 1
            if act.steps == 1:  # the brief reached the model
                with self.db.transaction() as conn:
                    news.mark_seen(conn, cycle_id, seen)
            act.turns = turns
            act.pending = []
            response = result.response or {}
            content = response.get("content") or []
            stop = response.get("stop_reason")
            if not content:
                # The API rejects an empty assistant turn, so it is never sent back: nudge inside the last
                # user turn instead (once), or end the act phase.
                can_nudge = act.turns and act.turns[-1]["role"] == "user" and step < max_steps
                if nudged < MAX_EMPTY_NUDGES and can_nudge:
                    nudged += 1
                    act.turns[-1] = {
                        "role": "user",
                        "content": [*act.turns[-1]["content"], {"type": "text", "text": NUDGE}],
                    }
                    continue
                act.end_reason = "done"
                break
            act.turns.append({"role": "assistant", "content": content})
            uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            text = _text_of(response)
            if text:
                self._save_text(result.call_id, text, response)
            if stop == "tool_use" and uses:
                act.pending = self._run_tools(ctx, uses, result.call_id, "act")
                if step == max_steps:
                    act.end_reason = "step limit reached"
                continue
            if uses:  # never run a tool call that may be incomplete: cut off by max_tokens, only the last one is
                whole = _whole_calls(content, uses) if stop == "max_tokens" else []
                why = CUT_CALL if stop == "max_tokens" else f"the reply ended ({stop}) before it could run"
                act.pending = self._run_tools(ctx, whole, result.call_id, "act") if whole else []
                act.pending += [
                    self._result_block(
                        u,
                        tools.skip(
                            ctx, u.get("name", "?"), u.get("input"), u.get("id", ""), result.call_id, "act", why
                        ),
                    )
                    for u in uses[len(whole) :]
                ]
            if stop == "refusal":
                act.end_reason = "refusal"
                return act
            if stop == "end_turn":
                if not text.strip() and not act.pending and nudged < MAX_EMPTY_NUDGES and step < max_steps:
                    nudged += 1
                    act.turns.append({"role": "user", "content": [{"type": "text", "text": NUDGE}]})
                    continue
                act.report = text
                act.end_reason = "done"
                break
            if stop == "max_tokens":
                if not act.pending:  # the conversation must end with a user turn (no prefill)
                    act.turns.append({"role": "user", "content": [{"type": "text", "text": CUT_OFF}]})
                continue
            act.end_reason = f"the model stopped ({stop})"
            break
        else:
            act.end_reason = act.end_reason or "step limit reached"
        return act

    def _with_pending(self, act: _Act) -> list[dict[str, Any]]:
        if not act.pending:
            return list(act.turns)
        return [*act.turns, {"role": "user", "content": list(act.pending)}]

    def _affordable(
        self, cycle_id: int, request: dict[str, Any], brief: str, turns: list[dict[str, Any]], ctx: tools.ToolContext
    ) -> bool:
        """Only take a step if a reflect call still fits after it; what that costs is kept in the cycle's state, for
        the step's calls of their own (research, brainstorms, workshop runs) to leave (0.12.0)."""
        venture = ctx.venture
        growth = _step_growth(ctx.state, turns)
        # The reflection as _reflect sends it: a trailing user turn's tool results come with its prompt.
        past, pending = list(turns), []
        if past and past[-1]["role"] == "user":
            pending = [b for b in past.pop()["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        longest = "ä" * prompts.ENDED_CHARS  # the reflection is told why the work ended: priced with the longest reason
        try:
            step_cost = self.meter.quote(request, "work")
            reflect_cost = self.meter.quote(
                prompts.reflect_request(
                    self.settings,
                    brief,
                    past,
                    pending,
                    mail=self.mail,
                    etsy=self.etsy_on,
                    ended=longest,
                    venture=venture,
                    library=self.library_on,
                ),
                "reflect",
                extra_tokens=growth,
            )
        except Unpriceable:
            return False
        ctx.state.reflect_reserve = reflect_cost
        return step_cost + reflect_cost <= self.meter.headroom(cycle_id)

    def _run_tools(
        self, ctx: tools.ToolContext, uses: list[dict[str, Any]], call_id: int, phase: str
    ) -> list[dict[str, Any]]:
        results = []
        counted = 0
        for use in uses:
            name, raw, use_id = use.get("name", "?"), use.get("input"), use.get("id", "")
            self._check_stop()
            # 0.12.0: the journal never counts toward the limit (a fifth call, it was skipped and the cycle lost it).
            counted += name != "write_journal"
            if counted > MAX_TOOL_CALLS_PER_TURN and name != "write_journal":
                outcome = tools.skip(
                    ctx, name, raw, use_id, call_id, phase, f"at most {MAX_TOOL_CALLS_PER_TURN} tool calls per turn"
                )
            else:
                with self.db.transaction() as conn:
                    store.update_cycle(conn, ctx.cycle_id, current_action=f"Using {name}"[:300])
                outcome = tools.run(ctx, name, raw, use_id, call_id, phase)
            results.append(self._result_block(use, outcome))
        return results

    @staticmethod
    def _result_block(use: dict[str, Any], outcome: tools.Outcome) -> dict[str, Any]:
        content: str | list[dict[str, Any]] = outcome.text or "-"
        if outcome.image is not None:
            content = [{"type": "text", "text": outcome.text or "-"}, tools.image_block(outcome.image)]
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": use.get("id", ""),
            "content": content,
        }
        if not outcome.ok:
            block["is_error"] = True
        return block

    def _reflect(self, cycle_id: int, ctx: tools.ToolContext, brief: str, act: _Act) -> bool:
        self._check_stop()
        turns = list(act.turns)
        pending = list(act.pending)
        if turns and turns[-1]["role"] == "user":
            # A trailing user turn (tool results plus a nudge, or a nudge alone): keep its tool results,
            # which answer the tool calls before it, and let the reflect prompt replace the rest.
            kept = [b for b in turns.pop()["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
            pending = [*kept, *pending]
        request = prompts.reflect_request(
            self.settings,
            brief,
            turns,
            pending,
            mail=self.mail,
            etsy=self.etsy_on,
            ended=act.end_reason,
            venture=ctx.venture,
            library=self.library_on,
        )
        # 0.12.0: why the work ended is kept first, also when the reflection can't be paid for.
        self._progress(cycle_id, act_end_reason=act.end_reason[:300] or None)
        try:
            if self.meter.quote(request, "reflect") > self.meter.headroom(cycle_id, "reflect"):
                return False
        except Unpriceable:
            return False
        self._progress(cycle_id, phase="reflect", current_action="Reflecting")
        try:
            result = self._call(cycle_id, "reflect", request)
        except (CallRefused, CallFailed):
            return False
        response = result.response or {}
        text = _text_of(response)
        if text:
            self._save_text(result.call_id, text, response)
        content = response.get("content") or []
        uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if response.get("stop_reason") == "tool_use":
            self._run_tools(ctx, uses, result.call_id, "reflect")
        elif response.get("stop_reason") == "max_tokens" and uses:  # 0.11.1: its whole calls used to be lost too
            whole = _whole_calls(content, uses)
            if whole:
                self._run_tools(ctx, whole, result.call_id, "reflect")
            for u in uses[len(whole) :]:
                tools.skip(
                    ctx, u.get("name", "?"), u.get("input"), u.get("id", ""), result.call_id, "reflect", CUT_CALL
                )
        if not ctx.state.journal_written and text.strip():
            with self.db.transaction() as conn:
                first = text.strip().splitlines()[0][:240]
                ctx.state.journal_written = store.write_journal(
                    conn, self.scope, cycle_id, "agent", first, text.strip()[:2000], to_iso(self.clock.now())
                )
        return True

    # --- research (a metered sub-call with Anthropic's web tools) ---

    def _research_fn(self, ctx: tools.ToolContext) -> tools.ResearchFn:
        def research(
            question: str, url: str | None, cycle_id: int, site: str | None = None, venture_id: int | None = None
        ) -> tools.Outcome:
            request = prompts.research_request(self.settings, question, url, site)
            try:
                quote = self.meter.quote(request, "research")
            except Unpriceable as exc:
                return tools.Outcome(False, f"Error: research can't be priced ({exc}).", "refused: unpriceable")
            if quote > self.meter.headroom(cycle_id, keep=ctx.state.reflect_reserve):
                return tools.Outcome(
                    False,
                    f"Error: research could cost up to ${micros_to_usd(quote):.3f}, more than this cycle has left"
                    f"{_kept(ctx)}.",
                    "refused: budget",
                )
            try:
                result = self._call(cycle_id, "research", request)
            except CallRefused as exc:  # a state or system refusal ends the cycle at its next call
                return tools.Outcome(False, f"Error: research refused ({exc.reason}).", "refused")
            except CallFailed as exc:
                blocked = _BLOCKED_SITES.search(exc.result.error or "")
                if blocked:
                    return tools.Outcome(
                        False,
                        f"Error: {blocked[1]} blocks Anthropic's web tools, so your research can't search or read it:"
                        " search without a site, or limit it to another site.",
                        "failed: site blocks the web tools",
                        paid=True,
                    )
                return tools.Outcome(
                    False, f"Error: research failed ({exc.result.error or exc.result.status}).", "failed", paid=True
                )
            response = result.response or {}
            cost = result.cost_micros
            if response.get("stop_reason") == "pause_turn":
                follow = dict(request)
                follow["messages"] = [
                    *request["messages"],
                    {"role": "assistant", "content": response.get("content") or []},
                ]
                try:
                    more = self._call(cycle_id, "research", follow)
                    response = more.response or response
                    cost += more.cost_micros
                except (CallRefused, CallFailed):
                    pass
            digest = _text_of(response)[:2_000] or "Nothing useful was found."
            sources = _sources(response)
            ctx.state.seen_urls.update(sources)
            self._save_text(result.call_id, digest, response)
            body = tools.wrap(ctx, "research", digest)
            source_text = ("\nSources:\n" + "\n".join(f"- {u}" for u in sources[:5])) if sources else ""
            counted = ""
            if venture_id is not None:  # 0.12.0: research bound to a venture
                with self.db.transaction() as conn:
                    found = ventures.add_research(
                        conn,
                        venture_id,
                        cycle_id,
                        result.call_id,
                        question,
                        url,
                        len(sources),
                        cost,
                        to_iso(self.clock.now()),
                    )
                counted = (
                    f"\nResearch for venture #{venture_id}: {found} call{'s' if found != 1 else ''} that found "
                    "something."
                    if sources
                    else f"\nIt found no web page, so it doesn't count as research for venture #{venture_id}."
                )
            return tools.Outcome(
                True,
                f"{body}{source_text}{counted}\n(cost ${micros_to_usd(cost):.4f})",
                f"research: {question[:80]}",
            )

        return research

    # --- brainstorms (0.10.0: a metered call on the planner's model that grows the venture tree) ---

    def _brainstorm_fn(self, ctx: tools.ToolContext) -> tools.BrainstormFn:
        def brainstorm(theme: str, venture_id: int | None) -> tools.Outcome:
            with self.db.connection() as conn:
                tree = ventures.all_ventures(conn, self.scope)
                parent = ventures.get(conn, self.scope, venture_id) if venture_id is not None else None
                standing = store.standing_instructions(conn, self.scope)
            if venture_id is not None and (parent is None or parent["stage"] == "killed"):
                return tools.Outcome(False, f"Error: there is no venture #{venture_id} to branch from.", "refused")
            if len(tree) >= ventures.MAX_VENTURES:
                return tools.Outcome(
                    False, f"Error: the tree holds {ventures.MAX_VENTURES} ventures, as many as it can.", "refused"
                )
            status = self.economy.life.evaluate()
            earned = self.economy.books.totals(self.economy.life.scope())["revenue"]
            request = prompts.brainstorm_request(
                self.settings,
                _brainstorm_context(status, earned, standing["text"] if standing else "", tree, parent, theme),
            )
            try:
                quote = self.meter.quote(request, "brainstorm")
            except Unpriceable as exc:
                return tools.Outcome(False, f"Error: a brainstorm can't be priced ({exc}).", "refused: unpriceable")
            if quote > self.meter.headroom(ctx.cycle_id, keep=ctx.state.reflect_reserve):
                return tools.Outcome(
                    False,
                    f"Error: a brainstorm could cost up to ${micros_to_usd(quote):.3f}, more than this cycle has left"
                    f"{_kept(ctx)}.",
                    "refused: budget",
                )
            try:
                result = self._call(ctx.cycle_id, "brainstorm", request)
            except CallRefused as exc:  # a state or system refusal ends the cycle at its next call
                return tools.Outcome(False, f"Error: the brainstorm was refused ({exc.reason}).", "refused")
            except CallFailed as exc:
                return tools.Outcome(
                    False,
                    f"Error: the brainstorm failed ({exc.result.error or exc.result.status}).",
                    "failed",
                    paid=True,
                )
            response = result.response or {}
            text = _text_of(response)
            self._save_text(result.call_id, text, response)
            cost = f"(cost ${micros_to_usd(result.cost_micros):.4f})"
            ideas = _ideas(text) if response.get("stop_reason") == "end_turn" else []
            if not ideas:
                return tools.Outcome(
                    False, f"Error: the brainstorm brought no usable ideas {cost}.", "failed: no ideas", paid=True
                )
            added, known = self._plant(ctx, ideas, parent)
            self._keep_ideas(ctx, added, known, parent, theme)
            lines = [
                f"#{vid} {idea['title']} · {ventures.scores_text({**idea['scores'], 'scores_by': 'brainstorm'})}\n"
                f"   {idea['pitch']}\n   first question: {idea['first_question'] or '-'}"
                for vid, idea in added
            ]
            if known:
                lines.append("Already in the tree, so not added again: " + "; ".join(known) + ".")
            where = f" as branches of #{parent['id']}" if parent else ""
            head = (
                f"The brainstorm added {len(added)} ideas to your tree{where} {cost}; all are in {ventures.IDEAS_FILE}."
            )
            return tools.Outcome(True, "\n".join([head, *lines]), f"brainstorm: {len(added)} ideas")

        return brainstorm

    def _plant(
        self, ctx: tools.ToolContext, ideas: list[dict[str, Any]], parent: Any
    ) -> tuple[list[tuple[int, dict[str, Any]]], list[str]]:
        """The brainstorm's ideas as new ventures (stage idea, scores guessed); the titles already in the tree."""
        added: list[tuple[int, dict[str, Any]]] = []
        known: list[str] = []
        now = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            room = ventures.MAX_VENTURES - ventures.count(conn, self.scope)
            for idea in ideas[: ventures.BRAINSTORM_IDEAS]:
                if room <= 0 or ventures.by_title(conn, self.scope, idea["title"]) is not None:
                    known.append(idea["title"])
                    continue
                venture_id = ventures.create(
                    conn,
                    self.scope,
                    title=idea["title"],
                    pitch=idea["pitch"],
                    stage="idea",
                    now=now,
                    cycle_id=ctx.cycle_id,
                    next_question=idea["first_question"],
                    parent_id=parent["id"] if parent is not None else None,
                    scores=idea["scores"],
                    scores_by="brainstorm",
                )
                room -= 1
                added.append((venture_id, idea))
        return added, known

    def _keep_ideas(
        self, ctx: tools.ToolContext, added: list[tuple[int, dict[str, Any]]], known: list[str], parent: Any, theme: str
    ) -> None:
        """Every brainstorm's ideas in one file of the agent's workspace (a full file starts again)."""
        about = [f"branch of #{parent['id']} {parent['title']}"] if parent is not None else []
        if theme:
            about.append(f"theme: {theme}")
        lines = [
            f"\n## {self.clock.today().isoformat()}, cycle #{ctx.cycle_id}"
            + (f" ({'; '.join(about)})" if about else "")
        ]
        for venture_id, idea in added:
            lines.append(f"- #{venture_id} {idea['title']}: {idea['pitch']} First question: {idea['first_question']}")
        lines.extend(f"- (already in the tree) {title}" for title in known)
        section = "\n".join(lines) + "\n"
        try:
            self.workspace.write(ventures.IDEAS_FILE, section, append=True)
        except SandboxError:
            try:
                self.workspace.write(
                    ventures.IDEAS_FILE, "# Brainstorms (the older ones are in your venture tree)\n" + section
                )
            except SandboxError:
                log.warning("Could not keep the brainstorm's ideas in %s", ventures.IDEAS_FILE)

    # --- the workshop (metered sub-calls with Anthropic's code execution tool) ---

    def _workshop_fn(self, ctx: tools.ToolContext) -> tools.WorkshopFn:
        shop = Workshop(self.db, self.clock, self.settings, self.meter, self.scope, self.workspace)

        def workshop(task: str, files: list[str], script: str | None, folder: str | None) -> tools.Outcome:
            try:
                run = shop.run(ctx.cycle_id, task, files, script, folder, keep=ctx.state.reflect_reserve)
            except WorkshopError as exc:
                return tools.Outcome(False, f"Error: {exc}.", f"refused: {exc}"[:300])
            except CallRefused as exc:  # the budget guard's state or system refusal: the next call ends the cycle
                return tools.Outcome(False, f"Error: the workshop was refused ({exc.reason}).", "refused")
            ok, text, summary = workshop_report(run, lambda source, body: tools.wrap(ctx, source, body))
            return tools.Outcome(ok, text, summary, paid=run.calls > 0)

        return workshop

    # --- the last will ---

    def _last_will(self, cycle_id: int) -> CycleEnd:
        snap = self._snapshot()
        self._progress(cycle_id, phase="last_will", current_action="Writing the last will")
        request = prompts.will_request(self.settings, context.will_context(snap, self.dry_run))
        if not context.fits(request, LAST_WILL.input_tokens):
            request = prompts.will_request(self.settings, context.cut(context.will_context(snap, self.dry_run), 2_000))
        try:
            result = self._call(cycle_id, "last_will", request)
        except CallRefused as exc:
            return CycleEnd("refused", f"last will refused: {exc.reason}")
        except CallFailed as exc:
            return CycleEnd("failed", f"last will failed: {exc.result.error or exc.result.status}")
        response = result.response or {}
        text = _text_of(response).strip()
        if not text:
            return CycleEnd("failed", "the last will was empty")
        cut_off = response.get("stop_reason") == "max_tokens"
        now = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            store.save_call_text(conn, result.call_id, text)
            store.save_last_will(conn, self.scope.life_id, cycle_id, result.call_id, text, cut_off, now)
            self.economy.life.record_last_will(conn, self.scope.life_id, now)
            store.write_journal(conn, self.scope, cycle_id, "agent", "Wrote my last will", text[:2000], now)
            events.record(
                self.db, "warning", "agent", f"{self.settings.agent_name} wrote a last will", {"cycle_id": cycle_id}
            )
        return CycleEnd("completed", "last will written")

    def _save_text(self, call_id: int, text: str, response: dict[str, Any]) -> None:
        details = response.get("stop_details") if isinstance(response.get("stop_details"), dict) else None
        with self.db.transaction() as conn:
            store.save_call_text(conn, call_id, text, details)


def _step(text: str) -> str:
    """A plan step as the brief shows it: at most STEP_CHARS characters, a longer one cut with "…" (0.11.1: steps were
    cut silently, often the first one, which answers the owner)."""
    return text if len(text) <= STEP_CHARS else text[: STEP_CHARS - 1].rstrip() + "…"


def _whole_calls(content: list[Any], uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool calls of a reply cut off by max_tokens that are whole: all but one that is the reply's last block (the
    only block the cut can have left incomplete)."""
    return uses[:-1] if uses and content and content[-1] is uses[-1] else uses


def _text_of(response: dict[str, Any]) -> str:
    parts = [
        b.get("text", "")
        for b in response.get("content") or []
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return "\n".join(p for p in parts if p)


def _sources(response: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for block in response.get("content") or []:
        if not isinstance(block, dict) or not str(block.get("type", "")).endswith("_tool_result"):
            continue
        content = block.get("content")
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"] not in urls:
                urls.append(item["url"][:300])
    return urls


def _code_journal(conn: Any, cycle_id: int) -> str:
    """0.12.0: the journal of a cycle whose reflection wrote none, built by Ember's code from its records: the goal,
    what its tools did (and what was refused or skipped, so it isn't taken for done) and what it cost. It was only
    "Goal: …"."""
    row = conn.execute("SELECT plan FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    try:
        plan = json.loads(row["plan"]) if row and row["plan"] else {}
    except ValueError:
        plan = {}
    goal = plan.get("goal") if isinstance(plan, dict) else None
    lines = [
        "Written by Ember's code: the reflection wrote no journal.",
        f"Goal: {goal}" if goal else "No plan was made.",
    ]
    calls = conn.execute(
        "SELECT tool, status, summary FROM tool_calls WHERE cycle_id = ? AND parent_id IS NULL ORDER BY id", (cycle_id,)
    ).fetchall()
    done = [f"{c['tool']} ({' '.join(str(c['summary'] or '').split())[:80]})" for c in calls if c["status"] == "ok"]
    other = [f"{c['tool']} ({c['status']})" for c in calls if c["status"] != "ok"]
    if done:
        lines.append(f"Done: {'; '.join(done[:15])}" + (f"; and {len(done) - 15} more" if len(done) > 15 else ""))
    if other:
        lines.append(f"Refused, failed or skipped (not done): {'; '.join(other[:10])}")
    if not calls:
        lines.append("No tool was used.")
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_micros), 0) FROM llm_calls WHERE cycle_id = ? AND status IN ('ok', 'interrupted')",
        (cycle_id,),
    ).fetchone()[0]
    lines.append(f"Cost: ${micros_to_usd(int(cost)):.4f}")
    return "\n".join(lines)[:2_000]


def _kept(ctx: tools.ToolContext) -> str:
    """What a refusal says of the reflection's reserve (0.12.0), if there is one."""
    reserve = ctx.state.reflect_reserve
    return f" after the ${micros_to_usd(reserve):.3f} kept for your reflection" if reserve else ""


def _brainstorm_context(status: Any, earned: int, instructions: str, tree: list[Any], parent: Any, theme: str) -> str:
    """What a brainstorm is told: the owner's standing instructions, the money, the tree so far and the task."""
    runway = f"{status.runway.days:.0f} days" if status.runway.days is not None else "unknown"
    quoted = json.dumps(instructions, ensure_ascii=False) if instructions.strip() else "None."
    task = "Find ideas in new ground: anything that fits the agent and its owner and isn't in the tree yet."
    if parent is not None:
        task = (
            f"Grow the tree from #{parent['id']} {json.dumps(parent['title'], ensure_ascii=False)} ({parent['stage']}):"
            " its variants, niches, customers, channels and next steps. Its pitch: "
            + json.dumps(parent["pitch"], ensure_ascii=False)
        )
    if theme:
        task += f"\nTheme: {json.dumps(theme, ensure_ascii=False)}"
    return "\n\n".join(
        [
            f"THE OWNER'S STANDING INSTRUCTIONS (their words)\n{quoted}",
            f"MONEY\nBalance ${micros_to_usd(status.balance):.2f}, runway {runway} at the recent spending;"
            f" revenue so far ${micros_to_usd(earned):.2f}.",
            "THE VENTURE TREE (don't repeat these ideas; branch from them or go somewhere new)\n"
            + ventures.tree_text(tree),
            f"TASK\n{task}",
        ]
    )


def _ideas(text: str) -> list[dict[str, Any]]:
    """The brainstorm's ideas: titles, pitches and questions cut to their limits, scores kept only from 1 to 5."""
    data: Any = None
    for candidate in (text, _first_object(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            break
        except ValueError:
            continue
    items = data.get("ideas") if isinstance(data, dict) else None
    ideas = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[: ventures.LIMITS["title"]]
        pitch = " ".join(str(item.get("pitch") or "").split())[: ventures.LIMITS["pitch"]]
        if not title or not pitch:
            continue
        scores = {}
        for name in ventures.SCORE_FIELDS:
            value = item.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5:
                scores[name] = value
        question = " ".join(str(item.get("first_question") or "").split())[: ventures.LIMITS["next_question"]]
        ideas.append({"title": title, "pitch": pitch, "first_question": question, "scores": scores})
    return ideas


# An error the API gives for a request that fails as it is (a retry can't help); 408, 409 and 429 can pass later.
_REJECTED = re.compile(r"^HTTP 4(?!08|09|29)\d\d\b")
# The API's words for a site that blocks Anthropic's web tools, e.g. "... not accessible to our user agent: ['x.com']".
_BLOCKED_SITES = re.compile(r"not accessible to our user agent: \[['\"]?([^'\"\]]+)")


def _rejected(error: str | None) -> bool:
    return bool(_REJECTED.match(error or ""))


def _first_object(text: str) -> str | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else None


def _step_growth(state: tools.CycleTools, turns: list[dict[str, Any]]) -> int:
    """What the next step may add to the conversation, in tokens (0.12.0): 1.5 times the largest step of this cycle so
    far (measured like the rough token count: half a token a byte, a picture by its pixels), at least
    STEP_GROWTH_TOKENS. ``turns`` is the conversation before the next step."""
    size = math.ceil(_size(turns) / 2)
    if state.conversation_tokens:
        state.largest_step_tokens = max(state.largest_step_tokens, size - state.conversation_tokens)
    state.conversation_tokens = size
    return max(STEP_GROWTH_TOKENS, math.ceil(state.largest_step_tokens * STEP_GROWTH_FACTOR))


def _size(turns: list[dict[str, Any]]) -> int:
    """The conversation's size in bytes of JSON, a picture counted by its pixels (``_picture_bytes``), not its data."""
    pictures = 0

    def without_pictures(node: Any) -> Any:
        nonlocal pictures
        if isinstance(node, dict):
            if node.get("type") == "image":
                pictures += _picture_bytes(node)
                return {}
            return {key: without_pictures(value) for key, value in node.items()}
        if isinstance(node, list):
            return [without_pictures(value) for value in node]
        return node

    text = json.dumps(without_pictures(turns), ensure_ascii=False)
    return len(text.encode("utf-8")) + pictures


def _picture_bytes(block: dict[str, Any]) -> int:
    """How much text a picture counts as: IMAGE_EQUIVALENT_BYTES for the largest a look shows, less for fewer pixels."""
    source = block.get("source")
    data = source.get("data") if isinstance(source, dict) else None
    size = picture_size(data) if isinstance(data, str) else None
    if size is None:
        return IMAGE_EQUIVALENT_BYTES
    width, height = size
    return min(IMAGE_EQUIVALENT_BYTES, math.ceil(width * height * IMAGE_EQUIVALENT_BYTES / tools.LOOK_PIXELS**2))
