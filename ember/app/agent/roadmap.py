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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
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
# it is met, missed after its date) and sets the next one (0.12.0 to 0.34.0: with decision points at a quarter and at
# half of the runway; 0.35.0: the plan tree's decide-by dates ask about each product). A met goal's successor asks for
# more: twice, three times what you spend.
MONEY_GOAL_DAYS = 90
MONEY_WINDOW_DAYS = 30
DECISION_TITLE = "Decision point: go on, change or stop"  # the goal's decision points until 0.35.0 (old rows)
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


# 0.35.0: a product's own milestone (plan.product_milestone) carries the owner's Autonomy unlocks for it: no reader of
# the roadmap shows it
SHOWN = "product_node IS NULL"


def open_milestones(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The open milestones, the first due first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND {SHOWN} AND status = 'open' ORDER BY due, id", params
    ).fetchall()


def of_line(milestone: Mapping[str, Any], line: int, venture_id: int | None) -> bool:
    """0.28.0: a milestone of a product line: its own, or its venture's without a project of its own (a backed
    venture's first test)."""
    if milestone["project_id"] is not None:
        return milestone["project_id"] == line
    return venture_id is not None and milestone["venture_id"] == venture_id


def serves_line(milestone: Mapping[str, Any], line: int, venture_id: int | None) -> bool:
    """0.28.0: whether a cycle on product ``line`` (of venture ``venture_id``) may aim at a milestone: the line's own,
    its venture's, or one of no line (a goal: the money goal and its decision points serve every line)."""
    return milestone["project_id"] in (None, line) and milestone["venture_id"] in (None, venture_id)


def line_milestone(conn: sqlite3.Connection, scope: AgentScope, line: int, today: date) -> int | None:
    """0.28.0: a product line's open milestone due first that doesn't wait (``of_line``), None without one."""
    row = conn.execute("SELECT venture_id FROM projects WHERE id = ?", (line,)).fetchone()
    venture_id = row["venture_id"] if row is not None else None
    for m in open_milestones(conn, scope):
        if not waiting(m, today) and of_line(m, line, venture_id):
            return int(m["id"])
    return None


def closed_since(conn: sqlite3.Connection, scope: AgentScope, since: str, limit: int = 12) -> list[sqlite3.Row]:
    """The milestones closed since ``since`` (a timestamp), the newest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND {SHOWN} AND status <> 'open' AND closed_at >= ?"
        " ORDER BY closed_at DESC, id DESC LIMIT ?",
        (*params, since, limit),
    ).fetchall()


def all_milestones(conn: sqlite3.Connection, scope: AgentScope, limit: int = 300) -> list[sqlite3.Row]:
    """The newest ``limit`` milestones, open or closed."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND {SHOWN} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def count(conn: sqlite3.Connection, scope: AgentScope, status: str | None = None) -> int:
    where, params = scope.where()
    if status is None:
        return int(conn.execute(f"SELECT COUNT(*) FROM milestones WHERE {where} AND {SHOWN}", params).fetchone()[0])
    row = conn.execute(f"SELECT COUNT(*) FROM milestones WHERE {where} AND {SHOWN} AND status = ?", (*params, status))
    return int(row.fetchone()[0])


def placed(conn: sqlite3.Connection, scope: AgentScope) -> int:
    """0.15.0: the open milestones that take a place of MAX_OPEN: the agent's and the owner's (0.29.0: not the
    owner's goal)."""
    where, params = scope.where()
    marks = ", ".join("?" for _ in PLACED_BY)
    row = conn.execute(
        f"SELECT COUNT(*) FROM milestones WHERE {where} AND {SHOWN} AND status = 'open' AND owner_goal = 0"
        f" AND created_by IN ({marks})",
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
    owner_goal: bool = False,
    counts_from: str | None = None,
    comment: str | None = None,
    replaces_id: int | None = None,
) -> int:
    """A new open milestone; one the owner adds is news for the agent (owner_action 'added', with their ``comment``).
    With a metric (0.12.0), Ember's code checks it (metrics.grade). 0.29.0: ``owner_goal``, the owner's goal at the
    root (``set_goal``), counting from ``counts_from``; ``replaces_id``, the goal it took the place of."""
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
        "replaces_id": replaces["id"] if replaces is not None else replaces_id,
        "owner_action": "added" if owner else None,
        "owner_comment": comment if owner else None,
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
        "owner_goal": 1 if owner_goal else 0,
        "counts_from": counts_from,
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


