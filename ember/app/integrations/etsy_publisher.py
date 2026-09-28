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
from datetime import timedelta
from typing import Any

from .. import events
from ..agent import netguard
from ..agent.sandbox import Jail, SandboxError
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from . import etsy
from .etsy import EtsyError, Listing, NotSent, Shop, Unclear

log = logging.getLogger(__name__)

CLOSED_BY = "Ember"
APPROVED = ("approved", "approved_with_changes")
COUNTED = ("running", "active", "draft", "unclear")  # what uses up the daily limit
SYNC_MINUTES = 60
ORDER_DAYS = 30  # how far back the sync looks for orders
INTERRUPTED = "the app stopped while creating the listing"
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


class Publisher:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Callable[[], AgentScope],
        shop: Callable[[], Shop | None],
        workspace: Callable[[], Jail],
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.shop = shop
        self.workspace = workspace
        self._lock = threading.Lock()  # one run (or sync) at a time in this process

    # --- approved listings ---

    def run(self) -> list[tuple[int, str]]:
        shop = self.shop()
        if shop is None or not self._lock.acquire(blocking=False):
            return []
        try:
            scope = self.scope()
            where, params = scope.where()
            with self.db.connection() as conn:
                ids = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM approvals WHERE {where} AND executor = 'etsy_listing' AND status IN {APPROVED}"
                        " AND NOT EXISTS (SELECT 1 FROM etsy_listings x WHERE x.approval_id = approvals.id)"
                        " ORDER BY id",
                        params,
                    )
                ]
            done = []
            for approval_id in ids:
                outcome = self._one(shop, scope, approval_id)
                done.append((approval_id, outcome))
                if outcome == "waiting_limit":
                    break  # the rest waits for tomorrow too, in order
            return done
        finally:
            self._lock.release()

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
        jail = self.workspace()
        read: list[list[tuple[str, bytes]]] = [[], []]
        for group, uploads in enumerate((listing.photos, listing.files)):
            for u in uploads:
                try:
                    data = jail.read_bytes(u.path)
                except SandboxError as exc:
                    raise EtsyError(f"{u.path} can't be read: {exc}") from None
                if hashlib.sha256(data).hexdigest() != u.sha256:
                    raise EtsyError(CHANGED.format(path=u.path))
                read[group].append((u.path.rsplit("/", 1)[-1], data))
        return read[0], read[1]

    def _after(self, approval_id: int, status: str, listing_id: int | None, note: str, error: str | None = None) -> str:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE etsy_listings SET status = ?, finished_at = ?, listing_id = COALESCE(?, listing_id),"
                " result = ?, error = ? WHERE approval_id = ?",
                (status, to_iso(self.clock.now()), listing_id, note[:500], (error or "")[:500] or None, approval_id),
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
        conn.execute(
            "UPDATE etsy_listings SET status = 'failed', finished_at = ?, error = ? WHERE approval_id = ?",
            (to_iso(self.clock.now()), reason[:500], approval_id),
        )
        self._close(conn, approval_id, "failed", note, None)
        return "failed"

    @staticmethod
    def _start(conn: sqlite3.Connection, scope: AgentScope, approval_id: int, stamp: str, title: str) -> None:
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, status, title)"
            " VALUES (?, ?, ?, ?, 'running', ?)",
            (scope.mode, scope.session, approval_id, stamp, title[:140]),
        )

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
        for row in rows:
            note = f"It is unclear whether the listing was finished ({INTERRUPTED}). Ember won't try again; check Etsy."
            self._after(row["approval_id"], "unclear", row["listing_id"], note, INTERRUPTED)
        return len(rows)

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
            with self.db.transaction() as conn:
                for item in remote:
                    conn.execute(
                        f"UPDATE etsy_listings SET state = ?, views = ?, favorites = ?, synced_at = ? WHERE {where}"
                        " AND listing_id = ?",
                        (item.state[:20], item.views, item.favorites, stamp, *params, item.listing_id),
                    )
                for order in orders:
                    items = [i for i in order.items if i.get("listing_id") in ours]
                    if not items:
                        continue  # the owner's own products: not Ember's business
                    total = f"{order.total_cents / 100:.2f} {order.currency}"
                    conn.execute(
                        "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, total, total_cents, currency,"
                        " items, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (mode, session, receipt_id) DO UPDATE SET synced_at = excluded.synced_at",
                        (
                            scope.mode,
                            scope.session,
                            order.receipt_id,
                            order.ordered_at,
                            total,
                            order.total_cents,
                            order.currency[:3],
                            json.dumps(items, ensure_ascii=False)[:4000],
                            stamp,
                        ),
                    )
            self.db.set_meta(meta_key(scope.mode, "last_sync_at"), stamp)
            self.db.set_meta(meta_key(scope.mode, "last_error"), "")
            return None
        finally:
            self._lock.release()


