"""Evidence with source quality (0.12.0).

A venture's case was prose: a number and a link counted the same whether the page was a search result, a vendor
selling the very tool it praised, or nothing the agent ever read. Now the agent saves each claim as evidence (the
``evidence`` tool: a metric, a low and a high value, a unit, a region and the page), and Ember's code grades the page:

* unchecked: not a page the research tool returned (``research_sources``, recorded as research runs): the agent's
  word only;
* marketing: a vendor's page (it sells what it describes: Shopify, Printful, Etsy research tools, course platforms)
  or an affiliate's (a link that pays whoever sends a buyer);
* independent: any other page from the research results.

The grade is final (a claim never changes), the venture's FOCUS shows its evidence by grade, and the Ventures tab
lists it.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from .store import AgentScope

GRADES = ("independent", "marketing", "unchecked")
# Vendors: pages about a market by someone selling into it (tools, platforms, print on demand, dropshipping, courses).
MARKETING_DOMAINS = (
    "shopify.com",
    "oberlo.com",
    "printful.com",
    "printify.com",
    "gelato.com",
    "gooten.com",
    "spocket.co",
    "zendrop.com",
    "dsers.com",
    "cjdropshipping.com",
    "alibaba.com",
    "aliexpress.com",
    "sellfy.com",
    "gumroad.com",
    "podia.com",
    "payhip.com",
    "teachable.com",
    "thinkific.com",
    "kajabi.com",
    "clickfunnels.com",
    "canva.com",
    "creativemarket.com",
    "marmalead.com",
    "erank.com",
    "everbee.io",
    "alura.io",
    "sale-samurai.com",
    "insightfactory.app",
    "ecomhunt.com",
    "sellthetrend.com",
    "junglescout.com",
    "helium10.com",
    "wix.com",
    "squarespace.com",
    "bigcommerce.com",
    "woocommerce.com",
    "hubspot.com",
    "semrush.com",
    "ahrefs.com",
)
# Affiliates: a link that pays whoever sends a buyer.
_AFFILIATE_PARAMS = frozenset({"ref", "aff", "affiliate", "aff_id", "affid", "partner", "via", "tag", "irclickid"})
_AFFILIATE_PATH = re.compile(r"/(?:go|recommends|aff|affiliate|refer|out)/", re.IGNORECASE)
_TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|mc_\w+)$", re.IGNORECASE)
SHOWN = 3  # claims FOCUS lists (the newest), by their values
LINE_CHARS = 440  # FOCUS's evidence line
LISTED = 30  # claims a venture's card on the Ventures tab lists (the newest)


def url_key(url: str) -> str:
    """A URL as evidence is matched to a research result: the host without www., the path without a trailing slash,
    the query without tracking parameters, no fragment."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()[:300]
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return f"{host}{parts.path.rstrip('/')}{'?' + query if query else ''}"[:300] or url.strip()[:300]


def record_sources(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int | None,
    llm_call_id: int | None,
    urls: list[str],
    now: str,
) -> None:
    """The pages a research call returned (Ember's code, as research runs): what evidence can be checked against."""
    for url in filter(str.strip, urls):
        conn.execute(
            "INSERT INTO research_sources (mode, session, cycle_id, llm_call_id, url, url_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scope.mode, scope.session, cycle_id, llm_call_id, url[:300], url_key(url), now),
        )


def grade(conn: sqlite3.Connection, scope: AgentScope, url: str) -> str:
    """unchecked, marketing or independent (see the module's docstring)."""
    where, params = scope.where()
    found = conn.execute(
        f"SELECT 1 FROM research_sources WHERE {where} AND url_key = ? LIMIT 1", (*params, url_key(url))
    ).fetchone()
    if found is None:
        return "unchecked"
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    vendor = any(host == d or host.endswith(f".{d}") for d in MARKETING_DOMAINS)
    affiliate = _AFFILIATE_PATH.search(parts.path) is not None or any(
        k.lower() in _AFFILIATE_PARAMS for k, _ in parse_qsl(parts.query, keep_blank_values=True)
    )
    return "marketing" if vendor or affiliate else "independent"


