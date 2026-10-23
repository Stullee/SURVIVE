"""Ember's mailbox in SQLite: storing what a fetch brought, the opt-outs, and what the agent's tools read.

A fetch runs at the start of a wake cycle, outside the sealed tool handlers (in live mode it needs the
network), within a time budget. Errors never stop the cycle: they are recorded in the events and in the
meta keys ``integrations.email.<mode>.*``, which the dashboard shows. Everything is scoped to a mode and a
dry-run session, like the agent's other records. Functions that change something take the caller's
connection and run inside its transaction.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import timedelta

from .. import events
from ..agent.store import AgentScope as Scope
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from ..logging_setup import redact
from .mail import MAX_FETCH, TEXT_VERSION, FetchResult, IncomingMail, Mailbox, MailError, one_line
from .optout import opt_out

log = logging.getLogger(__name__)

FETCH_BUDGET_SECONDS = 30.0
# 0.12.0: fetches per check while new mail waits (MAX_FETCH each, the oldest first): a backlog is read over a few
# checks, never dropped (the newest 20 were kept and the rest lost for good).
FETCH_ROUNDS = 5
ERROR_CHARS = 300
# 0.15.0: a mailbox that keeps failing (a changed password, a locked account) is read less often: the wait between
# reads doubles with each failed read in a row after the first, up to BACKOFF_MINUTES; a read that works resets it.
BACKOFF_MINUTES = 240
REFUSED_TRIES = 5  # 0.15.0: checks in a row the server may refuse an email before Ember skips it
# 0.45.4: after a change to how an email's text is read (mail.TEXT_VERSION), the emails stored in these days with an
# older one are read again from the server, this many per check, the newest first (reread)
REREAD_DAYS = 30
REREAD_BATCH = 10


def meta_key(mode: str, name: str) -> str:
    return f"integrations.email.{mode}.{name}"


@dataclass
class Fetched:
    stored: list[int] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    waiting: int = 0  # new emails left for the next check
    error: str | None = None
    reread: list[int] = field(default_factory=list)  # 0.45.4: stored emails read again whose text changed


def fetch(db: Database, clock: Clock, scope: Scope, mailbox: Mailbox, budget: float = FETCH_BUDGET_SECONDS) -> Fetched:
    """Fetch new mail, the oldest first, store it, apply the replies that ask to stop and record how it went. Up to
    FETCH_ROUNDS fetches while more waits and time is left; what is left waits for the next check. Never raises.
    0.39.5: storing is part of the read, so a read that fails while storing is recorded and shown like any other
    (the mailbox's last error, on the dashboard and on an approved email's card); it raised past the record, and
    every read after it failed the same way, unseen."""
    result = Fetched()
    now = to_iso(clock.now())
    deadline = time.monotonic() + budget
    last = _int(db.get_meta(meta_key(scope.mode, "last_uid")))
    validity = _int(db.get_meta(meta_key(scope.mode, "uidvalidity"))) or None
    for _ in range(FETCH_ROUNDS):
        try:
            fetched = mailbox.fetch_new(last, MAX_FETCH, validity, deadline)
            _store(db, scope, mailbox, fetched, now, result)
        except MailError as exc:
            result.error = redact(str(exc))[:ERROR_CHARS]
        except Exception as exc:  # noqa: BLE001 - reading mail must never end the cycle
            log.exception("Reading the mailbox failed")
            result.error = redact(f"{type(exc).__name__}: {exc}")[:ERROR_CHARS]
        if result.error is not None:
            break
        last, validity = _refused(db, scope.mode, fetched), fetched.uidvalidity or None
        db.set_meta(meta_key(scope.mode, "last_uid"), str(last))
        db.set_meta(meta_key(scope.mode, "uidvalidity"), str(fetched.uidvalidity or ""))
        result.waiting = fetched.waiting
        if not fetched.waiting or not fetched.mails or fetched.refused is not None or time.monotonic() > deadline:
            break
    if result.stored:
        count = len(result.stored)
        events.record(db, "info", "email", f"{count} new email{'s' if count > 1 else ''} in Ember's mailbox")
    for _ in result.suppressed:
        events.record(db, "info", "email", "A sender asked not to get emails; Ember won't write to them again")
    failures = _int(db.get_meta(meta_key(scope.mode, "failures")))
    if result.error is not None:
        # 0.15.0: one warning while the same error goes on (it was one every 15 minutes), and a longer wait
        if not failures or result.error != db.get_meta(meta_key(scope.mode, "last_error")):
            events.record(db, "warning", "email", f"Checking Ember's mailbox failed: {result.error}")
        db.set_meta(meta_key(scope.mode, "last_error"), result.error)
        db.set_meta(meta_key(scope.mode, "failures"), str(failures + 1))
        db.set_meta(meta_key(scope.mode, "failed_at"), now)
        return result
    if failures:
        events.record(db, "info", "email", f"Ember's mailbox can be read again (after {failures} failed checks)")
    db.set_meta(meta_key(scope.mode, "failures"), "0")
    db.set_meta(meta_key(scope.mode, "last_fetch_at"), now)
    db.set_meta(meta_key(scope.mode, "last_error"), "")
    db.set_meta(meta_key(scope.mode, "left"), str(result.waiting))  # 0.37.3: what it left for the next read (read_at)
    if result.waiting:
        events.record(db, "info", "email", f"{result.waiting} more new email(s) wait for the next check of the mailbox")
    elif time.monotonic() < deadline:  # 0.45.4: new mail first; what time is left reads stored emails again
        reread(db, clock, scope, mailbox, deadline, result)
    return result


def _store(db: Database, scope: Scope, mailbox: Mailbox, fetched: FetchResult, now: str, result: Fetched) -> None:
    """One fetch's emails, each stored once; a sender whose email asks to stop is never written to again. 0.39.5:
    each email in a savepoint of its own: one the database refuses as it is is stored with its headers only
    (IncomingMail.headers_only), and the others are kept. One such email rolled back all of its fetch, a "stop"
    before it included, and the read failed on it again every time."""
    stored: list[int] = []
    suppressed: list[str] = []
    cut: list[int] = []
    with db.transaction() as conn:
        for mail in fetched.mails:
            conn.execute("SAVEPOINT incoming")
            try:
                email_id, asked = _store_one(conn, scope, mailbox, mail, fetched.uidvalidity or 0, now)
            except Exception:  # noqa: BLE001 - one email Ember can't store must never stop the mailbox being read
                log.exception("Storing email number %d failed: its headers only are stored", mail.uid)
                conn.execute("ROLLBACK TO incoming")
                email_id, asked = _store_one(conn, scope, mailbox, mail.headers_only(), fetched.uidvalidity or 0, now)
                cut.append(mail.uid)
            conn.execute("RELEASE incoming")
            stored += [email_id] if email_id is not None else []
            suppressed += asked
    result.stored += stored
    result.suppressed += suppressed
    for uid in cut:
        events.record(
            db,
            "warning",
            "email",
            f"Ember couldn't store email number {uid} as it was, so only its headers were stored: read it in the"
            " mailbox's webmail",
        )


def _store_one(
    conn: sqlite3.Connection, scope: Scope, mailbox: Mailbox, mail: IncomingMail, uidvalidity: int, now: str
) -> tuple[int | None, list[str]]:
    """One email, stored once, and the addresses it asks Ember never to write to again: (its id, or None if it was
    stored before; the addresses newly suppressed)."""
    email_id = store_incoming(conn, scope, mail, uidvalidity, now, mailbox.address)
    if email_id is None:
        return None, []
    return email_id, _asked(conn, scope, mailbox, mail, email_id, now)


def _asked(
    conn: sqlite3.Connection, scope: Scope, mailbox: Mailbox, mail: IncomingMail, email_id: int, now: str
) -> list[str]:
    """The addresses stored email ``email_id`` (its text in ``mail``) asks Ember never to write to again, now
    suppressed (those that weren't before)."""
    own = mail.from_addr.lower() == mailbox.address.lower()  # Ember's own address is never suppressed
    if not mail.from_addr or own:
        return []
    # 0.15.0: "replied" only to an email Ember sent (live: a provider's own mail was said to reply)
    answered = _answered(conn, scope, mail)
    # A newsletter's "unsubscribe" is about Ember leaving it (0.12.0). 0.21.0: unless it answers Ember's own
    # email: a "stop" sent through a group or a list (with its List-Id) was ignored.
    words = opt_out(mail.subject, mail.body) if answered or not mail.bulk else None
    if words is None:
        return []
    asked = []
    how = "replied" if answered else "wrote"
    if suppress(conn, scope, mail.from_addr, now, f'{how} "{words}"', email_id):
        asked.append(mail.from_addr)
    # 0.15.0: a stop in answer to Ember's email covers the address Ember wrote to as well (a colleague may
    # answer for info@); it was only the sender's.
    reason = f"an answer to Ember's email to this address asked to stop (email #{email_id})"
    for to in answered:
        if to not in (mail.from_addr.lower(), mailbox.address.lower()) and suppress(
            conn, scope, to, now, reason, email_id
        ):
            asked.append(to)
    return asked


def _answered(conn: sqlite3.Connection, scope: Scope, mail: IncomingMail) -> list[str]:
    """The addresses of the emails Ember sent that ``mail`` answers (by In-Reply-To and References)."""
    ids = sorted({mail.in_reply_to or "", *(mail.references or "").split()} - {""})
    if not ids:
        return []
    where, params = scope.where()
    marks = ", ".join("?" for _ in ids)
    found = conn.execute(
        f"SELECT DISTINCT lower(to_addr) FROM emails WHERE {where} AND direction = 'out' AND message_id IN ({marks})",
        (*params, *ids),
    )
    return sorted(str(r[0]) for r in found)


def reread(db: Database, clock: Clock, scope: Scope, mailbox: Mailbox, deadline: float, result: Fetched) -> None:
    """0.45.4: the emails stored in the last REREAD_DAYS with an older TEXT_VERSION, read again from the server, the
    newest REREAD_BATCH first. A stored email never changes: what was read again is kept beside it (email_rereads),
    once per version, and a text that changed is the one email_read shows, unread until the agent opens it; a "stop"
    in it counts as in a new email. Live, KDP's "Attention needed" emails #64 and #65 were stored as their footer and
    case number, and 0.45.2's better reading couldn't reach them. Emails of a mailbox the server renumbered since aren't
    read (their UIDs are other emails). Never raises: a read that fails is tried again with the next check."""
    validity = _int(db.get_meta(meta_key(scope.mode, "uidvalidity")))
    since = to_iso(clock.now() - timedelta(days=REREAD_DAYS))
    where, params = scope.where("e")
    with db.connection() as conn:
        due = conn.execute(
            f"SELECT e.id, e.uid, e.message_id, e.body, e.body_cut, e.attachments FROM emails e WHERE {where}"
            " AND e.direction = 'in' AND COALESCE(e.text_version, 0) < ? AND e.uidvalidity = ? AND e.received_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM email_rereads r WHERE r.email_id = e.id AND r.text_version >= ?)"
            " ORDER BY e.id DESC LIMIT ?",
            (*params, TEXT_VERSION, validity, since, TEXT_VERSION, REREAD_BATCH),
        ).fetchall()
    if not due or not validity:
        return
    try:
        found = mailbox.fetch_again([int(r["uid"]) for r in due], validity, deadline)
    except Exception as exc:  # noqa: BLE001 - reading mail again must never end the cycle, nor fail the read
        log.warning("Reading stored emails again failed: %s", redact(str(exc))[:ERROR_CHARS])
        return
    now = to_iso(clock.now())
    asked: list[str] = []
    with db.transaction() as conn:
        for row in due:
            if int(row["uid"]) not in found:
                continue  # out of time: the next check
            conn.execute("SAVEPOINT reread")
            try:
                changed, stops = _reread_one(conn, scope, mailbox, row, found[int(row["uid"])], now)
            except Exception:  # noqa: BLE001 - one email Ember can't store again keeps the text it has
                log.exception("Storing email #%d as read again failed", row["id"])
                conn.execute("ROLLBACK TO reread")
                _reread_row(conn, int(row["id"]), now, "gone")
                changed, stops = False, []
            conn.execute("RELEASE reread")
            result.reread += [int(row["id"])] if changed else []
            asked += stops
    result.suppressed += asked
    if result.reread:
        listed = ", ".join(f"#{n}" for n in sorted(result.reread))
        events.record(db, "info", "email", f"Ember read stored emails again from the server; new text in {listed}")
    for _ in asked:
        events.record(db, "info", "email", "A sender asked not to get emails; Ember won't write to them again")


def _reread_one(
    conn: sqlite3.Connection, scope: Scope, mailbox: Mailbox, row: sqlite3.Row, mail: IncomingMail | None, now: str
) -> tuple[bool, list[str]]:
    """One stored email (``row``) as the server handed it over again: whether its text changed, and the addresses it
    asks Ember never to write to again (newly suppressed). The server's UIDs are never another email's while its
    UIDVALIDITY holds; one with another Message-ID counts as gone all the same."""
    if mail is None or (row["message_id"] and mail.message_id != row["message_id"]):
        _reread_row(conn, int(row["id"]), now, "gone")
        return False, []
    newest = (
        conn.execute(
            "SELECT body, body_cut, attachments FROM email_rereads WHERE email_id = ? AND status = 'changed'"
            " ORDER BY text_version DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        or row
    )
    if (mail.body, int(mail.body_cut), mail.attachments_json()) == (
        newest["body"],
        int(newest["body_cut"]),
        newest["attachments"],
    ):
        _reread_row(conn, int(row["id"]), now, "same")
        return False, []
    _reread_row(conn, int(row["id"]), now, "changed", mail)
    return True, _asked(conn, scope, mailbox, mail, int(row["id"]), now)


def _reread_row(
    conn: sqlite3.Connection, email_id: int, now: str, status: str, mail: IncomingMail | None = None
) -> None:
    conn.execute(
        "INSERT INTO email_rereads (email_id, text_version, read_at, status, body, body_cut, attachments)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            email_id,
            TEXT_VERSION,
            now,
            status,
            None if mail is None else mail.body,
            None if mail is None else int(mail.body_cut),
            None if mail is None else mail.attachments_json(),
        ),
    )


def _refused(db: Database, mode: str, fetched: FetchResult) -> int:
    """0.15.0: where the next fetch starts. An email the server didn't hand over is asked for again at the next check,
    and skipped with a warning once it was refused REFUSED_TRIES times in a row."""
    key = meta_key(mode, "refused")
    if fetched.refused is None:
        db.set_meta(key, "")
        return fetched.last_uid
    seen, _, count = (db.get_meta(key) or "").partition(":")
    tries = _int(count) + 1 if seen == str(fetched.refused) else 1
    db.set_meta(key, f"{fetched.refused}:{tries}")
    if tries < REFUSED_TRIES:
        return fetched.last_uid
    events.record(
        db,
        "warning",
        "email",
        f"The mail server didn't hand over one email (number {fetched.refused}) in {tries} checks: Ember skipped it."
        " Read it in the mailbox's webmail",
    )
    return fetched.refused


def wait_minutes(db: Database, mode: str, every: int) -> int:
    """0.15.0: the minutes between reads of the mailbox between cycles: ``every``, doubled for each failed read in a
    row after the first, at most BACKOFF_MINUTES."""
    failures = _int(db.get_meta(meta_key(mode, "failures")))
    return min(every * 2 ** min(max(failures - 1, 0), 10), max(every, BACKOFF_MINUTES))


def due(db: Database, clock: Clock, mode: str, every: int) -> bool:
    """0.15.0: whether the mailbox may be read now: always while it works or failed once, else once wait_minutes have
    passed since the last failed read (so the read at the start of each cycle backs off too)."""
    failed_at = db.get_meta(meta_key(mode, "failed_at"))
    if _int(db.get_meta(meta_key(mode, "failures"))) < 2 or not failed_at:
        return True
    return clock.now() - from_iso(failed_at) >= timedelta(minutes=wait_minutes(db, mode, every))


def read_at(db: Database, mode: str) -> str | None:
    """0.37.3: when the mailbox was last read through: the start of the last read that worked and left no new email for
    the next one (None: never, or the last read that worked left some, so a "stop" may still wait unread)."""
    if _int(db.get_meta(meta_key(mode, "left"))):
        return None
    return db.get_meta(meta_key(mode, "last_fetch_at")) or None


def _int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def store_incoming(
    conn: sqlite3.Connection, scope: Scope, mail: IncomingMail, uidvalidity: int, now: str, own: str = ""
) -> int | None:
    """Store one fetched email once; returns its id, or None if it is already stored. 0.15.0: a machine's mail and
    mail from Ember's own address (``own``) are stored as no person's (bulk), like a list's: Ember can't answer its
    own address, and nothing could close it."""
    where, params = scope.where()
    if (
        mail.message_id
        and conn.execute(
            f"SELECT 1 FROM emails WHERE {where} AND direction = 'in' AND message_id = ?", (*params, mail.message_id)
        ).fetchone()
    ):
        return None  # the same email again (after the server renumbered its mailbox)
    own_mail = bool(own) and mail.from_addr.lower() == own.lower()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, in_reply_to,"
        " references_, from_addr, from_name, to_addr, subject, sent_at, received_at, body, body_cut, attachments,"
        " bulk, authenticated, text_version) VALUES (?, ?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            1 if mail.bulk or mail.machine or own_mail else 0,  # no inquiry (0.15.0: nor a machine's or Ember's own)
            1 if mail.authenticated else 0,  # 0.15.0: the receiving provider verified the sender
            TEXT_VERSION,  # 0.45.4: how its text was read (reread)
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


def person(alias: str = "") -> str:
    """0.15.0: the one test of a person's email, for all that rests on someone having written (the first-contact
    guard, inquiries and their metrics, answers in their thread, event wakes): received, its sender verified by the
    receiving mail provider (authenticated), and no list's, machine's or Ember's own (bulk). Mail stored before 0.15.0
    has no verdict (NULL) and never counts. The database's NEVER view (approvals_never) reads the same."""
    e = f"{alias}." if alias else ""
    return f"({e}direction = 'in' AND {e}authenticated = 1 AND {e}bulk = 0)"


def has_written(conn: sqlite3.Connection, scope: Scope, address: str) -> bool:
    """Whether a person ever wrote to Ember from ``address`` (if not, an email to it is a first contact)."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT 1 FROM emails WHERE {where} AND {person()} AND lower(from_addr) = ? LIMIT 1",
        (*params, address.lower()),
    ).fetchone()
    return row is not None


# --- inquiries (0.13.0, Phase E1): the emails of people that wait for an answer ---

INQUIRIES_SHOWN = 6


# An email request that answers someone: made after their email arrived, and not turned down or dropped.
_ANSWERED = (
    "EXISTS (SELECT 1 FROM approvals a WHERE a.mode = e.mode AND a.session = e.session AND a.executor = 'email'"
    " AND lower(json_extract(a.action, '$.to')) = lower(e.from_addr) AND a.created_at >= e.received_at"
    " AND a.status NOT IN ('rejected', 'withdrawn', 'expired', 'failed'))"
)


def inquiries(conn: sqlite3.Connection, scope: Scope) -> list[sqlite3.Row]:
    """The emails of people that wait for an answer, the oldest first: a person's (``person``: 0.15.0, verified, and
    no list's, machine's or Ember's own), from someone who didn't ask to stop, neither answered nor closed."""
    where, params = scope.where("e")
    return conn.execute(
        f"SELECT e.* FROM emails e WHERE {where} AND {person('e')}"
        " AND NOT EXISTS (SELECT 1 FROM email_suppressions s WHERE s.mode = e.mode AND s.session = e.session"
        " AND s.address = lower(e.from_addr)) AND NOT EXISTS (SELECT 1 FROM inquiry_closures c WHERE"
        f" c.email_id = e.id) AND NOT {_ANSWERED} ORDER BY e.id",
        params,
    ).fetchall()


def is_inquiry(conn: sqlite3.Connection, scope: Scope, email_id: int) -> bool:
    return any(r["id"] == email_id for r in inquiries(conn, scope))


def close_inquiry(conn: sqlite3.Connection, email_id: int, reason: str, by: str, now: str) -> None:
    """An inquiry that needs no answer, closed with why (final)."""
    conn.execute(
        "INSERT INTO inquiry_closures (email_id, reason, by, created_at) VALUES (?, ?, ?, ?)",
        (email_id, reason[:300], by[:60], now),
    )


def counts(conn: sqlite3.Connection, scope: Scope, since: str) -> tuple[int, int]:
    """People's emails (``person``) received since then, and how many of them an email Ember sent answered (the
    metrics inquiries_received and inquiries_answered)."""
    where, params = scope.where("e")
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(EXISTS (SELECT 1 FROM emails o WHERE o.mode = e.mode AND o.session ="
        " e.session AND o.direction = 'out' AND lower(o.to_addr) = lower(e.from_addr) AND COALESCE(o.sent_at,"
        f" o.received_at) >= e.received_at)), 0) FROM emails e WHERE {where} AND {person('e')}"
        " AND e.received_at >= ?",
        (*params, since),
    ).fetchone()
    return int(row[0]), int(row[1])


# --- what the agent reads ---

# 0.45.4: an email read again from the server whose new text the agent hasn't opened (reread)
_AGAIN = (
    "EXISTS (SELECT 1 FROM email_rereads x WHERE x.email_id = emails.id AND x.status = 'changed'"
    " AND x.read_by_agent_at IS NULL)"
)
# An incoming email the agent hasn't read: not opened, or (0.45.4) with a text read again it hasn't opened
_UNREAD = f"(read_by_agent_at IS NULL OR {_AGAIN})"


def unread(conn: sqlite3.Connection, scope: Scope, limit: int = 3) -> tuple[int, list[sqlite3.Row]]:
    """How many incoming emails the agent hasn't read, and the newest of them (``again``: 0.45.4, one it read before
    whose text read again from the server it hasn't)."""
    where, params = scope.where()
    condition = f"{where} AND direction = 'in' AND {_UNREAD}"
    count = conn.execute(f"SELECT COUNT(*) FROM emails WHERE {condition}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, from_addr, subject, read_by_agent_at IS NOT NULL AS again FROM emails WHERE {condition}"
        " ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return int(count), rows


def inbox(conn: sqlite3.Connection, scope: Scope, unread_only: bool = False, limit: int = 15) -> list[sqlite3.Row]:
    """The newest incoming emails, each with whether the agent hasn't read it (``unread``)."""
    where, params = scope.where()
    only = f" AND {_UNREAD}" if unread_only else ""
    return conn.execute(
        f"SELECT *, {_UNREAD} AS unread FROM emails WHERE {where} AND direction = 'in'{only} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()


def email(conn: sqlite3.Connection, scope: Scope, email_id: int) -> sqlite3.Row | None:
    """A stored email, with whether the agent hasn't read it (``unread``)."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT *, {_UNREAD} AS unread FROM emails WHERE id = ? AND {where}", (email_id, *params)
    ).fetchone()


def text_again(conn: sqlite3.Connection, email_id: int) -> sqlite3.Row | None:
    """0.45.4: the newest text of a stored email read again from the server, if it changed (reread)."""
    return conn.execute(
        "SELECT read_at, body, body_cut, attachments FROM email_rereads WHERE email_id = ? AND status = 'changed'"
        " ORDER BY text_version DESC LIMIT 1",
        (email_id,),
    ).fetchone()


def mark_read(conn: sqlite3.Connection, email_id: int, now: str, cycle_id: int) -> None:
    """The agent opened this email (in wake cycle ``cycle_id``): it no longer counts as unread (0.45.4: nor its text
    read again)."""
    conn.execute(
        "UPDATE emails SET read_by_agent_at = ?, seen_cycle_id = ? WHERE id = ? AND read_by_agent_at IS NULL",
        (now, cycle_id, email_id),
    )
    conn.execute(
        "UPDATE email_rereads SET read_by_agent_at = ? WHERE email_id = ? AND status = 'changed'"
        " AND read_by_agent_at IS NULL",
        (now, email_id),
    )


def unread_reply(
    conn: sqlite3.Connection, scope: Scope, since: str, to: str, thread: str | None, own: str = ""
) -> int | None:
    """0.40.3 (analysis-0.40.1 codebase, M1): the first email the agent hasn't opened that came in after ``since``
    (an approved email's request) from its recipient ``to``, or in its thread: one that answers (In-Reply-To or
    References) a message of ``thread`` (the approved email's own headers) or an email Ember sent to ``to`` (not one
    from Ember's ``own`` address). None: there is none, and the approved email may go out. Until then only the
    opt-out check read such a reply, and a "stop" in words it missed went unseen."""
    where, params = scope.where()
    sent = conn.execute(
        f"SELECT message_id FROM emails WHERE {where} AND direction = 'out' AND lower(to_addr) = ?"
        " AND message_id IS NOT NULL",
        (*params, to.lower()),
    )
    ids = {*_REFERENCE.findall(thread or ""), *(str(r[0]) for r in sent)}
    for row in conn.execute(
        f"SELECT id, from_addr, in_reply_to, references_ FROM emails WHERE {where} AND direction = 'in'"
        f" AND {_UNREAD} AND received_at > ? ORDER BY id",  # 0.45.4: or with a text read again it hasn't opened
        (*params, since),
    ):
        sender = str(row["from_addr"] or "").lower()
        answers = set(_REFERENCE.findall(f"{row['in_reply_to'] or ''} {row['references_'] or ''}"))
        if sender == to.lower() or (sender != own.lower() and answers & ids):
            return int(row["id"])
    return None


def held(conn: sqlite3.Connection, scope: Scope, own: str = "") -> list[tuple[int, str, int]]:
    """0.40.3 (analysis-0.40.1 codebase, M1): the approved emails not begun that wait for the agent to open a newer
    email of their recipient (``unread_reply``): (request, recipient, email), the oldest request first."""
    where, params = scope.where()
    waiting = conn.execute(
        f"SELECT id, created_at, action FROM approvals WHERE {where} AND executor = 'email'"
        " AND status IN ('approved', 'approved_with_changes')"
        " AND NOT EXISTS (SELECT 1 FROM email_actions x WHERE x.approval_id = approvals.id) ORDER BY id",
        params,
    ).fetchall()
    found = []
    for row in waiting:
        try:
            action = json.loads(row["action"] or "null")
        except ValueError:
            continue
        to = action.get("to") if isinstance(action, dict) else None
        if not isinstance(to, str) or not to:
            continue
        thread = f"{action.get('in_reply_to') or ''} {action.get('references') or ''}"
        email_id = unread_reply(conn, scope, str(row["created_at"]), to, thread, own)
        if email_id is not None:
            found.append((int(row["id"]), to, email_id))
    return found


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
