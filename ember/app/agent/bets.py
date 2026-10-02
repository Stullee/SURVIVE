"""Bets (0.18.0): what the agent expects of a change to a project, settled by Ember's code.

The agent never said what it expected of a change, so nothing told it when it was wrong, and a surprise is what
learning starts from. Now project_update takes a bet ("+15 views in 7 days: the pins bring buyers"): Ember's code
keeps the project's funnel number as its baseline (reach.py: its live listings' views, favorites or orders at the last
Etsy sync) and settles it before every plan:

* won once the number gained what was bet (also before its date);
* lost at its date without it, or no_reach for a bet on views when nothing was done to bring buyers meanwhile (a bet on
  views without reach is a bet on luck);
* void when its project has no live listing any more at its date.

What can be bet is checked first: a bet on favorites or orders only once the listings were seen (reach.SEEN_VIEWS),
and on orders not while its last quality check said improve (quality.py): it loops back to the health check. One
open bet per project and metric. A settled bet goes to the next daily review, whose retrospective asks why
(learning.py).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import reach
from .store import AgentScope

MIN_DAYS, MAX_DAYS = 3, 28
METRICS = {"view": "views", "views": "views", "favorite": "favorites", "favorites": "favorites"}
METRICS |= {"order": "orders", "orders": "orders", "sale": "orders", "sales": "orders"}
_BET = re.compile(r"^\+?\s*(\d+)\s+([a-z]+)\s+(?:in|within)\s+(\d+)\s+days?\s*[:,-]\s*(.+)$", re.IGNORECASE | re.DOTALL)
FORMAT = "write it as '+15 views in 7 days: why you expect it' (views, favorites or orders; 3 to 28 days)"


class BetError(ValueError):
    pass


def parse(text: str) -> tuple[int, str, int, str]:
    """(gain, metric, days, what is expected) of a bet's text; raises BetError with how to write it."""
    found = _BET.match(" ".join(text.split()))
    if found is None or found[2].lower() not in METRICS:
        raise BetError(FORMAT)
    gain, days = int(found[1]), int(found[3])
    if not 1 <= gain <= 100_000 or not MIN_DAYS <= days <= MAX_DAYS:
        raise BetError(FORMAT)
    return gain, METRICS[found[2].lower()], days, found[4].strip()


def value(funnel: reach.Funnel, metric: str) -> int:
    return int(getattr(funnel, metric))


def place(
    conn: sqlite3.Connection, scope: AgentScope, project_id: int, text: str, cycle_id: int | None, today: date, now: str
) -> str:
    """Place a bet on a project (raises BetError); the line project_update's answer adds."""
    gain, metric, days, expect = parse(text)
    funnel = reach.funnels(conn, scope).get(project_id)
    if funnel is None or not funnel.listings:
        raise BetError("a bet needs the project's live listings: Ember's code settles it from their numbers")
    if metric != "views" and funnel.stage == "not_seen":
        raise BetError(
            f"its listings aren't seen yet ({funnel.views} views): bet on views, and bring buyers to them first"
        )
    if metric == "orders":
        last = quality_verdict(conn, scope, project_id)
        if last == "improve":
            raise BetError("its last quality check said improve: fix what it named before betting on orders")
    where, params = scope.where()
    if conn.execute(
        f"SELECT 1 FROM bets WHERE {where} AND project_id = ? AND metric = ? AND status = 'open'",
        (*params, project_id, metric),
    ).fetchone():
        raise BetError(f"project #{project_id} has an open bet on {metric}: it settles first")
    due = (today + timedelta(days=days)).isoformat()
    cursor = conn.execute(
        "INSERT INTO bets (mode, session, cycle_id, project_id, metric, gain, baseline, reach, expect, placed_at, due)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            cycle_id,
            project_id,
            metric,
            gain,
            value(funnel, metric),
            funnel.reach,
            expect[:200],
            now,
            due,
        ),
    )
    return f"Bet #{cursor.lastrowid}: +{gain} {metric} by {due} (now {value(funnel, metric)}); Ember's code settles it."


def quality_verdict(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> str:
    """The verdict of a project's newest quality check that came through, or "" without one."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT verdict FROM quality_checks WHERE {where} AND project_id = ? AND status = 'ok' ORDER BY id DESC"
        " LIMIT 1",
        (*params, project_id),
    ).fetchone()
    return str(row["verdict"]) if row else ""


def settle(conn: sqlite3.Connection, scope: AgentScope, today: date, now: str) -> list[str]:
    """Settle the open bets that are won or due; returns what happened, for the events."""
    where, params = scope.where()
    rows = conn.execute(f"SELECT * FROM bets WHERE {where} AND status = 'open' ORDER BY id", params).fetchall()
    if not rows:
        return []
    funnels = reach.funnels(conn, scope)
    happened = []
    for b in rows:
        funnel = funnels.get(int(b["project_id"]))
        due = date.fromisoformat(str(b["due"])) <= today
        now_value = value(funnel, str(b["metric"])) if funnel is not None else None
        if now_value is not None and now_value - int(b["baseline"]) >= int(b["gain"]):
            status = "won"
        elif not due:
            continue
        elif funnel is None or not funnel.listings:
            status = "void"
        elif b["metric"] == "views" and funnel.reach <= int(b["reach"]):
            status = "no_reach"
        else:
            status = "lost"
        conn.execute(
            "UPDATE bets SET status = ?, final = ?, settled_at = ? WHERE id = ?", (status, now_value, now, b["id"])
        )
        happened.append(f"Bet #{b['id']} on project #{b['project_id']} {status}: {line(b, now_value, status)}")
    return happened


def line(b: Any, now_value: int | None, status: str = "open") -> str:
    """A bet in one line: what was bet, and how it stands or came out."""
    got = "?" if now_value is None else f"+{now_value - int(b['baseline'])}"
    said = f"+{b['gain']} {b['metric']} by {b['due']} ({got} so far)" if status == "open" else ""
    if status != "open":
        said = f"+{b['gain']} {b['metric']} by {b['due']}, got {got}"
    return f"{said}: {b['expect']}"


def open_lines(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, list[str]]:
    """The open bets by project, as the plan's OPEN PROJECTS shows them."""
    where, params = scope.where()
    rows = conn.execute(f"SELECT * FROM bets WHERE {where} AND status = 'open' ORDER BY id", params).fetchall()
    if not rows:
        return {}
    funnels = reach.funnels(conn, scope)
    found: dict[int, list[str]] = {}
    for b in rows:
        funnel = funnels.get(int(b["project_id"]))
        current = value(funnel, str(b["metric"])) if funnel is not None else None
        found.setdefault(int(b["project_id"]), []).append(f"bet #{b['id']}: {line(b, current)}")
    return found


def settled_since(conn: sqlite3.Connection, scope: AgentScope, since: str) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM bets WHERE {where} AND status <> 'open' AND settled_at >= ? ORDER BY id", (*params, since)
    ).fetchall()
