"""Reach and the funnel (0.18.0): how far a product line got toward buyers, and what was done to bring them.

A listing with no views said nothing about the product, yet the daily review read "0 views" as "no demand" and cut
spending, and the listing test parked product lines on day 14 for lack of visitors while nothing had been done to
bring any. Now Ember's code works out, for each project with live listings:

* its funnel from Etsy's numbers at the last sync: views, favorites and orders of its listings, and the stage where it
  is stuck (``stage``): not seen (a reach problem: market it), seen but not liked (the listing's appeal: photos,
  title, price), liked but not bought (price or trust), or selling;
* its reach: what was done to bring buyers to its listings, from Ember's records: blog posts published on the owner's
  site that recommend one of them, pins live at Pinterest that link one, and changes of them carried out at Etsy
  (titles, tags, photos).

The daily review reads both (``review_text``) and names each project's bottleneck; the listing test parks a product
line for too few views only once it had ENOUGH reach (gates.py), and owes a push to bring buyers otherwise.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from ..integrations import etsy_publisher, site_publisher
from . import metrics
from .store import AgentScope

ENOUGH = 3  # reach actions for a product line before too few views count against the product itself
SEEN_VIEWS = 10  # fewer views than this: not seen yet (the listing test's day-7 bar)
_LISTING = re.compile(r"etsy\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?listing/(\d+)", re.IGNORECASE)

STAGES = {
    "not_listed": "nothing live yet",
    "not_seen": "not seen: a reach problem, bring buyers to it",
    "not_liked": "seen but not liked: the listing's appeal (photos, title, price)",
    "not_bought": "liked but not bought: price or trust",
    "selling": "selling: scale it",
}


@dataclass
class Funnel:
    listings: list[int] = field(default_factory=list)  # its live listings' Etsy numbers
    views: int = 0
    favorites: int = 0
    orders: int = 0
    posts: int = 0  # blog posts published that recommend one of its listings
    pins: int = 0  # pins live that link one of its listings
    edits: int = 0  # changes of its listings carried out at Etsy

    @property
    def reach(self) -> int:
        return self.posts + self.pins + self.edits

    @property
    def stage(self) -> str:
        if not self.listings:
            return "not_listed"
        if self.orders:
            return "selling"
        if self.views < SEEN_VIEWS:
            return "not_seen"
        return "not_liked" if not self.favorites else "not_bought"

    def short(self) -> str:
        """The funnel in a few words, for the plan's OPEN PROJECTS."""
        if not self.listings:
            return "funnel: nothing live yet"
        where = STAGES[self.stage].split(":")[0]
        return (
            f"funnel: {self.views} views, {self.favorites} favorites, {self.orders} orders ({where}); reach done:"
            f" {self.reach}"
        )

    def text(self) -> str:
        if not self.listings:
            return f"funnel: {STAGES['not_listed']}"
        reach = f"{self.posts} blog post(s), {self.pins} pin(s), {self.edits} listing edit(s)"
        enough = "" if self.reach >= ENOUGH else f": less than the {ENOUGH} a fair test needs"
        return (
            f"funnel: {len(self.listings)} live listing(s), {self.views} views, {self.favorites} favorites,"
            f" {self.orders} orders: {STAGES[self.stage]} · reach done: {reach}{enough}"
        )


def listing_ids(text: str) -> set[int]:
    """The Etsy listings a text links (https://www.etsy.com/listing/123..., also with a language path)."""
    return {int(n) for n in _LISTING.findall(text or "")}


def funnels(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, Funnel]:
    """Each project's funnel and reach, by number (projects without a live listing are left out)."""
    rows = etsy_publisher.live_rows(metrics.listings(conn, scope, None, None), {})
    sold = etsy_publisher.sold_counts(conn, scope)
    found: dict[int, Funnel] = {}
    owner: dict[int, int] = {}
    for r in rows:
        if not r["for_project"]:
            continue
        f = found.setdefault(int(r["for_project"]), Funnel())
        listing = int(r["listing_id"])
        f.listings.append(listing)
        f.views += int(r["views"] or 0)
        f.favorites += int(r["favorites"] or 0)
        f.orders += sold.get(listing, 0)
        owner[listing] = int(r["for_project"])
    if not owner:
        return found
    for project_id in _linked(_posts(conn, scope), owner):
        found[project_id].posts += 1
    for project_id in _linked(_pins(conn, scope), owner):
        found[project_id].pins += 1
    where, params = scope.where()
    for e in conn.execute(
        f"SELECT listing_id FROM etsy_edits WHERE {where} AND status IN ('done', 'partial')", params
    ).fetchall():
        project_id = owner.get(int(e["listing_id"]))
        if project_id is not None:
            found[project_id].edits += 1
    return found


def _linked(texts: list[str], owner: dict[int, int]) -> list[int]:
    """For each text, the projects whose listings it links (each project once a text)."""
    projects = []
    for text in texts:
        projects += sorted({owner[n] for n in listing_ids(text) if n in owner})
    return projects


def _posts(conn: sqlite3.Connection, scope: AgentScope) -> list[str]:
    """The requests of the blog posts online on the owner's site (their text names the product they recommend)."""
    where, params = scope.where("b")
    return [
        str(r["payload"] or "")
        for r in conn.execute(
            f"SELECT a.payload FROM blog_posts b JOIN approvals a ON a.id = b.approval_id WHERE {where}"
            " AND a.executor = ?",
            (*params, site_publisher.POST),
        ).fetchall()
    ]


def _pins(conn: sqlite3.Connection, scope: AgentScope) -> list[str]:
    where, params = scope.where()
    return [
        str(r["link"])
        for r in conn.execute(f"SELECT link FROM pinterest_pins WHERE {where} AND status = 'active'", params)
    ]


def review_text(funnel: Funnel | None) -> str:
    """A project's funnel line for the daily review's PROJECTS, or "" for one without a live listing."""
    return funnel.text() if funnel is not None and funnel.listings else ""


def research_text(conn: sqlite3.Connection, scope: AgentScope, project_id: int, venture_id: int | None) -> str:
    """0.18.0: how deep a project's research went, for the daily review: its newest demand note (with the market
    probe's prices if any), its venture's independent evidence, and its newest quality check (quality.py)."""
    where, params = scope.where()
    note = conn.execute(
        f"SELECT * FROM demand_notes WHERE {where} AND project_id = ? ORDER BY id DESC LIMIT 1", (*params, project_id)
    ).fetchone()
    if note is None:
        demand = "no demand note"
    else:
        prices = f", market prices {note['low']:g}-{note['high']:g} {note['currency']}" if note["low"] else ""
        demand = f"demand note of {str(note['created_at'])[:10]}{prices}"
    independent = 0
    if venture_id:
        independent = conn.execute(
            f"SELECT COUNT(*) FROM evidence WHERE {where} AND venture_id = ? AND source = 'independent'",
            (*params, venture_id),
        ).fetchone()[0]
    check = conn.execute(
        f"SELECT * FROM quality_checks WHERE {where} AND project_id = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
        (*params, project_id),
    ).fetchone()
    quality = (
        f"quality {check['score']}/10, {check['verdict']} ({str(check['created_at'])[:10]}): {check['fixes'][:160]}"
        if check
        else "no quality check yet"
    )
    return f"research: {demand}; {independent} independent claim(s) for its venture · {quality}"
