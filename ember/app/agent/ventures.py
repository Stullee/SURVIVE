"""Ventures (0.10.0): the agent's ways to earn beyond what it does now, and the legs it already stands on.

A venture is a new market, platform or business model, or a channel that brings buyers to a leg the agent runs (its
Etsy shop is a leg; a blog that brings the shop buyers is a venture of its own). Ventures form a tree that keeps
growing: an idea branches from the venture it came from (``parent_id``: a variant, a niche, a channel, something
research turned up), and brainstorms add new ideas to it. Nothing is deleted; parked and killed ideas stay in it.

Each venture is scored from 1 to 5 on what makes it worth doing (``SCORES``): what it could earn, how much of it the
agent can do, how hard and how risky it is, how soon the first euro comes and what it costs to start. A brainstorm
guesses them first; research replaces the guesses. Their ``weight`` (0 to 100, revenue counting double) ranks the
ideas: the agent researches the heaviest first, and the Ventures tab draws the tree with it.

The agent finds and researches ventures in venture cycles: Ember's code makes a cycle a venture cycle while venture
cycles have had less than the owner's share of the day's spending (the ``venture_share`` option), so ventures get that
share whatever else is going on. What the agent learns goes into the venture's knowledge file in its workspace
(``file_of``); its scorecard (demand, economics, setup, first euro, risks and the first test) is the business case the
owner decides on the Ventures tab.

Stages: idea → researching → proposed (a business case) → building (the owner backed it) → live (launched or
earning), with parked and killed on the side. The agent can't back or kill a venture: only the owner can
(``owner.py``). Money: a cycle's cost counts toward the venture it focused on (``cycles.venture_id``), or else toward
the venture of its project; revenue counts through the venture's projects.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..economy.costs import micros_to_usd
from .store import AgentScope

STAGES = ("idea", "researching", "proposed", "building", "live", "parked", "killed")
OPEN_STAGES = ("idea", "researching", "proposed", "building", "live")
ACTIVE_STAGES = ("researching", "proposed", "building", "live")  # worked on: what MAX_ACTIVE limits
EXPLORING = ("idea", "researching", "proposed")  # not backed yet: what venture cycles find out about
# What the agent may set: backing (building) and killing are the owner's decisions.
AGENT_STAGES = ("idea", "researching", "proposed", "live", "parked")
AGENT_START_STAGES = ("idea", "researching", "live")  # live: a way it already earns, like its Etsy shop
# The business case: each must be filled in before a venture can be proposed. (name, label, characters)
CASE: tuple[tuple[str, str, int], ...] = (
    ("demand", "Demand", 400),
    ("economics", "Economics", 400),
    ("setup", "Setup", 400),
    ("first_euro", "First euro", 200),
    ("risks", "Risks", 400),
    ("first_test", "First test", 400),
)
CASE_FIELDS = tuple(name for name, _, _ in CASE)


@dataclass(frozen=True)
class Score:
    """One criterion, scored 1 to 5: ``good`` says whether 5 is the best (True) or the worst (False)."""

    name: str
    label: str
    good: bool
    factor: int
    low: str  # what 1 means
    high: str  # what 5 means


SCORES: tuple[Score, ...] = (
    Score("revenue", "Revenue", True, 2, "pocket money", "thousands a month"),
    Score("doability", "Doability", True, 1, "needs abilities you can't get", "you can do it all now"),
    Score("difficulty", "Difficulty", False, 1, "easy", "very hard"),
    Score("risk", "Risk", False, 1, "safe", "high risk"),
    Score("speed", "Speed", True, 1, "months to the first euro", "days to the first euro"),
    Score("cost", "Cost", False, 1, "free to start", "hundreds of euros to start"),
)
SCORE_FIELDS = tuple(s.name for s in SCORES)
LIMITS = {"title": 80, "pitch": 600, "next_question": 300, "notes": 2_000, **{n: c for n, _, c in CASE}}
MAX_ACTIVE = 8  # ventures being worked on at once (ideas don't count: the tree keeps growing)
MAX_VENTURES = 400  # in the whole tree, parked and killed ones included
DECIDE_USD = 3.0  # decide a venture that isn't backed yet (propose or park) after about this much spent on it
RESEARCH_CALLS = 8  # research calls in a venture cycle (3 in any other)
# 0.12.0: research calls made for a venture that found something (venture_research), before its scores count as
# research, and before its business case can be proposed.
RESEARCH_TO_SCORE = 1
RESEARCH_TO_PROPOSE = 2
# A business case names a source or an amount: a link, or euros ("12 EUR", "€12", "12,50 €").
_EVIDENCE = re.compile(r"https?://\S|€\s?\d|\d\s?(?:€|eur\b|euros?\b)|\beur(?:os?)?\s?\d", re.IGNORECASE)
# What research and brainstorms may take of a venture cycle's cap (0.10.1): the rest pays for the plan, the work steps
# and the reflection. The first venture cycle planned six research calls and a brainstorm into its $0.60 and stopped
# after four; the brainstorm never ran.
ROOM_SHARE = 0.35
USUAL_COSTS = {"research": 50_000, "brainstorm": 100_000}  # micros, until Ember has made such calls itself
COSTS_OVER = 20  # the recent calls an average is taken of
BRAINSTORM_IDEAS = 6  # ideas one brainstorm adds to the tree
NOTE_CHARS = 300
OWNER_ACTIONS = ("added", "research", "back", "park", "kill", "note")
IDEAS_SHOWN = 8  # the heaviest ideas the planner sees in a venture cycle (the rest are counted)
FOCUS_CHARS = 220  # each field of a venture's FOCUS in the brief, the owner's comment included (0.12.0)
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Money:
    """What a venture cost (every call of the cycles that worked on it) and earned (revenue for its projects)."""

    spent: int = 0
    earned: int = 0


def slug(title: str) -> str:
    """A venture's name in its file name: lower-case ASCII words joined by '-', at most 40 characters."""
    flat = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    name = ""
    for word in _WORD.findall(flat):
        candidate = f"{name}-{word}" if name else word
        if len(candidate) > 40:
            break
        name = candidate
    return name or "venture"


