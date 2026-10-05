"""Approved actions Ember's code carries out itself: sending approved emails.

The scheduler runs this in its round, in a worker thread, before it decides on a wake cycle (and is poked
when the owner decides on such a request). For every approved email of the current mode and session, oldest
first:

1. At the daily send limit (counted per local day) it waits for tomorrow.
2. A recipient who asked not to get emails: the request is closed as failed.
3. The email is built from the approved action (the owner's text if approved with changes), with a footer
   the model can't remove: plain text, one recipient, no copies, no attachments.
4. A 'running' row in ``email_actions`` is committed before anything is sent, and the approval is re-checked
   in that same transaction (the owner may have cancelled it). Then it is sent once. A failure before the
   email was handed over is 'failed'; anything during or after that is 'unclear', and nothing is ever sent
   again automatically. A row still 'running' after a restart becomes 'unclear' too.

In dry run the fake mailbox only records the email ('simulated'). The approval is closed by Ember (done or
failed, with the result); if the owner closed it meanwhile, the action row is kept and nothing fails. The
agent hears the result at its next wake-up, like any other decision.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any

from .. import events
from ..agent import netguard, policy
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, to_iso
from . import connectors, mail, mailstore
from .mail import Mailbox, MailError, NotSent, Unclear

log = logging.getLogger(__name__)

CLOSED_BY = "Ember"
APPROVED = ("approved", "approved_with_changes")
COUNTED = ("running", "sent", "unclear", "simulated")  # what uses up the daily limit
SUPPRESSED = "the recipient asked not to get emails"
UNCLEAR = (
    "It is unclear whether it was sent ({error}). Ember won't resend it; check the Sent folder at your mail provider."
)
INTERRUPTED = "the app stopped while sending"


def _cap(text: str | None, limit: int) -> str | None:
    return text[:limit] if text else None


def footer(settings: Settings, unlocked: bool = False) -> str:
    """The AI footer. 0.15.0: an email an unlock of the owner's approved says so (the owner didn't review it)."""
    agent, owner = settings.agent_name, settings.email_owner_name or "its owner"
    how = (
        "and sent under rules they set, without their review of this email"
        if unlocked
        else "and approved by them before sending"
    )
    return (
        f"\n\n-- \nThis email was written by {agent}, an AI agent, on behalf of {owner}, {how}. "
        f'Reply "stop" and {agent} won\'t write to you again.'
    )


def sender_name(settings: Settings) -> str:
    owner = settings.email_owner_name
    return f"{settings.agent_name} (AI agent of {owner})" if owner else f"{settings.agent_name} (AI agent)"


def parse_action(raw: str | None) -> dict[str, Any]:
    """An email approval's stored action, checked again before it is sent. Raises ValueError."""
    data = json.loads(raw or "null")
    if not isinstance(data, dict):
        raise ValueError("the action is missing")
    return mail.email_action(
        data.get("to"), data.get("subject"), data.get("body"), data.get("in_reply_to"), data.get("references")
    )


def email_body(row: sqlite3.Row, action: dict[str, Any]) -> str:
    """What is sent (before the footer): the owner's version if they approved with changes."""
    if row["status"] == "approved_with_changes" and row["final_payload"]:
        return str(row["final_payload"])
    return str(action["body"])


def build_message(
    action: dict[str, Any], body: str, address: str, settings: Settings, now: datetime, unlocked: bool = False
) -> tuple[EmailMessage, str]:
    """The email as it is sent, and its Message-ID. Header values can't hold line breaks (EmailMessage refuses)."""
    message = EmailMessage()
    message["From"] = Address(display_name=sender_name(settings), addr_spec=address)
    message["To"] = action["to"]
    message["Subject"] = action["subject"]
    message["Date"] = format_datetime(now)
    message_id = make_msgid(domain=address.rpartition("@")[2])
    message["Message-ID"] = message_id
    if action.get("in_reply_to"):
        message["In-Reply-To"] = action["in_reply_to"]
        message["References"] = action.get("references") or action["in_reply_to"]
    message.set_content(body.rstrip() + footer(settings, unlocked), charset="utf-8", cte="quoted-printable")
    mail.check_outgoing(message, action["to"])
    return message, message_id


def sent_today(conn: sqlite3.Connection, clock: Clock, scope: AgentScope) -> int:
    start, end = clock.day_bounds(clock.today())
    where, params = scope.where("a")
    row = conn.execute(
        f"SELECT COUNT(*) FROM email_actions x JOIN approvals a ON a.id = x.approval_id WHERE {where}"
        f" AND x.status IN {COUNTED} AND x.started_at >= ? AND x.started_at < ?",
        (*params, start, end),
    ).fetchone()
    return int(row[0])


