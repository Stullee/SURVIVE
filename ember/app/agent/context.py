"""What the agent sees when it wakes up: a compact, byte-budgeted picture.

Each section has a budget in UTF-8 bytes (measured on the JSON-escaped text,
which is what the budget guard's token count sees). Sections are cut at a line
boundary and marked, so the model knows something is missing. If a request
still doesn't fit the call profile the economy reserves money for, the context
is rebuilt with smaller budgets; a request never silently exceeds its profile.
The planner's context and the brief also say which of the owner's items they
showed whole (``news.Shown``): only those can be marked seen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..economy.costs import micros_to_usd
from ..economy.life import LifeStatus
from ..economy.metering import rough_token_count
from . import store
from .memory import Memory
from .news import CHANGELOG_LIMIT, Item, News, Shown
from .sandbox import Jail
from .store import AgentScope

# The agent's last research calls (question and how the digest began), so it doesn't buy the same answer twice.
RESEARCH_BUDGET = 1_200
RESEARCH_CALLS = 5
RESEARCH_CHARS = 200  # of each question and digest
PLANNER_BUDGETS = {
    "status": 500,
    "news": 2_300,
    "software": CHANGELOG_LIMIT,
    "projects": 2_000,
    "pending": 400,
    "strategy": 2_000,
    "identity": 600,
    "lessons": 1_300,
    "journal": 600,
    "workspace": 400,
    "research": RESEARCH_BUDGET,
}
# The owner's decisions and messages in the brief and the will context, as much as the planner's news share:
# room for one whole message of plain text at the owner's limit of 2,000 characters.
OWNER_BUDGET = 2_300
QUOTE_CAP = 300  # characters of each text quoted in a decision or upgrade line, when the owner's news is shortened
SHORTEST_QUOTE = 40  # no quoted text is shortened below this; if that isn't enough, the last lines are cut
# The owner's and the research sections (and their headings) come on top of the brief's budget, so they never
# squeeze the rest.
BRIEF_BUDGET = 5_000
# The largest brief, the two sections and their headings included: the WORK and REFLECT profiles are measured on it.
BRIEF_MAX = BRIEF_BUDGET + OWNER_BUDGET + RESEARCH_BUDGET + 100
WILL_BUDGET = 4_500 + OWNER_BUDGET + 100  # the largest will context: the LAST_WILL profile is measured on it
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')  # a JSON string: how the owner's and the agent's texts are quoted
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
    memory: dict[str, str] = field(default_factory=dict)
    workspace: list[str] = field(default_factory=list)
    journal: list[sqlite3.Row] = field(default_factory=list)
    news: News = field(default_factory=News)
    research: list[sqlite3.Row] = field(default_factory=list)


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
) -> Snapshot:
    projects = store.open_projects(conn, scope)
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
    files = [f"{e.path}/" if e.is_dir else f"{e.path} ({e.size:,} B)" for e in _safe_listing(workspace)]
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
        owner_messages=store.unseen(conn, "messages", scope, 8),
        pending=[r for r in store.queue(conn, "approvals", scope, 20) if r["status"] == "pending"],
        last_cycle=last_cycle,
        last_journal=journal[0] if journal else None,
        memory=memory.read_all(),
        workspace=files,
        journal=journal,
        news=news or News(),
        research=store.recent_research(conn, scope, RESEARCH_CALLS),
    )


def _safe_listing(workspace: Jail) -> list[Any]:
    try:
        return workspace.listing()[:40]
    except Exception:  # noqa: BLE001 - the context must still be built
        return []


def status_text(s: Snapshot, dry_run: bool) -> str:
    st = s.status
    runway = f"{st.runway.days:.1f} days" if st.runway.days is not None else (st.runway.note or "unknown")
    lines = [
        f"Time: {s.local_time}. You are {s.agent_name}, version {s.version}."
        + (" DRY RUN (simulated money)." if dry_run else ""),
        f"State: {st.state}. Balance ${micros_to_usd(st.balance):.2f}. Runway {runway}.",
        f"Spent today ${micros_to_usd(s.today_spend):.2f} of ${s.daily_cap:.2f}."
        f" This cycle may spend up to ${s.cycle_cap:.2f}.",
    ]
    if st.last_will_due:
        lines.append("Your money is nearly gone: your last will is due.")
    return "\n".join(lines)


def project_lines(s: Snapshot) -> str:
    if not s.projects:
        return "No open projects."
    lines = []
    for p in s.projects[:8]:
        spent, earned = s.project_money.get(p["id"], (0, 0))
        lines.append(
            f"#{p['id']} [{p['status']}] {p['title']} · next: {p['next_step'] or '-'} · spent "
            f"${micros_to_usd(spent):.2f} · earned ${micros_to_usd(earned):.2f}"
        )
        lines.append(f"   hypothesis: {p['hypothesis']}")
    return "\n".join(lines)


def _news_head(s: Snapshot) -> str:
    lines = []
    if s.last_cycle is not None:
        c = s.last_cycle
        lines.append(f"Last cycle #{c['id']} ended {c['status']}" + (f" ({c['note']})" if c["note"] else "") + ".")
    if s.last_journal is not None:
        lines.append(f"Your last journal summary: {s.last_journal['summary']}")
    return "\n".join(lines)


def owner_text(s: Snapshot, budget: int) -> str:
    """What the owner wrote or decided since the last plan, messages first, in ``budget`` bytes.

    The planner, the brief and the will see the same lines. Every item keeps its line: when they don't all fit,
    the texts quoted in decisions and upgrade notes are cut to QUOTE_CAP characters, then every quoted text to
    the same length, the longest that fits (so the messages share the room), each saying how much was left out.
    Only when even SHORTEST_QUOTE characters are too many are the last lines cut.
    """
    return _owner(s, budget)[0]


def _owner(s: Snapshot, budget: int) -> tuple[str, list[tuple[Item, str]]]:
    """``owner_text``, and every item with its line as that text holds it whole (unless it was cut)."""
    decisions = [*s.news.approval_lines(), *s.news.upgrade_lines()]
    items: list[Item] = [*(("message", m["id"], None) for m in s.owner_messages), *s.news.items()]

    def shortened(chars: int | None) -> str:
        messages = [
            f"Message from your owner ({m['created_at']}): {_quote(m['text'], chars)}" for m in s.owner_messages
        ]
        if chars is None:
            return "\n".join([*messages, *decisions])
        return "\n".join([*messages, *(_shorten(line, min(chars, QUOTE_CAP)) for line in decisions)])

    text = shortened(None)
    if json_bytes(text) > budget:
        longest = max([QUOTE_CAP, *(len(m["text"]) for m in s.owner_messages)])
        text = shortened(_largest(SHORTEST_QUOTE, longest, lambda n: json_bytes(shortened(n)) <= budget))
    lines = list(zip(items, text.split("\n"), strict=True)) if items else []
    return cut(text, budget), lines


def _whole(text: str, before: str, lines: list[tuple[Item, str]]) -> frozenset[Item]:
    """The items whose lines ``text`` holds whole right after ``before`` (a cut only takes lines from the end)."""
    shown = []
    for item, line in lines:
        before += line
        if text != before and not text.startswith(before + "\n"):
            break
        shown.append(item)
        before += "\n"
    return frozenset(shown)


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


def _sections(parts: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"== {title} ==\n{body}" for title, body in parts)


def planner_context(s: Snapshot, dry_run: bool, scale: float = 1.0) -> tuple[str, Shown]:
    """The planner's context, and what of the owner's news and of the changelog it shows whole."""
    b = {k: int(v * scale) for k, v in PLANNER_BUDGETS.items()}
    pending = "\n".join(f"#{r['id']} {r['type']}: {r['title']}" for r in s.pending) or "None."
    head = _news_head(s)
    owner, lines = _owner(s, b["news"] - json_bytes(head))
    since = cut("\n".join(part for part in (head, owner) if part) or "Nothing new.", b["news"])
    software = cut(s.news.changelog, b["software"])
    research = research_text(s)
    parts = [
        ("STATUS", cut(status_text(s, dry_run), b["status"])),
        ("SINCE YOUR LAST WAKE", since),
        *([("YOUR SOFTWARE", software)] if s.news.changelog else []),
        ("OPEN PROJECTS", cut(project_lines(s), b["projects"])),
        ("WAITING FOR YOUR OWNER", cut(pending, b["pending"])),
        ("STRATEGY", cut(s.memory.get("strategy", ""), b["strategy"])),
        ("IDENTITY", cut(s.memory.get("identity", ""), b["identity"])),
        ("LESSONS (newest last)", cut(_newest_lines(s.memory.get("lessons", ""), b["lessons"]), b["lessons"])),
        ("WORKSPACE", cut("\n".join(s.workspace) or "Empty.", b["workspace"])),
        *([("RECENT RESEARCH", cut(research, b["research"]))] if research else []),
        ("TASK", "Plan this wake cycle. Reply with the JSON plan only."),
    ]
    shown = Shown(_whole(since, f"{head}\n" if head else "", lines), bool(software) and software == s.news.changelog)
    return _sections(parts), shown


def brief(
    s: Snapshot, dry_run: bool, plan: dict[str, Any], focus: sqlite3.Row | None, max_steps: int
) -> tuple[str, Shown]:
    """The act phase's brief (the same for every step and the reflection), and which of the owner's items it shows."""
    focus_text = "None."
    if focus is not None:
        focus_text = (
            f"Focus project: #{focus['id']} {focus['title']} [{focus['status']}]\n"
            f"Hypothesis: {focus['hypothesis']}\nNext step: {focus['next_step'] or '-'}\n"
            f"Notes: {focus['notes'][-600:] or '-'}"
        )
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(plan.get("steps", []), 1))
    head = [("STATUS", status_text(s, dry_run)), ("PLAN", f"Goal: {plan.get('goal', '')}\n{steps}")]
    owner, lines = _owner(s, OWNER_BUDGET)
    owners = [("FROM YOUR OWNER", owner)] if owner else []
    research = cut(research_text(s), RESEARCH_BUDGET)
    researched = [("RECENT RESEARCH", research)] if research else []
    parts = [
        *head,
        *owners,
        ("FOCUS", focus_text),
        ("LESSONS", _newest_lines(s.memory.get("lessons", ""), 800)),
        ("WORKSPACE", "\n".join(s.workspace[:20]) or "Empty."),
        *researched,
        ("LIMITS", f"At most {max_steps} steps this cycle and 4 tool calls per step. Stop when the goal is reached."),
    ]
    on_top = [*owners, *researched]
    room = sum(json_bytes(f"\n\n== {title} ==\n{body}") - 2 for title, body in on_top)  # - 2: its own JSON quotes
    text = cut(_sections(parts), BRIEF_BUDGET + room)
    return text, Shown(_whole(text, _sections(head) + "\n\n== FROM YOUR OWNER ==\n", lines))


def will_context(s: Snapshot, dry_run: bool) -> str:
    closed = "\n".join(f"- {j['summary']}" for j in reversed(s.journal)) or "No journal yet."
    parts = [
        ("STATUS", status_text(s, dry_run)),
        *owner_section(s),
        ("PROJECTS", project_lines(s)),
        ("RECENT JOURNAL", closed),
        ("LESSONS", _newest_lines(s.memory.get("lessons", ""), 1_200)),
        ("STRATEGY", s.memory.get("strategy", "")[:800]),
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
