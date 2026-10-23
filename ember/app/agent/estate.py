"""The estate (0.47.0, vision/generations.md): the world's products a generation hands on, and what becomes of each.

A generation change gives Ember a clean mind in the same world (AgentScope.world). Her products stay in that world:
the Etsy listings, the Printify products, the KDP books. Handing them on with their lines would hand on her hopes for
them too, their history and the money spent on them, and the next generation would defend them from her first plan.
So a product passes, its line never does, and the owner decides for each, from its numbers:

* adopt: it becomes a line of the next mind, with a first test and its date; filed at her first cycle (``file``);
* passive: it stays live (free: Etsy charges at a renewal, Printify when it sells), synced, its sales recorded, but
  it isn't hers to work on; one that sells is offered to the owner to adopt;
* retire: it is taken down before the change.

Ember's code suggests a verdict by a rule (``suggest``), an evaluation proposes one from the same data (``propose``,
from the inheritance), and the owner decides (``decide``). The rows keep what the hand-over read (migration 0103):
the world's session (``session``), the mind that handed each on and the mind it went to (sessions of minds)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..economy.clock import Clock, to_iso
from ..integrations import etsy, etsy_publisher, kdp
from ..integrations.etsy import EtsyError
from . import claims, reach
from .store import AgentScope

KINDS = ("etsy_listing", "printify_product", "kdp_book")
VERDICTS = ("adopt", "passive", "retire")
METRICS = ("views", "favorites", "orders")
FAIR_DAYS = 21  # an Etsy listing has had a fair test after three weeks live (the plan's day-21 keep-or-drop)
KDP_FAIR_DAYS = 45  # a book after six weeks: Amazon's review, its search and the first royalties take weeks
TEST_DAYS = (7, 120)  # an adopted product's first test falls due 7 to 120 days after the owner decides
# The first test the owner is offered for an adopted product: a buyer's first signal in four weeks, a book's first sale
# in six and a half (what passes it is the owner's to set)
DEFAULT_TESTS = {
    "etsy_listing": ("favorites", 1, 28),
    "printify_product": ("favorites", 1, 28),
    "kdp_book": ("orders", 1, 45),
}
KIND_WORDS = {"etsy_listing": "Etsy listing", "printify_product": "Printify product", "kdp_book": "KDP book"}


class EstateError(ValueError):
    def __init__(self, field: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


@dataclass(frozen=True)
class Product:
    """One of the world's products as the hand-over reads it now."""

    kind: str
    ref: str
    listing_id: int | None
    approval_id: int
    title: str
    link: str | None
    numbers: dict[str, Any]

    def suggestion(self) -> tuple[str, str]:
        return suggest(self.kind, self.numbers)


def _days(since: str | None, clock: Clock) -> int | None:
    """Whole days from a timestamp to today, the owner's days both (clock.local_date)."""
    return None if not since else max(0, (clock.today() - date.fromisoformat(clock.local_date(since))).days)


def _day(stamp: str | None, clock: Clock) -> str:
    return clock.local_date(stamp) if stamp else ""


def _rule(texts: list[str]) -> str:
    """The first rule a product's public text breaks (claims.refusal: a person named its maker, legal certainty, "no
    refunds"), in short, or ""."""
    said = claims.refusal(texts)
    return " ".join(said.split())[:200] if said else ""


def _listing_texts(conn: sqlite3.Connection, scope: AgentScope, listing_id: int, approval: sqlite3.Row) -> list[str]:
    try:
        listing = etsy_publisher.current_listing(conn, scope, listing_id)
    except EtsyError:
        listing = None
    if listing is not None:
        return claims.texts(listing.to_action())
    return claims.texts(json.loads(approval["action"] or "null"))


