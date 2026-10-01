"""The agenda (0.13.0): what happens between wake cycles, noted by Ember's code, and when it wakes the agent.

Only the timer and the owner's messages and decisions (0.12.0) woke the agent: a sale, a reply to Ember's email or a
milestone's last day waited for the next scheduled cycle, up to the longest sleep. Now Ember's code notes such events
as it sees them (``note``: after each round's Etsy sync, and after reading the mailbox every MAIL_MINUTES, also while
the agent sleeps), once each:

* order: an Etsy order of Ember's listings (0.15.0: not urgent, Ember's code records it);
* reply: an email that answers one Ember sent (urgent);
* inquiry: a person writes to Ember first (0.13.0, Phase E1): an email that waits for an answer (mailstore.inquiries),
  unread and come in after the last cycle ended (a cycle's MAIL section already showed what came before; urgent);
* favorites: a listing's favorites reaching one of FAVORITE_STEPS (the counts a listing had when the agenda began are
  a baseline, never shown);
* milestone_due: an open milestone due today, from CHECK_HOUR (urgent: its last day; 0.15.0: not one Ember's code
  checks itself, a metric's or the money goal: that is bookkeeping).

The next plan lists what it hasn't seen in SINCE YOUR LAST WAKE. An urgent event wakes the agent for a lean reactive
cycle (no venture work, no review, study or critic, at most REACTIVE_STEPS work steps): at most EVENT_WAKES a day,
MIN_GAP apart, never while dormant, and (0.15.0) only with the owner's wake_on_events option and behind the schedule's
guards (agent/service.py). What can't wake it waits in the agenda for the next cycle. Until 20:00, a share of the daily
cap is kept for these wakes (metering.event_reserve): a scheduled cycle can't spend it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ..economy.clock import Clock, from_iso, to_iso
from ..integrations import etsy, mailstore
from . import metrics
from .store import AgentScope

KINDS = ("order", "reply", "inquiry", "favorites", "milestone_due")
URGENT = frozenset({"reply", "inquiry", "milestone_due"})  # 0.15.0: an order no longer wakes the agent
EVENT_WAKES = 4  # a day
MIN_GAP = timedelta(minutes=30)  # between two event wake-ups
REACTIVE_STEPS = 5  # a reactive cycle's work steps at most
MAIL_MINUTES = 15  # the mailbox is read this often between cycles
FAVORITE_STEPS = (5, 10, 25, 50, 100, 250, 500, 1000)
CHECK_HOUR = 8  # the owner's hour a milestone's last day is noted
SHOWN = 8  # the events one plan lists
SINCE_KEY = "agenda.since.{mode}"  # when the agenda began: what happened before is history, not news
TEXT_CHARS = 300


def _add(
    conn: sqlite3.Connection,
    scope: AgentScope,
    kind: str,
    key: str,
    text: str,
    now: str,
    baseline: bool = False,
    urgent: bool = True,
) -> bool:
    """``urgent``: False keeps an event of an urgent kind from waking the agent (0.15.0)."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO agenda (mode, session, kind, key, text, urgent, baseline, noted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            kind,
            key[:100],
            text[:TEXT_CHARS],
            int(kind in URGENT and urgent),
            int(baseline),
            now,
        ),
    )
    return cursor.rowcount == 1


def _orders(conn: sqlite3.Connection, scope: AgentScope, since: str, now: str) -> list[str]:
    ours = {int(r["listing_id"]): str(r["title"]) for r in metrics.listings(conn, scope, None, None)}
    if not ours:
        return []
    where, params = scope.where()
    noted = []
    for order in conn.execute(
        f"SELECT receipt_id, ordered_at, items FROM etsy_orders WHERE {where} AND {etsy.COUNTED_ORDERS}"
        " AND ordered_at >= ? ORDER BY ordered_at",
        (*params, since),
    ):
        items = [i for i in json.loads(order["items"] or "[]") if isinstance(i, dict) and i.get("listing_id") in ours]
        if not items:
            continue
        what = ", ".join(
            f"#{i['listing_id']} {json.dumps(ours[i['listing_id']][:60], ensure_ascii=False)}" for i in items
        )
        when = str(order["ordered_at"])[:16].replace("T", " ")
        text = f"An Etsy order (receipt #{order['receipt_id']}, {when} UTC) of your listing {what}"
        if _add(conn, scope, "order", str(order["receipt_id"]), text, now):
            noted.append(text)
    return noted