def money_needed(conn: sqlite3.Connection, scope: AgentScope, spent: int) -> int:
    """0.29.0: what the open money goal asks the last 30 days to bring (revenue less expenses, micros): its level
    times the API spending ``spent`` in them."""
    return _money_level(conn, scope) * spent


def _goal_decision(row: Mapping[str, Any]) -> bool:
    """A decision point of a goal (a venture's or a product line's scale point is linked to it, and isn't)."""
    return (
        _column(row, "kind") == "decision" and _column(row, "venture_id") is None and _column(row, "project_id") is None
    )


def _drop_decisions(conn: sqlite3.Connection, goal_id: int, now: str, result: str) -> list[int]:
    """The open decision points of goal ``goal_id``, dropped by Ember's code: a goal brings its own."""
    dropped = []
    for step in children(conn, goal_id):
        if step["status"] == "open" and _goal_decision(step):
            conn.execute(
                "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code',"
                " updated_at = ? WHERE id = ?",
                (result[: LIMITS["result"]], now, now, step["id"]),
            )
            dropped.append(int(step["id"]))
    return dropped


def keep_money_goal(
    conn: sqlite3.Connection,
    scope: AgentScope,
    today: date,
    now: str,
    earned: int,
    spent: int,
    runway_days: float | None,
    settle: bool = True,
) -> list[str]:
    """The goal at the roadmap's root, kept before every plan (``earned``: revenue less expenses, ``spent``: API
    spending, both over the last MONEY_WINDOW_DAYS, in micros). 0.29.0: while the owner's goal stands, Ember's code
    keeps no money goal of its own (one still open gives way to it); otherwise it settles its money goal from the books
    (not without ``settle``: once a plan) and sets a new one while none is open (none once the owner dropped one: then
    the roadmap is theirs to shape). Then everything leads to the goal (``adopt``). 0.35.0: no decision points any
    more (the plan tree's decide-by dates ask the owner about each product). Returns what happened, for the events."""
    happened: list[str] = []
    mine = owner_goal(conn, scope)
    if mine is not None:
        money = money_goal(conn, scope)
        if money is not None:
            happened += _give_way(conn, money, int(mine["id"]), now)
        return happened + _adopted(conn, scope, int(mine["id"]), now)
    moving: list[int] = []  # the agent's and the owner's milestones that led to a closed goal: they lead to the next
    goal = money_goal(conn, scope)
    if goal is not None:
        level = _money_level(conn, scope)
        met = spent > 0 and earned >= level * spent
        if not settle or (not met and _due(goal) >= today):
            return happened + _adopted(conn, scope, int(goal["id"]), now)
        numbers = (
            f"Over the last {MONEY_WINDOW_DAYS} days: revenue less expenses ${earned / 1_000_000:.2f}, API spending "
            f"${spent / 1_000_000:.2f}."
        )
        status = "done" if met else "missed"
        update(conn, goal["id"], now, status=status, result=numbers, closed_at=now, closed_by="code")
        _drop_decisions(conn, goal["id"], now, f"The money goal #{goal['id']} was closed {status}.")
        moving += [int(step["id"]) for step in children(conn, goal["id"]) if step["status"] == "open"]
        happened.append(f"Ember's code closed the money goal #{goal['id']} {status}: {numbers}")
    where, params = scope.where()
    dropped = conn.execute(  # 0.29.0: the owner's drop (one that gave way to their goal was Ember's code's)
        f"SELECT 1 FROM milestones WHERE {where} AND kind = 'money_goal' AND status = 'dropped' AND closed_by = 'owner'"
        " LIMIT 1",
        params,
    ).fetchone()
    if dropped is not None:  # 0.15.0: the goal takes none of the agent's or owner's places
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
    for step_id in moving:
        conn.execute("UPDATE milestones SET parent_id = ?, updated_at = ? WHERE id = ?", (goal_id, now, step_id))
    happened.append(f"Ember's code set the money goal #{goal_id} ({money_goal_title(level)}, due {due.isoformat()})")
    return happened + _adopted(conn, scope, goal_id, now)


# --- the owner's goal (0.29.0) ---

