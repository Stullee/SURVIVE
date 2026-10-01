"""Google Search Console (0.16.0): how the owner's website does in Google Search, read by Ember's code.

Read-only: Ember's code signs in as the owner's service account (its JSON key in the options, search_console_key)
with the scope webmasters.readonly, and reads the Search Analytics of one property (search_console_site, by default
the Domain property of the website's address): impressions, clicks and average position per day, and the top searches
and pages of the last 28 days. Nothing is ever written to Google, and the site itself is unchanged (no script, no
cookie: its privacy page stays true). Requests go only to oauth2.googleapis.com and searchconsole.googleapis.com
(anything else is refused before it leaves, redirects aren't followed); responses are size-limited and never logged;
the key is registered for log redaction. The numbers are kept (search_console_days, search_console_top) for the plan's
GOOGLE SEARCH, the dashboard and the metrics search_impressions and search_clicks. In a dry run a fake property stands
in: nothing leaves the app.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from .. import events
from ..agent.store import AgentScope
from ..config import SEARCH_SITE, Settings
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from ..logging_setup import register_secret

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - Google's token endpoint, not a secret
API_URL = "https://searchconsole.googleapis.com"
HOSTS = frozenset({"oauth2.googleapis.com", "searchconsole.googleapis.com"})
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
SYNC_HOURS = 12  # Google's numbers change about once a day, two to three days late
RETRY_HOURS = 1  # after a failed sync
DAYS = 30  # per day, kept
PERIOD = 28  # the totals and the top searches and pages (Search Console's own default)
TOP = 15  # searches and pages kept
FRESH_HOURS = 48  # a metric reads numbers synced at most this long ago
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_EMAIL = re.compile(r"^[a-z0-9-]{1,63}@[a-z0-9-]{1,63}\.iam\.gserviceaccount\.com$")


class SearchConsoleError(Exception):
    """What went wrong, in words for the owner (never holding the key)."""


@dataclass(frozen=True)
class ServiceKey:
    client_email: str
    private_key: str = field(repr=False)
    private_key_id: str = field(repr=False)
    token_uri: str = TOKEN_URL


def parse_key(raw: str) -> ServiceKey:
    """The owner's service-account key (the JSON file Google Cloud gives, pasted whole, line breaks or not). Raises
    SearchConsoleError naming what is wrong (never the key)."""
    text = raw.strip()
    if not text:
        raise SearchConsoleError("search_console_key is missing: paste the service account's JSON key")
    try:
        data = json.loads(text)
    except ValueError:
        raise SearchConsoleError("search_console_key isn't JSON: paste the whole key file, from { to }") from None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise SearchConsoleError("search_console_key isn't a service account's key (its type must be service_account)")
    email = str(data.get("client_email") or "").strip().lower()
    key = str(data.get("private_key") or "")
    key_id = str(data.get("private_key_id") or "")
    token_uri = str(data.get("token_uri") or TOKEN_URL)
    if not _EMAIL.match(email):
        raise SearchConsoleError("the key has no service account email (client_email ...@...iam.gserviceaccount.com)")
    if "-----BEGIN PRIVATE KEY-----" not in key or not key_id:
        raise SearchConsoleError("the key has no private key: paste the whole key file")
    if token_uri != TOKEN_URL:
        raise SearchConsoleError(f"the key's token_uri must be {TOKEN_URL}")
    lines = [line for line in key.replace("\\n", "\n").splitlines() if line and not line.startswith("-----")]
    for secret in (text, key, key_id, *lines):  # each line of the key too: a log never shows a part of it
        register_secret(secret)
    return ServiceKey(email, key, key_id, token_uri)


def site_of(settings: Settings) -> str:
    """The Search Console property: search_console_site, or the Domain property of the website's address ("": none)."""
    chosen = settings.search_console_site.strip()
    if chosen:
        return chosen
    host = (urlsplit(settings.site_url.strip()).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"sc-domain:{host}" if host else ""


def problems(settings: Settings, mode: str) -> list[str]:
    """What keeps Ember's code from reading Search Console (none: it can). A dry run needs no key."""
    if not settings.search_console_enabled:
        return ["Search Console is off (search_console_enabled)"]
    found = []
    site = site_of(settings)
    if not site:
        found.append("search_console_site is missing (or set site_url, the website's address)")
    elif not SEARCH_SITE.match(site):
        found.append("search_console_site must be sc-domain:example.org or https://example.org/")
    if mode == "live":
        try:
            parse_key(settings.search_console_key.get_secret_value())
        except SearchConsoleError as exc:
            found.append(str(exc))
    return found


def meta_key(mode: str, name: str) -> str:
    return f"integrations.search_console.{mode}.{name}"


# --- the numbers ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    key: str  # a day (YYYY-MM-DD), a search or a page
    clicks: int
    impressions: int
    position: float


class Property(Protocol):
    simulated: bool

    def rows(self, dimension: str, start: date, end: date, limit: int) -> list[Row]: ...


def _rows(data: Any) -> list[Row]:
    found = []
    for item in data.get("rows", []) if isinstance(data, dict) else []:
        try:
            found.append(
                Row(
                    str(item["keys"][0])[:500],
                    int(item.get("clicks", 0)),
                    int(item.get("impressions", 0)),
                    round(float(item.get("position", 0.0)), 1),
                )
            )
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return found


class FakeProperty:
    """The dry run's property: a few made-up numbers that grow day by day; nothing leaves the app."""

    simulated = True
    QUERIES = ("haushaltsbuch vorlage", "bewerbung nachfassen", "bewerbungs tracker excel", "nebenkostenabrechnung")
    PAGES = ("/blog/haushaltsbuch-vorlage.html", "/blog/bewerbung-nachfassen.html", "/", "/blog/")

    def __init__(self, site_url: str) -> None:
        parts = urlsplit(site_url)
        self.base_url = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "https://example.org"

    def rows(self, dimension: str, start: date, end: date, limit: int) -> list[Row]:
        if dimension == "date":
            days = (end - start).days + 1
            return [
                Row((start + timedelta(days=i)).isoformat(), 1 if i % 3 == 0 else 0, 2 + i, round(30 - i * 0.5, 1))
                for i in range(days)
            ][:limit]
        names = self.QUERIES if dimension == "query" else tuple(self.base_url + p for p in self.PAGES)
        return [Row(name, 3 - i if i < 3 else 0, 40 - i * 8, 12.0 + i * 4) for i, name in enumerate(names)][:limit]


# --- what is kept, and how the plan and the dashboard show it --------------------------------------------------------


def totals(conn: sqlite3.Connection, scope: AgentScope, end: date, days: int = PERIOD) -> tuple[int, int, float | None]:
    """Impressions, clicks and the impression-weighted average position of the ``days`` days up to ``end``."""
    where, params = scope.where()
    start = (end - timedelta(days=days - 1)).isoformat()
    row = conn.execute(
        f"SELECT COALESCE(SUM(impressions), 0), COALESCE(SUM(clicks), 0), SUM(position * impressions)"
        f" FROM search_console_days WHERE {where} AND day BETWEEN ? AND ?",
        (*params, start, end.isoformat()),
    ).fetchone()
    impressions, clicks = int(row[0]), int(row[1])
    position = round(float(row[2]) / impressions, 1) if impressions and row[2] is not None else None
    return impressions, clicks, position


def top(conn: sqlite3.Connection, scope: AgentScope, kind: str, limit: int = TOP) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM search_console_top WHERE {where} AND kind = ? ORDER BY rank LIMIT ?", (*params, kind, limit)
    ).fetchall()


