"""Ember's mailbox in SQLite: storing what a fetch brought, the opt-outs, and what the agent's tools read.

A fetch runs at the start of a wake cycle, outside the sealed tool handlers (in live mode it needs the
network), within a time budget. Errors never stop the cycle: they are recorded in the events and in the
meta keys ``integrations.email.<mode>.*``, which the dashboard shows. Everything is scoped to a mode and a
dry-run session, like the agent's other records. Functions that change something take the caller's
connection and run inside its transaction.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

from .. import events
from ..agent.store import AgentScope as Scope
from ..db import Database
from ..economy.clock import Clock, to_iso
from ..logging_setup import redact
from .mail import MAX_FETCH, FetchResult, IncomingMail, Mailbox, MailError, one_line
from .optout import opt_out

log = logging.getLogger(__name__)

FETCH_BUDGET_SECONDS = 30.0
# 0.12.0: fetches per check while new mail waits (MAX_FETCH each, the oldest first): a backlog is read over a few
# checks, never dropped (the newest 20 were kept and the rest lost for good).
FETCH_ROUNDS = 5
ERROR_CHARS = 300


def meta_key(mode: str, name: str) -> str:
    return f"integrations.email.{mode}.{name}"


@dataclass
class Fetched:
    stored: list[int] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    waiting: int = 0  # new emails left for the next check
    error: str | None = None


def fetch(db: Database, clock: Clock, scope: Scope, mailbox: Mailbox, budget: float = FETCH_BUDGET_SECONDS) -> Fetched:
    """Fetch new mail, the oldest first, store it, apply the replies that ask to stop and record how it went. Up to
    FETCH_ROUNDS fetches while more waits and time is left; what is left waits for the next check. Never raises."""
    result = Fetched()
    now = to_iso(clock.now())
    deadline = time.monotonic() + budget
    last = _int(db.get_meta(meta_key(scope.mode, "last_uid")))
    validity = _int(db.get_meta(meta_key(scope.mode, "uidvalidity"))) or None
    for _ in range(FETCH_ROUNDS):
        try:
            fetched = mailbox.fetch_new(last, MAX_FETCH, validity, deadline)
        except MailError as exc:
            result.error = redact(str(exc))[:ERROR_CHARS]
        except Exception as exc:  # noqa: BLE001 - reading mail must never end the cycle
            log.exception("Fetching mail failed")
            result.error = redact(f"{type(exc).__name__}: {exc}")[:ERROR_CHARS]
        if result.error is not None:
            break
        _store(db, scope, mailbox, fetched, now, result)
        last, validity = fetched.last_uid, fetched.uidvalidity or None
        db.set_meta(meta_key(scope.mode, "last_uid"), str(fetched.last_uid))
        db.set_meta(meta_key(scope.mode, "uidvalidity"), str(fetched.uidvalidity or ""))
        result.waiting = fetched.waiting
        if not fetched.waiting or not fetched.mails or time.monotonic() > deadline:
            break
    if result.stored:
        count = len(result.stored)
        events.record(db, "info", "email", f"{count} new email{'s' if count > 1 else ''} in Ember's mailbox")
    for _ in result.suppressed:
        events.record(db, "info", "email", "A sender asked not to get emails; Ember won't write to them again")
    if result.error is not None:
        db.set_meta(meta_key(scope.mode, "last_error"), result.error)
        events.record(db, "warning", "email", f"Checking Ember's mailbox failed: {result.error}")
        return result
    db.set_meta(meta_key(scope.mode, "last_fetch_at"), now)
    db.set_meta(meta_key(scope.mode, "last_error"), "")
    if result.waiting:
        events.record(db, "info", "email", f"{result.waiting} more new email(s) wait for the next check of the mailbox")
    return result


def _store(db: Database, scope: Scope, mailbox: Mailbox, fetched: FetchResult, now: str, result: Fetched) -> None:
    """One fetch's emails, each stored once; a sender whose email asks to stop is never written to again."""
    with db.transaction() as conn:
        for mail in fetched.mails:
            email_id = store_incoming(conn, scope, mail, fetched.uidvalidity or 0, now)
            if email_id is None:
                continue
            result.stored.append(email_id)
            own = mail.from_addr.lower() == mailbox.address.lower()  # Ember's own address is never suppressed
            # A newsletter's "unsubscribe" is about Ember leaving it (0.12.0).
            words = opt_out(mail.subject, mail.body) if mail.from_addr and not own and not mail.bulk else None
            if words is not None and suppress(conn, scope, mail.from_addr, now, f'replied "{words}"', email_id):
                result.suppressed.append(mail.from_addr)


