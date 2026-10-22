"""0.42.2 (analysis-0.40.1, 5.6): what every tool handler shares, split out of tools.py.

A handler gets a ``ToolContext`` (the database, the clock, the scope, the workspace, the memory, what the cycle's tools
remember in ``CycleTools``, and the accounts the cycle has: ``MailAccess`` to ``KdpAccess``) and returns an
``Outcome``; it refuses by raising ``ToolError``, which tools.run turns into an error result. ``wrap`` marks text from
outside (a file, an email, a research result) as data for the model; ``_unstop``, ``_numbers``, ``_cut``, ``_local``
and ``_quoted`` are the small text helpers the handlers of every area use.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from ..integrations import etsy
from ..products import blog
from ..products.site import Owner as SiteOwner
from .memory import Memory
from .sandbox import Jail
from .store import AgentScope

# --- running tools ---


class ToolError(Exception):
    """Refused or invalid; shown to the model as an error result."""


@dataclass
class CycleTools:
    """What the tools remember during one cycle."""

    sleep_minutes: int | None = None
    sleep_reason: str = ""
    seen_urls: set[str] = field(default_factory=set)  # URLs from this cycle's research results
    focus_project_id: int | None = None
    focus_venture_id: int | None = None
    # 0.28.0: one product line a cycle (lines.py): set by the loop for an ordinary, marketing or reactive cycle, whose
    # tools then work on its line (focus_project_id) only; until it has one, a call takes the line it works on
    # (touched) once it succeeds (_lock).
    one_line: bool = False
    touched: int | None = None
    # 0.39.1: the step the plan tree took for this cycle (YOUR STEP): plan_step means it when no step_id is named
    step_id: int | None = None
    journal_written: bool = False
    # 0.24.0: a journal a work step wrote (write_journal's checked arguments): the cycle's, unless the reflection writes
    # one (loop._keep_draft)
    journal_draft: dict[str, Any] | None = None
    strikes: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    # 0.12.0: what the reflection may cost, as the last work step was checked against: research, brainstorms and
    # workshop runs leave it (they spent it, and the cycle ended without reflecting). Under the cycle cap, what it is
    # expected to cost (reflect_reserve); under the daily cap and the balance, its worst case (reflect_money).
    reflect_reserve: int = 0
    reflect_money: int = 0
    # 0.12.0: the memory files read whole this cycle (memory_read), each with the model reply that asked for it;
    # reply is the one whose tool calls run now (a file read in the same reply was not seen yet).
    read_memory: dict[str, int] = field(default_factory=dict)
    reply: int = 0
    # 0.12.0: the conversation's size before the latest step, and the largest step so far, in rough tokens (what the
    # reflection's price leaves room for).
    conversation_tokens: int = 0
    largest_step_tokens: int = 0
    policy_note: str = ""  # 0.13.0: what the owner's unlock did with the request just made (policy.apply)
    # 0.33.0: the messages to the owner sent back once for a promise in their words only (_unrecorded_promise)
    bounced: set[str] = field(default_factory=set)
    # 0.40.0: the paperback interiors whose pages a KDP check drew this cycle, by path (_page_check)
    drawn: dict[str, Drawn] = field(default_factory=dict)


@dataclass(frozen=True)
class Drawn:
    """0.40.0: a paperback interior's pages a KDP check drew: of the file with this SHA-256, in this model reply."""

    sha256: str
    reply: int
    pages: tuple[int, ...]  # the pages drawn
    count: int  # its pages in all
    findings: tuple[str, ...]  # what Ember's code is sure is wrong (pagecheck.Check.findings)
    placeholders: tuple[str, ...] = ()  # 0.40.2: the placeholders its pages print (pagecheck.Check.placeholders)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    text: str
    summary: str
    project_id: int | None = None
    image: bytes | None = None  # a PNG the model sees with the text (the look tool)
    pictures: tuple[bytes, ...] = ()  # 0.40.0: more PNGs it sees after it (a KDP check's pages)
    # 0.12.0: a paid call (research, brainstorm, workshop) was sent: it counts toward the tool's per-cycle limit even
    # when it failed, or retries of failing calls went on spending past the limit.
    paid: bool = False
    # 0.12.0: answered from an earlier call without a new one (a repeated research question): free, and it doesn't
    # count toward the tool's per-cycle limit.
    reused: bool = False
    # 0.39.4: the calls Ember's code took out of this one's journal and ran (run): they count toward the reply's
    # MAX_TOOL_CALLS_PER_TURN, which the journal itself doesn't (one reply of 8 journals ran 16 calls).
    taken: int = 0


# question, a page to read, cycle, the site searched, the venture it is for (0.12.0)
ResearchFn = Callable[[str, "str | None", int, "str | None", "int | None"], Outcome]
# task, workspace files, a kept script to run again, the folder for the results
WorkshopFn = Callable[[str, list[str], "str | None", "str | None"], Outcome]
BrainstormFn = Callable[[str, "int | None"], Outcome]  # the theme ("" for anything), the venture to branch from


@dataclass(frozen=True)
class Drafted:
    """A draft's text (0.12.0), whether its reply was cut off at its length, and what it cost."""

    text: str
    cut_off: bool
    cost_micros: int


DraftFn = Callable[[str, str], "Drafted | Outcome"]  # the brief, the sources marked as data: the text, or why not


