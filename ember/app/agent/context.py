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
from datetime import date, timedelta
from typing import Any

from ..economy.costs import micros_to_usd
from ..economy.life import LifeStatus
from ..economy.metering import rough_token_count
from ..integrations import mailstore
from . import digest, library, review, roadmap, store, ventures
from .memory import Memory, heading_like
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
    "status": 700,  # (0.10.1: with a venture cycle's research room)
    "instructions": INSTRUCTIONS_BUDGET,
    "news": 2_300,
    "software": CHANGELOG_LIMIT,
    "projects": 2_000,
    "pending": 500,  # 0.12.0: with the note that the owner's decision wakes the agent
    "mail": MAIL_BUDGET,
    "strategy": 2_000,
    "identity": 600,
    "lessons": 1_300,
    "journal": 1_700,  # YOUR LAST CYCLE (0.12.0: the handoff, the last journal and the last 2 cycles' digests)
    "workspace": 900,
    "research": RESEARCH_BUDGET,
    "workshop": 800,
    "review": 1_400,
    "etsy": 1_600,
    "ventures": 2_600,
    "roadmap": 1_800,  # 0.11.0 (and never less than ROADMAP_FLOOR, whatever the scale: 0.12.0)
    "library": 1_200,  # 0.12.0: the owner's library, when it holds documents
}
# 0.12.0: the ROADMAP isn't scaled down with the other sections (its checks and goals come first, and the cut took
# every goal once the budget shrank).
ROADMAP_FLOOR = 1_800
# 0.12.0: while requests wait for the owner, the plan is told that waiting isn't its job.
WAITING_NOTE = "Your owner's decision on these wakes you: don't wait for it, work on something else meanwhile."
# The owner's decisions and messages in the brief and the will context, as much as the planner's news share:
# room for one whole message of plain text at the owner's limit of 2,000 characters.
OWNER_BUDGET = 2_300
QUOTE_CAP = 300  # characters of each text quoted in a decision or upgrade line, when the owner's news is shortened
SHORTEST_QUOTE = 40  # no quoted text is shortened below this; if that isn't enough, the last lines are cut
# The owner's (standing instructions and news), the mail and the research sections (and their headings) come on top
# of the brief's budget, so they never squeeze the rest.
# (6,500 since 0.10.0: a venture's focus is longer. A milestone's (0.11.0) is short: a brief with a venture's, a
# project's and a milestone's focus at their longest loses its end, as a brief over its budget always does.)
BRIEF_BUDGET = 6_500
# 0.12.0: the learnings from the owner's library that match the plan (Ember's code picks them), on top of the brief.
KNOWLEDGE_HEADING = "WHAT YOU LEARNED (from your owner's library)"
KNOWLEDGE_BUDGET = 1_800
VENTURE_FOCUS_BUDGET = 1_900  # a venture's FOCUS in the brief
MILESTONE_FOCUS_BUDGET = 1_100  # a milestone's FOCUS in the brief (0.11.0; 0.12.0: with its last cycle's digest)
VENTURE_BRIEF = (
    "This is a venture cycle: read guide 'ventures' first, research as often as this cycle can pay for (STATUS), "
    "grow the tree with brainstorm (first, if you plan one), save what you learn with venture_update (learned, with "
    "sources) and rescore the venture from the evidence."
)
# The largest brief, those sections and their headings included: the WORK and REFLECT profiles are measured on it.
BRIEF_MAX = BRIEF_BUDGET + INSTRUCTIONS_BUDGET + OWNER_BUDGET + MAIL_BUDGET + RESEARCH_BUDGET + KNOWLEDGE_BUDGET + 260
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
    cycle_cap: float
    projects: list[sqlite3.Row] = field(default_factory=list)
    project_money: dict[int, tuple[int, int]] = field(default_factory=dict)
    owner_messages: list[sqlite3.Row] = field(default_factory=list)
    pending: list[sqlite3.Row] = field(default_factory=list)
    last_cycle: sqlite3.Row | None = None
    last_journal: sqlite3.Row | None = None
    digests: list[str] = field(default_factory=list)  # the last cycles' digests, newest first (0.12.0)
    memory: dict[str, str] = field(default_factory=dict)
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
    ventures: list[sqlite3.Row] = field(default_factory=list)  # the venture tree (0.10.0)
    venture_money: dict[int, ventures.Money] = field(default_factory=dict)
    venture: bool = False  # a venture cycle
    venture_share: int = 0  # the owner's share of the spending for ventures, in percent
    venture_day: tuple[int, int] = (0, 0)  # today's spending, and the venture cycles' part of it
    call_costs: dict[str, int] = field(default_factory=dict)  # what research and brainstorms cost lately (0.10.1)
    today: date | None = None  # the owner's local date (the roadmap's horizons are counted from it)
    roadmap: list[sqlite3.Row] = field(default_factory=list)  # the open milestones, the first due first (0.11.0)
    roadmap_closed: list[sqlite3.Row] = field(default_factory=list)  # closed in the last roadmap.CLOSED_DAYS days
    roadmap_spent: dict[int, int] = field(default_factory=dict)  # what each milestone's work cost (0.12.0)
    library: library.Shelf | None = None  # the owner's library (0.12.0): None while it is empty
    decision_wakes: bool = False  # the owner's decisions wake the agent (0.12.0, the wake_on_decision option)


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
    news: News | None = None,
    mail_address: str | None = None,
    today: date | None = None,
    etsy: str = "",
    venture: bool = False,
    venture_share: int = 0,
    shelf: library.Shelf | None = None,
    decision_wakes: bool = False,
) -> Snapshot:
    """What the planner, the brief and the will see; ``today`` (the owner's local date) finds the day's review and
    the day's spending on ventures."""
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
    files = _safe_listing(workspace)
    standing = store.standing_instructions(conn, scope)
    return Snapshot(
        status=status,
        local_time=local_time,
        version=version,
        agent_name=agent_name,
        today_spend=today_spend,
        daily_cap=daily_cap,
        cycle_cap=cycle_cap,
        projects=projects,
        project_money=money,
        owner_messages=store.open_messages(conn, scope, 8),
        pending=[r for r in store.queue(conn, "approvals", scope, 20) if r["status"] == "pending"],
        last_cycle=last_cycle,
        last_journal=journal[0] if journal else None,
        digests=digest.latest(conn, scope),
        memory=memory.read_all(),
        workspace=files,
        workspace_usage=_usage_line(workspace),
        journal=journal,
        news=news or News(),
        research=store.recent_research(conn, scope, RESEARCH_CALLS),
        mail=mail,
        instructions=standing["text"] if standing else "",
        proven=store.proven_scripts(conn, scope),
        review=review.planner_text(conn, todays_review) if todays_review is not None else "",
        etsy=etsy,
        ventures=ventures.all_ventures(conn, scope),
        venture_money=ventures.money(conn, scope),
        venture=venture,
        venture_share=venture_share,
        venture_day=ventures.day_spend(conn, scope, today) if today is not None else (0, 0),
        call_costs=ventures.call_costs(conn, scope) if venture else {},
        today=today,
        roadmap=roadmap.open_milestones(conn, scope),
        roadmap_closed=_closed_lately(conn, scope, today),
        roadmap_spent={mid: cost for mid, (_, cost) in roadmap.effort(conn, scope).items()},
        library=shelf,
        decision_wakes=decision_wakes,
    )


