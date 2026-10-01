"""Ember live on the owner's website (0.16.0): Ember's code takes a snapshot of its own numbers, renders the live page,
the home page's banner and the balance chart (products/live.py) and uploads them over the blog's SFTP login, every
UPLOAD_MINUTES while the app runs and at once when the life state changes or the owner changed what is shown.

The owner turns it on once (live_enabled) and chooses the parts (live_*); after that it needs no approval, because it
shows only what Ember's code makes itself: numbers from the ledger and the records, the titles of ventures and
milestones in which privacy.Masker finds nothing to mask (an address, a code, a link's token, a sender's name, a word
the owner removed), listings and posts that are public already (Etsy's listings only for 6 hours after they were read,
as Etsy's API terms allow), and the last will under the same check. Never an email, an order, a customer or the
agent's journal. Every file is checked again before it goes up (live.check), and only blog.LIVE_FILES are ever written.

Switched off again, Ember's code replaces the page and the banner with ones saying the live view is off (once). The
dry run uploads to the publisher's fake server: nothing leaves the app.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .. import events, privacy
from ..config import Settings
from ..economy.clock import from_iso, to_iso
from ..economy.costs import micros_to_usd
from ..economy.life import RUNWAY_CAP_DAYS
from ..products import live
from . import etsy, sftp, site_publisher

if TYPE_CHECKING:
    from ..agent.service import Agent

log = logging.getLogger(__name__)

RETRY_MINUTES = 30  # after a failed connection
ETSY_SHOWN_HOURS = 6  # Etsy's API terms: listings may be shown for 6 hours after they were read
MILESTONES_SHOWN = 5
VENTURES_SHOWN = 8
LISTINGS_SHOWN = 6
POSTS_SHOWN = 5


def key(mode: str, name: str) -> str:
    return f"integrations.live.{mode}.{name}"


def parts_of(settings: Settings) -> live.Parts:
    return live.Parts(
        banner=settings.live_banner,
        money=settings.live_show_money,
        revenue=settings.live_show_revenue,
        grants=settings.live_show_grants,
        chart=settings.live_show_chart,
        work=settings.live_show_work,
        shop=settings.live_show_shop,
        record=settings.live_show_record,
        memorial=settings.live_show_memorial,
    )


def problems(settings: Settings, mode: str) -> list[str]:
    """What keeps the live view from being uploaded (none: it can be)."""
    if not settings.live_enabled:
        return ["the live view is off (live_enabled)"]
    found = site_publisher.owner_of(settings).problems()
    if mode == "live":
        for name, value in (
            ("blog_sftp_host", settings.blog_sftp_host),
            ("blog_sftp_user", settings.blog_sftp_user),
            ("blog_sftp_password", settings.blog_sftp_password.get_secret_value()),
        ):
            if not value.strip():
                found.append(f"{name} is missing")
        try:
            site_publisher.login_of(settings)
        except ValueError as exc:
            found.append(str(exc))
    return found


def _options_fingerprint(settings: Settings) -> str:
    shown = parts_of(settings)
    text = json.dumps([shown.__dict__, settings.agent_name, settings.site_url, settings.site_name], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- the snapshot ----------------------------------------------------------------------------------------------------


def _masker(agent: Agent) -> privacy.Masker:
    """privacy.Masker as for the shareable diagnostics report: a text it changes isn't shown at all."""
    from .. import diagnostics  # the report's own lists of other people's words (it imports the whole app)

    own = getattr(agent.mailbox, "address", "") or agent.settings.email_address or ""
    with agent.db.connection() as conn:
        redactor = privacy.load(conn)
        others = {**diagnostics._others(conn, own), **diagnostics._owners(conn, agent.settings.owner_user_ids)}
    return privacy.Masker(own, redactor, False, others)


def _clean(masker: privacy.Masker, text: str, limit: int) -> str | None:
    """A text of the agent's for the public page, or None if Ember's code found something in it to mask."""
    text = " ".join(str(text or "").split()) if limit < 1000 else str(text or "").strip()
    if not text or masker(text) != text:
        return None
    return text[:limit]


