"""Approved Etsy listings, created by Ember's code; and the sync that brings back how they do.

The scheduler runs ``Publisher.run`` in its round, next to the email executor. For every approved listing of the
current mode and session, oldest first:

1. At the daily limit (``etsy_listings_per_day``, per local day) it waits for tomorrow.
2. The listing is read from the approval (with the owner's changes, if any), and every file and photo is read from
   the workspace and must still have the SHA-256 the owner approved; otherwise the request fails, and nothing is
   sent.
3. A 'running' row in ``etsy_listings`` is committed before anything is sent, and the approval is re-checked in
   that transaction. Then: a draft, its photos, its files, and live. Etsy refusing the draft is 'failed'; anything
   unclear before the draft's number is known is 'unclear'; a failure after the draft exists leaves a 'draft' at
   Etsy for the owner to finish. Nothing is ever created twice: a row still 'running' after a restart becomes
   'unclear'.

Approved changes to live listings (0.9.0, executor 'etsy_edit') follow in the same run, oldest first, without a
daily limit (Etsy charges nothing for them). The files and photos are checked the same way, and an 'etsy_edits' row is
committed as 'running' before anything is sent. Then: the listing's own fields (title, description, tags and
category, one request), its price (in its inventory), its photos and its files, each new set uploaded before the old
one is deleted, so the listing never is without them. A refusal before anything changed is 'failed', after something
changed 'partial'; anything unclear is 'unclear'. The row keeps the listing as it is afterwards (as far as Ember
knows), for the agent's etsy_listing and the next change. Nothing is ever changed twice.

``sync`` (in the scheduler's rounds, so while the agent sleeps too, and at the start of a wake cycle; at most every
SYNC_MINUTES, well within the 6 hours Etsy's API terms allow for listing content) records each listing's state, views
and favorites, and the orders that hold Ember's listings: dates, totals and which listings, never who bought.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from .. import events
from ..agent import netguard
from ..agent.sandbox import Jail, SandboxError
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from . import connectors, etsy, printify_publisher, qa
from .etsy import Edit, EtsyError, Listing, NotSent, Shop, Unclear, Upload

log = logging.getLogger(__name__)

CLOSED_BY = "Ember"
APPROVED = ("approved", "approved_with_changes")
_APPROVED_SQL = ", ".join(f"'{status}'" for status in APPROVED)
COUNTED = ("running", "active", "draft", "unclear")  # what uses up the daily limit
SYNC_MINUTES = 60
FEE_READS = 10  # order payments read per sync, for the fees (0.12.0)
ORDER_DAYS = 30  # how far back the sync looks for orders
INTERRUPTED = "the app stopped while creating the listing"
CHANGE_INTERRUPTED = "the app stopped while changing the listing"
CHANGED = "{path} changed after you approved it (its SHA-256 differs); ask for a new request"


def created_today(conn: sqlite3.Connection, clock: Clock, scope: AgentScope) -> int:
    start, end = clock.day_bounds(clock.today())
    where, params = scope.where()
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM etsy_listings WHERE {where} AND status IN {COUNTED} AND started_at >= ?"
            " AND started_at < ?",
            (*params, start, end),
        ).fetchone()[0]
    )


def execution(
    conn: sqlite3.Connection, row: sqlite3.Row, scope: AgentScope, clock: Clock, daily_limit: int
) -> dict[str, Any] | None:
    """What happened to an approved listing, for the dashboard (None before approval)."""
    listing = conn.execute("SELECT * FROM etsy_listings WHERE approval_id = ?", (row["id"],)).fetchone()
    if listing is not None:
        return {
            "status": listing["status"],
            "started_at": listing["started_at"],
            "finished_at": listing["finished_at"],
            "result": listing["result"],
            "error": listing["error"],
            "listing_id": listing["listing_id"],
            "url": etsy.listing_url(listing["listing_id"]) if listing["listing_id"] else None,
        }
    if row["status"] not in APPROVED:
        return None
    status = "waiting_limit" if created_today(conn, clock, scope) >= daily_limit else "waiting"
    return {"status": status, "started_at": None, "finished_at": None, "result": None, "error": None}


def waiting(conn: sqlite3.Connection, scope: AgentScope) -> int:
    where, params = scope.where()
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM approvals WHERE {where} AND executor = 'etsy_listing' AND status IN {APPROVED}"
            " AND NOT EXISTS (SELECT 1 FROM etsy_listings x WHERE x.approval_id = approvals.id)",
            params,
        ).fetchone()[0]
    )


def approved_listing(row: sqlite3.Row) -> Listing:
    """The listing to create: the agent's, with the owner's words if they approved it with changes."""
    listing = etsy.listing_from_action(row["action"])
    if row["status"] == "approved_with_changes" and row["final_payload"]:
        listing = etsy.with_changes(listing, row["final_payload"])
    return listing


def approved_edit(row: sqlite3.Row) -> Edit:
    """The change to make: the agent's, with the owner's words and price if they approved it with changes."""
    edit = etsy.edit_from_action(row["action"])
    if row["status"] == "approved_with_changes" and row["final_payload"]:
        edit = etsy.edit_with_changes(edit, row["final_payload"])
    return edit


def current_listing(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> Listing | None:
    """One of Ember's live listings as Ember listed it or last changed it (changes the owner made at Etsy aren't
    known); None when Ember didn't list it (live) in this mode and session."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT approval_id FROM etsy_listings WHERE {where} AND listing_id = ? AND status = 'active'",
        (*params, listing_id),
    ).fetchone()
    if row is None:
        return None
    changed = conn.execute(
        f"SELECT listing FROM etsy_edits WHERE {where} AND listing_id = ? AND listing IS NOT NULL ORDER BY id DESC"
        " LIMIT 1",
        (*params, listing_id),
    ).fetchone()
    if changed is not None:
        return etsy.listing_from_action(changed["listing"])
    approval = conn.execute("SELECT * FROM approvals WHERE id = ?", (row["approval_id"],)).fetchone()
    return approved_listing(approval)