def _closed_lately(conn: sqlite3.Connection, scope: AgentScope, today: date | None) -> list[sqlite3.Row]:
    if today is None:
        return []
    return roadmap.closed_since(conn, scope, (today - timedelta(days=roadmap.CLOSED_DAYS)).isoformat())


def roadmap_text(s: Snapshot) -> str:
    """The planner's ROADMAP (0.11.0), counted from the owner's today; what Ember's code closed since the last cycle
    ended is a check of its own (0.12.0)."""
    since = dict(s.last_cycle).get("ended_at") if s.last_cycle is not None else None
    return roadmap.planner_text(s.roadmap, s.roadmap_closed, s.today or date.today(), since, s.roadmap_spent)


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
        f" This cycle may spend up to ${s.cycle_cap:.2f}.",
    ]
    if s.workspace_usage:
        lines.append(s.workspace_usage)
    if s.venture_share:
        spent, ventured = s.venture_day
        lines.append(
            f"Your owner gives ventures {s.venture_share}% of your spending: ${micros_to_usd(ventured):.2f} of today's"
            f" ${micros_to_usd(spent):.2f} so far." + (" This is a venture cycle." if s.venture else "")
        )
        if s.venture:
            lines.append(ventures.room_text(s.cycle_cap, s.call_costs))
    if st.last_will_due:
        lines.append("Your money is nearly gone: your last will is due.")
    return "\n".join(lines)


def flat(text: Any) -> str:
    """The agent's text on one line (0.12.0: a text over several lines could pose as a section of its own)."""
    return " ".join(str(text or "").split())