def file_of(venture_id: int, title: str) -> str:
    """The venture's knowledge file in the agent's workspace (its title never changes, so neither does the path)."""
    return f"ventures/{venture_id}-{slug(title)}.md"


IDEAS_FILE = "ventures/ideas.md"  # every brainstorm's ideas, as they came


def knowledge_head(row: Mapping[str, Any]) -> str:
    """The start of a venture's knowledge file, written when the first finding is saved."""
    by = "your owner" if row["created_by"] == "owner" else "you"
    return (
        f"# Venture #{row['id']}: {row['title']}\n"
        f"Pitch: {' '.join(str(row['pitch']).split())}\n"
        f"Started by {by} on {str(row['created_at'])[:10]}.\n\n"
        "## What you learned (newest last)\n"
    )


def _value(values: Mapping[str, Any] | sqlite3.Row, name: str) -> Any:
    """A column of a row, or a key of a dict; None if it has none."""
    try:
        return values[name]
    except (IndexError, KeyError):
        return None


def missing_case(values: Mapping[str, Any] | sqlite3.Row) -> list[str]:
    """The business case's fields still empty in ``values`` (a row or the fields after an update)."""
    return [name for name in CASE_FIELDS if not str(_value(values, name) or "").strip()]


def researched(values: Mapping[str, Any] | sqlite3.Row) -> int:
    """How many research calls for the venture found something (0.12.0), as ``get`` and ``all_ventures`` count."""
    return int(_value(values, "researched") or 0)


