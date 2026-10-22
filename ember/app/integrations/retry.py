"""0.42.0 (analysis-0.40.1 codebase, M3): a service that asks Ember's code to come back later, before anything changed.

Etsy, Pinterest and Bluesky answer HTTP 429 (too many requests) when Ember's code asks too often, and 503 with a
Retry-After when they are down for a while, and say when to come back. Until 0.42.0 such an answer closed the approval
as failed, so the agent had to propose again and the owner to approve again. Now, when it refuses the call that would
have created or changed something first (nothing changed yet: an Etsy listing's draft, the first change of a live
listing, a pin on an existing board, a Bluesky post), the request stays approved and waits:

* its attempt is finished in the action journal as 'failed' with when it is tried again (``after.retry_at``: as the
  service said, at least FLOOR and at most CEILING), and its executor's row says 'failed' until then (``park``), so the
  daily limits don't count it and a restart doesn't call it unclear;
* at that time the publisher tries it again (``due``, ``reopen``) through the same start transaction: the approval, the
  kill switch, the files' SHA-256, the daily limit and the publishing queue all apply again;
* after TRIES waits, the next refusal fails it as before.

Only the owner's own approvals wait: an unlock approves the agent's next proposal again by itself, and taking an
unlock back stops only what nothing began. The owner can cancel a waiting request, and a park or kill of its venture
stops it (``waiting_sql``). Anything that may have changed something (a timeout, a 5xx without Retry-After, a refusal
after the draft or the new board exists) is never tried again, and an email never is.

0.42.3 (analysis-0.40.1 audit, M3): Printify too, which had none. A product waits only when Printify refuses its
picture's upload, the first of its three calls that change anything (the upload, the product, its publishing); a change
of a live product only when Printify refuses the first of its calls (the new design's upload, else the change itself).
A refusal after that fails it as before: by then the picture is in the owner's Printify library (where it stays, unlike
a Bluesky picture no post uses), and the product may exist or have changed.

0.42.4 (M3): the owner's Undo of a pin, a post or a product (UNDOS) too, which a 429 to its delete failed, so the owner
had to press it again. Its delete is one call: a "come back later" to it changed nothing. It keeps no row of an
executor's, so it waits in its journal entry alone (``park`` with no table; ``waiting`` and ``due`` read the journal as
for the others), and its card reads it there (``undo_execution``). Etsy's Undo is a change, which waited already. An
Undo never ages out (connectors.AGING), waiting or not.

0.42.5: the sections the agent reads (ETSY SHOP, PINTEREST, BLUESKY, PRINTIFY and the daily review's listings) showed
the executor's 'failed' row of a request that waits, with the service's "HTTP 429", so the agent could propose the same
listing, pin, post or product again while its approval was still to be tried. Now they say it waits, until when, and
not to propose it again (``waiting_words``); one its service refused after its tries still reads as failed.
"""

from __future__ import annotations

import email.utils
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from ..economy.clock import Clock, from_iso, to_iso
from . import connectors

TRIES = 3  # times a request may wait for its service; the next refusal fails it
FLOOR = timedelta(minutes=1)  # the shortest wait, whatever Retry-After says
CEILING = timedelta(hours=6)  # and the longest
UNSAID = timedelta(minutes=15)  # the wait when the service names none
LATER = "retrying"  # what a run says of a request that waits for its service, and its status on the Approvals tab
SHOWN = "waiting"  # 0.42.5: its status in the sections the agent reads (waiting_words says why)
APPROVED = ("approved", "approved_with_changes")
# The executors' rows a request waits in (0.42.3: and Printify's products and changes)
PARKED = ("etsy_listings", "etsy_edits", "pinterest_pins", "bluesky_posts", "printify_products", "printify_edits")
# 0.42.4 (M3): the owner's Undos that wait with no row of their executor's, in their journal entry alone
UNDOS = ("pinterest_delete", "bluesky_delete", "printify_delete")


class Busy(Exception):
    """Mixed into an integration's NotSent: the service asked Ember's code to come back later, and nothing changed.
    ``wait``: the seconds it named (None when it named none)."""

    def __init__(self, message: str, wait: float | None = None) -> None:
        super().__init__(message)
        self.wait = wait


