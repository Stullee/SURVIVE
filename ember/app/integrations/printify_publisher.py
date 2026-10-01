"""Carrying out approved Printify products, the catalog Ember's code keeps, and the sync (0.13.0, Phase E4).

An approved product (executor 'printify_product') is carried out once, like an Etsy listing: its row is committed as
'running' before anything is sent, so a crash never makes it twice ('unclear' when it can't be known; the app's next
start marks what a crash left running). The picture must be exactly the file the owner approved (its SHA-256). Ember's
code uploads it, creates the product at Printify (not published yet) and reads what each variant costs to make: every
price must keep printify.MIN_MARGIN after Etsy's fees, making and shipping, or the unpublished product is deleted and
the request fails with the price each variant would need. Then it publishes the product to the Etsy shop through
Printify ('publishing' until the sync reads the Etsy listing Printify made: 'active'). At most
printify_products_per_day a day. The owner's Undo of a product is a request of theirs (executor 'printify_delete'),
carried out here too.

The catalog (Printify's products, their providers, and a provider's variants with their print area and the shipping to
Germany) is kept in printify_catalog for CATALOG_DAYS, so the agent's proposal is checked without reaching Printify.
The sync (at most every SYNC_MINUTES) reads the Etsy listing of each product being published, notes a product deleted
at Printify, and keeps the Printify orders of Ember's products: what making and shipping them costs the owner.

0.14.0: what each variant costs to make is kept in the catalog too ('costs'), from every product Ember's code creates
and from a cost probe: an unpublished product Ember's code creates only to read its costs, and deletes at once (never
published). So the agent sees a product's costs and least prices before it proposes one. The sync also reads a product
whose publish was unclear, or that reached no Etsy listing: it takes the listing Printify made, or fails the product
with the reason after PUBLISH_HOURS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from PIL import Image

from .. import events
from ..agent.sandbox import Jail, SandboxError
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from ..products import images
from . import connectors, etsy
from .printify import (
    MIN_MARGIN,
    Account,
    Blueprint,
    Gone,
    Made,
    NotSent,
    PrintifyError,
    Product,
    Provider,
    Upload,
    Variant,
    convert,
    keeps,
    kept,
    least_price,
    money,
    product_from_action,
)

log = logging.getLogger(__name__)

APPROVED = "('approved', 'approved_with_changes')"
CLOSED_BY = "Ember"
SYNC_MINUTES = 60
CATALOG_DAYS = {"blueprints": 7, "providers": 7, "variants": 1, "costs": 7}
LIVE = ("publishing", "active")
INTERRUPTED = "the app stopped while creating the product"
GONE = "Deleted at Printify, not by Ember's code"
# 0.14.0: a product published at Printify whose Etsy listing isn't there PUBLISH_HOURS later failed with STALE (the sync
# still takes the listing if it comes); the cost probe's product, never published, made at most once in PROBE_DAYS
# for a product and provider, whatever came of it.
PUBLISH_HOURS = 24
STALE = "Printify didn't publish it to Etsy"
PROBE_TITLE = "Ember cost probe (not for sale)"
PROBE_PICTURE = "ember-cost-probe.png"
PROBE_VARIANTS = 100
PROBE_PRICE = 99_900  # any price: the probe is never published
PROBE_DAYS = 1
_PROBING = threading.Lock()  # a probe, and the sync's cleaning up after one, one at a time
SHOWN = 10  # blueprints a catalog search shows
CANCELED = "('canceled', 'cancelled')"  # an order's status that doesn't count (SQL)


def meta_key(mode: str, name: str) -> str:
    return f"integrations.printify.{mode}.{name}"


# --- the catalog Ember's code keeps ---------------------------------------------------------------------------------


def cached(conn: sqlite3.Connection, mode: str, kind: str, key: str, now: Any = None) -> Any:
    """What the catalog holds for ``kind`` and ``key`` (parsed JSON), or None; with ``now``, only if still fresh."""
    row = conn.execute(
        "SELECT data, fetched_at FROM printify_catalog WHERE mode = ? AND kind = ? AND key = ?", (mode, kind, key)
    ).fetchone()
    if row is None:
        return None
    if now is not None and now - from_iso(row["fetched_at"]) > timedelta(days=CATALOG_DAYS[kind]):
        return None
    return json.loads(row["data"])


def _store(conn: sqlite3.Connection, mode: str, kind: str, key: str, data: Any, now: str) -> None:
    conn.execute(
        "INSERT INTO printify_catalog (mode, kind, key, data, fetched_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (mode, kind, key) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
        (mode, kind, key, json.dumps(data, ensure_ascii=False, separators=(",", ":")), now),
    )


def variants(conn: sqlite3.Connection, mode: str, blueprint_id: int, provider_id: int) -> list[Variant] | None:
    """The variants a provider makes of a product, as the catalog last read them (None: never read)."""
    data = cached(conn, mode, "variants", f"{blueprint_id}:{provider_id}")
    return None if data is None else [Variant(*v) for v in data]


def names(conn: sqlite3.Connection, mode: str, blueprint_id: int, provider_id: int) -> tuple[str, str]:
    """A product's and a provider's names, as the catalog last read them."""
    product = next((b[1] for b in cached(conn, mode, "blueprints", "") or [] if b[0] == blueprint_id), "")
    provider = next((p[1] for p in cached(conn, mode, "providers", str(blueprint_id)) or [] if p[0] == provider_id), "")
    return product or f"product #{blueprint_id}", provider or f"provider #{provider_id}"


def costs_of(
    conn: sqlite3.Connection, mode: str, blueprint_id: int, provider_id: int, now: Any = None
) -> dict[int, int]:
    """0.14.0: what each variant a provider makes of a product costs to make (cents, in the currency Printify states
    for the variants), as Printify last said ({}: not known; with ``now``, only if still fresh)."""
    data = cached(conn, mode, "costs", f"{blueprint_id}:{provider_id}", now)
    return {int(v): int(c) for v, c in data["costs"]} if data else {}


def keep_costs(
    conn: sqlite3.Connection,
    mode: str,
    blueprint_id: int,
    provider_id: int,
    costs: dict[int, int],
    now: str,
    probed: str = "",
    error: str = "",
) -> None:
    """0.14.0: add what Printify said variants cost to make (a product Ember's code created, or a probe: when it was
    made, and why it failed) to the catalog."""
    key = f"{blueprint_id}:{provider_id}"
    data = cached(conn, mode, "costs", key, from_iso(now)) or {"probed": "", "error": "", "costs": []}
    merged = {int(v): int(c) for v, c in data["costs"]} | {int(v): int(c) for v, c in costs.items()}
    if probed:
        data["probed"], data["error"] = probed, error
    data["costs"] = sorted([v, c] for v, c in merged.items())
    _store(conn, mode, "costs", key, data, now)


def clean_probe(db: Database, mode: str, account: Account, shop: int) -> None:
    """0.14.0: delete what a cost probe left at Printify (its delete failed), or tell the owner once when it can't be
    known (the app stopped, or Printify's answer was lost, while it was being created). Not while a probe runs."""
    if _PROBING.acquire(blocking=False):
        try:
            _clean_probe(db, mode, account, shop)
        finally:
            _PROBING.release()


def _clean_probe(db: Database, mode: str, account: Account, shop: int) -> None:
    key = meta_key(mode, "probe")
    raw = db.get_meta(key)
    if not raw:
        return
    left = json.loads(raw).get("product_id")
    if left:
        try:
            account.delete(shop, str(left))
        except Gone:
            pass
        except PrintifyError as exc:
            log.warning("Deleting the cost probe %s at Printify failed again: %s", left, exc)
            return  # tried again at the next probe or sync
    else:
        events.record(
            db,
            "warning",
            "printify",
            f"A cost probe of Ember's code may have left an unpublished product ({PROBE_TITLE}) in your Printify "
            "account: delete it there if you see it",
        )
    db.set_meta(key, "")


class Catalog:
    """Printify's catalog for the agent's printify_catalog: read from Printify when the kept copy is old."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        mode: str,
        account: Callable[[], Account | None],
        shop_id: Callable[[], int | None] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.mode = mode
        self.account = account
        self.shop_id = shop_id  # 0.14.0: the shop a cost probe is made in (None: no probe)

    def _read(self, kind: str, key: str, fetch: Callable[[Account], list[Any]]) -> list[Any]:
        now = self.clock.now()
        with self.db.connection() as conn:
            kept_copy = cached(conn, self.mode, kind, key, now)
        if kept_copy is not None:
            return list(kept_copy)
        account = self.account()
        if account is None:
            raise PrintifyError("Printify isn't set up")
        rows = fetch(account)
        with self.db.transaction() as conn:
            _store(conn, self.mode, kind, key, rows, to_iso(now))
        return rows

    def blueprints(self) -> list[Blueprint]:
        rows = self._read("blueprints", "", lambda a: [[b.blueprint_id, b.title, b.brand] for b in a.blueprints()])
        return [Blueprint(int(i), str(t), str(b)) for i, t, b in rows]

    def providers(self, blueprint_id: int) -> list[Provider]:
        rows = self._read(
            "providers", str(blueprint_id), lambda a: [[p.provider_id, p.title] for p in a.providers(blueprint_id)]
        )
        return [Provider(int(i), str(t)) for i, t in rows]

    def variants(self, blueprint_id: int, provider_id: int) -> list[Variant]:
        rows = self._read(
            "variants",
            f"{blueprint_id}:{provider_id}",
            lambda a: [
                [v.variant_id, v.title, v.width, v.height, v.shipping_cents, v.currency]  # 0.14.0: with its currency
                for v in a.variants(blueprint_id, provider_id)
            ],
        )
        return [Variant(*v) for v in rows]

    def costs(self, blueprint_id: int, provider_id: int, found: list[Variant]) -> tuple[dict[int, int], str]:
        """0.14.0: what the variants cost to make, as kept, or read now by a cost probe (at most once in PROBE_DAYS):
        the costs, and why some aren't known ("" when all are)."""
        now = self.clock.now()
        wanted = [v.variant_id for v in found[:PROBE_VARIANTS]]
        with self.db.connection() as conn:
            data = cached(conn, self.mode, "costs", f"{blueprint_id}:{provider_id}", now) or {}
        known = {int(v): int(c) for v, c in data.get("costs", [])}
        if all(v in known for v in wanted):
            return known, ""
        probed = data.get("probed")
        if probed and now - from_iso(probed) < timedelta(days=PROBE_DAYS):
            return known, data.get("error") or "Printify said nothing about the others"
        account, shop = self.account(), self.shop_id() if self.shop_id is not None else None
        if account is None or shop is None:
            return known, "no probe can be made now"
        try:
            read, error = self._probe(account, shop, blueprint_id, provider_id, found[:PROBE_VARIANTS]), ""
        except PrintifyError as exc:
            read, error = {}, f"the probe failed: {exc}"[:300]
        with self.db.transaction() as conn:
            keep_costs(conn, self.mode, blueprint_id, provider_id, read, to_iso(now), probed=to_iso(now), error=error)
        known |= read
        return known, error or ("" if all(v in known for v in wanted) else "Printify said nothing about the others")

    def _probe(
        self, account: Account, shop: int, blueprint_id: int, provider_id: int, found: list[Variant]
    ) -> dict[int, int]:
        """An unpublished product of the variants, only to read what each costs to make, deleted at once: never
        published. Its number is kept (meta) until it is deleted, so what a failed delete leaves is deleted later.
        Raises PrintifyError."""
        if not _PROBING.acquire(blocking=False):
            raise PrintifyError("another probe is under way")
        try:
            _clean_probe(self.db, self.mode, account, shop)
            key = meta_key(self.mode, "probe")
            if self.db.get_meta(key):
                raise PrintifyError("the last probe's product couldn't be deleted yet")
            picture = images.png(Image.new("RGB", (400, 600), (255, 255, 255)))
            image_key = meta_key(self.mode, "probe_image")  # one blank picture, uploaded once
            area = max(found, key=lambda v: v.width * v.height)
            product = Product(
                title=PROBE_TITLE,
                description="Made by Ember's code only to read what making it costs; deleted at once, never published.",
                tags=(),
                blueprint_id=blueprint_id,
                provider_id=provider_id,
                prices=tuple((v.variant_id, PROBE_PRICE) for v in found),
                shipping=(),
                image=Upload(PROBE_PICTURE, hashlib.sha256(picture).hexdigest(), len(picture)),
                width=400,
                height=600,
                area_width=area.width,
                area_height=area.height,
                currency="",
            )
            image_id = self.db.get_meta(image_key)
            while True:
                fresh = not image_id
                if fresh:
                    image_id = account.upload(PROBE_PICTURE, picture)
                    self.db.set_meta(image_key, image_id)
                self.db.set_meta(key, json.dumps({"product_id": None}))  # before anything is made
                try:
                    made = account.create(shop, product, str(image_id))
                    break
                except NotSent:
                    self.db.set_meta(key, "")  # nothing was made
                    if fresh:
                        raise
                    image_id = None  # the kept picture may be gone at Printify: once more with a new one
            self.db.set_meta(key, json.dumps({"product_id": made.product_id}))
            _clean_probe(self.db, self.mode, account, shop)
            return made.costs
        finally:
            _PROBING.release()

    def answer(
        self,
        search: str | None,
        blueprint_id: int | None,
        provider_id: int | None,
        currency: str,
        usd_per_eur: float = 0.0,
    ) -> str:
        """The printify_catalog tool's answer. Raises PrintifyError."""
        if blueprint_id is None:
            if not search:
                raise PrintifyError("give search words, or a blueprint_id")
            nodes = [(b.blueprint_id, f"{b.title} ({b.brand})") for b in self.blueprints()]
            found = etsy.search_categories(nodes, search, limit=len(nodes))
            if not found:
                return f"No Printify product holds all of: {search}. Try fewer or other words."
            lines = "\n".join(f"#{i}: {title}" for i, title in found[:SHOWN])
            more = f"\n...and {len(found) - SHOWN} more: add a word." if len(found) > SHOWN else ""
            return f"Printify's products (blueprint_id: name):\n{lines}{more}\nNext: blueprint_id for its providers."
        if provider_id is None:
            found = self.providers(blueprint_id)
            if not found:
                return f"No print provider makes product #{blueprint_id} now."
            lines = "\n".join(f"#{p.provider_id}: {p.title}" for p in found)
            return (
                f"Who makes product #{blueprint_id} (provider_id: name):\n{lines}\nNext: blueprint_id and provider_id."
            )
        found_variants = self.variants(blueprint_id, provider_id)
        if not found_variants:
            return f"Provider #{provider_id} makes nothing of product #{blueprint_id} that ships to Germany."
        # 0.14.0: shipping in the currency Printify states (converted to printify_currency's), and what making costs
        # with the least price that keeps the margin (a cost probe reads it)
        costs, unknown = self.costs(blueprint_id, provider_id, found_variants)
        lines, refused = [], ""
        for v in found_variants:
            line = f"#{v.variant_id}: {v.title}: print area {v.width} x {v.height} pixels, shipping to Germany "
            try:
                shipping = convert(v.shipping_cents, v.currency, currency, usd_per_eur)
            except PrintifyError as exc:
                lines.append(line + money(v.shipping_cents, v.currency))
                refused = str(exc)
                continue
            line += money(shipping, currency)
            if v.currency not in ("", currency):
                line += f" ({money(v.shipping_cents, v.currency)})"
            if v.variant_id in costs:
                making = convert(costs[v.variant_id], v.currency, currency, usd_per_eur)
                least = least_price(making, shipping, currency, usd_per_eur)
                line += f", making {money(making, currency)}, least price {money(least, currency)}"
            lines.append(line)
        joined = "\n".join(lines)
        return (
            f"Variants of product #{blueprint_id} by provider #{provider_id} (variant_id: name):\n{joined}\n"
            + (f"Shipping: {refused}, so no product of it can be proposed.\n" if refused else "")
            + f"A price keeps {MIN_MARGIN * 100:.0f}% after Etsy's fees, making and shipping from its least price on"
            + (f"; making isn't known for all ({unknown})." if unknown else ".")
        )


# --- Ember's products -------------------------------------------------------------------------------------------


def products(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[sqlite3.Row]:
    """Ember's products, the newest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM printify_products WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def listing_ids(conn: sqlite3.Connection, scope: AgentScope) -> list[int]:
    """The Etsy listings Printify made of Ember's products (Etsy's sync reads them as Ember's)."""
    where, params = scope.where()
    return [
        int(r[0])
        for r in conn.execute(
            f"SELECT listing_id FROM printify_products WHERE {where} AND listing_id IS NOT NULL", params
        )
    ]


def product_json(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "approval_id": r["approval_id"],
        "product_id": r["product_id"],
        "listing_id": r["listing_id"],
        "url": etsy.listing_url(r["listing_id"]) if r["listing_id"] else None,
        "title": r["title"],
        "status": r["status"],
        "currency": r["currency"],
        "prices": json.loads(r["prices"]) if r["prices"] else [],  # [variant, price, cost, shipping, kept]
        "views": r["views"],
        "favorites": r["favorites"],
        "state": r["state"],
        "result": r["result"],
    }


def order_key(order_id: str) -> str:
    """The ledger's idempotency key for the cost of a Printify order recorded as an expense (as for Etsy's fees)."""
    return hashlib.sha256(f"printify-order-{order_id}".encode()).hexdigest()[:32]


def orders_json(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[dict[str, Any]]:
    """The Printify orders of Ember's products, newest first: what making and shipping each costs the owner, and
    whether that cost is in the ledger."""
    where, params = scope.where("o")
    # 0.14.0: with the tax Printify bills, and the project (and its venture) the product belongs to, as its sale's
    # revenue (the cost was the owner's overhead)
    rows = conn.execute(
        "SELECT o.order_id, o.currency, o.status, MIN(o.created_at) AS created_at, SUM(o.quantity) AS quantity,"
        " SUM(o.cost_cents) AS cost_cents, SUM(o.shipping_cents) AS shipping_cents, SUM(o.tax_cents) AS tax_cents,"
        " GROUP_CONCAT(p.title, '; ') AS titles, MIN(j.id) AS project_id, MIN(j.venture_id) AS venture_id"
        " FROM printify_orders o LEFT JOIN printify_products p"
        " ON p.product_id = o.product_id AND p.mode = o.mode AND p.session = o.session"
        " LEFT JOIN approvals a ON a.id = p.approval_id LEFT JOIN cycles y ON y.id = a.cycle_id"
        f" LEFT JOIN projects j ON j.id = COALESCE(a.project_id, y.project_id) WHERE {where}"
        " GROUP BY o.order_id ORDER BY created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    found = []
    for r in rows:
        key = order_key(str(r["order_id"]))
        recorded = conn.execute("SELECT 1 FROM ledger WHERE idempotency_key = ?", (key,)).fetchone() is not None
        total = int(r["cost_cents"] or 0) + int(r["shipping_cents"] or 0) + int(r["tax_cents"] or 0)
        found.append(
            {
                "order_id": r["order_id"],
                "created_at": r["created_at"],
                "status": r["status"],
                "quantity": r["quantity"],
                "titles": r["titles"] or "",
                "currency": r["currency"],
                "cost_cents": total,
                "cost": money(total, str(r["currency"])),
                "key": key,
                "recorded": recorded,
                "project_id": r["project_id"],
                "venture_id": r["venture_id"],
            }
        )
    return found


def totals(conn: sqlite3.Connection, scope: AgentScope) -> tuple[int, int]:
    """Ember's products in the shop now, and the orders of its products in all (the metrics pod_products_live and
    pod_orders): Printify's records at the last sync."""
    where, params = scope.where()
    live = conn.execute(  # 0.14.0: live at Etsy as the Etsy sync last read it (an expired listing isn't)
        f"SELECT COUNT(*) FROM printify_products WHERE {where} AND status = 'active' AND COALESCE(state, ?) = ?",
        (*params, etsy.LIVE_STATE, etsy.LIVE_STATE),
    )
    orders = conn.execute(
        f"SELECT COUNT(DISTINCT order_id) FROM printify_orders WHERE {where} AND status NOT IN {CANCELED}",
        params,
    )
    return int(live.fetchone()[0]), int(orders.fetchone()[0])


def created_today(conn: sqlite3.Connection, clock: Clock, scope: AgentScope) -> int:
    where, params = scope.where()
    start = to_iso(clock.day_start(clock.today()))
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM printify_products WHERE {where} AND started_at >= ? AND status <> 'failed'",
            (*params, start),
        ).fetchone()[0]
    )


def execution(
    conn: sqlite3.Connection, row: sqlite3.Row, scope: AgentScope, clock: Clock, daily_limit: int
) -> dict[str, Any] | None:
    """What happened to an approved product, or to the owner's Undo of one, for the dashboard (None before
    approval)."""
    if row["executor"] == "printify_delete":
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
        made = conn.execute("SELECT * FROM printify_products WHERE approval_id = ?", (row["id"],)).fetchone()
        if made is not None:
            return {
                "status": made["status"],
                "started_at": made["started_at"],
                "finished_at": made["finished_at"],
                "result": made["result"],
                "error": made["error"],
                "url": etsy.listing_url(made["listing_id"]) if made["listing_id"] else None,
            }
    if row["status"] not in ("approved", "approved_with_changes"):
        return None
    limited = row["executor"] == "printify_product" and created_today(conn, clock, scope) >= daily_limit
    return {
        "status": "waiting_limit" if limited else "waiting",
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "url": None,
    }


def text(conn: sqlite3.Connection, scope: AgentScope, limit: int = 6) -> str:
    """The plan's PRINTIFY: Ember's newest products with their prices, costs and numbers, and the orders."""
    made = products(conn, scope, limit)
    if not made:
        return "No product of yours yet: your first one waits for your owner's decision."
    lines = []
    for r in made:
        where = f"Etsy #{r['listing_id']}" if r["listing_id"] else "no Etsy listing yet"
        numbers = f", {r['views'] or 0} views, {r['favorites'] or 0} favorites" if r["synced_at"] else ""
        why = f": {r['error'][:80]}" if r["status"] == "failed" and r["error"] else ""  # 0.14.0
        lines.append(f"- {r['title'][:60]} ({r['status']}, {where}{numbers}){why}")
        for variant, price, cost, shipping, left in json.loads(r["prices"]) if r["prices"] else []:
            currency = str(r["currency"])
            lines.append(
                f"  variant {variant}: {money(price, currency)}, making {money(cost, currency)} + shipping "
                f"{money(shipping, currency)}, keeps {money(left, currency)}"
            )
    live, orders = totals(conn, scope)
    lines.append(f"Orders of your products at Printify: {orders} ({live} products in the shop).")
    return "\n".join(lines)


# --- carrying out ----------------------------------------------------------------------------------------------


class Publisher:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Callable[[], AgentScope],
        account: Callable[[], Account | None],
        shop_id: Callable[[], int | None],
        workspace: Callable[[], Jail],
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.account = account
        self.shop_id = shop_id
        self.workspace = workspace
        self._lock = threading.Lock()  # one run (or sync) at a time in this process

    def run(self) -> list[tuple[int, str]]:
        """Carry out the approved products (and the owner's Undos of products) that are due."""
        account = self.account()
        if account is None or not self._lock.acquire(blocking=False):
            return []
        try:
            scope = self.scope()
            creating = self._approved(scope, "printify_product")
            deleting = self._approved(scope, "printify_delete")
            if not creating and not deleting:
                return []
            shop = self.shop_id()
            if shop is None:
                return []  # it can't be told which shop: the dashboard says why, and the requests wait
            done = []
            for approval_id in creating:
                outcome = self._one(account, scope, shop, approval_id)
                done.append((approval_id, outcome))
                if outcome == "waiting_limit":
                    break  # the rest waits for tomorrow too, in order
            for approval_id in deleting:
                done.append((approval_id, self._delete(account, scope, shop, approval_id)))
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
                    " AND NOT EXISTS (SELECT 1 FROM printify_products p WHERE p.approval_id = approvals.id)"
                    " ORDER BY id",
                    (*params, executor),
                )
            ]

    def _image(self, product: Product) -> bytes:
        try:
            data = self.workspace().read_bytes(product.image.path)
        except SandboxError as exc:
            raise PrintifyError(f"{product.image.path} can't be read: {exc}") from None
        if hashlib.sha256(data).hexdigest() != product.image.sha256:
            raise PrintifyError(f"{product.image.path} changed after it was approved: propose the product again")
        return data

    def _one(self, account: Account, scope: AgentScope, shop: int, approval_id: int) -> str:
        stamp = to_iso(self.clock.now())
        currency = self.settings.printify_currency
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            started = conn.execute("SELECT 1 FROM printify_products WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in ("approved", "approved_with_changes") or started is not None:
                return "skipped"  # cancelled or decided meanwhile
            if created_today(conn, self.clock, scope) >= self.settings.printify_products_per_day:
                return "waiting_limit"
            try:
                product = product_from_action(row["action"])
                data = self._image(product)
                if product.currency != currency:  # 0.14.0: its cents would be sent as prices in another currency
                    raise PrintifyError(
                        f"its prices are in {product.currency}, but printify_currency is {currency} now: propose it "
                        "again"
                    )
            except PrintifyError as exc:
                self._start(conn, scope, approval_id, stamp, str(row["title"]), currency)
                connectors.begin(conn, approval_id, stamp)
                return self._failed(conn, approval_id, str(exc))
            self._start(conn, scope, approval_id, stamp, product.title, product.currency)
            connectors.begin(conn, approval_id, stamp)
        # Committed: from here on this product is never made a second time, whatever happens.
        made = None
        try:
            image_id = account.upload(product.image.path.rsplit("/", 1)[-1], data)
            made = account.create(shop, product, image_id)
            self._created(approval_id, product, made)
            prices, short = self._margins(product, made.costs)
            if short:
                note = f"Not published: {'; '.join(short)}. {self._discard(account, shop, made.product_id)}"
                return self._after(
                    approval_id, "failed", made.product_id, None, prices, note, "prices below the margin"
                )
            account.publish(shop, made.product_id)
        except NotSent as exc:
            note = f"Not published: Printify refused it ({exc})"
            if made is not None:
                note += f". {self._discard(account, shop, made.product_id)}"
            return self._after(approval_id, "failed", made.product_id if made else None, None, None, note, str(exc))
        except Exception as exc:  # noqa: BLE001 - Unclear, or anything else: something may exist at Printify
            error = str(exc) if isinstance(exc, PrintifyError) else type(exc).__name__
            if not isinstance(exc, PrintifyError):
                log.exception("Creating the product of request #%d failed", approval_id)
            note = f"It is unclear what Printify made ({error}). Ember won't try again; check your Printify products."
            return self._after(approval_id, "unclear", made.product_id if made else None, None, None, note, error)
        listing_id = None
        try:
            listing_id = account.product(shop, made.product_id).listing_id  # known at once only sometimes
        except PrintifyError:
            listing_id = None
        status = "active" if listing_id else "publishing"
        note = f"Published to your Etsy shop through Printify (product {made.product_id})" + (
            f": {etsy.listing_url(listing_id)}" if listing_id else "; its Etsy listing follows in a few minutes"
        )
        if account.simulated:
            note = f"Published in the dry run's fake Printify account as {made.product_id}; nothing reached Printify."
        return self._after(approval_id, status, made.product_id, listing_id, prices, note, simulated=account.simulated)

    def _created(self, approval_id: int, product: Product, made: Made) -> None:
        """0.14.0: the product's number, kept at once (a crash or a failed delete no longer loses it), and what each
        variant costs to make, kept in the catalog for the next proposal."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE printify_products SET product_id = ? WHERE approval_id = ?", (made.product_id, approval_id)
            )
            if made.costs:
                mode = self.scope().mode
                now = to_iso(self.clock.now())
                keep_costs(conn, mode, product.blueprint_id, product.provider_id, made.costs, now)

    def _margins(self, product: Product, costs: dict[int, int]) -> tuple[list[list[int]], list[str]]:
        """Each variant's price, cost, shipping and what it keeps; and the prices that keep too little (0.14.0: by
        econ's fee model, with VAT on Etsy's fees and on Printify's bill)."""
        shipping = dict(product.shipping)
        prices, short = [], []
        rate, currency = self.settings.etsy_usd_per_eur, product.currency
        for variant, price in product.prices:
            cost = costs.get(variant)
            ship = shipping.get(variant, 0)
            if cost is None:
                short.append(f"Printify didn't say what variant {variant} costs")
                continue
            try:  # 0.14.0: in the currency Printify states, as the proposal's shipping was
                cost = convert(cost, product.billed_in, currency, rate)
            except PrintifyError as exc:
                short.append(f"what variant {variant} costs: {exc}")
                continue
            left = kept(price, cost, ship, currency, rate)
            prices.append([variant, price, cost, ship, left])
            if not keeps(price, cost, ship, currency, rate):
                short.append(
                    f"variant {variant} at {money(price, currency)} keeps {money(left, currency)} after Etsy's fees, "
                    f"making {money(cost, currency)} and shipping {money(ship, currency)} (VAT on both): at least "
                    f"{money(least_price(cost, ship, currency, rate), currency)}"
                )
        return prices, short

    @staticmethod
    def _discard(account: Account, shop: int, product_id: str) -> str:
        """Delete a product that wasn't published (it is invisible in the shop either way): what happened, for the
        note (0.14.0: it said the product was deleted when the delete failed)."""
        try:
            account.delete(shop, product_id)
        except Gone:
            pass
        except PrintifyError as exc:
            log.warning("Deleting the unpublished Printify product %s failed: %s", product_id, exc)
            return (
                f"Ember's code couldn't delete the unpublished product {product_id} at Printify ({exc}): delete it "
                "there"
            )
        return "The unpublished product was deleted at Printify."

    @staticmethod
    def _start(
        conn: sqlite3.Connection, scope: AgentScope, approval_id: int, stamp: str, title: str, currency: str
    ) -> None:
        conn.execute(
            "INSERT INTO printify_products (mode, session, approval_id, title, currency, status, started_at)"
            " VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (scope.mode, scope.session, approval_id, title[:140], currency[:3], stamp),
        )

    def _failed(self, conn: sqlite3.Connection, approval_id: int, reason: str) -> str:
        note = f"Not created: {reason}"
        now = to_iso(self.clock.now())
        conn.execute(
            "UPDATE printify_products SET status = 'failed', finished_at = ?, error = ? WHERE approval_id = ?",
            (now, reason[:500], approval_id),
        )
        connectors.finish(conn, approval_id, "failed", now, note=note)
        self._close(conn, approval_id, "failed", note, None)
        return "failed"

    def _after(
        self,
        approval_id: int,
        status: str,
        product_id: str | None,
        listing_id: int | None,
        prices: list[list[int]] | None,
        note: str,
        error: str | None = None,
        simulated: bool = False,
    ) -> str:
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            conn.execute(
                "UPDATE printify_products SET status = ?, finished_at = ?, product_id = ?, listing_id = ?, prices = ?,"
                " result = ?, error = ? WHERE approval_id = ?",
                (
                    status,
                    now,
                    product_id,
                    listing_id,
                    json.dumps(prices) if prices else None,
                    note[:1000],
                    (error or "")[:500] or None,
                    approval_id,
                ),
            )
            made = status in ("publishing", "active")
            journaled = ("simulated" if simulated else "done") if made else status
            connectors.finish(
                conn,
                approval_id,
                journaled,
                now,
                {"product_id": product_id, "listing_id": listing_id, "prices": prices} if made else None,
                note,
                subject=product_id if made else None,
            )
            link = etsy.listing_url(listing_id) if listing_id else None
            self._close(conn, approval_id, "done" if made else "failed", note, link)
        events.record(self.db, "info" if made else "warning", "printify", f"Request #{approval_id}: {note}"[:300])
        return status

    def _close(self, conn: sqlite3.Connection, approval_id: int, outcome: str, note: str, link: str | None) -> None:
        """Close the approval (unless the owner did meanwhile): the agent hears the result at its next wake."""
        conn.execute(
            "UPDATE approvals SET status = ?, closed_at = ?, closed_by = ?, result_note = ?, result_link = ?,"
            f" version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status IN {APPROVED}",
            (outcome, to_iso(self.clock.now()), CLOSED_BY, note[:2000], link, approval_id),
        )

    def _delete(self, account: Account, scope: AgentScope, shop: int, approval_id: int) -> str:
        """The owner's Undo of a product: Ember's code deletes it at Printify (and Printify its Etsy listing)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in ("approved", "approved_with_changes"):
                return "skipped"
            try:
                product_id = str(json.loads(row["action"])["product_id"])
            except (ValueError, KeyError, TypeError):
                product_id = ""
            connectors.begin(conn, approval_id, to_iso(self.clock.now()), subject=product_id or None)
        gone = False
        try:
            if not product_id:
                raise NotSent("the request names no product")
            account.delete(shop, product_id)
        except Gone:
            gone = True  # deleted at Printify already: what the Undo wanted
        except Exception as exc:  # noqa: BLE001 - reported on the request, never raised
            error = str(exc) if isinstance(exc, PrintifyError) else type(exc).__name__
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
                f"UPDATE printify_products SET status = 'deleted' WHERE {where} AND product_id = ?",
                (*params, product_id),
            )
            note = f"Deleted product {product_id} at Printify, which takes its Etsy listing down too: check your shop"
            if gone:
                note = f"Product {product_id} was gone from Printify already"
            elif account.simulated:
                note = f"Deleted product {product_id} (dry run: the fake Printify account)"
            connectors.finish(
                conn, approval_id, "simulated" if account.simulated else "done", now, {"deleted": product_id}, note
            )
            self._close(conn, approval_id, "done", note, None)
        events.record(self.db, "info", "printify", f"Request #{approval_id}: {note}"[:300])
        return "done"

    def recover(self) -> int:
        """Rows left 'running' by a crash: unclear, never retried."""
        with self.db.connection() as conn:
            left = conn.execute(
                "SELECT approval_id, product_id FROM printify_products WHERE status = 'running'"
            ).fetchall()
        for row in left:
            note = f"It is unclear what Printify made ({INTERRUPTED}). Ember won't try again; check your products."
            self._after(row["approval_id"], "unclear", row["product_id"], None, None, note, INTERRUPTED)
        return len(left)

    # --- the sync ---

    def due(self) -> bool:
        last = self.db.get_meta(meta_key(self.scope().mode, "last_sync_at"))
        return not last or self.clock.now() - from_iso(last) >= timedelta(minutes=SYNC_MINUTES)

    def sync(self, force: bool = False) -> str | None:
        """Read the Etsy listing of each product being published, note the products deleted at Printify, and keep the
        Printify orders of Ember's products, at most every SYNC_MINUTES. Returns an error, or None. 0.14.0: also a
        product whose publish was unclear, or that reached no Etsy listing (see ``_reconcile``)."""
        account = self.account()
        if account is None or not self._lock.acquire(blocking=False):
            return None
        try:
            if not force and not self.due():
                return None
            scope = self.scope()
            now = self.clock.now()
            where, params = scope.where()
            with self.db.connection() as conn:
                mine = conn.execute(
                    f"SELECT approval_id, product_id, status, currency, finished_at, error FROM printify_products"
                    f" WHERE {where} AND product_id IS NOT NULL",
                    params,
                ).fetchall()
            error = None
            try:
                shop = self.shop_id()
                if shop is None:
                    raise PrintifyError("it can't be told which Printify shop sells through Etsy")
                clean_probe(self.db, scope.mode, account, shop)  # 0.14.0: what a failed delete of a probe left
                for row in mine:
                    self._reconcile(account, shop, row, now)
                ours = {str(r["product_id"]): str(r["currency"]) for r in mine}
                lines = [line for line in account.orders(shop) if line.product_id in ours] if ours else []
                with self.db.transaction() as conn:
                    for line in lines:
                        conn.execute(
                            "INSERT INTO printify_orders (mode, session, order_id, product_id, quantity, cost_cents,"
                            " shipping_cents, tax_cents, currency, status, created_at, synced_at) VALUES"
                            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (mode, session, order_id, product_id)"
                            " DO UPDATE SET quantity = excluded.quantity, cost_cents = excluded.cost_cents,"
                            " shipping_cents = excluded.shipping_cents, tax_cents = excluded.tax_cents,"
                            " currency = excluded.currency, status = excluded.status, synced_at = excluded.synced_at",
                            (
                                scope.mode,
                                scope.session,
                                line.order_id,
                                line.product_id,
                                max(0, line.quantity),
                                max(0, line.cost_cents),
                                max(0, line.shipping_cents),
                                max(0, line.tax_cents),
                                line.currency or ours[line.product_id],  # 0.14.0: Printify's, when it states one
                                line.status or "?",
                                line.created_at or to_iso(now),
                                to_iso(now),
                            ),
                        )
            except PrintifyError as exc:
                error = str(exc)[:300]
            self.db.set_meta(meta_key(scope.mode, "last_sync_at"), to_iso(now))
            self.db.set_meta(meta_key(scope.mode, "last_error"), error or "")
            return error
        finally:
            self._lock.release()

    def _reconcile(self, account: Account, shop: int, row: sqlite3.Row, now: Any) -> None:
        """A product the sync reads: publishing or live, and (0.14.0) one whose publish was unclear or that reached no
        Etsy listing (STALE). The listing Printify made makes it active; a product gone at Printify is deleted; one
        with no listing PUBLISH_HOURS after it was sent fails with the reason. Nothing is ever sent again."""
        stale = row["status"] == "failed" and row["error"] == STALE
        if row["status"] not in (*LIVE, "unclear") and not stale:
            return
        try:
            found = account.product(shop, str(row["product_id"]))
        except Gone:
            self._update(row["approval_id"], status="deleted", result=GONE)
            return
        if row["status"] != "active" and found.listing_id:
            self._adopt(row, found.listing_id, account.simulated)
        elif row["status"] in ("publishing", "unclear") and now - from_iso(row["finished_at"]) >= timedelta(
            hours=PUBLISH_HOURS
        ):
            where = "published at Printify" if found.visible else "at Printify, unpublished"
            why = (
                f"{STALE} within {PUBLISH_HOURS} hours: product {row['product_id']} is {where}. Check it there "
                "(Etsy may want a production partner), then publish or delete it"
            )
            self._update(row["approval_id"], status="failed", error=STALE, result=why)
            events.record(self.db, "warning", "printify", f"Request #{row['approval_id']}: {why}"[:300])

    def _adopt(self, row: sqlite3.Row, listing_id: int, simulated: bool) -> None:
        """A product found in the shop: active with its Etsy listing. 0.14.0: one whose publish was unclear (or that
        failed for want of a listing) gets its journal entry now, with the Undo that deletes it."""
        url = etsy.listing_url(listing_id)
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            journaled = conn.execute(
                "SELECT 1 FROM action_journal WHERE approval_id = ? AND status IN ('done', 'simulated')",
                (row["approval_id"],),
            ).fetchone()
            result = None if journaled else f"Published after all (the sync found it): {url}"
            conn.execute(
                "UPDATE printify_products SET status = 'active', listing_id = ?, error = NULL,"
                " result = COALESCE(?, result) WHERE approval_id = ?",
                (listing_id, result, row["approval_id"]),
            )
            if not journaled:
                connectors.begin(conn, row["approval_id"], now, subject=str(row["product_id"]))
                connectors.finish(
                    conn,
                    row["approval_id"],
                    "simulated" if simulated else "done",
                    now,
                    {"product_id": row["product_id"], "listing_id": listing_id},
                    result,
                )
        events.record(self.db, "info", "printify", f"Request #{row['approval_id']}: live in the shop: {url}")

    def _update(self, approval_id: int, **columns: Any) -> None:
        sets = ", ".join(f"{name} = ?" for name in columns)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE printify_products SET {sets} WHERE approval_id = ?",  # noqa: S608 - fixed names
                (*columns.values(), approval_id),
            )