def proposal_gaps(values: Mapping[str, Any] | sqlite3.Row, stage: str) -> list[str]:
    """What a venture at ``stage`` still needs before it is proposed (0.12.0), with ``values`` its fields after the
    update: the researching stage, research that found something, all six scores from research and a complete
    business case that names a source or an amount in euros. Empty when it can be proposed."""
    gaps = []
    if stage != "researching":
        gaps.append("the researching stage first")
    count = researched(values)
    if count < RESEARCH_TO_PROPOSE:
        gaps.append(f"{RESEARCH_TO_PROPOSE} research calls for it that found something (it has {count})")
    unscored = [name for name in SCORE_FIELDS if _value(values, name) is None]
    if unscored:
        gaps.append(f"scores for {', '.join(unscored)}")
    elif _value(values, "scores_by") != "research":
        gaps.append("its scores from research (rescore it)")
    missing = missing_case(values)
    if missing:
        gaps.append(f"{', '.join(missing)} filled in")
    elif not any(_EVIDENCE.search(str(_value(values, name) or "")) for name in CASE_FIELDS):
        gaps.append("a source link or an amount in euros in its business case")
    return gaps


def weight(values: Mapping[str, Any] | sqlite3.Row) -> int | None:
    """0 to 100 from the scores given (revenue counting double; the others as far as they are judged), or None
    while none is."""
    got = total = 0
    for s in SCORES:
        value = _value(values, s.name)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
            continue
        got += s.factor * ((value - 1) if s.good else (5 - value))
        total += s.factor * 4
    return round(100 * got / total) if total else None


def scores_text(values: Mapping[str, Any] | sqlite3.Row) -> str:
    """ "weight 72 (revenue 4, doability 3, ...)", or "not scored yet"."""
    known = [f"{s.name} {_value(values, s.name)}" for s in SCORES if _value(values, s.name)]
    w = weight(values)
    if w is None:
        return "not scored yet"
    by = " guessed" if _value(values, "scores_by") == "brainstorm" else ""
    return f"weight {w}{by} ({', '.join(known)})"


# --- records ---


# A venture's rows carry how many research calls for it found something (``researched``).
_RESEARCHED = (
    "(SELECT COUNT(*) FROM venture_research r WHERE r.venture_id = ventures.id AND r.sources > 0) AS researched"
)


def get(conn: sqlite3.Connection, scope: AgentScope, venture_id: int) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(
        f"SELECT *, {_RESEARCHED} FROM ventures WHERE id = ? AND {where}", (venture_id, *params)
    ).fetchone()