# The owner's goal leads the roadmap: an amount to earn a month (revenue_month_usd: the last 30 days) or in total
# (revenue_verified_usd: from counts_from on), by a date, at its root. Ember's code checks it from the books
# (metrics.grade) and keeps everything else leading to it (``adopt``). While it stands, Ember's code keeps no money goal
# of its own; once it closes (met, missed, or removed by the owner), the money goal stands in for it until the owner
# sets the next one.
GOAL_METRICS = {"month": "revenue_month_usd", "total": "revenue_verified_usd"}
GOAL_DAYS_MIN = 7  # how soon the owner's goal can be due, at the earliest
PACE_MARGIN = 10  # points of progress ahead of, or behind, the share of its time gone: "ahead", "behind"


def owner_goal(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    """The owner's open goal, if any (one at a time: migration 0084)."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND owner_goal = 1 AND status = 'open' LIMIT 1", params
    ).fetchone()


def root(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    """The goal everything leads to: the owner's, or the money goal Ember's code keeps in its place (None: neither)."""
    return owner_goal(conn, scope) or money_goal(conn, scope)


def is_goal(row: Mapping[str, Any]) -> bool:
    """Whether a milestone is the owner's goal."""
    return bool(_column(row, "owner_goal"))


def is_root(row: Mapping[str, Any]) -> bool:
    """Whether a milestone is a goal at the root: the owner's, or Ember's code's money goal."""
    return is_goal(row) or _column(row, "kind") == "money_goal"


def title_q(row: Mapping[str, Any]) -> str:
    """A milestone's title, JSON-quoted and short, for a tool's words."""
    return _q(row["title"], 80)


def goal_per(row: Mapping[str, Any]) -> str | None:
    """'month' or 'total' for the owner's goal (its metric), else None."""
    metric = _column(row, "metric")
    return next((per for per, name in GOAL_METRICS.items() if name == metric), None) if is_goal(row) else None


def dollars(micros: int) -> str:
    """An amount as the owner writes it: $1,000, $12.50 or -$3."""
    sign, cents = ("-" if micros < 0 else ""), abs(int(micros)) // 10_000
    return f"{sign}${cents // 100:,}" if cents % 100 == 0 else f"{sign}${cents / 100:,.2f}"


def goal_title(per: str, target: int) -> str:
    return f"Earn {dollars(target)} {'a month' if per == 'month' else 'in total'}"


def goal_measure(per: str, target: int, counts_from: str | None) -> str:
    if per == "month":
        return (
            f"Over the last {MONEY_WINDOW_DAYS} days, the revenue recorded, less expenses, is at least "
            f"{dollars(target)} (Ember's code checks it from the books)"
        )
    return (
        f"From {counts_from} on, the revenue recorded, less expenses, adds up to at least {dollars(target)} (Ember's "
        "code checks it from the books)"
    )


def set_goal(
    conn: sqlite3.Connection,
    scope: AgentScope,
    *,
    per: str,
    target: int,
    due: date,
    today: date,
    now: str,
    who: str | None,
    comment: str | None = None,
) -> tuple[int, list[str]]:
    """The owner's goal at the roadmap's root: earn ``target`` (micros) a month or in total (``per``) by ``due``. One
    set in place of another closes that one (dropped by the owner) and takes over what led to it; a goal in total set
    in place of one in total keeps counting from that one's day. The money goal Ember's code kept gives way the same
    way. The decision points of either go with it: the keeper sets the new goal its own. Returns the goal's number and
    what happened."""
    if per not in GOAL_METRICS:
        raise ValueError("per is 'month' or 'total'")
    old = owner_goal(conn, scope)
    money = money_goal(conn, scope)
    counts_from = None
    if per == "total":
        kept = old["counts_from"] if old is not None and goal_per(old) == "total" else None
        counts_from = kept or today.isoformat()
    happened = []
    if old is not None:
        conn.execute(
            "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'owner', updated_at = ?"
            " WHERE id = ?",
            ("Your owner set a new goal in its place.", now, now, old["id"]),
        )
    goal_id = create(
        conn,
        scope,
        title=goal_title(per, target),
        measure=goal_measure(per, target, counts_from),
        due=due.isoformat(),
        now=now,
        created_by="owner",
        entered_by=who,
        metric=GOAL_METRICS[per],
        target=target,
        owner_goal=True,
        counts_from=counts_from,
        comment=comment,
        replaces_id=int(old["id"]) if old is not None else None,
    )
    if old is not None:
        _drop_decisions(conn, old["id"], now, f"Your owner set the goal #{goal_id} in place of #{old['id']}.")
        _move_steps(conn, int(old["id"]), goal_id, now)
        happened.append(f"the goal #{old['id']} gave way to #{goal_id}")
    if money is not None:
        happened += _give_way(conn, money, goal_id, now)
    _adopted(conn, scope, goal_id, now)
    return goal_id, happened


def _move_steps(conn: sqlite3.Connection, from_id: int, to_id: int, now: str) -> None:
    for step in children(conn, from_id):
        if step["status"] == "open":
            conn.execute("UPDATE milestones SET parent_id = ?, updated_at = ? WHERE id = ?", (to_id, now, step["id"]))


def _give_way(conn: sqlite3.Connection, money: Mapping[str, Any], goal_id: int, now: str) -> list[str]:
    """The money goal Ember's code kept, closed (dropped by Ember's code) in favour of the owner's goal ``goal_id``:
    its decision points go with it, what led to it leads to the owner's goal."""
    _drop_decisions(conn, money["id"], now, f"Your owner set their goal #{goal_id}: it leads the roadmap now.")
    _move_steps(conn, int(money["id"]), goal_id, now)
    conn.execute(
        "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code', updated_at = ?"
        " WHERE id = ?",
        (f"Your owner set their goal #{goal_id}: it takes the place of this one.", now, now, money["id"]),
    )
    return [f"Ember's code closed the money goal #{money['id']}: your owner's goal #{goal_id} takes its place"]


def _nearest_open(conn: sqlite3.Connection, parent_id: int | None) -> int | None:
    """The nearest open milestone at or above ``parent_id`` (None: there is none)."""
    seen: set[int] = set()
    current = parent_id
    while current is not None and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT id, status, parent_id FROM milestones WHERE id = ?", (current,)).fetchone()
        if row is None:
            return None
        if row["status"] == "open":
            return int(row["id"])
        current = row["parent_id"]
    return None


def adopt(conn: sqlite3.Connection, scope: AgentScope, root_id: int, now: str) -> list[int]:
    """0.29.0: everything leads to the goal ``root_id``. An open milestone that leads to no open one (to none, or to
    one that closed) is linked to the nearest open milestone above it, else to the goal (one Ember's code set, to the
    goal: migration 0084); a decision point of a goal that closed goes with that goal. Returns the milestones
    linked."""
    rows = open_milestones(conn, scope)
    open_ids = {int(r["id"]) for r in rows}
    linked = []
    for r in rows:
        if int(r["id"]) == root_id or is_root(r) or r["parent_id"] in open_ids:
            continue
        if r["parent_id"] is not None and _goal_decision(r):
            above = conn.execute("SELECT * FROM milestones WHERE id = ?", (r["parent_id"],)).fetchone()
            if above is not None and is_root(above):
                conn.execute(
                    "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code',"
                    " updated_at = ? WHERE id = ?",
                    (f"The goal #{above['id']} it led to was closed {above['status']}.", now, now, r["id"]),
                )
                continue
        target = root_id
        if r["created_by"] != "code":
            target = _nearest_open(conn, r["parent_id"]) or root_id
        conn.execute("UPDATE milestones SET parent_id = ?, updated_at = ? WHERE id = ?", (target, now, r["id"]))
        linked.append(int(r["id"]))
    return linked


def _adopted(conn: sqlite3.Connection, scope: AgentScope, root_id: int, now: str) -> list[str]:
    linked = adopt(conn, scope, root_id, now)
    if not linked:
        return []
    return [
        f"Ember's code linked {', '.join(f'#{i}' for i in linked[:8])} to the goal #{root_id}: everything leads to it"
    ]


# --- progress (0.29.0) ---


@dataclass(frozen=True)
class Progress:
    """How far a milestone got."""

    percent: int | None  # 0 to 100; None: not measured (a ceiling, a dropped or missed one without a metric)
    # 'metric': Ember's code's reading of its metric; 'books': the money goal's; 'steps': from what leads to it;
    # 'done'; 'open': no measure until it is done; 'dropped', 'missed'
    basis: str
    text: str = ""  # where it stands, in words: "$340 of $1,000", "2 of 5 steps done"
    pace: str | None = None  # open and measured: 'ahead', 'on pace' or 'behind' the share of its time gone
    elapsed: int | None = None  # that share, in percent (from the day it was set, or counts from)


def _metered(m: metrics.Metric, value: int | None, target: int) -> Progress:
    """A metric's reading against its target (a ceiling isn't progress: what it used, of what it may)."""
    amount = (lambda v: dollars(v)) if m.kind == "usd" else (lambda v: metrics.amount(m, v))
    if m.ceiling:
        return Progress(None, "metric", f"{amount(value or 0)} of {amount(target)} at most")
    if value is None:
        return Progress(0, "metric", "not checked yet")
    if m.kind == "yes":
        return Progress(100 if value else 0, "metric", "met" if value else "not yet")
    if m.kind == "stage":
        return Progress(
            min(100, max(0, value) * 100 // max(1, target)),
            "metric",
            f"{metrics.amount(m, value)}, {metrics.amount(m, target)} wanted",
        )
    of = f"{value:,} of {amount(target)}" if m.kind == "count" else f"{amount(value)} of {amount(target)}"
    return Progress(min(100, max(0, value) * 100 // max(1, target)), "metric", of)


def _paced(row: Mapping[str, Any], p: Progress, today: date) -> Progress:
    if row["status"] != "open" or p.percent is None or p.basis not in ("metric", "books", "steps"):
        return p
    start = parse_day(_column(row, "counts_from")) or parse_day(str(row["created_at"])[:10]) or today
    total = (_due(row) - start).days
    gone = 100 if total <= 0 else min(100, max(0, (today - start).days * 100 // total))
    pace = "ahead" if p.percent >= gone + PACE_MARGIN else "behind" if p.percent + PACE_MARGIN < gone else "on pace"
    return replace(p, pace=pace, elapsed=gone)


def progress(
    rows: Iterable[Mapping[str, Any]],
    today: date,
    money: tuple[int, int] | None = None,
    readings: Mapping[int, int] | None = None,
) -> dict[int, Progress]:
    """How far each milestone of ``rows`` got. One with a metric: its reading against its target (``readings``: newer
    ones than the rows', by milestone); the open money goal: ``money`` (revenue less expenses over the last 30 days,
    and what it needs); one without, that open steps or done ones lead to: their mean; one without either: nothing
    until it is done. An open, measured one says its pace: ahead of, on, or behind the share of its time gone."""
    rows = list(rows)
    below: dict[int, list[Mapping[str, Any]]] = {}
    for r in rows:
        if r["parent_id"] is not None:
            below.setdefault(int(r["parent_id"]), []).append(r)
    found: dict[int, Progress] = {}

    def of(r: Mapping[str, Any], seen: frozenset[int]) -> Progress:
        rid = int(r["id"])
        if rid not in found:
            found[rid] = _paced(r, measure(r, seen | {rid}), today)
        return found[rid]

    def measure(r: Mapping[str, Any], seen: frozenset[int]) -> Progress:
        status = r["status"]
        if status == "done":
            return Progress(100, "done")
        if status == "dropped":
            return Progress(None, "dropped")
        m = metrics.CATALOGUE.get(_column(r, "metric") or "")
        if m is not None:
            value = (readings or {}).get(int(r["id"]), _column(r, "progress"))
            return _metered(m, None if value is None else int(value), int(r["target"]))
        if status != "open":
            return Progress(None, status)
        if _column(r, "kind") == "money_goal":
            if money is None:
                return Progress(None, "books")
            earned, needed = money
            if needed <= 0:
                return Progress(0, "books", f"{dollars(earned)} earned; no API spending in the last 30 days")
            return Progress(
                min(100, max(0, earned) * 100 // needed), "books", f"{dollars(earned)} of {dollars(needed)}"
            )
        steps = [s for s in below.get(int(r["id"]), []) if s["status"] in ("open", "done") and int(s["id"]) not in seen]
        counted = [(s, of(s, seen)) for s in steps]
        counted = [(s, p) for s, p in counted if p.percent is not None]
        if not counted:
            return Progress(0, "open")
        done = sum(1 for s, _ in counted if s["status"] == "done")
        mean = round(sum(p.percent or 0 for _, p in counted) / len(counted))
        return Progress(
            min(100, mean), "steps", f"{done} of {len(counted)} step{'s' if len(counted) != 1 else ''} done"
        )

    for r in rows:
        of(r, frozenset())
    return found


def tree_rows(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The open milestones and the closed ones that lead to an open one: what progress is counted from."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND {SHOWN} AND (status = 'open' OR parent_id IN"
        f" (SELECT id FROM milestones WHERE {where} AND status = 'open')) ORDER BY due, id",
        (*params, *params),
    ).fetchall()


def progress_for(
    conn: sqlite3.Connection,
    scope: AgentScope,
    today: date,
    money: tuple[int, int] | None = None,
    readings: Mapping[int, int] | None = None,
) -> dict[int, Progress]:
    """``progress`` of the open milestones (``money``: the money goal's revenue less expenses and API spending over
    the last 30 days, as Ember's code's keeper reads them; its level makes what it needs)."""
    needed = (money[0], money_needed(conn, scope, money[1])) if money is not None else None
    return progress(tree_rows(conn, scope), today, needed, readings)


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
    owner_version). Returns the steps dropped with it. 0.29.0: a goal at the root (the owner's, or the money goal)
    takes only its decision points with it: what leads to it goes on, and leads to the goal that stands next."""
    if action not in ("note", "drop", "accept", "reject"):
        raise ValueError("unknown owner action")
    top = conn.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
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
    if top is not None and is_root(top):  # what led to it leads to no goal until the next one stands (``adopt``)
        conn.execute(
            "UPDATE milestones SET parent_id = NULL, updated_at = ? WHERE parent_id = ? AND status = 'open'"
            " AND NOT (kind IS 'decision' AND venture_id IS NULL AND project_id IS NULL)",
            (now, milestone_id),
        )
        dropped = [
            int(step["id"])
            for step in children(conn, milestone_id)
            if step["status"] == "open" and _goal_decision(step)
        ]
        for step_id in dropped:
            conn.execute(
                "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'owner',"
                " updated_at = ? WHERE id = ?",
                (f"Dropped by your owner with #{milestone_id}{why}"[: LIMITS["result"]], now, now, step_id),
            )
        return dropped
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


def _progressed(row: Mapping[str, Any], progress: Mapping[int, Progress] | None, full: bool = False) -> str:
    """0.29.0: how far a milestone got, as a short clause ("" when nothing measures it before it is done): the share,
    and for one measured by its steps or the books (``full``: by its metric too), where it stands; and its pace."""
    p = (progress or {}).get(int(row["id"]))
    if p is None or p.percent is None or p.basis not in ("metric", "books", "steps"):
        return ""
    pace = f", {p.pace}" if p.pace else ""
    if p.basis == "metric" and not full:  # where it stands is in Ember's code's check
        return f" · {p.percent}%{pace}"
    return f" · {p.percent}% ({p.text}{pace})"


def milestone_line(
    row: Mapping[str, Any],
    today: date,
    detail: bool,
    open_ids: set[int] | None = None,
    spent: Mapping[int, int] | None = None,
    unlocks: Mapping[int, str] | None = None,
    progress: Mapping[int, Progress] | None = None,
) -> str:
    """One milestone for the planner: its number, title, date, and (with ``detail``) its measure and newest note; with
    a metric, where it stands; what it may cost and spent (``spent``: its work's cost by milestone), and its wait;
    (0.16.3) what stands unlocked for it (``unlocks``); (0.29.0) how far it got (``progress``)."""
    due = _due(row)
    line = f"#{row['id']} {_q(row['title'], 100)} · due {_day(due)} ({when(due, today)})" + _progressed(row, progress)
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


def _top(rows: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The goal at the root among ``rows``: the owner's, else the money goal (0.29.0)."""
    rows = list(rows)
    return next((r for r in rows if is_goal(r)), None) or next(
        (r for r in rows if _column(r, "kind") == "money_goal"), None
    )


def goal_line(
    row: Mapping[str, Any],
    today: date,
    spent: Mapping[int, int] | None = None,
    unlocks: Mapping[int, str] | None = None,
    progress: Mapping[int, Progress] | None = None,
    detail: bool = False,
) -> str:
    """A goal for the plan, compact on one line (0.12.0: the ROADMAP's cut took every goal); 0.29.0: a sub-goal of the
    goal at the root, with how far it got, and (``detail``: overdue or due this week) its newest note."""
    due = _due(row)
    links = "".join(f" · {name} #{row[f'{name}_id']}" for name in ("venture", "project") if row[f"{name}_id"])
    said = ""
    if row["created_by"] == "owner":
        said = " · your owner's" + (f": {_q(row['owner_comment'], 60)}" if row["owner_comment"] else "")
    elif row["created_by"] == "code":
        said = f" · {CODE_SET}"
    measure = _checked(row) or f" · measure: {_q(row['measure'], 90)}"
    return (
        f"#{row['id']} {_q(row['title'], 70)} · due {_day(due)} ({when(due, today)}){_progressed(row, progress)}"
        f"{measure}{_stake(row)}{_money(row, spent)}"
        f"{_waits(row, today)}{links}{_moved(row)}{_replaces(row)}{_proposed(row)}{said}{_unlocked(row, unlocks)}"
        f"{_last_note(row) if detail else ''}"
    )


def root_line(row: Mapping[str, Any], today: date, progress: Mapping[int, Progress] | None = None) -> str:
    """0.29.0: the goal at the root, first in ROADMAP: the owner's, or the money goal standing in for it."""
    due = _due(row)
    p = (progress or {}).get(int(row["id"]))
    where = ""
    if p is not None and p.percent is not None:
        where = f" · {p.percent}%: {p.text}" + (f" ({p.elapsed}% of its time gone: {p.pace})" if p.pace else "")
    head = f"#{row['id']} {_q(row['title'])} · due {_day(due)} ({when(due, today)}){where}"
    if is_goal(row):
        said = f" Their word on it: {_q(row['owner_comment'], 200)}." if row["owner_comment"] else ""
        return (
            f"Your owner's goal: {head} · Ember's code checks it from the books; only your owner changes it. "
            f"Everything in your plan leads to it.{said}"
        )
    return (
        f"The goal (Ember's code's, until your owner sets theirs): {head} · {_q(row['measure'], 160)}. Everything in "
        "your plan leads to it."
    )


def focus_text(
    row: Mapping[str, Any],
    today: date,
    parent: Mapping[str, Any] | None,
    spent: Mapping[int, int] | None = None,
    replaced: Mapping[str, Any] | None = None,
    last: str = "",
    unlocked: str = "",
    goal: Mapping[str, Any] | None = None,
    progress: Mapping[int, Progress] | None = None,
) -> str:
    """The brief's FOCUS for the plan's milestone: what it takes to be done and how to close it first (a cut takes
    the end), (0.29.0) how far it got, then (0.12.0) the digest of the last cycle aimed at it (``last``), what it leads
    to, (0.29.0) the goal at the root (``goal``) and what it serves, the owner's word, (0.16.3, analysis bug 5) what
    stands unlocked for it (``unlocked``: policy.unlocked_text) and the notes."""
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
    elif is_goal(row):  # 0.29.0: the owner's goal itself
        how = (
            f"{checked}. It is your owner's goal: Ember's code closes it done once met, missed if its date passes "
            "first, and only your owner changes it. Work on what leads to it."
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
        + _proposed(row)
        + _progressed(row, progress),
        f"Measure of done: {_q(row['measure'])}",
        how,
    ]
    if last:
        lines.append(f"Its last cycle (Ember's code's digest): {last}")
    if parent is not None:
        lines.append(
            f"Leads to: #{parent['id']} {_q(parent['title'], 100)} (due {parent['due']}, {parent['status']})"
            + _progressed(parent, progress)
        )
    if goal is not None and goal["id"] not in (row["id"], parent["id"] if parent is not None else None):
        lines.append(
            f"Toward the goal: #{goal['id']} {_q(goal['title'], 100)} (due {goal['due']})"
            + _progressed(goal, progress, full=True)
        )
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
    if action == "added" and is_goal(row):  # 0.29.0
        replaced = f" in place of #{row['replaces_id']}" if _column(row, "replaces_id") else ""
        line = (
            f"Your owner set their goal{replaced}: {name}, due {row['due']}: done when {_q(row['measure'])}. "
            "Your plan works toward it now: YOUR PLAN starts with it"  # 0.35.0: milestone_plan retired
        )
    elif action == "drop" and is_goal(row):
        line = (
            f"Your owner removed their goal {name}: what led to it goes on, under the goal Ember's code sets in its "
            "place at your next plan"
        )
    elif action == "added":
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
    """The scorecard's ROADMAP: (0.29.0) the goal at the root and how far it got, what is overdue, due this week and
    planned further out, and what was closed and moved (``since``: the start of the period)."""
    rows = open_milestones(conn, scope)
    closed = closed_since(conn, scope, since, limit=50)
    if not rows and not closed:
        return "ROADMAP\nYour roadmap is empty: nothing is planned ahead."
    top = _top(rows)
    rows = [r for r in rows if r is not top]  # the goal isn't the review's to judge
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
    if top is not None:
        lines.append(f"- {root_line(top, today, progress_for(conn, scope, today))}")

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