def project_lines(s: Snapshot) -> str:
    if not s.projects:
        return "No open projects."
    lines = []
    for p in s.projects[:8]:
        spent, earned = s.project_money.get(p["id"], (0, 0))
        lines.append(
            f"#{p['id']} [{p['status']}] {flat(p['title'])} · next: {flat(p['next_step']) or '-'} · spent "
            f"${micros_to_usd(spent):.2f} · earned ${micros_to_usd(earned):.2f}"
        )
        lines.append(f"   hypothesis: {flat(p['hypothesis'])}")
    return "\n".join(lines)


def _news_head(s: Snapshot) -> str:
    lines = []
    if s.last_cycle is not None:
        c = s.last_cycle
        lines.append(
            f"Last cycle #{c['id']} ended {c['status']}" + (f" ({flat(c['note'])})" if c["note"] else "") + "."
        )
    return "\n".join(lines)


def last_cycle_text(s: Snapshot) -> str:
    """0.12.0: the plan's YOUR LAST CYCLE: the handoff its reflection left for this cycle and its journal's summary
    (the agent's words, JSON-quoted; the handoff never reached a plan before), then the digests Ember's code wrote of
    the last two cycles (what they did and didn't do: a journal can claim work that never happened). Without a
    digest (a cycle from before 0.12.0), the last cycle's goal."""
    lines = []
    journal = s.last_journal
    if journal is not None and journal["handoff"]:
        lines.append(f"Your handoff to this cycle: {json.dumps(journal['handoff'], ensure_ascii=False)}")
    goal = _plan_goal(s.last_cycle)
    if goal and not s.digests:
        lines.append(f"Its goal: {json.dumps(goal, ensure_ascii=False)}")
    if journal is not None:
        lines.append(f"Its journal: {json.dumps(journal['summary'], ensure_ascii=False)}")
    if s.digests:
        lines.append("What your last cycles did, from Ember's records (newest first):")
        lines += s.digests
    return "\n".join(lines)


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


def _lessons(s: Snapshot, budget: int) -> str:
    """The planner's LESSONS: the newest lessons that fit (0.12.0: without the memory checks that asked for blind
    rewrites)."""
    return cut(_newest_lines(s.memory.get("lessons", ""), budget), budget)


def _strategy(s: Snapshot, budget: int) -> str:
    """The planner's STRATEGY: as much of it as fits."""
    return cut(s.memory.get("strategy", ""), budget)


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


def planner_context(s: Snapshot, dry_run: bool, scale: float = 1.0) -> tuple[str, Shown]:
    """The planner's context, and what of the owner's news and of the changelog it lists and shows whole."""
    b = {k: int(v * scale) for k, v in PLANNER_BUDGETS.items()}
    pending = (
        "\n".join(f"#{r['id']} {r['type']}: {flat(r['title'])} (expires {store.expires_at(r)[:10]})" for r in s.pending)
        or "None."
    )
    if s.pending and s.decision_wakes:  # first, so a cut never takes it (0.12.0: it slept 12 hours for a decision)
        pending = f"{WAITING_NOTE}\n{pending}"
    head = _news_head(s)
    owner, lines, _ = _owner(s, b["news"] - json_bytes(head))
    since = cut("\n".join(part for part in (head, owner) if part) or "Nothing new.", b["news"])
    software = cut(s.news.changelog, b["software"])
    research = research_text(s)
    last_cycle = last_cycle_text(s)
    parts = [
        ("STATUS", cut(status_text(s, dry_run), b["status"])),
        *instructions_section(s, b["instructions"]),
        ("SINCE YOUR LAST WAKE", since),
        *([("YOUR LAST CYCLE", cut(last_cycle, b["journal"]))] if last_cycle else []),
        *([("YOUR SOFTWARE", software)] if s.news.changelog else []),
        *([("TODAY'S REVIEW", cut(s.review, b["review"]))] if s.review else []),
        ("ROADMAP", cut(roadmap_text(s), max(b["roadmap"], ROADMAP_FLOOR))),
        ("OPEN PROJECTS", cut(project_lines(s), b["projects"])),
        ("VENTURES", cut(ventures.planner_lines(s.ventures, s.venture_money, s.venture), b["ventures"])),
        ("WAITING FOR YOUR OWNER", cut(pending, b["pending"])),
        *([("MAIL", cut(mail_text(s), b["mail"]))] if s.mail is not None else []),
        *([("ETSY SHOP", cut(s.etsy, b["etsy"]))] if s.etsy else []),
        (STRATEGY_HEADING, _strategy(s, b["strategy"])),
        (IDENTITY_HEADING, cut(s.memory.get("identity", ""), b["identity"])),
        (LESSONS_HEADING, _lessons(s, b["lessons"])),
        ("WORKSPACE", cut("\n".join(s.workspace) or "Empty.", b["workspace"])),
        *([(RESEARCH_HEADING, cut(research, b["research"]))] if research else []),
        *([("WORKSHOP", cut(workshop_text(s), b["workshop"]))] if s.proven else []),
        *([("YOUR OWNER'S LIBRARY", cut(library.planner_text(s.library), b["library"]))] if s.library else []),
        ("TASK", f"Plan this {'venture' if s.venture else 'wake'} cycle. Reply with the JSON plan only."),
    ]
    held = _held(since, f"{head}\n" if head else "", lines)
    whole = frozenset(item for item, shown_whole in held.items() if shown_whole)
    return _sections(parts), Shown(whole, bool(software) and software == s.news.changelog, frozenset(held))


