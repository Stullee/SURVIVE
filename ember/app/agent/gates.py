"""A product line's listing test (0.13.0): the bars of the analysis' section 8, as milestones Ember's code sets and
checks.

A product line (a project) whose first listing is live on Etsy is tested from that day on: 10 views in all by day 7,
30 views and 2 favorites by day 14, and a first order by day 21. Ember's code sets each bar as a milestone linked to
the project, so its metric counts only its listings, one bar at a time: the next once the one before is closed, each
due on its day from the start. Etsy's own numbers grade them: the listings' views, favorites and orders in all, as the
last sync read them (no views history is kept for this). 0.14.0: day 14's favorites are a bar once its views are met
(a miss of the views misses the day-14 bar), so a product line holds one open milestone at a time; these take none of
the agent's or the owner's places (roadmap.placed). A bar dropped (by the owner, or with a parked venture) ends the
product line's test: no next bar is set.

A missed bar is an obligation with its action: fix the titles, tags and category once (day 7); park the product line
with the numbers (day 14: one obligation for its two bars); stop building that product type (day 21). A first order by
day 21 is met with its own action: scale it (5 variants or a bundle), a decision point Ember's code sets for the agent
to close. Their dates never move and only the owner drops them; a closed project takes its open ones with it.

The bars are milestones of the kind 'first_test' (a product line's first test; listing_gates names each one's bar),
under the money goal when it is due later; the milestone to scale is a decision point ('decision') of its own, a goal
in the plan.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..integrations import etsy_publisher
from . import metrics, roadmap
from .store import AgentScope

OPEN_PROJECTS = ("idea", "active", "waiting")
SCALE_DAYS = 14  # after a first order by day 21: the time to scale the product line


@dataclass(frozen=True)
class Gate:
    key: str
    day: int
    metric: str
    target: int
    title: str  # with the project's name after it
    measure: str
    missed: str  # what the agent owes when it is missed


PARK = "park the product line (its project) with the numbers, unless your owner says otherwise"
GATES = (
    Gate(
        "day7_views",
        7,
        "views_total",
        10,
        "Day 7: 10 views",
        "Its listings have 10 views in all by day 7 (Etsy's numbers). Missed: fix their titles, tags and category once",
        "fix the titles, tags and category of its listings once (propose_etsy_edit), then let them run",
    ),
    Gate(
        "day14_views",
        14,
        "views_total",
        30,
        "Day 14: 30 views",
        "Its listings have 30 views in all by day 14 (Etsy's numbers; then 2 favorites, the next bar). Missed: park "
        "the product line with the numbers",
        PARK,
    ),
    Gate(
        "day14_favorites",
        14,
        "favorites_total",
        2,
        "Day 14: 2 favorites",
        "Its listings have 2 favorites in all by day 14 (Etsy's numbers). Missed: park the product line with the "
        "numbers",
        PARK,
    ),
    Gate(
        "day21_sale",
        21,
        "orders_total",
        1,
        "Day 21: a first order",
        "A buyer orders one of its listings by day 21 (Etsy's orders). Met: scale it (5 variants or a bundle). "
        "Missed: stop building this product type",
        "stop building this product type: no new listings of it",
    ),
)
BY_KEY = {g.key: g for g in GATES}
BARS = ("day7_views", "day14_views", "day14_favorites", "day21_sale")  # one after the other (0.14.0: one at a time)
SCALE_TITLE = "Scale it: 5 variants or a bundle"
SCALE_MEASURE = (
    "A buyer ordered by day 21: the product line has 5 variants or a bundle live (you close it when they are live)"
)


def _title(text: str, project: str) -> str:
    limit = roadmap.LIMITS["title"]
    whole = f"{text} of {' '.join(project.split())}"
    return whole if len(whole) <= limit else whole[: limit - 1].rstrip() + "…"


def started(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, list[sqlite3.Row]]:
    """The bars Ember's code set, by project: their rows with their milestones' state."""
    where, params = scope.where("g")
    found: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute(
        "SELECT g.*, m.status, m.result FROM listing_gates g"
        f" JOIN milestones m ON m.id = g.milestone_id WHERE {where} ORDER BY g.id",
        params,
    ):
        found.setdefault(int(r["project_id"]), []).append(r)
    return found


def _live_projects(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, sqlite3.Row]:
    """The open projects with a listing of Ember's live on Etsy, by number."""
    rows = etsy_publisher.live_rows(metrics.listings(conn, scope, None, None), {})
    live = {int(r["for_project"]) for r in rows if r["for_project"]}
    if not live:
        return {}
    marks = ", ".join("?" for _ in live)
    where, params = scope.where()
    return {
        int(p["id"]): p
        for p in conn.execute(
            f"SELECT * FROM projects WHERE {where} AND id IN ({marks}) AND status IN {OPEN_PROJECTS}",
            (*params, *sorted(live)),
        )
    }


def _record(
    conn: sqlite3.Connection, scope: AgentScope, project_id: int, key: str, milestone_id: int, start: date, now: str
) -> None:
    conn.execute(
        "INSERT INTO listing_gates (mode, session, project_id, gate, milestone_id, started_on, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, project_id, key, milestone_id, start.isoformat(), now),
    )


