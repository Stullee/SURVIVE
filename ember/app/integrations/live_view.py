"""Ember live on the owner's website (0.16.0): Ember's code takes a snapshot of its own numbers, renders the live page,
the home page's banner and the balance chart (products/live.py) and uploads them over the blog's SFTP login, every
UPLOAD_MINUTES while the app runs and at once when the life state changes or the owner changed what is shown.

The owner turns it on once (live_enabled) and chooses the parts (live_*); after that the numbers need no approval:
they come from the ledger and the records, and the listings and posts shown are public already (Etsy's listings only
for 6 hours after they were read, as Etsy's API terms allow). The agent's own words are another matter. 0.16.1 showed
them whenever privacy.Masker found nothing to mask, and phone numbers, links, names and IBANs went up. Now:

* a venture's or a milestone's title is shown only once the owner approved that very text on the dashboard's Live
  view card (``LiveView.decide``; their word is kept as a digest of the text, never the text). Until then, and once
  they keep it off, the page only counts it;
* the last will is shown only once the owner approved it in a request of its own, which Ember's code makes after
  Ember died (executor live_will: owner-only, so an unlock never approves it), and only as approved;
* neither ever with an @, a web address, an IBAN, a phone number or a number of 5 digits (privacy.unpublishable),
  what the Masker masks, or a secret the logs hide (logging_setup.redact).

Never an email, an order, a customer or the agent's journal. Every file is checked again before it goes up
(live.check), and only blog.LIVE_FILES are ever written. A file the owner switched off is replaced by one saying so,
and switched off as a whole, the page, the banner and the chart say the live view is off (once). The dry run uploads to
the publisher's fake server: nothing leaves the app.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .. import events, privacy
from ..agent import store
from ..agent.store import AgentScope
from ..config import Settings
from ..economy.clock import from_iso, to_iso
from ..economy.costs import micros_to_usd
from ..economy.life import RUNWAY_CAP_DAYS
from ..logging_setup import redact
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
TITLE_MAX = {"venture": 80, "milestone": 100}
WILL_MAX = 6000
WILL = "live_will"  # the executor of the owner's request to show the last will (owner-only: never an unlock's)
APPROVED = ("approved", "approved_with_changes", "done")
DECISIONS_KEPT = 300  # the owner's words on titles kept, the newest
CARD_SECONDS = 60  # how long the Live view card's titles are reused (finding them reads every email's sender)
MASKED = "what the diagnostics report masks (an address, a code, a link's token, a word you removed, an email's sender)"


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


# --- the agent's words ---------------------------------------------------------------------------------------------


def _masker(agent: Agent) -> privacy.Masker:
    """privacy.Masker as for the shareable diagnostics report: a text it changes isn't shown at all."""
    from .. import diagnostics  # the report's own lists of other people's words (it imports the whole app)

    own = getattr(agent.mailbox, "address", "") or agent.settings.email_address or ""
    with agent.db.connection() as conn:
        redactor = privacy.load(conn)
        others = {**diagnostics._others(conn, own), **diagnostics._owners(conn, agent.settings.owner_user_ids)}
    return privacy.Masker(own, redactor, False, others)


def public(masker: privacy.Masker, text: str, limit: int) -> tuple[str, str | None]:
    """A text of the agent's as the public page would show it (its invisible characters out, a title on one line), and
    what keeps it off the page whatever the owner says (None: nothing). 0.16.1 checked only what the Masker changes,
    and not what the logs hide (redact): a registered secret in a title would have been uploaded."""
    shown = privacy.visible(str(text or ""))
    shown = (" ".join(shown.split()) if limit < 1000 else shown.strip())[:limit]
    if not shown:
        return "", "only characters a reader can't see"
    why = privacy.unpublishable(shown)
    for form in (shown, privacy.folded(shown)):
        if why is None and masker(form) != form:
            why = MASKED
        if why is None and redact(form) != form:
            why = "a secret (a key or a password)"
    return shown, why


def text_id(kind: str, text: str) -> str:
    """How the owner's word on a title is kept: a digest of the title as shown (never its text)."""
    return hashlib.sha256(f"{kind}\n{text}".encode()).hexdigest()


def decisions(raw: str | None) -> dict[str, dict[str, Any]]:
    """The owner's words on titles, by text_id: {"shown": True (approved) or False (kept off), "at": when}."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Title:
    """A venture's or a milestone's title as the page would show it, and its state: shown (the owner approved it),
    waiting (for the owner), off (the owner keeps it off) or refused (``why``: Ember's code keeps it off)."""

    kind: str  # venture or milestone
    ref: int  # its number
    text: str
    note: str  # the venture's stage, or the milestone's due day
    state: str
    why: str | None = None

    @property
    def id(self) -> str:
        return text_id(self.kind, self.text)

    def json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "ref": self.ref,
            "text": self.text,
            "note": self.note,
            "state": self.state,
            "why": self.why,
        }