def all_ventures(conn: sqlite3.Connection, scope: AgentScope, limit: int = MAX_VENTURES) -> list[sqlite3.Row]:
    """The whole tree (the oldest first, so a parent comes before its branches)."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT *, {_RESEARCHED} FROM ventures WHERE {where} ORDER BY id LIMIT ?", (*params, limit)
    ).fetchall()


def count(conn: sqlite3.Connection, scope: AgentScope, stages: tuple[str, ...] = STAGES) -> int:
    where, params = scope.where()
    marks = ", ".join("?" for _ in stages)
    row = conn.execute(f"SELECT COUNT(*) FROM ventures WHERE {where} AND stage IN ({marks})", (*params, *stages))
    return int(row.fetchone()[0])


def by_title(conn: sqlite3.Connection, scope: AgentScope, title: str) -> sqlite3.Row | None:
    """A venture with this title, whatever its case and spacing."""
    where, params = scope.where()
    wanted = " ".join(title.split()).lower()
    for row in conn.execute(f"SELECT * FROM ventures WHERE {where}", params):
        if " ".join(row["title"].split()).lower() == wanted:
            return row
    return None


def create(
    conn: sqlite3.Connection,
    scope: AgentScope,
    *,
    title: str,
    pitch: str,
    stage: str,
    now: str,
    cycle_id: int | None = None,
    next_question: str = "",
    parent_id: int | None = None,
    created_by: str = "agent",
    entered_by: str | None = None,
    scores: Mapping[str, int] | None = None,
    scores_by: str | None = None,
    news: bool | None = None,
) -> int:
    """A new venture; one the owner adds is news for the agent (owner_action 'added'), unless ``news`` says not."""
    owner = created_by == "owner" if news is None else news
    given = {name: int(v) for name, v in (scores or {}).items() if name in SCORE_FIELDS and v}
    columns = {
        "mode": scope.mode,
        "session": scope.session,
        "life_id": scope.life_id,
        "parent_id": parent_id,
        "created_cycle_id": cycle_id,
        "created_by": created_by,
        "entered_by": entered_by,
        "created_at": now,
        "updated_at": now,
        "title": title,
        "pitch": pitch,
        "stage": stage,
        "next_question": next_question,
        **given,
        "scores_by": scores_by if given else None,
        "owner_action": "added" if owner else None,
        "owner_at": now if owner else None,
        "owner_by": entered_by if owner else None,
        "owner_version": 1 if owner else 0,
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    cursor = conn.execute(f"INSERT INTO ventures ({names}) VALUES ({marks})", tuple(columns.values()))
    return int(cursor.lastrowid)


_COLUMNS = frozenset(
    {"stage", "pitch", "next_question", "notes", "proposed_at", "scores_by", "parked_by", *CASE_FIELDS, *SCORE_FIELDS}
)


def update(conn: sqlite3.Connection, venture_id: int, now: str, **columns: Any) -> None:
    """The agent's changes (the owner's go through ``owner_word``)."""
    if not columns or set(columns) - _COLUMNS:
        raise ValueError("unknown venture columns")
    sets = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE ventures SET {sets}, updated_at = ? WHERE id = ?", (*columns.values(), now, venture_id))


def add_research(
    conn: sqlite3.Connection,
    venture_id: int,
    cycle_id: int,
    llm_call_id: int | None,
    question: str,
    url: str | None,
    sources: int,
    cost_micros: int,
    now: str,
) -> int:
    """Record a research call made for a venture (0.12.0, by Ember's code); returns how many of the venture's research
    calls found something."""
    conn.execute(
        "INSERT INTO venture_research (venture_id, cycle_id, llm_call_id, question, url, sources, cost_micros,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (venture_id, cycle_id, llm_call_id, question[:500], url, sources, cost_micros, now),
    )
    row = conn.execute("SELECT COUNT(*) FROM venture_research WHERE venture_id = ? AND sources > 0", (venture_id,))
    return int(row.fetchone()[0])


def owner_word(
    conn: sqlite3.Connection,
    venture_id: int,
    now: str,
    action: str,
    comment: str | None,
    who: str | None,
    stage: str | None = None,
) -> None:
    """The owner's decision or note on a venture: news for the agent again (a new owner_version), until shown. A
    venture the owner parks is theirs to take up again (``parked_by``, 0.12.0)."""
    if action not in OWNER_ACTIONS:
        raise ValueError("unknown owner action")
    conn.execute(
        "UPDATE ventures SET stage = COALESCE(?, stage), owner_action = ?, owner_comment = ?, owner_at = ?,"
        " owner_by = ?, owner_version = owner_version + 1, seen_cycle_id = NULL, updated_at = ?,"
        " parked_by = CASE WHEN ? IS NULL THEN parked_by WHEN ? = 'parked' THEN 'owner' END WHERE id = ?",
        (stage, action, comment, now, who, now, stage, stage, venture_id),
    )


def add_note(notes: str, cycle_id: int | None, note: str) -> str:
    """``notes`` with a stamped line added; the oldest drop off at the column's limit."""
    stamp = f"[#c{cycle_id}] " if cycle_id else ""
    return (notes + "\n" + stamp + " ".join(note.split())).strip()[-LIMITS["notes"] :]


def projects_of(conn: sqlite3.Connection, venture_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, title, status FROM projects WHERE venture_id = ? ORDER BY id", (venture_id,)
    ).fetchall()


# --- money ---


def money(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, Money]:
    """What each venture cost and earned (see the module's docstring); ventures without either aren't listed."""
    spent = conn.execute(
        "SELECT COALESCE(y.venture_id, p.venture_id) AS vid, COALESCE(SUM(c.cost_micros), 0)"
        " FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id LEFT JOIN projects p ON p.id = y.project_id"
        " WHERE y.session = ? AND y.simulated = ? AND COALESCE(y.venture_id, p.venture_id) IS NOT NULL GROUP BY vid",
        (scope.session, 1 if scope.simulated else 0),
    ).fetchall()
    # Revenue named for the venture, or for one of its projects (0.12.0: the owner names them when recording it).
    earned = conn.execute(
        "SELECT COALESCE(l.venture_id, p.venture_id) AS vid, COALESCE(SUM(l.amount_micros), 0) FROM ledger l"
        " LEFT JOIN projects p ON p.id = l.project_id JOIN ventures v ON v.id = COALESCE(l.venture_id, p.venture_id)"
        " WHERE l.type = 'revenue' AND v.mode = ? AND v.session = ? GROUP BY vid",
        (scope.mode, scope.session),
    ).fetchall()
    found: dict[int, Money] = {int(r[0]): Money(spent=int(r[1])) for r in spent}
    for r in earned:
        found[int(r[0])] = Money(found.get(int(r[0]), Money()).spent, int(r[1]))
    return found


def day_spend(conn: sqlite3.Connection, scope: AgentScope, day: date) -> tuple[int, int]:
    """(all, venture cycles') spending on the owner's local ``day``, in micros: every model call of the scope's
    cycles, finished or still reserved at its estimate."""
    amount = "CASE WHEN c.status = 'pending' THEN c.estimate_micros ELSE c.cost_micros END"
    row = conn.execute(
        f"SELECT COALESCE(SUM({amount}), 0), COALESCE(SUM(CASE WHEN y.venture = 1 THEN {amount} ELSE 0 END), 0)"
        " FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
        " WHERE c.local_day = ? AND y.session = ? AND y.simulated = ?",
        (day.isoformat(), scope.session, 1 if scope.simulated else 0),
    ).fetchone()
    return int(row[0]), int(row[1])


def call_costs(conn: sqlite3.Connection, scope: AgentScope) -> dict[str, int]:
    """What a research call and a brainstorm have cost lately, in micros (USUAL_COSTS for what hasn't run yet)."""
    found = dict(USUAL_COSTS)
    for purpose in USUAL_COSTS:
        row = conn.execute(
            "SELECT AVG(cost_micros) FROM (SELECT c.cost_micros FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
            " WHERE y.session = ? AND y.simulated = ? AND c.purpose = ? AND c.status = 'ok'"
            " ORDER BY c.id DESC LIMIT ?)",
            (scope.session, 1 if scope.simulated else 0, purpose, COSTS_OVER),
        ).fetchone()
        if row[0] is not None and row[0] > 0:
            found[purpose] = int(row[0])
    return found


def room_text(cycle_cap: float, costs: dict[str, int]) -> str:
    """For a venture cycle's STATUS: what its research and brainstorms can cost, and how many of them that is."""
    room = round(cycle_cap * ROOM_SHARE * 1_000_000)
    research = max(costs.get("research") or USUAL_COSTS["research"], 1)
    brainstorm = max(costs.get("brainstorm") or USUAL_COSTS["brainstorm"], 1)
    calls = min(RESEARCH_CALLS, room // research)
    after = min(RESEARCH_CALLS, max(0, room - brainstorm) // research)
    return (
        f"A research call costs about {usd(research)} and a brainstorm about {usd(brainstorm)} (lately): this"
        f" cycle's ${cycle_cap:.2f} pays for about {usd(room)} of them after planning, the work steps and the"
        f" reflection, so about {calls} research calls, or a brainstorm and {after}."
    )


def venture_turn(share: int, spent: int, ventured: int) -> bool:
    """Whether the next cycle is a venture cycle: ventures have had less than ``share`` percent of the day's spending
    (the day's first cycle, with nothing spent yet, is an ordinary one; 100 makes every cycle a venture cycle)."""
    if share <= 0:
        return False
    if share >= 100:
        return True
    return ventured * 100 < spent * share


# --- what the agent is shown ---


def _one_line(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _q(text: Any) -> str:
    return json.dumps(str(text or ""), ensure_ascii=False)


def usd(micros: int) -> str:
    return f"${micros_to_usd(micros):.2f}"


def owner_said(row: Mapping[str, Any], limit: int = 160) -> str:
    """The owner's latest word on the venture, as one short clause ("" if none), their comment at most ``limit``
    characters."""
    action = row["owner_action"]
    if not action:
        return ""
    words = {
        "added": "your owner's idea",
        "research": "your owner wants it researched next",
        "back": "your owner backed it",
        "park": "your owner parked it",
        "kill": "your owner killed it",
        "note": "your owner's note",
    }[action]
    comment = f": {_q(_one_line(row['owner_comment'], limit))}" if row["owner_comment"] else ""
    return f"{words} ({str(row['owner_at'])[:10]}){comment}"


def _head(v: Mapping[str, Any], m: Money) -> str:
    branch = f" (branch of #{v['parent_id']})" if v["parent_id"] else ""
    head = f"#{v['id']} [{v['stage']}] {_one_line(v['title'], 80)}{branch} · {scores_text(v)}"
    if m.spent or m.earned:
        head += f" · spent {usd(m.spent)} · earned {usd(m.earned)}"
    said = owner_said(v)
    return head + (f" · {said}" if said else "")


def planner_lines(rows: list[sqlite3.Row], paid: dict[int, Money], full: bool) -> str:
    """The VENTURES section. In a venture cycle (``full``): the ventures being worked on (with their next question
    and what their business case lacks), then the heaviest ideas (with the start of their pitch), how many other
    ideas there are, and which are parked or killed (so none is started again). In any other cycle: one line per
    venture being worked on, and how many ideas wait."""
    if not rows:
        return "None yet: the tree is empty."
    active = [v for v in rows if v["stage"] in ACTIVE_STAGES]
    ideas = sorted((v for v in rows if v["stage"] == "idea"), key=lambda v: (-(weight(v) or -1), -v["id"]))
    closed = [v for v in rows if v["stage"] in ("parked", "killed")]
    lines = []
    for v in active:
        lines.append(_head(v, paid.get(v["id"], Money())))
        if full and v["stage"] in EXPLORING:
            missing = missing_case(v)
            case = f"missing {', '.join(missing)}" if missing else "complete"
            count = researched(v)
            lines.append(
                f"   next question: {_one_line(v['next_question'], 160) or '-'} · research: {count} call"
                f"{'s' if count != 1 else ''} · business case: {case}"
            )
    shown = ideas[:IDEAS_SHOWN] if full else []
    for v in shown:
        lines.append(_head(v, paid.get(v["id"], Money())))
        lines.append(f"   {_one_line(v['pitch'], 150)}")
    rest = len(ideas) - len(shown)
    if rest:
        lines.append(f"{rest} {'more ' if shown else ''}idea{'s' if rest != 1 else ''} in the tree.")
    if closed and full:
        names = ", ".join(f"#{v['id']} {_one_line(v['title'], 40)}" for v in closed[-12:])
        lines.append(f"Parked or killed (don't start them again): {names}.")
    return "\n".join(lines)


def tree_text(rows: list[sqlite3.Row], limit: int = 120) -> str:
    """The whole tree for a brainstorm: one short line per venture, branches under their parents."""
    children: dict[int | None, list[sqlite3.Row]] = {}
    ids = {v["id"] for v in rows}
    for v in rows:
        parent = v["parent_id"] if v["parent_id"] in ids else None
        children.setdefault(parent, []).append(v)
    lines: list[str] = []

    def walk(parent: int | None, depth: int) -> None:
        for v in children.get(parent, []):
            if len(lines) >= limit:
                return
            w = weight(v)
            score = f", weight {w}" if w is not None else ""
            lines.append(f"{'  ' * depth}- #{v['id']} {_one_line(v['title'], 70)} ({v['stage']}{score})")
            walk(v["id"], depth + 1)

    walk(None, 0)
    if len(rows) > len(lines):
        lines.append(f"… and {len(rows) - len(lines)} more")
    return "\n".join(lines) or "The tree is empty."


def focus_text(row: Mapping[str, Any], paid: Money, file_size: int | None, projects: list[sqlite3.Row]) -> str:
    """The brief's FOCUS for a venture: everything the agent knows of it, the most important first, as the brief cuts
    it from the end (0.12.0: it lost the owner's comment and the first test): the owner's word, the first test, the
    next question and the knowledge file, then the scores, the pitch and the rest of the business case, each field at
    most FOCUS_CHARS characters."""
    file = file_of(row["id"], row["title"])
    kept = f"{file} ({file_size:,} B)" if file_size is not None else f"{file} (not written yet)"
    branch = f" (branch of #{row['parent_id']})" if row["parent_id"] else ""
    lines = [
        f"Focus venture: #{row['id']} {row['title']}{branch} [{row['stage']}] · spent {usd(paid.spent)} · earned "
        f"{usd(paid.earned)}"
    ]
    said = owner_said(row, FOCUS_CHARS)
    if said:
        lines.append(f"Owner: {said}")
    count = researched(row)
    lines += [
        f"First test: {_one_line(row['first_test'], FOCUS_CHARS) or '-'}",
        f"Next question: {_one_line(row['next_question'], FOCUS_CHARS) or '-'}",
        f"Knowledge file: {kept}",
        f"Scores: {scores_text(row)}",
        f"Research for it: {count} call{'s' if count != 1 else ''} that found something (scores need "
        f"{RESEARCH_TO_SCORE}, a business case {RESEARCH_TO_PROPOSE})",
        f"Pitch: {_one_line(row['pitch'], FOCUS_CHARS)}",
    ]
    lines += [f"{label}: {_one_line(row[name], FOCUS_CHARS) or '-'}" for name, label, _ in CASE if name != "first_test"]
    if projects:  # the newest three
        shown = ", ".join(f"#{p['id']} {_one_line(p['title'], 40)} [{p['status']}]" for p in projects[-3:])
        more = f" and {len(projects) - 3} older" if len(projects) > 3 else ""
        lines.append(f"Projects: {shown}{more}")
    if row["notes"]:
        lines.append(f"Notes: {_one_line(row['notes'][-FOCUS_CHARS:], FOCUS_CHARS)}")  # the newest
    return "\n".join(lines)


def news_line(row: Mapping[str, Any]) -> str:
    """The owner's latest word on a venture, for FROM YOUR OWNER (texts JSON-quoted, like every owner line)."""
    name = f"venture #{row['id']} {_q(row['title'])}"
    action = row["owner_action"]
    if action == "added":
        branch = f" (a branch of #{row['parent_id']})" if row["parent_id"] else ""
        line = f"Your owner added a venture idea{branch}, {name}: {_q(row['pitch'])}. Research and score it"
    elif action == "research":
        line = f"Your owner wants {name} researched next (it is {row['stage']} now)"
    elif action == "back":
        line = f"Your owner backed {name}: it is building now. Plan its first test with them"
    elif action == "park":
        line = f"Your owner parked {name}"
    elif action == "kill":
        line = f"Your owner killed {name}: stop all work on it"
    else:
        line = f"Your owner wrote a note on {name}"
    if row["owner_comment"]:
        line += f". Owner's comment: {_q(row['owner_comment'])}"
    return line + "."


# --- the tree's first ideas ---

# The ideas the agent and its owner had before 0.10.0 (the owner's from their messages and talks), planted once in
# every scope's empty tree, so it grows from them. (key, parent key, title, pitch, stage, created_by, note)
SEEDS: tuple[tuple[str, str | None, str, str, str, str, str], ...] = (
    (
        "etsy",
        None,
        "Etsy digital products",
        "Digital downloads in your owner's Etsy shop for German and English buyers: CV and cover letter templates, "
        "trackers and planners that buyers download and use at once. Your first leg.",
        "live",
        "agent",
        "",
    ),
    (
        "pinterest",
        "etsy",
        "Pinterest for the Etsy shop",
        "A Pinterest account with pins and boards you design, bringing buyers to the Etsy shop's listings.",
        "idea",
        "owner",
        "",
    ),
    (
        "dropshipping",
        None,
        "Dropshipping store",
        "A web shop selling physical products that suppliers ship straight to the buyers: products, suppliers, the "
        "store, Germany's legal duties and marketing, researched and built step by step. Your owner offered to help "
        "with accounts and setup.",
        "idea",
        "owner",
        "",
    ),
    (
        "print-on-demand",
        "dropshipping",
        "Print on demand in the Etsy shop",
        "Physical products (posters, mugs, shirts, notebooks) with your designs, made on order by a print-on-demand "
        "partner and sold in the Etsy shop: no stock and no new store.",
        "idea",
        "owner",
        "",
    ),
    (
        "website",
        None,
        "Website or blog with ad revenue",
        "A website or blog you write and run, earning from ads (such as Google AdSense) or affiliate links once it "
        "has visitors.",
        "idea",
        "owner",
        "",
    ),
    (
        "recruiting",
        None,
        "Recruiting and headhunting service",
        "Find the right candidates for companies' hard-to-fill jobs, or the right jobs for candidates: research people "
        "and companies and connect them, for a fee when a match works out.",
        "idea",
        "owner",
        "",
    ),
    (
        "companion",
        None,
        "AI chat companion service",
        "A paid chat service where people talk with an AI character you design and host.",
        "idea",
        "owner",
        "",
    ),
    (
        "fiverr",
        None,
        "Services on Fiverr",
        "Services such as research, writing or documents that you deliver, sold in your owner's Fiverr account.",
        "parked",
        "owner",
        "Your owner put Fiverr on hold.",
    ),
)


def seed(conn: sqlite3.Connection, scope: AgentScope, now: str) -> int:
    """Plant the first ideas in an empty tree (once per scope: nothing is ever deleted, so a planted tree is never
    empty again) and put the projects that made Etsy listings under the Etsy leg. Returns how many were planted."""
    if scope.life_id <= 0 or count(conn, scope):
        return 0
    where, params = scope.where()
    listed = conn.execute(f"SELECT 1 FROM etsy_listings WHERE {where} AND status = 'active' LIMIT 1", params).fetchone()
    ids: dict[str, int] = {}
    for key, parent, title, pitch, stage, by, note in SEEDS:
        if key == "etsy" and listed is None:
            stage = "researching"  # no listing is live yet: not a leg to stand on
        ids[key] = create(
            conn,
            scope,
            title=title,
            pitch=pitch,
            stage=stage,
            now=now,
            parent_id=ids[parent] if parent else None,
            created_by=by,
            news=False,  # planted, not news: the owner's words reached the agent before
        )
        if note:
            update(conn, ids[key], now, notes=note)
        if stage == "parked":  # the owner's ideas were put on hold by them (0.12.0)
            update(conn, ids[key], now, parked_by="owner" if by == "owner" else "agent")
    # The projects whose cycles proposed or changed Etsy listings belong to the Etsy leg (open ones: a closed
    # project is final).
    conn.execute(
        f"UPDATE projects SET venture_id = ? WHERE {where} AND venture_id IS NULL AND status IN ('idea', 'active',"
        " 'waiting') AND id IN (SELECT y.project_id FROM approvals a JOIN cycles y ON y.id = a.cycle_id"
        " WHERE a.executor IN ('etsy_listing', 'etsy_edit') AND y.project_id IS NOT NULL)",
        (ids["etsy"], *params),
    )
    return len(ids)
