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
(``owner.py``). Money: each model call counts toward the venture its work served (0.12.0: ``llm_calls.venture_id``,
the cycle's focus or its project's venture, or the venture a research call names; plans, reviews and brainstorms are
overhead); revenue counts for the venture it names or through the venture's projects.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..economy.costs import micros_to_usd
from . import econ
from .sandbox import Jail, SandboxError
from .store import AgentScope

STAGES = ("idea", "researching", "proposed", "building", "live", "parked", "killed")
OPEN_STAGES = ("idea", "researching", "proposed", "building", "live")
ACTIVE_STAGES = ("researching", "proposed", "building", "live")  # worked on
# 0.19.3: what MAX_ACTIVE limits: the ventures being found out about. A backed one is its project's work, and no longer
# takes the room of a new one (live: 4 backed ventures and the Etsy leg left room for 2).
EXPLORED = ("researching", "proposed")
EXPLORING = ("idea", "researching", "proposed")  # not backed yet: what venture cycles find out about
# What the agent may set: backing (building) and killing are the owner's decisions.
AGENT_STAGES = ("idea", "researching", "proposed", "live", "parked")
# 0.15.0: not live: "Ember earns somewhere" let any new venture skip the owner's backing. A venture goes live once the
# owner backed it and its first test is met (the seeded Etsy leg starts live by Ember's code).
AGENT_START_STAGES = ("idea", "researching")
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
MAX_ACTIVE = 8  # ventures researched or proposed at once (ideas don't count: the tree keeps growing)
# 0.12.0: the stages' rules Ember's code keeps (agent/stages.py): research that brings no business case this many days
# after it began is parked, and a backed venture's first test is due this many days after the owner backed it.
RESEARCH_DAYS = 21
FIRST_TEST_DAYS = 21
# A backed venture's first test is missed once it is still unmet this many days after its date, in words or with a
# metric (0.16.3, analysis bug 1: a metric's was closed missed at the first check after its date, with no grace).
FIRST_TEST_GRACE_DAYS = 7
# 0.13.0: triage, an idea of the agent's is researched or parked within this many days of coming up; a live venture that
# has sold nothing this many days after going live is parked (and one that earns more than it costs gets a decision
# point to scale it, due in SCALE_DAYS).
TRIAGE_DAYS = 30
LIVE_DAYS = 60
SCALE_DAYS = 21
MAX_VENTURES = 400  # in the whole tree, parked and killed ones included
# 0.12.0: a venture that isn't backed has this much for research calls (their cost, from the start or since the owner
# last asked for research on it), then a decision: Ember's code refuses more (it was "decide within about $3", a line
# nothing checked). Only the owner grants a new budget (Research next, more or again).
RESEARCH_BUDGET_USD = 0.60
BUDGETED = ("idea", "researching", "proposed", "parked")  # the stages it holds in (a backed venture is being built)
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
    """What a venture cost (the calls whose work served it: 0.12.0) and earned (revenue for it or its projects, less
    its refunds), and, 0.12.0, its P&L: the refunds and the expenses (Etsy's fees, say) recorded for it."""

    spent: int = 0
    earned: int = 0
    refunds: int = 0  # corrections of its revenue (an order refunded, say), as a positive amount
    expenses: int = 0  # its expenses, less their corrections

    @property
    def revenue(self) -> int:
        """Its revenue before refunds."""
        return self.earned + self.refunds

    @property
    def net(self) -> int:
        """What it earned less its expenses and the API calls that worked for it."""
        return self.earned - self.expenses - self.spent


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


def file_of(venture_id: int, title: str, part: int = 1) -> str:
    """The venture's knowledge file in the agent's workspace (its title never changes, so neither does the path). A full
    one continues in part 2, 3, ... (0.12.0: a full file made every venture_update fail, scores and stage too)."""
    return f"ventures/{venture_id}-{slug(title)}{f'-{part}' if part > 1 else ''}.md"


KNOWLEDGE_PARTS = 9  # parts of a venture's knowledge file (64 KB each)


