"""The prediction ledger (0.13.0): the agent's estimates, settled by Ember's code against what happened.

The agent's odds and its business cases were never checked against what came of them, so nothing told it, its owner
or the critic whether they could be trusted. Now Ember's code keeps two kinds of prediction:

* milestone: a metric milestone the agent gives a likelihood (milestone_plan's ``likely``, LIKELY percent): hit once
  the milestone is met by its first date (a date moved later doesn't move the prediction), miss once that date has
  passed without it or it was missed, void when the owner or Ember's code dropped it first (0.15.0: the agent's own
  drop is a miss: dropping a losing call voided it);
* first_sale: when the owner backs a venture, its newest business case's days to the first sale, as a 50% call
  (the case's middle estimate): hit once an Etsy order of its listings or revenue for it is recorded by then, miss
  after (0.15.0: FIRST_SALE_GRACE_DAYS after, so that a sale made in time but recorded late still counts).

``settle`` runs before every plan, after the metrics are read, with no model call. ``calibration`` is their record in
a few words: how often the milestones given odds were met against the odds given (with the Brier score), and how many
first sales came on time. Triage reads it (the decision desk's READY), and so do the critic and the daily review.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from .. import events
from ..db import Database
from ..economy.clock import Clock, to_iso
from ..integrations import etsy
from . import metrics
from .store import AgentScope

LIKELY = (5, 95)  # the odds a milestone takes, in percent
FIRST_SALE_ODDS = 0.5  # a business case's days to the first sale: its middle estimate
FIRST_SALE_MIN_DAYS = 14  # even "this month" gets two weeks
FIRST_SALE_GRACE_DAYS = 7  # 0.15.0: a sale by its date, recorded this much later, still counts
MIN_SETTLED = 5  # settled milestone predictions before the calibration says which way the odds lean
LEAN = 0.10  # a gap this big between the odds given and the share met is a lean
CALIBRATION_CHARS = 240
REVIEW_SHOWN = 6  # predictions settled in the review's period that it lists
RESULT_CHARS = 300


def add_milestone(
    conn: sqlite3.Connection, scope: AgentScope, milestone_id: int, likely: int, claim: str, due: str, now: str
) -> int:
    """The agent's odds (``likely``, in percent) that a metric milestone is met by its date."""
    cursor = conn.execute(
        "INSERT INTO predictions (mode, session, kind, milestone_id, claim, probability, due, created_at)"
        " VALUES (?, ?, 'milestone', ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, milestone_id, claim[:300], likely / 100, due, now),
    )
    return int(cursor.lastrowid)


def add_first_sale(
    conn: sqlite3.Connection,
    scope: AgentScope,
    venture_id: int,
    case_row: Mapping[str, Any] | None,
    today: date,
    now: str,
) -> int | None:
    """A backed venture's first sale by its business case's month, as a 50% call (None without a case, or when this
    case already made it)."""
    if case_row is None:
        return None
    if conn.execute("SELECT 1 FROM predictions WHERE case_id = ?", (case_row["id"],)).fetchone() is not None:
        return None
    days = max(FIRST_SALE_MIN_DAYS, int(case_row["first_sale_days"]))  # 0.15.0: the case's days
    due = today + timedelta(days=days)
    claim = f"venture #{venture_id}'s first sale within {days} days of being backed (case #{case_row['id']})"
    cursor = conn.execute(
        "INSERT INTO predictions (mode, session, kind, venture_id, case_id, claim, probability, due, created_at)"
        " VALUES (?, ?, 'first_sale', ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, venture_id, case_row["id"], claim, FIRST_SALE_ODDS, due.isoformat(), now),
    )
    return int(cursor.lastrowid)


# --- settling ---


