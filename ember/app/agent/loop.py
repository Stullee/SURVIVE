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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .. import events, paths
from ..config import Settings
from ..db import Database
from ..economy import burn
from ..economy.clock import Clock, to_iso
from ..economy.costs import micros_to_usd
from ..economy.estimate import Unpriceable
from ..economy.metering import (
    CONSOLIDATE,
    CRITIC,
    EVENT_RESERVE_HOUR,
    RESEARCH_CHECK,
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
from ..integrations import (
    bluesky_publisher,
    etsy_publisher,
    mailstore,
    pinterest_publisher,
    printify_publisher,
    site_publisher,
)
from ..integrations.bluesky_connection import BlueskyConnection
from ..integrations.etsy_connection import EtsyConnection
from ..integrations.etsy_publisher import Publisher
from ..integrations.mail import Mailbox
from ..integrations.pinterest_connection import PinterestConnection
from ..integrations.printify import PrintifyError
from ..integrations.printify_connection import PrintifyConnection
from ..version import app_version
from . import (
    agenda,
    bets,
    context,
    critic,
    desk,
    digest,
    econ,
    evidence,
    gates,
    knockouts,
    learning,
    library,
    metrics,
    netguard,
    news,
    obligations,
    policy,
    predictions,
    prompts,
    quality,
    research_check,
    review,
    roadmap,
    slack,
    stages,
    store,
    tools,
    ventures,
    website,
    weekly,
)
from . import memory as memory_files
from .memory import Memory
from .sandbox import Jail, SandboxError
from .store import AgentScope
from .workshop import Workshop, WorkshopError
from .workshop import report as workshop_report

log = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_TURN = tools.MAX_TOOL_CALLS_PER_TURN
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
STEP_CHARS = prompts.STEP_CHARS  # a plan step's length (prompts.PLANNER_RULES tells the planner)
RESEARCH_DIGEST_CHARS = 2_000  # a research digest kept (prompts.RESEARCH_RULES asks for less)
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
WEEKLY_INPUT_TOKENS = 12_000  # 0.18.0: the weekly look's prompt at most (its view is cut at weekly.VIEW_CHARS)
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
    sleep_cut: str | None = None  # 0.19.2: why Ember's code cut the sleep the agent chose (asked: what it chose)
    asked_minutes: int | None = None


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
    ready: str = ""  # 0.13.0: a venture plan's READY item (its key), or "none: " and why

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
            **({"ready": self.ready} if self.ready else {}),
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
        pinterest: PinterestConnection | None = None,
        pins: pinterest_publisher.Publisher | None = None,
        printify: PrintifyConnection | None = None,
        pod: printify_publisher.Publisher | None = None,
        unlocks_off: str | None = None,
        bluesky: BlueskyConnection | None = None,
        bluesky_posts: bluesky_publisher.Publisher | None = None,
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
        self.pinterest = pinterest  # 0.13.0 (Phase E2): the owner's Pinterest account
        self.pins = pins
        self.pinterest_on = False  # the Pinterest tools and the PINTEREST section: with the account and a shop
        self.bluesky = bluesky  # 0.19.0: the Bluesky account the owner made for Ember
        self.bluesky_posts = bluesky_posts
        self.bluesky_on = False  # the Bluesky tools and the BLUESKY section: with the account
        self.printify = printify  # 0.13.0 (Phase E4): the owner's Printify account
        self.pod = pod
        self.printify_on = False  # the Printify tools and the PRINTIFY section: with the account, its shop and ours
        self.printify_waits = ""  # 0.15.0: why Printify's tools are off although it is set up (its shop isn't known)
        self.site_on = settings.site_enabled  # 0.13.0 (Phase E3): the owner's website: its tool and WEBSITE section
        self.blog_on = settings.blog_enabled  # 0.14.0: the owner's blog: its tools and BLOG section
        self.library_on = False  # the library's tools (0.12.0): set when a cycle starts with documents in it
        self.news_kept: frozenset[news.Item] = frozenset()  # 0.15.0: the owner's news marked seen once the cycle ends
        self.net_runway_days: float | None = None  # at the last snapshot (0.13.0: the knock-outs' slow rule)
        self.ready_items: list[desk.Item] = []  # 0.13.0: the READY list the last venture plan was shown
        self.slack_items: list[slack.Item] = []  # 0.18.0: the READY list the last ordinary plan was shown
        self.reactive = False  # 0.13.0: a cycle an event woke (run sets it)
        self.max_steps = settings.max_tool_steps
        # 0.15.0: why the owner's unlocks don't act in this cycle (policy.off; the service knows safe mode)
        self.unlocks_off = policy.off(settings.owner_user_ids, False) if unlocks_off is None else unlocks_off

    # --- the cycle ---

    def planner_preview(self, venture: bool) -> str:
        """The planner's context as a wake cycle would build it now (the diagnostics report shows it): nothing is
        fetched, synced, marked or spent. 0.15.0: and nothing kept: Ember's code's keepers run in a cycle only (they
        ran here, outside the cycle's lock, and parked ventures and closed milestones when the report was made). So
        what they would change now (an obligation, a grade, a settled forecast) shows only after the next cycle."""
        self.etsy_on = self.etsy is not None and self.publisher is not None and self.etsy.shop() is not None
        self.pinterest_on = self.etsy_on and self.pinterest is not None and self.pinterest.account() is not None
        self.printify_on = self.etsy_on and self.printify is not None and self.printify.account() is not None
        self.bluesky_on = self.bluesky is not None and self.bluesky.account() is not None
        snap = self._snapshot(venture, keep=False)
        planner = ""
        for scale in PLANNER_SCALES:
            planner, _ = context.planner_context(snap, self.dry_run, scale)
            if context.fits(
                prompts.plan_request(self.settings, planner, venture=venture), PLANNER_OPENING.input_tokens
            ):
                break
        return planner

    def run(self, trigger: str) -> CycleEnd:
        # 0.13.0: an event's wake-up is a lean reactive cycle: no venture work, review, study or critic, few steps
        self.reactive = trigger == "event"
        self.news_kept = frozenset()  # 0.15.0: set by the first work step (a cycle that plans no work keeps none)
        self.max_steps = (
            min(self.settings.max_tool_steps, agenda.REACTIVE_STEPS) if self.reactive else self.settings.max_tool_steps
        )
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
            usd_per_eur=self.settings.etsy_usd_per_eur,  # 0.13.0: a venture case's rate (0: econ assumes one)
            venture_cash_eur=self.settings.venture_cash_eur,  # 0.13.0: the knock-outs' cash budget
            unlocks_off=self.unlocks_off,  # 0.15.0
        )
        ctx.research = self._research_fn(ctx)
        ctx.draft = self._draft_fn(ctx)
        end = CycleEnd("failed", "the cycle ended unexpectedly")
        try:
            mode = self._cycle_burn(cycle_id)
            # 0.15.0: no workshop runs in maintenance (a run costs about what the whole cycle may)
            ctx.workshop = self._workshop_fn(ctx) if prompts.workshop_on(self.settings) and mode.workshop else None
            if trigger != "last_will":
                ctx.venture = self._venture_cycle(cycle_id, trigger) if not self.reactive else False
                # 0.12.0: brainstorms only while the burn mode is explore
                ctx.brainstorm = self._brainstorm_fn(ctx) if ctx.venture and mode.brainstorms else None
                with self.db.connection() as conn:
                    self.library_on = ctx.library = library.totals(conn, self.scope)[0] > 0
                self._fetch_mail(cycle_id)
                self._sync_etsy(cycle_id, ctx)
                self._sync_pinterest(cycle_id, ctx)
                self._sync_bluesky(cycle_id, ctx)
                self._sync_printify(cycle_id, ctx)
                ctx.site = website.owner(self.settings) if self.site_on else None  # 0.13.0 (Phase E3)
                ctx.blog = self._blog_access() if self.blog_on else None  # 0.14.0
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
        # 0.18.0: no long sleep while work waits. 0.21.0: never an idle plan's (the agent chose to do nothing), nor
        # below the owner's default interval.
        if end.status == "completed" and trigger != "last_will":
            mode_now = burn.peek(self.db, self.economy.life.evaluate()).mode
            shortest = max(self.settings.min_sleep_minutes, self.settings.wake_interval_minutes)
            kept = slack.sleep(end.sleep_minutes, self.slack_items, shortest, mode_now)
            if kept != end.sleep_minutes:  # 0.19.2: the agent's choice and words stay ("Ember chose" the cut)
                end.asked_minutes, end.sleep_minutes = end.sleep_minutes, kept
                end.sleep_cut = f"Ember's code cut it to {kept} min: READY lists useful work"
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
            # 0.12.0: every cycle's digest, from its records, and its journal if the agent wrote none. The guard may
            # have closed it already (an overrun): 0.15.0: both say how it really ended (the journal named the refusal
            # that followed the stop, or "completed").
            row = conn.execute("SELECT status, note FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
            ended = row is not None and row["status"] != "running"
            status = row["status"] if ended else "failed" if end.status == "skipped" else end.status
            write_records(conn, self.scope, cycle_id, status, row["note"] if ended else end.note, now)
            if status in ("completed", "idle"):  # 0.15.0: the owner's news its work saw (_act)
                news.mark_seen(conn, cycle_id, self.news_kept)
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
        # 0.15.0: the sleep it chose (the scheduler may cut it: the dashboard's next wake says)
        tail = f"; chose {end.asked_minutes or end.sleep_minutes} min of sleep" if end.sleep_minutes else ""
        tail += f", cut to {end.sleep_minutes} (READY lists useful work)" if end.sleep_cut else ""
        level = "info" if final in ("completed", "idle") else "warning"
        events.record(
            self.db,
            level,
            "agent",
            f"Cycle #{cycle_id} {final}: ${micros_to_usd(spent):.4f}" + (f" ({end.note})" if end.note else "") + tail,
            {"cycle_id": cycle_id},
        )

    def _cycle_burn(self, cycle_id: int) -> burn.Burn:
        """0.15.0: the burn mode the cycle opened in (its row), the one the money guard uses too."""
        with self.db.connection() as conn:
            row = conn.execute("SELECT burn_mode FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if row is None or row["burn_mode"] is None:
            return burn.peek(self.db, self.economy.life.evaluate())
        return burn.Burn(row["burn_mode"], None)

    def _venture_cycle(self, cycle_id: int, trigger: str = "schedule") -> bool:
        """Whether this is a venture cycle: venture cycles have had less than the owner's share of the day's spending
        (``ventures.venture_turn``). Recorded on the cycle; an empty venture tree gets its first ideas first. 0.19.3:
        the owner's waiting messages make it an ordinary cycle only when one woke it (``trigger`` 'owner': they wait
        for what they asked); otherwise the venture cycle answers them first. Live, messages turned 4 of 12 cycles
        that were the ventures' turn into ordinary ones, and 2 of 12 were venture cycles."""
        allowed = self._burn_allows_ventures()  # 0.12.0: read before the transaction
        with self.db.transaction() as conn:
            ventures.seed(conn, self.scope, to_iso(self.clock.now()))
            spent, ventured = ventures.day_spend(conn, self.scope, self.clock.today())
            turn = allowed and ventures.venture_turn(self.settings.venture_share, spent, ventured)
            # 0.12.0: what the agent owes comes first (a venture cycle deferred the owner's quick fix)
            today = self.clock.today()
            owed = obligations.pressing(conn, self.scope, today, messages=trigger == "owner") if turn else []
            if turn and not owed:
                store.update_cycle(conn, cycle_id, venture=1)
        if owed:
            events.record(
                self.db,
                "info",
                "agent",
                f"Cycle #{cycle_id} is an ordinary cycle: {owed[0]} {_comes(owed[0])} first"[:300],
            )
        return turn and not owed

    def _burn_allows_ventures(self) -> bool:
        """0.12.0: venture cycles run in the explore burn mode (0.19.3: no longer in focus, where they ran only the
        backed ventures' tests: a backed venture's work is its project's, in ordinary cycles); not in maintenance or
        dormant."""
        return burn.peek(self.db, self.economy.life.evaluate()).venture_cycles

    def _expire_requests(self) -> None:
        """0.12.0: the requests the owner didn't decide within their type's days expire (news for the agent)."""
        with self.db.transaction() as conn:
            expired = store.expire_requests(conn, self.scope, to_iso(self.clock.now()))
        for r in expired:
            days = store.REQUEST_DAYS[r["type"]]
            events.record(self.db, "info", "agent", f"Request #{r['id']} expired: no decision in {days} days")

    def _fetch_mail(self, cycle_id: int) -> None:
        """New mail before the plan (errors are recorded and shown, and never stop the cycle). 0.15.0: not while a
        failing mailbox's wait (mailstore.due) runs."""
        if self.mailbox is None or self.stop.is_set():
            return
        if not mailstore.due(self.db, self.clock, self.scope.mode, agenda.MAIL_MINUTES):
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
            stats_history=self.settings.etsy_stats_history,
        )
        if self.settings.etsy_market_probe:  # 0.12.0: Etsy's numbers for a demand note's keywords
            ctx.market = _market_fn(shop)
        self.etsy_on = True

    def _sync_pinterest(self, cycle_id: int, ctx: tools.ToolContext) -> None:
        """0.13.0 (Phase E2): the pins' numbers before the plan, and what the tools know of the owner's account (errors
        are recorded and shown, and never stop the cycle). A pin links to a listing: nothing without the shop."""
        self.pinterest_on = False
        if self.pinterest is None or self.pins is None or not self.etsy_on:
            return
        account = self.pinterest.account()
        if account is None:
            return
        if not self.stop.is_set():
            self._progress(cycle_id, current_action="Checking the pins on Pinterest")
            try:
                # The fake account of a dry run needs no network; the owner's is reached by Ember's code only.
                with netguard.sealed() if account.simulated else contextlib.nullcontext():
                    self.pins.sync()
            except Exception:  # noqa: BLE001 - Pinterest must never end a cycle
                log.exception("Checking the pins on Pinterest failed")
        ctx.pinterest = tools.PinterestAccess(
            self.pinterest.username() or "your owner's account", self.settings.pinterest_pins_per_day
        )
        self.pinterest_on = True

    def _sync_bluesky(self, cycle_id: int, ctx: tools.ToolContext) -> None:
        """0.19.0: the account's followers and the posts' numbers before the plan, and what the tools know of the
        account (errors are recorded and shown, and never stop the cycle). A post needs no shop: it may link the owner's
        website, or nothing."""
        self.bluesky_on = False
        if self.bluesky is None or self.bluesky_posts is None:
            return
        account = self.bluesky.account()
        if account is None:
            return
        if not self.stop.is_set():
            self._progress(cycle_id, current_action="Checking the posts on Bluesky")
            try:
                # The fake account of a dry run needs no network; the owner's is reached by Ember's code only.
                with netguard.sealed() if account.simulated else contextlib.nullcontext():
                    self.bluesky_posts.sync()
            except Exception:  # noqa: BLE001 - Bluesky must never end a cycle
                log.exception("Checking the posts on Bluesky failed")
        ctx.bluesky = tools.BlueskyAccess(
            self.bluesky.handle() or "Ember's account",
            self.settings.bluesky_posts_per_day,
            website.address(self.settings),  # the owner's website, which a post may link
        )
        self.bluesky_on = True

    def _blog_access(self) -> tools.BlogAccess:
        """0.14.0: what the blog's tools know: the site's data, and what keeps the blog from being published."""
        problems = site_publisher.problems(self.settings, self.scope.mode)
        return tools.BlogAccess(site_publisher.owner_of(self.settings), tuple(problems))

    def _sync_printify(self, cycle_id: int, ctx: tools.ToolContext) -> None:
        """0.13.0 (Phase E4): the products' state and orders before the plan, and what the tools know of the owner's
        Printify account (errors are recorded and shown, and never stop the cycle). Its products become listings in
        the shop: nothing without it, nor while it can't be told which Printify shop sells there."""
        self.printify_on, self.printify_waits = False, ""
        if self.printify is None or self.pod is None or not self.etsy_on:
            return
        account = self.printify.account()
        if account is None:
            return
        # The fake account of a dry run needs no network; the owner's is reached by Ember's code only.
        sealed = netguard.sealed if account.simulated else contextlib.nullcontext
        try:
            with sealed():
                shop = self.printify.shop()
        except PrintifyError as exc:
            self.printify_waits = f"Printify's shop isn't known: {exc}"
            events.record(self.db, "warning", "printify", self.printify_waits[:300])
            return
        except Exception:  # noqa: BLE001 - Printify must never end a cycle
            log.exception("Finding the Printify shop failed")
            return
        if shop is None:
            self.printify_waits = "Printify's shop isn't known"
            return
        if not self.stop.is_set():
            self._progress(cycle_id, current_action="Checking the products at Printify")
            try:
                with sealed():
                    self.pod.sync()
            except Exception:  # noqa: BLE001 - Printify must never end a cycle
                log.exception("Checking the products at Printify failed")
        currency = self.settings.printify_currency
        ctx.printify = tools.PrintifyAccess(
            shop.title,
            currency,
            self.settings.printify_products_per_day,
            self.settings.printify_buyer_pays_shipping,
            self.settings.printify_bill_vat,
        )
        # 0.15.0: with the shop, for a cost probe (an unpublished product that tells what making costs)
        catalog = printify_publisher.Catalog(
            self.db, self.clock, self.scope.mode, lambda: account, lambda: shop.shop_id
        )
        rate = self.settings.etsy_usd_per_eur

        def look(search: str | None, blueprint_id: int | None, provider_id: int | None) -> str:
            with sealed():
                return catalog.answer(
                    search,
                    blueprint_id,
                    provider_id,
                    currency,
                    rate,
                    self.settings.printify_buyer_pays_shipping,
                    self.settings.printify_bill_vat,
                )

        ctx.catalog = look
        self.printify_on = True

    def _progress(self, cycle_id: int, **columns: Any) -> None:
        with self.db.transaction() as conn:
            store.update_cycle(conn, cycle_id, **columns)

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Stopping

    def _snapshot(self, venture: bool = False, cycle_id: int | None = None, keep: bool = True) -> context.Snapshot:
        """What the plan, the brief and the will see; ``cycle_id``: the cycle's (none for the diagnostics' preview).
        First (``keep``) Ember's code keeps its rules: the money goal, the stages, the grading, the listing tests, the
        predictions and the obligations."""
        status = self.economy.life.evaluate()
        scope = self.economy.life.scope()
        self.net_runway_days = status.runway.net_days  # 0.13.0
        mode = burn.peek(self.db, status)
        room, why = self.meter.cycle_room(cycle_id, mode)  # 0.15.0: the cap in force, not the options'
        if keep:
            self._keep_money_goal(scope, status.runway.net_days)  # 0.12.0: its decision points on the net runway
            self._keep_stages(cycle_id)
            metrics.grade_all(self.db, self.scope, scope, self.clock, self.settings.etsy_stats_history)  # 0.12.0
            self._keep_gates()  # 0.13.0: after the grading, so a bar missed now is owed at once
            self._keep_bets()  # 0.18.0: after the grading, from the same Etsy numbers
            predictions.settle_all(self.db, self.scope, scope, self.clock)  # 0.13.0: after the milestones are graded
            self._keep_obligations()  # 0.12.0: after the grading, so a miss it closed is owed a decision now
        today = self.economy.books.cap_spend_on(scope, self.clock.today())
        local = self.clock.now().astimezone(self.clock.tz).strftime("%A %Y-%m-%d %H:%M %Z")
        with self.db.connection() as conn:
            fresh = news.collect(conn, self.db, self.scope, app_version())
            # 0.13.0: the decision desk: a venture plan takes one of READY's items, or says why none
            self.ready_items = (
                desk.ready(
                    conn,
                    self.scope,
                    mode=mode.mode,
                    today=self.clock.today(),
                    cash_eur=self.settings.venture_cash_eur,
                    net_days=status.runway.net_days,
                )
                if venture
                else []
            )
            # 0.18.0: an ordinary plan's READY: useful work while projects wait (slack.py)
            self.slack_items = (
                slack.items(conn, self.scope, self.clock.today()) if not venture and not self.reactive else []
            )
            shop = ""
            if self.etsy_on and self.etsy is not None:
                name = self.etsy.shop_name() or "your shop"
                # 0.12.0: Ember's code records the orders' revenue when the owner turned that on (and can convert it)
                auto = self.settings.etsy_auto_record_revenue and (
                    self.settings.etsy_usd_per_eur > 0 or self.etsy.currency() == "USD"
                )
                shop = etsy_publisher.shop_text(
                    conn, self.scope, self.clock, name, self.settings.etsy_listings_per_day, auto_revenue=auto
                )
            pins = ""
            if self.pinterest_on and self.pinterest is not None:  # 0.13.0 (Phase E2)
                account = self.pinterest.username() or "your owner's account"
                pins = (
                    f"Your owner's account: {account} (at most {self.settings.pinterest_pins_per_day} pins a day).\n"
                    + pinterest_publisher.text(conn, self.scope)
                )
            elif self.pinterest is not None:  # 0.15.0: switched on, but not set up
                pins = _waiting("Pinterest", self.pinterest.status(), self._etsy_state())
            posted = ""
            if self.bluesky_on and self.bluesky is not None:  # 0.19.0
                posted = _bluesky_head(self.bluesky, self.settings.bluesky_posts_per_day) + bluesky_publisher.text(
                    conn, self.scope
                )
            elif self.bluesky is not None:  # switched on, but not set up (or Bluesky refused the login)
                posted = _waiting("Bluesky", self.bluesky.status(), "ok", venture=False)
            pod = ""
            if self.printify_on and self.printify is not None:  # 0.13.0 (Phase E4)
                known = self.printify.describe().get("shop") or {}
                pod = (
                    f"Printify shop: {known.get('title') or 'yours'} (prices in {self.settings.printify_currency};"
                    f" at most {self.settings.printify_products_per_day} products a day).\n"
                    + printify_publisher.text(conn, self.scope)
                )
            elif self.printify is not None:  # 0.15.0: switched on, but not set up
                pod = _waiting("Printify", self.printify.status(), self._etsy_state(), self.printify_waits)
            site_text = website.planner_text(conn, self.scope, website.owner(self.settings)) if self.site_on else ""
            blog_text = site_publisher.text(conn, self.db, self.scope, self.settings) if self.blog_on else ""
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
                cycle_cap=micros_to_usd(room),
                cap_note=_cap_note(why),
                news=fresh,
                mail_address=self.mailbox.address if self.mailbox else None,
                today=self.clock.today(),
                etsy=shop,
                pinterest=pins,
                bluesky=posted,
                printify=pod,
                website=site_text,
                blog=blog_text,
                venture=venture,
                venture_share=self.settings.venture_share,
                shelf=library.shelf(conn, self.scope),
                decision_wakes=self.settings.wake_on_decision,
                burn=_burn_line(mode, self.clock),
                brainstorm=mode.brainstorms,
                ready=(
                    desk.text(self.ready_items, predictions.calibration(conn, self.scope) if venture else "")
                    if venture
                    else slack.text(self.slack_items)
                ),
                agenda=agenda.unseen(conn, self.scope),  # 0.13.0: what happened between cycles
                reactive=self.reactive,
            )

    def _call(self, cycle_id: int, purpose: str, request: dict[str, Any], venture_id: int | None = None) -> CallResult:
        """One metered call (``venture_id``: the venture it serves, if not the cycle's: 0.12.0); a failure that cost
        nothing (no connection, overloaded) is retried once, but never a request the API rejected as it is (0.10.1: a
        search limited to a blocked site was sent twice)."""
        self._check_stop()
        try:
            return self.meter.call(cycle_id, purpose, request, venture_id)
        except CallFailed as exc:
            if exc.result.status != "failed" or exc.result.cost_micros or _rejected(exc.result.error):
                raise
            log.info("Call #%d failed without cost (%s); retrying once", exc.result.call_id, exc.result.error)
            if self.stop.wait(RETRY_DELAY_SECONDS):
                raise Stopping from exc
            return self.meter.call(cycle_id, purpose, request, venture_id)

    # --- plan, act, reflect ---

    def _plan_act_reflect(self, cycle_id: int, ctx: tools.ToolContext) -> CycleEnd:
        self._guarded(self._retire_lessons, cycle_id, "the lessons' check after an upgrade")  # 0.19.2
        with self.db.connection() as conn:
            review_due = review.due(conn, self.scope, self.clock) and not self.reactive
        if review_due:
            self._review(cycle_id, ctx)
        if not self.reactive:
            with self.db.connection() as conn:
                weekly_due = weekly.due(conn, self.scope, self.clock.today())
            if weekly_due:
                self._guarded(self._weekly, cycle_id, "the weekly look")  # 0.18.0: after the review's cases
        if self.library_on and not self.reactive:
            self._study(cycle_id)
        if not self.reactive:
            self._critique(cycle_id)  # 0.13.0
            self._guarded(self._quality, cycle_id, "the quality check")  # 0.18.0
        snap = self._snapshot(ctx.venture, cycle_id)
        ctx.net_runway_days = self.net_runway_days  # 0.13.0: the knock-outs' slow rule
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
        shown = [int(r["id"]) for r in snap.agenda if agenda.line(r) in planner]  # 0.13.0: the events it listed
        if shown:
            with self.db.transaction() as conn:
                agenda.mark_seen(conn, cycle_id, shown)
        if snap.library is not None and snap.library.new:
            shown = [i for item in snap.library.new if library.studied_line(item) in planner for i in item.ids]
            with self.db.transaction() as conn:
                library.mark_seen(conn, cycle_id, shown)  # the documents this plan listed as newly studied
        if planned.changelog:
            news.mark_changelog_seen(self.db, self.scope, snap.news)
        taken = self._take_ready(cycle_id, plan) if ctx.venture else None  # 0.13.0
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
                elif ctx.venture and venture["stage"] not in ventures.EXPLORING:
                    plan.focus_venture_id = None  # 0.19.3: a backed or live venture is its project's work
                    venture_focus = (
                        f"Venture #{venture['id']} is {venture['stage']}: your owner backed it, so its work is its "
                        "project's, in ordinary cycles. This venture cycle finds and decides new ventures (READY)."
                    )
                else:
                    venture_focus = self._venture_focus(conn, venture, cycle_id)
                    if taken is not None and taken.venture_id == plan.focus_venture_id:
                        venture_focus = f"Decision desk: you took {taken.key}: {taken.text}\n{venture_focus}"
            if plan.focus_milestone_id is not None:
                milestone = roadmap.get(conn, self.scope, plan.focus_milestone_id)
                if milestone is None or milestone["status"] != "open":
                    plan.focus_milestone_id = None
                else:
                    parent = roadmap.get(conn, self.scope, milestone["parent_id"]) if milestone["parent_id"] else None
                    spent = {mid: cost for mid, (_, cost) in roadmap.effort(conn, self.scope).items()}
                    replaced = (
                        roadmap.get(conn, self.scope, milestone["replaces_id"]) if milestone["replaces_id"] else None
                    )
                    last = digest.newest_for(conn, self.scope, "milestone_id", milestone["id"], cycle_id)
                    # 0.16.3 (analysis bug 5): what stands unlocked for it, from the grants
                    unlocked = policy.unlocked_text(policy.standing(conn, self.scope).get(int(milestone["id"]), []))
                    milestone_focus = roadmap.focus_text(
                        milestone, self.clock.today(), parent, spent, replaced, last=last, unlocked=unlocked
                    )
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
            self.max_steps,
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
        # 0.15.0: every cycle that worked reflects (the journal is the reflection's: one written during the work
        # skipped it).
        reflected = self._reflect(cycle_id, ctx, brief, act)
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

    def _take_ready(self, cycle_id: int, plan: Plan) -> desk.Item | None:
        """0.13.0: the READY item the venture plan took (its venture becomes the cycle's focus), kept with the list it
        came from, or why it took none."""
        if not self.ready_items:
            return None
        taken, why = desk.choose(self.ready_items, plan.ready)
        if taken is not None and taken.venture_id is not None:
            plan.focus_venture_id = taken.venture_id
        with self.db.transaction() as conn:
            desk.record(conn, cycle_id, self.ready_items, taken, why, to_iso(self.clock.now()))
        return taken

    def _keep_money_goal(self, books_scope: Any, runway_days: float | None) -> None:
        """0.12.0: the roadmap is never empty: Ember's code settles its money goal from the books and sets the next
        one (roadmap.keep_money_goal) before every plan."""
        now = self.clock.now()
        start = now - timedelta(days=roadmap.MONEY_WINDOW_DAYS)
        earned = self.economy.books.net_revenue_between(books_scope, start, now)
        spent = self.economy.books.api_spend_between(books_scope, start, now)
        with self.db.transaction() as conn:
            happened = roadmap.keep_money_goal(
                conn, self.scope, self.clock.today(), to_iso(now), earned, spent, runway_days
            )
        for line in happened:
            events.record(self.db, "info", "agent", line[:300])

    def _keep_obligations(self) -> None:
        """0.12.0: the decisions and misses the agent owes a reaction to, from the day the ledger began (not the history
        before), kept by Ember's code before every plan (obligations.keep)."""
        now = to_iso(self.clock.now())
        key = obligations.SINCE_KEY.format(mode=self.scope.mode)
        since = self.db.get_meta(key)
        if not since:
            since = now
            self.db.set_meta(key, since)
        with self.db.transaction() as conn:
            happened = obligations.keep(conn, self.scope, now, since)
        for line in happened:
            events.record(self.db, "info", "agent", line[:300])

    def _keep_gates(self) -> None:
        """0.13.0: a product line's listing test (gates.keep): its bars as milestones once its first listing is live,
        and what a miss or a first order asks of the agent, kept by Ember's code before every plan."""
        with self.db.transaction() as conn:
            happened = gates.keep(conn, self.scope, self.clock.today(), to_iso(self.clock.now()))
        for line in happened:
            events.record(self.db, "info", "agent", line[:300])

    def _keep_bets(self) -> None:
        """0.18.0: the agent's bets that are won or due, settled by Ember's code before every plan (bets.settle)."""
        with self.db.transaction() as conn:
            happened = bets.settle(conn, self.scope, self.clock.today(), to_iso(self.clock.now()))
            happened += learning.fade(conn, self.scope, to_iso(self.clock.now()))  # the playbook's old guesses
        for line in happened:
            events.record(self.db, "info", "agent", line[:300])

    def _keep_stages(self, cycle_id: int | None = None) -> None:
        """0.12.0: the rules of the ventures' stages (a first test for each backed venture, research without a business
        case and a missed first test parked), kept by Ember's code before every plan (stages.keep); 0.19.3: in a cycle
        (``cycle_id``), a backed venture's project too."""
        # 0.15.0: a channel's venture gets its first test once the channel is set up (its tools are on), and one set
        # while the owner hadn't set it up yet (or switched it off since) starts again then
        ready = [name for name, on in (("pinterest", self.pinterest_on), ("printify", self.printify_on)) if on]
        unset = [
            name
            for name, channel in (("pinterest", self.pinterest), ("printify", self.printify))
            if channel is not None
            and (channel.status()[0] == "disabled" or _unset(channel.status(), self._etsy_state()))
        ]
        with self.db.transaction() as conn:
            happened = stages.keep(
                conn, self.scope, self.clock.today(), to_iso(self.clock.now()), ready, unset, cycle_id=cycle_id
            )
            # 0.16.3 (analysis bug 1): the owner hears once, a week before a first test's date, what is at stake
            warned = stages.warn(conn, self.scope, self.clock.today(), to_iso(self.clock.now()))
        for line in happened:
            events.record(self.db, "info", "agent", line[:300])
        for line in warned:
            events.record(self.db, "warning", "agent", line[:600])

    def _etsy_state(self) -> str:
        """0.15.0: the Etsy shop's state (ok, disabled, not_configured or not_connected): a channel's pins and
        products need it."""
        if self.etsy is None:
            return "disabled"
        return self.etsy.status()[0]

    def _venture_focus(self, conn: Any, row: Any, cycle_id: int) -> str:
        """The brief's FOCUS for the plan's venture: its record, money, projects and knowledge file, and (0.12.0) the
        digest of the last cycle aimed at it."""
        paid = ventures.money(conn, self.scope).get(row["id"], ventures.Money())
        parts = ventures.knowledge_parts(self.workspace, row["id"], row["title"])
        try:
            size = self.workspace.size_of(parts[-1], "text") if parts else None
        except SandboxError:
            size = None
        last = digest.newest_for(conn, self.scope, "venture_id", row["id"], cycle_id)
        found = evidence.focus_line(conn, row["id"])  # 0.12.0: its claims, by their sources' grade
        case_row = ventures.latest_case(conn, row["id"])
        numbers = ventures.numbers_text(case_row)  # 0.13.0
        judged = critic.text(critic.latest(conn, row["id"]), case_row)  # 0.13.0: the critic's review of it
        knocked = (  # 0.13.0: while it isn't backed
            knockouts.text(
                knockouts.check(conn, row, cash_eur=self.settings.venture_cash_eur, net_days=self.net_runway_days)
            )
            if row["stage"] in ventures.EXPLORING
            else ""
        )
        return ventures.focus_text(
            row,
            paid,
            size,
            ventures.projects_of(conn, row["id"]),
            parts,
            last=last,
            evidence=found,
            numbers=numbers,
            knocked=knocked,
            critic=judged,
        )

    def _review(self, cycle_id: int, ctx: tools.ToolContext) -> None:
        """The daily review, before the first plan of the day. It never ends the cycle: a review the budget can't
        cover now is tried at the next cycle, and a failed one is recorded (at most review.MAX_ATTEMPTS a day). Its
        verdicts on milestones are applied first (0.12.0), and it is kept with what came of them."""
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
        # 0.15.0: it leaves what the cycle needs to work after it, as the study does (in maintenance the cycle's cap
        # bounds the review too, and a review that took most of it left no plan)
        working = working_cycle_cost(self.settings, self.db, self.economy.life.mode) or 0
        if quote > self.meter.headroom(cycle_id, REVIEW, keep=working):
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
        subjects = {item.subject for item in card.settled}
        parsed = review.parse(text, card.project_ids, subjects) if stop == "end_turn" else None
        note = None
        if parsed is None:
            note = "the review wasn't valid JSON" if stop == "end_turn" else f"the review was cut off ({stop})"
        for verdict in parsed.milestones if parsed else []:
            outcome = tools.update_milestone(ctx, review.update_args(verdict, self.clock.today()))
            verdict.applied, verdict.outcome = outcome.ok, outcome.text.removeprefix("Error: ")
            if outcome.ok:
                events.record(self.db, "info", "agent", f"The daily review: {outcome.text}"[:300])
        review_id = self._save_review(cycle_id, card, parsed, note)
        if parsed is not None and parsed.retros:  # 0.18.0: its retrospectives become cases
            with self.db.transaction() as conn:
                made = learning.save_cases(
                    conn, self.scope, review_id, parsed.retros, card.settled, to_iso(self.clock.now())
                )
            events.record(self.db, "info", "agent", f"The daily review kept {len(made)} case(s) of what settled")
        if parsed is not None:
            self._keep_lesson(cycle_id, parsed.lesson)
            self._consolidate(cycle_id)

    def _keep_lesson(self, cycle_id: int, lesson: str) -> None:
        """0.18.0: the daily review's lesson goes into the lessons file (live, no plan copied it: the review's
        conclusions were lost), on one line, as the agent's own append would; one already noted is skipped."""
        line = " ".join(lesson.split())
        if not line:
            return
        try:
            with self.db.transaction() as conn:
                said = self.memory.update(
                    conn, "lessons", "append", line, cycle_id, to_iso(self.clock.now()), tools.SPECS
                )
        except memory_files.MemoryError_ as exc:
            log.warning("The daily review's lesson wasn't kept: %s", exc)
            return
        if not said.startswith("already noted"):
            events.record(self.db, "info", "agent", f"The daily review's lesson was kept: {line}"[:300])

    def _retire_lessons(self, cycle_id: int) -> None:
        """0.19.2: once a version, before its first plan: the lessons that name a tool its release notes name are
        marked to re-check (memory_files.recheck_for_release; 0.21.0: they were deleted), before the plan and the work
        steps read them. Live, an hour after 0.19.1 lifted project_create's limit, a lesson that it refuses a ninth
        open project made the work step skip its plan's project_create, and it told its owner the limit still held.
        Free: no call."""
        running = app_version()
        key = f"agent.{self.scope.mode}.lessons_version"
        checked = self.db.get_meta(key)
        if checked == running:
            return
        since = checked or self.db.get_meta(news.changelog_key(self.scope.mode))
        notes = news.changelog_news(paths.CHANGELOG_PATH, since, running) if since else ""
        text = self.memory.read("lessons")
        done = None
        if notes:
            with self.db.connection() as conn:
                pinned = {memory_files.lesson_key(p["text"]) for p in memory_files.pins(conn, self.scope)}
            done = memory_files.recheck_for_release(text, notes, tools.SPECS, pinned, running)
        if done is not None:
            with self.db.transaction() as conn:
                if self.memory.read("lessons") == text:
                    self.memory.rewrite(conn, "lessons", done[0], "consolidation", to_iso(self.clock.now()))
        self.db.set_meta(key, running)
        if done is not None:
            quoted = "; ".join(json.dumps(line[:60], ensure_ascii=False) for line in done[1][:3])
            more = f" and {len(done[1]) - 3} more" if len(done[1]) > 3 else ""
            message = (
                f"Ember's code marked {len(done[1])} lesson(s) to re-check: tools {running} changed: {quoted}{more}"
            )
            events.record(self.db, "info", "agent", message[:300])

    def _guarded(self, step: Callable[[int], None], cycle_id: int, name: str) -> None:
        """0.18.0: a learning step before the plan: a bug in it is logged and the cycle goes on (Stopping and EndCycle
        still end it)."""
        try:
            step(cycle_id)
        except (Stopping, EndCycle):
            raise
        except Exception as exc:  # noqa: BLE001 - a learning step never ends the cycle
            log.exception("%s failed in cycle #%d", name, cycle_id)
            events.record(self.db, "warning", "agent", f"{name.capitalize()} failed: {type(exc).__name__}"[:300])

    def _weekly(self, cycle_id: int) -> None:
        """0.18.0: the weekly look at the whole business (weekly.py). It never ends the cycle: one the money can't
        cover now waits for the next cycle, and a failed one is kept and tried again the next day."""
        now = self.clock.now()
        books, ledger_scope = self.economy.books, self.economy.life.scope()
        status = self.economy.life.evaluate()
        net = status.runway.net_days
        runway = f"{net:.1f} days" if net is not None else "no end (it earns what it spends)"
        lines = [f"Balance {_usd(status.balance)}; net runway: {runway}."]
        for days in (7, 30):
            start = now - timedelta(days=days)
            earned = books.net_revenue_between(ledger_scope, start, now)
            spent = books.api_spend_between(ledger_scope, start, now)
            lines.append(f"Last {days} days: revenue less expenses {_usd(earned)}, API spending {_usd(spent)}.")
        with self.db.connection() as conn:
            standing = store.standing_instructions(conn, self.scope)
            text = weekly.view(
                conn,
                self.scope,
                now,
                "MONEY\n" + "\n".join(lines),
                self.memory.read("strategy"),
                standing["text"] if standing else "",
            )
        request = prompts.weekly_request(self.settings, text)
        if not context.fits(request, WEEKLY_INPUT_TOKENS):
            log.warning("The weekly look doesn't fit its budget; skipped")
            return
        try:
            quote = self.meter.quote(request, REVIEW)
        except Unpriceable as exc:
            log.warning("The weekly look can't be priced (%s); skipped", exc)
            return
        working = working_cycle_cost(self.settings, self.db, self.economy.life.mode) or 0
        if quote > self.meter.headroom(cycle_id, REVIEW, keep=working):
            log.info("The weekly look can't be afforded now; it is tried at the next cycle")
            return
        self._progress(cycle_id, phase="review", current_action="Looking at the whole business (weekly)")
        answer, note = None, None
        try:
            result = self._call(cycle_id, REVIEW, request)
        except CallRefused:
            return
        except CallFailed as exc:
            note = f"the call failed: {exc.result.error or exc.result.status}"
        else:
            response = result.response or {}
            reply = _text_of(response)
            self._save_text(result.call_id, reply, response)
            stop = response.get("stop_reason")
            answer = weekly.parse(reply) if stop == "end_turn" else None
            if answer is None:
                note = "its answer wasn't usable" if stop == "end_turn" else f"it was cut off ({stop})"
        stamp = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            happened = weekly.apply(conn, self.scope, self.memory, answer, stamp) if answer is not None else []
            weekly.save(conn, self.scope, cycle_id, stamp, self.clock.today(), text, answer, happened, note)
        if answer is None:
            events.record(self.db, "warning", "agent", f"The weekly look failed: {note}"[:300])
        else:
            events.record(self.db, "info", "agent", ("The weekly look: " + "; ".join(happened or ["no changes"]))[:300])

    def _consolidate(self, cycle_id: int) -> None:
        """0.12.0: the lessons' daily consolidation, after the daily review: a call of its own on the planner's model
        merges the lessons that say the same and retires those newer ones contradict, once the file holds
        memory_files.CONSOLIDATE_FROM lessons. Ember's code checks its answer (memory_files.consolidate). It counts
        toward the daily cap only, leaves what the cycle needs to work, and never ends the cycle."""
        text = self.memory.read("lessons")
        if len(memory_files.lesson_lines(text)[1]) < memory_files.CONSOLIDATE_FROM:
            return
        with self.db.connection() as conn:
            pinned = {memory_files.lesson_key(p["text"]) for p in memory_files.pins(conn, self.scope)}
        named = frozenset(tools.SPECS)  # 0.18.0: a lesson naming a tool is marked, and its limit may go
        request = prompts.consolidate_request(self.settings, memory_files.consolidation_input(text, pinned, named))
        try:
            quote = self.meter.quote(request, CONSOLIDATE)
        except Unpriceable as exc:
            log.warning("The lessons' consolidation can't be priced (%s); skipped", exc)
            return
        working = working_cycle_cost(self.settings, self.db, self.economy.life.mode) or 0
        if quote > self.meter.headroom(cycle_id, CONSOLIDATE, keep=working):
            log.info("The lessons' consolidation can't be afforded now; it waits for the next daily review")
            return
        self._progress(cycle_id, current_action="Consolidating the lessons")
        try:
            result = self._call(cycle_id, CONSOLIDATE, request)
        except (CallRefused, CallFailed):
            return  # a refusal meets the plan too; a failure waits for the next daily review
        response = result.response or {}
        reply = _text_of(response)
        self._save_text(result.call_id, reply, response)
        try:
            answer = json.loads(reply) if response.get("stop_reason") == "end_turn" else None
        except ValueError:
            answer = None
        with self.db.transaction() as conn:
            done = memory_files.consolidate(text, answer, pinned, memory_files.CAPS["lessons"], named)
            if done is not None and self.memory.read("lessons") == text:
                self.memory.rewrite(conn, "lessons", done[0], "consolidation", to_iso(self.clock.now()))
        message = (
            f"Ember's code consolidated the lessons: {done[1]}"
            if done
            else "The lessons' consolidation changed nothing"
        )
        events.record(self.db, "info", "agent", message[:300])

    def _critique(self, cycle_id: int) -> None:
        """0.13.0: the independent critic, before the plan. A call of its own on the strategy model reviews the newest
        business case of a proposed venture (the oldest proposal first, one a cycle) with its evidence. Ember's code
        checks its answer, works out the economics of its numbers like the agent's and keeps it, for the owner and the
        agent. Only while venture cycles run (the burn mode). It counts toward the daily cap only, leaves what the
        cycle needs to work, and never ends the cycle: a critique the money can't cover now waits, and a failed one is
        kept and tried again at the next cycle (critic.MAX_ATTEMPTS times a case)."""
        if not burn.peek(self.db, self.economy.life.evaluate()).venture_cycles:
            return
        with self.db.connection() as conn:
            venture = critic.due(conn, self.scope)
            case_row = ventures.latest_case(conn, int(venture["id"])) if venture is not None else None
            if venture is None or case_row is None:
                return
            case_text = critic.case_text(conn, venture, case_row, predictions.calibration(conn, self.scope))
        vid, case_id = int(venture["id"]), int(case_row["id"])
        request = prompts.critic_request(self.settings, case_text)
        try:
            quote = self.meter.quote(request, CRITIC)
        except Unpriceable as exc:
            log.warning("The critic can't be priced (%s); skipped", exc)
            return
        working = working_cycle_cost(self.settings, self.db, self.economy.life.mode) or 0
        if quote > self.meter.headroom(cycle_id, CRITIC, keep=working):
            log.info("The critic can't be afforded now; venture #%d's case waits for the next cycle", vid)
            return
        self._progress(cycle_id, current_action=f"The critic reviews venture #{vid}")
        call_id = None
        try:
            result = self._call(cycle_id, CRITIC, request)
        except CallRefused:
            return  # the money: it waits
        except CallFailed as exc:
            call_id, note, parsed = (
                exc.result.call_id,
                f"the call failed ({exc.result.error or exc.result.status})",
                None,
            )
        else:
            response = result.response or {}
            reply = _text_of(response)
            self._save_text(result.call_id, reply, response)
            stop = response.get("stop_reason")
            try:
                answer = json.loads(reply) if stop == "end_turn" else None
            except ValueError:
                answer = None
            call_id = result.call_id
            parsed = critic.parse(answer, ventures.case_of(case_row)[0])
            note = "its answer wasn't usable" if stop == "end_turn" else f"it was cut off ({stop})"
        now = to_iso(self.clock.now())
        if parsed is None:
            with self.db.transaction() as conn:
                critic.add_failed(conn, vid, case_id, call_id, note, now)
            message = f"The critic's review of venture #{vid} (case #{case_id}) failed: {note}"
            events.record(self.db, "warning", "agent", message[:300])
            return
        texts, theirs = parsed
        economics = econ.compute(theirs, self.settings.etsy_usd_per_eur)
        with self.db.transaction() as conn:
            critic.add(conn, vid, case_id, call_id, texts, theirs, economics, now)
        message = (
            f"The critic on venture #{vid} (case #{case_id}): {texts['verdict']}; expected EUR "
            f"{economics.ev_eur:.0f} a month by its numbers. Fatal flaw: {texts['fatal_flaw']}"
        )
        events.record(self.db, "info", "agent", message[:300])

    def _quality(self, cycle_id: int) -> None:
        """0.18.0: the quality critic, before the plan: one product line's live listing a cycle (quality.py). It
        counts toward the daily cap only, leaves what the cycle needs to work, and never ends the cycle."""
        with self.db.connection() as conn:
            project_id = quality.due(conn, self.scope, self.clock.today())
            if project_id is None:
                return
            text, picture = quality.case(conn, self.scope, self.workspace, project_id)
        request = prompts.quality_request(self.settings, text, picture)
        try:
            quote = self.meter.quote(request, CRITIC)
        except Unpriceable as exc:
            log.warning("The quality check can't be priced (%s); skipped", exc)
            return
        working = working_cycle_cost(self.settings, self.db, self.economy.life.mode) or 0
        if quote > self.meter.headroom(cycle_id, CRITIC, keep=working):
            log.info("The quality check can't be afforded now; project #%d waits for the next cycle", project_id)
            return
        self._progress(cycle_id, current_action=f"The quality critic looks at project #{project_id}")
        answer, note, call_id = None, None, None
        try:
            result = self._call(cycle_id, CRITIC, request)
        except CallRefused:
            return
        except CallFailed as exc:
            call_id, note = exc.result.call_id, f"the call failed ({exc.result.error or exc.result.status})"
        else:
            response = result.response or {}
            reply = _text_of(response)
            self._save_text(result.call_id, reply, response)
            call_id = result.call_id
            stop = response.get("stop_reason")
            answer = quality.parse(reply) if stop == "end_turn" else None
            if answer is None:
                note = "its answer wasn't usable" if stop == "end_turn" else f"it was cut off ({stop})"
        with self.db.transaction() as conn:
            quality.save(conn, self.scope, project_id, call_id, answer, note, to_iso(self.clock.now()))
        if answer is None:
            events.record(self.db, "warning", "agent", f"The quality check of project #{project_id} failed: {note}")
        else:
            said = f"{answer['score']}/10, {answer['verdict']}" + (f": {answer['fixes']}" if answer["fixes"] else "")
            events.record(self.db, "info", "agent", f"The quality critic on project #{project_id}: {said}"[:300])

    def _study(self, cycle_id: int) -> None:
        """0.12.0: study the owner's library before the plan: the next parts of the documents waiting, a few calls a
        cycle, within the owner's daily study budget (counted toward the daily cap, not the cycle cap). What is
        learned is kept, so a text is read once. It never ends the cycle: what the money can't cover now waits."""
        budget = usd_cap_to_micros(self.settings.library_study_usd_per_day)
        for _ in range(library.STUDY_CALLS if budget > 0 else 0):
            self._check_stop()
            with self.db.transaction() as conn:
                document = library.next_to_study(conn, self.scope)
                if document is None:
                    return
                if library.end_full(conn, document, to_iso(self.clock.now())):  # 0.15.0: nothing more to keep
                    continue
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
        """0.12.0: the learnings from the owner's library that match the plan, picked by Ember's code, for the brief;
        0.18.0: after the agent's own principles and cases that match it (learning.relevant)."""
        query = " ".join([plan.goal, plan.money_path, *plan.steps])
        with self.db.connection() as conn:
            own = learning.relevant(conn, self.scope, query)
            rows = (
                library.relevant(conn, self.scope, query, plan.focus_venture_id, plan.focus_project_id)
                if self.library_on
                else []
            )
        return "\n".join([*own, *(library.learning_line(r) for r in rows)])

    def _save_review(
        self, cycle_id: int, card: review.Scorecard, parsed: review.Review | None, note: str | None
    ) -> int:
        with self.db.transaction() as conn:
            made = review.save(
                conn, self.scope, cycle_id, to_iso(self.clock.now()), self.clock.today(), card, parsed, note
            )
        if parsed is None:
            events.record(self.db, "warning", "agent", f"The daily review failed: {note}")
        return made

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
        steps = (
            [_step(s) for s in steps if isinstance(s, str) and s.strip()][: prompts.PLAN_STEPS]
            if isinstance(steps, list)
            else []
        )
        focus = data.get("focus_project_id")
        venture = data.get("focus_venture_id")
        milestone = data.get("focus_milestone_id")
        sleep = data.get("sleep_minutes")
        ready = " ".join(str(data.get("ready") or "").split())[:300]  # 0.13.0: a venture plan's
        return Plan(
            assessment=str(data.get("assessment") or "")[: prompts.PLAN_CHARS["assessment"]],
            goal=str(data.get("goal") or "")[: prompts.PLAN_CHARS["goal"]],
            money_path=str(data.get("money_path") or "")[: prompts.PLAN_CHARS["money_path"]],
            focus_project_id=focus if isinstance(focus, int) and not isinstance(focus, bool) else None,
            focus_venture_id=venture if isinstance(venture, int) and not isinstance(venture, bool) else None,
            focus_milestone_id=milestone if isinstance(milestone, int) and not isinstance(milestone, bool) else None,
            steps=steps,
            sleep_minutes=self._clamp_sleep(sleep) if isinstance(sleep, int) and not isinstance(sleep, bool) else None,
            ready=ready,
        )

    def _clamp_sleep(self, minutes: int) -> int:
        return max(self.settings.min_sleep_minutes, min(self.settings.max_sleep_minutes, minutes))

    def _act(self, cycle_id: int, ctx: tools.ToolContext, brief: str, seen: frozenset[news.Item]) -> _Act:
        """The work steps; ``seen`` (the owner's items the plan listed and the brief showed in full) is marked once one
        is answered (0.15.0: the owner's messages; the rest once the cycle ends normally)."""
        act = _Act()
        max_steps = self.max_steps
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
                pinterest=self.pinterest_on,
                printify=self.printify_on,
                site=self.site_on,
                **_offered(ctx),
                blog=self.blog_on,
                bluesky=self.bluesky_on,
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
                # 0.15.0: the owner's messages now (an answer needs them seen), the rest of their news once the cycle
                # ends normally (_write_report): a stopped cycle took their comment on an approval with it.
                self.news_kept = frozenset(item for item in seen if item[0] != "message")
                with self.db.transaction() as conn:
                    news.mark_seen(conn, cycle_id, [item for item in seen if item[0] == "message"])
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
                if any(u.get("name") == "write_journal" for u in uses):
                    # 0.15.0: the agent's work is over (the journal is the last thing it does): its reflection
                    # writes it. The step after it only reported.
                    act.end_reason = "done"
                    break
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
        reflect = prompts.reflect_request(
            self.settings,
            brief,
            past,
            pending,
            mail=self.mail,
            etsy=self.etsy_on,
            ended=longest,
            venture=venture,
            library=self.library_on,
            pinterest=self.pinterest_on,
            printify=self.printify_on,
            site=self.site_on,
            **_offered(ctx),
            blog=self.blog_on,
            bluesky=self.bluesky_on,
        )
        try:
            step_worst = self.meter.quote(request, "work")
            step_expected = self.meter.expected(request, "work", cycle_id)
            reflect_worst = self.meter.quote(reflect, "reflect", extra_tokens=growth)
            # the reflection reads what this step caches: the step's whole prompt
            cached = self.meter.prompt_tokens(request)
            reflect_expected = self.meter.expected(reflect, "reflect", cycle_id, extra_tokens=growth, cached=cached)
        except Unpriceable:
            return False
        # 0.12.0: the cycle cap counts expected costs (the reflection's: at least 1.5 times the 95th percentile of the
        # recent ones), the daily cap and the balance worst cases
        ctx.state.reflect_reserve = self.meter.reflection_reserve(reflect_expected, str(reflect["model"]))
        ctx.state.reflect_money = reflect_worst
        cycle_room, money_room = self.meter.rooms(cycle_id)
        return step_expected + ctx.state.reflect_reserve <= cycle_room and step_worst + reflect_worst <= money_room

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
        with self.db.connection() as conn:
            undone = digest.undone(conn, cycle_id)  # 0.12.0: what its work didn't do, so it isn't reported as done
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
            undone=undone,
            pinterest=self.pinterest_on,
            printify=self.printify_on,
            site=self.site_on,
            **_offered(ctx),
            blog=self.blog_on,
            bluesky=self.bluesky_on,
        )
        # 0.12.0: why the work ended is kept first, also when the reflection can't be paid for.
        self._progress(cycle_id, act_end_reason=act.end_reason[:300] or None)
        try:
            if not self.meter.affordable(request, "reflect", cycle_id)[0]:
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
            model, checking = self._research_model()
            request = prompts.research_request(self.settings, question, url, site, model)
            try:
                fits, expected, _ = self.meter.affordable(
                    request, "research", cycle_id, ctx.state.reflect_reserve, ctx.state.reflect_money
                )
            except Unpriceable as exc:
                return tools.Outcome(False, f"Error: research can't be priced ({exc}).", "refused: unpriceable")
            if not fits:
                return tools.Outcome(
                    False,
                    f"Error: research could cost about ${micros_to_usd(expected):.3f}, more than this cycle has left"
                    f"{_kept(ctx)}.",
                    "refused: budget",
                )
            try:
                result = self._call(cycle_id, "research", request, venture_id)
            except CallRefused as exc:  # a state or system refusal ends the cycle at its next call
                return tools.Outcome(False, f"Error: research refused ({exc.reason}).", "refused")
            except CallFailed as exc:
                # 0.15.0: a failed call that was paid counts toward the venture's research budget too
                if venture_id is not None and exc.result.cost_micros > 0:
                    with self.db.transaction() as conn:
                        ventures.add_research(
                            conn,
                            venture_id,
                            cycle_id,
                            exc.result.call_id,
                            question,
                            url,
                            0,
                            exc.result.cost_micros,
                            to_iso(self.clock.now()),
                        )
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
            partial = ""
            if response.get("stop_reason") == "pause_turn":
                follow = dict(request)
                follow["messages"] = [
                    *request["messages"],
                    {"role": "assistant", "content": response.get("content") or []},
                ]
                try:
                    # 0.15.0: the continuation leaves the reflection's money too (it was sent unchecked)
                    if self.meter.affordable(
                        follow, "research", cycle_id, ctx.state.reflect_reserve, ctx.state.reflect_money
                    )[0]:
                        more = self._call(cycle_id, "research", follow, venture_id)
                        response = more.response or response
                        cost += more.cost_micros
                    else:
                        partial = (
                            "\n(The search paused and wasn't continued: that would use the money kept for your"
                            " reflection. This answer may be partial.)"
                        )
                except (Unpriceable, CallRefused):
                    pass
                except CallFailed as exc:  # 0.15.0: paid, so it counts toward the budget too
                    cost += exc.result.cost_micros
            answer = _text_of(response)
            digest = answer[:RESEARCH_DIGEST_CHARS] or "Nothing useful was found."
            sources = _sources(response)
            with self.db.transaction() as conn:  # 0.12.0: what evidence can be checked against
                evidence.record_sources(conn, self.scope, cycle_id, result.call_id, sources, to_iso(self.clock.now()))
            if checking is not None:  # 0.12.0: the research model's check, on the same question
                self._compare_research(ctx, checking, question, url, site, result.call_id, sources, answer)
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
                f"{body}{source_text}{counted}{partial}\n(cost ${micros_to_usd(cost):.4f})",
                f"research: {question[:80]}",
            )

        return research

    def _research_model(self) -> tuple[str, str | None]:
        """0.12.0: (the model research runs on, the research model being checked on the same questions, if any).
        The owner's research model takes over once its check passed (research_check)."""
        worker, candidate = self.settings.worker_model, self.settings.research_model
        if not candidate or candidate == worker:
            return worker, None
        with self.db.connection() as conn:
            state = research_check.check(conn, self.scope, candidate)
        if not state.done:
            return worker, candidate
        return (candidate if state.passed else worker), None

    def _compare_research(
        self,
        ctx: tools.ToolContext,
        candidate: str,
        question: str,
        url: str | None,
        site: str | None,
        worker_call: int,
        worker_sources: list[str],
        worker_answer: str,
    ) -> None:
        """0.12.0: ask the research model being checked the same question and keep the comparison; never more than
        the cycle can pay for after its reflection (a comparison it can't pay for waits for another question)."""
        request = prompts.research_request(self.settings, question, url, site, candidate)
        try:
            fits = self.meter.affordable(
                request, RESEARCH_CHECK, ctx.cycle_id, ctx.state.reflect_reserve, ctx.state.reflect_money
            )[0]
        except Unpriceable:
            return
        if not fits:
            return
        try:
            result = self._call(ctx.cycle_id, RESEARCH_CHECK, request)
            response = result.response or {}
            answer = _text_of(response).strip()
            compared = (result.call_id, len(_sources(response)), len(answer), bool(answer))
        except CallRefused:
            return
        except CallFailed as exc:
            compared = (exc.result.call_id, 0, 0, False)
        with self.db.transaction() as conn:
            state = research_check.record(
                conn,
                self.scope,
                candidate,
                ctx.cycle_id,
                question,
                (worker_call, len(worker_sources), len(worker_answer)),
                compared,
                to_iso(self.clock.now()),
            )
        if state.compared == research_check.QUESTIONS:
            level = "info" if state.passed else "warning"
            events.record(self.db, level, "agent", f"The research model's check {state.text()}"[:300])

    # --- drafts (0.12.0: a long file written by a metered call of its own) ---

    def _draft_fn(self, ctx: tools.ToolContext) -> tools.DraftFn:
        def draft(brief: str, sources: str) -> tools.Drafted | tools.Outcome:
            request = prompts.draft_request(self.settings, brief, sources)
            try:
                fits, expected, _ = self.meter.affordable(
                    request, "draft", ctx.cycle_id, ctx.state.reflect_reserve, ctx.state.reflect_money
                )
            except Unpriceable as exc:
                return tools.Outcome(False, f"Error: the draft can't be priced ({exc}).", "refused: unpriceable")
            if not fits:
                return tools.Outcome(
                    False,
                    f"Error: the draft could cost about ${micros_to_usd(expected):.3f}, more than this cycle has left"
                    f"{_kept(ctx)}.",
                    "refused: budget",
                )
            try:
                result = self._call(ctx.cycle_id, "draft", request)
            except CallRefused as exc:  # a state or system refusal ends the cycle at its next call
                return tools.Outcome(False, f"Error: the draft was refused ({exc.reason}).", "refused")
            except CallFailed as exc:
                why = exc.result.error or exc.result.status
                return tools.Outcome(False, f"Error: the draft failed ({why}).", "failed", paid=True)
            response = result.response or {}
            text = _unfenced(_text_of(response))
            if not text.strip():
                cost = micros_to_usd(result.cost_micros)
                return tools.Outcome(
                    False, f"Error: the draft came back empty (cost ${cost:.4f}).", "failed: empty", paid=True
                )
            self._save_text(result.call_id, text, response)
            return tools.Drafted(
                text.rstrip("\n") + "\n", response.get("stop_reason") == "max_tokens", result.cost_micros
            )

        return draft

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
                fits, expected, _ = self.meter.affordable(
                    request, "brainstorm", ctx.cycle_id, ctx.state.reflect_reserve, ctx.state.reflect_money
                )
            except Unpriceable as exc:
                return tools.Outcome(False, f"Error: a brainstorm can't be priced ({exc}).", "refused: unpriceable")
            if not fits:
                return tools.Outcome(
                    False,
                    f"Error: a brainstorm could cost about ${micros_to_usd(expected):.3f}, more than this cycle has"
                    f" left{_kept(ctx)}.",
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
                run = shop.run(ctx.cycle_id, task, files, script, folder, keep=ctx.state.reflect_money)
            except WorkshopError as exc:
                return tools.Outcome(False, f"Error: {exc}.", f"refused: {exc}"[:300])
            except CallRefused as exc:  # the budget guard's state or system refusal: the next call ends the cycle
                return tools.Outcome(False, f"Error: the workshop was refused ({exc.reason}).", "refused")
            ok, text, summary = workshop_report(run, lambda source, body: tools.wrap(ctx, source, body))
            return tools.Outcome(ok, text, summary, paid=run.calls > 0)

        return workshop

    # --- the last will ---

    def _last_will(self, cycle_id: int) -> CycleEnd:
        snap = self._snapshot(cycle_id=cycle_id)
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


def _market_fn(shop: Any) -> Callable[[str], Any]:
    """0.12.0: the owner's Etsy market probe (etsy_market_probe), for demand notes: the fake shop of a dry run needs no
    network; the owner's is reached by Ember's code only (the tool gets this function, not the shop)."""

    def probe(keywords: str) -> Any:
        with netguard.sealed() if shop.simulated else contextlib.nullcontext():
            return shop.market(keywords)

    return probe


def _unset(status: tuple[str, str | None], etsy: str) -> bool:
    """0.15.0: whether a channel switched on waits for its owner's setup: its own, or the Etsy shop's it needs."""
    return status[0] in ("not_configured", "not_connected") or (status[0] == "ok" and etsy != "ok")


def _comes(what: str) -> str:
    """0.19.2: "1 message ... comes first", "2 messages ... come first"."""
    return "come" if re.match(r"\d+ messages\b", what) else "comes"


def _bluesky_head(connection: BlueskyConnection, daily_limit: int) -> str:
    """0.19.0: BLUESKY's first line: the account, its followers at the last sync and the day's limit."""
    followers = connection.followers()
    known = f"{followers} follower{'' if followers == 1 else 's'}; " if followers is not None else ""
    return f"Ember's account: @{connection.handle()} ({known}at most {daily_limit} posts a day).\n"


def _waiting(name: str, status: tuple[str, str | None], etsy: str, why: str = "", venture: bool = True) -> str:
    """0.15.0: the one line of a channel switched on whose tools are off ("" when it is switched off, or nothing says
    why), so the agent knows it waits for the owner rather than asking for it again: the channel's own setup or the Etsy
    shop it needs. What else stopped a set-up channel (``why``) is given as it is, with no claim on the owner. 0.19.0:
    ``venture`` False for a channel no venture's first test waits for (Bluesky)."""
    state, reason = status
    if state == "disabled":
        return ""
    if state != "ok":
        why = (reason or state.replace("_", " ")).rstrip(".")
        if state == "not_configured" and name == "Pinterest":
            why += ", then System, Pinterest, Connect"
    elif etsy != "ok":
        why = "the Etsy shop isn't connected"
    elif why:  # set up, but its shop wasn't reached: the reason says whether the owner must act
        return f"Switched on, but no {name} tools this cycle: {why.rstrip('.')}."
    if not why:
        return ""
    why = why.rstrip(".")
    line = f"Switched on, but it waits for your owner's setup ({why}): no {name} tools until then."
    return f"{line} A venture it serves starts its first test only then." if venture else line


def _step(text: str) -> str:
    """A plan step as the brief shows it: at most STEP_CHARS characters, a longer one cut with "…" (0.11.1: steps were
    cut silently, often the first one, which answers the owner)."""
    return text if len(text) <= STEP_CHARS else text[: STEP_CHARS - 1].rstrip() + "…"


def _unfenced(text: str) -> str:
    """A draft's text without a code fence around all of it (0.12.0: it is told not to, but may)."""
    lines = text.strip("\n").split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


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


def write_records(conn: Any, scope: AgentScope, cycle_id: int, status: str, note: str | None, now: str) -> None:
    """What Ember's code writes of a cycle that ended ``status`` (``note``: why): its journal, when the agent wrote
    none, then its digest (0.12.0). Both say how it really ended (0.15.0)."""
    if not store.has_journal(conn, cycle_id):
        summary = f"Cycle ended {status}" + (f": {note}" if note else "")
        store.write_journal(conn, scope, cycle_id, "system", summary, _code_journal(conn, cycle_id, status), now)
    digest.write(conn, cycle_id, status, note, now)


def recover_records(conn: Any, scope: AgentScope, now: str) -> None:
    """0.15.0: the journal and digest of the cycles the app died in (stopped from outside, a crash, a power cut), which
    the budget guard marked interrupted at this start. They got neither, and the next plan saw the cycle before them.
    Only the ones since the scope's last cycle that ended otherwise: older ones are history."""
    session = (scope.session, 1 if scope.simulated else 0)
    killed = conn.execute(
        "SELECT id, status, note FROM cycles c WHERE status = 'interrupted' AND session = ? AND simulated = ?"
        " AND life_id = ? AND NOT EXISTS (SELECT 1 FROM cycle_digests d WHERE d.cycle_id = c.id)"
        " AND id > (SELECT COALESCE(MAX(id), 0) FROM cycles WHERE session = ? AND simulated = ?"
        " AND status <> 'interrupted') ORDER BY id",
        (*session, scope.life_id, *session),
    ).fetchall()
    for row in killed:
        write_records(conn, scope, int(row["id"]), str(row["status"]), row["note"], now)


def _code_journal(conn: Any, cycle_id: int, status: str = "completed") -> str:
    """0.12.0: the journal of a cycle whose reflection wrote none, built by Ember's code from its records: the goal,
    what its tools did (and what was refused or skipped, so it isn't taken for done) and what it cost. It was only
    "Goal: …". 0.15.0: of a cycle that ended ``status`` before its work did, where its work stopped and its plan's
    steps, as its digest says."""
    row = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    try:
        plan = json.loads(row["plan"]) if row and row["plan"] else {}
    except ValueError:
        plan = {}
    goal = plan.get("goal") if isinstance(plan, dict) else None
    lines = [
        "Written by Ember's code: the reflection wrote no journal.",
        f"Goal: {goal}" if goal else "No plan was made.",
    ]
    if goal and status not in ("completed", "idle"):
        lines += digest.ended(row, status)
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


def _burn_line(mode: burn.Burn, clock: Clock) -> str:
    """STATUS's burn mode (0.12.0), when it holds the agent back or (0.15.0) is projected to within
    burn.PROJECTED_DAYS, or (0.18.0) says to fight for a first euro: nothing in explore otherwise."""
    projected = burn.projected_text(mode, clock.now().astimezone(clock.tz))
    if mode.mode == burn.EXPLORE and not projected and not mode.fight:
        return ""
    return mode.text() + (f"; {projected}" if projected else "")


def _offered(ctx: tools.ToolContext) -> dict[str, bool]:
    """0.15.0: the tools the burn mode leaves this cycle (the workshop not in maintenance, brainstorm only in
    explore), the same for every step and the reflection, so the cache holds."""
    return {"workshop": ctx.workshop is not None, "brainstorm": ctx.brainstorm is not None}


def _cap_note(why: str) -> str:
    """0.15.0: why STATUS's cycle cap is below the owner's option (metering.cycle_room says which)."""
    if why == "maintenance":
        return f" (maintenance: ${burn.MAINTENANCE_CYCLE_USD:.2f} a cycle, every call counted)"
    if why == "events":
        return f" (until {EVENT_RESERVE_HOUR}:00 a fifth of today's cap is kept for event wake-ups)"
    return ""


def _usd(micros: int) -> str:
    return f"${micros_to_usd(micros):.2f}"