def knowledge_parts(workspace: Jail, venture_id: int, title: str) -> list[str]:
    """The parts of a venture's knowledge file that exist, the first first (the last is the one written to)."""
    found = []
    for part in range(1, KNOWLEDGE_PARTS + 1):
        path = file_of(venture_id, title, part)
        try:
            if workspace.size_of(path, "text") is None:
                break
        except SandboxError:
            break
        found.append(path)
    return found


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


def research_left(values: Mapping[str, Any] | sqlite3.Row) -> int | None:
    """What is left of the venture's research budget (micros), or None in a stage without one (backed, killed)."""
    if values["stage"] not in BUDGETED:
        return None
    return max(0, round(RESEARCH_BUDGET_USD * 1_000_000) - int(_value(values, "research_spent") or 0))


def budget_text(values: Mapping[str, Any] | sqlite3.Row) -> str:
    """Its research budget in a few words, for FOCUS and VENTURES ("" in a stage without one)."""
    left = research_left(values)
    if left is None:
        return ""
    if left <= 0:
        return f"research budget of ${RESEARCH_BUDGET_USD:.2f} used: decide it (a business case or parked)"
    return f"research budget: {usd(left)} of ${RESEARCH_BUDGET_USD:.2f} left"


def research_refusal(values: Mapping[str, Any] | sqlite3.Row) -> str:
    """Why research for the venture is refused ("" when it isn't): its research budget is used."""
    left = research_left(values)
    if left is None or left > 0:
        return ""
    spent = usd(int(_value(values, "research_spent") or 0))
    decide = (
        ": decide it now: its business case (stage proposed) or parked, with why in its note."
        if values["stage"] in ("idea", "researching")
        else "."
    )
    return (
        f"venture #{values['id']} has used its research budget ({spent} of ${RESEARCH_BUDGET_USD:.2f}){decide} Only "
        "your owner grants more research for it (Research more on the Ventures tab)"
    )


def proposal_gaps(values: Mapping[str, Any] | sqlite3.Row, stage: str) -> list[str]:
    """What a venture at ``stage`` still needs before it is proposed (0.12.0), with ``values`` its fields after the
    update: the researching stage, research that found something, all six scores from research, a complete business
    case that names a source or an amount in euros and (0.13.0) its numbers. Empty when it can be proposed."""
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
    if not int(_value(values, "cases") or 0):  # 0.13.0
        gaps.append("its numbers (venture_case)")
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
    "(SELECT COUNT(*) FROM venture_research r WHERE r.venture_id = ventures.id AND r.sources > 0) AS researched,"
    # 0.12.0: when its research in this stage began (its first research call since the stage changed), or NULL
    " (SELECT MIN(r.created_at) FROM venture_research r WHERE r.venture_id = ventures.id"
    " AND r.created_at >= ventures.stage_at) AS research_from,"
    # 0.12.0: what its research calls cost since its research budget began (the owner's last word asking for research)
    " (SELECT COALESCE(SUM(r.cost_micros), 0) FROM venture_research r WHERE r.venture_id = ventures.id"
    " AND r.created_at > COALESCE(ventures.research_granted_at, '')) AS research_spent,"
    # 0.13.0: its numeric business cases (venture_cases; the newest counts)
    " (SELECT COUNT(*) FROM venture_cases c WHERE c.venture_id = ventures.id) AS cases"
)


def get(conn: sqlite3.Connection, scope: AgentScope, venture_id: int) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(
        f"SELECT *, {_RESEARCHED} FROM ventures WHERE id = ? AND {where}", (venture_id, *params)
    ).fetchone()


def owner_stopped(conn: sqlite3.Connection, scope: AgentScope, venture_id: int | None) -> sqlite3.Row | None:
    """0.23.2: the venture when the owner parked or killed it (its projects wait, or were closed), else None. The
    owner's word stops its project work (0.22.0, stages.stop_projects): what reaches it, it stops too."""
    row = get(conn, scope, venture_id) if venture_id is not None else None
    return row if row is not None and (row["stage"] == "killed" or row["parked_by"] == "owner") else None


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


