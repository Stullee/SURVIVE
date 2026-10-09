"""The quality critic (0.18.0, vision/learning.md part 1): an independent score of a product line's live listings.

The agent graded its own work, so "good enough" was its word, and the owner did the quality checks ("the photos look
like duplicates"). Now, before a plan, a call of its own on the strategy model looks at one product line's newest live
Etsy listing as a demanding buyer and an experienced seller would: its cover photo (the first thing buyers see), its
title, tags, price and description, against the demand note's market prices, and the owner's rule that a product must
beat what a free AI chat gives. It answers a score from 1 to 10, pass (MIN_PASS and more) or improve, and the fixes
that matter most. Ember's code keeps it: the daily review shows it in the project's research line (reach.py), a bet on
orders is refused while it says improve (bets.py), and the waiting-time list asks for its fixes (slack.py).

One listing a cycle: one never checked first, then the one checked longest ago, again after RECHECK_DAYS or after a
change of it was carried out. It counts toward the daily cap only (as the venture critic), leaves what the cycle needs
to work, never ends the cycle, and a failed one is tried again the next day.

0.24.0: each live listing of a product line is checked, and every verdict names its listing. The critic judged only a
line's newest listing and said nothing of which, so live, three verdicts on a €39 licence bundle ("the cover letter is
PDF only", tags that contradict its licence) were read as verdicts on the line's cover letter listing for three days:
the agent checked that listing four times and found nothing, and an edit of it had the bundle checked again.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..integrations import etsy_publisher, printify
from ..products import images
from . import prompts, reach, ventures
from .sandbox import Jail, SandboxError
from .store import AgentScope

MIN_PASS = 7
RECHECK_DAYS = 14
LOOK_PIXELS = 1_000  # the cover photo's longer side, as the agent's own look sees it
FIXES_CHARS = prompts.QUALITY_FIXES_CHARS
TITLE_CHARS = 50  # of a listing's title, where a verdict names it
REVIEW_SHOWN = 2  # the listings whose checks the daily review's research line shows
REVIEW_FIXES_CHARS = 160  # of each one's fixes there


def due(conn: sqlite3.Connection, scope: AgentScope, today: date) -> tuple[int, int] | None:
    """The product line (project) and its live listing checked next, or None: a listing never checked first, then
    the one checked longest ago, once a change of it was carried out since or RECHECK_DAYS passed."""
    where, params = scope.where()
    candidates = []
    stale = (today - timedelta(days=RECHECK_DAYS)).isoformat()
    for project_id, funnel in reach.funnels(conn, scope).items():
        if not funnel.listings:
            continue
        rows = conn.execute(
            f"SELECT status, created_at, listing_id FROM quality_checks WHERE {where} AND project_id = ?"
            " ORDER BY id DESC",
            (*params, project_id),
        ).fetchall()
        if rows and rows[0]["status"] == "failed" and str(rows[0]["created_at"])[:10] == today.isoformat():
            continue  # failed today: tomorrow
        for listing in funnel.listings:
            if ventures.listing_stopped(conn, scope, listing) is not None:
                continue  # 0.24.0: none for a listing the owner's park stopped (0.23.3: none may change it)
            ok = next((r for r in rows if r["status"] == "ok" and r["listing_id"] == listing), None)
            if ok is None:
                candidates.append(("", project_id, listing))
                continue
            checked = str(ok["created_at"])
            if changed_since(conn, scope, ok) or checked[:10] <= stale:
                candidates.append((checked, project_id, listing))
    if not candidates:
        return None
    _, project_id, listing = min(candidates)
    return project_id, listing


def _title(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> str | None:
    """A listing's title as Ember's records keep it (its own or the one Printify made), or None."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT title FROM etsy_listings WHERE {where} AND listing_id = ? UNION ALL SELECT title FROM"
        f" printify_products WHERE {where} AND listing_id = ? LIMIT 1",
        (*params, listing_id, *params, listing_id),
    ).fetchone()
    return " ".join(str(row["title"]).split()) if row is not None else None


def label(conn: sqlite3.Connection, scope: AgentScope, listing_id: int | None) -> str:
    """0.24.0: a checked listing as the verdicts name it: 'listing #4587912058 "Resume Template Commercial…"'."""
    if listing_id is None:
        return "its newest listing"  # a check from before 0.24.0 that the upgrade couldn't place
    title = _title(conn, scope, listing_id)
    if title is None:
        return f"listing #{listing_id}"
    short = title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1].rstrip() + "…"
    return f'listing #{listing_id} "{short}"'


