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
METRICS |= {"favourite": "favorites", "favourites": "favorites"}
METRICS |= {"order": "orders", "orders": "orders", "sale": "orders", "sales": "orders"}
# 0.19.2: also "by" a day (OPEN PROJECTS shows a bet so: "+10 views by 2026-10-09"), a note in brackets after the
# days or the day ("+15 views in 13 days (by 10-17): why"), and up to two words before the metric ("+5 listing
# views"); live, these were refused three times in one cycle, and the bet was lost.
_WHEN = (
    r"(?:(?:in|within)\s+(?P<days>\d+)\s+days?"
    r"|by\s+(?P<day>\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}|\d{1,2}\.\d{1,2}\.(?:\d{4})?))"
)
# 0.24.0: the why after the day without a colon too ("+5 views by 10-14 from the new photo"): live, six bets were
# refused in four cycles for it.
_BET = re.compile(
    rf"^\+?\s*(?P<gain>\d+)\s+(?:[a-z][a-z-]*\s+){{0,2}}?(?P<metric>[a-z]+)\s+{_WHEN}(?:\s*\([^()]{{0,80}}\))?"
    r"(?:\s*[:,;\u2013\u2014-]\s*|\s+)(?P<why>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
_METRIC_WORD = re.compile(r"\b(" + "|".join(sorted(METRICS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
FORMAT = (
    "write it as '+15 views in 7 days: why you expect it' or '+15 views by 2026-10-17: why' (views, favorites or "
    "orders; 3 to 28 days)"
)


class BetError(ValueError):
    pass


def parse(text: str, today: date | None = None) -> tuple[int, str, int, str]:
    """(gain, metric, days, what is expected) of a bet's text (``today``: for a bet by a day); raises BetError with how
    to write it."""
    found = _BET.match(" ".join(text.split()))
    if found is None or found["metric"].lower() not in METRICS:
        raise BetError(FORMAT)
    gain = int(found["gain"])
    days = int(found["days"]) if found["days"] else _days_to(found["day"], today)
    if not 1 <= gain <= 100_000 or days is None or not MIN_DAYS <= days <= MAX_DAYS:
        raise BetError(FORMAT)
    return gain, METRICS[found["metric"].lower()], days, found["why"].strip()


def _days_to(text: str, today: date | None) -> int | None:
    """The days from ``today`` to a day written as 2026-10-17, 10-17 or 17.10. (without a year: the next one to come),
    or None."""
    if today is None:
        return None
    parts = [int(part) for part in re.split(r"[-.]", text.strip(".")) if part]
    try:
        if "-" in text:
            year, month, number = parts if len(parts) == 3 else (None, *parts)
        else:
            number, month, year = parts if len(parts) == 3 else (*parts, None)
        day = date(year or today.year, month, number)
    except (TypeError, ValueError):
        return None
    if year is None and day < today:
        day = day.replace(year=today.year + 1)
    return (day - today).days


def value(funnel: reach.Funnel, metric: str) -> int:
    return int(getattr(funnel, metric))


def place(
    conn: sqlite3.Connection, scope: AgentScope, project_id: int, text: str, cycle_id: int | None, today: date, now: str
) -> str:
    """Place a bet on a project (raises BetError); the line project_update's answer adds. 0.24.0: a bet on a metric with
    an open bet is refused for that first, also when it isn't written right (live, the agent rewrote a bet to the format
    and was then told the project had one open)."""
    try:
        gain, metric, days, expect = parse(text, today)
    except BetError:
        word = _METRIC_WORD.search(text)
        held = _open(conn, scope, project_id, METRICS[word[1].lower()]) if word else None
        if held is not None:
            raise BetError(f"{held}; a new one on it waits") from None
        raise
    held = _open(conn, scope, project_id, metric)
    if held is not None:
        raise BetError(held)
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


def _open(conn: sqlite3.Connection, scope: AgentScope, project_id: int, metric: str) -> str | None:
    """Why a project's open bet on ``metric`` stops a new one ("project #5 has an open bet on views (bet #1, +10 views
    by 2026-10-07): it settles first"), or None without one."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT id, gain, due FROM bets WHERE {where} AND project_id = ? AND metric = ? AND status = 'open'",
        (*params, project_id, metric),
    ).fetchone()
    if row is None:
        return None
    return (
        f"project #{project_id} has an open bet on {metric} (bet #{row['id']}, +{row['gain']} {metric} by "
        f"{row['due']}): it settles first"
    )


def quality_verdict(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> str:
    """The quality critic's verdict on a project's live listings (quality.verdict: improve while any says so), or ""
    without a check."""
    from . import quality  # 0.24.0: here, not at the top: quality imports prompts, which imports tools, then this

    return quality.verdict(conn, scope, project_id)


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
