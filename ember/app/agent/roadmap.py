"""The roadmap (0.11.0): the agent's plan for the weeks and months ahead, and how it keeps to it.

A milestone is something the agent will reach by a date, with a measure that shows it is reached ("3 listings live",
"business case for venture #3 proposed"). The roadmap holds goals for the next three months, the milestones this month
that lead to them (``parent_id``) and this week's steps, each linked to the venture or project it serves. Every plan
sees the roadmap by horizon (overdue, this week, this month, the next three months, later) and aims the cycle at a
milestone (``cycles.milestone_id``); the daily review checks it.

It stays honest by construction: what a milestone says it will reach (title) and how that is known (measure) never
change; a date can move twice at most, and every move is counted (``moves``, with ``first_due`` kept); a milestone
ends done (with the evidence), missed (why, and what now; only once its date has passed) or dropped (why, and the open
milestones leading to it with it), and is final then (migration 0014). Ember's code flags an empty roadmap, overdue
milestones, a week with nothing due and a roadmap that ends within the month (``checks``).

The owner adds milestones, leaves notes and drops milestones on the Roadmap tab (``owner.py``); their word is news
for the agent like a decision. A milestone the owner added is theirs (0.12.0, migration 0021): the agent can't drop
it, and its new date for one is a proposal the owner accepts or rejects. The last ``OWNER_SLOTS`` open places are kept
for the owner. Dates are the owner's local days.

0.15.0: only the agent's and the owner's milestones take the roadmap's places (``placed``). Ember's code's have a bound
of their own: one money goal with its two decision points, one first test or scale point for each backed venture, and
one bar or scale point at a time for each product line's listing test (agent/gates.py).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from ..economy import metering
from . import metrics, ventures
from .store import AgentScope

STATUSES = ("open", "done", "missed", "dropped")
CLOSED = ("done", "missed", "dropped")
LIMITS = {"title": 100, "measure": 300, "result": 600, "notes": 2_000, "comment": 1_000}
NOTE_CHARS = 300
MAX_OPEN = 20  # the agent's and the owner's open milestones at once: a roadmap every plan can read
OWNER_SLOTS = 4  # the last open places, kept for the owner: the agent adds milestones while fewer than 16 are open
PLACED_BY = ("agent", "owner")  # 0.15.0: whose milestones take a place (Ember's code's are bounded by what it keeps)
MAX_MOVES = 2  # how often a milestone's date can move
MAX_MILESTONES = 2_000  # in all, closed ones included
AHEAD_DAYS = 366  # how far ahead a milestone can be dated
CLOSED_DAYS = 14  # the planner sees the milestones closed in these days
OWNER_ACTIONS = ("added", "note", "drop", "accept", "reject")
# The horizons of an open milestone that isn't overdue, by the days until it is due: (key, label, last day).
HORIZONS = (("week", "This week", 6), ("month", "This month", 30), ("quarter", "Next three months", 91))
LATER = ("later", "Later")
OVERDUE = ("overdue", "Overdue")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COLUMNS = frozenset(
    {
        "due",
        "moves",
        "status",
        "result",
        "closed_at",
        "closed_cycle_id",
        "closed_by",
        "notes",
        "parent_id",
        "venture_id",
        "project_id",
        "proposed_due",
        "proposed_note",
        "proposed_at",
        "proposed_cycle_id",
        "wait_for",
        "check_at",
    }
)
NO_PROPOSAL = {"proposed_due": None, "proposed_note": None, "proposed_at": None, "proposed_cycle_id": None}
# 0.12.0: the roadmap is never empty. Ember's code keeps a money goal at its root, settles it from the books (done once
# it is met, missed after its date) and sets the next one, with decision points at a quarter and at half of the runway
# (the goal's 90 days at most). A met goal's successor asks for more: twice, three times what you spend.
MONEY_GOAL_DAYS = 90
MONEY_WINDOW_DAYS = 30
DECISION_FRACTIONS = (0.25, 0.5)
DECISION_TITLE = "Decision point: go on, change or stop"
DECISION_MEASURE = (
    "You decided from the numbers which projects and ventures go on, change or stop, and closed this milestone done "
    "with that decision"
)
_CLEAR_PROPOSAL = ", ".join(f"{name} = NULL" for name in NO_PROPOSAL)
# 0.12.0: money and time on a milestone. What counts toward it: the calls that worked in the cycles aimed at it; plans,
# reviews, brainstorms, library study and the last will are overhead, charged to no milestone. A wait lasts at most
# WAIT_DAYS, and a waiting milestone isn't flagged overdue until its check is due.
WORK_PURPOSES = metering.WORK_PURPOSES
WAIT_DAYS = 14
WAIT_CHARS = 200


# --- dates ---


def parse_day(text: Any) -> date | None:
    """A date written YYYY-MM-DD, or None."""
    if not isinstance(text, str) or not _DATE.match(text.strip()):
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def horizon(due: date, today: date) -> str:
    """'overdue', 'week' (due in the next 7 days, today included), 'month' (30 days), 'quarter' (91) or 'later'."""
    days = (due - today).days
    if days < 0:
        return OVERDUE[0]
    for key, _, last in HORIZONS:
        if days <= last:
            return key
    return LATER[0]


def when(due: date, today: date) -> str:
    """How far off a date is: "today", "in 3 days", "1 day late"."""
    days = (due - today).days
    if days == 0:
        return "today"
    if days > 0:
        return "tomorrow" if days == 1 else f"in {days} days"
    late = -days
    return f"{late} day{'s' if late != 1 else ''} late"


def _day(d: date) -> str:
    return f"{d:%a} {d.isoformat()}"


def _due(row: Mapping[str, Any]) -> date:
    return parse_day(row["due"]) or date.max


# --- records ---


def get(conn: sqlite3.Connection, scope: AgentScope, milestone_id: int) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM milestones WHERE id = ? AND {where}", (milestone_id, *params)).fetchone()


def open_milestones(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The open milestones, the first due first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status = 'open' ORDER BY due, id", params
    ).fetchall()


def closed_since(conn: sqlite3.Connection, scope: AgentScope, since: str, limit: int = 12) -> list[sqlite3.Row]:
    """The milestones closed since ``since`` (a timestamp), the newest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status <> 'open' AND closed_at >= ?"
        " ORDER BY closed_at DESC, id DESC LIMIT ?",
        (*params, since, limit),
    ).fetchall()


