"""Demand notes (0.12.0): what shows a product line will sell, before its first listing.

Ember made generic templates without checking that anyone searched for them ("no more generic templates without a
demand note"). Now a project's first Etsy listing needs a demand note from the last DAYS days (the ``demand_note``
tool): the keywords buyers search, the demand the agent found and its source (a page from its research results, or a
document of the owner's library, such as the keyword export the owner adds each week) and, with the owner's Etsy
market probe on (``etsy_market_probe``), Etsy's numbers for the keywords: how many active listings match and the
quartiles of the first ones' prices. The probe keeps only these aggregates, never another seller's listing.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from ..economy.clock import from_iso, to_iso
from ..integrations import etsy
from . import evidence
from .store import AgentScope

DAYS = 14  # how old a demand note may be when its product line's first listing is proposed
# A listing request that makes a project's product line an existing one (a rejected, withdrawn, expired or failed one
# doesn't: the next is still its first).
LISTED = ("pending", "approved", "approved_with_changes", "done")
_LIBRARY = re.compile(r"^library #(\d+)$", re.IGNORECASE)


def source_problem(conn: sqlite3.Connection, scope: AgentScope, source: str) -> str:
    """Why ``source`` can't back a demand note ("" when it can): it must be a page from the research results (as the
    evidence store checks one) or a document of the owner's library ('library #12')."""
    found = _LIBRARY.match(source)
    if found:
        where, params = scope.where()
        row = conn.execute(
            f"SELECT 1 FROM library_documents WHERE id = ? AND {where}", (int(found[1]), *params)
        ).fetchone()
        return "" if row else f"there is no document #{found[1]} in your owner's library"
    if source.startswith(("https://", "http://")) and evidence.grade(conn, scope, source) != "unchecked":
        return ""
    return (
        "source must be a page from your research results (its address) or a document of your owner's library "
        "('library #12')"
    )


def add(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int | None,
    project_id: int,
    keywords: str,
    demand: str | None,
    source: str | None,
    found: etsy.Market | None,
    now: str,
) -> int:
    """Save a demand note (with the market probe's aggregates, when it ran); returns its number."""
    priced = found is not None and found.sampled > 0
    cursor = conn.execute(
        "INSERT INTO demand_notes (mode, session, cycle_id, project_id, created_at, keywords, demand, source, listings,"
        " sampled, currency, low, median, high) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            cycle_id,
            project_id,
            now,
            keywords,
            demand,
            source,
            found.listings if found is not None else None,
            found.sampled if found is not None else None,
            found.currency if priced else None,
            found.low if priced else None,
            found.median if priced else None,
            found.high if priced else None,
        ),
    )
    return int(cursor.lastrowid)


def recent(conn: sqlite3.Connection, project_id: int, now: str) -> sqlite3.Row | None:
    """The project's newest demand note from the last DAYS days, or None."""
    since = to_iso(from_iso(now) - timedelta(days=DAYS))
    return conn.execute(
        "SELECT * FROM demand_notes WHERE project_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
        (project_id, since),
    ).fetchone()


def listed(conn: sqlite3.Connection, project_id: int) -> bool:
    """Whether the project's product line has a listing already: a listing request for it (its own, or made in a
    cycle focused on it, as the revenue form attributes a sale) that is waiting, approved or carried out (0.13.0: a
    Printify product's request too: it becomes a listing in the shop)."""
    marks = ", ".join("?" for _ in LISTED)
    row = conn.execute(
        "SELECT 1 FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
        " WHERE a.executor IN ('etsy_listing', 'printify_product')"
        f" AND COALESCE(a.project_id, y.project_id) = ? AND a.status IN ({marks}) LIMIT 1",
        (project_id, *LISTED),
    ).fetchone()
    return row is not None


def probe_text(row: sqlite3.Row) -> str:
    """A demand note's market probe in words ("" without one)."""
    if row["listings"] is None:
        return ""
    return etsy.Market(
        int(row["listings"]),
        int(row["sampled"] or 0),
        str(row["currency"] or ""),
        float(row["low"] or 0),
        float(row["median"] or 0),
        float(row["high"] or 0),
    ).text()