def brief(
    s: Snapshot,
    dry_run: bool,
    plan: dict[str, Any],
    focus: sqlite3.Row | None,
    max_steps: int,
    venture_focus: str = "",
    milestone_focus: str = "",
    knowledge: str = "",
) -> tuple[str, Shown]:
    """The act phase's brief (the same for every step and the reflection: built from the cycle's snapshot only), and
    which of the owner's items it shows. ``venture_focus`` and ``milestone_focus``: the plan's venture and milestone
    as ``ventures.focus_text`` and ``roadmap.focus_text`` show them; ``knowledge``: the learnings from the owner's
    library that match the plan (0.12.0)."""
    focus_parts = [cut(milestone_focus, MILESTONE_FOCUS_BUDGET)] if milestone_focus else []
    if venture_focus:
        focus_parts.append(cut(venture_focus, VENTURE_FOCUS_BUDGET))
    if focus is not None:
        focus_parts.append(
            f"Focus project: #{focus['id']} {flat(focus['title'])} [{focus['status']}]\n"
            f"Hypothesis: {flat(focus['hypothesis'])}\nNext step: {flat(focus['next_step']) or '-'}\n"
            f"Notes: {flat(focus['notes'][-600:]) or '-'}"
        )
    focus_text = "\n\n".join(focus_parts) or "None."
    steps = "\n".join(f"{i}. {flat(step)}" for i, step in enumerate(plan.get("steps", []), 1))
    money = f"\nPath to money: {flat(plan['money_path'])}" if plan.get("money_path") else ""
    head = [("STATUS", status_text(s, dry_run)), ("PLAN", f"Goal: {flat(plan.get('goal'))}{money}\n{steps}")]
    standing = instructions_section(s)
    owner, lines, too_long = _owner(s, OWNER_BUDGET)
    owners = [("FROM YOUR OWNER", owner)] if owner else []
    mailed = mail_section(s)
    research = cut(research_text(s), RESEARCH_BUDGET)
    researched = [(RESEARCH_HEADING, research)] if research else []
    learned = [(KNOWLEDGE_HEADING, cut(knowledge, KNOWLEDGE_BUDGET))] if knowledge else []
    parts = [
        *head,
        *standing,
        *owners,
        *mailed,
        *([("VENTURE CYCLE", VENTURE_BRIEF)] if s.venture else []),
        *learned,  # before the FOCUS: a brief over its budget loses its end, and this section's room is its own
        ("FOCUS", focus_text),
        (LESSONS_HEADING, _newest_lines(s.memory.get("lessons", ""), 800)),
        ("WORKSPACE", "\n".join(s.workspace[:20]) or "Empty."),
        *researched,
        ("LIMITS", f"At most {max_steps} steps this cycle and 4 tool calls per step. Stop when the goal is reached."),
    ]
    on_top = [*standing, *owners, *mailed, *researched, *learned]
    room = sum(json_bytes(f"\n\n== {title} ==\n{body}") - 2 for title, body in on_top)  # - 2: its own JSON quotes
    text = cut(_sections(parts), BRIEF_BUDGET + room)
    held = _held(text, _sections([*head, *standing]) + "\n\n== FROM YOUR OWNER ==\n", lines)
    # A message longer than the brief can ever hold is shown in full as far as it can be.
    full = frozenset(item for item, shown_whole in held.items() if shown_whole or item == too_long)
    return text, Shown(full, listed=frozenset(held))


def will_context(s: Snapshot, dry_run: bool) -> str:
    closed = "\n".join(f"- {flat(j['summary'])}" for j in reversed(s.journal)) or "No journal yet."
    parts = [
        ("STATUS", status_text(s, dry_run)),
        *owner_section(s),
        ("PROJECTS", project_lines(s)),
        ("RECENT JOURNAL", closed),
        (LESSONS_HEADING, _newest_lines(s.memory.get("lessons", ""), 1_200)),
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