def all_milestones(conn: sqlite3.Connection, scope: AgentScope, limit: int = 300) -> list[sqlite3.Row]:
    """The newest ``limit`` milestones, open or closed."""
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM milestones WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()


def count(conn: sqlite3.Connection, scope: AgentScope, status: str | None = None) -> int:
    where, params = scope.where()
    if status is None:
        return int(conn.execute(f"SELECT COUNT(*) FROM milestones WHERE {where}", params).fetchone()[0])
    row = conn.execute(f"SELECT COUNT(*) FROM milestones WHERE {where} AND status = ?", (*params, status))
    return int(row.fetchone()[0])


def placed(conn: sqlite3.Connection, scope: AgentScope) -> int:
    """0.15.0: the open milestones that take a place of MAX_OPEN: the agent's and the owner's."""
    where, params = scope.where()
    marks = ", ".join("?" for _ in PLACED_BY)
    row = conn.execute(
        f"SELECT COUNT(*) FROM milestones WHERE {where} AND status = 'open' AND created_by IN ({marks})",
        (*params, *PLACED_BY),
    )
    return int(row.fetchone()[0])


def open_by_title(conn: sqlite3.Connection, scope: AgentScope, title: str) -> sqlite3.Row | None:
    """An open milestone with this title, whatever its case and spacing."""
    wanted = " ".join(title.split()).lower()
    return next((r for r in open_milestones(conn, scope) if " ".join(r["title"].split()).lower() == wanted), None)


