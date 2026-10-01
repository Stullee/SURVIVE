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
  parked when it is missed (closed missed, or still open FIRST_TEST_GRACE_DAYS after its date). 0.14.0: a venture a
  channel of Ember's code serves (CHANNEL_TESTS) gets its first test only once that channel is set up: its clock
  doesn't run while the owner hasn't connected it.
* idea (0.13.0, triage): an idea of the agent's is researched (researching) or parked within TRIAGE_DAYS of coming up;
  Ember's code parks it then. The owner's ideas wait for them.
* live (0.13.0, scale): a live venture that earns more than it costs (its P&L: revenue less expenses and the API calls
  that worked for it) gets a decision point Ember's code sets, to scale it (``scale``, due in SCALE_DAYS); one that has
  sold nothing (no revenue recorded, no Etsy order of its listings) LIVE_DAYS after it went live is parked.
For a venture already in the stage when a rule came, the rule counts from then (``rules_from``).

Ember's code parks reversibly (``parked_by`` 'code'): only the owner takes such a venture up again, and only the owner
backs or kills one (migration 0030). A venture parked or killed takes its open milestones with it
(``drop_milestones``): all of them when the owner parked or killed it, else all but the owner's.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping
from datetime import date, timedelta
from typing import Any

from ..economy.clock import from_iso
from . import metrics, roadmap, ventures
from .store import AgentScope

RESEARCH_DAYS = ventures.RESEARCH_DAYS
FIRST_TEST_DAYS = ventures.FIRST_TEST_DAYS
TRIAGE_DAYS = ventures.TRIAGE_DAYS  # 0.13.0
LIVE_DAYS = ventures.LIVE_DAYS
SCALE_DAYS = ventures.SCALE_DAYS
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


def sold(conn: sqlite3.Connection, scope: AgentScope, venture_id: int, paid: ventures.Money) -> bool:
    """0.13.0: whether a venture has sold anything: revenue recorded for it, or an Etsy order of its listings."""
    if paid.earned > 0:
        return True
    ours = {int(r["listing_id"]) for r in metrics.listings(conn, scope, None, venture_id)}
    return bool(ours) and bool(metrics.orders_of(conn, scope, ours, ""))


def scale(
    conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], paid: ventures.Money, today: date, now: str
) -> int:
    """0.13.0: the decision point of a live venture that earns more than it costs: scale what sells. A goal of its own
    in the plan, due in SCALE_DAYS, closed by the agent once that is under way. Returns its number."""
    title = f"Scale it: {' '.join(str(venture['title']).split())}"
    limit = roadmap.LIMITS["title"]
    milestone_id = roadmap.create(
        conn,
        scope,
        title=title if len(title) <= limit else title[: limit - 1].rstrip() + "…",
        measure=(
            f"It earns more than it costs ({ventures.money_text(paid)}): more of what sells (variants, bundles, "
            "another channel) is under way; you close it then"
        )[: roadmap.LIMITS["measure"]],
        due=(today + timedelta(days=SCALE_DAYS)).isoformat(),
        now=now,
        venture_id=int(venture["id"]),
        created_by="code",
        kind="decision",
    )
    conn.execute(
        "UPDATE ventures SET scale_milestone_id = ?, updated_at = ? WHERE id = ?", (milestone_id, now, venture["id"])
    )
    return milestone_id


def waits_for_channel(venture: Mapping[str, Any], ready: Collection[str]) -> bool:
    """0.14.0: whether a venture's first test waits for its channel to be set up (``ready``: the channels that are)."""
    channel = venture["channel"] if "channel" in venture.keys() else None  # noqa: SIM118 - a Row, not a dict
    return channel in CHANNEL_TESTS and channel not in ready


def restart_test(
    conn: sqlite3.Connection, scope: AgentScope, venture: Mapping[str, Any], test: Mapping[str, Any], now: str
) -> str:
    """0.14.0: a channel venture's open first test, set before the owner had set its channel up (as 0.13.0 did when
    they backed it), is dropped: its date can't move, and its clock shouldn't run until then. ``keep`` sets a new one
    once the channel is set up. Returns what happened, for the events."""
    vid, name = int(venture["id"]), str(venture["channel"]).capitalize()
    why = f"{name} isn't set up yet: a new first test starts once it is."
    conn.execute(
        "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code', updated_at = ?"
        " WHERE id = ? AND status = 'open'",
        (f"Venture #{vid}'s channel {why}", now, now, test["id"]),
    )
    conn.execute("UPDATE ventures SET test_milestone_id = NULL, updated_at = ? WHERE id = ?", (now, vid))
    return f"Ember's code dropped milestone #{test['id']}, the first test of venture #{vid}: {why}"


def keep(
    conn: sqlite3.Connection,
    scope: AgentScope,
    today: date,
    now: str,
    ready: Collection[str] = tuple(CHANNEL_TESTS),
    unset: Collection[str] = (),
) -> list[str]:
    """Before every plan: a first test for each backed venture that has none (0.14.0: a channel's venture once its
    channel is ``ready``; while the owner hasn't set the channel up (``unset``), an open first test Ember's code set
    earlier is dropped, and a new one comes once it is), and the stages' rules (research without a business case, a
    missed first test; 0.13.0: an idea no one took up, a live venture that sells nothing or earns more than it costs).
    Returns what happened, for the events."""
    happened = []
    paid = ventures.money(conn, scope)
    for v in ventures.all_ventures(conn, scope):
        if v["stage"] == "idea":  # 0.13.0: triage
            triage = ventures.triage_date(v)
            if triage is not None and today >= triage:
                happened.append(
                    park(conn, scope, v, now, f"no one took the idea up within {TRIAGE_DAYS} days (triage)")
                )
            continue
        if v["stage"] == "live":  # 0.13.0: scale, or park what sells nothing
            money = paid.get(int(v["id"]), ventures.Money())
            start = ventures.rule_clock(v)
            if money.earned > 0 and money.net > 0 and not v["scale_milestone_id"]:
                made = scale(conn, scope, v, money, today, now)
                happened.append(
                    f"Ember's code set milestone #{made}: scale venture #{v['id']} (it earns more than it costs)"
                )
            elif start is not None and (today - start).days >= LIVE_DAYS and not sold(conn, scope, int(v["id"]), money):
                happened.append(park(conn, scope, v, now, f"nothing sold {LIVE_DAYS} days after it went live"))
            continue
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
        if waits_for_channel(v, ready):
            # 0.14.0: no first test runs, nor is missed, while its channel can't be used
            if test is not None and test["status"] == "open" and test["created_by"] == "code" and v["channel"] in unset:
                happened.append(restart_test(conn, scope, v, test, now))
            continue
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