def _set(
    conn: sqlite3.Connection, scope: AgentScope, project: Any, gate: Gate, start: date, today: date, now: str
) -> int:
    """One bar as a milestone of Ember's code, due on its day from the start (0.14.0: or today, for a bar that opens
    after its day because the one before was graded after its date)."""
    due = max(start + timedelta(days=gate.day), today).isoformat()
    goal = roadmap.money_goal(conn, scope)
    milestone_id = roadmap.create(
        conn,
        scope,
        title=_title(gate.title, str(project["title"])),
        measure=gate.measure,
        due=due,
        now=now,
        parent_id=goal["id"] if goal is not None and goal["due"] >= due else None,
        project_id=int(project["id"]),
        created_by="code",
        kind="first_test",
        metric=gate.metric,
        target=gate.target,
    )
    _record(conn, scope, int(project["id"]), gate.key, milestone_id, start, now)
    return milestone_id


def _next_bar(
    conn: sqlite3.Connection, scope: AgentScope, project: Any, rows: list[sqlite3.Row], today: date, now: str
) -> list[int]:
    """The next bar of a product line's test, once the one before is closed (the first one at its start). 0.14.0: none
    once a bar was dropped (the test ended), and day 14's favorites only once its views are met."""
    have = {r["gate"]: r for r in rows}
    if any(r["status"] == "dropped" for r in rows):
        return []
    start = date.fromisoformat(str(rows[0]["started_on"])) if rows else today
    for key in BARS:
        if key in have:
            if have[key]["status"] == "open":
                return []  # this bar is being checked
            continue
        views = have.get("day14_views")
        if key == "day14_favorites" and views is not None and views["status"] != "done":
            continue  # the day-14 bar is missed already
        return [_set(conn, scope, project, BY_KEY[key], start, today, now)]
    return []


def _owe(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row, gate: Gate, project: str, now: str) -> str:
    """The obligation a missed bar leaves: its action, with the numbers."""
    what = (
        f"milestone #{row['milestone_id']} {_title(gate.title, project)!r} was missed ({str(row['result'])[:160]}): "
        f"{gate.missed}"
    )
    conn.execute(
        "INSERT INTO obligations (mode, session, kind, what, due, created_at, milestone_id)"
        " VALUES (?, ?, 'miss', ?, ?, ?, ?)",
        (scope.mode, scope.session, what[:400], now[:10], now, row["milestone_id"]),
    )
    return f"Obligation: {gate.missed.split(':')[0]} (project #{row['project_id']}, milestone #{row['milestone_id']})"


def _scale(
    conn: sqlite3.Connection, scope: AgentScope, project_id: int, name: str, start: str, today: date, now: str
) -> int:
    milestone_id = roadmap.create(  # a goal of its own: the money goal's decision points go with the goal
        conn,
        scope,
        title=_title(SCALE_TITLE, name),
        measure=SCALE_MEASURE,
        due=(today + timedelta(days=SCALE_DAYS)).isoformat(),
        now=now,
        project_id=project_id,
        created_by="code",
        kind="decision",
    )
    _record(conn, scope, project_id, "scale", milestone_id, date.fromisoformat(start), now)
    return milestone_id


def keep(conn: sqlite3.Connection, scope: AgentScope, today: date, now: str) -> list[str]:
    """After the milestones are graded, before every plan: the test of each product line whose first listing went
    live (its next bar once the one before is closed), the obligations its misses leave, the decision point a first
    order by day 21 sets, and a closed project's open bars dropped. Returns what happened, for the events."""
    happened: list[str] = []
    tests = started(conn, scope)
    where, params = scope.where()
    projects = {int(p["id"]): p for p in conn.execute(f"SELECT * FROM projects WHERE {where}", params)}
    owed = {
        int(o["milestone_id"])
        for o in conn.execute(
            f"SELECT milestone_id FROM obligations WHERE {where} AND milestone_id IS NOT NULL", params
        )
    }
    for project_id, rows in tests.items():
        project = projects.get(project_id)
        if project is None or project["status"] not in OPEN_PROJECTS:
            for r in rows:
                if r["status"] == "open":
                    conn.execute(
                        "UPDATE milestones SET status = 'dropped', result = ?, closed_at = ?, closed_by = 'code',"
                        " updated_at = ? WHERE id = ? AND status = 'open'",
                        (f"Project #{project_id} is closed.", now, now, r["milestone_id"]),
                    )
                    happened.append(
                        f"Ember's code dropped milestone #{r['milestone_id']}: project #{project_id} is closed"
                    )
            continue
        name = str(project["title"])
        by_key = {r["gate"]: r for r in rows}
        for r in rows:
            gate = BY_KEY.get(r["gate"])
            if gate is None or r["status"] != "missed" or int(r["milestone_id"]) in owed:
                continue
            twin = by_key.get({"day14_views": "day14_favorites", "day14_favorites": "day14_views"}.get(gate.key, ""))
            if twin is not None and int(twin["milestone_id"]) in owed:
                continue  # one obligation for the day-14 bar
            happened.append(_owe(conn, scope, r, gate, name, now))
            owed.add(int(r["milestone_id"]))
        sale = by_key.get("day21_sale")
        if sale is not None and sale["status"] == "done" and "scale" not in by_key:
            made = _scale(conn, scope, project_id, name, str(sale["started_on"]), today, now)
            happened.append(
                f"Ember's code set milestone #{made}: scale project #{project_id} (a first order by day 21)"
            )
        opened = _next_bar(conn, scope, project, rows, today, now)
        if opened:
            happened.append(
                f"Ember's code set the next bar of project #{project_id}'s listing test: " + _numbers(opened)
            )
    for project_id, project in _live_projects(conn, scope).items():
        if project_id not in tests:
            made = _next_bar(conn, scope, project, [], today, now)
            happened.append(f"Ember's code began the listing test of project #{project_id}: " + _numbers(made))
    return happened


def _numbers(ids: list[int]) -> str:
    return ", ".join(f"milestone #{i}" for i in ids)