def _int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def store_incoming(
    conn: sqlite3.Connection, scope: Scope, mail: IncomingMail, uidvalidity: int, now: str
) -> int | None:
    """Store one fetched email once; returns its id, or None if it is already stored."""
    where, params = scope.where()
    if (
        mail.message_id
        and conn.execute(
            f"SELECT 1 FROM emails WHERE {where} AND direction = 'in' AND message_id = ?", (*params, mail.message_id)
        ).fetchone()
    ):
        return None  # the same email again (after the server renumbered its mailbox)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, in_reply_to,"
        " references_, from_addr, from_name, to_addr, subject, sent_at, received_at, body, body_cut, attachments,"
        " bulk) VALUES (?, ?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id or None,
            uidvalidity,
            mail.uid,
            mail.message_id,
            mail.in_reply_to,
            mail.references,
            mail.from_addr,
            mail.from_name,
            mail.to_addr,
            mail.subject,
            mail.sent_at,
            now,
            mail.body,
            1 if mail.body_cut else 0,
            mail.attachments_json(),
            1 if mail.bulk else 0,  # 0.13.0: a list's or a machine's mail is no inquiry
        ),
    )
    return int(cursor.lastrowid) if cursor.rowcount == 1 else None


def store_outgoing(
    conn: sqlite3.Connection,
    scope: Scope,
    *,
    approval_id: int,
    message_id: str,
    in_reply_to: str | None,
    references: str | None,
    from_addr: str,
    from_name: str,
    to_addr: str,
    subject: str,
    body: str,
    now: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO emails (mode, session, life_id, direction, message_id, in_reply_to, references_, from_addr,"
        " from_name, to_addr, subject, sent_at, received_at, body, approval_id)"
        " VALUES (?, ?, ?, 'out', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            scope.life_id or None,
            message_id,
            in_reply_to,
            references,
            from_addr,
            one_line(from_name, 200),
            to_addr,
            subject,
            now,
            now,
            body[:8_000],
            approval_id,
        ),
    )
    return int(cursor.lastrowid)


# --- opt-outs ---


def suppress(
    conn: sqlite3.Connection, scope: Scope, address: str, since: str, reason: str, email_id: int | None
) -> bool:
    """Never email ``address`` again (in this mode and session). Returns True if it is new."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO email_suppressions (mode, session, address, since, reason, email_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, address.lower(), since, reason[:300] or "asked to stop", email_id),
    )
    return cursor.rowcount == 1


def suppressions(conn: sqlite3.Connection, scope: Scope, limit: int = 20) -> tuple[int, list[dict[str, str]]]:
    """How many addresses Ember never emails, and the newest ``limit`` of them (0.12.0: for the owner)."""
    where, params = scope.where()
    count = conn.execute(f"SELECT COUNT(*) FROM email_suppressions WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT address, since, reason FROM email_suppressions WHERE {where} ORDER BY since DESC, address LIMIT ?",
        (*params, limit),
    ).fetchall()
    return int(count), [dict(r) for r in rows]


def is_suppressed(conn: sqlite3.Connection, scope: Scope, address: str) -> bool:
    where, params = scope.where()
    row = conn.execute(
        f"SELECT 1 FROM email_suppressions WHERE {where} AND address = ?", (*params, address.lower())
    ).fetchone()
    return row is not None


def has_written(conn: sqlite3.Connection, scope: Scope, address: str) -> bool:
    """Whether Ember ever received mail from ``address`` (if not, an email to it is a first contact)."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT 1 FROM emails WHERE {where} AND direction = 'in' AND lower(from_addr) = ? LIMIT 1",
        (*params, address.lower()),
    ).fetchone()
    return row is not None


# --- inquiries (0.13.0, Phase E1): the emails of people that wait for an answer ---

INQUIRIES_SHOWN = 6
_MACHINE = re.compile(r"^(?:mailer-daemon|postmaster|no-?reply|do-?not-?reply)@", re.IGNORECASE)


def machine(address: str) -> bool:
    """Whether an address is a machine's (bounces, no-reply senders): its mail is no inquiry."""
    return _MACHINE.match(address.strip()) is not None


# An email request that answers someone: made after their email arrived, and not turned down or dropped.
_ANSWERED = (
    "EXISTS (SELECT 1 FROM approvals a WHERE a.mode = e.mode AND a.session = e.session AND a.executor = 'email'"
    " AND lower(json_extract(a.action, '$.to')) = lower(e.from_addr) AND a.created_at >= e.received_at"
    " AND a.status NOT IN ('rejected', 'withdrawn', 'expired', 'failed'))"
)