@dataclass(frozen=True)
class Work:
    """The titles the work part would show (the ventures', the latest first, then the next milestones'), and how many
    there are of each."""

    titles: tuple[Title, ...] = ()
    ventures: int = 0  # being worked on (live.VENTURE_STAGES)
    ideas: int = 0
    parked: int = 0
    milestones: int = 0  # open


def work(conn: sqlite3.Connection, agent: Agent, masker: privacy.Masker, scope: AgentScope) -> Work:
    decided = decisions(agent.db.get_meta(key(agent.mode, "texts")))

    def title(kind: str, ref: int, raw: str, note: str) -> Title:
        text, why = public(masker, raw, TITLE_MAX[kind])
        word = decided.get(text_id(kind, text), {}).get("shown")
        state = "refused" if why else "shown" if word is True else "off" if word is False else "waiting"
        return Title(kind, ref, text, note, state, why)

    where, params = scope.where()
    found = []
    ventures = ideas = parked = milestones = 0
    for row in conn.execute(
        f"SELECT id, stage, title FROM ventures WHERE {where} AND life_id = ? ORDER BY updated_at DESC, id DESC",
        (*params, scope.life_id),
    ):
        stage = str(row["stage"])
        if stage == "idea":
            ideas += 1
        elif stage in ("parked", "killed"):
            parked += 1
        elif stage in live.VENTURE_STAGES:
            ventures += 1
            if ventures <= VENTURES_SHOWN:
                found.append(title("venture", int(row["id"]), row["title"], stage))
    for row in conn.execute(
        f"SELECT id, title, due FROM milestones WHERE {where} AND life_id = ? AND status = 'open' ORDER BY due, id",
        (*params, scope.life_id),
    ):
        milestones += 1
        if milestones <= MILESTONES_SHOWN:
            found.append(title("milestone", int(row["id"]), row["title"], str(row["due"])))
    return Work(tuple(found), ventures, ideas, parked, milestones)