def snapshot(agent: Agent) -> live.Snapshot:
    """Ember's numbers now, and the public parts of its records."""
    economy = agent.economy
    status = economy.life.evaluate()
    now = agent.clock.now()
    local = now.astimezone(agent.clock.tz)
    books_scope = economy.life.scope()
    scope = agent.scope()
    dead = status.state in ("dead", "ended")
    totals = economy.books.totals(books_scope, since=status.born_at, until=status.ended_at if dead else None)
    days = economy.books.day_series(books_scope)
    if status.born_at:  # the chart starts at its birth: no month of nothing before it
        born = from_iso(status.born_at).astimezone(agent.clock.tz).date().isoformat()
        days = [d for d in days if str(d["date"]) >= born]
    since_30 = to_iso(now - timedelta(days=30))
    revenue_30 = economy.books.totals(books_scope, since=since_30)["revenue"]
    runway = status.runway.days
    masker = _masker(agent)
    notes: list[str] = []
    ventures: list[tuple[str, str]] = []
    ideas = parked = cycles = cycles_today = 0
    milestones: list[tuple[str, str]] = []
    listings: list[live.Item] = []
    posts: list[live.Item] = []
    record: dict[str, Any] = {}
    will = ""
    life_no = 1
    with agent.db.connection() as conn:
        life_no = (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM lives WHERE mode = ? AND id <= ?", (agent.mode, status.life_id or 0)
                ).fetchone()[0]
            )
            or 1
        )
        life = status.life_id or 0
        cycles = int(conn.execute("SELECT COUNT(*) FROM cycles WHERE life_id = ?", (life,)).fetchone()[0])
        start_of_day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        cycles_today = int(
            conn.execute(
                "SELECT COUNT(*) FROM cycles WHERE life_id = ? AND started_at >= ?",
                (status.life_id or 0, to_iso(start_of_day)),
            ).fetchone()[0]
        )
        where, params = scope.where()
        for row in conn.execute(
            f"SELECT stage, title FROM ventures WHERE {where} AND life_id = ? ORDER BY updated_at DESC",
            (*params, scope.life_id),
        ):
            stage = str(row["stage"])
            if stage == "idea":
                ideas += 1
            elif stage in ("parked", "killed"):
                parked += 1
            elif stage in live.VENTURE_STAGES and len(ventures) < VENTURES_SHOWN:
                title = _clean(masker, row["title"], 80)
                if title is None:
                    notes.append("a venture's title")
                else:
                    ventures.append((stage, title))
        for row in conn.execute(
            f"SELECT title, due FROM milestones WHERE {where} AND life_id = ? AND status = 'open' ORDER BY due, id",
            (*params, scope.life_id),
        ):
            if len(milestones) >= MILESTONES_SHOWN:
                break
            title = _clean(masker, row["title"], 100)
            if title is None:
                notes.append("a milestone's title")
            else:
                milestones.append((title, str(row["due"])))
        fresh = to_iso(now - timedelta(hours=ETSY_SHOWN_HOURS))
        for row in conn.execute(
            f"SELECT listing_id, title, views, favorites FROM etsy_listings WHERE {where} AND status = 'active'"
            f" AND COALESCE(state, '{etsy.LIVE_STATE}') = '{etsy.LIVE_STATE}' AND listing_id IS NOT NULL"
            " AND synced_at >= ? ORDER BY id DESC LIMIT ?",
            (*params, fresh, LISTINGS_SHOWN),
        ):
            note = []
            if row["views"] is not None:
                note.append(f"{live.number(int(row['views']))} Aufrufe")
            if row["favorites"] is not None:
                note.append(f"{live.number(int(row['favorites']))} Favoriten")
            listings.append(live.Item(str(row["title"])[:140], etsy.listing_url(row["listing_id"]), ", ".join(note)))
        if agent.settings.blog_enabled:
            for row in site_publisher.posts(conn, scope)[:POSTS_SHOWN]:
                url = f"/blog/{row['slug']}.html"
                posts.append(live.Item(str(row["title"])[:140], url, live.blog.date_de(str(row["day"]))))
        settled = conn.execute(
            f"SELECT kind, probability, status FROM predictions WHERE {where} AND status IN ('hit', 'miss')", params
        ).fetchall()
        odds = [(float(r["probability"]), r["status"] == "hit") for r in settled if r["kind"] == "milestone"]
        sales = [r["status"] == "hit" for r in settled if r["kind"] == "first_sale"]
        if odds:
            record = {
                "met": sum(1 for _, hit in odds if hit),
                "settled": len(odds),
                "odds": sum(p for p, _ in odds) / len(odds),
                "brier": sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in odds) / len(odds),
            }
        record["sales_on_time"], record["sales_settled"] = sum(sales), len(sales)
        if dead and status.life_id:
            row = conn.execute("SELECT text FROM last_wills WHERE life_id = ?", (status.life_id,)).fetchone()
            if row is not None:
                cleaned = _clean(masker, row["text"], 6000)
                if cleaned is None:
                    notes.append("the last will")
                else:
                    will = cleaned
    age = None
    if status.born_at:
        end = from_iso(status.ended_at) if status.ended_at else now
        age = max(0.0, (end - from_iso(status.born_at)).total_seconds() / 86_400)
    return live.Snapshot(
        name=agent.settings.agent_name,
        state=status.state,
        at=local,
        simulated=agent.mode == "dry_run",
        life_no=life_no,
        age_days=age,
        balance=micros_to_usd(status.balance),
        runway_days=min(runway, RUNWAY_CAP_DAYS) if runway is not None else None,
        runway_capped=runway is not None and runway > RUNWAY_CAP_DAYS,
        today_spend=micros_to_usd(economy.books.cap_spend_on(books_scope, agent.clock.today())),
        daily_cap=float(agent.settings.daily_spend_cap_usd),
        api_cost=micros_to_usd(totals["api_cost"]),
        expenses=micros_to_usd(totals["expense"]),
        revenue=micros_to_usd(totals["revenue"]),
        revenue_30=micros_to_usd(revenue_30),
        grants=micros_to_usd(totals["grant"]),
        days=tuple(
            (str(d["date"]), float(d["balance_usd"]), float(d["revenue_usd"]), float(d["grant_usd"])) for d in days
        ),
        cycles=cycles,
        cycles_today=cycles_today,
        ventures=tuple(ventures),
        ideas=ideas,
        parked=parked,
        milestones=tuple(milestones),
        listings=tuple(listings),
        posts=tuple(posts),
        met=int(record.get("met", 0)),
        settled=int(record.get("settled", 0)),
        odds=float(record.get("odds", 0.0)),
        brier=float(record.get("brier", 0.0)),
        sales_on_time=int(record.get("sales_on_time", 0)),
        sales_settled=int(record.get("sales_settled", 0)),
        born_at=status.born_at,
        died_at=status.ended_at,
        will=will,
        notes=tuple(notes),
    )