def latest_day(conn: sqlite3.Connection, scope: AgentScope) -> date | None:
    where, params = scope.where()
    row = conn.execute(f"SELECT MAX(day) FROM search_console_days WHERE {where}", params).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def _short(page: str) -> str:
    """A page of the property by its path (the property is one site)."""
    parts = urlsplit(page)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "") if parts.netloc else page


def text(conn: sqlite3.Connection, db: Database, scope: AgentScope, settings: Settings) -> str:
    """The plan's GOOGLE SEARCH: what keeps it from being read (first), then the numbers."""
    lines = []
    trouble = problems(settings, scope.mode)
    if trouble:
        lines.append(f"Your owner's Search Console can't be read: {'; '.join(trouble)}. Tell them.")
    error = db.get_meta(meta_key(scope.mode, "last_error"))
    if error:
        lines.append(f"The last read failed: {error}")
    synced = db.get_meta(meta_key(scope.mode, "last_sync_at"))
    last = latest_day(conn, scope)
    site = site_of(settings)
    if not synced or last is None:
        lines.append(f"No numbers yet for {site or 'the site'}: Ember's code reads Google twice a day.")
        return "\n".join(lines)
    impressions, clicks, position = totals(conn, scope, last)
    week = totals(conn, scope, last, 7)
    where = f" ({site}; read {synced[:16].replace('T', ' ')} UTC; Google's numbers come 2-3 days late)"
    lines.append(
        f"Last {PERIOD} days to {last.isoformat()}{where}: {impressions} impressions, {clicks} clicks"
        + (f", average position {position}" if position is not None else "")
        + f". Last 7 days: {week[0]} impressions, {week[1]} clicks."
    )
    searches = top(conn, scope, "query", 8)
    if searches:
        lines.append(
            "Top searches: "
            + "; ".join(
                f"{json.dumps(r['key'], ensure_ascii=False)} {r['impressions']}/{r['clicks']} pos {r['position']}"
                for r in searches
            )
            + " (impressions/clicks)."
        )
    pages = top(conn, scope, "page", 8)
    if pages:
        lines.append(
            "Top pages: " + "; ".join(f"{_short(str(r['key']))} {r['impressions']}/{r['clicks']}" for r in pages) + "."
        )
    return "\n".join(lines)