def _mail(conn: sqlite3.Connection, scope: AgentScope, since: str, now: str) -> list[str]:
    """Emails that arrived: an answer to one Ember sent (reply), or a person writing to Ember first (inquiry). 0.15.0:
    either only a person's email (mailstore.person) from someone who didn't ask to stop, so an out-of-office, a bounce
    or a forged sender wakes no one (the next plan's MAIL section still shows it)."""
    where, params = scope.where()
    sent = {
        str(r["message_id"])
        for r in conn.execute(
            f"SELECT message_id FROM emails WHERE {where} AND direction = 'out' AND message_id IS NOT NULL", params
        )
    }
    ended = conn.execute(
        "SELECT MAX(ended_at) FROM cycles WHERE session = ? AND simulated = ?",
        (scope.session, 1 if scope.mode == "dry_run" else 0),
    ).fetchone()[0]
    waiting = {int(r["id"]) for r in mailstore.inquiries(conn, scope)}
    noted = []
    for mail in conn.execute(
        f"SELECT id, from_addr, subject, in_reply_to, references_, received_at, read_by_agent_at,"
        f" {mailstore.person()} AS person FROM emails WHERE {where} AND direction = 'in' AND received_at >= ?"
        " ORDER BY id",
        (*params, since),
    ):
        answers = {mail["in_reply_to"], *str(mail["references_"] or "").split()}
        subject = json.dumps(" ".join(str(mail["subject"] or "").split())[:80], ensure_ascii=False)
        sender = str(mail["from_addr"])
        if answers & sent and mail["person"] and not mailstore.is_suppressed(conn, scope, sender):
            kind, text = "reply", f"Email #{mail['id']} from {sender} answers one you sent: {subject}"
        elif (
            int(mail["id"]) in waiting
            and mail["read_by_agent_at"] is None
            and (ended is None or str(mail["received_at"]) > str(ended))
        ):
            kind, text = "inquiry", f"Email #{mail['id']} from {sender} writes to you: {subject}"
        else:
            continue
        if _add(conn, scope, kind, str(mail["id"]), text, now):
            noted.append(text)
    return noted


def _favorites(conn: sqlite3.Connection, scope: AgentScope, now: str, first: bool) -> list[str]:
    noted = []
    for listing in metrics.listings(conn, scope, None, None):
        count = int(listing["favorites"] or 0)
        reached = [step for step in FAVORITE_STEPS if count >= step]
        if not reached:
            continue
        step = reached[-1]
        title = json.dumps(str(listing["title"])[:60], ensure_ascii=False)
        text = f"Your listing #{listing['listing_id']} {title} has {count} favorites ({step} or more)"
        if _add(conn, scope, "favorites", f"{listing['listing_id']}:{step}", text, now, baseline=first) and not first:
            noted.append(text)
    return noted


def _milestones(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, now: str) -> list[str]:
    local = clock.now().astimezone(clock.tz)
    if local.hour < CHECK_HOUR:
        return []
    today = local.date().isoformat()
    where, params = scope.where()
    noted = []
    for m in conn.execute(
        f"SELECT id, title, due, metric, kind FROM milestones WHERE {where} AND status = 'open' AND due = ?"
        " ORDER BY id",
        (*params, today),
    ):
        title = json.dumps(" ".join(str(m["title"]).split())[:80], ensure_ascii=False)
        text = f"Milestone #{m['id']} {title} is due today: its last day"
        # 0.15.0: Ember's code checks a metric's milestone (a listing test's bars too) and the money goal itself: their
        # last day is bookkeeping, noted for the next plan, and wakes no one
        checked = m["metric"] is not None or m["kind"] == "money_goal"
        if _add(conn, scope, "milestone_due", f"{m['id']}:{m['due']}", text, now, urgent=not checked):
            noted.append(text)
    return noted