def listing_row(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> sqlite3.Row | None:
    """Ember's record of one of the listings it made live (its state at Etsy as the last sync read it: 0.12.0), or
    None."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM etsy_listings WHERE {where} AND listing_id = ? AND status = 'active'", (*params, listing_id)
    ).fetchone()


def etsy_state(row: sqlite3.Row) -> str:
    """A listing's state at Etsy as the last sync read it: live ('active') until a sync says otherwise."""
    return str(row["state"] or etsy.LIVE_STATE)


def state_text(row: sqlite3.Row) -> str:
    """How a listing stands at Etsy, in a few words: its state, when it ends, whether it renews itself."""
    state, ends = etsy_state(row), (row["ends_at"] or "")[:10]
    if state == etsy.LIVE_STATE:
        if row["auto_renew"]:
            return "live, renews itself" + (f" on {ends}" if ends else "")
        return f"live until {ends}" if ends else "live"
    words = {"expired": f"expired on {ends}" if ends else "expired", "inactive": "deactivated", "sold_out": "sold out"}
    return words.get(state, state)


def idle_listings(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """0.12.0: Ember's listings that Etsy says aren't live (expired, sold out, deactivated), newest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM etsy_listings WHERE {where} AND status = 'active' AND listing_id IS NOT NULL"
        f" AND COALESCE(state, '{etsy.LIVE_STATE}') <> '{etsy.LIVE_STATE}' ORDER BY id DESC",
        params,
    ).fetchall()


def live_listings(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[tuple[int, Listing]]:
    """Ember's live listings (newest first), each as Ember listed it or last changed it; live as Etsy last said
    (0.12.0: an expired listing counted as live)."""
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT listing_id FROM etsy_listings WHERE {where} AND status = 'active' AND listing_id IS NOT NULL"
        f" AND COALESCE(state, '{etsy.LIVE_STATE}') = '{etsy.LIVE_STATE}' ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    found = []
    for row in rows:
        try:
            listing = current_listing(conn, scope, row["listing_id"])
        except EtsyError:
            continue
        if listing is not None:
            found.append((int(row["listing_id"]), listing))
    return found


def open_edit(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> int | None:
    """A change to this listing that waits for the owner or for Ember's code (its request number), or None."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT id FROM approvals WHERE {where} AND executor = 'etsy_edit' AND status IN ('pending', {_APPROVED_SQL})"
        " AND json_extract(action, '$.listing_id') = ? ORDER BY id LIMIT 1",
        (*params, listing_id),
    ).fetchone()
    return int(row["id"]) if row else None


def edit_execution(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any] | None:
    """What happened to an approved change, for the dashboard (None before approval)."""
    edit = conn.execute("SELECT * FROM etsy_edits WHERE approval_id = ?", (row["id"],)).fetchone()
    if edit is not None:
        return {
            "status": edit["status"],
            "started_at": edit["started_at"],
            "finished_at": edit["finished_at"],
            "result": edit["result"],
            "error": edit["error"],
            "listing_id": edit["listing_id"],
            "url": etsy.listing_url(edit["listing_id"]),
        }
    if row["status"] not in APPROVED:
        return None
    return {"status": "waiting", "started_at": None, "finished_at": None, "result": None, "error": None}


class Publisher:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Callable[[], AgentScope],
        shop: Callable[[], Shop | None],
        workspace: Callable[[], Jail],
        after_sync: Callable[[AgentScope, Settings], Any] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.shop = shop
        self.workspace = workspace
        # 0.12.0: what Ember's code does with the fresh numbers of a sync that worked (the orders in the ledger)
        self.after_sync = after_sync
        self._lock = threading.Lock()  # one run (or sync) at a time in this process

    # --- approved listings ---

    def run(self) -> list[tuple[int, str]]:
        shop = self.shop()
        if shop is None or not self._lock.acquire(blocking=False):
            return []
        try:
            scope = self.scope()
            done = []
            for approval_id in self._approved(scope, "etsy_listing", "etsy_listings"):
                outcome = self._one(shop, scope, approval_id)
                done.append((approval_id, outcome))
                if outcome == "waiting_limit":
                    break  # the rest waits for tomorrow too, in order
            for approval_id in self._approved(scope, "etsy_edit", "etsy_edits"):  # changes have no daily limit
                done.append((approval_id, self._change(shop, scope, approval_id)))
            return done
        finally:
            self._lock.release()

    def _approved(self, scope: AgentScope, executor: str, table: str) -> list[int]:
        """The approved requests of ``executor`` that Ember's code hasn't started (no row in ``table``), oldest
        first."""
        where, params = scope.where()
        with self.db.connection() as conn:
            return [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM approvals WHERE {where} AND executor = ? AND status IN {APPROVED}"
                    f" AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.approval_id = approvals.id) ORDER BY id",
                    (*params, executor),
                )
            ]

    def _one(self, shop: Shop, scope: AgentScope, approval_id: int) -> str:
        stamp = to_iso(self.clock.now())
        where, params = scope.where()
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT * FROM approvals WHERE id = ? AND {where} AND executor = 'etsy_listing'",
                (approval_id, *params),
            ).fetchone()
            started = conn.execute("SELECT 1 FROM etsy_listings WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in APPROVED or started is not None:
                return "skipped"  # cancelled or decided meanwhile
            if created_today(conn, self.clock, scope) >= self.settings.etsy_listings_per_day:
                return "waiting_limit"
            title = ""
            try:
                listing = approved_listing(row)
                title = listing.title
                photos, files = self._read(listing)
            except EtsyError as exc:
                self._start(conn, scope, approval_id, stamp, title or row["title"][:140])
                return self._failed(conn, approval_id, str(exc))
            self._start(conn, scope, approval_id, stamp, listing.title)
        # Committed: from here on this listing is never created a second time, whatever happens.
        try:
            with _guard(shop):
                listing_id = shop.create_draft(listing)
        except NotSent as exc:
            return self._after(approval_id, "failed", None, f"Not listed: Etsy refused it ({exc})")
        except Exception as exc:  # noqa: BLE001 - Unclear, or anything else: a draft may exist
            error = str(exc) if isinstance(exc, Unclear) else type(exc).__name__
            if not isinstance(exc, EtsyError):
                log.exception("Creating the Etsy listing of request #%d failed", approval_id)
            note = f"It is unclear whether Etsy created a draft ({error}). Ember won't try again; check your drafts."
            return self._after(approval_id, "unclear", None, note, error)
        with self.db.transaction() as conn:
            conn.execute("UPDATE etsy_listings SET listing_id = ? WHERE approval_id = ?", (listing_id, approval_id))
        try:
            with _guard(shop):
                for rank, (name, data) in enumerate(photos, 1):
                    shop.upload_photo(listing_id, name, data, rank)
                for rank, (name, data) in enumerate(files, 1):
                    shop.upload_file(listing_id, name, data, rank)
                state = shop.activate(listing_id)
        except Exception as exc:  # noqa: BLE001 - the draft exists: the owner finishes it at Etsy
            error = str(exc) if isinstance(exc, EtsyError) else type(exc).__name__
            if not isinstance(exc, EtsyError):
                log.exception("Finishing Etsy listing %d (request #%d) failed", listing_id, approval_id)
            note = (
                f"The listing is a draft at Etsy ({error}). Finish it there: photos, files and publishing."
                f" {etsy.edit_url(listing_id)}"
            )
            return self._after(approval_id, "draft", listing_id, note, error)
        note = f"Listed on Etsy as #{listing_id} ({state}): {etsy.listing_url(listing_id)}"
        if shop.simulated:
            note = f"Listed in the dry run's fake shop as #{listing_id}; nothing reached Etsy."
        return self._after(approval_id, "active", listing_id, note)

    def _read(self, listing: Listing) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]]]:
        """The photos and files, exactly as approved (their SHA-256)."""
        return self._uploads(listing.photos), self._uploads(listing.files)

    def _uploads(self, uploads: tuple[Upload, ...]) -> list[tuple[str, bytes]]:
        """(file name, data) of each upload, exactly as approved (its SHA-256)."""
        jail = self.workspace()
        read: list[tuple[str, bytes]] = []
        for u in uploads:
            try:
                data = jail.read_bytes(u.path)
            except SandboxError as exc:
                raise EtsyError(f"{u.path} can't be read: {exc}") from None
            if hashlib.sha256(data).hexdigest() != u.sha256:
                raise EtsyError(CHANGED.format(path=u.path))
            read.append((u.path.rsplit("/", 1)[-1], data))
        return read

    def _after(self, approval_id: int, status: str, listing_id: int | None, note: str, error: str | None = None) -> str:
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            conn.execute(
                "UPDATE etsy_listings SET status = ?, finished_at = ?, listing_id = COALESCE(?, listing_id),"
                " result = ?, error = ? WHERE approval_id = ?",
                (status, now, listing_id, note[:500], (error or "")[:500] or None, approval_id),
            )
            connectors.finish(  # 0.13.0: the shared journal (a draft Etsy holds: partial)
                conn,
                approval_id,
                {"active": "done", "draft": "partial"}.get(status, status),
                now,
                {"listing_id": listing_id, "status": status} if listing_id else None,
                note,
                subject=str(listing_id) if listing_id else None,
            )
            link = etsy.listing_url(listing_id) if status == "active" and listing_id else None
            if status == "draft" and listing_id:
                link = etsy.edit_url(listing_id)
            self._close(conn, approval_id, "done" if status == "active" else "failed", note, link)
        level = "info" if status == "active" else "warning"
        events.record(self.db, level, "etsy", f"Request #{approval_id}: {note}"[:300])
        return status

    def _failed(self, conn: sqlite3.Connection, approval_id: int, reason: str) -> str:
        note = f"Not listed: {reason}"
        now = to_iso(self.clock.now())
        conn.execute(
            "UPDATE etsy_listings SET status = 'failed', finished_at = ?, error = ? WHERE approval_id = ?",
            (now, reason[:500], approval_id),
        )
        connectors.finish(conn, approval_id, "failed", now, note=note)
        self._close(conn, approval_id, "failed", note, None)
        return "failed"

    @staticmethod
    def _start(conn: sqlite3.Connection, scope: AgentScope, approval_id: int, stamp: str, title: str) -> None:
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, status, title)"
            " VALUES (?, ?, ?, ?, 'running', ?)",
            (scope.mode, scope.session, approval_id, stamp, title[:140]),
        )
        connectors.begin(conn, approval_id, stamp)  # 0.13.0: its listing's number comes when it is finished

    def _close(self, conn: sqlite3.Connection, approval_id: int, outcome: str, note: str, link: str | None) -> None:
        """Close the approval (unless the owner did meanwhile): the agent hears the result at its next wake."""
        conn.execute(
            "UPDATE approvals SET status = ?, closed_at = ?, closed_by = ?, result_note = ?, result_link = ?,"
            " version = version + 1, seen_cycle_id = NULL"
            " WHERE id = ? AND status IN ('approved', 'approved_with_changes')",
            (outcome, to_iso(self.clock.now()), CLOSED_BY, note[:2000], link, approval_id),
        )

    def recover(self) -> int:
        """Rows left 'running' by a crash: unclear, never retried."""
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT approval_id, listing_id FROM etsy_listings WHERE status = 'running'").fetchall()
            changes = conn.execute("SELECT approval_id, listing_id FROM etsy_edits WHERE status = 'running'").fetchall()
        for row in rows:
            note = f"It is unclear whether the listing was finished ({INTERRUPTED}). Ember won't try again; check Etsy."
            self._after(row["approval_id"], "unclear", row["listing_id"], note, INTERRUPTED)
        for row in changes:
            note = (
                f"It is unclear how much of the change was made ({CHANGE_INTERRUPTED}). Ember won't try again; check"
                f" the listing at Etsy: {etsy.edit_url(row['listing_id'])}"
            )
            self._changed(row["approval_id"], "unclear", None, None, note, CHANGE_INTERRUPTED)
        return len(rows) + len(changes)

    # --- approved changes to live listings (0.9.0) ---

    def _change(self, shop: Shop, scope: AgentScope, approval_id: int) -> str:
        stamp = to_iso(self.clock.now())
        where, params = scope.where()
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT * FROM approvals WHERE id = ? AND {where} AND executor = 'etsy_edit'", (approval_id, *params)
            ).fetchone()
            started = conn.execute("SELECT 1 FROM etsy_edits WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in APPROVED or started is not None:
                return "skipped"  # cancelled or decided meanwhile
            try:
                listing_id = int(json.loads(row["action"])["listing_id"])
            except (ValueError, KeyError, TypeError):
                listing_id = 0
            try:
                edit = approved_edit(row)
                before = current_listing(conn, scope, edit.listing_id)
                if before is None:
                    raise EtsyError(f"#{edit.listing_id} isn't one of the listings Ember made (live)")
                photos, files = self._uploads(edit.photos or ()), self._uploads(edit.files or ())
            except EtsyError as exc:
                self._start_change(conn, scope, approval_id, listing_id, stamp)
                return self._change_failed(conn, approval_id, str(exc))
            self._start_change(conn, scope, approval_id, edit.listing_id, stamp, before)
        # Committed: from here on this change is never made a second time, whatever happens.
        return self._make(shop, scope, approval_id, edit, before, photos, files)

    def _make(
        self,
        shop: Shop,
        scope: AgentScope,
        approval_id: int,
        edit: Edit,
        before: Listing,
        photos: list[tuple[str, bytes]],
        files: list[tuple[str, bytes]],
    ) -> str:
        """The change at Etsy, part by part; what was made is recorded, whatever happens."""
        listing_id = edit.listing_id
        steps: list[tuple[set[str], Callable[[list[str]], None]]] = []

        def set_state(_: list[str]) -> None:
            shop.set_state(listing_id, etsy.STATES[edit.state or ""])

        if edit.state == "renew":  # first: the rest of the change is made to a live listing
            steps.append(({"renew"}, set_state))
        fields = edit.listing_fields()
        if fields:
            steps.append((set(edit.parts()) & etsy.LISTING_PARTS, lambda _: shop.update_listing(listing_id, fields)))
        if edit.price is not None:
            price = edit.price
            steps.append(({"price"}, lambda _: shop.set_price(listing_id, price)))
        if edit.photos is not None:

            def new_photos(progress: list[str]) -> None:
                _replace(
                    shop.photo_ids(listing_id),
                    lambda name, data, rank: shop.upload_photo(listing_id, name, data, rank),
                    lambda photo_id: shop.delete_photo(listing_id, photo_id),
                    photos,
                    etsy.MAX_PHOTOS,
                    progress,
                )

            steps.append(({"photos"}, new_photos))
        if edit.files is not None:

            def new_files(progress: list[str]) -> None:
                _replace(
                    shop.file_ids(listing_id),
                    lambda name, data, rank: shop.upload_file(listing_id, name, data, rank),
                    lambda file_id: shop.delete_file(listing_id, file_id),
                    files,
                    etsy.MAX_FILES,
                    progress,
                )

            steps.append(({"files"}, new_files))
        if edit.auto_renew is not None:  # 0.13.0: only in the owner's Undo
            renewal = edit.auto_renew
            steps.append(({"auto_renew"}, lambda _: shop.set_auto_renew(listing_id, renewal)))
        if edit.state == "deactivate":  # on its own
            steps.append(({"deactivate"}, set_state))
        made: set[str] = set()
        progress: list[str] = []  # uploads and deletions of the photos or files being replaced
        halfway: set[str] = set()  # parts left half replaced
        status, error = "done", None
        try:
            with _guard(shop):
                for parts, step in steps:
                    progress.clear()
                    try:
                        step(progress)
                    except Exception:
                        if progress:
                            halfway.update(parts)
                        raise
                    made.update(parts)
        except NotSent as exc:
            status, error = ("partial" if made or halfway else "failed"), str(exc)
        except Exception as exc:  # noqa: BLE001 - Unclear, or anything else: something may have changed
            status = "unclear"
            error = str(exc) if isinstance(exc, EtsyError) else type(exc).__name__
            if not isinstance(exc, EtsyError):
                log.exception("Changing Etsy listing %d (request #%d) failed", listing_id, approval_id)
        after = etsy.edited(before, edit, made) if made else None
        note = _change_note(shop, edit, status, made, halfway, error)
        title = edit.title if "title" in made else None
        state = next((etsy.STATES[part] for part in ("renew", "deactivate") if part in made), None)
        renewal = edit.auto_renew if "auto_renew" in made else None
        return self._changed(approval_id, status, after, title, note, error, scope, state, renewal)

    def _start_change(
        self,
        conn: sqlite3.Connection,
        scope: AgentScope,
        approval_id: int,
        listing_id: int,
        stamp: str,
        before: Listing | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO etsy_edits (mode, session, approval_id, listing_id, started_at, status)"
            " VALUES (?, ?, ?, ?, ?, 'running')",
            (scope.mode, scope.session, approval_id, listing_id, stamp),
        )
        # 0.13.0: the shared journal, with the listing as it was (what changing it back restores)
        connectors.begin(conn, approval_id, stamp, str(listing_id), before.to_action() if before else None)

    def _change_failed(self, conn: sqlite3.Connection, approval_id: int, reason: str) -> str:
        note = f"Not changed: {reason}"
        now = to_iso(self.clock.now())
        conn.execute(
            "UPDATE etsy_edits SET status = 'failed', finished_at = ?, error = ? WHERE approval_id = ?",
            (now, reason[:500], approval_id),
        )
        connectors.finish(conn, approval_id, "failed", now, note=note)
        self._close(conn, approval_id, "failed", note, None)
        return "failed"

    def _changed(
        self,
        approval_id: int,
        status: str,
        after: Listing | None,
        title: str | None,
        note: str,
        error: str | None,
        scope: AgentScope | None = None,
        state: str | None = None,
        renewal: bool | None = None,
    ) -> str:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM etsy_edits WHERE approval_id = ?", (approval_id,)).fetchone()
            now = to_iso(self.clock.now())
            conn.execute(
                "UPDATE etsy_edits SET status = ?, finished_at = ?, listing = ?, result = ?, error = ?"
                " WHERE approval_id = ?",
                (
                    status,
                    now,
                    json.dumps(after.to_action(), ensure_ascii=False) if after else None,
                    note[:500],
                    (error or "")[:500] or None,
                    approval_id,
                ),
            )
            connectors.finish(conn, approval_id, status, now, after.to_action() if after else None, note)
            if title is not None and scope is not None:
                where, params = scope.where()
                conn.execute(
                    f"UPDATE etsy_listings SET title = ? WHERE {where} AND listing_id = ?",
                    (title[:140], *params, row["listing_id"]),
                )
            if (
                state is not None and scope is not None
            ):  # 0.12.0: as it is now; a renewal's end comes with the next sync
                where, params = scope.where()
                conn.execute(
                    "UPDATE etsy_listings SET state = ?, ends_at = CASE WHEN ? = 'active' THEN NULL ELSE ends_at END"
                    f" WHERE {where} AND listing_id = ?",
                    (state, state, *params, row["listing_id"]),
                )
            if renewal is not None and scope is not None:  # 0.13.0: the owner's Undo of an automatic renewal
                where, params = scope.where()
                conn.execute(
                    f"UPDATE etsy_listings SET auto_renew = ? WHERE {where} AND listing_id = ?",
                    (int(renewal), *params, row["listing_id"]),
                )
            listing_id = row["listing_id"]
            link = etsy.listing_url(listing_id) if status == "done" else etsy.edit_url(listing_id)
            self._close(conn, approval_id, "done" if status == "done" else "failed", note, link)
        level = "info" if status == "done" else "warning"
        events.record(self.db, level, "etsy", f"Request #{approval_id}: {note}"[:300])
        return status

    # --- how the listings do ---

    def due(self) -> bool:
        """Whether the shop should be read again: never read yet, or last read SYNC_MINUTES ago."""
        last = self.db.get_meta(meta_key(self.scope().mode, "last_sync_at"))
        return not last or self.clock.now() - from_iso(last) >= timedelta(minutes=SYNC_MINUTES)

    def sync(self, force: bool = False) -> str | None:
        """Bring back the listings' state, views and favorites, and the orders with Ember's listings. Returns an
        error text (also kept for the dashboard), or None."""
        shop = self.shop()
        if shop is None or not self._lock.acquire(blocking=False):
            return None
        try:
            scope = self.scope()
            now = self.clock.now()
            if not force and not self.due():
                return None
            where, params = scope.where()
            with self.db.connection() as conn:
                ids = [
                    int(r[0])
                    for r in conn.execute(
                        f"SELECT listing_id FROM etsy_listings WHERE {where} AND listing_id IS NOT NULL", params
                    )
                ]
                # 0.13.0 (Phase E4): the listings Printify made of Ember's products are Ember's too
                ids += [i for i in printify_publisher.listing_ids(conn, scope) if i not in ids]
                known = {
                    int(r[0])
                    for r in conn.execute(
                        f"SELECT receipt_id FROM etsy_orders WHERE {where} AND fees_cents IS NOT NULL", params
                    )
                }
            try:
                with _guard(shop):
                    remote = shop.listings(ids) if ids else []
                    orders = shop.orders(now - timedelta(days=ORDER_DAYS)) if ids else []
            except Exception as exc:  # noqa: BLE001 - reported on the dashboard, never raised
                error = str(exc) if isinstance(exc, EtsyError) else type(exc).__name__
                self.db.set_meta(meta_key(scope.mode, "last_error"), error[:300])
                log.warning("The Etsy sync failed: %s", error)
                return error
            stamp = to_iso(now)
            ours = set(ids)
            processing = self._processing_fees(shop, orders, ours, known)
            with self.db.transaction() as conn:
                for item in remote:
                    conn.execute(
                        "UPDATE printify_products SET state = ?, views = ?, favorites = ?, synced_at = ?"
                        f" WHERE {where} AND listing_id = ?",
                        (item.state[:20], item.views, item.favorites, stamp, *params, item.listing_id),
                    )
                    conn.execute(
                        "UPDATE etsy_listings SET state = ?, views = ?, favorites = ?, ends_at = ?, auto_renew = ?,"
                        f" synced_at = ? WHERE {where} AND listing_id = ?",
                        (
                            item.state[:20],
                            item.views,
                            item.favorites,
                            item.ends_at,
                            None if item.auto_renew is None else int(item.auto_renew),
                            stamp,
                            *params,
                            item.listing_id,
                        ),
                    )
                for order in orders:
                    items = [i for i in order.items if i.get("listing_id") in ours]
                    if not items:
                        continue  # the owner's own products: not Ember's business
                    # 0.12.0: only Ember's lines, net of tax, shipping, the coupon and refunds (the whole receipt was
                    # stored), with the order's status, which later syncs keep current.
                    net = etsy.order_net(order, items)
                    values = (
                        f"{net / 100:.2f} {order.currency}",
                        net,
                        json.dumps(items, ensure_ascii=False)[:4000],
                        order.status,
                        stamp,
                    )
                    if order.paid:
                        fee = processing.get(order.receipt_id)
                        fees = etsy.fees_share(order, items, fee) if fee is not None else None
                        conn.execute(
                            "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, currency, total,"
                            " total_cents, items, status, synced_at, fees_cents)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                            " ON CONFLICT (mode, session, receipt_id) DO UPDATE SET total = excluded.total,"
                            " total_cents = excluded.total_cents, items = excluded.items, status = excluded.status,"
                            " synced_at = excluded.synced_at,"
                            " fees_cents = COALESCE(excluded.fees_cents, etsy_orders.fees_cents)",
                            (
                                scope.mode,
                                scope.session,
                                order.receipt_id,
                                order.ordered_at,
                                order.currency,
                                *values,
                                fees,
                            ),
                        )
                    else:  # cancelled, refunded or not paid (yet): only an order already stored learns it
                        conn.execute(
                            "UPDATE etsy_orders SET total = ?, total_cents = ?, items = ?, status = ?, synced_at = ?"
                            f" WHERE {where} AND receipt_id = ?",
                            (*values, *params, order.receipt_id),
                        )
                observe(conn, scope, self.clock.today().isoformat(), stamp, self.settings.etsy_stats_history)
            if self.settings.etsy_auto_renew_sold:
                self._renew_sellers(shop, scope)
            self.db.set_meta(meta_key(scope.mode, "last_sync_at"), stamp)
            self.db.set_meta(meta_key(scope.mode, "last_error"), "")
            if self.after_sync is not None:
                try:
                    self.after_sync(scope, self.settings)
                except Exception:  # noqa: BLE001 - the numbers are stored; the next sync tries again
                    log.exception("Recording the Etsy orders in the ledger failed")
            return None
        finally:
            self._lock.release()

    def _processing_fees(self, shop: Shop, orders: list[etsy.Order], ours: set[int], known: set[int]) -> dict[int, int]:
        """0.12.0: the processing fee of each paid order with Ember's listings whose fees aren't known yet (at most
        FEE_READS a sync), read from its payment. A failure is left for the next sync, never raised."""
        found: dict[int, int] = {}
        for order in orders:
            if len(found) >= FEE_READS:
                break
            if not order.paid or order.receipt_id in known or not any(i.get("listing_id") in ours for i in order.items):
                continue
            try:
                with _guard(shop):
                    fee = shop.payment_fees(order.receipt_id)
            except Exception as exc:  # noqa: BLE001 - the next sync tries again
                error = str(exc) if isinstance(exc, EtsyError) else type(exc).__name__
                log.warning("Reading the payment of Etsy order %d failed: %s", order.receipt_id, error)
                break  # the same answer for the rest, most likely
            if fee is not None:
                found[order.receipt_id] = fee
        return found

    def _renew_sellers(self, shop: Shop, scope: AgentScope) -> None:
        """0.12.0: Etsy's automatic renewal for each live listing that sold and doesn't renew itself, tried once per
        listing: after that, the owner's own choice at Etsy stands (and a refusal isn't repeated every hour)."""
        where, params = scope.where()
        with self.db.connection() as conn:
            sold = sold_counts(conn, scope)
            rows = conn.execute(
                f"SELECT listing_id FROM etsy_listings WHERE {where} AND status = 'active' AND listing_id IS NOT NULL"
                f" AND COALESCE(state, '{etsy.LIVE_STATE}') = '{etsy.LIVE_STATE}' AND auto_renew = 0"
                " AND renew_set_at IS NULL",
                params,
            ).fetchall()
        for listing_id in [int(r["listing_id"]) for r in rows if sold.get(r["listing_id"])]:
            error = None
            try:
                with _guard(shop):
                    shop.set_auto_renew(listing_id, True)
            except Exception as exc:  # noqa: BLE001 - reported, never raised: the sync goes on
                error = str(exc) if isinstance(exc, EtsyError) else type(exc).__name__
                log.warning("Turning on automatic renewal for Etsy listing %d failed: %s", listing_id, error)
            with self.db.transaction() as conn:
                now = to_iso(self.clock.now())
                conn.execute(
                    f"UPDATE etsy_listings SET renew_set_at = ?, auto_renew = COALESCE(?, auto_renew) WHERE {where}"
                    " AND listing_id = ?",
                    (now, None if error else 1, *params, listing_id),
                )
                connectors.record(  # 0.13.0: Ember's code's own action, in the shared journal
                    conn,
                    scope,
                    "etsy.auto_renew",
                    str(listing_id),
                    "failed" if error else "simulated" if shop.simulated else "done",
                    now,
                    before={"auto_renew": False},
                    after=None if error else {"auto_renew": True},
                    note=error,
                )
            shop_name = "the dry run's fake shop" if shop.simulated else "Etsy"
            if error is None:
                message = (
                    f"Listing #{listing_id} sold {sold[listing_id]} time(s): Ember turned on automatic renewal at"
                    f" {shop_name} ({etsy.RENEWAL_FEE} every four months)"
                )
                events.record(self.db, "info", "etsy", message)
            else:
                message = (
                    f"Listing #{listing_id} sold, but Ember couldn't turn on its automatic renewal ({error}): turn it"
                    " on at Etsy, or it expires after four months"
                )
                events.record(self.db, "warning", "etsy", message[:300])


def _replace(
    old_ids: list[int],
    upload: Callable[[str, bytes, int], None],
    delete: Callable[[int], None],
    items: list[tuple[str, bytes]],
    most: int,
    progress: list[str],
) -> None:
    """Put ``items`` first (ranks 1, 2, ...) and delete the old ones, one old one earlier only where Etsy's limit
    (``most``) needs the room: a live digital listing never is without a photo or a file."""
    old = list(old_ids)
    for rank, (name, data) in enumerate(items, 1):
        while old and len(old) + rank - 1 >= most:
            delete(old.pop())
            progress.append("deleted")
        upload(name, data, rank)
        progress.append("uploaded")
    for item_id in old:
        delete(item_id)
        progress.append("deleted")


def _change_note(shop: Shop, edit: Edit, status: str, made: set[str], halfway: set[str], error: str | None) -> str:
    """What happened to an approved change, for the owner and the agent."""

    def words(parts: set[str] | list[str]) -> str:
        named = {"renew": "renewal", "deactivate": "deactivation", "auto_renew": "automatic renewal"}
        return ", ".join(named.get(p, p) for p in etsy.EDIT_PARTS if p in parts) or "nothing"

    url = etsy.listing_url(edit.listing_id)
    if status == "done":
        if shop.simulated:
            return f"Changed in the dry run's fake shop ({words(made)}); nothing reached Etsy."
        return f"Changed at Etsy: {words(made)}. {url}"
    rest = [p for p in edit.parts() if p not in made]
    half = f" Its {words(halfway)} were left half replaced." if halfway else ""
    if status == "failed":
        return f"Not changed: Etsy refused it ({error})."
    if status == "partial":
        return (
            f"Partly changed: {words(made)} changed; {words(rest)} not, Etsy refused it ({error}).{half} Check it at"
            f" Etsy: {etsy.edit_url(edit.listing_id)}"
        )
    return (
        f"It is unclear whether Etsy made all of the change ({error}). Changed for sure: {words(made)}.{half} Ember"
        f" won't try again; check the listing at Etsy: {etsy.edit_url(edit.listing_id)}"
    )


def _guard(shop: Shop) -> contextlib.AbstractContextManager[Any]:
    """The fake shop of a dry run needs no network, so it runs sealed; the owner's is reached by Ember's code only.
    (A new guard for every use: a sealed block can't be entered twice.)"""
    return netguard.sealed() if shop.simulated else contextlib.nullcontext()


def meta_key(mode: str, name: str) -> str:
    return f"integrations.etsy.{mode}.{name}"


NEWEST_SHOWN = 5  # listings (and requests) the plan describes one by one, the newest first
IDLE_SHOWN = 8  # listings Etsy says aren't live, named in the plan (0.12.0)


def _ending(row: sqlite3.Row) -> str:
    """When a listing ends at Etsy, as far as the last sync knew (0.12.0)."""
    if row["auto_renew"]:
        return "renews itself"
    if not row["ends_at"]:
        return ""
    return ("ends " if etsy_state(row) == etsy.LIVE_STATE else "ended ") + str(row["ends_at"])[:10]


def sold_counts(conn: sqlite3.Connection, scope: AgentScope, since: str | None = None) -> dict[int, int]:
    """How many of each listing sold (in orders that count: not cancelled or refunded), since ``since`` if given."""
    where, params = scope.where()
    period = " AND ordered_at >= ?" if since else ""
    sold: dict[int, int] = {}
    for order in conn.execute(
        f"SELECT items FROM etsy_orders WHERE {where} AND {etsy.COUNTED_ORDERS}{period}",
        (*params, since) if since else params,
    ):
        for item in json.loads(order["items"] or "[]"):
            sold[item.get("listing_id")] = sold.get(item.get("listing_id"), 0) + int(item.get("quantity") or 1)
    return sold


def live_rows(rows: list[sqlite3.Row], sold: dict[int, int]) -> list[sqlite3.Row]:
    """The listings live on Etsy, top sellers first, then the most favorited and the most seen (0.12.0: the plan saw
    only the newest 10 and the review the newest 8, so the oldest listings, seen the longest, dropped out first)."""
    live = [r for r in rows if r["listing_id"] and r["status"] == "active" and (r["state"] or "active") == "active"]
    return sorted(
        live, key=lambda r: (-sold.get(r["listing_id"], 0), -(r["favorites"] or 0), -(r["views"] or 0), -r["id"])
    )


def live_line(
    rows: list[sqlite3.Row],
    sold: dict[int, int],
    what: str = "sold",
    week: dict[int, int] | None = None,
    photos: dict[int, int] | None = None,
) -> str | None:
    """One line for every live listing: its number, its title's start and its numbers, with the views it gained this
    week where the history has them (``week``: its views a week ago) and its photos in Ember's records (``photos``,
    0.12.0: a separate line dropped out once there were about 10 listings)."""
    live = live_rows(rows, sold)
    if not live:
        return None
    entries = []
    for r in live:
        title = " ".join((r["title"] or "").split())
        title = title if len(title) <= 24 else title[:23].rstrip() + "…"
        views = r["views"] if r["views"] is not None else "?"
        before = (week or {}).get(r["listing_id"])
        gained = f"(+{r['views'] - before})" if before is not None and r["views"] is not None else ""
        favorites = r["favorites"] if r["favorites"] is not None else "?"
        shown = f" {photos[r['listing_id']]}p" if photos and r["listing_id"] in photos else ""
        entries.append(
            f"#{r['listing_id']} {title} {sold.get(r['listing_id'], 0)}s {views}v{gained} {favorites}f{shown}"
        )
    legend = f"{what} s, views v (gained this week), favorites f" if week else f"{what} s, views v, favorites f"
    legend += ", photos p" if photos else ""
    return f"All {len(live)} live listings, top sellers first ({legend}): " + " · ".join(entries)


def few_photos(conn: sqlite3.Connection, scope: AgentScope) -> list[tuple[int, int]]:
    """0.12.0: the live listings with fewer than qa.MIN_PHOTOS photos in Ember's records, as (listing, photos): a defect
    the obligations list until it is fixed."""
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT * FROM etsy_listings WHERE {where} AND listing_id IS NOT NULL AND status = 'active' ORDER BY id",
        params,
    ).fetchall()
    found = []
    for r in rows:
        if etsy_state(r) != etsy.LIVE_STATE:
            continue
        listing = recorded_listing(conn, scope, r)
        count = qa.distinct(listing.photos) if listing is not None else qa.MIN_PHOTOS  # 0.14.0: a copied file once
        if count < qa.MIN_PHOTOS:
            found.append((int(r["listing_id"]), count))
    return found


