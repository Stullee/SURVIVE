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
from .mail import MAX_FETCH, IncomingMail, Mailbox, MailError, one_line

log = logging.getLogger(__name__)

FETCH_BUDGET_SECONDS = 30.0
ERROR_CHARS = 300
STOP_WORDS = frozenset({"stop", "unsubscribe", "abmelden"})


def meta_key(mode: str, name: str) -> str:
    return f"integrations.email.{mode}.{name}"


@dataclass
class Fetched:
    stored: list[int] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    skipped: int = 0
    error: str | None = None


def fetch(db: Database, clock: Clock, scope: Scope, mailbox: Mailbox, budget: float = FETCH_BUDGET_SECONDS) -> Fetched:
    """Fetch new mail, store it, apply "stop" replies and record how it went. Never raises."""
    result = Fetched()
    now = to_iso(clock.now())
    last = _int(db.get_meta(meta_key(scope.mode, "last_uid")))
    validity = _int(db.get_meta(meta_key(scope.mode, "uidvalidity"))) or None
    try:
        fetched = mailbox.fetch_new(last, MAX_FETCH, validity, time.monotonic() + budget)
    except MailError as exc:
        result.error = redact(str(exc))[:ERROR_CHARS]
    except Exception as exc:  # noqa: BLE001 - reading mail must never end the cycle
        log.exception("Fetching mail failed")
        result.error = redact(f"{type(exc).__name__}: {exc}")[:ERROR_CHARS]
    if result.error is not None:
        db.set_meta(meta_key(scope.mode, "last_error"), result.error)
        events.record(db, "warning", "email", f"Checking Ember's mailbox failed: {result.error}")
        return result
    with db.transaction() as conn:
        for mail in fetched.mails:
            email_id = store_incoming(conn, scope, mail, fetched.uidvalidity or 0, now)
            if email_id is None:
                continue
            result.stored.append(email_id)
            own = mail.from_addr.lower() == mailbox.address.lower()  # Ember's own address is never suppressed
            if _asks_to_stop(mail.body) and mail.from_addr and not own:
                reason = f'replied "{_first_line(mail.body)}"'
                if suppress(conn, scope, mail.from_addr, now, reason, email_id):
                    result.suppressed.append(mail.from_addr)
    result.skipped = fetched.skipped
    db.set_meta(meta_key(scope.mode, "last_uid"), str(fetched.last_uid))
    db.set_meta(meta_key(scope.mode, "uidvalidity"), str(fetched.uidvalidity or ""))
    db.set_meta(meta_key(scope.mode, "last_fetch_at"), now)
    db.set_meta(meta_key(scope.mode, "last_error"), "")
    if result.stored:
        count = len(result.stored)
        events.record(db, "info", "email", f"{count} new email{'s' if count > 1 else ''} in Ember's mailbox")
    if result.skipped:
        events.record(
            db, "warning", "email", f"{result.skipped} older new email(s) were skipped: at most {MAX_FETCH} per check"
        )
    for _ in result.suppressed:
        events.record(db, "info", "email", "A sender asked not to get emails; Ember won't write to them again")
    return result


def _int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def _first_line(body: str) -> str:
    return next((line.strip() for line in body.splitlines() if line.strip()), "")[:40]


def _asks_to_stop(body: str) -> bool:
    """The first non-empty line is only "stop", "unsubscribe" or "abmelden" (any case, trailing punctuation)."""
    return _first_line(body).lower().rstrip(".!") in STOP_WORDS


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
        " references_, from_addr, from_name, to_addr, subject, sent_at, received_at, body, body_cut, attachments)"
        " VALUES (?, ?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