def _guard(shop: Shop) -> contextlib.AbstractContextManager[Any]:
    """The fake shop of a dry run needs no network, so it runs sealed; the owner's is reached by Ember's code only.
    (A new guard for every use: a sealed block can't be entered twice.)"""
    return netguard.sealed() if shop.simulated else contextlib.nullcontext()


def meta_key(mode: str, name: str) -> str:
    return f"integrations.etsy.{mode}.{name}"


def shop_text(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, shop_name: str, daily_limit: int) -> str:
    """The ETSY SHOP section of the plan: the listings Ember made and how they do, and this week's orders."""
    where, params = scope.where()
    rows = conn.execute(f"SELECT * FROM etsy_listings WHERE {where} ORDER BY id DESC LIMIT 10", params).fetchall()
    week = to_iso(clock.now() - timedelta(days=7))
    orders = conn.execute(
        f"SELECT total_cents, currency, items FROM etsy_orders WHERE {where} AND ordered_at >= ?", (*params, week)
    ).fetchall()
    sold: dict[int, int] = {}
    for order in conn.execute(f"SELECT items FROM etsy_orders WHERE {where}", params):
        for item in json.loads(order["items"] or "[]"):
            sold[item.get("listing_id")] = sold.get(item.get("listing_id"), 0) + int(item.get("quantity") or 1)
    lines = [
        f"Shop: {shop_name}. Listings you made (newest first):" if rows else f"Shop: {shop_name}. No listings yet."
    ]
    for r in rows:
        if r["listing_id"] and r["status"] in ("active", "draft"):
            views = r["views"] if r["views"] is not None else "?"
            favorites = r["favorites"] if r["favorites"] is not None else "?"
            numbers = f"{views} views · {favorites} favorites · {sold.get(r['listing_id'], 0)} sold"
            state = r["state"] or r["status"]
            lines.append(f"- #{r['listing_id']} [{state}] {r['title'][:80]} · {numbers} · since {r['started_at'][:10]}")
        else:
            lines.append(f"- request #{r['approval_id']} [{r['status']}] {r['title'][:80]}: {(r['error'] or '')[:100]}")
    totals: dict[str, int] = {}
    for o in orders:
        totals[o["currency"]] = totals.get(o["currency"], 0) + int(o["total_cents"])
    money = ", ".join(f"{cents / 100:.2f} {currency}" for currency, cents in totals.items())
    lines.append(
        f"Orders in the last 7 days: {len(orders)}" + (f" ({money})" if money else "") + "; revenue counts once your"
        " owner records it."
    )
    left = max(0, daily_limit - created_today(conn, clock, scope))
    lines.append(f"Listings Ember can still create today: {left} of {daily_limit}.")
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


def orders_json(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[dict[str, Any]]:
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT * FROM etsy_orders WHERE {where} ORDER BY ordered_at DESC, id DESC LIMIT ?", (*params, limit)
    ).fetchall()
    out = []
    for r in rows:
        key = revenue_key(r["receipt_id"])
        recorded = conn.execute("SELECT 1 FROM ledger WHERE idempotency_key = ?", (key,)).fetchone() is not None
        out.append(
            {
                "receipt_id": r["receipt_id"],
                "ordered_at": r["ordered_at"],
                "total": r["total"],
                "total_cents": r["total_cents"],
                "currency": r["currency"],
                "items": json.loads(r["items"] or "[]"),
                "revenue_key": key,
                "recorded": recorded,
            }
        )
    return out


def revenue_key(receipt_id: int) -> str:
    """The ledger's request key for an order recorded as revenue (32 hex characters, the ledger's format), always
    the same for one order, so it is never recorded twice."""
    return hashlib.sha256(f"etsy-order-{receipt_id}".encode()).hexdigest()[:32]
