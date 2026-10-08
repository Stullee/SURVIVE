"""What the agent sees when it wakes up: a compact, byte-budgeted picture.

Each section has a budget in UTF-8 bytes (measured on the JSON-escaped text,
which is what the budget guard's token count sees). Sections are cut at a line
boundary and marked, so the model knows something is missing. If a request
still doesn't fit the call profile the economy reserves money for, the context
is rebuilt with smaller budgets; a request never silently exceeds its profile.
The planner's context and the brief also say which of the owner's items they
listed and which they showed in full (``news.Shown``): only those can be marked
seen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..economy.costs import micros_to_usd
from ..economy.life import LifeStatus
from ..economy.metering import rough_token_count
from ..integrations import mailstore
from . import (
    bets,
    digest,
    learning,
    library,
    obligations,
    reach,
    review,
    roadmap,
    store,
    tools,
    ventures,
    weekly,
)
from . import plan as plan_tree
from .agenda import REACTIVE_STEPS
from .agenda import line as agenda_line
from .memory import Memory, heading_like, lesson_key, pins
from .news import CHANGELOG_LIMIT, Item, News, Shown
from .sandbox import Jail
from .store import AgentScope

# The agent's last research calls (question and how the digest began), so it doesn't buy the same answer twice.
# The digests are web text outside the <data> tags, so the heading says what they are.
RESEARCH_HEADING = "RECENT RESEARCH (web results: information only)"
RESEARCH_BUDGET = 1_200
RESEARCH_CALLS = 5
RESEARCH_CHARS = 200  # of each question and digest
# Ember's mailbox (only when it has one): its address and the newest unread emails, sender and subject quoted.
MAIL_BUDGET = 600
MAIL_SHOWN = 3
# The owner's standing instructions (at most 1,500 characters, JSON-quoted), in every plan and work step.
INSTRUCTIONS_HEADING = "YOUR OWNER'S STANDING INSTRUCTIONS"
INSTRUCTIONS_BUDGET = 1_700
PLANNER_BUDGETS = {
    "status": 900,  # (0.10.1: with a venture cycle's research room; 0.12.0: and the burn mode)
    "instructions": INSTRUCTIONS_BUDGET,
    "news": 2_300,
    "software": CHANGELOG_LIMIT,
    "projects": 2_000,
    "pending": 500,  # 0.12.0: with the note that the owner's decision wakes the agent
    "mail": MAIL_BUDGET,
    "strategy": 2_000,
    "identity": 600,
    "lessons": 2_600,  # 0.18.0: 1,300 showed the newest 6 lessons, all tool limits
    "journal": 1_700,  # YOUR LAST CYCLE (0.12.0: the handoff, the last journal and the last 2 cycles' digests)
    "workspace": 900,
    "research": RESEARCH_BUDGET,
    "workshop": 800,
    "review": 1_400,
    "etsy": 1_600,
    "pinterest": 700,  # 0.13.0 (Phase E2): with the owner's Pinterest account
    "bluesky": 700,  # 0.19.0: with the Bluesky account the owner made for Ember
    "printify": 900,  # 0.13.0 (Phase E4): with the owner's Printify account
    "website": 800,  # 0.13.0 (Phase E3): when the owner switched their website on
    "blog": 900,  # 0.14.0: when the owner switched their blog on
    "kdp": 900,  # 0.25.0: when the owner switched Amazon KDP on
    "ventures": 2_600,
    # 0.13.0: a venture cycle's READY list (desk.MAX_ITEMS items) and the forecasts' record; 0.35.0: or an ordinary or
    # marketing cycle's YOUR STEP (plan.step_text): a cycle has one of them
    "ready": 1_550,
    # 0.11.0's ROADMAP (and never less than PLAN_FLOOR, whatever the scale: 0.12.0; 0.29.0: the goal); 0.35.0: YOUR
    # PLAN, the plan tree's products under the goal
    "plan": 2_200,
    "library": 1_200,  # 0.12.0: the owner's library, when it holds documents
}
# 0.12.0: the ROADMAP isn't scaled down with the other sections (its checks and goals come first, and the cut took
# every goal once the budget shrank). 0.29.0: with the owner's goal first, and how far each milestone got. 0.35.0: YOUR
# PLAN, in its place.
PLAN_FLOOR = 2_200
PLAN_HEADING = "YOUR PLAN"  # 0.35.0: the plan tree under the owner's goal (plan.plan_text)
SHOWN_MILESTONES = 6  # 0.35.0: the milestones still open YOUR PLAN lists (a venture's first test, the owner's own)
# 0.15.0: what the sections leave of their budgets goes to the sections that were cut, in this order (the day's review
# lost its advice and ROADMAP a first test while 8.6 KB went unused). The plan stays within the budgets' sum.
SPARE_ORDER = (
    "review",
    "plan",
    "journal",
    "projects",
    "ventures",
    "ready",
    "pending",
    "etsy",
    "printify",
    "pinterest",
    "bluesky",
    "website",
    "blog",
    "kdp",
    "strategy",
    "identity",
    "library",
    "workshop",
)
READY_HEADING = "READY"  # 0.13.0: a venture cycle's decisions, ranked by Ember's code (desk.py)
STEP_HEADING = "YOUR STEP"  # 0.35.0: the step the plan tree took for an ordinary or marketing cycle (plan.py)
# 0.12.0: while requests wait for the owner, the plan is told that waiting isn't its job.
WAITING_NOTE = "Your owner's decision on these wakes you: don't wait for it, work on something else meanwhile."
# The owner's decisions and messages in the brief and the will context, as much as the planner's news share:
# room for one whole message of plain text at the owner's limit of 2,000 characters.
OWNER_BUDGET = 2_300
OPEN_UPGRADES = 5  # 0.15.0: the open upgrade requests WAITING FOR YOUR OWNER lists
SHOWN_PROJECTS = 8  # open projects the plan shows in full, the ones updated last (0.19.1: the others by name)
QUOTE_CAP = 300  # characters of each text quoted in a decision or upgrade line, when the owner's news is shortened
SHORTEST_QUOTE = 40  # no quoted text is shortened below this; if that isn't enough, the last lines are cut
# The owner's (standing instructions and news), the mail and the research sections (and their headings) come on top
# of the brief's budget, so they never squeeze the rest.
# (6,500 since 0.10.0: a venture's focus is longer. A milestone's (0.11.0) is short: a brief with a venture's, a
# project's and a milestone's focus at their longest loses its end, as a brief over its budget always does.)
BRIEF_BUDGET = 6_500
# 0.12.0: the learnings from the owner's library that match the plan (Ember's code picks them), on top of the brief.
KNOWLEDGE_HEADING = "WHAT YOU LEARNED (your playbook and cases, your owner's library)"  # 0.18.0: with your own
KNOWLEDGE_BUDGET = 1_800
VENTURE_FOCUS_BUDGET = 2_400  # a venture's FOCUS in the brief (0.15.0: 1,900 cut its numbers and pitch)
MILESTONE_FOCUS_BUDGET = 1_100  # a milestone's FOCUS in the brief (0.11.0; 0.12.0: with its last cycle's digest)
BRAINSTORM_BRIEF = "grow the tree with brainstorm (first, if you plan one), "  # 0.15.0: only in explore
VENTURE_BRIEF = (
    "This is a venture cycle: answer your owner's waiting messages first, read guide 'ventures', research as often "
    "as this cycle can pay for (STATUS), "
    f"{BRAINSTORM_BRIEF}save each number your research finds with evidence and "
    "the rest with venture_update (learned, with sources), and rescore the venture from the evidence."
)
# 0.28.0: a marketing cycle's work steps (lines.py): what the cycle is for, beside its line in FOCUS.
MARKETING_BRIEF = (
    "This is a marketing cycle: answer your owner's waiting messages first, read the guides of the channels you use "
    "('pinterest', 'bluesky', 'blog'), and bring buyers to this line's listings (FOCUS lists them): pins and Bluesky "
    "posts that link them, a blog post that recommends one, a Reddit post, better titles and tags; then bet on what "
    "it will bring."
)
# 0.28.0: the line's funnel and reach in FOCUS (a marketing cycle's live listings too); 0.30.0: and the next step its
# last cycle left
LINE_FOCUS_BUDGET = 1_000
# 0.33.0: the strategy in the work steps' brief, inside its budget (the strategy file holds 2,000 bytes at most)
STRATEGY_BRIEF_BUDGET = 1_200
# 0.12.0: the brief's copy of OBLIGATIONS (the plan's is never cut), on top of the brief's budget like the owner's.
OBLIGATIONS_BRIEF_BUDGET = 1_000
# 0.12.0: the lessons the owner pinned come first in LESSONS, on top of its budget (at most memory.MAX_PINS of them).
PINS_BUDGET = 2_000
PLAYBOOK_SHARE = 0.6  # 0.18.0: of LESSONS' budget, the most the playbook takes
PINS_HEADING = "Pinned by your owner (always kept):"
# The largest brief, those sections and their headings included: the WORK and REFLECT profiles are measured on it.
BRIEF_MAX = (
    BRIEF_BUDGET
    + INSTRUCTIONS_BUDGET
    + OWNER_BUDGET
    + MAIL_BUDGET
    + RESEARCH_BUDGET
    + KNOWLEDGE_BUDGET
    + OBLIGATIONS_BRIEF_BUDGET
    + PINS_BUDGET
    + 330
)
WILL_BUDGET = 4_500 + OWNER_BUDGET + 100  # the largest will context: the LAST_WILL profile is measured on it
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')  # a JSON string: how the owner's and the agent's texts are quoted
# 0.12.0: the agent's memory files are headed as its own words, so a forged "FROM YOUR OWNER" in one is plainly its own.
STRATEGY_HEADING = "STRATEGY (written by you)"
IDENTITY_HEADING = "IDENTITY (written by you)"
LESSONS_HEADING = "LESSONS (written by you, newest last)"
_LINE_BREAK = re.compile(r"(\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029])")  # what str.splitlines() splits at
_DIGEST = re.compile(r'<data src="research" id="[0-9a-f]+">\n(.*?)\n</data id="[0-9a-f]+">', re.DOTALL)


def json_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def cut(text: str, budget: int) -> str:
    """At most ``budget`` bytes (JSON-escaped), cut at a line boundary with a marker."""
    if json_bytes(text) <= budget:
        return text
    lines = text.split("\n")  # only at "\n", so what is kept is exactly the text's start
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join([*kept, line])
        if json_bytes(candidate) > budget - 40:
            break
        kept.append(line)
    if not kept:  # one long line: as many characters as fit, whatever their size
        kept = [text[: _largest(0, len(text), lambda n: json_bytes(text[:n]) <= budget - 40)]]
    removed = len(text.encode("utf-8")) - len("\n".join(kept).encode("utf-8"))
    return "\n".join(kept) + f"\n…[{removed} bytes cut]"


def _largest(low: int, high: int, ok: Callable[[int], bool]) -> int:
    """The largest n from ``low`` to ``high`` for which ``ok(n)`` holds, or ``low`` (``ok`` turns false as n grows)."""
    while low < high:
        middle = (low + high + 1) // 2
        if ok(middle):
            low = middle
        else:
            high = middle - 1
    return low


def fits(request: dict[str, Any], input_tokens: int) -> bool:
    return rough_token_count(request) <= input_tokens


@dataclass(frozen=True)
class MailView:
    """What the MAIL section shows: Ember's address and its unread emails, the newest (id, sender, subject) first."""

    address: str
    unread: int = 0
    newest: tuple[tuple[int, str, str], ...] = ()