def seconds(retry_after: str | None, date: str | None = None) -> float | None:
    """Retry-After's wait in seconds: a number of seconds, or an HTTP date counted from the answer's own Date (so no
    clock matters); None when there is none or it can't be read."""
    text = (retry_after or "").strip()
    if text.isdigit() and len(text) <= 9:
        return float(text)
    try:
        then = email.utils.parsedate_to_datetime(text) if text else None
        sent = email.utils.parsedate_to_datetime(date) if date else None
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if then is None or sent is None or then.tzinfo is None or sent.tzinfo is None:
        return None
    return max(0.0, (then - sent).total_seconds())


def asks_to_wait(status: int, retry_after: str | None, date: str | None = None) -> bool:
    """Whether an answer is the service's "come back later" to a request it didn't carry out: 429, or 503 with a
    Retry-After. A 503 without one may come from a proxy whose service was reached, and stays what it was."""
    return status == 429 or (status == 503 and seconds(retry_after, date) is not None)


def said(status: int, wait: float | None) -> str:
    """The answer in a few words, for the request's notes."""
    what = "too many requests" if status == 429 else "unavailable for now"
    return f" ({what}; retry after {round(wait)} s)" if wait is not None else f" ({what})"


def until(now: datetime, wait: float | None) -> datetime:
    """When a request the service asked to wait ``wait`` seconds is tried again: at least FLOOR, at most CEILING."""
    named = UNSAID if wait is None else timedelta(seconds=min(max(wait, 0.0), CEILING.total_seconds()))
    return now + min(max(named, FLOOR), CEILING)


# --- the request that waits -----------------------------------------------------------------------------------------


def retry_at_sql(alias: str) -> str:
    """SQL: when request ``alias`` is tried again (NULL unless its newest attempt ended waiting for its service). Only
    an approved request waits: callers ask for the approved ones."""
    return (
        f"(SELECT json_extract(j.after, '$.retry_at') FROM action_journal j WHERE j.approval_id = {alias}.id"
        " ORDER BY j.id DESC LIMIT 1)"
    )


def waiting_sql(alias: str) -> str:
    """SQL: whether request ``alias`` is approved and waits for its service (begun, but nothing changed): not once its
    executor's row runs again (a pin's attempt is journaled only after its row; 0.42.4: an Undo's, once its new
    journal entry is begun, which has no ``retry_at``)."""
    running = " UNION ALL ".join(
        f"SELECT 1 FROM {table} x WHERE x.approval_id = {alias}.id AND x.status = 'running'" for table in PARKED
    )
    return f"({alias}.status IN {APPROVED} AND {retry_at_sql(alias)} IS NOT NULL AND NOT EXISTS ({running}))"


def waiting(conn: sqlite3.Connection, approval_id: int) -> datetime | None:
    """When an approved request that waits for its service is tried again; None when it doesn't wait."""
    row = conn.execute(
        f"SELECT {retry_at_sql('a')} AS at FROM approvals a WHERE a.id = ? AND {waiting_sql('a')}", (approval_id,)
    ).fetchone()
    return from_iso(str(row["at"])) if row is not None else None


def due(conn: sqlite3.Connection, approval_id: int, now: datetime) -> bool:
    """Whether a request that waits for its service is to be tried again now."""
    at = waiting(conn, approval_id)
    return at is not None and at <= now


def waited(conn: sqlite3.Connection, approval_id: int) -> int:
    """How many times a request waited for its service."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM action_journal WHERE approval_id = ?"
            " AND json_extract(after, '$.retry_at') IS NOT NULL",
            (approval_id,),
        ).fetchone()[0]
    )


def may_wait(conn: sqlite3.Connection, approval_id: int) -> bool:
    """Whether a request the service refused for now may wait for it: still approved, by the owner (not an unlock),
    and it waited fewer than TRIES times."""
    row = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    carried = conn.execute(
        "SELECT 1 FROM policy_uses WHERE approval_id = ? AND approved_at IS NOT NULL", (approval_id,)
    ).fetchone()
    return row is not None and row["status"] in APPROVED and carried is None and waited(conn, approval_id) < TRIES


def _when(clock: Clock, at: datetime) -> str:
    local, today = at.astimezone(clock.tz), clock.today()
    # 0.42.5: or a day past (waiting_words: while paused, or until a pin's or post's slot), which read "tomorrow"
    days = {today: "", today + timedelta(days=1): " tomorrow", today - timedelta(days=1): " yesterday"}
    return f"{local:%H:%M}{days.get(local.date(), f' on {local:%Y-%m-%d}')}"


def waiting_words(conn: sqlite3.Connection, approval_id: int | None, clock: Clock, service: str) -> str | None:
    """0.42.5: why a request that waits for its service isn't out yet, for the sections the agent reads (its executor's
    row says 'failed' until it is tried again): "Etsy asked to come back at 14:05; don't propose it again". None when
    it doesn't wait: one its service refused after its tries reads as failed."""
    at = waiting(conn, approval_id) if approval_id is not None else None
    return None if at is None else f"{service} asked to come back at {_when(clock, at)}; don't propose it again"