def inventory(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> list[Product]:
    """The world's products now, whoever made them: Ember's Etsy listings (live or not, but not removed at Etsy), the
    Printify products she made (published or being published) and her KDP books the owner published."""
    sold = etsy_publisher.sold_counts(conn, scope)
    reached = reach.by_listing(conn, scope)
    found = []
    world, shop = scope.world_where("l")
    for r in conn.execute(
        f"SELECT l.*, a.action FROM etsy_listings l JOIN approvals a ON a.id = l.approval_id WHERE {world}"
        " AND l.status = 'active' AND l.listing_id IS NOT NULL AND COALESCE(l.state, '') <> 'removed' ORDER BY l.id",
        shop,
    ).fetchall():
        listing = int(r["listing_id"])
        numbers = {
            "state": etsy_publisher.etsy_state(r),
            "views": int(r["views"] or 0),
            "favorites": int(r["favorites"] or 0),
            "sold": sold.get(listing, 0),
            "reach": reached.get(listing, 0),
            "since": _day(r["started_at"], clock),
            "days": _days(r["started_at"], clock),
            "rule": _rule(_listing_texts(conn, scope, listing, r)),
        }
        found.append(
            Product(
                "etsy_listing",
                str(listing),
                listing,
                int(r["approval_id"]),
                r["title"] or f"#{listing}",
                etsy.listing_url(listing),
                numbers,
            )
        )
    world, shop = scope.world_where("p")
    for r in conn.execute(
        f"SELECT p.*, a.action FROM printify_products p JOIN approvals a ON a.id = p.approval_id WHERE {world}"
        " AND p.status IN ('publishing', 'active') ORDER BY p.id",
        shop,
    ).fetchall():
        listing = int(r["listing_id"]) if r["listing_id"] is not None else None
        numbers = {
            "state": str(r["state"] or r["status"]),
            "views": int(r["views"] or 0),
            "favorites": int(r["favorites"] or 0),
            "sold": sold.get(listing, 0) if listing else 0,
            "reach": reached.get(listing, 0) if listing else 0,
            "since": _day(r["started_at"], clock),
            "days": _days(r["started_at"], clock),
            "rule": _rule(claims.texts(json.loads(r["action"] or "null"))),
        }
        ref = str(r["product_id"] or f"request-{r['approval_id']}")
        found.append(
            Product(
                "printify_product",
                ref,
                listing,
                int(r["approval_id"]),
                r["title"] or ref,
                etsy.listing_url(listing) if listing else None,
                numbers,
            )
        )
    minds, params = scope.minds_where()
    for r in conn.execute(
        f"SELECT id, project_id, action, closed_at, result_link FROM approvals WHERE {minds} AND executor = ?"
        " AND status = 'done' ORDER BY id",
        (*params, kdp.EXECUTOR),
    ).fetchall():
        try:
            book = kdp.book_from_action(r["action"])
        except kdp.KdpError:
            continue
        royalties = (
            conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type = 'revenue' AND project_id = ?"
                " AND simulated = ?",
                (r["project_id"], 1 if scope.simulated else 0),
            ).fetchone()
            if r["project_id"]
            else (0, 0)
        )
        numbers = {
            "format": book.format,
            "since": _day(r["closed_at"], clock),
            "days": _days(r["closed_at"], clock),
            "linked": bool(r["result_link"]),
            "royalties": int(royalties[0]),
            "royalties_usd": round(int(royalties[1]) / 1_000_000, 2),
            "rule": _rule(claims.texts(json.loads(r["action"] or "null"))),
        }
        found.append(Product("kdp_book", str(r["id"]), None, int(r["id"]), book.title, r["result_link"], numbers))
    return found


def suggest(kind: str, n: dict[str, Any]) -> tuple[str, str]:
    """The verdict Ember's code's rule suggests for a product, and why (at most 300 characters): adopt what is too new
    to judge or shows a buyer's signal, passive what had a fair test without one, retire what breaks a rule or is
    down."""
    if n.get("gone"):
        return "retire", "it is gone from the shop: nothing to hand on"
    if n.get("rule"):
        return (
            "retire",
            f"its public text breaks a rule ({n['rule'][:180]}): correct it before the change, or retire it",
        )
    if kind == "kdp_book":
        if n.get("royalties"):
            return "adopt", f"a buyer's signal: {n['royalties']} royalty entries recorded"
        days = n.get("days")
        if days is None or days < KDP_FAIR_DAYS:
            ago = days if days is not None else "?"
            return "adopt", f"too new to judge: published {ago} days ago (a fair test is {KDP_FAIR_DAYS})"
        return "passive", f"a fair test without a sale recorded: published {days} days ago"
    if n.get("state") not in (etsy.LIVE_STATE, "publishing"):
        return "retire", f"not live at Etsy ({n.get('state')}): it stays down"
    if n.get("sold"):
        return "adopt", f"a buyer's signal: {n['sold']} sold"
    if n.get("favorites"):
        return "adopt", f"a buyer's signal: {n['favorites']} favorites"
    days = n.get("days")
    if days is None or days < FAIR_DAYS:
        return "adopt", f"too new to judge: {days if days is not None else '?'} days live (a fair test is {FAIR_DAYS})"
    return (
        "passive",
        f"a fair test without a buyer's signal: {days} days live, {n.get('views', 0)} views, {n.get('reach', 0)} reach"
        " done for it, no favorite or sale",
    )


