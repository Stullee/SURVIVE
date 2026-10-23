"""0.39.8 (analysis-0.39.6, 3.5): a paid test of Ember's listings, kept by Ember's code.

Live, the owner turned on Etsy Ads at 01:03 on 2026-10-16 for the three listings request #128 named (€1 a day for 7
days, approved with "done starting from now"), and nothing in Ember's plan knew: no bet, no step, nothing in STATUS.
Its daily review and weekly look had run an hour before, and its keep-or-drop dates fell three days into the test.

An approved spend request (type spend_money) that names Ember's listings becomes a test (``keep``, before every plan):
its listings, their views on the day the owner decided it, and the day it ends (the request's "for N days", else
DEFAULT_DAYS later). Once it ends, the listings' views and favorites then are kept with it. STATUS shows a test while it
runs and SHOWN_DAYS after it ended (``status_text``): a paid test is judged on what it brought, with the owner's numbers
(the ads' spend, impressions and clicks), not by a keep-or-drop date that falls in the middle of it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..economy.clock import Clock, to_iso
from . import ventures
from .store import AgentScope

DEFAULT_DAYS = 7  # a test that names no days runs this long
MAX_DAYS = 60
SHOWN_DAYS = 3  # STATUS shows an ended test this many days after its end
DECIDED = ("approved", "approved_with_changes", "done")
_NUMBER = re.compile(r"(?<!\d)(\d{9,12})(?!\d)")  # a listing's number, alone or in its address
# 0.42.2 (analysis-0.40.1 audit): one \s* each side of the "-": two in a row split a run of spaces every way (quadratic)
_DAYS = re.compile(r"(?<![\d/])(\d{1,2})\s*(?:-\s*)?(?:days?|tage[n]?)\b", re.IGNORECASE)


def listings_named(conn: sqlite3.Connection, scope: AgentScope, text: str) -> list[int]:
    """The numbers of Ember's own listings a text names (#4587912058, or its address), in order, each once."""
    found = []
    for number in dict.fromkeys(int(n) for n in _NUMBER.findall(text or "")):
        if ventures.listing_line(conn, scope, number) is not None:
            found.append(number)
    return found


def days_named(text: str) -> int:
    """How many days a request's words say it runs ("for 7 days", "a 14-day test"), else DEFAULT_DAYS."""
    for match in _DAYS.finditer(text or ""):
        days = int(match.group(1))
        if 1 <= days <= MAX_DAYS:
            return days
    return DEFAULT_DAYS


def _views(conn: sqlite3.Connection, scope: AgentScope, listing: int, on: str | None = None) -> dict[str, int]:
    """A listing's views and favorites: those Ember's code observed on the day ``on`` or the last day before it, else
    (or with no day) its numbers at the last sync."""
    where, params = scope.world_where()  # 0.46.0: a listing's numbers are the world's
    found: dict[str, int] = {}
    if on is not None:
        for metric in ("views", "favorites"):
            row = conn.execute(
                f"SELECT value FROM observations WHERE {where} AND subject = 'listing' AND subject_id = ?"
                " AND metric = ? AND day <= ? ORDER BY day DESC LIMIT 1",
                (*params, listing, metric, on),
            ).fetchone()
            if row is not None:
                found[metric] = int(row["value"])
    if len(found) < 2:
        where_l, params_l = scope.world_where("l")
        for table in ("etsy_listings", "printify_products"):
            favorites = "l.favorites" if table == "etsy_listings" else "0"
            row = conn.execute(
                f"SELECT l.views, {favorites} AS favorites FROM {table} l WHERE {where_l} AND l.listing_id = ?"
                " ORDER BY l.id DESC LIMIT 1",
                (*params_l, listing),
            ).fetchone()
            if row is not None:
                found.setdefault("views", int(row["views"] or 0))
                found.setdefault("favorites", int(row["favorites"] or 0))
                break
    return {"views": found.get("views", 0), "favorites": found.get("favorites", 0)}