def _milestone(conn: sqlite3.Connection, p: Mapping[str, Any], clock: Clock, today: date) -> tuple[str, str] | None:
    m = conn.execute(
        "SELECT id, status, closed_at, closed_by FROM milestones WHERE id = ?", (p["milestone_id"],)
    ).fetchone()
    due = date.fromisoformat(str(p["due"]))
    if m is None:
        return "void", "its milestone is gone"
    closed = clock.local_day(str(m["closed_at"])) if m["closed_at"] else None
    if m["status"] == "done" and closed is not None:
        if closed <= due:
            return "hit", f"milestone #{m['id']} was met on {closed.isoformat()}"
        return "miss", f"milestone #{m['id']} was met only on {closed.isoformat()}, after {due.isoformat()}"
    if m["status"] == "dropped" and closed is not None and closed <= due:
        if m["closed_by"] == "agent":  # 0.15.0: dropping a losing call no longer voids it
            return "miss", f"milestone #{m['id']} was dropped by you on {closed.isoformat()}, before its date"
        return "void", f"milestone #{m['id']} was dropped on {closed.isoformat()}, before its date"
    if m["status"] == "missed" or today > due:
        return "miss", f"milestone #{m['id']} wasn't met by {due.isoformat()} (it is {m['status']})"
    return None


def _sold(
    conn: sqlite3.Connection, scope: AgentScope, books: metrics.Books, venture_id: int, since: str, until: str
) -> str:
    """The venture's first sale from ``since`` to the end of the day ``until``: its evidence, or ""."""
    ours = {int(r["listing_id"]) for r in metrics.listings(conn, scope, None, venture_id)}
    if ours:
        where, params = scope.where()
        for order in conn.execute(
            f"SELECT receipt_id, items, ordered_at FROM etsy_orders WHERE {where} AND {etsy.COUNTED_ORDERS}"
            " AND ordered_at >= ? ORDER BY ordered_at",
            (*params, since),
        ):
            day = books.clock.local_day(str(order["ordered_at"])).isoformat()
            if day > until:
                break
            items = json.loads(order["items"] or "[]")
            if any(isinstance(i, dict) and i.get("listing_id") in ours for i in items):
                return f"an Etsy order of its listings on {day} (receipt #{order['receipt_id']})"
    if books.ledger is None:
        return ""
    where, params = books.ledger.where("l")
    row = conn.execute(
        "SELECT l.id, l.occurred_on FROM ledger l LEFT JOIN projects p ON p.id = l.project_id"
        f" WHERE l.type = 'revenue' AND {where} AND COALESCE(l.venture_id, p.venture_id) = ?"
        " AND l.occurred_on >= ? AND l.occurred_on <= ? ORDER BY l.occurred_on, l.id LIMIT 1",
        (*params, venture_id, books.clock.local_day(since).isoformat(), until),
    ).fetchone()
    return f"revenue for it on {row['occurred_on']} (ledger entry #{row['id']})" if row is not None else ""


def settle(conn: sqlite3.Connection, scope: AgentScope, books: metrics.Books) -> list[str]:
    """Settle every open prediction that can be (no model call); returns what happened, for the events."""
    today = books.clock.today()
    now = to_iso(books.clock.now())
    where, params = scope.where()
    happened = []
    for p in conn.execute(
        f"SELECT * FROM predictions WHERE {where} AND status = 'open' ORDER BY id", params
    ).fetchall():
        if p["kind"] == "milestone":
            outcome = _milestone(conn, p, books.clock, today)
        else:
            sold = _sold(conn, scope, books, int(p["venture_id"]), str(p["created_at"]), str(p["due"]))
            late = (date.fromisoformat(str(p["due"])) + timedelta(days=FIRST_SALE_GRACE_DAYS)).isoformat()
            outcome = (
                ("hit", sold)
                if sold
                else ("miss", f"no sale recorded for it by {p['due']}")
                if today.isoformat() > late
                else None
            )
        if outcome is None:
            continue
        status, result = outcome
        conn.execute(
            "UPDATE predictions SET status = ?, settled_at = ?, result = ? WHERE id = ? AND status = 'open'",
            (status, now, result[:RESULT_CHARS], p["id"]),
        )
        happened.append(
            f"Ember's code settled prediction #{p['id']} ({p['claim']}, {float(p['probability']):.0%}): {status}: "
            f"{result}"
        )
    return happened