def note(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, since: str, first: bool = False) -> list[str]:
    """Note what happened since ``since`` (when the agenda began) that isn't noted yet; ``first``: the agenda's first
    look, when the listings' favorites so far are a baseline. Returns the new events' texts, for the System log."""
    now = to_iso(clock.now())
    return [
        *_orders(conn, scope, since, now),
        *_mail(conn, scope, since, now),
        *_favorites(conn, scope, now, first),
        *_milestones(conn, scope, clock, now),
    ]


def unseen(conn: sqlite3.Connection, scope: AgentScope, limit: int = SHOWN) -> list[sqlite3.Row]:
    """The events no plan has shown yet (the baseline aside), the oldest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM agenda WHERE {where} AND baseline = 0 AND seen_cycle_id IS NULL ORDER BY id LIMIT ?",
        (*params, limit),
    ).fetchall()


def waking(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The urgent events that haven't woken the agent and no plan has shown yet. 0.15.0: not an order, nor the last
    day of a milestone Ember's code checks, noted as urgent before 0.15.0 (an event never changes)."""
    where, params = scope.where()
    kinds = ", ".join(f"'{k}'" for k in sorted(URGENT))
    return conn.execute(
        f"SELECT * FROM agenda WHERE {where} AND baseline = 0 AND urgent = 1 AND seen_cycle_id IS NULL"
        f" AND woke_at IS NULL AND kind IN ({kinds}) AND NOT (kind = 'milestone_due' AND EXISTS ("
        " SELECT 1 FROM milestones m WHERE m.id = CAST(substr(agenda.key, 1, instr(agenda.key, ':') - 1) AS INTEGER)"
        " AND (m.metric IS NOT NULL OR m.kind = 'money_goal'))) ORDER BY id",
        params,
    ).fetchall()


def wakes(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> tuple[int, datetime | None]:
    """Today's event wake-ups so far (the owner's day), and when the last one began."""
    start = to_iso(clock.day_start(clock.today()))
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END), 0), MAX(started_at) FROM cycles"
        " WHERE trigger = 'event' AND session = ? AND simulated = ?",
        (start, scope.session, 1 if scope.simulated else 0),
    ).fetchone()
    return int(row[0]), from_iso(row[1]) if row[1] else None


def mark_woke(conn: sqlite3.Connection, ids: list[int], now: str) -> None:
    marks = ", ".join("?" for _ in ids)
    conn.execute(f"UPDATE agenda SET woke_at = ? WHERE id IN ({marks}) AND woke_at IS NULL", (now, *ids))


def mark_seen(conn: sqlite3.Connection, cycle_id: int, ids: list[int]) -> None:
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)
    conn.execute(
        f"UPDATE agenda SET seen_cycle_id = ? WHERE id IN ({marks}) AND seen_cycle_id IS NULL", (cycle_id, *ids)
    )


def line(row: sqlite3.Row) -> str:
    """An event as the plan lists it."""
    when = str(row["noted_at"])[:16].replace("T", " ")
    return f"Agenda ({row['kind'].replace('_', ' ')}, noted {when} UTC): {row['text']}"


def open_count(conn: sqlite3.Connection, scope: AgentScope) -> int:
    where, params = scope.where()
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM agenda WHERE {where} AND baseline = 0 AND seen_cycle_id IS NULL", params
        ).fetchone()[0]
    )


def recent(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[dict[str, Any]]:
    """The latest events for the dashboard, the newest first."""
    where, params = scope.where()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "text": r["text"],
            "urgent": bool(r["urgent"]),
            "noted_at": r["noted_at"],
            "woke_at": r["woke_at"],
            "seen_cycle_id": r["seen_cycle_id"],
        }
        for r in conn.execute(
            f"SELECT * FROM agenda WHERE {where} AND baseline = 0 ORDER BY id DESC LIMIT ?", (*params, limit)
        )
    ]