def waiting(conn: sqlite3.Connection, scope: AgentScope) -> int:
    """Approved emails not sent yet (waiting, or being sent right now)."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT COUNT(*) FROM approvals WHERE {where} AND executor = 'email' AND status IN {APPROVED}", params
    ).fetchone()
    return int(row[0])


def execution(
    conn: sqlite3.Connection, row: sqlite3.Row, scope: AgentScope, clock: Clock, daily_limit: int
) -> dict[str, Any] | None:
    """What happened to an approved email, for the dashboard (None for other requests, or before approval)."""
    if row["executor"] != "email":
        return None
    action = conn.execute("SELECT * FROM email_actions WHERE approval_id = ?", (row["id"],)).fetchone()
    if action is not None:
        return {
            "status": action["status"],
            "started_at": action["started_at"],
            "finished_at": action["finished_at"],
            "result": action["result"],
            "error": action["error"],
        }
    if row["status"] not in APPROVED:
        return None
    status = "waiting_limit" if sent_today(conn, clock, scope) >= daily_limit else "waiting"
    return {"status": status, "started_at": None, "finished_at": None, "result": None, "error": None}


def integration(
    db: Database | None,
    clock: Clock | None,
    settings: Settings,
    mode: str,
    scope: AgentScope | None,
    mailbox: Mailbox | None,
) -> dict[str, Any]:
    """The email integration as the dashboard and the diagnostics show it (never the password)."""
    status, reason = mail.status(settings, mode)
    info: dict[str, Any] = {
        "available": False,
        "mode": None,
        "status": status,
        "reason": reason,
        "address": settings.email_address or None,
        "last_fetch_at": None,
        "last_error": None,
        "unread": 0,
        "sent_today": 0,
        "daily_limit": settings.email_daily_limit,
        "suppressed_count": 0,
        "suppressed": [],  # 0.12.0: the newest addresses Ember never emails
        "sender_check": mail.sender_check(settings, mode),  # 0.22.0: whose Authentication-Results header counts
    }
    if mailbox is None or db is None or clock is None or scope is None:
        return info
    last_fetch_at, last_error = mailstore.last_fetch(db, mode)
    with db.connection() as conn:
        info["unread"] = mailstore.unread(conn, scope, 0)[0]
        info["sent_today"] = sent_today(conn, clock, scope)
        info["suppressed_count"], info["suppressed"] = mailstore.suppressions(conn, scope)
    info.update(
        available=True,
        mode="fake" if mailbox.simulated else "live",
        status="error" if last_error else status,
        address=mailbox.address,
        last_fetch_at=last_fetch_at,
        last_error=last_error,
    )
    return info


class Executor:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Callable[[], AgentScope],
        mailbox: Mailbox | None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.mailbox = mailbox
        self._lock = threading.Lock()  # one run at a time in this process

    def run(self) -> list[tuple[int, str]]:
        """Send what is approved and due. Returns (approval id, outcome) for every request it looked at."""
        if self.mailbox is None or not self._lock.acquire(blocking=False):
            return []
        try:
            scope = self.scope()
            where, params = scope.where()
            with self.db.connection() as conn:
                if conn.in_transaction:
                    raise RuntimeError("the executor must not run inside a database transaction")
                ids = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM approvals WHERE {where} AND executor = 'email' AND status IN {APPROVED}"
                        " AND NOT EXISTS (SELECT 1 FROM email_actions x WHERE x.approval_id = approvals.id)"
                        " ORDER BY id",
                        params,
                    )
                ]
            done = []
            for approval_id in ids:
                outcome = self._one(scope, approval_id)
                done.append((approval_id, outcome))
                if outcome == "waiting_limit":
                    break  # the rest waits for tomorrow too, in order
            return done
        finally:
            self._lock.release()

    def _one(self, scope: AgentScope, approval_id: int) -> str:
        assert self.mailbox is not None
        now = self.clock.now()
        stamp = to_iso(now)
        where, params = scope.where()
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT * FROM approvals WHERE id = ? AND {where} AND executor = 'email'", (approval_id, *params)
            ).fetchone()
            started = conn.execute("SELECT 1 FROM email_actions WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in APPROVED or started is not None:
                return "skipped"  # cancelled or decided meanwhile
            if sent_today(conn, self.clock, scope) >= self.settings.email_daily_limit:
                return "waiting_limit"
            try:
                action = parse_action(row["action"])
                unlocked = row["decided_by"] == policy.POLICY_BY  # 0.15.0: the footer says who approved it
                message, message_id = build_message(
                    action, email_body(row, action), self.mailbox.address, self.settings, now, unlocked
                )
            except ValueError as exc:
                self._start(conn, approval_id, stamp, None)
                return self._failed(conn, approval_id, f"the approved email can't be sent: {exc}")
            self._start(conn, approval_id, stamp, message_id, action["to"])
            if mailstore.is_suppressed(conn, scope, action["to"]):
                return self._failed(conn, approval_id, SUPPRESSED)
        # Committed: from here on this email is never sent a second time, whatever happens.
        try:
            # The fake mailbox of a dry run never needs the network: sealed, it couldn't reach it either.
            with netguard.sealed() if self.mailbox.simulated else contextlib.nullcontext():
                result = self.mailbox.send(message, action["to"])
        except NotSent as exc:
            return self._after(approval_id, "failed", error=str(exc))
        except Unclear as exc:
            return self._after(approval_id, "unclear", error=str(exc))
        except (MailError, ValueError) as exc:  # refused before anything was contacted
            return self._after(approval_id, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - it may have gone out: treat it as unclear, never retry
            log.exception("Sending the email of request #%d failed", approval_id)
            return self._after(approval_id, "unclear", error=type(exc).__name__)
        local = now.astimezone(self.clock.tz).strftime("%Y-%m-%d %H:%M %Z")
        note = result.detail if result.status == "simulated" else f"Sent {local} as {message_id}"
        with self.db.transaction() as conn:
            self._finish(conn, approval_id, result.status, result=note)
            mailstore.store_outgoing(
                conn,
                scope,
                approval_id=approval_id,
                message_id=message_id,
                in_reply_to=action.get("in_reply_to"),
                references=action.get("references"),
                from_addr=self.mailbox.address,
                from_name=sender_name(self.settings),
                to_addr=action["to"],
                subject=action["subject"],
                body=message.get_content(),
                now=to_iso(self.clock.now()),
            )
            self._close(conn, approval_id, "done", note)
        what = "was recorded, not sent (dry run)" if result.status == "simulated" else "was sent"
        events.record(self.db, "info", "email", f"The approved email of request #{approval_id} {what}")
        return result.status

    def _after(self, approval_id: int, status: str, error: str) -> str:
        note = UNCLEAR.format(error=error[:200]) if status == "unclear" else f"Not sent: {error}"
        with self.db.transaction() as conn:
            self._finish(conn, approval_id, status, error=error)
            self._close(conn, approval_id, "failed", note)
        level = "error" if status == "unclear" else "warning"
        events.record(self.db, level, "email", f"The approved email of request #{approval_id} {status}: {error}")
        return status

    def _failed(self, conn: sqlite3.Connection, approval_id: int, reason: str) -> str:
        """Closed without sending anything (inside the caller's transaction)."""
        self._finish(conn, approval_id, "failed", error=reason)
        self._close(conn, approval_id, "failed", f"Not sent: {reason}")
        events.record(
            self.db, "warning", "email", f"The approved email of request #{approval_id} was not sent: {reason}"
        )
        return "failed"

    @staticmethod
    def _start(
        conn: sqlite3.Connection, approval_id: int, stamp: str, message_id: str | None, to: str | None = None
    ) -> None:
        conn.execute(
            "INSERT INTO email_actions (approval_id, started_at, status, message_id) VALUES (?, ?, 'running', ?)",
            (approval_id, stamp, message_id),
        )
        connectors.begin(conn, approval_id, stamp, subject=to)  # 0.13.0: the shared journal

    def _finish(
        self,
        conn: sqlite3.Connection,
        approval_id: int,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        now = to_iso(self.clock.now())
        conn.execute(
            "UPDATE email_actions SET status = ?, finished_at = ?, result = ?, error = ?"
            " WHERE approval_id = ? AND status = 'running'",
            (status, now, _cap(result, 500), _cap(error, 500), approval_id),
        )
        journaled = {"sent": "done"}.get(status, status)  # simulated, failed and unclear are the journal's too
        connectors.finish(conn, approval_id, journaled, now, {"status": status}, result or error)

    def _close(self, conn: sqlite3.Connection, approval_id: int, outcome: str, note: str) -> None:
        """Close the approval as Ember, unless the owner already closed it (then the action row stays as it is)."""
        conn.execute(
            "UPDATE approvals SET status = ?, closed_at = ?, closed_by = ?, result_note = ?, version = version + 1,"
            f" seen_cycle_id = NULL WHERE id = ? AND status IN {APPROVED}",
            (outcome, to_iso(self.clock.now()), CLOSED_BY, note[:2000], approval_id),
        )

    def recover(self) -> int:
        """At startup: an email still 'running' may or may not have been sent. It becomes 'unclear', never resent."""
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT approval_id FROM email_actions WHERE status = 'running'").fetchall()
            for row in rows:
                self._finish(conn, row["approval_id"], "unclear", error=INTERRUPTED)
                self._close(conn, row["approval_id"], "failed", UNCLEAR.format(error=INTERRUPTED))
        for row in rows:
            events.record(
                self.db,
                "error",
                "email",
                f"The app stopped while sending the email of request #{row['approval_id']}; it is unclear whether it"
                " went out, and it won't be sent again",
            )
        return len(rows)
