"""What the agent owes (0.12.0), kept by Ember's code.

Promises were only prose: a venture cycle deferred the owner's quick fix, an answer that promised work for later was
forgotten, and a decision of the owner's was shown once. Now Ember's code keeps an obligations ledger:

* promises: what the agent promised its owner in a message (``message_owner`` commits), due on the day it named;
* decisions: a request of the agent's that the owner rejected or carried out, or that failed: it must react;
* misses: a milestone Ember's code closed missed (a metric's): the agent decides what now;

and reads the rest from their own records: the owner's messages waiting for an answer, overdue milestones and live
listings with too few photos. The plan shows them first and never cuts them; a pressing one (``pressing``) makes a
wake cycle an ordinary one rather than a venture cycle. The agent closes a promise, decision or miss with
``obligation_done``, saying what it did; a promise only once its owner has heard from it since, and a miss also closes
when a milestone replaces it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..integrations import etsy_publisher, qa
from . import roadmap
from .store import AgentScope

KINDS = ("promise", "decision", "miss")
WHAT_CHARS = 100  # of a promise, request or milestone, as the plan shows it
NOTE_CHARS = 60  # of the owner's comment or a result note
LINE_CHARS = 160  # an obligation's line in the plan, at most
SHOWN = 5  # the obligations the plan lists one by one (the most urgent first)
# The plan's OBLIGATIONS is never cut, so it is bounded: its lines can't take more (JSON-escaped UTF-8 bytes).
MAX_BYTES = 2_600
PROMISE_DAYS = 14  # a promise is due within this many days
PRESSING_OVERDUE_DAYS = 3  # a promise overdue this long still makes the cycle an ordinary one
PRESSING_NEW_DAYS = 2  # a decision or a miss is pressing this long
HEADING = "OBLIGATIONS (kept by Ember's code: deal with them first)"
SINCE_KEY = "agent.{mode}.obligations_since"  # decisions and misses from then on (not the history before)


def keep(conn: sqlite3.Connection, scope: AgentScope, now: str, since: str) -> list[str]:
    """Record the decisions and misses the agent owes a reaction to (from ``since`` on), and close the misses a
    milestone replaced. Returns what happened, for the events."""
    where, params = scope.where()
    happened = []
    decided = conn.execute(
        f"SELECT * FROM approvals WHERE {where} AND (status = 'rejected' AND decided_at >= ?"
        " OR status = 'failed' AND closed_at >= ? OR status = 'done' AND closed_at >= ?"
        " AND (executor IS NULL OR executor = 'reddit_link'))"
        " AND NOT EXISTS (SELECT 1 FROM obligations o WHERE o.approval_id = approvals.id) ORDER BY id",
        (*params, since, since, since),
    ).fetchall()
    for r in decided:
        what = decision_what(r)
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, approval_id)"
            " VALUES (?, ?, 'decision', ?, ?, ?, ?)",
            (scope.mode, scope.session, what, now[:10], now, r["id"]),
        )
        happened.append(f"Obligation: react to request #{r['id']} ({r['status']})")
    missed = conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status = 'missed' AND closed_by = 'code'"
        " AND created_by <> 'code' AND closed_at >= ?"
        " AND NOT EXISTS (SELECT 1 FROM obligations o WHERE o.milestone_id = milestones.id) ORDER BY id",
        (*params, since),
    ).fetchall()
    for m in missed:
        what = f"milestone #{m['id']} {_q(m['title'])} was missed ({_flat(m['result'], NOTE_CHARS)}): decide what now"
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, milestone_id)"
            " VALUES (?, ?, 'miss', ?, ?, ?, ?)",
            (scope.mode, scope.session, what[:400], now[:10], now, m["id"]),
        )
        happened.append(f"Obligation: decide about missed milestone #{m['id']}")
    owed, owed_params = scope.where("o")
    replaced = conn.execute(
        "SELECT o.id, n.id AS by_id FROM obligations o JOIN milestones n ON n.replaces_id = o.milestone_id"
        f" WHERE {owed} AND o.status = 'open'",
        owed_params,
    ).fetchall()
    for r in replaced:
        close_one(conn, r["id"], f"milestone #{r['by_id']} replaces it", "code", None, now)
    return happened


def decision_what(r: sqlite3.Row) -> str:
    """What a decided request asks of the agent now."""
    title = _q(r["title"])
    if r["status"] == "rejected":
        said = f": {_q(_flat(r['decision_comment'], NOTE_CHARS))}" if r["decision_comment"] else ""
        return f"your owner rejected request #{r['id']} {title}{said}: take it into account"
    if r["status"] == "failed":
        return f"request #{r['id']} {title} failed ({_flat(r['result_note'], NOTE_CHARS)}): decide what now"
    note = f": {_q(_flat(r['result_note'], NOTE_CHARS))}" if r["result_note"] else ""
    return f"your owner carried out request #{r['id']} {title}{note}: take the next step"


def promise(
    conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, message_id: int, what: str, due: str, now: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO obligations (mode, session, kind, what, due, created_at, cycle_id, message_id)"
        " VALUES (?, ?, 'promise', ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, what, due, now, cycle_id, message_id),
    )
    return int(cursor.lastrowid)


def open_rows(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The open obligations, the most urgent first: the earliest due, promises before decisions and misses."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND status = 'open'"
        " ORDER BY due, CASE kind WHEN 'promise' THEN 0 WHEN 'decision' THEN 1 ELSE 2 END, id",
        params,
    ).fetchall()


def close_one(
    conn: sqlite3.Connection, obligation_id: int, result: str, by: str, cycle_id: int | None, now: str
) -> None:
    conn.execute(
        "UPDATE obligations SET status = 'closed', closed_at = ?, closed_by = ?, closed_cycle_id = ?, result = ?"
        " WHERE id = ? AND status = 'open'",
        (now, by, cycle_id, result[:300], obligation_id),
    )


def told_since(conn: sqlite3.Connection, scope: AgentScope, message_id: int) -> bool:
    """Whether the agent sent its owner a message after message ``message_id`` (the one that promised)."""
    where, params = scope.where()
    found = conn.execute(
        f"SELECT 1 FROM messages WHERE {where} AND sender = 'agent' AND id > ? LIMIT 1", (*params, message_id)
    ).fetchone()
    return found is not None


def pressing(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[str]:
    """What makes this wake cycle an ordinary one rather than a venture cycle: the owner's messages waiting for an
    answer, a promise due by tomorrow (or overdue for PRESSING_OVERDUE_DAYS at most), a decision or a miss of the last
    PRESSING_NEW_DAYS days. Empty when nothing presses."""
    where, params = scope.where()
    found = []
    messages = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE {where} AND sender = 'owner' AND answered_by IS NULL"
        " AND removed_at IS NULL",
        params,
    ).fetchone()[0]
    if messages:
        found.append(f"{messages} message{'s' if messages != 1 else ''} of your owner's to answer")
    first = (today - timedelta(days=PRESSING_OVERDUE_DAYS)).isoformat()
    last = (today + timedelta(days=1)).isoformat()
    since = (today - timedelta(days=PRESSING_NEW_DAYS)).isoformat()
    for r in open_rows(conn, scope):
        if r["kind"] == "promise" and first <= r["due"] <= last or r["kind"] != "promise" and r["due"] >= since:
            found.append(f"obligation #{r['id']} ({r['kind']})")
    return found


def text(conn: sqlite3.Connection, scope: AgentScope, today: date) -> str:
    """The plan's OBLIGATIONS, at most SHOWN obligations and a line each for the owner's messages, the overdue
    milestones and the listings with too few photos: bounded, so it is never cut. Empty when nothing is owed."""
    lines = []
    where, params = scope.where()
    waiting = conn.execute(
        f"SELECT id, created_at FROM messages WHERE {where} AND sender = 'owner' AND answered_by IS NULL"
        " AND removed_at IS NULL ORDER BY id",
        params,
    ).fetchall()
    if waiting:
        ids = ", ".join(f"#{r['id']}" for r in waiting[:6]) + (
            f" and {len(waiting) - 6} more" if len(waiting) > 6 else ""
        )
        lines.append(
            f"- Answer your owner's message{'s' if len(waiting) != 1 else ''} {ids} (FROM YOUR OWNER; waiting since"
            f" {str(waiting[0]['created_at'])[:16].replace('T', ' ')} UTC)."
        )
    rows = open_rows(conn, scope)
    for r in rows[:SHOWN]:
        lines.append(f"- {line(r, today)}")
    if len(rows) > SHOWN:
        lines.append(f"- and {len(rows) - SHOWN} more obligations, due later.")
    overdue = [
        m
        for m in roadmap.open_milestones(conn, scope)
        if (roadmap.parse_day(m["due"]) or today) < today and not roadmap.waiting(m, today)
    ]
    if overdue:
        ids = ", ".join(f"#{m['id']}" for m in overdue[:6]) + (
            f" and {len(overdue) - 6} more" if len(overdue) > 6 else ""
        )
        lines.append(f"- Overdue milestones {ids}: close, move or drop each (ROADMAP).")
    few = etsy_publisher.few_photos(conn, scope)
    if few:
        shown = ", ".join(f"#{listing_id} ({count})" for listing_id, count in few[:4])
        more = f" and {len(few) - 4} more" if len(few) > 4 else ""
        lines.append(
            f"- Live listings with fewer than {qa.MIN_PHOTOS} photos: {shown}{more}: give each the whole"
            " set with propose_etsy_edit."
        )
    if not lines:
        return ""
    if rows:
        lines.append(
            "Close a promise, decision or miss you have met with obligation_done, saying what you did (a promise once "
            "your owner has heard from you since)."
        )
    while len(lines) > 1 and _bytes("\n".join(lines)) > MAX_BYTES:  # can't happen with the limits above
        lines.pop(-2)
    return "\n".join(lines)


def _bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


def line(r: sqlite3.Row, today: date) -> str:
    due = roadmap.parse_day(r["due"]) or today
    if r["kind"] == "promise":
        days = (due - today).days
        when = (
            f"OVERDUE since {r['due']}" if days < 0 else "due today" if days == 0 else f"due {r['due']} (in {days} d)"
        )
        return f"#{r['id']} promise to your owner, {when}: {_q(_flat(r['what'], WHAT_CHARS))}"
    return f"#{r['id']} {r['kind']} ({str(r['created_at'])[:10]}): {_flat(r['what'], LINE_CHARS)}"


def _q(text: Any) -> str:
    return json.dumps(_flat(text, WHAT_CHARS), ensure_ascii=False)


def _flat(text: Any, chars: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= chars else flat[: chars - 1] + "…"