@dataclass
class Snapshot:
    """Everything the context needs, read in one transaction."""

    status: LifeStatus
    local_time: str
    version: str
    agent_name: str
    today_spend: int
    daily_cap: float
    cycle_cap: float  # 0.15.0: what this cycle may still spend under the cap in force (metering.cycle_room)
    cap_note: str = ""  # 0.15.0: why that is below the owner's option ("" if it isn't)
    brainstorm: bool = True  # 0.15.0: the burn mode allows brainstorms (explore)
    projects: list[sqlite3.Row] = field(default_factory=list)
    project_money: dict[int, tuple[int, int]] = field(default_factory=dict)
    project_funnels: dict[int, str] = field(default_factory=dict)  # 0.18.0: reach.Funnel.short
    project_bets: dict[int, list[str]] = field(default_factory=dict)  # 0.18.0: the open bets
    playbook: list[Any] = field(default_factory=list)  # 0.18.0: the active principles (learning.principles)
    owner_messages: list[sqlite3.Row] = field(default_factory=list)
    pending: list[sqlite3.Row] = field(default_factory=list)
    upgrades: list[sqlite3.Row] = field(default_factory=list)  # 0.15.0: its open upgrade requests, newest first
    last_cycle: sqlite3.Row | None = None
    last_journal: sqlite3.Row | None = None
    handoff: sqlite3.Row | None = None  # 0.15.0: the newest handoff the agent wrote (its cycle_id and handoff)
    # 0.30.0: what the cycles that wrote those handoffs worked on ("line #4", "a venture cycle", ...), by cycle
    handoff_from: dict[int, str] = field(default_factory=dict)
    digests: list[str] = field(default_factory=list)  # the last cycles' digests, newest first (0.12.0)
    obligations: str = ""  # 0.12.0: what the agent owes (OBLIGATIONS), bounded: never cut in the plan
    memory: dict[str, str] = field(default_factory=dict)
    pins: list[str] = field(default_factory=list)  # 0.12.0: the lessons the owner pinned
    workspace: list[str] = field(default_factory=list)
    workspace_usage: str = ""  # 0.12.0: what the workspace holds of its limits, for STATUS
    journal: list[sqlite3.Row] = field(default_factory=list)
    news: News = field(default_factory=News)
    research: list[sqlite3.Row] = field(default_factory=list)
    mail: MailView | None = None  # None: Ember has no mailbox (then there is no MAIL section)
    instructions: str = ""  # the owner's standing instructions ("" while there are none)
    proven: list[tuple[str, str]] = field(default_factory=list)  # workshop scripts worth building in: (path, why)
    review: str = ""  # today's daily review, as the planner sees it ("" before it is made)
    etsy: str = ""  # the ETSY SHOP section ("" without a shop)
    pinterest: str = ""  # the PINTEREST section ("" while off; 0.15.0: one line while not set up), 0.13.0
    bluesky: str = ""  # the BLUESKY section ("" while off; one line while not set up), 0.19.0
    printify: str = ""  # the PRINTIFY section ("" while off; 0.15.0: one line while not set up), 0.13.0
    website: str = ""  # the WEBSITE section ("" while the owner's website is off), 0.13.0
    blog: str = ""  # the BLOG section ("" while the owner's blog is off), 0.14.0
    kdp: str = ""  # the KDP section ("" while Amazon KDP is off), 0.25.0
    ventures: list[sqlite3.Row] = field(default_factory=list)  # the venture tree (0.10.0)
    venture_money: dict[int, ventures.Money] = field(default_factory=dict)
    venture: bool = False  # a venture cycle
    venture_share: int = 0  # the owner's share of the spending for ventures, in percent
    venture_day: tuple[int, int] = (0, 0)  # today's spending, and the venture cycles' part of it
    marketing: bool = False  # 0.28.0: a marketing cycle (lines.py)
    marketing_apart: bool = False  # 0.28.0: marketing cycles run, so an ordinary cycle has no marketing tools
    call_costs: dict[str, int] = field(default_factory=dict)  # what research and brainstorms cost lately (0.10.1)
    today: date | None = None  # the owner's local date (the milestones' horizons are counted from it)
    # 0.35.0: YOUR PLAN, the plan tree under the goal (plan.plan_text), in place of the ROADMAP (0.11.0 to 0.34.0):
    # with how far the goal got (0.29.0) and what stands unlocked for each product (0.16.3, analysis bug 5)
    plan: str = ""
    library: library.Shelf | None = None  # the owner's library (0.12.0): None while it is empty
    # the owner's decisions on its requests wake the agent (0.12.0, the wake_on_decision option; 0.31.0, and the
    # wake_on_approval or wake_on_rejection one)
    decision_wakes: bool = False
    burn: str = ""  # 0.12.0: the burn mode Ember's code set from the net runway (burn.Burn.text)
    # READY, ranked by Ember's code: a venture cycle's (desk.text); 0.35.0: an ordinary or marketing cycle's YOUR STEP,
    # the step the plan tree took (plan.step_text)
    ready: str = ""
    agenda: list[sqlite3.Row] = field(default_factory=list)  # 0.13.0: events no plan has shown yet (agenda.py)
    reactive: bool = False  # 0.13.0: a cycle an event woke


