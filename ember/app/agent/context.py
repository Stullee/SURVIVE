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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..economy.costs import micros_to_usd
from ..economy.life import LifeStatus
from ..economy.metering import rough_token_count
from ..integrations import mailstore
from . import review, store
from .memory import CAPS, Memory
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
# When the lessons file holds more than this share of its cap, or lessons or strategy hold notes a later version made
# wrong, the planner is asked to rewrite the file (the note takes the room of the file's text it would show).
LESSONS_FULL = 0.7
# Outdated since 0.4.0: write_journal only in some phase (it works whenever the agent is done), and the length limits
# of tool fields (tools show them, and cut notes): a line naming a tool or field (snake_case) and a length.
_PHASE = re.compile(r"\b(?:phase|reflect)", re.IGNORECASE)
_CODE_NAME = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
_LENGTH = re.compile(r"\b\d[\d,.]*\s*(?:chars?|characters)\b|\blength\b|\btoo long\b|\bmaxlength\b", re.IGNORECASE)
# Outdated since 0.5.0: stopping or saving while the owner decides ("don't draft while waiting", "no spend beyond
# planning", "sleep long to save money"); waiting is never a reason to stop, and the daily cap is for experiments.
_WAITING = re.compile(
    r"\b(?:don'?t|do not|never|no|stop|avoid)\b(?: \w+){0,3} (?:draft|build|mak|creat|writ|work)\w*\b.{0,60}?"
    r"\b(?:while|until|before)\b.{0,40}?\b(?:wait|block|owner|approv|decision)"
    r"|\bno spend(?:ing)? beyond\b|\b(?:don'?t|do not|never|avoid) spend(?:ing)?\b.{0,40}?\b(?:beyond|except|outside)\b"
    r"|\bsleep (?:long|longer|as long as|the max)\w*\b(?!.*\bonly\b)",
    re.IGNORECASE,
)
# Outdated since 0.6.0: the owner building files from Ember's specs (Canva, "owner builds the file"), or Ember only
# delivering text; Ember makes PDF, Word, Excel files and listing photos itself.
_HANDOFF = re.compile(
    r"\bcanva\b|\bowner(?:'s)?(?: [\w,'/]+){0,4} (?:builds?|designs?|formats?|lays? out|makes?|exports?|converts?)"
    r"(?: \w+){0,2} (?:files?|templates?|pdfs?|designs?|layouts?)\b"
    r"|\b(?:can'?t|cannot|can not|unable to|no way to) (?:make|create|produce|export|design|build|generate)"
    r"(?: \w+){0,2} (?:pdfs?|docx|word files?|excel|xlsx|spreadsheets?|images?|pngs?|photos?|files?|templates?"
    r"|designs?)\b"
    r"|\bonly (?:\w+ )?text files\b|\bcan (?:actually )?deliver\W+(?:\w+\W+){0,3}text\b",
    re.IGNORECASE,
)
OUTDATED: tuple[tuple[str, str, Callable[[str], bool]], ...] = (
    (
        "0.4.0",
        "write_journal works whenever you are done, and tools show their length limits",
        lambda line: (
            ("write_journal" in line and bool(_PHASE.search(line)))
            or bool(_CODE_NAME.search(line) and _LENGTH.search(line))
        ),
    ),
    (
        "0.5.0",
        "waiting for your owner is never a reason to stop, and your daily cap is there to be spent on experiments",
        lambda line: bool(_WAITING.search(line)),
    ),
    (
        "0.6.0",
        "you make finished PDF, Word and Excel files and listing photos yourself, so your owner never builds them",
        lambda line: bool(_HANDOFF.search(line)),
    ),
)
PLANNER_BUDGETS = {
    "status": 500,
    "instructions": INSTRUCTIONS_BUDGET,
    "news": 2_300,
    "software": CHANGELOG_LIMIT,
    "projects": 2_000,
    "pending": 400,
    "mail": MAIL_BUDGET,
    "strategy": 2_000,
    "identity": 600,
    "lessons": 1_300,
    "journal": 600,
    "workspace": 900,
    "research": RESEARCH_BUDGET,
    "workshop": 800,
    "review": 1_400,
}
# The owner's decisions and messages in the brief and the will context, as much as the planner's news share:
# room for one whole message of plain text at the owner's limit of 2,000 characters.
OWNER_BUDGET = 2_300
QUOTE_CAP = 300  # characters of each text quoted in a decision or upgrade line, when the owner's news is shortened
SHORTEST_QUOTE = 40  # no quoted text is shortened below this; if that isn't enough, the last lines are cut
# The owner's (standing instructions and news), the mail and the research sections (and their headings) come on top
# of the brief's budget, so they never squeeze the rest.
BRIEF_BUDGET = 5_000
# The largest brief, those sections and their headings included: the WORK and REFLECT profiles are measured on it.
BRIEF_MAX = BRIEF_BUDGET + INSTRUCTIONS_BUDGET + OWNER_BUDGET + MAIL_BUDGET + RESEARCH_BUDGET + 200
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
    memory: dict[str, str] = field(default_factory=dict)
    workspace: list[str] = field(default_factory=list)
    journal: list[sqlite3.Row] = field(default_factory=list)
    news: News = field(default_factory=News)
    research: list[sqlite3.Row] = field(default_factory=list)
    mail: MailView | None = None  # None: Ember has no mailbox (then there is no MAIL section)
    instructions: str = ""  # the owner's standing instructions ("" while there are none)
    proven: list[tuple[str, str]] = field(default_factory=list)  # workshop scripts worth building in: (path, why)
    review: str = ""  # today's daily review, as the planner sees it ("" before it is made)


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
) -> Snapshot:
    """What the planner, the brief and the will see; ``today`` (the owner's local date) finds the day's review."""
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
        owner_messages=store.unseen(conn, "messages", scope, 8),
        pending=[r for r in store.queue(conn, "approvals", scope, 20) if r["status"] == "pending"],
        last_cycle=last_cycle,
        last_journal=journal[0] if journal else None,
        memory=memory.read_all(),
        workspace=files,
        journal=journal,
        news=news or News(),
        research=store.recent_research(conn, scope, RESEARCH_CALLS),
        mail=mail,
        instructions=standing["text"] if standing else "",
        proven=store.proven_scripts(conn, scope),
        review=review.planner_text(conn, todays_review) if todays_review is not None else "",
    )