def add_case(
    conn: sqlite3.Connection,
    venture_id: int,
    cycle_id: int | None,
    case: econ.Case,
    result: econ.Economics,
    now: str,
    needs: str = "",
) -> int:
    """Save a venture's numbers with Ember's code's economics of them (0.13.0), and what it needs that a knock-out
    rules out (``needs``: knockouts.NEEDS, comma-separated); returns the case's number."""
    cursor = conn.execute(
        "INSERT INTO venture_cases (venture_id, cycle_id, created_at, channel, price_eur, unit_cost_eur,"
        " monthly_costs_eur, sales_low, sales_mid, sales_high, setup_eur, owner_hours, first_sale_months, api_usd,"
        " usd_per_eur, fees_eur, net_eur, break_even, net_low, net_mid, net_high, ev_eur, ev_per_api_usd,"
        " ev_per_hour, needs, first_sale_days)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            venture_id,
            cycle_id,
            now,
            case.channel,
            case.price_eur,
            case.unit_cost_eur,
            case.monthly_costs_eur,
            *case.sales,
            case.setup_eur,
            case.owner_hours,
            min(econ.MAX_FIRST_SALE_MONTHS, round(case.first_sale_days / econ.DAYS_A_MONTH)),  # as 0.13.0 kept it
            case.api_usd,
            result.usd_per_eur,
            result.fees_eur,
            result.net_eur,
            result.break_even,
            *result.net,
            result.ev_eur,
            result.ev_per_api_usd,
            result.ev_per_hour,
            needs,
            case.first_sale_days,
        ),
    )
    return int(cursor.lastrowid)


def latest_case(conn: sqlite3.Connection, venture_id: int) -> sqlite3.Row | None:
    """A venture's newest numeric business case (0.13.0), or None."""
    return conn.execute(
        "SELECT * FROM venture_cases WHERE venture_id = ? ORDER BY id DESC LIMIT 1", (venture_id,)
    ).fetchone()


def case_of(row: Mapping[str, Any]) -> tuple[econ.Case, econ.Economics]:
    """A stored case as the agent gave it and as Ember's code computed it."""
    case = econ.Case(
        channel=str(row["channel"]),
        price_eur=float(row["price_eur"]),
        unit_cost_eur=float(row["unit_cost_eur"]),
        monthly_costs_eur=float(row["monthly_costs_eur"]),
        sales=(int(row["sales_low"]), int(row["sales_mid"]), int(row["sales_high"])),
        setup_eur=float(row["setup_eur"]),
        owner_hours=float(row["owner_hours"]),
        first_sale_days=int(row["first_sale_days"]),
        api_usd=float(row["api_usd"]),
    )
    result = econ.Economics(
        usd_per_eur=float(row["usd_per_eur"]),
        fees_eur=float(row["fees_eur"]),
        net_eur=float(row["net_eur"]),
        break_even=None if row["break_even"] is None else float(row["break_even"]),
        net=(float(row["net_low"]), float(row["net_mid"]), float(row["net_high"])),
        ev_eur=float(row["ev_eur"]),
        ev_per_api_usd=None if row["ev_per_api_usd"] is None else float(row["ev_per_api_usd"]),
        ev_per_hour=None if row["ev_per_hour"] is None else float(row["ev_per_hour"]),
    )
    return case, result