def settle_all(db: Database, scope: AgentScope, ledger: Any, clock: Clock) -> list[str]:
    """``settle`` in its own transaction (``ledger``: the books' scope); what happened becomes events."""
    with db.transaction() as conn:
        happened = settle(conn, scope, metrics.Books(ledger, clock))
    for line in happened:
        events.record(db, "info", "agent", line[:300])
    return happened


# --- the record ---


def _settled(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM predictions WHERE {where} AND status IN ('hit', 'miss') ORDER BY id", params
    ).fetchall()


def calibration(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """The record of the settled predictions in a few words (``record_text``)."""
    settled = _settled(conn, scope)
    return record_text([(str(r["kind"]), float(r["probability"]), r["status"] == "hit") for r in settled])


def record_text(settled: list[tuple[str, float, bool]]) -> str:
    """The record in a few words ("" before any prediction is settled; ``settled``: each one's kind, odds and whether
    it came true): how often the milestones given odds were met against those odds, with the Brier score, and how
    many first sales came on time."""
    odds = [(p, hit) for kind, p, hit in settled if kind == "milestone"]
    sales = [hit for kind, _, hit in settled if kind == "first_sale"]
    parts = []
    if odds:
        met = sum(1 for _, hit in odds if hit)
        given = sum(p for p, _ in odds) / len(odds)
        share = met / len(odds)
        brier = sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in odds) / len(odds)
        lean = ""
        if len(odds) >= MIN_SETTLED and abs(given - share) >= LEAN:
            lean = ": your odds run high" if given > share else ": your odds run low"
        parts.append(
            f"milestones given odds: {met} of {len(odds)} met ({share:.0%}) at {given:.0%} on average{lean} (Brier "
            f"{brier:.2f}; a coin toss scores 0.25)"
        )
    if sales:
        parts.append(f"first sales by the business case's month: {sum(sales)} of {len(sales)} on time")
    return "; ".join(parts)[:CALIBRATION_CHARS]


def of_milestones(conn: sqlite3.Connection, milestone_ids: list[int]) -> dict[int, sqlite3.Row]:
    """The predictions on these milestones, by milestone."""
    if not milestone_ids:
        return {}
    marks = ", ".join("?" for _ in milestone_ids)
    rows = conn.execute(f"SELECT * FROM predictions WHERE milestone_id IN ({marks})", milestone_ids).fetchall()
    return {int(r["milestone_id"]): r for r in rows}


def review_text(conn: sqlite3.Connection, scope: AgentScope, since: str) -> str:
    """The daily review's check of the predictions against the results ("" without any)."""
    where, params = scope.where()
    settled = conn.execute(
        f"SELECT * FROM predictions WHERE {where} AND status <> 'open' AND settled_at >= ? ORDER BY settled_at DESC,"
        " id DESC LIMIT ?",
        (*params, since, REVIEW_SHOWN),
    ).fetchall()
    waiting = conn.execute(
        f"SELECT COUNT(*), MIN(due) FROM predictions WHERE {where} AND status = 'open'", params
    ).fetchone()
    record = calibration(conn, scope)
    if not settled and not waiting[0] and not record:
        return ""
    lines = ["YOUR FORECASTS (Ember's code settles them against its records)"]
    lines += [f"#{r['id']} {r['status']} ({float(r['probability']):.0%}): {r['claim']}: {r['result']}" for r in settled]
    if waiting[0]:
        lines.append(f"Open: {waiting[0]} (the next due {waiting[1]}).")
    if record:
        lines.append(f"Your record: {record}.")
    return "\n".join(lines)