# --- the hand-over's rows ---


def _where(scope: AgentScope) -> tuple[str, tuple[Any, ...]]:
    return "mode = ? AND session = ?", (scope.mode, scope.world_session)


def handover(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The products this mind hands on, not handed on yet, in the order the inventory reads them."""
    where, params = _where(scope)
    return conn.execute(
        f"SELECT * FROM estate WHERE {where} AND from_session = ? AND to_session IS NULL ORDER BY id",
        (*params, scope.session),
    ).fetchall()


def received(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The products this mind received from the one before (none in a first generation)."""
    where, params = _where(scope)
    return conn.execute(
        f"SELECT * FROM estate WHERE {where} AND to_session = ? ORDER BY id", (*params, scope.session)
    ).fetchall()


def refresh(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> list[sqlite3.Row]:
    """The hand-over's rows as the world is now: a row for each product (its verdict open), each one's numbers and
    suggestion read again until the change; a product gone from the shop since keeps its row, marked gone."""
    now = to_iso(clock.now())
    where, params = _where(scope)
    known = {(r["kind"], r["ref"]): r for r in handover(conn, scope)}
    seen = set()
    for product in inventory(conn, scope, clock):
        verdict, why = product.suggestion()
        seen.add((product.kind, product.ref))
        row = known.get((product.kind, product.ref))
        numbers = json.dumps(product.numbers, sort_keys=True)
        if row is None:
            conn.execute(
                "INSERT INTO estate (mode, session, from_session, kind, ref, listing_id, approval_id, title, link,"
                " numbers, suggested, suggested_why, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *params,
                    scope.session,
                    product.kind,
                    product.ref,
                    product.listing_id,
                    product.approval_id,
                    product.title[:200],
                    product.link,
                    numbers,
                    verdict,
                    why[:300],
                    now,
                    now,
                ),
            )
        elif (row["numbers"], row["suggested"], row["suggested_why"], row["title"], row["link"]) != (
            numbers,
            verdict,
            why[:300],
            product.title[:200],
            product.link,
        ):
            conn.execute(
                "UPDATE estate SET numbers = ?, suggested = ?, suggested_why = ?, title = ?, link = ?, listing_id = ?,"
                " updated_at = ? WHERE id = ?",
                (numbers, verdict, why[:300], product.title[:200], product.link, product.listing_id, now, row["id"]),
            )
    for key, row in known.items():
        if key in seen or json.loads(row["numbers"]).get("gone"):
            continue
        numbers = {**json.loads(row["numbers"]), "gone": True}
        verdict, why = suggest(row["kind"], numbers)
        conn.execute(
            "UPDATE estate SET numbers = ?, suggested = ?, suggested_why = ?, updated_at = ? WHERE id = ?",
            (json.dumps(numbers, sort_keys=True), verdict, why, now, row["id"]),
        )
    return handover(conn, scope)


def _row(conn: sqlite3.Connection, scope: AgentScope, kind: str, ref: str) -> sqlite3.Row:
    """The product's row in this mind's hand-over, else in what it received (a verdict changes there too)."""
    where, params = _where(scope)
    row = conn.execute(
        f"SELECT * FROM estate WHERE {where} AND kind = ? AND ref = ?"
        " AND ((from_session = ? AND to_session IS NULL) OR to_session = ?) ORDER BY id DESC LIMIT 1",
        (*params, kind, ref, scope.session, scope.session),
    ).fetchone()
    if row is None:
        raise EstateError("ref", f"there is no {KIND_WORDS.get(kind, kind)} {ref} in the estate", 404)
    return row


def test_of(kind: str, body: dict[str, Any] | None, today: date) -> tuple[str, int, str]:
    """An adopted product's first test: its metric, target and the day it falls due (the defaults: DEFAULT_TESTS)."""
    metric, target, days = DEFAULT_TESTS[kind]
    body = body or {}
    metric = body.get("metric", metric)
    target = body.get("target", target)
    due = body.get("due") or (today + timedelta(days=days)).isoformat()
    if metric not in METRICS or (kind == "kdp_book" and metric != "orders"):
        raise EstateError(
            "metric", "a KDP book's test is its orders" if kind == "kdp_book" else "views, favorites or orders"
        )
    if not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= 100_000:
        raise EstateError("target", "a whole number from 1 to 100000")
    try:
        day = date.fromisoformat(str(due))
    except ValueError:
        raise EstateError("due", "a day as YYYY-MM-DD") from None
    low, high = TEST_DAYS
    if not low <= (day - today).days <= high:
        raise EstateError("due", f"{low} to {high} days from today")
    return str(metric), int(target), day.isoformat()


def decide(
    conn: sqlite3.Connection,
    scope: AgentScope,
    clock: Clock,
    kind: str,
    ref: str,
    verdict: str,
    by: str,
    test: dict[str, Any] | None = None,
) -> sqlite3.Row:
    """The owner's verdict on a product (``test``: an adopted one's first test, else the defaults). A verdict changes
    until the product's line is made; once made, the line is the owner's to keep or drop on the Plan tab."""
    if verdict not in VERDICTS:
        raise EstateError("verdict", "adopt, passive or retire")
    row = _row(conn, scope, kind, ref)
    if row["project_id"] is not None and verdict != "adopt":
        raise EstateError("verdict", f"it is line #{row['project_id']} now: keep or drop it on the Plan tab", 409)
    metric = target = due = None
    if verdict == "adopt":
        metric, target, due = test_of(kind, test, clock.today())
    elif test:
        raise EstateError("test", "only an adopted product has a first test")
    conn.execute(
        "UPDATE estate SET verdict = ?, decided_by = ?, decided_at = ?, test_metric = ?, test_target = ?, test_due = ?,"
        " test_result = NULL, updated_at = ? WHERE id = ?",
        (verdict, by[:60], to_iso(clock.now()), metric, target, due, to_iso(clock.now()), row["id"]),
    )
    return conn.execute("SELECT * FROM estate WHERE id = ?", (row["id"],)).fetchone()


def propose(
    conn: sqlite3.Connection, scope: AgentScope, clock: Clock, kind: str, ref: str, verdict: str, why: str, by: str
) -> None:
    """An evaluation's proposal for a product of this mind's hand-over (the inheritance), shown beside the rule's."""
    if verdict not in VERDICTS:
        raise EstateError("verdict", "adopt, passive or retire")
    why = " ".join(str(why or "").split())
    if not 1 <= len(why) <= 600:
        raise EstateError("why", "say why, in at most 600 characters")
    row = _row(conn, scope, kind, ref)
    if row["to_session"] is not None:
        raise EstateError("ref", "it was handed on already", 409)
    conn.execute(
        "UPDATE estate SET proposed = ?, proposed_why = ?, proposed_by = ?, updated_at = ? WHERE id = ?",
        (verdict, why, by[:60], to_iso(clock.now()), row["id"]),
    )


def to_json(row: sqlite3.Row) -> dict[str, Any]:
    keys = (
        "id",
        "kind",
        "ref",
        "listing_id",
        "approval_id",
        "title",
        "link",
        "suggested",
        "suggested_why",
        "proposed",
        "proposed_why",
        "proposed_by",
        "verdict",
        "decided_by",
        "decided_at",
        "test_metric",
        "test_target",
        "test_due",
        "test_result",
        "project_id",
        "from_session",
        "to_session",
    )
    data = {k: row[k] for k in keys}
    data["numbers"] = json.loads(row["numbers"])
    data["default_test"] = dict(zip(("metric", "target", "days"), DEFAULT_TESTS[row["kind"]], strict=True))
    return data


# --- in the mind that received them ---


def not_hers(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> sqlite3.Row | None:
    """0.47.0: the estate's row of a listing this mind received but doesn't work on (passive, or retired and still
    up), else None: a listing of hers, or of no hand-over."""
    where, params = _where(scope)
    return conn.execute(
        f"SELECT * FROM estate WHERE {where} AND to_session = ? AND listing_id = ? AND verdict <> 'adopt'"
        " ORDER BY id DESC LIMIT 1",
        (*params, scope.session, listing_id),
    ).fetchone()


def not_hers_ids(conn: sqlite3.Connection, scope: AgentScope) -> set[int]:
    """The listings of ``not_hers``, all of them."""
    where, params = _where(scope)
    return {
        int(r[0])
        for r in conn.execute(
            f"SELECT listing_id FROM estate WHERE {where} AND to_session = ? AND verdict <> 'adopt'"
            " AND listing_id IS NOT NULL",
            (*params, scope.session),
        )
    }


def progress(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> int:
    """What an adopted product gained in its test's metric since the hand-over: a listing's views or favorites as the
    sync last read them, its orders that count; a book's royalty entries the owner recorded, for its old line or its
    new one."""
    then = json.loads(row["numbers"])
    metric = row["test_metric"]
    if row["kind"] == "kdp_book":
        old = conn.execute("SELECT project_id FROM approvals WHERE id = ?", (row["approval_id"],)).fetchone()
        lines = [p for p in ((old["project_id"] if old else None), row["project_id"]) if p is not None]
        marks = ", ".join("?" for _ in lines) or "NULL"
        now = conn.execute(
            f"SELECT COUNT(*) FROM ledger WHERE type = 'revenue' AND simulated = ? AND project_id IN ({marks})",
            (1 if scope.simulated else 0, *lines),
        ).fetchone()[0]
        return max(0, int(now) - int(then.get("royalties", 0)))
    if row["listing_id"] is None:
        return 0
    if metric == "orders":
        sold = etsy_publisher.sold_counts(conn, scope).get(int(row["listing_id"]), 0)
        return max(0, sold - int(then.get("sold", 0)))
    table = "etsy_listings" if row["kind"] == "etsy_listing" else "printify_products"
    world, params = scope.world_where()
    found = conn.execute(
        f"SELECT views, favorites FROM {table} WHERE {world} AND listing_id = ? ORDER BY id DESC LIMIT 1",
        (*params, row["listing_id"]),
    ).fetchone()
    have = int((found[metric] if found is not None else 0) or 0)
    return max(0, have - int(then.get(metric, 0)))


def shop_line(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """ETSY SHOP's line on the listings her predecessor handed on that aren't hers ("" when there are none): how many,
    what they did since the hand-over, and that they stay as they are."""
    rows = [r for r in received(conn, scope) if r["verdict"] != "adopt" and r["listing_id"] is not None]
    if not rows:
        return ""
    ids = {int(r["listing_id"]) for r in rows}
    sold = etsy_publisher.sold_counts(conn, scope)
    world, params = scope.world_where()
    marks = ", ".join("?" for _ in ids)
    views = 0
    for table in ("etsy_listings", "printify_products"):
        views += int(
            conn.execute(
                f"SELECT COALESCE(SUM(views), 0) FROM {table} WHERE {world} AND listing_id IN ({marks})",
                (*params, *ids),
            ).fetchone()[0]
        )
    before = sum(int(json.loads(r["numbers"]).get("views", 0)) for r in rows)
    sales = sum(sold.get(i, 0) for i in ids) - sum(int(json.loads(r["numbers"]).get("sold", 0)) for r in rows)
    shown = ", ".join(f"#{i}" for i in sorted(ids)[:8]) + (", ..." if len(ids) > 8 else "")
    gained = max(0, views - before)
    return (
        f"Handed on by your predecessor and not yours to work on: {len(ids)} listing{'s' if len(ids) != 1 else ''}"
        f" ({shown}), {gained} views and"
        f" {max(0, sales)} sold since the hand-over. They stay as they are; if one sells, your owner can give it to"
        " you."
    )


def book_lines(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """KDP's lines on the books her predecessor handed on ("" when there are none): a book adopted is hers, with its
    line and first test; one passive stays as it is."""
    said = []
    for r in received(conn, scope):
        if r["kind"] != "kdp_book":
            continue
        name = f"#{r['ref']} {json.dumps(r['title'][:80], ensure_ascii=False)}"
        if r["verdict"] == "adopt":
            line = f"your line #{r['project_id']}" if r["project_id"] else "your line from your next cycle"
            test = f"{r['test_target']} {r['test_metric']} by {r['test_due']}"
            result = f", {r['test_result']}" if r["test_result"] else ""
            link = f" at {r['link']}" if r["link"] else " (no link at Amazon yet)"
            said.append(f"{name}{link}: {line}, first test {test}{result}")
        else:
            said.append(f"{name}: passive, not yours to work on")
    return ("Books your predecessor handed on: " + "; ".join(said) + ".") if said else ""


def generation(scope: AgentScope) -> int:
    """Which generation of this world the mind is: 1 for the first."""
    return scope.session - scope.world_session + 1


def refusal(row: sqlite3.Row) -> str:
    """Why a tool won't work on a listing the mind received but doesn't work on."""
    verdict = "passive" if row["verdict"] == "passive" else "retired"
    return (
        f"listing #{row['listing_id']} came from your predecessor and is {verdict}: it stays as it is, and it isn't"
        " yours to work on. If it sells, your owner can give it to you."
    )