# --- uploading -------------------------------------------------------------------------------------------------------


class LiveView:
    """Uploads the live view over the blog publisher's connection (one upload or check at a time)."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.blog = agent.blog
        self.mode = agent.mode

    def _meta(self, name: str) -> str:
        return self.agent.db.get_meta(key(self.mode, name)) or ""

    def _set(self, name: str, value: str) -> None:
        self.agent.db.set_meta(key(self.mode, name), value)

    def due(self) -> str | None:
        """Why the live view is uploaded now (None: not yet): the first time, after UPLOAD_MINUTES, when the life
        state or the owner's choice of parts changed, or (switched off) once to say so."""
        settings = self.agent.settings
        now = self.agent.clock.now()
        error_at = self._meta("error_at")
        if error_at and now - from_iso(error_at) < timedelta(minutes=RETRY_MINUTES):
            return None
        if not settings.live_enabled:
            return "off" if self._meta("shown") == "1" else None
        last = self._meta("uploaded_at")
        if not last or self._meta("shown") != "1":
            return "first"
        if self._meta("options") != _options_fingerprint(settings):
            return "options"
        if self._meta("state") != self.agent.economy.life.evaluate().state:
            return "state"
        if now - from_iso(last) >= timedelta(minutes=live.UPLOAD_MINUTES):
            return "schedule"
        return None

    def files(self) -> dict[str, bytes]:
        """The files as they would go up now (the dashboard's preview, and the upload)."""
        settings = self.agent.settings
        owner = site_publisher.owner_of(settings)
        if not settings.live_enabled:
            name = settings.agent_name
            return {live.PAGE: live.render_off(owner, name), live.BANNER: live.render_banner_off(name)}
        return live.render(snapshot(self.agent), parts_of(settings), owner)

    def run(self) -> str | None:
        """Upload the live view if it is due; returns what happened (None: nothing was due)."""
        why = self.due()
        if why is None:
            return None
        settings = self.agent.settings
        trouble = [p for p in problems(settings, self.mode) if why != "off" or "live_enabled" not in p]
        if trouble:
            return self._failed("; ".join(trouble), quiet=True)
        try:
            files = self.files()
        except Exception as exc:  # noqa: BLE001 - the live view must never stop the scheduler
            log.exception("Making the live view failed")
            return self._failed(f"the live view couldn't be made ({type(exc).__name__})")
        found = live.check(files, site_publisher.owner_of(settings))
        if found:
            return self._failed("a file didn't pass the check: " + "; ".join(found))
        try:
            simulated = self.blog.put(files)
        except sftp.SftpError as exc:
            return self._failed(str(exc))
        if simulated is None:
            return None  # the blog is uploading: the next round
        return self._done(why, files, simulated=simulated)

    def _done(self, why: str, files: dict[str, bytes], simulated: bool) -> str:
        now = to_iso(self.agent.clock.now())
        self._set("uploaded_at", now)
        self._set("error", "")
        self._set("error_at", "")
        if why == "off":
            self._set("shown", "0")
            note = "The live view is off: Ember's code uploaded a page saying so"
            events.record(self.agent.db, "info", "website", note)
            return "off"
        first = self._meta("shown") != "1"
        self._set("shown", "1")
        self._set("options", _options_fingerprint(self.agent.settings))
        self._set("state", self.agent.economy.life.evaluate().state)
        self._set("files", json.dumps(sorted(files)))
        if first:
            where = "the dry run's fake server" if simulated else site_publisher.owner_of(self.agent.settings).url
            events.record(
                self.agent.db, "info", "website", f"The live view is on: uploaded {', '.join(sorted(files))} to {where}"
            )
        return "done"

    def _failed(self, error: str, quiet: bool = False) -> str:
        """Kept for the dashboard; an event only when the error is new (a missing option says so once)."""
        if self._meta("error") != error[:500] and not quiet:
            events.record(self.agent.db, "warning", "website", f"The live view wasn't uploaded: {error}"[:300])
        self._set("error", error[:500])
        self._set("error_at", to_iso(self.agent.clock.now()))
        return "failed"

    def describe(self) -> dict[str, Any]:
        """The dashboard's Live view section (never the password)."""
        settings = self.agent.settings
        if not settings.live_enabled and self._meta("shown") != "1":
            return {"status": "disabled"}
        trouble = problems(settings, self.mode) if settings.live_enabled else []
        uploaded = self._meta("uploaded_at") or None
        owner = site_publisher.owner_of(settings)
        shown = parts_of(settings)
        return {
            "status": "off" if not settings.live_enabled else "not_ready" if trouble else "ok",
            "reason": "; ".join(trouble) or None,
            "simulated": self.mode == "dry_run",
            "url": f"{owner.url}/{live.PAGE}" if owner.url else None,
            "uploaded_at": uploaded,
            "every_minutes": live.UPLOAD_MINUTES,
            "last_error": self._meta("error") or None,
            "parts": {name: bool(value) for name, value in shown.__dict__.items()},
            "snippet": live.snippet(settings.agent_name) if shown.banner else None,
        }