def _safe_listing(workspace: Jail, shown: int = 19, budget: int = 900) -> list[str]:
    """The WORKSPACE lines: the files in every folder with their sizes, then how many more if some are left out.

    At most ``shown`` files and ``budget`` bytes in all, so the count is within the brief's 20 lines and the brief
    keeps room for its LIMITS, even with a long plan and focus.
    """
    try:
        files = workspace.walk(workspace.limits.max_files).files
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
    decisions = [*s.news.approval_lines(), *s.news.upgrade_lines()]
    items: list[Item] = [*(("message", m["id"], None) for m in s.owner_messages), *s.news.items()]
    texts = [m["text"] for m in s.owner_messages]

    def message(i: int, chars: int | None) -> str:
        return f"Message from your owner ({s.owner_messages[i]['created_at']}): {_quote(texts[i], chars)}"

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


def outdated(text: str) -> list[str]:
    """What changed since the notes in ``text`` were written ("since 0.6.0 you make ..."), oldest change first."""
    lines = text.splitlines()
    return [f"since {version} {change}" for version, change, test in OUTDATED if any(test(line) for line in lines)]


def lessons_note(s: Snapshot) -> str:
    """For the planner only: a request to rewrite the lessons when they fill most of their file or hold notes that a
    later version made wrong (empty otherwise)."""
    lessons = s.memory.get("lessons", "")
    size = len(lessons.encode("utf-8"))
    full = size > CAPS["lessons"] * LESSONS_FULL
    changes = outdated(lessons)
    if not full and not changes:
        return ""
    why = []
    if full:
        why.append(f"holds {size:,} of {CAPS['lessons']:,} bytes")
    if changes:
        why.append(f"has outdated notes ({'; '.join(changes)})")
    return (
        f"Memory check: lessons.md {' and '.join(why)}. Plan one step that rewrites it (memory_update lessons replace),"
        " keeping only what still helps you earn money."
    )


def strategy_note(s: Snapshot) -> str:
    """For the planner only: a request to rewrite the strategy when it holds notes a later version made wrong."""
    changes = outdated(s.memory.get("strategy", ""))
    if not changes:
        return ""
    return (
        f"Memory check: strategy.md has outdated notes ({'; '.join(changes)}). Plan one step that rewrites it "
        "(memory_update strategy replace) for how you work now."
    )


def _lessons(s: Snapshot, budget: int) -> str:
    """The planner's LESSONS: the newest lessons that fit, and the memory check's note after them (in the budget)."""
    note = lessons_note(s)
    room = budget - (json_bytes("\n" + note) - 2 if note else 0)
    lessons = cut(_newest_lines(s.memory.get("lessons", ""), room), room) if room > 0 else ""
    return "\n".join(part for part in (lessons, note) if part)


def _strategy(s: Snapshot, budget: int) -> str:
    """The planner's STRATEGY: as much of it as fits, and the memory check's note after it (in the budget)."""
    note = strategy_note(s)
    if not note:
        return cut(s.memory.get("strategy", ""), budget)
    room = budget - (json_bytes("\n" + note) - 2)
    strategy = cut(s.memory.get("strategy", "").rstrip("\n"), room) if room > 0 else ""
    return "\n".join(part for part in (strategy, note) if part)