def recorded_listing(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> Listing | None:
    """A listing as Ember's records have it: a live one as listed or last changed, a draft as approved (0.12.0: drafts
    had no photo count at all). None when there is no readable record."""
    try:
        if row["status"] == "active":
            return current_listing(conn, scope, row["listing_id"])
        approval = conn.execute("SELECT * FROM approvals WHERE id = ?", (row["approval_id"],)).fetchone()
        return approved_listing(approval) if approval is not None else None
    except EtsyError:
        return None


def observe(conn: sqlite3.Connection, scope: AgentScope, day: str, now: str, listing_history: bool) -> int:
    """The day's observations (0.12.0), written by the first sync of the owner's day: the shop's counts from Ember's
    own records and, while the owner allows the history, each live listing's views and favorites. A day's first value
    stays. Returns how many were new."""
    where, params = scope.where()
    rows = conn.execute(f"SELECT * FROM etsy_listings WHERE {where}", params).fetchall()
    sold = sold_counts(conn, scope)
    live = live_rows(rows, sold)
    orders = conn.execute(
        f"SELECT COUNT(*) FROM etsy_orders WHERE {where} AND {etsy.COUNTED_ORDERS}", params
    ).fetchone()[0]
    values = [("shop", 0, "listings_live", len(live)), ("shop", 0, "orders", orders)]
    values.append(("shop", 0, "units_sold", sum(sold.values())))
    if listing_history:
        for r in live:
            values.extend(("listing", r["listing_id"], m, r[m]) for m in ("views", "favorites") if r[m] is not None)
    added = 0
    for subject, subject_id, metric, value in values:
        added += conn.execute(
            "INSERT INTO observations (mode, session, day, observed_at, subject, subject_id, metric, value)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (scope.mode, scope.session, day, now, subject, subject_id, metric, int(value)),
        ).rowcount
    return added


def week_ago(conn: sqlite3.Connection, scope: AgentScope, today: date, metric: str = "views") -> dict[int, int]:
    """Each listing's ``metric`` at the start of the last 7 days: the first value observed since then."""
    where, params = scope.where()
    first: dict[int, int] = {}
    for r in conn.execute(
        f"SELECT subject_id, value FROM observations WHERE {where} AND subject = 'listing' AND metric = ?"
        " AND day >= ? ORDER BY day",
        (*params, metric, (today - timedelta(days=7)).isoformat()),
    ):
        first.setdefault(int(r["subject_id"]), int(r["value"]))
    return first


def _flat(text: str | None, chars: int) -> str:
    """A listing's title or error on one line (0.12.0: the plan's sections are headed by lines of their own)."""
    return " ".join((text or "").split())[:chars]


def shop_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    clock: Clock,
    shop_name: str,
    daily_limit: int,
    auto_revenue: bool = False,
) -> str:
    """The ETSY SHOP section of the plan: every live listing and how it does (0.12.0: all of them, top sellers first),
    this week's orders, what to fix, and the newest listings and requests one by one. ``auto_revenue``: Ember's code
    records the orders' revenue (0.12.0, the owner's option), else the owner does."""
    where, params = scope.where()
    rows = conn.execute(f"SELECT * FROM etsy_listings WHERE {where} ORDER BY id DESC", params).fetchall()
    week = to_iso(clock.now() - timedelta(days=7))
    orders = conn.execute(
        f"SELECT total_cents, currency, items, fees_cents FROM etsy_orders WHERE {where} AND {etsy.COUNTED_ORDERS}"
        " AND ordered_at >= ?",
        (*params, week),
    ).fetchall()
    sold = sold_counts(conn, scope)
    lines = [f"Shop: {shop_name}." + ("" if rows else " No listings yet.")]
    photos: dict[int, int] = {}  # each listing's photos in Ember's records, drafts too
    for r in rows:
        if r["listing_id"] and r["status"] in ("active", "draft"):
            listing = recorded_listing(conn, scope, r)
            if listing is not None:
                photos[int(r["listing_id"])] = len(listing.photos)
    summary = live_line(rows, sold, week=week_ago(conn, scope, clock.today()), photos=photos)
    if summary:
        lines.append(summary)
    # 0.11.1: the plans never saw a photo count, so single-photo listings stayed that way
    few = [f"#{listing_id} ({count})" for listing_id, count in photos.items() if count < qa.MIN_PHOTOS]
    if few:
        lines.append(
            f"Fewer than {qa.MIN_PHOTOS} photos: {', '.join(few)}. Etsy shows up to {etsy.MAX_PHOTOS}: make more and"
            " give the whole set with propose_etsy_edit."
        )
    totals: dict[str, int] = {}
    fees: dict[str, int] = {}  # 0.12.0: Etsy's fees on them, where the payments were read
    for o in orders:
        totals[o["currency"]] = totals.get(o["currency"], 0) + int(o["total_cents"])
        fees[o["currency"]] = fees.get(o["currency"], 0) + int(o["fees_cents"] or 0)
    money = ", ".join(
        f"{cents / 100:.2f} {currency}" + (f" less {fees[currency] / 100:.2f} of Etsy's fees" if fees[currency] else "")
        for currency, cents in totals.items()
    )
    who = "Ember's code records it from Etsy's numbers" if auto_revenue else "your owner records it"
    lines.append(
        f"Orders in the last 7 days: {len(orders)}" + (f" ({money})" if money else "") + f"; revenue counts once {who}."
    )
    if rows:
        lines.append("Newest:")
    for r in rows[:NEWEST_SHOWN]:
        if r["listing_id"] and r["status"] in ("active", "draft"):
            state = r["state"] or r["status"]
            ending = f" · {_ending(r)}" if r["status"] == "active" and _ending(r) else ""
            lines.append(
                f"- #{r['listing_id']} [{state}] {_flat(r['title'], 80)} · since {r['started_at'][:10]}{ending}"
            )
        else:
            lines.append(
                f"- request #{r['approval_id']} [{r['status']}] {_flat(r['title'], 80)}: {_flat(r['error'], 100)}"
            )
    idle = [r for r in rows if r["listing_id"] and r["status"] == "active" and etsy_state(r) != etsy.LIVE_STATE]
    if idle:  # 0.12.0: they counted as live
        shown = ", ".join(f"#{r['listing_id']} ({state_text(r)})" for r in idle[:IDLE_SHOWN])
        more = f" and {len(idle) - IDLE_SHOWN} more" if len(idle) > IDLE_SHOWN else ""
        lines.append(
            f"Not live at Etsy: {shown}{more}. Renew one worth selling again with propose_etsy_edit (state renew,"
            f" {etsy.RENEWAL_FEE})."
        )
    left = max(0, daily_limit - created_today(conn, clock, scope))
    lines.append(f"Listings Ember can still create today: {left} of {daily_limit}.")
    if any(r["listing_id"] and r["status"] == "active" for r in rows):
        lines.append("Change a live listing (free at Etsy): etsy_listing shows it, propose_etsy_edit asks your owner.")
    waiting = conn.execute(
        f"SELECT id, status, json_extract(action, '$.listing_id') AS listing_id FROM approvals WHERE {where}"
        f" AND executor = 'etsy_edit' AND status IN ('pending', {_APPROVED_SQL}) ORDER BY id",
        params,
    ).fetchall()
    if waiting:
        where_now = {"pending": "your owner decides"}
        changes = [
            f"request #{w['id']} for #{w['listing_id']} ({where_now.get(w['status'], 'approved')})" for w in waiting
        ]
        lines.append(f"Changes not made yet: {'; '.join(changes)}.")
    return "\n".join(lines)