def will_request(conn: sqlite3.Connection, scope: AgentScope, life_id: int) -> sqlite3.Row | None:
    """The newest request asking the owner to show this life's last will on the live page."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM approvals WHERE {where} AND executor = ? AND json_extract(action, '$.life_id') = ?"
        " ORDER BY id DESC LIMIT 1",
        (*params, WILL, life_id),
    ).fetchone()


def execution(row: sqlite3.Row) -> dict[str, Any] | None:
    """What happened to the owner's approved request for the last will, for the dashboard (None before approval):
    waiting for the next upload, done once it is on the page, or cancelled by the owner."""
    if row["status"] in ("approved", "approved_with_changes"):
        status = "waiting"
    elif row["status"] in ("done", "failed"):
        status = str(row["status"])
    else:
        return None
    return {
        "status": status,
        "started_at": None,
        "finished_at": row["closed_at"],
        "result": row["result_note"],
        "error": None,
        "url": row["result_link"],
    }


def _approved(request: sqlite3.Row | None, text: str) -> bool:
    """Whether the owner approved exactly this text of the last will (as it is: never with changes)."""
    if request is None or request["status"] not in APPROVED or request["final_payload"] is not None:
        return False
    try:
        action = json.loads(request["action"])
    except ValueError:
        return False
    return isinstance(action, dict) and action.get("sha256") == store.sha256(text)


# --- the snapshot ----------------------------------------------------------------------------------------------------


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
        doing = work(conn, agent, masker, scope)
        ventures = [(t.note, t.text) for t in doing.titles if t.kind == "venture" and t.state == "shown"]
        milestones = [(t.text, t.note) for t in doing.titles if t.kind == "milestone" and t.state == "shown"]
        notes += [f"a {t.kind}'s title" for t in doing.titles if t.state != "shown"]
        fresh = to_iso(now - timedelta(hours=ETSY_SHOWN_HOURS))
        for row in conn.execute(
            f"SELECT listing_id, title, views, favorites FROM etsy_listings WHERE {where} AND status = 'active'"
            f" AND COALESCE(state, '{etsy.LIVE_STATE}') = '{etsy.LIVE_STATE}' AND listing_id IS NOT NULL"
            " AND synced_at >= ? ORDER BY id DESC LIMIT ?",
            (*params, fresh, LISTINGS_SHOWN),
        ):
            views, favorites = row["views"], row["favorites"]
            listings.append(
                live.Item(
                    str(row["title"])[:140],
                    etsy.listing_url(row["listing_id"]),
                    int(views) if views is not None else None,
                    int(favorites) if favorites is not None else None,
                )
            )
        if agent.settings.blog_enabled:
            for row in site_publisher.posts(conn, scope)[:POSTS_SHOWN]:
                url = f"/blog/{row['slug']}.html"
                posts.append(live.Item(str(row["title"])[:140], url, day=str(row["day"])))
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
            row = store.last_will(conn, status.life_id)
            if row is not None:  # 0.16.1 showed it unasked: only as the owner approved it
                text, why = public(masker, row["text"], WILL_MAX)
                if why is None and _approved(will_request(conn, scope, status.life_id), text):
                    will = text
                else:
                    notes.append("the last will")
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
        ventures_more=doing.ventures - len(ventures),
        ideas=doing.ideas,
        parked=doing.parked,
        milestones=tuple(milestones),
        milestones_more=doing.milestones - len(milestones),
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


@dataclass(frozen=True)
class Made:
    """The files as they would go up now: which of them show the live view (the others say a part is off), whether
    they show the last will, and what the owner had decided when they were made (``fingerprint``)."""

    files: dict[str, bytes]
    shown: tuple[str, ...] = ()
    will: bool = False
    fingerprint: str = ""


class LiveView:
    """Uploads the live view over the blog publisher's connection (one upload or check at a time)."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.blog = agent.blog
        self.mode = agent.mode
        self._card: tuple[float, Work] | None = None  # the Live view card's titles, and when they were found

    def _meta(self, name: str) -> str:
        return self.agent.db.get_meta(key(self.mode, name)) or ""

    def _set(self, name: str, value: str) -> None:
        self.agent.db.set_meta(key(self.mode, name), value)

    def _fingerprint(self) -> str:
        """What the owner decides about the page: the parts, the names and the address, and their words on titles and
        on the last will (a change uploads the page at once)."""
        settings = self.agent.settings
        with self.agent.db.connection() as conn:
            where, params = self.agent.scope().where()
            will = conn.execute(
                f"SELECT id, status FROM approvals WHERE {where} AND executor = ? ORDER BY id DESC LIMIT 1",
                (*params, WILL),
            ).fetchone()
        approved = int(will["id"]) if will is not None and will["status"] in APPROVED else None
        text = json.dumps(
            [parts_of(settings).__dict__, settings.agent_name, settings.site_url, settings.site_name]
            + [self._meta("texts"), approved],
            sort_keys=True,
        )
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _on_server(self) -> set[str]:
        """The files of an earlier upload that show the live view (not its version saying a part is off). 0.16.1
        didn't keep them: after the upgrade, any of the live view's files may be up."""
        raw = self._meta("on_server")
        if raw:
            try:
                return {str(path) for path in json.loads(raw)}
            except (TypeError, ValueError):
                pass
        return set(live.FILES) if self._meta("uploaded_at") else set()

    def due(self) -> str | None:
        """Why the live view is uploaded now (None: not yet): the first time, after UPLOAD_MINUTES, when the life
        state or the owner's choice of parts or their word on a text changed, or (switched off) once to say so."""
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
        if self._meta("options") != self._fingerprint():
            return "options"
        if self._meta("state") != self.agent.economy.life.evaluate().state:
            return "state"
        if now - from_iso(last) >= timedelta(minutes=live.UPLOAD_MINUTES):
            return "schedule"
        return None

    def make(self) -> Made:
        """The files as they would go up now (the dashboard's preview, and the upload): the parts the owner shows, and
        in place of a file switched off since it went up its version saying so."""
        settings = self.agent.settings
        owner = site_publisher.owner_of(settings)
        fingerprint = self._fingerprint()
        if not settings.live_enabled:
            return Made(live.render_all_off(owner, settings.agent_name), fingerprint=fingerprint)
        s = snapshot(self.agent)
        parts = parts_of(settings)
        files = live.render(s, parts, owner, self._on_server())
        shown = tuple(live.shown_paths(s, parts))
        return Made(files, shown, bool(s.will) and parts.memorial and live.dead(s), fingerprint)

    def files(self) -> dict[str, bytes]:
        return self.make().files

    def ask(self) -> int | None:
        """Ask the owner, once, to approve the last will for the live page: once Ember is dead, while the memorial is
        shown, unless something in the will keeps it off a public page. The new request (None: none was made)."""
        settings = self.agent.settings
        if not (settings.live_enabled and settings.live_show_memorial):
            return None
        status = self.agent.economy.life.evaluate()
        if status.state not in ("dead", "ended") or not status.life_id:
            return None
        scope = self.agent.scope()
        with self.agent.db.connection() as conn:
            row = store.last_will(conn, status.life_id)
            if row is None or will_request(conn, scope, status.life_id) is not None:
                return None
        text, why = public(_masker(self.agent), row["text"], WILL_MAX)
        if why is not None:
            return None
        name = settings.agent_name
        died = f" on {status.ended_at[:10]}" if status.ended_at else ""
        with self.agent.db.transaction() as conn:
            if will_request(conn, scope, status.life_id) is not None:
                return None
            made = store.insert_approval(
                conn,
                scope,
                int(row["cycle_id"]),
                to_iso(self.agent.clock.now()),
                type="publish",
                title=f"Live page: {name}'s last will"[:120],
                description=(
                    f"{name} died{died}. Ember's code shows its last will on your live page (live.html and "
                    "en/live.html) only if you approve it, exactly as below, and only while live_show_memorial is on. "
                    f"Until then, and if you reject it, the memorial shows {name}'s life in numbers without it. No "
                    "unlock approves this: only you do."
                )[:2000],
                payload=text,
                expected_cost="none",
                expected_benefit=f"Readers of your live page read {name}'s last words."[:300],
                executor=WILL,
                action=store.canonical({"life_id": status.life_id, "sha256": store.sha256(text)}),
            )
        note = f"Request #{made}: {name}'s last will waits for you to show it on the live page"
        events.record(self.agent.db, "info", "website", note)
        return made

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
            self.ask()
            made = self.make()
        except Exception as exc:  # noqa: BLE001 - the live view must never stop the scheduler
            log.exception("Making the live view failed")
            return self._failed(f"the live view couldn't be made ({type(exc).__name__})")
        found = live.check(made.files, site_publisher.owner_of(settings))
        if found:
            return self._failed("a file didn't pass the check: " + "; ".join(found))
        try:
            simulated = self.blog.put(made.files)
        except sftp.SftpError as exc:
            return self._failed(str(exc))
        except Exception as exc:  # noqa: BLE001 - 0.16.1: what escaped here was tried again every round, unbounded
            log.exception("Uploading the live view failed")
            return self._failed(f"the upload failed ({type(exc).__name__})")
        if simulated is None:
            return None  # the blog is uploading: the next round
        return self._done(why, made, simulated=simulated)

    def _done(self, why: str, made: Made, simulated: bool) -> str:
        now = to_iso(self.agent.clock.now())
        self._set("uploaded_at", now)
        self._set("error", "")
        self._set("error_at", "")
        self._set("on_server", json.dumps(list(made.shown)))
        if why == "off":
            self._set("shown", "0")
            note = "The live view is off: Ember's code uploaded a page saying so"
            events.record(self.agent.db, "info", "website", note)
            return "off"
        first = self._meta("shown") != "1"
        self._set("shown", "1")
        self._set("options", made.fingerprint)
        self._set("state", self.agent.economy.life.evaluate().state)
        self._set("files", json.dumps(sorted(made.files)))
        if made.will:
            self._will_shown(now, simulated)
        if first:
            where = "the dry run's fake server" if simulated else site_publisher.owner_of(self.agent.settings).url
            note = f"The live view is on: uploaded {', '.join(sorted(made.files))} to {where}"
            events.record(self.agent.db, "info", "website", note)
        return "done"

    def _will_shown(self, now: str, simulated: bool) -> None:
        """The owner's approved request for the last will is carried out once it is on the page."""
        url = f"{site_publisher.owner_of(self.agent.settings).url}/{live.PAGE}"
        link = None if simulated else url
        note = (
            "Shown on the dry run's fake server's live page; nothing reached your website."
            if simulated
            else f"Shown on your live page: {url}"
        )
        scope = self.agent.scope()
        where, params = scope.where()
        with self.agent.db.transaction() as conn:
            ids = [
                int(r["id"])
                for r in conn.execute(
                    f"SELECT id FROM approvals WHERE {where} AND executor = ? AND json_extract(action, '$.life_id') = ?"
                    " AND status IN ('approved', 'approved_with_changes')",
                    (*params, WILL, scope.life_id),
                )
            ]
            for approval_id in ids:
                conn.execute(
                    "UPDATE approvals SET status = 'done', closed_at = ?, closed_by = ?, result_note = ?,"
                    " result_link = ?, version = version + 1, seen_cycle_id = NULL WHERE id = ?"
                    " AND status IN ('approved', 'approved_with_changes')",
                    (now, site_publisher.CLOSED_BY, note, link, approval_id),
                )
        for approval_id in ids:
            events.record(self.agent.db, "info", "website", f"Request #{approval_id}: {note}"[:300])

    def _failed(self, error: str, quiet: bool = False) -> str:
        """Kept for the dashboard; an event only when the error is new (a missing option says so once)."""
        if self._meta("error") != error[:500] and not quiet:
            events.record(self.agent.db, "warning", "website", f"The live view wasn't uploaded: {error}"[:300])
        self._set("error", error[:500])
        self._set("error_at", to_iso(self.agent.clock.now()))
        return "failed"

    # --- the owner's word on the agent's titles ---

    def titles(self, fresh: bool = False) -> Work:
        """The titles the work part would show, with the owner's word on each (reused for CARD_SECONDS)."""
        card = self._card
        if not fresh and card is not None and time.monotonic() - card[0] < CARD_SECONDS:
            return card[1]
        masker = _masker(self.agent)
        with self.agent.db.connection() as conn:
            found = work(conn, self.agent, masker, self.agent.scope())
        self._card = (time.monotonic(), found)
        return found

    def decide(self, text_id: str, show: bool, who: str | None) -> dict[str, Any]:
        """The owner's word on a title: shown from the next upload on (at once), or kept off. Only a title the page
        would show now (LookupError: it changed meanwhile), and never one Ember's code keeps off (ValueError)."""
        found = {t.id: t for t in self.titles(fresh=True).titles}
        title = found.get(text_id)
        if title is None:
            raise LookupError("this title isn't one the live page would show now (it changed meanwhile): reload")
        if show and title.why is not None:
            raise ValueError(f"Ember's code keeps this title off the page: it holds {title.why}")
        with self.agent.db.transaction():
            decided = decisions(self._meta("texts"))
            decided[text_id] = {"shown": show, "at": to_iso(self.agent.clock.now())}
            newest = sorted(decided.items(), key=lambda item: str(item[1].get("at")), reverse=True)[:DECISIONS_KEPT]
            self._set("texts", json.dumps(dict(newest), sort_keys=True))
            events.record(
                self.agent.db,
                "info",
                "owner",
                f"{who or 'The owner'} {'showed' if show else 'kept off'} {title.kind} #{title.ref}'s title on the live"
                " page",
            )
        self._card = None
        return self.describe()

    def describe(self) -> dict[str, Any]:
        """The dashboard's Live view section (never the password)."""
        settings = self.agent.settings
        if not settings.live_enabled and self._meta("shown") != "1":
            return {"status": "disabled"}
        trouble = problems(settings, self.mode) if settings.live_enabled else []
        uploaded = self._meta("uploaded_at") or None
        owner = site_publisher.owner_of(settings)
        shown = parts_of(settings)
        titles = [t.json() for t in self.titles().titles] if settings.live_enabled and shown.work else []
        return {
            "status": "off" if not settings.live_enabled else "not_ready" if trouble else "ok",
            "reason": "; ".join(trouble) or None,
            "simulated": self.mode == "dry_run",
            "url": f"{owner.url}/{live.PAGE}" if owner.url else None,
            "url_en": f"{owner.url}/{live.PAGE_EN}" if owner.url else None,
            "uploaded_at": uploaded,
            "every_minutes": live.UPLOAD_MINUTES,
            "last_error": self._meta("error") or None,
            "parts": {name: bool(value) for name, value in shown.__dict__.items()},
            "snippet": live.snippet(settings.agent_name) if shown.banner else None,
            "snippet_en": live.snippet(settings.agent_name, "en") if shown.banner else None,
            "titles": titles,  # the agent's titles the work part would show, and the owner's word on each
            "will": self._will_card() if settings.live_enabled and shown.memorial else None,
        }

    def _will_card(self) -> dict[str, Any] | None:
        """The last will's request, once Ember is dead (None: no will to show)."""
        status = self.agent.economy.life.evaluate()
        if status.state not in ("dead", "ended") or not status.life_id:
            return None
        with self.agent.db.connection() as conn:
            if store.last_will(conn, status.life_id) is None:
                return None
            row = will_request(conn, self.agent.scope(), status.life_id)
        if row is None:
            return {"approval_id": None, "status": None}
        return {"approval_id": int(row["id"]), "status": str(row["status"])}
