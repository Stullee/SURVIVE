"""Carrying out approved pins, and reading how they do (0.13.0, Phase E2).

An approved pin (executor 'pinterest_pin') is carried out once, like an Etsy listing: its row is committed as
'running' before anything is sent, so a crash never makes it twice ('unclear' when it can't be known; the app's next
start marks what a crash left running). A pin that goes on a new board makes the board first (pinterest_boards,
journaled as pinterest.create_board), then the pin (pinterest_pins, journaled as pinterest.create_pin: the owner's
Undo deletes it). The image must be exactly the file the owner approved (its SHA-256). At most pinterest_pins_per_day
pins a day. The owner's Undo of a pin is a request of theirs (executor 'pinterest_delete'), carried out here too. The
sync reads each live pin's numbers (impressions, saves, outbound clicks) at most every SYNC_HOURS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from .. import events
from ..agent.sandbox import Jail, SandboxError
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from . import connectors, etsy, etsy_publisher
from .pinterest import Account, Gone, NotSent, Pin, PinterestError, pin_from_action, pin_url

log = logging.getLogger(__name__)

APPROVED = "('approved', 'approved_with_changes')"
CLOSED_BY = "Ember"
SYNC_HOURS = 6
INTERRUPTED = "the app stopped while making the pin"
DELETE_INTERRUPTED = "the app stopped while deleting it"  # 0.15.0: the owner's Undo of a pin
GONE = "Deleted at Pinterest, not by Ember's code"
LISTING_LINK = re.compile(r"^https://www\.etsy\.com/listing/(\d{1,18})$")  # etsy.listing_url: what propose_pin links


def meta_key(mode: str, name: str) -> str:
    return f"integrations.pinterest.{mode}.{name}"


def boards(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """Ember's boards (made), the oldest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM pinterest_boards WHERE {where} AND status = 'active' ORDER BY id", params
    ).fetchall()


def board(conn: sqlite3.Connection, scope: AgentScope, board_id: str) -> sqlite3.Row | None:
    return next((b for b in boards(conn, scope) if b["board_id"] == board_id), None)


def pins(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[sqlite3.Row]:
    """Ember's pins, the newest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM pinterest_pins WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def pin_json(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "pin_id": r["pin_id"],
        "approval_id": r["approval_id"],
        "board_id": r["board_id"],
        "title": r["title"],
        "link": r["link"],
        "status": r["status"],
        "url": pin_url(r["pin_id"]) if r["pin_id"] else None,
        "impressions": r["impressions"],
        "saves": r["saves"],
        "clicks": r["clicks"],
        "synced_at": r["synced_at"],
        "result": r["result"],
    }


def totals(conn: sqlite3.Connection, scope: AgentScope) -> tuple[int, int]:
    """Ember's live pins and their outbound clicks at the last sync (the metrics pins_live and pin_clicks)."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(clicks), 0) FROM pinterest_pins WHERE {where} AND status = 'active'", params
    ).fetchone()
    return int(row[0]), int(row[1])


def made_any(conn: sqlite3.Connection, scope: AgentScope) -> bool:
    """0.16.1 analysis (bug 1): whether Ember's code ever made a pin (it has Pinterest's id), deleted since or not: a
    Pinterest venture's first test ran only then (agent/stages.py)."""
    where, params = scope.where()
    found = conn.execute(f"SELECT 1 FROM pinterest_pins WHERE {where} AND pin_id IS NOT NULL LIMIT 1", params)
    return found.fetchone() is not None


def created_today(conn: sqlite3.Connection, clock: Clock, scope: AgentScope) -> int:
    """The pins started today, for pinterest_pins_per_day. 0.15.0: not one that failed before any pin request was sent
    (no board: its checks refused it, such as a listing no longer live), so it doesn't hold a valid pin back a day."""
    where, params = scope.where()
    start = to_iso(clock.day_start(clock.today()))
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM pinterest_pins WHERE {where} AND started_at >= ?"
            " AND NOT (status = 'failed' AND board_id IS NULL)",
            (*params, start),
        ).fetchone()[0]
    )