def numbers_text(row: Mapping[str, Any] | None) -> str:
    """A venture's numbers for FOCUS: "" without a case."""
    if row is None:
        return ""
    case, result = case_of(row)
    return f"Numbers (case #{row['id']}, {str(row['created_at'])[:10]}): {result.text(case)}"


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
    venture the owner parks is theirs to take up again (``parked_by``, 0.12.0), and asking for research on one starts
    a new research budget for it (0.12.0)."""
    if action not in OWNER_ACTIONS:
        raise ValueError("unknown owner action")
    conn.execute(
        "UPDATE ventures SET stage = COALESCE(?, stage), owner_action = ?, owner_comment = ?, owner_at = ?,"
        " owner_by = ?, owner_version = owner_version + 1, seen_cycle_id = NULL, updated_at = ?,"
        " parked_by = CASE WHEN ? IS NULL THEN parked_by WHEN ? = 'parked' THEN 'owner' END,"
        # 0.12.0: asking for research on it starts a new research budget (only the owner's word can: migration 0041)
        " research_granted_at = CASE WHEN ? = 'research' THEN ? ELSE research_granted_at END WHERE id = ?",
        (stage, action, comment, now, who, now, stage, stage, action, now, venture_id),
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
    # 0.12.0: each call names the venture its work served (a research call the one it names); plans, reviews and
    # brainstorms are overhead (a cycle's whole cost was charged to its focus, and research for another venture too).
    spent = conn.execute(
        "SELECT c.venture_id AS vid, COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c"
        " JOIN cycles y ON y.id = c.cycle_id WHERE y.session = ? AND y.simulated = ? AND c.venture_id IS NOT NULL"
        " GROUP BY vid",
        (scope.session, 1 if scope.simulated else 0),
    ).fetchall()
    # Revenue and expenses named for the venture, or for one of its projects (0.12.0: the owner, or Ember's code for
    # an Etsy order, names them when recording them), with their corrections: a revenue's are its refunds.
    booked = conn.execute(
        "SELECT COALESCE(l.venture_id, p.venture_id) AS vid,"
        " COALESCE(SUM(CASE WHEN l.type = 'revenue' THEN l.amount_micros ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN l.type = 'revenue' AND l.corrects_id IS NOT NULL THEN -l.amount_micros ELSE 0 END),"
        " 0),"
        " COALESCE(SUM(CASE WHEN l.type = 'expense' THEN l.amount_micros ELSE 0 END), 0) FROM ledger l"
        " LEFT JOIN projects p ON p.id = l.project_id JOIN ventures v ON v.id = COALESCE(l.venture_id, p.venture_id)"
        " WHERE l.type IN ('revenue', 'expense') AND v.mode = ? AND v.session = ? GROUP BY vid",
        (scope.mode, scope.session),
    ).fetchall()
    found: dict[int, Money] = {int(r[0]): Money(spent=int(r[1])) for r in spent}
    for r in booked:
        found[int(r[0])] = Money(found.get(int(r[0]), Money()).spent, int(r[1]), int(r[2]), int(r[3]))
    return found


def money_text(m: Money) -> str:
    """A venture's money as the plans and the review show it (0.12.0: with its expenses and what it nets)."""
    text = f"spent {usd(m.spent)} · earned {usd(m.earned)}"
    if m.expenses:
        text += f" less {usd(m.expenses)} of expenses"
    if m.earned or m.expenses:
        text += f" · net {'-' if m.net < 0 else '+' if m.net > 0 else ''}{usd(abs(m.net))}"
    return text


def day_spend(conn: sqlite3.Connection, scope: AgentScope, day: date) -> tuple[int, int]:
    """(all, venture cycles') spending on the owner's local ``day``, in micros: every model call of the scope's
    cycles, finished (an uncertain one at what it is known to cost, 0.12.0) or still reserved at its estimate."""
    amount = "CASE WHEN c.status = 'pending' THEN c.estimate_micros ELSE c.floor_micros END"
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