def workshop_text(s: Snapshot) -> str:
    """For the planner only: the workshop scripts that proved useful, each with a request to have it built in."""
    return "\n".join(
        f"Workshop check: {path} has proven itself ({why}). Plan a step to ask for it to be built into Ember: "
        f"request_upgrade with workshop_script '{path}' (built in, it costs nothing to run and never breaks)."
        for path, why in s.proven
    )


def _sections(parts: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"== {title} ==\n{body}" for title, body in parts)


def planner_context(s: Snapshot, dry_run: bool, scale: float = 1.0) -> tuple[str, Shown]:
    """The planner's context, and what of the owner's news and of the changelog it lists and shows whole."""
    b = {k: int(v * scale) for k, v in PLANNER_BUDGETS.items()}
    pending = "\n".join(f"#{r['id']} {r['type']}: {r['title']}" for r in s.pending) or "None."
    head = _news_head(s)
    owner, lines, _ = _owner(s, b["news"] - json_bytes(head))
    since = cut("\n".join(part for part in (head, owner) if part) or "Nothing new.", b["news"])
    software = cut(s.news.changelog, b["software"])
    research = research_text(s)
    parts = [
        ("STATUS", cut(status_text(s, dry_run), b["status"])),
        *instructions_section(s, b["instructions"]),
        ("SINCE YOUR LAST WAKE", since),
        *([("YOUR SOFTWARE", software)] if s.news.changelog else []),
        *([("TODAY'S REVIEW", cut(s.review, b["review"]))] if s.review else []),
        ("OPEN PROJECTS", cut(project_lines(s), b["projects"])),
        ("WAITING FOR YOUR OWNER", cut(pending, b["pending"])),
        *([("MAIL", cut(mail_text(s), b["mail"]))] if s.mail is not None else []),
        ("STRATEGY", _strategy(s, b["strategy"])),
        ("IDENTITY", cut(s.memory.get("identity", ""), b["identity"])),
        ("LESSONS (newest last)", _lessons(s, b["lessons"])),
        ("WORKSPACE", cut("\n".join(s.workspace) or "Empty.", b["workspace"])),
        *([(RESEARCH_HEADING, cut(research, b["research"]))] if research else []),
        *([("WORKSHOP", cut(workshop_text(s), b["workshop"]))] if s.proven else []),
        ("TASK", "Plan this wake cycle. Reply with the JSON plan only."),
    ]
    held = _held(since, f"{head}\n" if head else "", lines)
    whole = frozenset(item for item, shown_whole in held.items() if shown_whole)
    return _sections(parts), Shown(whole, bool(software) and software == s.news.changelog, frozenset(held))


def brief(
    s: Snapshot, dry_run: bool, plan: dict[str, Any], focus: sqlite3.Row | None, max_steps: int
) -> tuple[str, Shown]:
    """The act phase's brief (the same for every step and the reflection: built from the cycle's snapshot only), and
    which of the owner's items it shows."""
    focus_text = "None."
    if focus is not None:
        focus_text = (
            f"Focus project: #{focus['id']} {focus['title']} [{focus['status']}]\n"
            f"Hypothesis: {focus['hypothesis']}\nNext step: {focus['next_step'] or '-'}\n"
            f"Notes: {focus['notes'][-600:] or '-'}"
        )
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(plan.get("steps", []), 1))
    money = f"\nPath to money: {plan['money_path']}" if plan.get("money_path") else ""
    head = [("STATUS", status_text(s, dry_run)), ("PLAN", f"Goal: {plan.get('goal', '')}{money}\n{steps}")]
    standing = instructions_section(s)
    owner, lines, too_long = _owner(s, OWNER_BUDGET)
    owners = [("FROM YOUR OWNER", owner)] if owner else []
    mailed = mail_section(s)
    research = cut(research_text(s), RESEARCH_BUDGET)
    researched = [(RESEARCH_HEADING, research)] if research else []
    parts = [
        *head,
        *standing,
        *owners,
        *mailed,
        ("FOCUS", focus_text),
        ("LESSONS", _newest_lines(s.memory.get("lessons", ""), 800)),
        ("WORKSPACE", "\n".join(s.workspace[:20]) or "Empty."),
        *researched,
        ("LIMITS", f"At most {max_steps} steps this cycle and 4 tool calls per step. Stop when the goal is reached."),
    ]
    on_top = [*standing, *owners, *mailed, *researched]
    room = sum(json_bytes(f"\n\n== {title} ==\n{body}") - 2 for title, body in on_top)  # - 2: its own JSON quotes
    text = cut(_sections(parts), BRIEF_BUDGET + room)
    held = _held(text, _sections([*head, *standing]) + "\n\n== FROM YOUR OWNER ==\n", lines)
    # A message longer than the brief can ever hold is shown in full as far as it can be.
    full = frozenset(item for item, shown_whole in held.items() if shown_whole or item == too_long)
    return text, Shown(full, listed=frozenset(held))


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