def children(conn: sqlite3.Connection, milestone_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM milestones WHERE parent_id = ? ORDER BY due, id", (milestone_id,)).fetchall()


def open_steps(conn: sqlite3.Connection, milestone_id: int) -> list[sqlite3.Row]:
    """The open milestones that lead to this one, directly or through others (the first due first)."""
    return conn.execute(
        "WITH RECURSIVE below(id) AS (SELECT id FROM milestones WHERE parent_id = ?"
        " UNION SELECT m.id FROM milestones m JOIN below b ON m.parent_id = b.id)"
        " SELECT * FROM milestones WHERE id IN below AND status = 'open' ORDER BY due, id",
        (milestone_id,),
    ).fetchall()


def drop_steps(
    conn: sqlite3.Connection, milestone_id: int, now: str, result: str, closed_by: str, cycle_id: int | None = None
) -> list[int]:
    """0.12.0: a dropped milestone's open steps are dropped with it (they stayed open and looked like goals of their
    own). Returns their numbers."""
    steps = open_steps(conn, milestone_id)
    for step in steps:
        conn.execute(
            "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_cycle_id = ?, closed_by = ?,"
            f" {_CLEAR_PROPOSAL}, updated_at = ? WHERE id = ?",
            (result[: LIMITS["result"]], now, cycle_id, closed_by, now, step["id"]),
        )
    return [int(step["id"]) for step in steps]


def leads_to(conn: sqlite3.Connection, milestone_id: int, parent_id: int) -> bool:
    """Whether ``parent_id`` is ``milestone_id`` or one of its descendants (linking them would make a loop)."""
    seen: set[int] = set()
    current: int | None = parent_id
    while current is not None and current not in seen:
        if current == milestone_id:
            return True
        seen.add(current)
        row = conn.execute("SELECT parent_id FROM milestones WHERE id = ?", (current,)).fetchone()
        current = row["parent_id"] if row else None
    return False


def create(
    conn: sqlite3.Connection,
    scope: AgentScope,
    *,
    title: str,
    measure: str,
    due: str,
    now: str,
    cycle_id: int | None = None,
    parent_id: int | None = None,
    venture_id: int | None = None,
    project_id: int | None = None,
    created_by: str = "agent",
    entered_by: str | None = None,
    metric: str | None = None,
    target: int | None = None,
    baseline: int | None = None,
    kind: str | None = None,
    budget_micros: int | None = None,
    cash_cents: int | None = None,
    owner_minutes: int | None = None,
    replaces: Mapping[str, Any] | None = None,
) -> int:
    """A new open milestone; one the owner adds is news for the agent (owner_action 'added'). With a metric (0.12.0),
    Ember's code checks it (metrics.grade)."""
    owner = created_by == "owner"
    columns = {
        "mode": scope.mode,
        "session": scope.session,
        "life_id": scope.life_id,
        "parent_id": parent_id,
        "venture_id": venture_id,
        "project_id": project_id,
        "created_cycle_id": cycle_id,
        "created_by": created_by,
        "entered_by": entered_by,
        "created_at": now,
        "updated_at": now,
        "title": title,
        "measure": measure,
        # 0.12.0: a replacement keeps the first date and the moves of the milestone it replaces (``replaced_moves``)
        "first_due": replaces["first_due"] if replaces is not None else due,
        "due": due,
        "moves": replaced_moves(replaces) if replaces is not None else 0,
        "replaces_id": replaces["id"] if replaces is not None else None,
        "owner_action": "added" if owner else None,
        "owner_at": now if owner else None,
        "owner_by": entered_by if owner else None,
        "owner_version": 1 if owner else 0,
        "metric": metric,
        "target": target,
        "baseline": baseline,
        "kind": kind,  # Ember's code's: 'money_goal', 'decision' or 'first_test' (0.12.0)
        "budget_micros": budget_micros,  # what it may cost (0.12.0): API spending, the owner's cash and time
        "cash_cents": cash_cents,
        "owner_minutes": owner_minutes,
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    cursor = conn.execute(f"INSERT INTO milestones ({names}) VALUES ({marks})", tuple(columns.values()))
    return int(cursor.lastrowid)


REPLACE_DAYS = 30  # a new milestone much like one dropped or missed this recently names it (0.12.0)
_TITLE_WORD = re.compile(r"[a-z0-9äöüß]+")


def replaced_moves(old: Mapping[str, Any]) -> int:
    """The moves a replacement starts with (0.12.0): a dropped milestone's and one more, as the replacement moves its
    date again; a missed one's as they were (the miss is on record, and a new attempt is honest). 0.23.2: one your
    owner or Ember's code dropped (a venture parked) as it was: the agent didn't move it."""
    agent = old["closed_by"] in (None, "agent")
    return int(old["moves"] or 0) + (1 if old["status"] == "dropped" and agent else 0)


def _words(title: str) -> set[str]:
    return set(_TITLE_WORD.findall(title.lower()))


def like_closed(conn: sqlite3.Connection, scope: AgentScope, title: str, today: date) -> sqlite3.Row | None:
    """0.12.0: a milestone of the agent's dropped or missed in the last REPLACE_DAYS whose title is much like
    ``title`` (most of their words shared): the one a new milestone of that title replaces."""
    words = _words(title)
    where, params = scope.where()
    since = (today - timedelta(days=REPLACE_DAYS)).isoformat()
    for row in conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND created_by = 'agent' AND status IN ('dropped', 'missed')"
        " AND closed_at >= ? ORDER BY closed_at DESC, id DESC",
        (*params, since),
    ):
        old = _words(row["title"])
        if words and old and len(words & old) / len(words | old) >= 0.6:
            return row
    return None


def money_goal_title(level: int) -> str:
    return {1: "Earn as much as you spend", 2: "Earn twice what you spend"}.get(
        level, f"Earn {level} times what you spend"
    )


def money_goal_measure(level: int) -> str:
    times = "at least" if level == 1 else f"at least {level} times"
    return (
        f"Over the last {MONEY_WINDOW_DAYS} days, the revenue recorded, less expenses, is {times} your API "
        "spending (Ember's code checks it)"
    )


def money_goal(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    """The open money goal Ember's code set, if any."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND kind = 'money_goal' AND status = 'open' ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()


def _money_level(conn: sqlite3.Connection, scope: AgentScope) -> int:
    """How much the next money goal asks for: once what you spend, then one more for every goal met."""
    where, params = scope.where()
    met = conn.execute(
        f"SELECT COUNT(*) FROM milestones WHERE {where} AND kind = 'money_goal' AND status = 'done'", params
    ).fetchone()[0]
    return 1 + int(met)


def keep_money_goal(
    conn: sqlite3.Connection,
    scope: AgentScope,
    today: date,
    now: str,
    earned: int,
    spent: int,
    runway_days: float | None,
) -> list[str]:
    """Settle the open money goal from the books (``earned``: revenue less expenses, ``spent``: API spending, both over
    the last MONEY_WINDOW_DAYS, in micros) and set a new one, with its decision points, while none is open. None is set
    once the owner dropped one: then the roadmap is theirs to shape. Returns what happened, for the events."""
    happened = []
    moving: list[int] = []  # the agent's and the owner's milestones that led to a closed goal: they lead to the next
    goal = money_goal(conn, scope)
    if goal is not None:
        level = _money_level(conn, scope)
        met = spent > 0 and earned >= level * spent
        if not met and _due(goal) >= today:
            return happened
        numbers = (
            f"Over the last {MONEY_WINDOW_DAYS} days: revenue less expenses ${earned / 1_000_000:.2f}, API spending "
            f"${spent / 1_000_000:.2f}."
        )
        status = "done" if met else "missed"
        update(conn, goal["id"], now, status=status, result=numbers, closed_at=now, closed_by="code")
        for step in children(conn, goal["id"]):
            if step["status"] != "open":
                continue
            if step["kind"] == "decision":  # its decision points: the next goal brings its own
                conn.execute(
                    "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code',"
                    " updated_at = ? WHERE id = ?",
                    (f"The money goal #{goal['id']} was closed {status}.", now, now, step["id"]),
                )
            else:
                moving.append(int(step["id"]))
        happened.append(f"Ember's code closed the money goal #{goal['id']} {status}: {numbers}")
    where, params = scope.where()
    dropped = conn.execute(
        f"SELECT 1 FROM milestones WHERE {where} AND kind = 'money_goal' AND status = 'dropped' LIMIT 1", params
    ).fetchone()
    if dropped is not None:  # 0.15.0: the goal and its decision points take none of the agent's or owner's places
        return happened
    level = _money_level(conn, scope)
    due = today + timedelta(days=MONEY_GOAL_DAYS)
    goal_id = create(
        conn,
        scope,
        title=money_goal_title(level),
        measure=money_goal_measure(level),
        due=due.isoformat(),
        now=now,
        created_by="code",
        kind="money_goal",
    )
    base = min(runway_days or MONEY_GOAL_DAYS, MONEY_GOAL_DAYS)
    last = 0
    for fraction in DECISION_FRACTIONS:
        days = min(MONEY_GOAL_DAYS - 1, max(3, last + 1, round(base * fraction)))
        last = days
        create(
            conn,
            scope,
            title=DECISION_TITLE,
            measure=DECISION_MEASURE,
            due=(today + timedelta(days=days)).isoformat(),
            now=now,
            parent_id=goal_id,
            created_by="code",
            kind="decision",
        )
    for step_id in moving:
        conn.execute("UPDATE milestones SET parent_id = ?, updated_at = ? WHERE id = ?", (goal_id, now, step_id))
    happened.append(f"Ember's code set the money goal #{goal_id} ({money_goal_title(level)}, due {due.isoformat()})")
    return happened


def update(conn: sqlite3.Connection, milestone_id: int, now: str, **columns: Any) -> None:
    """The agent's changes (the owner's go through ``owner_word``)."""
    if not columns or set(columns) - _COLUMNS:
        raise ValueError("unknown milestone columns")
    sets = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE milestones SET {sets}, updated_at = ? WHERE id = ?", (*columns.values(), now, milestone_id))


def owner_word(
    conn: sqlite3.Connection, milestone_id: int, now: str, action: str, comment: str | None, who: str | None
) -> list[int]:
    """The owner's note on a milestone, their dropping it (its open steps with it), or their answer to the agent's
    proposed date (accept moves the date, a counted move; reject keeps it): news for the agent again (a new
    owner_version). Returns the steps dropped with it."""
    if action not in ("note", "drop", "accept", "reject"):
        raise ValueError("unknown owner action")
    change = ""
    params: tuple[Any, ...] = ()
    why = f": {' '.join(comment.split())}" if comment else "."
    if action == "drop":
        change = f", status = 'dropped', result = ?, closed_at = ?, closed_by = 'owner', {_CLEAR_PROPOSAL}"
        params = (("Dropped by your owner" + why)[: LIMITS["result"]], now)
    elif action == "accept":
        change = f", due = proposed_due, moves = moves + 1, {_CLEAR_PROPOSAL}"
    elif action == "reject":
        change = f", {_CLEAR_PROPOSAL}"
    conn.execute(
        f"UPDATE milestones SET owner_action = ?, owner_comment = ?, owner_at = ?, owner_by = ?{change},"
        " owner_version = owner_version + 1, seen_cycle_id = NULL, updated_at = ? WHERE id = ?",
        (action, comment, now, who, *params, now, milestone_id),
    )
    if action != "drop":
        return []
    # 0.15.0: the first tests under it (a venture's, a product line's bars) go on, leading to no goal: the owner's drop
    # of the money goal brought them back one by one, and ended a backed venture's test without a word on it
    conn.execute(
        "UPDATE milestones SET parent_id = NULL, updated_at = ? WHERE parent_id = ? AND status = 'open'"
        " AND created_by = 'code' AND kind = 'first_test'",
        (now, milestone_id),
    )
    return drop_steps(conn, milestone_id, now, f"Dropped by your owner with #{milestone_id}{why}", "owner")


def closed_as(row: sqlite3.Row) -> str:
    """A closed milestone's status, "done (self-reported)" when only the agent's word says so (0.12.0)."""
    return (
        f"{row['status']} (self-reported)" if row["status"] == "done" and row["closed_by"] == "agent" else row["status"]
    )


def add_note(notes: str, cycle_id: int | None, note: str) -> str:
    """``notes`` with a stamped line added; the oldest drop off at the column's limit."""
    stamp = f"[#c{cycle_id}] " if cycle_id else ""
    return (notes + "\n" + stamp + " ".join(note.split())).strip()[-LIMITS["notes"] :]


def effort(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, tuple[int, int]]:
    """(cycles, cost in micros) of the calls that worked toward each milestone (0.12.0: each call names the milestone
    its work served; the whole cycle was charged, its plan too, and the overhead is in ``overhead``)."""
    rows = conn.execute(
        "SELECT c.milestone_id, COUNT(DISTINCT c.cycle_id), COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c"
        " JOIN cycles y ON y.id = c.cycle_id WHERE y.session = ? AND y.simulated = ? AND c.milestone_id IS NOT NULL"
        " GROUP BY c.milestone_id",
        (scope.session, 1 if scope.simulated else 0),
    ).fetchall()
    return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}