@dataclass(frozen=True)
class MailAccess:
    """What the tools know of Ember's mailbox: its address and send limit, never its password or a way to send."""

    address: str
    daily_limit: int


@dataclass(frozen=True)
class EtsyAccess:
    """What the tools know of the Etsy shop: its name, currency, daily limit and categories, never a token or a
    way to reach Etsy."""

    shop_name: str
    currency: str
    daily_limit: int
    categories: tuple[tuple[int, str], ...]
    stats_history: bool = False  # the owner keeps the listings' views over time (etsy_stats_history, 0.12.0)


@dataclass(frozen=True)
class PinterestAccess:
    """What the tools know of the owner's Pinterest account (0.13.0, Phase E2): its name and the daily limit, never a
    token or a way to reach Pinterest."""

    username: str
    daily_limit: int


@dataclass(frozen=True)
class BlueskyAccess:
    """What the tools know of the Bluesky account the owner made for Ember (0.19.0): its handle, the daily limit and
    the owner's website (a post may link it), never the app password or a way to reach Bluesky."""

    handle: str
    daily_limit: int
    site_url: str = ""  # the owner's website ("": none set)
    followers: int | None = None  # 0.37.7: at the last sync (None before one)


@dataclass(frozen=True)
class PrintifyAccess:
    """What the tools know of the owner's Printify account (0.13.0, Phase E4): its shop's name, the currency of its
    prices and the daily limit, never the token or a way to reach Printify (the catalog comes through ``catalog``)."""

    shop_title: str
    currency: str
    daily_limit: int
    buyer_ships: bool = False  # 0.15.0: the owner's printify_buyer_pays_shipping
    bill_vat: bool = True  # 0.15.0: the owner's printify_bill_vat


CatalogFn = Callable[[str | None, int | None, int | None], str]  # search, blueprint, provider: the catalog's answer


@dataclass(frozen=True)
class BlogAccess:
    """0.14.0: the owner's blog, when they switched it on: the site's data and what keeps it from being published."""

    owner: blog.Owner
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class KdpAccess:
    """0.25.0: what the KDP tools know: the author name the owner's books carry ("": they enter it at KDP)."""

    author: str = ""


@dataclass
class ToolContext:
    db: Database
    clock: Clock
    scope: AgentScope
    cycle_id: int
    workspace: Jail
    memory: Memory
    min_sleep: int
    max_sleep: int
    state: CycleTools
    research: ResearchFn | None = None
    workshop: WorkshopFn | None = None
    allow_fetch: bool = True  # the owner's web_fetch option (live mode)
    mail: MailAccess | None = None  # Ember's mailbox, when it has one
    etsy: EtsyAccess | None = None  # the Etsy shop, when there is one
    pinterest: PinterestAccess | None = None  # the owner's Pinterest account, when connected (0.13.0)
    bluesky: BlueskyAccess | None = None  # the Bluesky account the owner made for Ember, when set up (0.19.0)
    printify: PrintifyAccess | None = None  # the owner's Printify account, when set up (0.13.0)
    catalog: CatalogFn | None = None  # Printify's catalog (0.13.0): kept by Ember's code, read at Printify when old
    site: SiteOwner | None = None  # the owner's data, when they switched their website on (0.13.0): its pages
    blog: BlogAccess | None = None  # the owner's blog, when they switched it on (0.14.0)
    kdp: KdpAccess | None = None  # Amazon KDP, when the owner switched it on (0.25.0)
    venture: bool = False  # a venture cycle (0.10.0): brainstorm, and more research
    marketing: bool = False  # 0.28.0: a marketing cycle (lines.py): the marketing tools, none for building
    marketing_apart: bool = False  # 0.28.0: marketing cycles run, so an ordinary cycle has no marketing tools
    usd_per_eur: float = 0.0  # the owner's exchange rate (etsy_usd_per_eur; 0: none, econ assumes one), 0.13.0
    venture_cash_eur: float = 20.0  # the owner's cash for a venture's first test (a knock-out beyond it), 0.13.0
    net_runway_days: float | None = None  # at the cycle's start (None: it earns what it spends), 0.13.0
    unlocks_off: str = ""  # 0.15.0: why the owner's unlocks don't act now (policy.off; "" when they do)
    library: bool = False  # the owner's library holds documents (0.12.0): its tools
    brainstorm: BrainstormFn | None = None
    draft: DraftFn | None = None  # 0.12.0
    market: Callable[[str], etsy.Market] | None = None  # 0.12.0: the owner's Etsy market probe (etsy_market_probe)
    nonce: str = field(default_factory=lambda: secrets.token_hex(3))

    def now(self) -> str:
        return to_iso(self.clock.now())


def _unstop(text: str) -> str:
    """A message without its final full stop (an error result adds one)."""
    return text[:-1] if text.endswith(".") else text


def wrap(ctx: ToolContext, source: str, text: str) -> str:
    """Mark untrusted text as data, with a per-cycle nonce the text itself can't close."""
    clean = text.replace(ctx.nonce, "")
    return f'<data src="{source}" id="{ctx.nonce}">\n{clean}\n</data id="{ctx.nonce}">'


def _numbers(ids: list[int]) -> str:
    return ", ".join(f"#{i}" for i in ids)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _local(ctx: ToolContext, stamp: str | None) -> str:
    try:
        return from_iso(stamp).astimezone(ctx.clock.tz).strftime("%Y-%m-%d %H:%M") if stamp else "-"
    except ValueError:
        return "-"


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)