def describe(db: Database, scope: AgentScope, settings: Settings, today: date) -> dict[str, Any]:
    """The dashboard's Google Search card (never the key: only the service account's email, to add in Search
    Console)."""
    if not settings.search_console_enabled:
        return {"status": "disabled"}
    trouble = problems(settings, scope.mode)
    email = None
    if settings.search_console_key.get_secret_value().strip():  # also in a dry run: the owner sets it up first
        try:
            email = parse_key(settings.search_console_key.get_secret_value()).client_email
        except SearchConsoleError:
            email = None
    with db.connection() as conn:
        last = latest_day(conn, scope)
        impressions, clicks, position = totals(conn, scope, last or today)
        searches = [dict(r) for r in top(conn, scope, "query")]
        pages = [dict(r) for r in top(conn, scope, "page")]
    keep = ("key", "clicks", "impressions", "position")
    return {
        "status": "not_ready" if trouble else "ok",
        "reason": "; ".join(trouble) or None,
        "simulated": scope.mode == "dry_run",
        "site": site_of(settings) or None,
        "service_account": email,
        "last_sync_at": db.get_meta(meta_key(scope.mode, "last_sync_at")) or None,
        "last_error": db.get_meta(meta_key(scope.mode, "last_error")) or None,
        "last_day": last.isoformat() if last else None,
        "period_days": PERIOD,
        "impressions": impressions,
        "clicks": clicks,
        "position": position,
        "queries": [{k: r[k] for k in keep} for r in searches],
        "pages": [{k: r[k] for k in keep} for r in pages],
    }


# --- reading Google -------------------------------------------------------------------------------------------------


