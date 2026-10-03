"""The quality critic (0.18.0, vision/learning.md part 1): an independent score of a product line's live listings.

The agent graded its own work, so "good enough" was its word, and the owner did the quality checks ("the photos look
like duplicates"). Now, before a plan, a call of its own on the strategy model looks at one product line's newest live
Etsy listing as a demanding buyer and an experienced seller would: its cover photo (the first thing buyers see), its
title, tags, price and description, against the demand note's market prices, and the owner's rule that a product must
beat what a free AI chat gives. It answers a score from 1 to 10, pass (MIN_PASS and more) or improve, and the fixes
that matter most. Ember's code keeps it: the daily review shows it in the project's research line (reach.py), a bet on
orders is refused while it says improve (bets.py), and the waiting-time list asks for its fixes (slack.py).

One product line a cycle: the one never checked first, then the one checked longest ago, again after RECHECK_DAYS or
after a change of its listings was carried out. It counts toward the daily cap only (as the venture critic), leaves
what the cycle needs to work, never ends the cycle, and a failed one is tried again the next day.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..integrations import etsy_publisher, printify
from ..products import images
from . import prompts, reach
from .sandbox import Jail, SandboxError
from .store import AgentScope

MIN_PASS = 7
RECHECK_DAYS = 14
LOOK_PIXELS = 1_000  # the cover photo's longer side, as the agent's own look sees it
FIXES_CHARS = prompts.QUALITY_FIXES_CHARS


def due(conn: sqlite3.Connection, scope: AgentScope, today: date) -> int | None:
    """The project whose live listings are checked next, or None."""
    where, params = scope.where()
    candidates = []
    for project_id, funnel in reach.funnels(conn, scope).items():
        if not funnel.listings:
            continue
        rows = conn.execute(
            f"SELECT status, created_at FROM quality_checks WHERE {where} AND project_id = ? ORDER BY id DESC",
            (*params, project_id),
        ).fetchall()
        if rows and rows[0]["status"] == "failed" and str(rows[0]["created_at"])[:10] == today.isoformat():
            continue  # failed today: tomorrow
        ok = next((r for r in rows if r["status"] == "ok"), None)
        if ok is None:
            candidates.append(("", project_id))
            continue
        checked = str(ok["created_at"])
        marks = ", ".join("?" for _ in funnel.listings)
        edited = conn.execute(
            f"SELECT 1 FROM etsy_edits WHERE {where} AND listing_id IN ({marks}) AND status IN ('done', 'partial')"
            " AND finished_at > ? LIMIT 1",
            (*params, *funnel.listings, checked),
        ).fetchone()
        if edited or checked[:10] <= (today - timedelta(days=RECHECK_DAYS)).isoformat():
            candidates.append((checked, project_id))
    return min(candidates)[1] if candidates else None


def case(conn: sqlite3.Connection, scope: AgentScope, workspace: Jail, project_id: int) -> tuple[str, bytes | None]:
    """What the critic reads of a project: its newest live listing as text, and its cover photo (a PNG) if readable."""
    where, params = scope.where()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    rows = [
        r
        for r in conn.execute(
            f"SELECT * FROM etsy_listings WHERE {where} AND listing_id IS NOT NULL AND status = 'active' ORDER BY id"
            " DESC",
            params,
        ).fetchall()
        if r["listing_id"] in reach.funnels(conn, scope).get(project_id, reach.Funnel()).listings
    ]
    lines = [f"PRODUCT LINE: {project['title'] if project else project_id}"]
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
        made = _printify(conn, scope, project_id)  # 0.18.1: a product line live through Printify
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
            lines += [f"Title: {r['title']}" for r in rows[:1]]
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


def _printify(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> tuple[Any, str] | None:
    """0.18.1: the newest live Printify product of a project, as approved (its design is the picture), and its
    prices; None without one. Live, the critic scored a poster line 3/10 on its title alone."""
    listed = set(reach.funnels(conn, scope).get(project_id, reach.Funnel()).listings)
    where, params = scope.where("p")
    for row in conn.execute(
        f"SELECT a.action, p.listing_id FROM printify_products p JOIN approvals a ON a.id = p.approval_id"
        f" WHERE {where} AND p.listing_id IS NOT NULL AND p.status = 'active' ORDER BY p.id DESC",
        params,
    ).fetchall():
        if row["listing_id"] not in listed:
            continue
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
) -> int:
    cursor = conn.execute(
        "INSERT INTO quality_checks (mode, session, project_id, llm_call_id, created_at, status, score, verdict, fixes,"
        " note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    return int(cursor.lastrowid)