def overhead(conn: sqlite3.Connection, scope: AgentScope) -> int:
    """What no milestone is charged (0.12.0), in micros: plans, reviews, brainstorms, library study, the last will, and
    the work of cycles aimed at none."""
    row = conn.execute(
        "SELECT COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
        " WHERE y.session = ? AND y.simulated = ? AND c.milestone_id IS NULL",
        (scope.session, 1 if scope.simulated else 0),
    ).fetchone()
    return int(row[0])


def waiting(row: Mapping[str, Any], today: date) -> bool:
    """Whether a milestone waits (0.12.0): for something named, until its check is due."""
    check = parse_day(_column(row, "check_at"))
    return bool(_column(row, "wait_for")) and check is not None and check > today


def check_due(row: Mapping[str, Any], today: date) -> bool:
    """Whether a milestone that waited has its check due (0.12.0)."""
    check = parse_day(_column(row, "check_at"))
    return bool(_column(row, "wait_for")) and check is not None and check <= today


# --- what the agent is shown ---


def _one_line(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _q(text: Any, limit: int | None = None) -> str:
    return json.dumps(_one_line(text, limit) if limit else str(text or ""), ensure_ascii=False)


def _links(row: Mapping[str, Any], open_ids: set[int] | None = None) -> str:
    parts = []
    if row["venture_id"]:
        parts.append(f"venture #{row['venture_id']}")
    if row["project_id"]:
        parts.append(f"project #{row['project_id']}")
    if row["parent_id"] and (open_ids is None or row["parent_id"] in open_ids):
        parts.append(f"leads to #{row['parent_id']}")
    return "".join(f" · {p}" for p in parts)


def _codes(row: Mapping[str, Any]) -> str:
    """0.24.0: what the daily review can't change about a milestone: Ember's code checks it (and closes it from its
    metric), or set it (its date doesn't move). Live, a review extended six bars of listing tests, all refused."""
    parts = (["Ember's code checks and closes it"] if _column(row, "metric") else []) + (
        ["its date doesn't move"] if row["created_by"] == "code" else []
    )
    return "".join(f", {part}" for part in parts)


def _moved(row: Mapping[str, Any]) -> str:
    moves = int(row["moves"] or 0)
    if not moves:
        return ""
    return f" · moved {moves} time{'s' if moves != 1 else ''} (first due {row['first_due']})"


def _proposed(row: Mapping[str, Any]) -> str:
    """A proposed date waiting for the owner (0.12.0), as a short clause ("" if none)."""
    return f" · you proposed moving it to {row['proposed_due']} (your owner decides)" if row["proposed_due"] else ""


CODE_SET = "set by Ember's code (its date doesn't move)"


def owner_said(row: Mapping[str, Any]) -> str:
    """The owner's part in a milestone, as a short clause ("" if none); Ember's code's milestone says so (0.12.0)."""
    if (
        row["created_by"] == "code"
    ):  # 0.24.0: its date doesn't move (milestone_update refuses it; live, every plan meant to)
        return f" · {CODE_SET}" + (
            f", your owner's note: {_q(row['owner_comment'], 160)}" if row["owner_comment"] else ""
        )
    if row["created_by"] != "owner" and not row["owner_comment"]:
        return ""
    words = "your owner's milestone" if row["created_by"] == "owner" else "your owner's note"
    comment = f": {_q(row['owner_comment'], 160)}" if row["owner_comment"] else ""
    return f" · {words}{comment}"


def _money(row: Mapping[str, Any], spent: Mapping[int, int] | None) -> str:
    """What a milestone may cost and what its work spent so far (0.12.0), as a short clause ("" if nothing is set)."""
    parts = []
    budget = _column(row, "budget_micros")
    if budget:
        used = (spent or {}).get(int(row["id"]), 0)
        parts.append(f"spent ${used / 1_000_000:.2f} of ${budget / 1_000_000:.2f}")
    needs = []
    if _column(row, "cash_cents"):
        needs.append(f"{row['cash_cents'] / 100:.2f} EUR")
    if _column(row, "owner_minutes"):
        needs.append(f"{row['owner_minutes'] / 60:g} h")
    if needs:
        parts.append(f"needs {' and '.join(needs)} from your owner")
    return "".join(f" · {part}" for part in parts)


def _waits(row: Mapping[str, Any], today: date) -> str:
    """A milestone's wait (0.12.0), as a short clause ("" if it doesn't wait)."""
    if waiting(row, today):
        return f" · waiting for {_q(row['wait_for'], 80)} until {row['check_at']}"
    if check_due(row, today):
        return f" · its check is due: it waited for {_q(row['wait_for'], 80)}"
    return ""


def _last_note(row: Mapping[str, Any]) -> str:
    """A milestone's newest note (0.12.0: the planner never saw its notes), as a short clause."""
    notes = str(_column(row, "notes") or "").strip()
    return f" · last note: {_q(notes.splitlines()[-1], 100)}" if notes else ""


def _checked(row: Mapping[str, Any]) -> str:
    """How Ember's code checks a milestone with a metric, and where it stands (0.12.0), as a short clause."""
    text = metrics.status_text(row)
    return f" · {text}" if text else ""


def _replaces(row: Mapping[str, Any]) -> str:
    """The milestone a replacement stands for (0.12.0), as a short clause."""
    return f" · replaces #{row['replaces_id']}" if _column(row, "replaces_id") else ""


def _unlocked(row: Mapping[str, Any], unlocks: Mapping[int, str] | None) -> str:
    """0.16.3 (analysis bug 5): what the owner's unlocks that stand let Ember's code carry out for a milestone without
    their click (``unlocks``: policy.unlocked_text by milestone, short), as a short clause ("" if none stands). From the
    grants, never from a note: 0.13.0's note said "Unlocked" after every take-back."""
    text = (unlocks or {}).get(int(row["id"]), "")
    return f" · your owner unlocked: {text}" if text else ""


def _stake(row: Mapping[str, Any]) -> str:
    """0.16.3 (analysis bug 1): a backed venture's first test, as a short clause: the last day it can be met (its date
    and a week's grace), and what Ember's code does then ("" for any other milestone)."""
    ends = ventures.test_ends(row)
    return f" · unmet by {ends}, Ember's code parks venture #{row['venture_id']}" if ends else ""


def milestone_line(
    row: Mapping[str, Any],
    today: date,
    detail: bool,
    open_ids: set[int] | None = None,
    spent: Mapping[int, int] | None = None,
    unlocks: Mapping[int, str] | None = None,
) -> str:
    """One milestone for the planner: its number, title, date, and (with ``detail``) its measure and newest note; with
    a metric, where it stands; what it may cost and spent (``spent``: its work's cost by milestone), and its wait;
    (0.16.3) what stands unlocked for it (``unlocks``)."""
    due = _due(row)
    line = f"#{row['id']} {_q(row['title'], 100)} · due {_day(due)} ({when(due, today)})"
    if detail and not _column(row, "metric"):
        line += f" · measure: {_q(row['measure'], 160)}"
    line += _checked(row) + _stake(row) + _money(row, spent) + _waits(row, today)
    line += _links(row, open_ids) + _moved(row) + _replaces(row) + _proposed(row) + owner_said(row)
    line += _unlocked(row, unlocks)
    return line + (_last_note(row) if detail else "")


def _column(row: Mapping[str, Any], name: str) -> Any:
    """A column that rows built by hand (tests, older callers) may not have."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def checks(rows: list[Mapping[str, Any]], today: date, spent: Mapping[int, int] | None = None) -> list[str]:
    """Ember's code's notes on the roadmap's shape, for the planner: empty, overdue (not what waits, 0.12.0, nor a
    venture's first test in its week of grace, 0.16.3), checks due, spending over a milestone's budget, nothing
    due this week, nothing planned beyond this month."""
    if not rows:
        return [
            "Roadmap check: your roadmap is empty. Plan a step that lays it out with milestone_plan: 1 to 3 goals "
            "for the next three months (what you will earn, and the legs and ventures that bring it), the milestones "
            "this month that lead to them, and this week's."
        ]
    notes = []
    kinds = [horizon(_due(r), today) for r in rows]
    overdue = [
        r
        for r, k in zip(rows, kinds, strict=True)
        if k == OVERDUE[0] and not waiting(r, today) and not ventures.is_first_test(r)  # 0.16.3 (analysis bug 1)
    ]
    if overdue:
        ids = ", ".join(f"#{r['id']}" for r in overdue[:6])
        notes.append(
            f"Roadmap check: {len(overdue)} milestone{'s are' if len(overdue) != 1 else ' is'} overdue ({ids}). Close "
            "each with milestone_update: done if its measure is met (with the evidence), missed if not (why, and "
            "what now); or move its date with the reason, if it is still worth reaching."
        )
    checked = [r for r in rows if check_due(r, today)]
    if checked:
        notes.append(
            f"Roadmap check: the check of {', '.join(f'#{r["id"]}' for r in checked[:6])} is due: see whether what it "
            "waited for came, then close it, move it or wait again (milestone_update: wait_for, check_at)."
        )
    over = [
        r for r in rows if _column(r, "budget_micros") and (spent or {}).get(int(r["id"]), 0) > int(r["budget_micros"])
    ]
    for r in over[:3]:
        notes.append(
            f"Roadmap check: #{r['id']} spent ${(spent or {})[int(r['id'])] / 1_000_000:.2f} of its "
            f"${r['budget_micros'] / 1_000_000:.2f}: go on only if it is still worth it (say why in a note), or drop "
            "it."
        )
    if "week" not in kinds:
        notes.append(
            "Roadmap check: nothing is due this week. Add this week's milestone: the next step toward your nearest "
            "goal."
        )
    if not any(k in ("quarter", LATER[0]) for k in kinds):
        notes.append("Roadmap check: nothing is planned beyond this month. Add a goal for the next three months.")
    return notes


def goal_line(
    row: Mapping[str, Any],
    today: date,
    spent: Mapping[int, int] | None = None,
    unlocks: Mapping[int, str] | None = None,
) -> str:
    """A goal for the plan, compact on one line (0.12.0: the ROADMAP's cut took every goal)."""
    due = _due(row)
    links = "".join(f" · {name} #{row[f'{name}_id']}" for name in ("venture", "project") if row[f"{name}_id"])
    said = ""
    if row["created_by"] == "owner":
        said = " · your owner's" + (f": {_q(row['owner_comment'], 60)}" if row["owner_comment"] else "")
    elif row["created_by"] == "code":
        said = f" · {CODE_SET}"
    measure = _checked(row) or f" · measure: {_q(row['measure'], 90)}"
    return (
        f"#{row['id']} {_q(row['title'], 70)} · due {_day(due)} ({when(due, today)}){measure}{_stake(row)}"
        f"{_money(row, spent)}"
        f"{_waits(row, today)}{links}{_moved(row)}{_replaces(row)}{_proposed(row)}{said}{_unlocked(row, unlocks)}"
    )


def code_closed(closed: list[Mapping[str, Any]], since: str | None) -> list[str]:
    """0.12.0: what Ember's code closed from its records since ``since`` (the end of the last cycle): a done to build
    on, a miss to decide on (0.15.0: a missed bar of a product line's listing test has its action in OBLIGATIONS)."""
    fresh = [
        r
        for r in closed
        if _column(r, "closed_by") == "code"
        and r["status"] in ("done", "missed")
        and (since is None or r["closed_at"] >= since)
    ]
    if not fresh:
        return []
    listed = "; ".join(f"#{r['id']} {_q(r['title'], 60)} {r['status']}" for r in fresh[:4])
    missed = [r for r in fresh if r["status"] == "missed"]
    bars = [  # a product line's (a venture's first test has the venture's number)
        r
        for r in missed
        if (_column(r, "created_by"), _column(r, "kind")) == ("code", "first_test") and _column(r, "project_id")
    ]
    note = " For a miss, decide what now: aim again (a new milestone), change the approach, or let it go."
    return [
        f"Roadmap check: since your last cycle, Ember's code closed from its records: {listed}."
        + (note if len(bars) < len(missed) else "")
        + (" For a missed bar of a listing test, do what OBLIGATIONS says." if bars else "")
    ]


def planner_text(
    rows: list[Mapping[str, Any]],
    closed: list[Mapping[str, Any]],
    today: date,
    since: str | None = None,
    spent: Mapping[int, int] | None = None,
    unlocks: Mapping[int, str] | None = None,
) -> str:
    """The ROADMAP section: a count by horizon and the checks (what Ember's code closed since ``since`` among them),
    then the goals (the open milestones that lead to no other: what the rest is for) one line each, so a cut never
    takes them (0.12.0: with 18 milestones, the cut took all 3 goals at every budget); then the other open milestones
    by horizon (the measure shown for what is overdue or due this week), and what was closed lately. 0.16.3 (analysis
    bug 5): each open milestone says what stands unlocked for it (``unlocks``: policy.unlocked_text by milestone)."""
    open_ids = {int(r["id"]) for r in rows}
    goals = [r for r in rows if r["parent_id"] not in open_ids]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        groups.setdefault(horizon(_due(r), today), []).append(r)
    labels = [OVERDUE, *((k, label) for k, label, _ in HORIZONS), LATER]
    tally = ", ".join(f"{len(groups[k])} {label.lower()}" for k, label in labels if groups.get(k))
    head = f"Today: {today:%A} {today.isoformat()}. " + (
        f"{len(rows)} open milestone{'s' if len(rows) != 1 else ''}: {tally}." if rows else "No open milestones."
    )
    lines = [head, *checks(rows, today, spent), *code_closed(closed, since)]
    if goals:
        lines.append("Goals (the rest leads to them):")
        lines.extend(goal_line(r, today, spent, unlocks) for r in goals)
    ends = {key: today + timedelta(days=last) for key, _, last in HORIZONS}
    for key, label in labels:
        members = [r for r in groups.get(key, []) if r["parent_id"] in open_ids]  # the goals are listed above
        if not members:
            continue
        lines.append(f"{label} (to {_day(ends[key])}):" if key in ends else f"{label}:")
        for r in members:
            lines.append(milestone_line(r, today, key in (OVERDUE[0], "week"), open_ids, spent, unlocks))
    if closed:
        done = "; ".join(
            f"#{r['id']} {_q(r['title'], 60)} {closed_as(r)} {str(r['closed_at'])[:10]}"
            + (f": {_q(r['result'], 100)}" if r["status"] != "done" and r["result"] else "")
            for r in closed[:6]
        )
        lines.append(f"Closed in the last {CLOSED_DAYS} days: {done}.")
    return "\n".join(lines)


def focus_text(
    row: Mapping[str, Any],
    today: date,
    parent: Mapping[str, Any] | None,
    spent: Mapping[int, int] | None = None,
    replaced: Mapping[str, Any] | None = None,
    last: str = "",
    unlocked: str = "",
) -> str:
    """The brief's FOCUS for the plan's milestone: what it takes to be done and how to close it first (a cut takes
    the end), then (0.12.0) the digest of the last cycle aimed at it (``last``), what it leads to and serves, the
    owner's word, (0.16.3, analysis bug 5) what stands unlocked for it (``unlocked``: policy.unlocked_text) and the
    notes."""
    due = _due(row)
    checked = metrics.status_text(row)
    ends = ventures.test_ends(row)
    if ends is not None:  # 0.16.3 (analysis bug 1): a backed venture's first test: a week's grace, then the park
        how = (
            (
                f"{checked}. It closes it done once met."
                if checked
                else "Measure met: send your owner the evidence; their drop confirms it."
            )
            + f" Unmet by {ends} (a week after its date), Ember's code closes it missed and parks venture "
            f"#{row['venture_id']}. Its date never moves, and only your owner drops it."
        )
    elif checked:
        how = (
            f"{checked}. It closes it done once met, missed if its date passes first. Out of reach by its date: move "
            "it (why; twice at most), or drop it (why; your odds on it count as missed)."
        )
    else:
        how = (
            "Measure met: send your owner the evidence; their drop confirms it."
            if dict(row).get("kind") == "first_test"
            else "Measure met: close it done, with the evidence."
        ) + (
            " Out of reach by its date: move it (why; twice at most, and your owner decides on theirs), or close it "
            "missed once the date has passed."
        )
    lines = [
        f"Focus milestone: #{row['id']} {_q(row['title'])} [{row['status']}] · due {_day(due)} ({when(due, today)})"
        + _moved(row)
        + _proposed(row),
        f"Measure of done: {_q(row['measure'])}",
        how,
    ]
    if last:
        lines.append(f"Its last cycle (Ember's code's digest): {last}")
    if parent is not None:
        lines.append(f"Leads to: #{parent['id']} {_q(parent['title'], 100)} (due {parent['due']}, {parent['status']})")
    if replaced is not None:  # 0.12.0: what it stands for, and what that one asked
        lines.append(
            f"Replaces: #{replaced['id']} {_q(replaced['title'], 80)} ({replaced['status']} "
            f"{str(replaced['closed_at'] or '')[:10]}), whose measure was {_q(replaced['measure'], 160)}"
        )
    serves = _links({**dict(row), "parent_id": None})
    if serves:
        lines.append(f"Serves: {serves.removeprefix(' · ')}")
    said = owner_said(row)
    if said:
        lines.append(f"Owner: {said.removeprefix(' · ')}")
    if unlocked:
        lines.append(f"Unlocked by your owner (Ember's code carries these out without their click): {unlocked}")
    money = _money(row, spent)
    if money:
        lines.append(f"Money and time: {money.removeprefix(' · ')}")
    waits = _waits(row, today)
    if waits:
        lines.append(f"Wait: {waits.removeprefix(' · ')}")
    if row["notes"]:
        lines.append(f"Notes: {_one_line(row['notes'][-300:], 300)}")
    return "\n".join(lines)


def news_line(row: Mapping[str, Any]) -> str:
    """The owner's latest word on a milestone, for FROM YOUR OWNER (texts JSON-quoted, like every owner line)."""
    name = f"milestone #{row['id']} {_q(row['title'])}"
    action = row["owner_action"]
    if action == "added":
        parent = f", leading to #{row['parent_id']}" if row["parent_id"] else ""
        line = (
            f"Your owner put {name} on your roadmap{parent}, due {row['due']}: done when {_q(row['measure'])}. Plan "
            "toward it"
        )
    elif action == "drop":
        line = f"Your owner dropped {name}: stop working toward it"
    elif action == "accept":
        line = f"Your owner accepted your proposed date: {name} is due {row['due']} now"
    elif action == "reject":
        line = f"Your owner kept the date of {name}: it stays due {row['due']}"
    else:
        line = f"Your owner wrote a note on {name}"
    if row["owner_comment"]:
        line += f". Owner's comment: {_q(row['owner_comment'])}"
    return line + "."


def review_text(conn: sqlite3.Connection, scope: AgentScope, today: date, since: str) -> str:
    """The scorecard's ROADMAP: what is overdue, due this week and planned further out, and what was closed and
    moved (``since``: the start of the period)."""
    rows = open_milestones(conn, scope)
    closed = closed_since(conn, scope, since, limit=50)
    if not rows and not closed:
        return "ROADMAP\nYour roadmap is empty: nothing is planned ahead."
    kinds = [horizon(_due(r), today) for r in rows]
    overdue = [r for r, k in zip(rows, kinds, strict=True) if k == OVERDUE[0]]
    week = [r for r, k in zip(rows, kinds, strict=True) if k == "week"]
    month = [r for r, k in zip(rows, kinds, strict=True) if k == "month"]  # 0.19.2: they were left out
    further = [r for r, k in zip(rows, kinds, strict=True) if k in ("quarter", LATER[0])]
    split: dict[str, int] = {}
    for r in closed:
        split[r["status"]] = split.get(r["status"], 0) + 1
    ended = ", ".join(f"{n} {status}" for status, n in split.items()) or "none closed"
    lines = [f"ROADMAP ({len(rows)} open, {len(overdue)} overdue; in the period: {ended})"]

    def listed(members: list[sqlite3.Row]) -> str:
        return ", ".join(
            f"#{r['id']} {_q(r['title'], 60)} ({r['due']}{_moved(r).replace(' · ', ', ')}{_codes(r)})"
            for r in members[:6]
        )

    if overdue:
        lines.append(f"- overdue: {listed(overdue)}")
    lines.append(f"- due in the next 7 days: {listed(week) or 'nothing'}")
    lines.append(f"- due later this month: {listed(month) or 'nothing'}")
    lines.append(f"- planned beyond this month: {listed(further) or 'nothing'}")
    if closed:
        lines.append(
            "- closed in the period: "
            + "; ".join(f"#{r['id']} {_q(r['title'], 50)} {closed_as(r)}: {_q(r['result'], 120)}" for r in closed[:6])
        )
    return "\n".join(lines)