class SearchConsole:
    """Reads the owner's property at most every SYNC_HOURS (an hour after a failure) and keeps the numbers."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Any,
        mode: str,
        transport: Any = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.mode = mode
        self._transport = transport  # tests only
        self._lock = threading.Lock()
        self._live: Property | None = None

    def property(self) -> Property | None:
        """The property to read now, or None (off, or what is missing is in problems())."""
        if problems(self.settings, self.mode):
            return None
        site = site_of(self.settings)
        if self.mode == "dry_run":
            return FakeProperty(self.settings.site_url.strip())
        if self._live is None:
            from .search_console_live import LiveProperty  # the only module that talks to Google (and imports httpx2)

            key = parse_key(self.settings.search_console_key.get_secret_value())
            self._live = LiveProperty(key, site, self.clock, self._transport)
        return self._live

    def due(self) -> bool:
        if not self.settings.search_console_enabled:
            return False
        error = self.db.get_meta(meta_key(self.mode, "last_error"))
        last = self.db.get_meta(meta_key(self.mode, "last_attempt_at"))
        if not last:
            return True
        wait = timedelta(hours=RETRY_HOURS if error else SYNC_HOURS)
        return self.clock.now() - from_iso(last) >= wait

    def sync(self, force: bool = False) -> str | None:
        """Read the property (when due, or ``force``): the days, the top searches and pages. Returns an error or
        None."""
        if not (force or self.due()) or not self._lock.acquire(blocking=force):
            return None
        try:
            found = self.property()
            if found is None:
                error = "; ".join(problems(self.settings, self.mode))
                self._failed(error)
                return error
            today = self.clock.today()
            self.db.set_meta(meta_key(self.mode, "last_attempt_at"), to_iso(self.clock.now()))
            try:
                days = found.rows("date", today - timedelta(days=DAYS), today, DAYS + 5)
                start = today - timedelta(days=PERIOD + 2)  # the last PERIOD days of data (they come 2-3 days late)
                searches = found.rows("query", start, today, TOP)
                pages = found.rows("page", start, today, TOP)
            except SearchConsoleError as exc:
                self._failed(str(exc))
                return str(exc)
            except Exception as exc:  # noqa: BLE001 - reported on the dashboard, never raised into the scheduler
                log.exception("Reading Search Console failed")
                self._failed(f"reading failed ({type(exc).__name__})")
                return type(exc).__name__
            self._keep(days, searches, pages)
            return None
        finally:
            self._lock.release()

    def _failed(self, error: str) -> None:
        key = meta_key(self.mode, "last_error")
        if self.db.get_meta(key) != error:
            events.record(self.db, "warning", "search_console", f"Search Console: {error}"[:300])
        self.db.set_meta(key, error[:500])
        self.db.set_meta(meta_key(self.mode, "last_attempt_at"), to_iso(self.clock.now()))

    def _keep(self, days: list[Row], searches: list[Row], pages: list[Row]) -> None:
        scope: AgentScope = self.scope()
        now = to_iso(self.clock.now())
        site = site_of(self.settings)
        with self.db.transaction() as conn:
            where, params = scope.where()
            for row in days:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.key):
                    continue
                conn.execute(
                    "INSERT INTO search_console_days (mode, session, site, day, clicks, impressions, position,"
                    " synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (mode, session, site, day) DO UPDATE SET"
                    " clicks = excluded.clicks, impressions = excluded.impressions, position = excluded.position,"
                    " synced_at = excluded.synced_at",
                    (scope.mode, scope.session, site, row.key, row.clicks, row.impressions, row.position, now),
                )
            conn.execute(f"DELETE FROM search_console_days WHERE {where} AND site <> ?", (*params, site))
            conn.execute(f"DELETE FROM search_console_top WHERE {where}", params)
            for kind, rows in (("query", searches), ("page", pages)):
                for rank, row in enumerate(rows[:TOP], 1):
                    conn.execute(
                        "INSERT INTO search_console_top (mode, session, kind, rank, key, clicks, impressions, position,"
                        " synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            scope.mode,
                            scope.session,
                            kind,
                            rank,
                            row.key,
                            row.clicks,
                            row.impressions,
                            row.position,
                            now,
                        ),
                    )
        if self.db.get_meta(meta_key(self.mode, "last_error")):
            events.record(self.db, "info", "search_console", "Search Console: reading works again")
        self.db.set_meta(meta_key(self.mode, "last_error"), "")
        self.db.set_meta(meta_key(self.mode, "last_sync_at"), now)