def execution(
    conn: sqlite3.Connection, row: sqlite3.Row, scope: AgentScope, clock: Clock, daily_limit: int
) -> dict[str, Any] | None:
    """What happened to an approved pin, or to the owner's Undo of one, for the dashboard (None before approval)."""
    if row["executor"] == "pinterest_delete":
        entry = conn.execute(
            "SELECT * FROM action_journal WHERE approval_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        if entry is not None:
            status = {"done": "deleted", "simulated": "deleted"}.get(str(entry["status"]), str(entry["status"]))
            return {
                "status": status,
                "started_at": entry["started_at"],
                "finished_at": entry["finished_at"],
                "result": entry["note"],
                "error": None,
                "url": None,
            }
    else:
        pin = conn.execute("SELECT * FROM pinterest_pins WHERE approval_id = ?", (row["id"],)).fetchone()
        if pin is not None:
            return {
                "status": pin["status"],
                "started_at": pin["started_at"],
                "finished_at": pin["finished_at"],
                "result": pin["result"],
                "error": pin["error"],
                "url": pin_url(pin["pin_id"]) if pin["pin_id"] else None,
            }
    if row["status"] not in ("approved", "approved_with_changes"):
        return None
    limited = row["executor"] == "pinterest_pin" and created_today(conn, clock, scope) >= daily_limit
    return {
        "status": "waiting_limit" if limited else "waiting",
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "url": None,
    }


def text(conn: sqlite3.Connection, scope: AgentScope, limit: int = 6) -> str:
    """The plan's PINTEREST: Ember's boards and newest pins with their numbers ("" before the first)."""
    made = boards(conn, scope)
    recent = pins(conn, scope, limit)
    if not made and not recent:
        return "No board or pin of yours yet: your first board waits for your owner's decision."
    lines = ["Boards: " + ", ".join(f"{b['name']} ({b['board_id']})" for b in made)] if made else []
    for r in recent:
        numbers = (
            f"{r['impressions'] or 0} impressions, {r['saves'] or 0} saves, {r['clicks'] or 0} clicks"
            if r["synced_at"]
            else "not read yet"
        )
        lines.append(f"- pin {r['pin_id'] or '-'} ({r['status']}): {r['title'][:60]} -> {r['link']}: {numbers}")
    return "\n".join(lines)


class Publisher:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Callable[[], AgentScope],
        account: Callable[[], Account | None],
        workspace: Callable[[], Jail],
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.account = account
        self.workspace = workspace
        self._lock = threading.Lock()  # one run (or sync) at a time in this process

    def run(self, undos: bool = False) -> list[tuple[int, str]]:
        """Carry out the approved pins (and the owner's Undos of pins) that are due; with ``undos`` (0.15.0: while the
        agent is paused or waits for money), only the Undos."""
        account = self.account()
        if account is None or not self._lock.acquire(blocking=False):
            return []
        try:
            scope = self.scope()
            done = []
            for approval_id in [] if undos else self._approved(scope, "pinterest_pin"):
                outcome = self._one(account, scope, approval_id)
                done.append((approval_id, outcome))
                if outcome == "waiting_limit":
                    break  # the rest waits for tomorrow too, in order
            for approval_id in self._approved(scope, "pinterest_delete"):
                done.append((approval_id, self._delete(account, scope, approval_id)))
            return done
        finally:
            self._lock.release()

    def _approved(self, scope: AgentScope, executor: str) -> list[int]:
        where, params = scope.where()
        with self.db.connection() as conn:
            return [
                int(r[0])
                for r in conn.execute(
                    f"SELECT id FROM approvals WHERE {where} AND executor = ? AND status IN {APPROVED}"
                    " AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id)"
                    " AND NOT EXISTS (SELECT 1 FROM pinterest_pins p WHERE p.approval_id = approvals.id) ORDER BY id",
                    (*params, executor),
                )
            ]

    @staticmethod
    def _listing(conn: sqlite3.Connection, scope: AgentScope, link: str, now: str) -> None:
        """0.15.0: the listing a pin links to must still be Ember's and live (as Ember's Etsy records say, and not past
        its end without renewing) when the pin is made: an approved pin can wait days for its turn. Raises
        PinterestError with why it isn't."""
        found = LISTING_LINK.match(link)
        listing_id = int(found.group(1)) if found else 0
        row = etsy_publisher.listing_row(conn, scope, listing_id)
        if row is None:
            raise PinterestError(f"#{listing_id} isn't one of your live listings any more")
        if etsy_publisher.etsy_state(row) != etsy.LIVE_STATE:
            raise PinterestError(f"#{listing_id} isn't live at Etsy any more ({etsy_publisher.state_text(row)})")
        if not row["auto_renew"] and row["ends_at"] and from_iso(row["ends_at"]) <= from_iso(now):
            raise PinterestError(f"#{listing_id} isn't live at Etsy any more (it ended on {row['ends_at'][:10]})")

    def _image(self, pin: Pin) -> bytes:
        try:
            data = self.workspace().read_bytes(pin.image.path)
        except SandboxError as exc:
            raise PinterestError(f"{pin.image.path} can't be read: {exc}") from None
        if hashlib.sha256(data).hexdigest() != pin.image.sha256:
            raise PinterestError(f"{pin.image.path} changed after it was approved: propose the pin again")
        return data

    def _one(self, account: Account, scope: AgentScope, approval_id: int) -> str:
        stamp = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            started = conn.execute("SELECT 1 FROM pinterest_pins WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in ("approved", "approved_with_changes") or started is not None:
                return "skipped"  # cancelled or decided meanwhile
            if created_today(conn, self.clock, scope) >= self.settings.pinterest_pins_per_day:
                return "waiting_limit"
            try:
                pin = pin_from_action(row["action"])
                self._listing(conn, scope, pin.link, stamp)
                image = self._image(pin)
            except PinterestError as exc:
                self._start(conn, scope, approval_id, stamp, str(row["title"])[:100], "")
                connectors.begin(conn, approval_id, stamp)
                return self._failed(conn, approval_id, str(exc))
            self._start(conn, scope, approval_id, stamp, pin.title, pin.link)
        # Committed: from here on this pin is never made a second time, whatever happens.
        try:
            board_id = pin.board_id or self._named(scope, str(pin.board_name))
            board_id = board_id or self._board(account, scope, approval_id, str(pin.board_name))
        except PinterestError as exc:
            note = f"Not pinned: the new board couldn't be made ({exc})"
            with self.db.transaction() as conn:
                connectors.begin(conn, approval_id, to_iso(self.clock.now()))
            return self._after(approval_id, "failed", None, None, note, str(exc))
        with self.db.transaction() as conn:
            connectors.begin(conn, approval_id, to_iso(self.clock.now()))
        try:
            pin_id = account.create_pin(board_id, pin, image)
        except NotSent as exc:
            return self._after(
                approval_id, "failed", None, board_id, f"Not pinned: Pinterest refused it ({exc})", str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - Unclear, or anything else: a pin may exist
            error = str(exc) if isinstance(exc, PinterestError) else type(exc).__name__
            if not isinstance(exc, PinterestError):
                log.exception("Making the pin of request #%d failed", approval_id)
            note = f"It is unclear whether Pinterest made the pin ({error}). Ember won't try again; check the board."
            return self._after(approval_id, "unclear", None, board_id, note, error)
        note = f"Pinned: {pin_url(pin_id)}"
        if account.simulated:
            note = f"Pinned in the dry run's fake account as {pin_id}; nothing reached Pinterest."
        return self._after(approval_id, "active", pin_id, board_id, note, simulated=account.simulated)

    def _named(self, scope: AgentScope, name: str) -> str | None:
        """A board of Ember's with this name made since the pin was proposed (another pin's new board), or None."""
        with self.db.connection() as conn:
            same = next((b for b in boards(conn, scope) if str(b["name"]).lower() == name.lower()), None)
        return str(same["board_id"]) if same is not None else None

    def _board(self, account: Account, scope: AgentScope, approval_id: int, name: str) -> str:
        """The new board a pin goes on, made first (its own row and journal entry)."""
        stamp = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            row_id = int(
                conn.execute(
                    "INSERT INTO pinterest_boards (mode, session, approval_id, name, status, started_at)"
                    " VALUES (?, ?, ?, ?, 'running', ?)",
                    (scope.mode, scope.session, approval_id, name[:50], stamp),
                ).lastrowid
            )
            connectors.begin(conn, approval_id, stamp, subject=name, name="pinterest.create_board")
        try:
            made = account.create_board(name, "")
        except Exception as exc:  # noqa: BLE001 - recorded, then raised as a PinterestError for the pin
            status = "failed" if isinstance(exc, NotSent) else "unclear"
            error = str(exc) if isinstance(exc, PinterestError) else type(exc).__name__
            with self.db.transaction() as conn:
                now = to_iso(self.clock.now())
                conn.execute(
                    "UPDATE pinterest_boards SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                    (status, now, error[:500], row_id),
                )
                connectors.finish(conn, approval_id, status, now, note=f"The board wasn't made: {error}")
            raise PinterestError(error) from None
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            conn.execute(
                "UPDATE pinterest_boards SET status = 'active', finished_at = ?, board_id = ? WHERE id = ?",
                (now, made.board_id, row_id),
            )
            done = "simulated" if account.simulated else "done"
            connectors.finish(conn, approval_id, done, now, {"board_id": made.board_id}, f"Board {name!r} made")
        return made.board_id

    @staticmethod
    def _start(
        conn: sqlite3.Connection, scope: AgentScope, approval_id: int, stamp: str, title: str, link: str
    ) -> None:
        conn.execute(
            "INSERT INTO pinterest_pins (mode, session, approval_id, title, link, status, started_at)"
            " VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (scope.mode, scope.session, approval_id, title[:100], link[:500], stamp),
        )

    def _failed(self, conn: sqlite3.Connection, approval_id: int, reason: str) -> str:
        note = f"Not pinned: {reason}"
        now = to_iso(self.clock.now())
        conn.execute(
            "UPDATE pinterest_pins SET status = 'failed', finished_at = ?, error = ? WHERE approval_id = ?",
            (now, reason[:500], approval_id),
        )
        connectors.finish(conn, approval_id, "failed", now, note=note)
        self._close(conn, approval_id, "failed", note, None)
        return "failed"

    def _after(
        self,
        approval_id: int,
        status: str,
        pin_id: str | None,
        board_id: str | None,
        note: str,
        error: str | None = None,
        simulated: bool = False,
    ) -> str:
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            conn.execute(
                "UPDATE pinterest_pins SET status = ?, finished_at = ?, pin_id = ?, board_id = ?, result = ?,"
                " error = ? WHERE approval_id = ?",
                (status, now, pin_id, board_id, note[:500], (error or "")[:500] or None, approval_id),
            )
            journaled = {"active": "simulated" if simulated else "done"}.get(status, status)
            connectors.finish(
                conn,
                approval_id,
                journaled,
                now,
                {"pin_id": pin_id, "board_id": board_id} if pin_id else None,
                note,
                subject=pin_id,
            )
            self._close(
                conn, approval_id, "done" if status == "active" else "failed", note, pin_url(pin_id) if pin_id else None
            )
        level = "info" if status == "active" else "warning"
        events.record(self.db, level, "pinterest", f"Request #{approval_id}: {note}"[:300])
        return status

    def _close(self, conn: sqlite3.Connection, approval_id: int, outcome: str, note: str, link: str | None) -> None:
        """Close the approval (unless the owner did meanwhile): the agent hears the result at its next wake."""
        conn.execute(
            "UPDATE approvals SET status = ?, closed_at = ?, closed_by = ?, result_note = ?, result_link = ?,"
            f" version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status IN {APPROVED}",
            (outcome, to_iso(self.clock.now()), CLOSED_BY, note[:2000], link, approval_id),
        )

    def _delete(self, account: Account, scope: AgentScope, approval_id: int) -> str:
        """The owner's Undo of a pin: Ember's code deletes it at Pinterest."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in ("approved", "approved_with_changes"):
                return "skipped"
            try:
                pin_id = str(json.loads(row["action"])["pin_id"])
            except (ValueError, KeyError, TypeError):
                pin_id = ""
            connectors.begin(conn, approval_id, to_iso(self.clock.now()), subject=pin_id or None)
        gone = False
        try:
            if not pin_id.isdigit():
                raise NotSent("the request names no pin")
            account.delete_pin(pin_id)
        except Gone:
            gone = True  # deleted at Pinterest already: what the Undo wanted
        except Exception as exc:  # noqa: BLE001 - reported on the request, never raised
            error = str(exc) if isinstance(exc, PinterestError) else type(exc).__name__
            status = "failed" if isinstance(exc, NotSent) else "unclear"
            with self.db.transaction() as conn:
                note = f"Not deleted: {error}"
                connectors.finish(conn, approval_id, status, to_iso(self.clock.now()), note=note)
                self._close(conn, approval_id, "failed", note, None)
            return status
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            where, params = scope.where()
            conn.execute(
                f"UPDATE pinterest_pins SET status = 'deleted' WHERE {where} AND pin_id = ?", (*params, pin_id)
            )
            note = f"Deleted pin {pin_id} at Pinterest" + (" (dry run: the fake account)" if account.simulated else "")
            if gone:
                note = f"Pin {pin_id} was gone from Pinterest already"
            connectors.finish(
                conn, approval_id, "simulated" if account.simulated else "done", now, {"deleted": pin_id}, note
            )
            self._close(conn, approval_id, "done", note, None)
        events.record(self.db, "info", "pinterest", f"Request #{approval_id}: {note}"[:300])
        return "done"

    def recover(self) -> int:
        """Rows left 'running' by a crash: unclear, never retried. 0.15.0: the owner's Undo of a pin too (its journal
        entry was left 'running', the Undo "under way" for good): unclear, and the owner may press it again."""
        with self.db.transaction() as conn:
            left = conn.execute("SELECT approval_id, board_id FROM pinterest_pins WHERE status = 'running'").fetchall()
            conn.execute(
                "UPDATE pinterest_boards SET status = 'unclear', finished_at = ?, error = ? WHERE status = 'running'",
                (to_iso(self.clock.now()), INTERRUPTED),
            )
            deleting = conn.execute(
                "SELECT j.approval_id, j.subject FROM action_journal j JOIN approvals a ON a.id = j.approval_id"
                " WHERE j.status = 'running' AND a.executor = 'pinterest_delete'"
            ).fetchall()
            for row in deleting:
                note = (
                    f"It is unclear whether pin {row['subject']} was deleted ({DELETE_INTERRUPTED}). Ember won't try"
                    " again on its own: check Pinterest, or press Delete the pin again (a pin gone already counts as"
                    " deleted)."
                )
                connectors.finish(conn, row["approval_id"], "unclear", to_iso(self.clock.now()), note=note)
                self._close(conn, row["approval_id"], "failed", note, None)
        for row in left:
            note = f"It is unclear whether the pin was made ({INTERRUPTED}). Ember won't try again; check the board."
            self._after(row["approval_id"], "unclear", None, row["board_id"], note, INTERRUPTED)
        return len(left) + len(deleting)

    # --- how the pins do ---

    def sync(self, force: bool = False) -> str | None:
        """Read each live pin's numbers, at most every SYNC_HOURS. Returns an error, or None."""
        account = self.account()
        if account is None or not self._lock.acquire(blocking=False):
            return None
        try:
            scope = self.scope()
            now = self.clock.now()
            last = self.db.get_meta(meta_key(scope.mode, "last_sync_at"))
            if not force and last and now - from_iso(last) < timedelta(hours=SYNC_HOURS):
                return None
            where, params = scope.where()
            with self.db.connection() as conn:
                live = conn.execute(
                    f"SELECT approval_id, pin_id FROM pinterest_pins WHERE {where} AND status = 'active'", params
                ).fetchall()
            error = None
            try:
                account.keep_alive()  # 0.15.0: with or without pins, the connection is renewed before it lapses
            except PinterestError as exc:
                error = str(exc)[:300]
            for row in live if error is None else []:
                try:
                    stats = account.pin_stats(str(row["pin_id"]))
                except Gone:  # deleted at Pinterest: the others are still read
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE pinterest_pins SET status = 'deleted', result = ? WHERE approval_id = ?",
                            (GONE, row["approval_id"]),
                        )
                    continue
                except PinterestError as exc:
                    error = str(exc)[:300]
                    break
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE pinterest_pins SET impressions = ?, saves = ?, clicks = ?, synced_at = ?"
                        " WHERE approval_id = ?",
                        (stats.impressions, stats.saves, stats.clicks, to_iso(now), row["approval_id"]),
                    )
            self.db.set_meta(meta_key(scope.mode, "last_sync_at"), to_iso(now))
            self.db.set_meta(meta_key(scope.mode, "last_error"), error or "")
            return error
        finally:
            self._lock.release()
