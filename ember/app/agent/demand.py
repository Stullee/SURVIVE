"""Demand notes (0.12.0): what shows a product line will sell, before its first listing.

Ember made generic templates without checking that anyone searched for them ("no more generic templates without a
demand note"). Now a project's first Etsy listing needs a demand note from the last DAYS days (the ``demand_note``
tool): the keywords buyers search, the demand the agent found and its source (a page from its research results, or a
document of the owner's library, such as the keyword export the owner adds each week) and, with the owner's Etsy
market probe on (``etsy_market_probe``), Etsy's numbers for the keywords: how many active listings match and the
quartiles of the first ones' prices. The probe keeps only these aggregates, never another seller's listing.

0.14.0: any library document was a source, even a removed one, and no number was needed (live, a general Etsy guide
"backed" a product line). Now a library document counts when it is linked to the product line or its venture, or is
a keyword or market export the owner uploaded as a table (.csv, .tsv), and the demand cites a number found in it; a
page must be independent (not a vendor's or an affiliate's), and the demand must give a number.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from ..economy.clock import from_iso, to_iso
from ..integrations import etsy
from . import evidence, library
from .store import AgentScope

DAYS = 14  # how old a demand note may be when its product line's first listing is proposed
# A listing request that makes a project's product line an existing one (a rejected, withdrawn, expired or failed one
# doesn't: the next is still its first).
LISTED = ("pending", "approved", "approved_with_changes", "done")
_LIBRARY = re.compile(r"^library #(\d+)$", re.IGNORECASE)
_NUMBER = re.compile(r"\d(?:[\d.,]*\d)?")
_YEAR = re.compile(r"^(?:19|20)\d\d$")
# what a cited number counts: searches, sales, orders, reviews, buyers, listings (a word near it, in the note)
_DEMAND_WORDS = re.compile(
    r"\b(?:search\w*|sales|sold|sell\w*|orders?|reviews?|buyers?|customers?|downloads?|favou?rites|purchases|demand"
    r"|listings|results|month\w*|suchanfragen|suchvolumen|gesucht|verkäufe|verkauft|bestellungen|bewertungen|käufer"
    r"|kunden|nachfrage|treffer|angebote|monat\w*)\b",
    re.IGNORECASE,
)
EXPORTS = (".csv", ".tsv")  # a keyword or market export the owner uploaded: a table


def _digits(number: str) -> str:
    """A number as digits only (1,200 and 1.200 are both 1200), "" for one digit, a year or a decimal's cents: 4.99
    is 4, not 499."""
    whole = re.sub(r"[.,]\d{1,2}$", "", number)
    digits = re.sub(r"\D", "", whole)
    return "" if len(digits) < 2 or _YEAR.match(number) else digits


def numbers(text: str) -> set[str]:
    """The numbers of two digits or more in ``text``, as digits only (not years, nor a decimal's cents)."""
    return {digits for digits in (_digits(n) for n in _NUMBER.findall(text)) if digits}


def cited(said: str, text: str) -> bool:
    """0.14.0: whether the demand ``said`` cites a number of ``text`` with a demand word near it ("850 searches a
    month"): a year or "13 tags" in a general guide doesn't show demand."""
    found = numbers(text)
    for match in _NUMBER.finditer(said):
        near = said[max(0, match.start() - 40) : match.end() + 40]
        if _digits(match[0]) in found and _DEMAND_WORDS.search(near):
            return True
    return False


def source_problem(conn: sqlite3.Connection, scope: AgentScope, source: str, project_id: int, said: str | None) -> str:
    """Why ``source`` can't back a demand note for the project with the demand ``said`` ("" when it can): it must be
    an independent page from the research results (as the evidence store grades one), or a document of the owner's
    library ('library #12') that is linked to the product line or its venture, or is an export the owner uploaded;
    0.14.0: the demand cites a number found in the document, or gives one from the page."""
    found = _LIBRARY.match(source)
    if found:
        number = int(found[1])
        row = library.get(conn, scope, number)
        if row is None or row["removed_at"] is not None:
            return f"there is no document #{number} in your owner's library"
        project = conn.execute("SELECT venture_id FROM projects WHERE id = ?", (project_id,)).fetchone()
        venture_id = project["venture_id"] if project is not None else None
        linked = row["project_id"] == project_id or (venture_id is not None and row["venture_id"] == venture_id)
        if not linked and not str(row["file_name"] or "").lower().endswith(EXPORTS):
            return (
                f"library #{number} isn't linked to project #{project_id} or its venture, nor a keyword or market "
                "export your owner uploaded (.csv or .tsv): it can't show this product line's demand"
            )
        if not cited(said or "", library.full_text(conn, number)):
            return (
                f"cite a number from library #{number} in demand, with what it counts (searches, sales, orders): "
                "none of yours is in it"
            )
        return ""
    if source.startswith(("https://", "http://")):
        graded = evidence.grade(conn, scope, source)
        if graded == "marketing":
            return "a vendor's or an affiliate's page doesn't show demand: cite an independent page from your research"
        if graded == "independent":
            if not numbers(said or ""):
                return "give a number from the page in demand (searches, sales, competitors' prices)"
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