def inquiries(conn: sqlite3.Connection, scope: Scope, own: str | None = None) -> list[sqlite3.Row]:
    """The emails of people that wait for an answer, the oldest first: received (since 0.13.0), not from a list or a
    machine, not from Ember's own address, from someone who didn't ask to stop, neither answered nor closed."""
    where, params = scope.where("e")
    rows = conn.execute(
        f"SELECT e.* FROM emails e WHERE {where} AND e.direction = 'in' AND e.bulk = 0 AND lower(e.from_addr) <> ?"
        " AND NOT EXISTS (SELECT 1 FROM email_suppressions s WHERE s.mode = e.mode AND s.session = e.session"
        " AND s.address = lower(e.from_addr)) AND NOT EXISTS (SELECT 1 FROM inquiry_closures c WHERE"
        f" c.email_id = e.id) AND NOT {_ANSWERED} ORDER BY e.id",
        (*params, (own or "").lower()),
    ).fetchall()
    return [r for r in rows if not machine(str(r["from_addr"]))]


def is_inquiry(conn: sqlite3.Connection, scope: Scope, email_id: int) -> bool:
    return any(r["id"] == email_id for r in inquiries(conn, scope))


def close_inquiry(conn: sqlite3.Connection, email_id: int, reason: str, by: str, now: str) -> None:
    """An inquiry that needs no answer, closed with why (final)."""
    conn.execute(
        "INSERT INTO inquiry_closures (email_id, reason, by, created_at) VALUES (?, ?, ?, ?)",
        (email_id, reason[:300], by[:60], now),
    )


def counts(conn: sqlite3.Connection, scope: Scope, since: str) -> tuple[int, int]:
    """People's emails received since then, and how many of them an email Ember sent answered (the metrics
    inquiries_received and inquiries_answered)."""
    where, params = scope.where("e")
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(EXISTS (SELECT 1 FROM emails o WHERE o.mode = e.mode AND o.session ="
        " e.session AND o.direction = 'out' AND lower(o.to_addr) = lower(e.from_addr) AND COALESCE(o.sent_at,"
        f" o.received_at) >= e.received_at)), 0) FROM emails e WHERE {where} AND e.direction = 'in' AND e.bulk = 0"
        " AND e.received_at >= ?",
        (*params, since),
    ).fetchone()
    return int(row[0]), int(row[1])


# --- what the agent reads ---


def unread(conn: sqlite3.Connection, scope: Scope, limit: int = 3) -> tuple[int, list[sqlite3.Row]]:
    """How many incoming emails the agent hasn't read, and the newest of them."""
    where, params = scope.where()
    condition = f"{where} AND direction = 'in' AND read_by_agent_at IS NULL"
    count = conn.execute(f"SELECT COUNT(*) FROM emails WHERE {condition}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, from_addr, subject FROM emails WHERE {condition} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()
    return int(count), rows


def inbox(conn: sqlite3.Connection, scope: Scope, unread_only: bool = False, limit: int = 15) -> list[sqlite3.Row]:
    where, params = scope.where()
    only = " AND read_by_agent_at IS NULL" if unread_only else ""
    return conn.execute(
        f"SELECT * FROM emails WHERE {where} AND direction = 'in'{only} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def email(conn: sqlite3.Connection, scope: Scope, email_id: int) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM emails WHERE id = ? AND {where}", (email_id, *params)).fetchone()


def mark_read(conn: sqlite3.Connection, email_id: int, now: str, cycle_id: int) -> None:
    """The agent opened this email (in wake cycle ``cycle_id``): it no longer counts as unread."""
    conn.execute(
        "UPDATE emails SET read_by_agent_at = ?, seen_cycle_id = ? WHERE id = ? AND read_by_agent_at IS NULL",
        (now, cycle_id, email_id),
    )


def last_fetch(db: Database, mode: str) -> tuple[str | None, str | None]:
    """(when the mailbox was last read, the last error or None)."""
    return db.get_meta(meta_key(mode, "last_fetch_at")) or None, db.get_meta(meta_key(mode, "last_error")) or None


_REFERENCE = re.compile(r"<[^<>\s]{1,250}>")


def thread_headers(row: sqlite3.Row) -> tuple[str | None, str | None]:
    """In-Reply-To and References for a reply to the stored email ``row`` (the last few message ids)."""
    message_id = row["message_id"]
    if not message_id or not _REFERENCE.fullmatch(message_id):
        return None, None
    earlier = _REFERENCE.findall(row["references_"] or "")
    chain = [*[r for r in earlier if r != message_id][-4:], message_id]
    return message_id, " ".join(chain)
