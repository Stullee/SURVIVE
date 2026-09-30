"""A venture's stages (0.12.0): the rule of each, kept by Ember's code.

A venture moved through its stages on the agent's word alone: research could go on without ever reaching a business
case, the business case's first test never became a milestone, and a killed venture's milestones stayed open. Now each
stage has a rule (``RULES``): what completes it, and when Ember's code parks the venture instead (its kill rule).

* researching: a business case from research (proposed). Parked when there is none RESEARCH_DAYS after its research
  in the stage began (its first research call), while nothing is built for it (no open project). A venture no one
  researches waits: the seeded Etsy leg sits in researching until its first listing.
* proposed: the owner's decision (back, park or kill).
* building: when the owner backs a venture, its first test becomes a milestone Ember's code sets (``first_test``, due
  in FIRST_TEST_DAYS, its date fixed); the venture goes live once that is met (the database refuses it before), and is
  parked when it is missed (closed missed, or still open FIRST_TEST_GRACE_DAYS after its date).

Ember's code parks reversibly (``parked_by`` 'code'): only the owner takes such a venture up again, and only the owner
backs or kills one (migration 0030). A venture parked or killed takes its open milestones with it
(``drop_milestones``): all of them when the owner parked or killed it, else all but the owner's.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from ..economy.clock import from_iso
from . import roadmap, ventures
from .store import AgentScope

RESEARCH_DAYS = ventures.RESEARCH_DAYS
FIRST_TEST_DAYS = ventures.FIRST_TEST_DAYS
FIRST_TEST_GRACE_DAYS = 7
OPEN_PROJECTS = ("idea", "active", "waiting")
NO_TEST = (
    "Its first test is met: a small launch whose result shows whether it can earn (its business case names none yet)"
)
# 0.13.0 (Phase E2): a venture a channel of Ember's code serves has that channel's first test, a metric Ember's code
# checks (missed: the venture is parked, as any first test): its pins must bring buyers to the shop.
CHANNEL_TESTS = {
    "pinterest": ("pin_clicks", 10, "Its pins bring 10 clicks to the shop's listings (Pinterest's numbers)"),
    "printify": ("pod_orders", 1, "A buyer orders one of its products (Printify's records)"),  # Phase E4
}


def _day(stamp: str) -> date:
    return from_iso(stamp).date()


def first_test(conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], today: date, now: str) -> int:
    """The milestone of a backed venture's first test: set by Ember's code (its date never moves, only the owner drops
    it), due in FIRST_TEST_DAYS, leading to the money goal when that is due later, measured by the business case's
    first test. Returns its number."""
    due = today + timedelta(days=FIRST_TEST_DAYS)
    goal = roadmap.money_goal(conn, scope)
    parent = goal["id"] if goal is not None and goal["due"] >= due.isoformat() else None
    text = " ".join(str(venture["first_test"] or "").split())
    measure = f"Its first test is met: {text}" if text else NO_TEST
    metric, target = None, None
    channel = venture["channel"] if "channel" in venture.keys() else None  # noqa: SIM118 - a Row, not a dict
    if channel in CHANNEL_TESTS:
        metric, target, measure = CHANNEL_TESTS[channel]
    limit = roadmap.LIMITS["measure"]
    milestone_id = roadmap.create(
        conn,
        scope,
        title=f"First test: {' '.join(str(venture['title']).split())}"[: roadmap.LIMITS["title"]],
        measure=measure if len(measure) <= limit else measure[: limit - 1].rstrip() + "…",
        due=due.isoformat(),
        now=now,
        parent_id=parent,
        venture_id=int(venture["id"]),
        created_by="code",
        kind="first_test",
        metric=metric,
        target=target,
    )
    conn.execute(
        "UPDATE ventures SET test_milestone_id = ?, updated_at = ? WHERE id = ?", (milestone_id, now, venture["id"])
    )
    return milestone_id


def drop_milestones(
    conn: sqlite3.Connection, scope: AgentScope, venture_id: int, now: str, result: str, by: str
) -> list[int]:
    """A venture parked or killed takes its open milestones with it, and the open steps leading to them: all of them
    when the owner parked or killed it (``by`` 'owner'), else all but the owner's (which stay theirs to drop). Returns
    their numbers."""
    where, params = scope.where()
    linked = conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND venture_id = ? AND status = 'open' ORDER BY id",
        (*params, venture_id),
    ).fetchall()
    dropped: list[int] = []
    for row in [*linked, *(s for r in linked for s in roadmap.open_steps(conn, r["id"]))]:
        if row["id"] in dropped or (row["created_by"] == "owner" and by != "owner"):
            continue
        closed_by = "code" if by == "code" or (by == "agent" and row["created_by"] == "code") else by
        conn.execute(
            "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = ?, updated_at = ?,"
            " proposed_due = NULL, proposed_note = NULL, proposed_at = NULL, proposed_cycle_id = NULL"
            " WHERE id = ? AND status = 'open'",
            (result[: roadmap.LIMITS["result"]], now, closed_by, now, row["id"]),
        )
        dropped.append(int(row["id"]))
    return dropped


def park(conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], now: str, why: str) -> str:
    """Ember's code parks a venture by its stage's rule (``why``), reversibly: only the owner takes it up again. Its
    open milestones go with it, the owner's aside. Returns what happened, for the events."""
    vid = int(venture["id"])
    note = ventures.add_note(venture["notes"], None, f"Parked by Ember's code: {why}.")
    conn.execute(
        "UPDATE ventures SET stage = 'parked', parked_by = 'code', notes = ?, updated_at = ? WHERE id = ?",
        (note, now, vid),
    )
    dropped = drop_milestones(conn, scope, vid, now, f"Venture #{vid} was parked by Ember's code: {why}.", "code")
    also = f"; dropped with it: {', '.join(f'#{i}' for i in dropped)}" if dropped else ""
    return f"Ember's code parked venture #{vid} ({venture['title']}): {why}{also}"


