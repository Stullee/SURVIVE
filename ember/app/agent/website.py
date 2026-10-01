"""The owner's website as Ember keeps it (0.13.0, Phase E3; app/products/site.py builds it).

The agent's pages live in site_pages (per mode and dry-run session), written with the site_page tool. The owner's data
comes from their options. Each download the owner makes is kept in site_downloads with its files' fingerprints, so the
plan and the dashboard can say what changed since. Ember never publishes the site: the owner uploads it to their host.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..config import Settings
from ..products import site
from .store import AgentScope

_ADDRESS_LINES = re.compile(r"\n|\\n|,")  # the options' text field has one line: commas separate the address's
# 0.14.0: a home page's file name ends the address the owner gave (live: https://ember-ai.de/index.html made every page
# https://ember-ai.de/index.html/<name>.html): the site's address is its folder.
_HOME_FILE = re.compile(r"/index\.html?$", re.IGNORECASE)


def owner(settings: Settings) -> site.Owner:
    """The owner's data for the site, from their options."""
    lines = tuple(part.strip() for part in _ADDRESS_LINES.split(settings.site_address) if part.strip())
    return site.Owner(
        legal_name=settings.site_owner_name.strip(),
        address=lines,
        email=settings.site_email.strip(),
        phone=settings.site_phone.strip(),
        vat_id=settings.site_vat_id.strip(),
        host=settings.site_host.strip(),
        name=settings.site_name.strip(),
        language=settings.site_language,
        url=address(settings),
    )


def address(settings: Settings) -> str:
    """The site's address in use: the owner's site_url without a home page's file name or a closing slash."""
    return _HOME_FILE.sub("", settings.site_url.strip().rstrip("/")).rstrip("/")


def pages(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The site's pages, the home page first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM site_pages WHERE {where} AND removed_at IS NULL ORDER BY slug <> ?, slug", (*params, site.HOME)
    ).fetchall()


def save(conn: sqlite3.Connection, scope: AgentScope, page: site.Page, cycle_id: int | None, now: str) -> bool:
    """Write a page: a new one, or a new version of it (a removed one comes back). Returns whether it is new on the
    site. Raises site.SiteError when the site is full."""
    where, params = scope.where()
    known = conn.execute(
        f"SELECT removed_at FROM site_pages WHERE {where} AND slug = ?", (*params, page.slug)
    ).fetchone()
    new = known is None or known["removed_at"] is not None
    if new and len(pages(conn, scope)) >= site.MAX_PAGES:
        raise site.SiteError(f"the site has {site.MAX_PAGES} pages already: remove one first")
    conn.execute(
        "INSERT INTO site_pages (mode, session, slug, title, description, menu, source, cycle_id, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (mode, session, slug) DO UPDATE SET title = excluded.title,"
        " description = excluded.description, menu = excluded.menu, source = excluded.source,"
        " cycle_id = excluded.cycle_id, updated_at = excluded.updated_at, removed_at = NULL",
        (scope.mode, scope.session, page.slug, page.title, page.description, page.menu, page.source, cycle_id, now),
    )
    return new


def remove(conn: sqlite3.Connection, scope: AgentScope, slug: str, now: str) -> bool:
    """Take a page off the site. Returns whether there was one."""
    where, params = scope.where()
    cursor = conn.execute(
        f"UPDATE site_pages SET removed_at = ?, updated_at = ? WHERE {where} AND slug = ? AND removed_at IS NULL",
        (now, now, *params, slug),
    )
    return cursor.rowcount > 0


def built(conn: sqlite3.Connection, scope: AgentScope, who: site.Owner) -> dict[str, bytes]:
    """The site's files as Ember's code builds them now. Raises site.SiteError with why it can't be built yet."""
    rows = pages(conn, scope)
    return site.build([site.Page(r["slug"], r["title"], r["description"], r["source"], r["menu"]) for r in rows], who)


def last_download(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM site_downloads WHERE {where} ORDER BY id DESC LIMIT 1", params).fetchone()


def record_download(conn: sqlite3.Connection, scope: AgentScope, files: dict[str, bytes], now: str) -> None:
    fingerprints = json.dumps(site.fingerprints(files), separators=(",", ":"))
    conn.execute(
        "INSERT INTO site_downloads (mode, session, downloaded_at, files) VALUES (?, ?, ?, ?)",
        (scope.mode, scope.session, now, fingerprints),
    )


def state(conn: sqlite3.Connection, scope: AgentScope, who: site.Owner) -> dict[str, Any]:
    """The site as the plan and the dashboard show it: its pages, why it can't be built (if it can't), the owner's
    last download and the files changed since."""
    rows = pages(conn, scope)
    last = last_download(conn, scope)
    problem = None
    changed: list[str] = []
    try:
        files = built(conn, scope, who)
    except site.SiteError as exc:
        problem = str(exc)
    else:
        if last is not None:
            changed = site.changes(files, json.loads(last["files"]))
    return {
        "pages": [
            {
                "slug": r["slug"],
                "title": r["title"],
                "description": r["description"],
                "menu": r["menu"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ],
        "problem": problem,
        "downloaded_at": last["downloaded_at"] if last is not None else None,
        "changed": changed,
    }


def planner_text(conn: sqlite3.Connection, scope: AgentScope, who: site.Owner) -> str:
    """The WEBSITE section of the plan: what the owner has of the site first (the section's cut takes the end), then
    its address and its pages."""
    now = state(conn, scope, who)
    if now["problem"]:
        lines = [f"It can't be built yet: {now['problem']}."]
    elif now["downloaded_at"] is None:
        lines = ["Your owner hasn't downloaded it yet: they publish it themselves."]
    elif now["changed"]:
        lines = [f"Changed since your owner downloaded it ({now['downloaded_at'][:10]}): {', '.join(now['changed'])}."]
    else:
        lines = [f"Your owner downloaded it as it is ({now['downloaded_at'][:10]})."]
    if who.url:
        lines.append(f"Its address: {who.url.rstrip('/')}/ (a page is there as <name>.html).")
    shown = "; ".join(
        f"{p['slug']} {json.dumps(p['title'], ensure_ascii=False)} ({p['updated_at'][:10]})" for p in now["pages"]
    )
    lines.append(f"Pages ({len(now['pages'])} of {site.MAX_PAGES}): {shown or 'none yet'}.")
    return "\n".join(lines)


def describe(conn: sqlite3.Connection, scope: AgentScope, settings: Settings) -> dict[str, Any]:
    """The dashboard's Website card (never the owner's address: it is in their options)."""
    if not settings.site_enabled:
        return {"status": "disabled"}
    now = state(conn, scope, owner(settings))
    return {
        "status": "not_ready" if now["problem"] else "ok",
        "reason": now["problem"],
        "url": address(settings) or None,
        "language": settings.site_language,
        "max_pages": site.MAX_PAGES,
        "pages": now["pages"],
        "downloaded_at": now["downloaded_at"],
        "changed": now["changed"],
    }