def park(
    conn: sqlite3.Connection,
    table: str | None,
    approval_id: int,
    exc: Busy,
    clock: Clock,
    service: str,
    **values: Any,
) -> str:
    """The attempt ends waiting for its service: the executor's row (``table``, with ``values``) 'failed', the journal
    entry 'failed' with when it is tried again. Returns the note. 0.42.4 (M3): ``table`` None for the owner's Undo of a
    pin, a post or a product (UNDOS), which has no such row: its journal entry alone, which keeps what the service
    answered too (``after.error``, as a row's ``error``), for its card."""
    if table is not None and table not in PARKED:
        raise RuntimeError(f"{table} isn't an executor's table a request waits in")
    if table is None:
        found = conn.execute("SELECT executor FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if values or found is None or found["executor"] not in UNDOS:
            raise RuntimeError(f"request #{approval_id} waits in its executor's table")
    now = clock.now()
    at = until(now, exc.wait)
    note = f"{service} asked to wait: retrying at {_when(clock, at)} (wait {waited(conn, approval_id) + 1} of {TRIES}:"
    note += f" {exc})"
    stamp = to_iso(now)
    after: dict[str, Any] = {"retry_at": to_iso(at)}
    if table is not None:
        sets = "".join(f", {name} = ?" for name in values)
        cursor = conn.execute(
            f"UPDATE {table} SET status = 'failed', finished_at = ?, result = ?, error = ?{sets}"
            " WHERE approval_id = ? AND status = 'running'",
            (stamp, note[:500], str(exc)[:500], *values.values(), approval_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"request #{approval_id} isn't running in {table}")
    elif conn.execute(
        "SELECT 1 FROM action_journal WHERE approval_id = ? AND status = 'running'", (approval_id,)
    ).fetchone():
        after["error"] = str(exc)[:500]
    else:
        raise RuntimeError(f"request #{approval_id} isn't running")
    connectors.finish(conn, approval_id, "failed", stamp, after, note)
    return note


def reopen(conn: sqlite3.Connection, table: str, approval_id: int, stamp: str, **values: Any) -> None:
    """A request that waited is tried again: its executor's row 'running' from ``stamp`` (with ``values``), as when it
    was first begun, in the transaction that checked it again (the caller journals the attempt as it did the first)."""
    if table not in PARKED or waiting(conn, approval_id) is None:
        raise RuntimeError(f"request #{approval_id} doesn't wait in {table}")
    sets = "".join(f", {name} = ?" for name in values)
    cursor = conn.execute(
        f"UPDATE {table} SET status = 'running', started_at = ?, finished_at = NULL, result = NULL, error = NULL{sets}"
        " WHERE approval_id = ? AND status = 'failed'",
        (stamp, *values.values(), approval_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"request #{approval_id} doesn't wait in {table}")


def execution(row: Mapping[str, Any], at: datetime, **extra: Any) -> dict[str, Any]:
    """A request that waits for its service, for the Approvals tab: when it is tried again (``at``), and what the
    service said (``row``: its executor's row)."""
    return {
        "status": LATER,
        "due_at": to_iso(at),
        "started_at": None,
        "finished_at": None,
        "result": row["result"],
        "error": row["error"],
        "url": None,
        **extra,
    }


def undo_execution(conn: sqlite3.Connection, approval_id: int) -> dict[str, Any] | None:
    """0.42.4 (M3): the owner's Undo of a pin, a post or a product that waits for its service, for the Approvals tab
    as ``execution`` says the others: from its journal entry, the only record it keeps (``park``). None when it doesn't
    wait."""
    at = waiting(conn, approval_id)
    if at is None:
        return None
    entry = conn.execute(
        "SELECT note, after FROM action_journal WHERE approval_id = ? ORDER BY id DESC LIMIT 1", (approval_id,)
    ).fetchone()
    return execution({"result": entry["note"], "error": json.loads(entry["after"]).get("error")}, at)