def keep(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> list[str]:
    """Before every plan: an approved spend request naming Ember's listings becomes a test (one per request), and a
    test past its end ends with its listings' numbers then. Returns what happened, for the events."""
    said: list[str] = []
    now, today = to_iso(clock.now()), clock.today()
    where, params = scope.where("a")
    marks = ", ".join("?" for _ in DECIDED)
    for row in conn.execute(
        f"SELECT a.id, a.title, a.description, a.payload, a.final_payload, a.decided_at FROM approvals a WHERE {where}"
        f" AND a.type = 'spend_money' AND a.status IN ({marks}) AND a.decided_at IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM paid_tests t WHERE t.approval_id = a.id) ORDER BY a.id",
        (*params, *DECIDED),
    ).fetchall():
        words = " ".join(str(row[k] or "") for k in ("title", "description", "payload", "final_payload"))
        named = listings_named(conn, scope, words)
        if not named:
            continue
        started = clock.local_day(str(row["decided_at"]))
        ends = started + timedelta(days=days_named(words))
        baseline = {str(n): _views(conn, scope, n, started.isoformat()) for n in named}
        conn.execute(
            "INSERT INTO paid_tests (mode, session, approval_id, started_on, ends_on, listings, baseline, status,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (
                scope.mode,
                scope.session,
                row["id"],
                started.isoformat(),
                ends.isoformat(),
                json.dumps(named),
                json.dumps(baseline, sort_keys=True),
                now,
            ),
        )
        listed = ", ".join(f"#{n}" for n in named)
        said.append(f"Request #{row['id']} is a paid test of {listed} from {started:%m-%d} to {ends:%m-%d}.")
    where, params = scope.where()
    for test in conn.execute(
        f"SELECT * FROM paid_tests WHERE {where} AND status = 'running' AND ends_on < ? ORDER BY id",
        (*params, today.isoformat()),
    ).fetchall():
        final = {str(n): _views(conn, scope, int(n)) for n in json.loads(test["listings"])}
        conn.execute(
            "UPDATE paid_tests SET status = 'ended', final = ?, ended_at = ? WHERE id = ? AND status = 'running'",
            (json.dumps(final, sort_keys=True), now, test["id"]),
        )
        said.append(f"The paid test of request #{test['approval_id']} ended: {_gained(test, final)}.")
    return said


def running(conn: sqlite3.Connection, scope: AgentScope, today: date) -> dict[int, sqlite3.Row]:
    """0.42.1 (analysis-0.40.1 codebase, 6.2 "A paid test in the plan"): each of Ember's listings in a paid test that
    runs on ``today`` (to its last day, ends_on), with that test (the one that ends last): no change of it goes through
    (preconditions.PAID), and its line's keep-or-drop dates wait for the end (plan._decide_by)."""
    where, params = scope.where()
    found: dict[int, sqlite3.Row] = {}
    for test in conn.execute(
        f"SELECT * FROM paid_tests WHERE {where} AND status = 'running' AND ends_on >= ? ORDER BY ends_on, id",
        (*params, today.isoformat()),
    ).fetchall():
        found.update(dict.fromkeys((int(n) for n in json.loads(test["listings"])), test))
    return found


def _gained(test: sqlite3.Row, now: dict[str, dict[str, int]]) -> str:
    """'+14 views (10 to 24) and 0 favorites (0 to 0) on its 3 listings'."""
    start = json.loads(test["baseline"])
    views = [sum(v.get("views", 0) for v in d.values()) for d in (start, now)]
    favorites = [sum(v.get("favorites", 0) for v in d.values()) for d in (start, now)]
    count = len(json.loads(test["listings"]))
    return (
        f"{views[1] - views[0]:+d} views ({views[0]} to {views[1]}) and {favorites[1] - favorites[0]:+d} favorites"
        f" ({favorites[0]} to {favorites[1]}) on its {count} listing{'s' if count != 1 else ''}"
    )


def status_text(conn: sqlite3.Connection, scope: AgentScope, today: date) -> str:
    """STATUS's lines on the paid tests: each one running, and each that ended in the last SHOWN_DAYS, with what it
    brought so far. "" when there is none."""
    where, params = scope.where()
    lines = []
    shown_from = (today - timedelta(days=SHOWN_DAYS)).isoformat()
    for test in conn.execute(
        f"SELECT * FROM paid_tests WHERE {where} AND (status = 'running' OR ends_on >= ?) ORDER BY id",
        (*params, shown_from),
    ).fetchall():
        listed = ", ".join(f"#{n}" for n in json.loads(test["listings"]))
        begun, ends = date.fromisoformat(test["started_on"]), date.fromisoformat(test["ends_on"])
        if test["status"] == "running":
            numbers: Any = {str(n): _views(conn, scope, int(n)) for n in json.loads(test["listings"])}
            lines.append(
                f"Paid test (your owner's spend, request #{test['approval_id']}): {listed} from {begun:%m-%d} to"
                f" {ends:%m-%d}: {_gained(test, numbers)} so far. Leave these listings as they are while it runs, and"
                " judge it when it ends, not by a keep-or-drop date within it."
            )
        else:
            lines.append(
                f"Paid test of request #{test['approval_id']} ({listed}) ended {ends:%m-%d}:"
                f" {_gained(test, json.loads(test['final']))} in {(ends - begun).days} days. Judge it now: ask your"
                " owner for its numbers (what it cost, how often the listings were shown and clicked) and say whether"
                " it goes on."
            )
    return "\n".join(lines)