def listings_json(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[dict[str, Any]]:
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT * FROM etsy_listings WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()
    return [
        {
            "approval_id": r["approval_id"],
            "listing_id": r["listing_id"],
            "title": r["title"],
            "status": r["status"],
            "state": r["state"],
            "ends_at": r["ends_at"],  # 0.12.0
            "auto_renew": None if r["auto_renew"] is None else bool(r["auto_renew"]),
            "views": r["views"],
            "favorites": r["favorites"],
            "url": etsy.listing_url(r["listing_id"]) if r["listing_id"] else None,
            "started_at": r["started_at"],
            "synced_at": r["synced_at"],
            "result": r["result"],
            "error": r["error"],
        }
        for r in rows
    ]


MAX_ORDERS_READ = 1_000  # the orders looked at for the dashboard (0.12.0)


def orders_json(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``limit`` orders, and every older one that still needs the owner (0.12.0: the list stopped at 20,
    so older orders lost their Record as revenue button): not recorded yet, or recorded and refunded since."""
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT * FROM etsy_orders WHERE {where} ORDER BY ordered_at DESC, id DESC LIMIT ?", (*params, MAX_ORDERS_READ)
    ).fetchall()
    out = []
    for r in rows:
        key = revenue_key(r["receipt_id"])
        entry = conn.execute(
            "SELECT id, created_by, amount_micros + (SELECT COALESCE(SUM(k.amount_micros), 0) FROM ledger k"
            " WHERE k.corrects_id = ledger.id) AS left_micros FROM ledger WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        fees_key = fee_key(r["receipt_id"])
        fees_entry = conn.execute("SELECT id FROM ledger WHERE idempotency_key = ?", (fees_key,)).fetchone()
        items = json.loads(r["items"] or "[]")
        project_id, venture_id = order_project(conn, scope, items)
        status = r["status"]
        out.append(
            {
                "receipt_id": r["receipt_id"],
                "ordered_at": r["ordered_at"],
                "total": r["total"],
                "total_cents": r["total_cents"],
                "currency": r["currency"],
                "items": items,
                "revenue_key": key,
                "recorded": entry is not None,
                "entry_id": entry["id"] if entry else None,  # to correct if the order was refunded since
                "recorded_by": entry["created_by"] if entry else None,  # 0.12.0: the owner, or 'etsy' (Ember's code)
                "corrected_in_full": entry is not None and entry["left_micros"] <= 0,  # 0.12.0
                "project_id": project_id,  # what the revenue form suggests (0.12.0)
                "venture_id": venture_id,
                # 0.12.0: the order's status; an order from before has none, and its total is the whole receipt's.
                "status": status,
                "whole_receipt": status is None,
                "recordable": entry is None
                and status is not None
                and status not in etsy.DEAD_ORDERS
                and r["currency"] in ("EUR", "USD")
                and r["total_cents"] > 0,
                # 0.12.0: Ember's share of Etsy's fees, an expense the owner records once the revenue is recorded
                "fees_cents": r["fees_cents"],
                "fee_key": fees_key,
                "fees_recorded": fees_entry is not None,
                "fees_recordable": fees_entry is None
                and entry is not None
                and bool(r["fees_cents"])
                and status not in etsy.DEAD_ORDERS
                and r["currency"] in ("EUR", "USD"),
            }
        )
    return out[:limit] + [
        o for o in out[limit:] if o["recordable"] or (o["recorded"] and o["status"] in etsy.DEAD_ORDERS)
    ]


def order_project(conn: sqlite3.Connection, scope: AgentScope, items: list[Any]) -> tuple[int | None, int | None]:
    """The project an order's revenue belongs to, and its venture (0.12.0): the first of its listings Ember made, the
    request the owner approved for it, and that request's project (or its cycle's)."""
    for item in items:
        listing_id = item.get("listing_id") if isinstance(item, dict) else None
        if not isinstance(listing_id, int):
            continue
        row = conn.execute(
            "SELECT p.id AS project_id, p.venture_id FROM etsy_listings l JOIN approvals a ON a.id = l.approval_id"
            " LEFT JOIN cycles y ON y.id = a.cycle_id JOIN projects p ON p.id = COALESCE(a.project_id, y.project_id)"
            " WHERE l.listing_id = ? AND l.mode = ? AND l.session = ? ORDER BY l.id DESC LIMIT 1",
            (listing_id, scope.mode, scope.session),
        ).fetchone()
        if row is None:  # 0.13.0 (Phase E4): a listing Printify made of one of Ember's products
            row = conn.execute(
                "SELECT p.id AS project_id, p.venture_id FROM printify_products x JOIN approvals a"
                " ON a.id = x.approval_id LEFT JOIN cycles y ON y.id = a.cycle_id JOIN projects p"
                " ON p.id = COALESCE(a.project_id, y.project_id) WHERE x.listing_id = ? AND x.mode = ?"
                " AND x.session = ? ORDER BY x.id DESC LIMIT 1",
                (listing_id, scope.mode, scope.session),
            ).fetchone()
        if row is not None:
            return row["project_id"], row["venture_id"]
    return None, None


def fee_key(receipt_id: int) -> str:
    """The ledger's request key for an order's Etsy fees recorded as an expense (0.12.0), like ``revenue_key``."""
    return hashlib.sha256(f"etsy-fees-{receipt_id}".encode()).hexdigest()[:32]


def revenue_key(receipt_id: int) -> str:
    """The ledger's request key for an order recorded as revenue (32 hex characters, the ledger's format), always
    the same for one order, so it is never recorded twice."""
    return hashlib.sha256(f"etsy-order-{receipt_id}".encode()).hexdigest()[:32]