def keep(conn: sqlite3.Connection, scope: AgentScope, today: date, now: str) -> list[str]:
    """Before every plan: a first test for each backed venture that has none, and the stages' rules (research without
    a business case, a missed first test). Returns what happened, for the events."""
    happened = []
    for v in ventures.all_ventures(conn, scope):
        if v["stage"] == "researching":
            if not v["research_from"] or (today - _day(v["research_from"])).days < RESEARCH_DAYS:
                continue
            building = conn.execute(
                f"SELECT 1 FROM projects WHERE venture_id = ? AND status IN {OPEN_PROJECTS} LIMIT 1", (v["id"],)
            ).fetchone()
            if building is None:
                why = f"no business case {RESEARCH_DAYS} days after its research began"
                happened.append(park(conn, scope, v, now, why))
            continue
        if v["stage"] != "building":
            continue
        test = roadmap.get(conn, scope, v["test_milestone_id"]) if v["test_milestone_id"] else None
        if test is None:
            made = first_test(conn, scope, v, today, now)
            happened.append(f"Ember's code set the first test of venture #{v['id']} as milestone #{made}")
            continue
        late = (today - (roadmap.parse_day(test["due"]) or today)).days
        if test["status"] == "open" and late > FIRST_TEST_GRACE_DAYS:
            conn.execute(
                "UPDATE milestones SET status = 'missed', result = ?, closed_at = ?, closed_by = 'code', updated_at = ?"
                " WHERE id = ? AND status = 'open'",
                (f"Still open {late} days after its date: Ember's code closed it missed.", now, now, test["id"]),
            )
            test = roadmap.get(conn, scope, test["id"])
        if test is not None and test["status"] == "missed":
            happened.append(park(conn, scope, v, now, f"its first test (milestone #{test['id']}) was missed"))
    return happened