def add(
    conn: sqlite3.Connection,
    scope: AgentScope,
    venture_id: int,
    cycle_id: int | None,
    claim: str,
    metric: str,
    low: float,
    high: float,
    unit: str,
    region: str,
    url: str,
    now: str,
) -> tuple[int, str]:
    """Save a claim; returns (its number, its source's grade)."""
    source = grade(conn, scope, url)
    cursor = conn.execute(
        "INSERT INTO evidence (mode, session, venture_id, cycle_id, created_at, claim, metric, low, high, unit, region,"
        " url, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            venture_id,
            cycle_id,
            now,
            claim,
            metric,
            low,
            high,
            unit,
            region,
            url[:300],
            source,
        ),
    )
    return int(cursor.lastrowid), source


def of_venture(conn: sqlite3.Connection, venture_id: int, limit: int = SHOWN) -> list[sqlite3.Row]:
    """A venture's newest evidence, the newest first."""
    return conn.execute(
        "SELECT * FROM evidence WHERE venture_id = ? ORDER BY id DESC LIMIT ?", (venture_id, limit)
    ).fetchall()


def counts(conn: sqlite3.Connection, venture_id: int) -> dict[str, int]:
    """A venture's claims by grade."""
    found = dict(
        conn.execute(
            "SELECT source, COUNT(*) FROM evidence WHERE venture_id = ? GROUP BY source", (venture_id,)
        ).fetchall()
    )
    return {g: int(found.get(g, 0)) for g in GRADES}


def by_venture(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, dict[str, Any]]:
    """The Ventures tab's evidence: for each venture with any, its claims by grade and the newest LISTED."""
    where, params = scope.where()
    found: dict[int, dict[str, Any]] = {}
    for venture_id, source, n in conn.execute(
        f"SELECT venture_id, source, COUNT(*) FROM evidence WHERE {where} GROUP BY venture_id, source",
        params,
    ):
        entry = found.setdefault(int(venture_id), {"counts": dict.fromkeys(GRADES, 0), "items": []})
        entry["counts"][source] = int(n)
    for r in conn.execute(
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY venture_id ORDER BY id DESC) AS newest"
        f" FROM evidence WHERE {where}) WHERE newest <= ? ORDER BY id DESC",
        (*params, LISTED),
    ):
        found[int(r["venture_id"])]["items"].append(
            {
                "id": r["id"],
                "claim": r["claim"],
                "value": value_text(r),
                "url": r["url"],
                "source": r["source"],
                "created_at": r["created_at"],
            }
        )
    return found


def value_text(r: sqlite3.Row) -> str:
    low, high = float(r["low"]), float(r["high"])
    values = _number(low) if low == high else f"{_number(low)}–{_number(high)}"
    return f"{r['metric']}: {values} {r['unit']} ({r['region']})"


def focus_line(conn: sqlite3.Connection, venture_id: int) -> str:
    """FOCUS's evidence line: how much there is by grade, and the newest claims' values (empty without any)."""
    rows = of_venture(conn, venture_id)
    if not rows:
        return ""
    c = counts(conn, venture_id)
    total = sum(c.values())
    newest = "; ".join(f"#{r['id']} {value_text(r)} [{r['source']}]" for r in rows)
    line = (
        f"Evidence: {total} claim{'s' if total != 1 else ''} ({c['independent']} independent, "
        f"{c['marketing']} marketing, {c['unchecked']} unchecked); the newest: {newest}"
    )
    flat = " ".join(line.split())
    return flat if len(flat) <= LINE_CHARS else flat[: LINE_CHARS - 1] + "…"


def _number(value: float) -> str:
    """1,200 or 1,200.5 or 4.5: thousands grouped, no trailing zeros."""
    return f"{int(value):,}" if value == int(value) else f"{value:,.6f}".rstrip("0").rstrip(".")