def snapshot(
    conn: sqlite3.Connection,
    scope: AgentScope,
    status: LifeStatus,
    memory: Memory,
    workspace: Jail,
    *,
    local_time: str,
    version: str,
    agent_name: str,
    today_spend: int,
    daily_cap: float,
    cycle_cap: float,
    cap_note: str = "",
    brainstorm: bool = True,
    news: News | None = None,
    mail_address: str | None = None,
    today: date | None = None,
    etsy: str = "",
    venture: bool = False,
    venture_share: int = 0,
    marketing: bool = False,
    marketing_apart: bool = False,
    shelf: library.Shelf | None = None,
    pinterest: str = "",
    printify: str = "",
    website: str = "",
    blog: str = "",
    bluesky: str = "",
    kdp: str = "",
    decision_wakes: bool = False,
    burn: str = "",
    ready: str = "",
    agenda: list[sqlite3.Row] | None = None,
    reactive: bool = False,
    books: tuple[int, int] | None = None,
    now: str | None = None,
) -> Snapshot:
    """What the planner, the brief and the will see; ``today`` (the owner's local date) finds the day's review and
    the day's spending on ventures; ``now`` (0.35.0): the plan tree's numbers are read then."""
    projects = store.open_projects(conn, scope)
    todays_review = review.of_day(conn, scope, today) if today is not None else None
    mail = None
    if mail_address is not None:
        unread, newest = mailstore.unread(conn, scope, MAIL_SHOWN)
        mail = MailView(mail_address, unread, tuple((r["id"], r["from_addr"], r["subject"]) for r in newest))
    money: dict[int, tuple[int, int]] = {}
    for p in projects:
        spent = conn.execute(
            "SELECT COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
            " WHERE y.project_id = ?",
            (p["id"],),
        ).fetchone()[0]
        earned = conn.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type = 'revenue' AND project_id = ?", (p["id"],)
        ).fetchone()[0]
        money[p["id"]] = (int(spent), int(earned))
    last_cycle = conn.execute(
        "SELECT * FROM cycles WHERE session = ? AND simulated = ? AND status <> 'running' ORDER BY id DESC LIMIT 1",
        (scope.session, 1 if scope.simulated else 0),
    ).fetchone()
    journal = store.journal(conn, scope, 5)
    where, params = scope.where()
    handoff = conn.execute(
        f"SELECT cycle_id, handoff FROM journal WHERE {where} AND author = 'agent'"
        " AND handoff IS NOT NULL AND handoff <> '' ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    files = _safe_listing(workspace)
    standing = store.standing_instructions(conn, scope)
    spent, ventured, _ = ventures.day_spends(conn, scope, today) if today is not None else (0, 0, 0)
    memories = memory.read_all()
    written = {int(r["cycle_id"]) for r in (journal[:1] + ([handoff] if handoff is not None else []))}
    return Snapshot(
        status=status,
        local_time=local_time,
        version=version,
        agent_name=agent_name,
        today_spend=today_spend,
        daily_cap=daily_cap,
        cycle_cap=cycle_cap,
        cap_note=cap_note,
        brainstorm=brainstorm,
        projects=projects,
        project_money=money,
        project_funnels={pid: f.short() for pid, f in reach.funnels(conn, scope).items()},
        project_bets=bets.open_lines(conn, scope),
        playbook=learning.principles(conn, scope),
        owner_messages=store.open_messages(conn, scope, 8),
        pending=store.pending_requests(conn, scope),  # 0.15.0: every one, not those among the newest 20 requests
        upgrades=open_upgrades(conn, scope),
        last_cycle=last_cycle,
        last_journal=journal[0] if journal else None,
        handoff=handoff,
        handoff_from={cycle_id: _worked_on(conn, cycle_id) for cycle_id in written},
        digests=digest.latest(conn, scope),
        obligations=obligations.text(conn, scope, today, memories.get("strategy", "")) if today is not None else "",
        memory=memories,
        pins=[str(p["text"]) for p in pins(conn, scope)],
        workspace=files,
        workspace_usage=_usage_line(workspace),
        journal=journal,
        news=news or News(),
        research=store.recent_research(conn, scope, RESEARCH_CALLS),
        mail=mail,
        instructions=standing["text"] if standing else "",
        proven=store.proven_scripts(conn, scope),
        review="\n\n".join(  # 0.18.0: with the week's look
            text
            for text in (
                review.planner_text(conn, todays_review) if todays_review is not None else "",
                weekly.planner_text(weekly.latest(conn, scope, today)) if today is not None else "",
            )
            if text
        ),
        etsy=etsy,
        pinterest=pinterest,
        bluesky=bluesky,
        printify=printify,
        website=website,
        blog=blog,
        kdp=kdp,
        ventures=ventures.all_ventures(conn, scope),
        venture_money=ventures.money(conn, scope),
        venture=venture,
        venture_share=venture_share,
        venture_day=(spent, ventured),
        marketing=marketing,
        marketing_apart=marketing_apart,
        call_costs=ventures.call_costs(conn, scope) if venture else {},
        today=today,
        plan=_plan(conn, scope, now, today, books, last_cycle["ended_at"] if last_cycle else None)
        if today is not None
        else "",
        library=shelf,
        decision_wakes=decision_wakes,
        burn=burn,
        ready=ready,
        agenda=agenda or [],
        reactive=reactive,
    )


def _worked_on(conn: sqlite3.Connection, cycle_id: int) -> str:
    """0.30.0: what a cycle worked on, as YOUR LAST CYCLE names the cycle a handoff came from ('' for a cycle on no
    line, or none)."""
    row = conn.execute("SELECT project_id, venture, marketing FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is None:
        return ""
    if row["venture"]:
        return "a venture cycle"
    line = f"line #{row['project_id']}" if row["project_id"] is not None else ""
    if row["marketing"]:
        return f"a marketing cycle on {line}" if line else "a marketing cycle"
    return f"a cycle on {line}" if line else ""  # a handoff of no line is for whatever comes next


def _plan(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str | None,
    today: date,
    books: tuple[int, int] | None,
    since: str | None = None,
) -> str:
    """0.35.0: YOUR PLAN: the goal at the root (how far it got, 0.29.0), what the owner did to the plan since the last
    cycle (``since``), the plan tree's products, the milestones still open (a venture's first test, the owner's own)."""
    milestones = roadmap.open_milestones(conn, scope)
    progress = roadmap.progress_for(conn, scope, today, books)
    top = roadmap.root(conn, scope)
    goal = roadmap.root_line(top, today, progress) if top is not None else ""
    rest = [m for m in milestones if top is None or m["id"] != top["id"]]
    shown = [roadmap.milestone_line(m, today, False, progress=progress) for m in rest[:SHOWN_MILESTONES]]
    if len(rest) > SHOWN_MILESTONES:
        shown.append(f"… and {len(rest) - SHOWN_MILESTONES} more")
    return plan_tree.plan_text(conn, scope, now or f"{today.isoformat()}T00:00:00+00:00", today, goal, shown, since)


def _safe_listing(workspace: Jail, shown: int = 19, budget: int = 900) -> list[str]:
    """The WORKSPACE lines: the files in every folder with their sizes, then how many more if some are left out.

    At most ``shown`` files and ``budget`` bytes in all, so the count is within the brief's 20 lines and the brief
    keeps room for its LIMITS, even with a long plan and focus.
    """
    try:
        files = workspace.walk(workspace.limits.max_entries).files
    except Exception:  # noqa: BLE001 - the context must still be built
        return []
    lines: list[str] = []
    for e in files[:shown]:
        line = f"{e.path} ({e.size:,} B)"
        if json_bytes("\n".join([*lines, line])) > budget - 30:  # - 30: room for the count
            break
        lines.append(line)
    if len(files) > len(lines):
        lines.append(f"… and {len(files) - len(lines)} more files")
    return lines


def _usage_line(workspace: Jail) -> str:
    """What the workspace holds of its limits (0.12.0: its old limit stopped production without warning)."""
    try:
        used = workspace.usage()
        text, product = workspace.sizes()
    except Exception:  # noqa: BLE001 - the context must still be built
        return ""
    limits, mb = workspace.limits, 1024 * 1024
    return (
        f"Workspace: {used.files:,} of {limits.max_files:,} files; text {text / mb:.1f} of"
        f" {limits.max_total_bytes // mb:,} MB, products {product / mb:.1f} of"
        f" {limits.max_product_total_bytes // mb:,} MB."
    )


def status_text(s: Snapshot, dry_run: bool) -> str:
    st = s.status
    runway = review.runway_text(st.runway)  # 0.12.0: and net of revenue and expenses
    lines = [
        f"Time: {s.local_time}. You are {s.agent_name}, version {s.version}."
        + (" DRY RUN (simulated money)." if dry_run else ""),
        f"State: {st.state}. Balance ${micros_to_usd(st.balance):.2f}. Runway {runway}.",
        f"Spent today ${micros_to_usd(s.today_spend):.2f} of ${s.daily_cap:.2f}."
        f" This cycle may spend up to ${s.cycle_cap:.2f}{s.cap_note}.",
    ]
    if s.burn:
        lines.append(f"Burn mode, set by Ember's code: {s.burn}.")
    if s.workspace_usage:
        lines.append(s.workspace_usage)
    if s.venture_share:
        lines.append(_share_line(s))
        if s.venture:
            lines.append(ventures.room_text(s.cycle_cap, s.call_costs, s.brainstorm))
    if st.last_will_due:
        lines.append("Your money is nearly gone: your last will is due.")
    return "\n".join(lines)


def _share_line(s: Snapshot) -> str:
    """STATUS's line on the share of the spending the owner gives ventures, in one line (the section's room is small),
    and what kind of cycle this is (0.35.0: the marketing share retired; the plan tree's step decides a marketing
    cycle)."""
    spent, ventured = s.venture_day
    text = (
        f"Your owner gives ventures {s.venture_share}% of your spending: ${micros_to_usd(ventured):.2f} of today's"
        f" ${micros_to_usd(spent):.2f} so far."
    )
    if s.venture:
        return text + " This is a venture cycle."
    if s.marketing:
        return text + " This is a marketing cycle."
    if s.marketing_apart:
        return text + " Pins, Bluesky posts and blog posts belong to marketing cycles."
    return text


def flat(text: Any) -> str:
    """The agent's text on one line (0.12.0: a text over several lines could pose as a section of its own)."""
    return " ".join(str(text or "").split())


def project_lines(s: Snapshot) -> str:
    """The open projects: the ones updated last in full (SHOWN_PROJECTS); 0.19.1: with more open (there is no limit
    any more), a first line names the others, so no open project drops out of the plan when the section is cut."""
    if not s.projects:
        return "No open projects."
    lines = []
    others = s.projects[SHOWN_PROJECTS:]
    if others:
        named = ", ".join(f"#{p['id']} {flat(p['title'])[:40]} [{p['status']}]" for p in others)
        lines.append(
            f"{len(s.projects)} open projects: the {SHOWN_PROJECTS} you updated last in full below (the ones "
            f"waiting while your owner parks their venture come last); also open: {named}."
        )
    for p in s.projects[:SHOWN_PROJECTS]:
        spent, earned = s.project_money.get(p["id"], (0, 0))
        lines.append(  # 0.35.0: its next steps are YOUR PLAN's
            f"#{p['id']} [{p['status']}] {flat(p['title'])} · spent ${micros_to_usd(spent):.2f} · earned "
            f"${micros_to_usd(earned):.2f}"
        )
        lines.append(f"   hypothesis: {flat(p['hypothesis'])}")
        if p["id"] in s.project_funnels:  # 0.18.0
            lines.append(f"   {s.project_funnels[p['id']]}")
        lines += [f"   {flat(b)}" for b in s.project_bets.get(p["id"], [])]
    return "\n".join(lines)


def _news_head(s: Snapshot) -> str:
    lines = []
    if s.last_cycle is not None:
        c = s.last_cycle
        lines.append(
            f"Last cycle #{c['id']} ended {c['status']}" + (f" ({flat(c['note'])})" if c["note"] else "") + "."
        )
    return "\n".join(lines)


def last_cycle_text(s: Snapshot, budget: int = PLANNER_BUDGETS["journal"]) -> str:
    """0.12.0: the plan's YOUR LAST CYCLE: the handoff its reflection left for this cycle and its journal's summary
    (the agent's words, JSON-quoted; the handoff never reached a plan before), then the digests Ember's code wrote of
    the last two cycles (what they did and didn't do: a journal can claim work that never happened). Without a
    digest (a cycle from before 0.12.0), the last cycle's goal.

    0.15.0: after a cycle that left no handoff (stopped, failed, killed or idle), the last handoff the agent wrote,
    with its cycle. No journal line for a journal Ember's code wrote: its digest says more. Within ``budget`` bytes,
    each digest is cut to its own share: the section's cut took the older one down to its goal."""
    lines = []
    journal = s.last_journal
    written = journal is not None and _author(journal) != "system"
    if written and journal["handoff"]:
        cycle = journal["cycle_id"] if "cycle_id" in journal.keys() else None  # noqa: SIM118 - a Row's "in" sees values
        lines.append(
            f"Your handoff to this cycle{_written_in(s, cycle)}: " + json.dumps(journal["handoff"], ensure_ascii=False)
        )
    elif s.handoff is not None:
        lines.append(
            f"Your last handoff, from cycle #{s.handoff['cycle_id']}{_written_in(s, s.handoff['cycle_id'])} (the"
            " cycles after it left none): " + json.dumps(s.handoff["handoff"], ensure_ascii=False)
        )
    goal = _plan_goal(s.last_cycle)
    if goal and not s.digests:
        lines.append(f"Its goal: {json.dumps(goal, ensure_ascii=False)}")
    if written:
        lines.append(f"Its journal: {json.dumps(journal['summary'], ensure_ascii=False)}")
    if s.digests:
        lines.append("What your last cycles did, from Ember's records (newest first):")
        digests = s.digests
        room = budget - json_bytes("\n".join(lines))  # its quotes' 2 bytes hold a digest's line break
        sizes = [json_bytes(d) for d in digests]
        if sum(sizes) > room:
            digests = [
                d if size <= share else cut(d, share)
                for d, size, share in zip(digests, sizes, _shares(sizes, room), strict=True)
            ]
        lines += digests
    return "\n".join(lines)


def _written_in(s: Snapshot, cycle_id: Any) -> str:
    """0.30.0: where a handoff was written, as YOUR LAST CYCLE says it (a line's next step waits for that line's cycle;
    READY shows each line's own)."""
    where = s.handoff_from.get(int(cycle_id)) if isinstance(cycle_id, int) else None
    return f" (written in {where})" if where else ""


def _author(journal: Mapping[str, Any]) -> str:
    return str(journal["author"]) if "author" in journal.keys() else "agent"  # noqa: SIM118 - a Row's "in" sees values


def _shares(sizes: list[int], room: int) -> list[int]:
    """0.15.0: ``room`` shared out: the smallest first, each at most its size and an equal part of what is left."""
    shares = [0] * len(sizes)
    left = room
    for n, i in enumerate(sorted(range(len(sizes)), key=lambda i: sizes[i])):
        shares[i] = min(sizes[i], left // (len(sizes) - n))
        left -= shares[i]
    return shares


def _plan_goal(cycle: Mapping[str, Any] | None) -> str:
    raw = cycle["plan"] if cycle is not None and "plan" in cycle.keys() else None  # noqa: SIM118 - a Row's "in" sees values
    try:
        plan = json.loads(raw) if raw else {}
    except ValueError:
        return ""
    goal = plan.get("goal") if isinstance(plan, dict) else None
    return " ".join(goal.split())[:300] if isinstance(goal, str) else ""


def owner_text(s: Snapshot, budget: int) -> str:
    """What the owner wrote or decided since the last plan, messages first, in ``budget`` bytes.

    The planner, the brief and the will see the same lines. When they don't all fit, the oldest messages stay
    whole as far as they can, and the texts quoted in the others and in decisions and upgrade notes (these at most
    QUOTE_CAP characters) are cut to the same length, the longest that fits, each saying how much was left out. A
    shortened message stays news for the next cycle, when it comes first. If not even the oldest message fits whole
    beside SHORTEST_QUOTE characters of each other text, it keeps the room (whole, or as much of it as the section
    can hold) and the last lines are cut.
    """
    return _owner(s, budget)[0]


def _owner(s: Snapshot, budget: int) -> tuple[str, list[tuple[Item, str, bool]], Item | None]:
    """``owner_text``; every item with its line as that text holds it whole (unless it was cut) and whether the line
    shows it whole (a decision always, a message unshortened); and the oldest message if it was shortened only
    because the section can't hold it whole."""
    decisions = [*s.news.approval_lines(), *s.news.upgrade_lines(), *s.news.venture_lines()]
    items: list[Item] = [*(("message", m["id"], None) for m in s.owner_messages), *s.news.items()]
    texts = [m["text"] for m in s.owner_messages]

    def message(i: int, chars: int | None) -> str:
        m = s.owner_messages[i]
        try:
            shown = m["seen_cycle_id"] is not None
        except (IndexError, KeyError):  # a row built by hand (in tests) may lack it
            shown = False
        waiting = ", not answered yet" if shown else ""  # shown before, and still waiting for an answer (0.9.1)
        return f"Message #{m['id']} from your owner ({m['created_at']}{waiting}): {_quote(texts[i], chars)}"

    def shortened(limits: list[int | None], chars: int | None) -> str:
        """The first messages cut to their ``limits`` (None: whole), the others and the decisions to ``chars``."""
        messages = [message(i, n) for i, n in enumerate([*limits, *[chars] * (len(texts) - len(limits))])]
        if chars is None:
            return "\n".join([*messages, *decisions])
        return "\n".join([*messages, *(_shorten(line, min(chars, QUOTE_CAP)) for line in decisions)])

    def fits(limits: list[int | None], chars: int | None) -> bool:
        return json_bytes(shortened(limits, chars)) <= budget

    limits: list[int | None] = []
    chars: int | None = None
    too_long: Item | None = None
    if not fits(limits, chars):
        whole = _largest(0, len(texts), lambda k: fits([None] * k, SHORTEST_QUOTE))
        limits = [None] * whole
        if whole or not texts:  # the oldest messages whole, the others share what is left
            longest = max([QUOTE_CAP, *map(len, texts)])
            chars = _largest(SHORTEST_QUOTE, longest, lambda n: fits(limits, n))
        else:  # the oldest message keeps the room, as much of it as the section holds; the last lines are cut

            def kept(n: int | None) -> bool:
                return _holds(cut(shortened([n], SHORTEST_QUOTE), budget), message(0, n))

            limits = [None if kept(None) else _largest(SHORTEST_QUOTE, len(texts[0]), kept)]
            chars, too_long = SHORTEST_QUOTE, None if limits[0] is None else items[0]
    text = shortened(limits, chars)
    limits += [chars] * (len(texts) - len(limits))
    whole = [n is None or len(t) <= n for t, n in zip(texts, limits, strict=True)] + [True] * len(decisions)
    lines = list(zip(items, text.split("\n"), whole, strict=True)) if items else []
    return cut(text, budget), lines, too_long


def _holds(text: str, line: str) -> bool:
    """Whether ``text`` begins with ``line`` whole."""
    return text == line or text.startswith(line + "\n")


def _held(text: str, before: str, lines: list[tuple[Item, str, bool]]) -> dict[Item, bool]:
    """The items whose lines ``text`` holds whole right after ``before`` (a cut only takes lines from the end), each
    with whether its line showed it whole."""
    held = {}
    for item, line, whole in lines:
        before += line
        if not _holds(text, before):
            break
        held[item] = whole
        before += "\n"
    return held


def _quote(text: str, chars: int | None = None) -> str:
    """``text`` JSON-quoted (so it can't pose as a heading), cut to ``chars`` characters with a note.

    A cut text keeps its start and its end (where a question often is): the note on what is left out goes between.
    """
    if chars is None or len(text) <= chars:
        return json.dumps(text, ensure_ascii=False)
    tail = chars * 2 // 5
    start = json.dumps(text[: chars - tail] + "…", ensure_ascii=False)
    end = json.dumps("…" + text[len(text) - tail :], ensure_ascii=False)
    return f"{start} ({len(text) - chars:,} more characters; your owner has the full text) {end}"


def _shorten(line: str, chars: int) -> str:
    """A line of news with every text quoted in it cut to ``chars`` characters."""

    def one(match: re.Match[str]) -> str:
        try:
            return _quote(json.loads(match[0]), chars)
        except ValueError:  # not a quoted text after all: leave it
            return match[0]

    return _QUOTED.sub(one, line)


def owner_section(s: Snapshot) -> list[tuple[str, str]]:
    """The FROM YOUR OWNER section, or nothing when the owner has been quiet."""
    text = owner_text(s, OWNER_BUDGET)
    return [("FROM YOUR OWNER", text)] if text else []


def research_text(s: Snapshot) -> str:
    """The agent's last research, newest first: the cycle, the question and how the digest began."""
    lines = []
    for r in s.research:
        try:
            question = json.loads(r["input"])["question"]
        except (ValueError, TypeError, KeyError):
            continue
        found = _DIGEST.search(r["result"] or "")
        if not isinstance(question, str) or found is None:
            continue
        question, digest = (_start(" ".join(t.split())) for t in (question, found[1]))
        lines.append(f"Cycle #{r['cycle_id']}: {_quote(question)} → {_quote(digest)}")
    return "\n".join(lines)


def _start(text: str, chars: int = RESEARCH_CHARS) -> str:
    return text if len(text) <= chars else text[:chars] + "…"


def mail_text(s: Snapshot) -> str:
    """The MAIL section: the address and the newest unread emails (senders and subjects are quoted: they are data)."""
    if s.mail is None:
        return ""
    m = s.mail
    head = f"Your address: {m.address}. " + (f"{m.unread} unread; newest:" if m.unread else "No unread email.")
    lines = [
        f"#{i} from {_quote(_start(sender, 80))} {_quote(_start(subject, 100))}" for i, sender, subject in m.newest
    ]
    return "\n".join([head, *lines])


def mail_section(s: Snapshot) -> list[tuple[str, str]]:
    return [("MAIL", cut(mail_text(s), MAIL_BUDGET))] if s.mail is not None else []


def instructions_text(s: Snapshot, budget: int = INSTRUCTIONS_BUDGET) -> str:
    """The owner's standing instructions, JSON-quoted (so they can't pose as a heading), in ``budget`` bytes; empty
    while there are none. Instructions that don't fit (many multibyte characters, or a smaller planner) keep their
    start and their end, like a shortened message."""
    text = s.instructions.strip()
    if not text:
        return ""
    if json_bytes(_quote(text)) <= budget:
        return _quote(text)
    chars = _largest(SHORTEST_QUOTE, len(text), lambda n: json_bytes(_quote(text, n)) <= budget)
    return cut(_quote(text, chars), budget)


def instructions_section(s: Snapshot, budget: int = INSTRUCTIONS_BUDGET) -> list[tuple[str, str]]:
    text = instructions_text(s, budget)
    return [(INSTRUCTIONS_HEADING, text)] if text else []


def lessons_text(s: Snapshot, budget: int, pins_budget: int = PINS_BUDGET) -> str:
    """LESSONS: the lessons the owner pinned first (0.12.0, on top of the budget), then the newest others that fit
    (0.12.0: without the memory checks that asked for blind rewrites)."""
    pinned = {lesson_key(p) for p in s.pins}
    others = "\n".join(line for line in s.memory.get("lessons", "").splitlines() if lesson_key(line) not in pinned)
    # 0.18.0: the playbook first (at most PLAYBOOK_SHARE of the budget), then the newest lessons in what is left
    playbook = learning.playbook_text(s.playbook, int(budget * PLAYBOOK_SHARE))
    room = budget - json_bytes(playbook) - 2 if playbook else budget
    newest = cut(_newest_lines(others, room), room) if room > 0 else ""
    pinned_text = pins_text(s, pins_budget)
    return "\n\n".join(part for part in (pinned_text, playbook, newest) if part)


def pins_text(s: Snapshot, budget: int = PINS_BUDGET) -> str:
    """The lessons the owner pinned, as LESSONS shows them first (0.12.0); empty without any."""
    return f"{PINS_HEADING}\n{cut(chr(10).join(f'- {p}' for p in s.pins), budget)}" if s.pins else ""


def workshop_text(s: Snapshot) -> str:
    """For the planner only: the workshop scripts that proved useful, each with a request to have it built in."""
    return "\n".join(
        f"Workshop check: {path} has proven itself ({why}). Plan a step to ask for it to be built into Ember: "
        f"request_upgrade with workshop_script '{path}' (built in, it costs nothing to run and never breaks)."
        for path, why in s.proven
    )


def _sections(parts: list[tuple[str, str]]) -> str:
    """The sections under their headings. Only a heading begins a line with "=": a line of a section that does (0.12.0:
    in a memory file from before, or one changed outside Ember) is shown JSON-quoted, so it can't pose as one."""
    return "\n\n".join(f"== {title} ==\n{_unheaded(body)}" for title, body in parts)


def _unheaded(body: str) -> str:
    pieces = _LINE_BREAK.split(body)  # the lines, with the line breaks between them
    for i in range(0, len(pieces), 2):
        if heading_like(pieces[i]):
            pieces[i] = json.dumps(pieces[i], ensure_ascii=False)
    return "".join(pieces)


def open_upgrades(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """0.15.0: the agent's upgrade requests the owner hasn't released or declined, newest first (live: it paid to
    test whether upgrade #3 was in, as it couldn't see its status)."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT id, status, created_at, decided_at FROM upgrades WHERE {where} AND status IN ('new', 'accepted')"
        f" ORDER BY id DESC LIMIT {OPEN_UPGRADES}",
        params,
    ).fetchall()


def upgrades_line(rows: list[sqlite3.Row]) -> str:
    shown = ", ".join(f"#{r['id']} {r['status']} ({(r['decided_at'] or r['created_at'])[:10]})" for r in rows)
    return f"Your upgrade requests not built in yet: {shown}."


def _planner_texts(s: Snapshot, dry_run: bool, journal: int = PLANNER_BUDGETS["journal"]) -> dict[str, str]:
    """The planner's sections that are cut at a line boundary, uncut, by budget ("": no such section this time;
    YOUR LAST CYCLE shares its ``journal`` budget out among its digests, last_cycle_text)."""
    pending = (
        "\n".join(
            [
                *(
                    f"#{r['id']} {r['type']}: {flat(r['title'])} (expires {store.expires_at(r)[:10]})"
                    for r in s.pending
                ),
                *([upgrades_line(s.upgrades)] if s.upgrades else []),
            ]
        )
        or "None."
    )
    if s.pending and s.decision_wakes:  # first, so a cut never takes it (0.12.0: it slept 12 hours for a decision)
        pending = f"{WAITING_NOTE}\n{pending}"
    return {
        "status": status_text(s, dry_run),
        "journal": last_cycle_text(s, journal),
        "review": s.review,
        "plan": s.plan,  # 0.35.0: in place of the ROADMAP
        "projects": project_lines(s),
        "ready": s.ready,  # a venture plan's READY, 0.35.0: or an ordinary or marketing plan's YOUR STEP
        "ventures": ventures.planner_lines(s.ventures, s.venture_money, s.venture),
        "pending": pending,
        "mail": mail_text(s),
        "etsy": s.etsy,
        "pinterest": s.pinterest,
        "bluesky": s.bluesky,
        "printify": s.printify,
        "website": s.website,
        "blog": s.blog,
        "kdp": s.kdp,
        "strategy": s.memory.get("strategy", ""),
        "identity": s.memory.get("identity", ""),
        "workspace": "\n".join(s.workspace) or "Empty.",
        "research": research_text(s),
        "workshop": workshop_text(s),
        "library": library.planner_text(s.library) if s.library else "",
    }


def _allot(texts: dict[str, str], budgets: dict[str, int], used: int) -> dict[str, str]:
    """0.15.0: each text cut to its budget; then what is left of all the budgets (``used``: the bytes of the sections
    cut on their own) widens the cut ones, in SPARE_ORDER. The sections never hold more than the budgets' sum."""
    shown = {key: cut(text, budgets[key]) for key, text in texts.items()}
    spare = sum(budgets.values()) - used - sum(json_bytes(text) for text in shown.values() if text)
    for key in (key for key in SPARE_ORDER if key in texts):
        if spare <= 0:
            break
        wider = cut(texts[key], json_bytes(shown[key]) + spare)
        if json_bytes(wider) > json_bytes(shown[key]):  # it was cut, and the spare room holds more of it
            spare -= json_bytes(wider) - json_bytes(shown[key])
            shown[key] = wider
    return shown


def planner_context(s: Snapshot, dry_run: bool, scale: float = 1.0) -> tuple[str, Shown]:
    """The planner's context, and what of the owner's news and of the changelog it lists and shows whole."""
    b = {k: int(v * scale) for k, v in PLANNER_BUDGETS.items()}
    b["plan"] = max(b["plan"], PLAN_FLOOR)
    head = _news_head(s)
    owner, lines, _ = _owner(s, b["news"] - json_bytes(head))
    events = "\n".join(agenda_line(r) for r in s.agenda)  # 0.13.0: after the owner's news, cut first
    since = cut("\n".join(part for part in (head, owner, events) if part) or "Nothing new.", b["news"])
    software = cut(s.news.changelog, b["software"])
    standing = instructions_section(s, b["instructions"])
    lessons = lessons_text(s, b["lessons"], int(PINS_BUDGET * scale))
    # The sections cut on their own budget (the owner's, the changelog and the lessons, whose pins come on top).
    own = [since, software, *(text for _, text in standing)]
    used = sum(map(json_bytes, own)) + min(json_bytes(lessons), b["lessons"])
    t = _allot(_planner_texts(s, dry_run, b["journal"]), b, used)
    # 0.28.0: while marketing cycles run, an ordinary plan leaves the marketing channels (and their tools) to them
    elsewhere = s.marketing_apart and not (s.marketing or s.venture or s.reactive)
    parts = [
        ("STATUS", t["status"]),
        *([(obligations.HEADING, s.obligations)] if s.obligations else []),  # 0.12.0: first, never cut
        *standing,
        ("SINCE YOUR LAST WAKE", since),
        *([("YOUR LAST CYCLE", t["journal"])] if t["journal"] else []),
        *([("YOUR SOFTWARE", software)] if s.news.changelog else []),
        *([("TODAY'S REVIEW", t["review"])] if t["review"] else []),
        (PLAN_HEADING, t["plan"]),  # 0.35.0: in place of the ROADMAP
        ("OPEN PROJECTS", t["projects"]),
        # 0.13.0: the decision desk's READY; 0.35.0: or the step the plan tree took (YOUR STEP)
        *([(READY_HEADING if s.venture else STEP_HEADING, t["ready"])] if t["ready"] else []),
        ("VENTURES", t["ventures"]),
        ("WAITING FOR YOUR OWNER", t["pending"]),
        *([("MAIL", t["mail"])] if s.mail is not None else []),
        *([("ETSY SHOP", t["etsy"])] if t["etsy"] else []),
        *([("PINTEREST", t["pinterest"])] if t["pinterest"] and not elsewhere else []),
        *([("BLUESKY", t["bluesky"])] if t["bluesky"] and not elsewhere else []),
        *([("PRINTIFY", t["printify"])] if t["printify"] else []),
        *([("WEBSITE", t["website"])] if t["website"] else []),
        *([("BLOG", t["blog"])] if t["blog"] and not elsewhere else []),
        *([("KDP", t["kdp"])] if t["kdp"] else []),
        (STRATEGY_HEADING, t["strategy"]),
        (IDENTITY_HEADING, t["identity"]),
        (LESSONS_HEADING, lessons),
        ("WORKSPACE", t["workspace"]),
        *([(RESEARCH_HEADING, t["research"])] if t["research"] else []),
        *([("WORKSHOP", t["workshop"])] if s.proven else []),
        *([("YOUR OWNER'S LIBRARY", t["library"])] if s.library else []),
        ("TASK", _task(s)),
    ]
    held = _held(since, f"{head}\n" if head else "", lines)
    whole = frozenset(item for item, shown_whole in held.items() if shown_whole)
    return _sections(parts), Shown(whole, bool(software) and software == s.news.changelog, frozenset(held))


def _task(s: Snapshot) -> str:
    if s.reactive:  # 0.13.0: an event woke it
        return (
            f"Plan this reactive cycle: an event woke you (Agenda in SINCE YOUR LAST WAKE). React to it first, in at "
            f"most {REACTIVE_STEPS} steps. Reply with the JSON plan only."
        )
    kind = "venture" if s.venture else "marketing" if s.marketing else "wake"  # 0.28.0: a marketing cycle's
    return f"Plan this {kind} cycle. Reply with the JSON plan only."


def brief(
    s: Snapshot,
    dry_run: bool,
    plan: dict[str, Any],
    focus: sqlite3.Row | None,
    max_steps: int,
    venture_focus: str = "",
    milestone_focus: str = "",
    knowledge: str = "",
    set_aside: str = "",
    line: int | None = None,
    line_focus: str = "",
) -> tuple[str, Shown]:
    """The act phase's brief (the same for every step and the reflection: built from the cycle's snapshot only), and
    which of the owner's items it shows. ``venture_focus`` and ``milestone_focus``: the plan's venture and milestone
    as ``ventures.focus_text`` and ``roadmap.focus_text`` show them; ``knowledge``: the learnings from the owner's
    library that match the plan (0.12.0). ``set_aside`` (0.23.2): why the plan's focus project isn't the cycle's,
    first and in its own room (inside the venture's, it cut the venture's pitch). 0.28.0: ``line``, the product line a
    cycle on one line works on (another line's obligations wait for its own cycle), and ``line_focus``, what Ember's
    code says of it (lines.focus_text)."""
    focus_parts = [set_aside] if set_aside else []
    if line_focus:
        focus_parts.append(cut(line_focus, LINE_FOCUS_BUDGET))
    if milestone_focus:
        focus_parts.append(cut(milestone_focus, MILESTONE_FOCUS_BUDGET))
    if venture_focus:
        focus_parts.append(cut(venture_focus, VENTURE_FOCUS_BUDGET))
    if focus is not None:
        focus_parts.append(
            f"Focus project: #{focus['id']} {flat(focus['title'])} [{focus['status']}]\n"
            f"Hypothesis: {flat(focus['hypothesis'])}\nNotes: {flat(focus['notes'][-600:]) or '-'}"
        )
    focus_text = "\n\n".join(focus_parts) or "None."
    steps = "\n".join(f"{i}. {flat(step)}" for i, step in enumerate(plan.get("steps", []), 1))
    money = f"\nPath to money: {flat(plan['money_path'])}" if plan.get("money_path") else ""
    # 0.33.0: why the plan chose this (the work steps saw only its goal and steps: live, their answer to the owner on
    # "why do you scatter" said "not a token problem" right below a plan that blamed "$1 cycles and thin memory")
    why = f"Why: {flat(plan['assessment'])}\n" if plan.get("assessment") else ""
    head = [("STATUS", status_text(s, dry_run)), ("PLAN", f"{why}Goal: {flat(plan.get('goal'))}{money}\n{steps}")]
    standing = instructions_section(s)
    owner, lines, too_long = _owner(s, OWNER_BUDGET)
    owners = [("FROM YOUR OWNER", owner)] if owner else []
    mailed = mail_section(s)
    research = cut(research_text(s), RESEARCH_BUDGET)
    researched = [(RESEARCH_HEADING, research)] if research else []
    learned = [(KNOWLEDGE_HEADING, cut(knowledge, KNOWLEDGE_BUDGET))] if knowledge else []
    owing = obligations.for_line(s.obligations, line) if line is not None else s.obligations  # 0.28.0
    owed = [(obligations.HEADING, cut(owing, OBLIGATIONS_BRIEF_BUDGET))] if s.obligations else []
    parts = [
        *head,
        *standing,
        *owners,
        *mailed,
        *owed,  # 0.12.0: what the agent owes, with the numbers obligation_done closes
        *([("VENTURE CYCLE", _venture_brief(s))] if s.venture else []),
        *([("MARKETING CYCLE", MARKETING_BRIEF)] if s.marketing else []),  # 0.28.0
        *learned,  # before the FOCUS: a brief over its budget loses its end, and this section's room is its own
        ("FOCUS", focus_text),
        # 0.33.0: the strategy the plan was made by (the work steps saw none unless they read it)
        *([(STRATEGY_HEADING, cut(strategy, STRATEGY_BRIEF_BUDGET))] if (strategy := s.memory.get("strategy")) else []),
        (LESSONS_HEADING, lessons_text(s, 800)),
        ("WORKSPACE", "\n".join(s.workspace[:20]) or "Empty."),
        *researched,
        (
            "LIMITS",
            f"At most {max_steps} steps this cycle and {tools.MAX_TOOL_CALLS_PER_TURN} tool calls per step. Stop when "
            "the goal is reached.",
        ),
    ]
    on_top = [*owed, *standing, *owners, *mailed, *researched, *learned]
    room = sum(json_bytes(f"\n\n== {title} ==\n{body}") - 2 for title, body in on_top)  # - 2: its own JSON quotes
    room += json_bytes(f"{pins_text(s)}\n\n") - 2 if s.pins else 0  # 0.12.0: the owner's pins are on top too
    text = cut(_sections(parts), BRIEF_BUDGET + room)
    held = _held(text, _sections([*head, *standing]) + "\n\n== FROM YOUR OWNER ==\n", lines)
    # A message longer than the brief can ever hold is shown in full as far as it can be.
    full = frozenset(item for item, shown_whole in held.items() if shown_whole or item == too_long)
    return text, Shown(full, listed=frozenset(held))


def _venture_brief(s: Snapshot) -> str:
    """The brief's VENTURE CYCLE: without brainstorms outside explore (0.15.0: the focus mode's brief still asked for
    one, and the tool refused it)."""
    return VENTURE_BRIEF if s.brainstorm else VENTURE_BRIEF.replace(BRAINSTORM_BRIEF, "")


def will_context(s: Snapshot, dry_run: bool) -> str:
    closed = "\n".join(f"- {flat(j['summary'])}" for j in reversed(s.journal)) or "No journal yet."
    parts = [
        ("STATUS", status_text(s, dry_run)),
        *owner_section(s),
        ("PROJECTS", project_lines(s)),
        ("RECENT JOURNAL", closed),
        (LESSONS_HEADING, lessons_text(s, 1_200)),
        (STRATEGY_HEADING, s.memory.get("strategy", "")[:800]),
        ("TASK", "Write your last will now."),
    ]
    return cut(_sections(parts), WILL_BUDGET)


def _newest_lines(text: str, budget: int) -> str:
    """The newest lines of a file that fit the budget (lessons are appended at the end)."""
    lines = [line for line in text.splitlines() if line.strip()]
    kept: list[str] = []
    for line in reversed(lines):
        if json_bytes("\n".join([line, *kept])) > budget:
            break
        kept.insert(0, line)
    return "\n".join(kept)