def room_text(cycle_cap: float, costs: dict[str, int], brainstorms: bool = True) -> str:
    """For a venture cycle's STATUS: what its research and brainstorms can cost, and how many of them that is
    (0.15.0: research only, where the burn mode allows no brainstorm)."""
    room = round(cycle_cap * ROOM_SHARE * 1_000_000)
    research = max(costs.get("research") or USUAL_COSTS["research"], 1)
    brainstorm = max(costs.get("brainstorm") or USUAL_COSTS["brainstorm"], 1)
    calls = min(RESEARCH_CALLS, room // research)
    after = min(RESEARCH_CALLS, max(0, room - brainstorm) // research)
    if not brainstorms:
        return (
            f"A research call costs about {usd(research)} (lately): this cycle's ${cycle_cap:.2f} pays for about"
            f" {usd(room)} of them after planning, the work steps and the reflection, so about {calls} research calls."
        )
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
    if m.spent or m.earned or m.expenses:
        head += f" · {money_text(m)}"
    rule = stage_rule(v)
    said = owner_said(v)
    return head + (f" · {rule}" if rule else "") + (f" · {said}" if said else "")


def rule_clock(v: Mapping[str, Any]) -> date | None:
    """0.13.0: the day a stage's rule counts from: when the venture reached its stage or, for one already in it when
    the rule came (rules_from), that day. None for a row built by hand without the columns."""
    reached = _value(v, "stage_at") or _value(v, "created_at")
    if not reached:
        return None
    start = date.fromisoformat(str(reached)[:10])
    since = _value(v, "rules_from")
    return max(start, date.fromisoformat(str(since)[:10])) if since else start


def triage_date(v: Mapping[str, Any]) -> date | None:
    """0.13.0: the day Ember's code parks an idea of the agent's no one took up (None for the owner's ideas)."""
    start = rule_clock(v)
    if v["stage"] != "idea" or _value(v, "created_by") != "agent" or start is None:
        return None
    return start + timedelta(days=TRIAGE_DAYS)


def is_first_test(row: Mapping[str, Any]) -> bool:
    """0.16.3 (analysis bug 1): whether a milestone is a backed venture's first test, set by Ember's code (not a bar of
    a product line's listing test, agent/gates.py, which is a first test of a project)."""
    return (
        _value(row, "kind") == "first_test"
        and _value(row, "created_by") == "code"
        and bool(_value(row, "venture_id"))
        and not _value(row, "project_id")
    )


def test_ends(row: Mapping[str, Any]) -> date | None:
    """0.16.3 (analysis bug 1): the last day a venture's first test can be met, its date and FIRST_TEST_GRACE_DAYS: the
    next day Ember's code closes it missed and parks the venture (agent/stages.py). None for any other milestone."""
    if not is_first_test(row):
        return None
    try:
        return date.fromisoformat(str(row["due"])) + timedelta(days=FIRST_TEST_GRACE_DAYS)
    except ValueError:
        return None


def stage_rule(v: Mapping[str, Any]) -> str:
    """The rule of the venture's stage that Ember's code keeps (0.12.0, agent/stages.py), in a few words: "" for a
    stage without one, or a row built by hand without the columns."""
    began, test, parked_by = (_value(v, name) for name in ("research_from", "test_milestone_id", "parked_by"))
    triage = triage_date(v)
    if triage is not None:  # 0.13.0
        return f"an idea: research it by {triage.isoformat()} or Ember's code parks it (triage)"
    if v["stage"] == "live":  # 0.13.0
        scale = _value(v, "scale_milestone_id")
        if scale:
            return f"it earns more than it costs: milestone #{scale} scales it"
        start = rule_clock(v)
        if start is not None:
            ends = (start + timedelta(days=LIVE_DAYS)).isoformat()
            return f"live: nothing sold by {ends} parks it; earning more than it costs sets a milestone to scale it"
    if v["stage"] == "researching" and began:
        park = date.fromisoformat(str(began)[:10]) + timedelta(days=RESEARCH_DAYS)
        return f"researched since {str(began)[:10]}: no business case by {park.isoformat()} parks it"
    if v["stage"] == "building" and test:
        return f"first test: milestone #{test}; it goes live once that is met"
    if v["stage"] == "parked" and parked_by == "code":
        return "parked by Ember's code: only your owner takes it up again"
    return ""


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
            budget = budget_text(v)
            lines.append(
                f"   next question: {_one_line(v['next_question'], 160) or '-'} · research: {count} call"
                f"{'s' if count != 1 else ''}{f' · {budget}' if budget else ''} · business case: {case}"
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


def focus_text(
    row: Mapping[str, Any],
    paid: Money,
    file_size: int | None,
    projects: list[sqlite3.Row],
    parts: list[str] | None = None,
    last: str = "",
    evidence: str = "",
    numbers: str = "",
    knocked: str = "",
    critic: tuple[str, str] = ("", ""),
) -> str:
    """The brief's FOCUS for a venture: everything the agent knows of it, the most important first, as the brief cuts
    it from the end (0.12.0: it lost the owner's comment and the first test): the owner's word, its knock-outs
    (``knocked``) and the critic's verdict and flaw (``critic``: that line and the one with its numbers), the first
    test and the next question, its numbers (``numbers``), its evidence by grade (``evidence``) and the pitch (0.15.0:
    these came after the scores, and a case with a critique lost them), then the knowledge file, the digest of the
    last cycle aimed at it (``last``), the scores, the critic's numbers and the rest of the business case. Each field
    is at most FOCUS_CHARS characters (0.15.0: the knock-outs, the critic's verdict and the evidence too; the owner's
    card shows them whole), so VENTURE_FOCUS_BUDGET holds everything up to the pitch, each field at its longest."""
    file = parts[-1] if parts else file_of(row["id"], row["title"])  # ``parts``: the knowledge file's (0.12.0)
    kept = f"{file} ({file_size:,} B)" if file_size is not None else f"{file} (not written yet)"
    if parts and len(parts) > 1:
        kept += f"; its earlier parts: {', '.join(parts[:-1])}"
    branch = f" (branch of #{row['parent_id']})" if row["parent_id"] else ""
    lines = [f"Focus venture: #{row['id']} {row['title']}{branch} [{row['stage']}] · {money_text(paid)}"]
    said = owner_said(row, FOCUS_CHARS)
    if said:
        lines.append(f"Owner: {said}")
    rule = stage_rule(row)
    if rule:
        lines.append(f"Stage rule (Ember's code keeps it): {rule}")
    count = researched(row)
    budget = budget_text(row)  # 0.12.0
    verdict, reckoned = critic
    lines += [
        *([_one_line(knocked, FOCUS_CHARS)] if knocked else []),
        *([_one_line(verdict, FOCUS_CHARS)] if verdict else []),
        f"First test: {_one_line(row['first_test'], FOCUS_CHARS) or '-'}",
        f"Next question: {_one_line(row['next_question'], FOCUS_CHARS) or '-'}",
        *([numbers] if numbers else []),
        *([_one_line(evidence, FOCUS_CHARS)] if evidence else []),
        f"Pitch: {_one_line(row['pitch'], FOCUS_CHARS)}",
        f"Knowledge file: {kept}",
        *([f"Its last cycle (Ember's code's digest): {last}"] if last else []),
        f"Scores: {scores_text(row)}",
        f"Research for it: {count} call{'s' if count != 1 else ''} that found something (scores need "
        f"{RESEARCH_TO_SCORE}, a business case {RESEARCH_TO_PROPOSE})" + (f"; {budget}" if budget else ""),
        *([reckoned] if reckoned else []),
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
        # 0.15.0: "Research and score it", but only a venture cycle's venture_update takes scores
        line = f"Your owner added a venture idea{branch}, {name}: {_q(row['pitch'])}. "
        line += "Research it; a venture cycle scores it"
    elif action == "research":
        line = f"Your owner wants {name} researched next (it is {row['stage']} now)"
    elif action == "back":
        test, channel = _value(row, "test_milestone_id"), _value(row, "channel")
        line = f"Your owner backed {name}: it is building now. " + (
            f"Its first test is milestone #{test} on your roadmap: it goes live once Ember's code or your owner "
            "finds it met"
            if test
            # 0.15.0: Ember's code sets a channel's first test once the owner has set the channel up (stages.keep)
            else f"Its first test starts once {str(channel).title()} is set up"
            if channel
            else "Plan its first test with them"
        )
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
        channel = {"pinterest": "pinterest", "print-on-demand": "printify"}.get(key)  # 0.13.0 (Phase E2, E4)
        if channel is not None:  # the channel Ember's code serves it with
            conn.execute("UPDATE ventures SET channel = ? WHERE id = ?", (channel, ids[key]))
    # The projects whose cycles proposed or changed Etsy listings belong to the Etsy leg (open ones: a closed
    # project is final).
    conn.execute(
        f"UPDATE projects SET venture_id = ? WHERE {where} AND venture_id IS NULL AND status IN ('idea', 'active',"
        " 'waiting') AND id IN (SELECT y.project_id FROM approvals a JOIN cycles y ON y.id = a.cycle_id"
        " WHERE a.executor IN ('etsy_listing', 'etsy_edit') AND y.project_id IS NOT NULL)",
        (ids["etsy"], *params),
    )
    return len(ids)


ETSY_LEG = next(title for key, _, title, *_ in SEEDS if key == "etsy")


def channel_venture(conn: sqlite3.Connection, scope: AgentScope, channel: str) -> sqlite3.Row | None:
    """0.16.3 (analysis bug 1): the venture a product line of a channel sells for: the Etsy leg for an Etsy listing (a
    digital download), the print-on-demand venture ('printify') for a Printify product. None when there is none, or it
    is parked or killed."""
    row = _channel_row(conn, scope, channel)
    return row if row is not None and row["stage"] not in ("parked", "killed") else None


def _channel_row(conn: sqlite3.Connection, scope: AgentScope, channel: str) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM ventures WHERE {where} AND (channel = ? OR (? = 'etsy' AND title = ? AND parent_id IS NULL))"
        " ORDER BY id LIMIT 1",
        (*params, channel, channel, ETSY_LEG),
    ).fetchone()


CHANNEL_TABLES = (("etsy", "etsy_listings"), ("printify", "printify_products"))  # where a channel's listings are kept


def is_channel(venture: Mapping[str, Any]) -> bool:
    """0.23.3: a channel's own venture (the Etsy leg, print on demand), not a product venture that sells through it."""
    return venture["channel"] is not None or (venture["title"] == ETSY_LEG and venture["parent_id"] is None)


def project_stopped(
    conn: sqlite3.Connection, scope: AgentScope, project_id: int, channel: str | None = None
) -> sqlite3.Row | None:
    """0.23.3: the venture whose owner's park or kill stops a product line's work (None when none does): its own; or,
    for a line of no venture or of a channel's own venture (the Etsy leg's lines make Printify products too), the
    venture of the channel it works in: ``channel`` (a new listing or product, an edit), else the channels it sells
    in (its focus, milestones, bets), when every one of them is stopped. A product venture the owner backed sells
    through a channel on its own word. Live, a line of no venture went on selling in the parked Etsy leg's channel,
    and parking print on demand stopped no Printify product (they were the Etsy leg's lines')."""
    project = conn.execute("SELECT venture_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        return None
    own = get(conn, scope, project["venture_id"]) if project["venture_id"] is not None else None
    held = owner_stopped(conn, scope, project["venture_id"])
    if held is not None:
        return held
    if own is not None and not is_channel(own):
        return None
    names = [channel] if channel else [name for name, table in CHANNEL_TABLES if _lists_in(conn, table, project_id)]
    stopped = [channel_stopped(conn, scope, name) for name in names]
    return stopped[0] if stopped and all(v is not None for v in stopped) else None


def channel_stopped(conn: sqlite3.Connection, scope: AgentScope, channel: str) -> sqlite3.Row | None:
    """0.23.3: the channel's own venture when the owner parked or killed it, else None."""
    row = _channel_row(conn, scope, channel)
    return owner_stopped(conn, scope, row["id"]) if row is not None else None


def _lists_in(conn: sqlite3.Connection, table: str, project_id: int) -> bool:
    """Whether a product line has listings in ``table`` (etsy_listings, printify_products), as metrics.listings
    counts them: made (a listing number), by its request's project or its cycle's."""
    return (
        conn.execute(
            f"SELECT 1 FROM {table} l JOIN approvals a ON a.id = l.approval_id LEFT JOIN cycles y ON y.id = a.cycle_id"
            " WHERE l.listing_id IS NOT NULL AND COALESCE(a.project_id, y.project_id) = ? LIMIT 1",
            (project_id,),
        ).fetchone()
        is not None
    )


def listing_stopped(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> sqlite3.Row | None:
    """0.23.3: the venture whose owner's park or kill stops the work on one of Ember's listings (an edit, a pin, a
    post or a blog post recommending it): its product line's in its channel (``project_stopped``). None when none
    does, or it isn't Ember's."""
    where, params = scope.where("l")
    for name, table in CHANNEL_TABLES:
        row = conn.execute(
            f"SELECT COALESCE(a.project_id, y.project_id) AS project_id FROM {table} l JOIN approvals a"
            f" ON a.id = l.approval_id LEFT JOIN cycles y ON y.id = a.cycle_id WHERE {where} AND l.listing_id = ?"
            " ORDER BY l.id DESC LIMIT 1",
            (*params, listing_id),
        ).fetchone()
        if row is None:
            continue
        if row["project_id"] is None:
            return channel_stopped(conn, scope, name)
        return project_stopped(conn, scope, int(row["project_id"]), name)
    return None


# 0.23.3: the requests that carry on a product line's work when Ember's code carries them out (an email, a page of the
# owner's site or a Reddit link isn't a line's), with the table each executor claims a request in when it begins
HELD_EXECUTORS = {
    "etsy_listing": "etsy_listings",
    "printify_product": "printify_products",
    "etsy_edit": "etsy_edits",
    "pinterest_pin": "pinterest_pins",
    "bluesky_post": "bluesky_posts",
}
_NEW_LINE = {"etsy_listing": "etsy", "printify_product": "printify"}
LISTING_LINK = re.compile(r"etsy\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?listing/(\d{1,18})(?:[/?#]|$)", re.IGNORECASE)


def request_stopped(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any]) -> sqlite3.Row | None:
    """0.23.3: the venture whose owner's park or kill stops the work a request carries on (None when none does): a new
    listing or product of its line, or a change, pin or post of a listing (0.24.1: a post's second link too). Taking a
    listing out of the shop (deactivating it, ending its automatic renewal) stops nothing."""
    if row["executor"] not in HELD_EXECUTORS:
        return None
    try:
        action = json.loads(row["action"] or "{}")
    except ValueError:
        return None
    if not isinstance(action, dict):
        return None
    if row["executor"] in _NEW_LINE:
        project = row["project_id"]
        if project is None and row["cycle_id"] is not None:
            cycle = conn.execute("SELECT project_id FROM cycles WHERE id = ?", (row["cycle_id"],)).fetchone()
            project = cycle["project_id"] if cycle is not None else None
        channel = _NEW_LINE[row["executor"]]
        return project_stopped(conn, scope, int(project), channel) if project else channel_stopped(conn, scope, channel)
    if row["executor"] == "etsy_edit":
        ends_renewal = action.get("auto_renew") is False and set(action) <= {"listing_id", "currency", "auto_renew"}
        if action.get("state") == "deactivate" or ends_renewal:
            return None
        listings = [action.get("listing_id")]
    else:  # a pin's or a post's link, and a post's second link
        links = (str(action.get(key) or "") for key in ("link", "second_link"))
        listings = [found[1] for found in map(LISTING_LINK.search, links) if found]
    for listing in listings:
        if isinstance(listing, (int, str)) and str(listing).isdigit():
            stopped = listing_stopped(conn, scope, int(listing))
            if stopped is not None:
                return stopped
    return None


def adopt(conn: sqlite3.Connection, scope: AgentScope, project_id: int, channel: str, now: str) -> int | None:
    """0.15.0: a product line that sells in the Etsy shop belongs to a venture: a project without one joins its
    channel's (``channel_venture``), never a parked or killed one. Its sales counted for no venture, so a leg that sold
    was parked as one that sold nothing. 0.16.3 (analysis bug 1): the channel's, not the venture the cycle aimed at: a
    digital download made in a cycle for print on demand joined that venture, and went down with it when Ember's code
    parked it. Returns the venture it joined, or None."""
    project = conn.execute("SELECT venture_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None or project["venture_id"] is not None:
        return None
    venture = channel_venture(conn, scope, channel)
    if venture is None:
        return None
    conn.execute("UPDATE projects SET venture_id = ?, updated_at = ? WHERE id = ?", (venture["id"], now, project_id))
    return int(venture["id"])