def newest(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> list[sqlite3.Row]:
    """0.24.0: the newest check that came through of each live listing of a project, the newest first."""
    listings = set(reach.funnels(conn, scope).get(project_id, reach.Funnel()).listings)
    where, params = scope.where()
    found: dict[int | None, sqlite3.Row] = {}
    for row in conn.execute(
        f"SELECT * FROM quality_checks WHERE {where} AND project_id = ? AND status = 'ok' ORDER BY id DESC",
        (*params, project_id),
    ).fetchall():
        if row["listing_id"] in listings and row["listing_id"] not in found:
            found[row["listing_id"]] = row
    return list(found.values())


def case(
    conn: sqlite3.Connection, scope: AgentScope, workspace: Jail, project_id: int, listing_id: int
) -> tuple[str, bytes | None]:
    """What the critic reads of a project's live listing (``listing_id``): the listing as text, and its cover photo (a
    PNG) if readable."""
    where, params = scope.where()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    rows = conn.execute(
        f"SELECT * FROM etsy_listings WHERE {where} AND listing_id = ? AND status = 'active' ORDER BY id DESC",
        (*params, listing_id),
    ).fetchall()
    lines = [f"PRODUCT LINE: {project['title'] if project else project_id}", f"LISTING: #{listing_id}"]
    picture = None
    listing = etsy_publisher.recorded_listing(conn, scope, rows[0]) if rows else None
    if listing is not None:
        lines += [
            f"Title: {listing.title}",
            f"Price: {listing.price} {listing.currency}",
            f"Tags: {', '.join(listing.tags)}",
            f"Category: {listing.category}",
            f"Photos: {len(listing.photos)}; files the buyer gets: {', '.join(f.path for f in listing.files)}",
            f"Description:\n{listing.description[:3000]}",
        ]
        if listing.photos:
            try:
                picture = images.thumbnail(workspace.read_bytes(listing.photos[0].path), LOOK_PIXELS)[0]
            except (SandboxError, OSError, ValueError):
                picture = None
    else:
        made = _printify(conn, scope, listing_id)  # 0.18.1: a product line live through Printify
        if made is not None:
            product, prices = made
            lines += [
                f"Title: {product.title}",
                f"Prices: {prices} (print on demand: made and shipped by Printify)",
                f"Tags: {', '.join(product.tags)}",
                "Photos: Printify's mockups of the design below",
                f"Description:\n{product.description[:3000]}",
            ]
            try:
                picture = images.thumbnail(workspace.read_bytes(product.image.path), LOOK_PIXELS)[0]
            except (SandboxError, OSError, ValueError):
                picture = None
        else:
            lines.append("Its listing isn't in Ember's records: judge from its title.")
            title = _title(conn, scope, listing_id)
            lines += [f"Title: {title}"] if title else []
    note = conn.execute(
        f"SELECT * FROM demand_notes WHERE {where} AND project_id = ? ORDER BY id DESC LIMIT 1", (*params, project_id)
    ).fetchone()
    if note is not None:
        prices = (
            f"; {note['listings']} competing listings, prices {note['low']:g} / {note['median']:g} / {note['high']:g}"
            f" {note['currency']} (quartiles)"
            if note["low"] is not None
            else ""
        )
        lines.append(f"DEMAND NOTE: keywords {note['keywords']}; {note['demand'] or ''}{prices}")
    funnel = reach.funnels(conn, scope).get(project_id)
    if funnel is not None:
        lines.append(funnel.text())
    return "\n".join(lines), picture


def _printify(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> tuple[Any, str] | None:
    """0.18.1: the live Printify product of a listing, as approved (its design is the picture), and its prices; None
    without one. Live, the critic scored a poster line 3/10 on its title alone."""
    where, params = scope.where("p")
    for row in conn.execute(
        f"SELECT a.action, p.listing_id FROM printify_products p JOIN approvals a ON a.id = p.approval_id"
        f" WHERE {where} AND p.listing_id = ? AND p.status = 'active' ORDER BY p.id DESC",
        (*params, listing_id),
    ).fetchall():
        try:
            product = printify.product_from_action(row["action"])
        except (printify.PrintifyError, ValueError, KeyError, TypeError):
            continue
        prices = ", ".join(printify.money(price, product.currency) for _, price in product.prices)
        return product, prices
    return None


def parse(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    score = data.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 10:
        return None
    fixes = " ".join(str(data.get("fixes") or "").split())[:FIXES_CHARS]
    return {"score": score, "verdict": "pass" if score >= MIN_PASS else "improve", "fixes": fixes}


def save(
    conn: sqlite3.Connection,
    scope: AgentScope,
    project_id: int,
    call_id: int | None,
    answer: dict[str, Any] | None,
    note: str | None,
    now: str,
    listing_id: int | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO quality_checks (mode, session, project_id, llm_call_id, created_at, status, score, verdict, fixes,"
        " note, listing_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            project_id,
            call_id,
            now,
            "ok" if answer else "failed",
            answer["score"] if answer else None,
            answer["verdict"] if answer else None,
            answer["fixes"] if answer else "",
            (note or "")[:300] or None,
            listing_id,
        ),
    )
    return int(cursor.lastrowid)


def review_text(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> str:
    """0.24.0: a product line's quality checks for the daily review's research line, each with the listing it
    judged: the newest of each live listing (REVIEW_SHOWN), or the line's unplaced one; "" before any."""
    checked = newest(conn, scope, project_id) or unplaced(conn, scope, project_id)
    parts = [
        f"quality {r['score']}/10, {r['verdict']} ({str(r['created_at'])[:10]}, {label(conn, scope, r['listing_id'])}"
        f"{', changed at Etsy since' if changed_since(conn, scope, r) else ''}): {r['fixes'][:REVIEW_FIXES_CHARS]}"
        for r in checked[:REVIEW_SHOWN]
    ]
    if len(checked) > REVIEW_SHOWN:
        parts.append(f"{len(checked) - REVIEW_SHOWN} more of its listings checked")
    return "; ".join(parts)


def fixes(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> list[str]:
    """What the newest check of each listing of the line said to fix, when the listing wasn't changed since (0.24.0:
    each names its listing; a change waits for the next check, ``due``). 0.35.0: YOUR STEP quotes them on the plan
    tree's critic step (lines.py's, for READY, until 0.34.0)."""
    found = []
    for row in newest(conn, scope, project_id) or unplaced(conn, scope, project_id):
        if row["verdict"] == "improve" and row["fixes"] and not changed_since(conn, scope, row):
            found.append(f"the quality critic said of {label(conn, scope, row['listing_id'])}: {row['fixes'][:200]}")
    return found


def verdict(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> str:
    """A product line's verdict: improve while the newest check of any of its live listings says so, else pass; ""
    before any check. 0.24.0: a line none of whose live listings was checked yet has its unplaced check's."""
    checked = newest(conn, scope, project_id) or unplaced(conn, scope, project_id)
    if not checked:
        return ""
    return "improve" if any(r["verdict"] == "improve" for r in checked) else "pass"


def lowest(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> int | None:
    """0.35.3: the lowest score of the checks ``verdict`` reads (the plan tree weighs a low one as a defect, the rest
    of an improve verdict as suggestions); None before any."""
    checked = newest(conn, scope, project_id) or unplaced(conn, scope, project_id)
    scores = [int(r["score"]) for r in checked if r["score"] is not None]
    return min(scores) if scores else None


def changed_since(conn: sqlite3.Connection, scope: AgentScope, check: sqlite3.Row) -> bool:
    """0.24.0: whether a change of the checked listing was carried out after the check (its fixes may be done: the
    listing is checked again next, ``due``)."""
    if check["listing_id"] is None:
        return False
    where, params = scope.where()
    return (
        conn.execute(
            f"SELECT 1 FROM etsy_edits WHERE {where} AND listing_id = ? AND status IN ('done', 'partial')"
            " AND finished_at > ? LIMIT 1",
            (*params, check["listing_id"], check["created_at"]),
        ).fetchone()
        is not None
    )


def unplaced(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> list[sqlite3.Row]:
    """0.24.0: a line's newest check that names no listing (one from before 0.24.0 the upgrade couldn't place), as a
    list of none or one: it stands for the line until one of its live listings is checked. A check of a listing no
    longer live stands for nothing."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM quality_checks WHERE {where} AND project_id = ? AND status = 'ok' AND listing_id IS NULL"
        " ORDER BY id DESC LIMIT 1",
        (*params, project_id),
    ).fetchall()
