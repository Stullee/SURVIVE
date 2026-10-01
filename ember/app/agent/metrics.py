"""The metric catalogue (0.12.0): what a milestone can be measured by, so that Ember's code checks it.

A milestone was closed "done" on the agent's word: live, "3 listings live" was closed with "Done." while none existed.
Now a milestone can name a metric from this catalogue and a target (listings_live, 3). Ember's code reads the metric
from its records after each Etsy sync and before every plan (``grade``), with no model call. It closes the milestone
done once the target is met, with the numbers as its evidence, and missed once its date has passed without it; a
ceiling (api_spend_usd) is missed once it is passed and done at its date. The agent can't close a metric milestone
done: the tool refuses, and so does the database (migration 0029). Milestones without a metric stay allowed; the
agent's done on one is shown as self-reported.

Each metric says what it counts and in what unit, where its numbers come from (its trust label: Etsy, as the last sync
read it; the owner's records; Ember's own records), how fresh Etsy's numbers must be to count (FRESH_HOURS, from a
sync after the milestone was set), and the sample below which a miss says there was too little to judge. A milestone
linked to a project or venture counts only what belongs to it: a listing belongs to the project of the request that
created it (or of that request's cycle), and so to that project's venture (or the cycle's).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import events
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from ..integrations import etsy, etsy_publisher, mailstore, pinterest_publisher, printify_publisher, qa
from . import ventures
from .store import AgentScope

FRESH_HOURS = 3  # Etsy's numbers count while the last sync is at most this old (it runs hourly)
MAX_COUNT = 1_000_000
MAX_USD = Decimal("100000")
MICROS = 1_000_000
# A venture's stages in the order they are reached; parked and killed aren't reached.
STAGES = ("idea", "researching", "proposed", "building", "live")
SOURCES = {
    "etsy": "Etsy",
    "owner": "your owner's records",
    "ember": "Ember's records",
    "pinterest": "Pinterest",
    "printify": "Printify",
}


@dataclass(frozen=True)
class Metric:
    name: str
    what: str  # what it counts, for the agent and the owner
    kind: str  # "count", "usd", "yes" (met or not) or "stage"
    source: str  # where its numbers come from, a key of SOURCES: its trust label
    unit: str = ""  # a count's unit, singular
    since_set: bool = False  # counts what happened since the milestone was set (else: how things are now)
    ceiling: bool = False  # met while not above the target, at its date; missed once above it
    venture: bool = False  # measures a venture: the milestone must name one
    history: bool = False  # needs the owner's etsy_stats_history (views and favorites kept over time)
    code_only: bool = False  # 0.13.0: only Ember's code sets milestones with it (a listing test's bar: agent/gates.py)
    sample: str = ""  # what a miss is judged against: below min_sample, too little to judge
    min_sample: int = 0

    @property
    def etsy(self) -> bool:
        return self.source == "etsy"


CATALOGUE: dict[str, Metric] = {
    m.name: m
    for m in (
        Metric("listings_live", "your listings live on Etsy now", "count", "etsy", unit="listing"),
        Metric(
            "views_delta",
            "views your listings gained since it was set",
            "count",
            "etsy",
            unit="view",
            since_set=True,
            history=True,
        ),
        Metric(
            "favorites_delta",
            "favorites your listings gained since it was set",
            "count",
            "etsy",
            unit="favorite",
            since_set=True,
            history=True,
            sample="views",
            min_sample=100,
        ),
        Metric(
            "orders_observed",
            "Etsy orders of your listings since it was set",
            "count",
            "etsy",
            unit="order",
            since_set=True,
            sample="views",
            min_sample=200,
        ),
        Metric(
            "revenue_verified_usd",
            "revenue less expenses recorded since it was set",
            "usd",
            "owner",
            since_set=True,
        ),
        Metric(
            "research_calls_ok",
            "research calls that found something since it was set",
            "count",
            "ember",
            unit="call",
            since_set=True,
        ),
        Metric("case_complete", "the venture's business case is complete", "yes", "ember", venture=True),
        Metric("stage_reached", "the stage the venture reached", "stage", "ember", venture=True),
        Metric(
            "api_spend_usd",
            "API spending since it was set, at most the target",
            "usd",
            "ember",
            since_set=True,
            ceiling=True,
        ),
        # 0.13.0 (Phase E1): inbound email, Ember's records (a list's or a machine's mail isn't counted)
        Metric(
            "inquiries_received",
            "emails people wrote to you since it was set",
            "count",
            "ember",
            unit="email",
            since_set=True,
        ),
        Metric(
            "inquiries_answered",
            "of those emails, the ones you answered (an email sent)",
            "count",
            "ember",
            unit="email",
            since_set=True,
        ),
        # 0.13.0 (Phase E2): Ember's pins, as the last Pinterest sync read them
        Metric("pins_live", "your pins live on Pinterest now", "count", "pinterest", unit="pin"),
        Metric("pin_clicks", "clicks your pins brought to their links, in all", "count", "pinterest", unit="click"),
        # 0.13.0 (Phase E4): Ember's Printify products, as the last Printify sync read them
        Metric("pod_products_live", "your Printify products in the shop now", "count", "printify", unit="product"),
        Metric("pod_orders", "orders of your Printify products, in all", "count", "printify", unit="order"),
        # 0.13.0: a product line's listing test (agent/gates.py): its listings' views and favorites in all, as the last
        # sync read them (Etsy's own counts: no history kept). 0.14.0: the agent sets them too (its views goals were
        # self-reported), refused when met already; orders_total stays Ember's code's (orders_observed is the agent's)
        Metric("views_total", "views your listings have had in all", "count", "etsy", unit="view"),
        Metric("favorites_total", "favorites your listings have in all", "count", "etsy", unit="favorite"),
        Metric("orders_total", "Etsy orders of your listings, in all", "count", "etsy", unit="order", code_only=True),
        Metric(
            "qa_clean",
            f"each live listing has at least {qa.MIN_PHOTOS} photos",
            "yes",
            "etsy",
            sample="live listings",
            min_sample=1,
        ),
    )
}
NAMES = tuple(name for name, m in CATALOGUE.items() if not m.code_only)  # the ones the agent can set
# The catalogue in the tool's words, as short as it can be: every request of a work step carries it.
HELP = (
    "listings_live, views_total and favorites_total count now; the deltas, orders_observed, revenue_verified_usd "
    "(recorded revenue less expenses), research_calls_ok (found something), inquiries_received and "
    "inquiries_answered (people's emails; your answers) and api_spend_usd (a ceiling) count from when it is set; "
    f"case_complete and stage_reached are a venture's; qa_clean: {qa.MIN_PHOTOS}+ photos on each live "
    "listing"
)
# 0.13.0: a channel's metrics, offered with the channel (tools.definitions), and their words for HELP.
CHANNEL_METRICS = {
    "pinterest": (("pins_live", "pin_clicks"), "pins_live and pin_clicks: Pinterest, now"),
    "printify": (("pod_products_live", "pod_orders"), "pod_products_live and pod_orders: Printify, now"),
}


def help_text(off: frozenset[str] | set[str] = frozenset()) -> str:
    """HELP with the metrics of the channels a cycle has (``off``: the channels it hasn't)."""
    return "; ".join([HELP, *(words for channel, (_, words) in CHANNEL_METRICS.items() if channel not in off)])


class TargetError(ValueError):
    """A target that doesn't fit its metric, in words the agent can act on."""


def parse_target(m: Metric, text: Any) -> int:
    """A target in the metric's unit: a count, micros for USD, a stage's rank, or 1 for met."""
    raw = " ".join(str(text if text is not None else "").split()).lower()
    if m.kind == "yes":
        if raw in ("", "1", "yes", "true"):
            return 1
        raise TargetError(f"{m.name} is met or not: leave target out")
    if m.kind == "stage":
        if raw in STAGES[1:]:
            return STAGES.index(raw)
        raise TargetError(f"target for {m.name} is a stage: {', '.join(STAGES[1:])}")
    if not raw:
        raise TargetError(f"give target: {'the amount in USD' if m.kind == 'usd' else f'how many {m.unit}s'}")
    if m.kind == "usd":
        try:
            amount = Decimal(raw.removeprefix("$").removesuffix("usd").strip().replace(",", "."))
        except InvalidOperation:
            raise TargetError("target is an amount in USD, e.g. 12.50") from None
        if not amount.is_finite() or amount <= 0 or amount > MAX_USD or amount != amount.quantize(Decimal("0.01")):
            raise TargetError(f"target is an amount in USD from 0.01 to {MAX_USD:,}, in cents at most")
        return int(amount * MICROS)
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_COUNT:
        raise TargetError(f"target is a whole number of {m.unit}s, from 1 to {MAX_COUNT:,}")
    return int(raw)


def amount(m: Metric, value: int) -> str:
    """A value of the metric in words: "3 listings", "$1.20", "proposed", "yes"."""
    if m.kind == "usd":
        return f"${value / MICROS:,.2f}"
    if m.kind == "stage":
        return STAGES[value] if 0 <= value < len(STAGES) else "parked or killed"
    if m.kind == "yes":
        return "yes" if value else "no"
    return f"{value:,} {m.unit}{'' if value == 1 else 's'}"


def target_text(m: Metric, target: int) -> str:
    if m.kind == "yes":
        return "yes"
    if m.kind == "stage":
        return f"{amount(m, target)} or later"
    return f"{'at most' if m.ceiling else 'at least'} {amount(m, target)}"


def _of(project_id: int | None, venture_id: int | None) -> str:
    if project_id:
        return f" (project #{project_id})"
    return f" (venture #{venture_id})" if venture_id else ""


def measure_text(m: Metric, target: int, project_id: int | None, venture_id: int | None) -> str:
    """The measure Ember's code writes for a metric milestone the agent gave none."""
    what = m.what[0].upper() + m.what[1:]
    if m.venture:
        what = what.replace("the venture", f"venture #{venture_id}")
        return f"{what}: {target_text(m, target)} (Ember's code checks it)"
    return f"{what}{_of(project_id, venture_id)}: {target_text(m, target)} (Ember's code checks it)"


def progress_text(row: Mapping[str, Any]) -> str:
    """A metric milestone's metric, target and where it stands ("" without a metric), for the Roadmap tab."""
    m = CATALOGUE.get(_column(row, "metric") or "")
    if m is None:
        return ""
    text = f"{m.name} {target_text(m, int(row['target']))}"
    if _column(row, "progress") is None:
        return text + ("; not checked yet" if _column(row, "status") in ("open", None) else "")
    return text + f"; now {amount(m, int(row['progress']))} ({SOURCES[m.source]}, {str(row['checked_at'])[:10]})"


def status_text(row: Mapping[str, Any]) -> str:
    """How Ember's code checks a metric milestone and where it stands, for the planner ("" without a metric)."""
    text = progress_text(row)
    return f"Ember's code checks it: {text}" if text else ""


def _column(row: Mapping[str, Any], name: str) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


# --- reading ---


@dataclass(frozen=True)
class Reading:
    value: int
    at: str  # when the numbers were read: the Etsy sync's time, or now
    detail: str = ""  # what the value is made of: the evidence
    sample: int | None = None  # what a miss is judged against, for a metric that has one


@dataclass(frozen=True)
class Unread:
    why: str  # why Ember's code can't check it now


@dataclass(frozen=True)
class Books:
    """What reading a metric needs besides the records: the ledger's scope (its ``where``), the clock, when the shop
    was last read and whether the owner keeps the views history."""

    ledger: Any
    clock: Clock
    synced_at: str | None = None
    history: bool = False


def _ids(numbers: list[int], shown: int = 8) -> str:
    """Numbers as "#1, #2, …" (the first ``shown``)."""
    return ", ".join(f"#{n}" for n in numbers[:shown]) + (", …" if len(numbers) > shown else "")


def listings(
    conn: sqlite3.Connection, scope: AgentScope, project_id: int | None, venture_id: int | None
) -> list[sqlite3.Row]:
    """Ember's listings on Etsy (with their numbers at the last sync), those of the project or venture if given.
    0.14.0: with the listings Printify made of Ember's products (``printify`` 1), whose numbers Etsy's sync keeps too:
    the metrics, a product line's listing test and the ventures' rules left them out."""
    where, params = scope.where("l")
    joins = (
        " JOIN approvals a ON a.id = l.approval_id LEFT JOIN cycles y ON y.id = a.cycle_id"
        f" LEFT JOIN projects p ON p.id = COALESCE(a.project_id, y.project_id) WHERE {where}"
        " AND l.listing_id IS NOT NULL ORDER BY l.id"
    )
    # 0.14.0: a listing of a product line with no venture (a closed one the upgrade couldn't link) counts for its
    # channel's venture, as ventures.adopt would link it: the Etsy leg, or the print-on-demand venture.
    mine = (
        "p.id AS for_project, COALESCE(p.venture_id, y.venture_id, (SELECT v.id FROM ventures v WHERE"
        " v.mode = l.mode AND v.session = l.session AND v.stage NOT IN ('parked', 'killed') AND {leg}"
        " ORDER BY v.id LIMIT 1)) AS for_venture"
    )
    etsy_leg = mine.format(leg="v.parent_id IS NULL AND v.title = '{}'".format(ventures.ETSY_LEG.replace("'", "''")))
    rows = conn.execute(f"SELECT l.*, 0 AS printify, {etsy_leg} FROM etsy_listings l{joins}", params).fetchall()
    rows += conn.execute(
        "SELECT l.id, l.approval_id, l.listing_id, l.title, l.status, l.state, l.views, l.favorites, l.synced_at,"
        f" 1 AS printify, {mine.format(leg="v.channel = 'printify'")} FROM printify_products l{joins}",
        params,
    ).fetchall()
    if project_id:
        return [r for r in rows if r["for_project"] == project_id]
    if venture_id:
        return [r for r in rows if r["for_venture"] == venture_id]
    return rows


def read(
    conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any], books: Books, now: str, new: bool = False
) -> Reading | Unread:
    """The metric of a milestone (``row``: its metric, target, baseline, links and when it was set), now. Etsy's
    numbers count when read after it was set (``new``: before it is set, whenever read), at most FRESH_HOURS ago."""
    m = CATALOGUE.get(row["metric"] or "")
    if m is None:
        return Unread(f"Ember's code doesn't know the metric {row['metric']!r}")
    project_id, venture_id, since = row["project_id"], row["venture_id"], str(row["created_at"])
    if m.etsy:
        synced = books.synced_at
        if not synced or (synced <= since and not new):
            return Unread("Etsy hasn't been read since it was set")
        if from_iso(now) - from_iso(synced) > timedelta(hours=FRESH_HOURS):
            return Unread(f"Etsy was last read at {synced[:16].replace('T', ' ')} UTC, over {FRESH_HOURS} hours ago")
        if m.history and not books.history:
            return Unread("the views history is off (your owner's etsy_stats_history option)")
        return _read_etsy(conn, scope, m, row, synced)
    if m.name == "revenue_verified_usd":
        return _read_revenue(conn, books.ledger, project_id, venture_id, books.clock.local_day(since).isoformat(), now)
    if m.name == "api_spend_usd":
        return _read_spend(conn, scope, project_id, venture_id, since, now)
    if m.name in ("pins_live", "pin_clicks"):  # 0.13.0 (Phase E2)
        live, clicks = pinterest_publisher.totals(conn, scope)
        return Reading(live if m.name == "pins_live" else clicks, now, "")
    if m.name in ("pod_products_live", "pod_orders"):  # 0.13.0 (Phase E4)
        products, orders = printify_publisher.totals(conn, scope)
        return Reading(products if m.name == "pod_products_live" else orders, now, "")
    if m.name in ("inquiries_received", "inquiries_answered"):
        received, answered = mailstore.counts(conn, scope, since)
        return Reading(received if m.name == "inquiries_received" else answered, now, "")
    if m.name == "research_calls_ok":
        where, params = scope.where("v")
        mine = " AND r.venture_id = ?" if venture_id else ""
        found = conn.execute(
            "SELECT COUNT(*) FROM venture_research r JOIN ventures v ON v.id = r.venture_id"
            f" WHERE {where} AND r.sources > 0 AND r.created_at >= ?{mine}",
            (*params, since, *((venture_id,) if venture_id else ())),
        ).fetchone()[0]
        return Reading(int(found), now, _of(None, venture_id))
    venture = ventures.get(conn, scope, venture_id) if venture_id else None
    if venture is None:
        return Unread(f"there is no venture #{venture_id}")
    if m.name == "case_complete":
        gaps = ventures.proposal_gaps(venture, "researching")
        needs = f", which still needs {'; '.join(gaps)}" if gaps else ""
        return Reading(0 if gaps else 1, now, f" (venture #{venture_id}{needs})")
    stage = venture["stage"]
    rank = STAGES.index(stage) if stage in STAGES else -1
    return Reading(rank, now, f" (venture #{venture_id}{'' if rank >= 0 else ' is ' + stage})")


def listing_counts(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any], metric: str) -> int:
    """The views or favorites in all of the milestone's listings (their numbers at the last sync)."""
    return sum(int(r[metric] or 0) for r in listings(conn, scope, row["project_id"], row["venture_id"]))


def _read_etsy(
    conn: sqlite3.Connection, scope: AgentScope, m: Metric, row: Mapping[str, Any], synced: str
) -> Reading | Unread:
    rows = listings(conn, scope, row["project_id"], row["venture_id"])
    views = sum(int(r["views"] or 0) for r in rows)
    if m.name in ("views_total", "favorites_total"):  # 0.13.0: Etsy's counts in all, now
        column = "views" if m.name == "views_total" else "favorites"
        return Reading(sum(int(r[column] or 0) for r in rows), synced, f" ({len(rows)} listings)" if rows else "")
    if m.name in ("views_delta", "favorites_delta"):
        column = "views" if m.name == "views_delta" else "favorites"
        gained = sum(int(r[column] or 0) for r in rows) - int(row["baseline"] or 0)
        return Reading(max(0, gained), synced, sample=views if m.sample else None)
    live = etsy_publisher.live_rows(rows, {})
    if m.name == "listings_live":
        numbers = sorted(int(r["listing_id"]) for r in live)
        return Reading(len(live), synced, f" ({_ids(numbers)})" if numbers else "")
    if m.name in ("orders_observed", "orders_total"):  # 0.13.0: orders_total counts them in all
        since = str(row["created_at"]) if m.name == "orders_observed" else ""
        found = orders_of(conn, scope, {int(r["listing_id"]) for r in rows}, since)
        return Reading(len(found), synced, f" (receipts {_ids(found)})" if found else "", sample=views)
    few = []  # qa_clean: the live listings with too few photos in Ember's records
    for r in live:
        if r["printify"]:
            continue  # 0.14.0: its photos are Printify's mockups, not in Ember's records
        listing = etsy_publisher.recorded_listing(conn, scope, r)
        photos = qa.distinct(listing.photos) if listing is not None else 0  # 0.14.0: a copied file counts once
        if photos < qa.MIN_PHOTOS:
            few.append(f"#{r['listing_id']} has {photos}")
    detail = f" ({', '.join(few[:6])})" if few else f" ({len(live)} live)"
    return Reading(1 if live and not few else 0, synced, detail, sample=len(live))


def orders_of(conn: sqlite3.Connection, scope: AgentScope, ours: set[int], since: str, until: str = "") -> list[int]:
    """The receipts of the (counted) Etsy orders with one of the listings ``ours``, from ``since`` on ("": all) and
    before ``until`` ("": to now)."""
    where, params = scope.where()
    found = []
    for order in conn.execute(
        f"SELECT receipt_id, items FROM etsy_orders WHERE {where} AND {etsy.COUNTED_ORDERS} AND ordered_at >= ?"
        " AND (? = '' OR ordered_at < ?) ORDER BY ordered_at",
        (*params, since, until, until),
    ):
        items = json.loads(order["items"] or "[]")
        if any(isinstance(i, dict) and i.get("listing_id") in ours for i in items):
            found.append(int(order["receipt_id"]))
    return found


def _read_revenue(
    conn: sqlite3.Connection, ledger: Any, project_id: int | None, venture_id: int | None, first_day: str, now: str
) -> Reading:
    where, params = ledger.where("l")
    mine, more = "", ()
    if project_id:
        mine, more = " AND l.project_id = ?", (project_id,)
    elif venture_id:
        mine, more = " AND COALESCE(l.venture_id, p.venture_id) = ?", (venture_id,)
    earned = conn.execute(
        "SELECT COALESCE(SUM(CASE l.type WHEN 'revenue' THEN l.amount_micros ELSE -l.amount_micros END), 0)"
        " FROM ledger l LEFT JOIN projects p ON p.id = l.project_id"
        f" WHERE l.type IN ('revenue', 'expense') AND l.occurred_on >= ? AND {where}{mine}",
        (first_day, *params, *more),
    ).fetchone()[0]
    return Reading(int(earned), now, _of(project_id, venture_id))


def _read_spend(
    conn: sqlite3.Connection, scope: AgentScope, project_id: int | None, venture_id: int | None, since: str, now: str
) -> Reading:
    mine, more = "", ()
    if project_id:
        mine, more = " AND y.project_id = ?", (project_id,)
    elif venture_id:
        mine, more = " AND COALESCE(y.venture_id, p.venture_id) = ?", (venture_id,)
    spent = conn.execute(
        "SELECT COALESCE(SUM(c.cost_micros), 0) FROM llm_calls c JOIN cycles y ON y.id = c.cycle_id"
        " LEFT JOIN projects p ON p.id = y.project_id"
        f" WHERE y.session = ? AND y.simulated = ? AND c.ts >= ?{mine}",
        (scope.session, 1 if scope.simulated else 0, since, *more),
    ).fetchone()[0]
    return Reading(int(spent), now, _of(project_id, venture_id))


# --- grading ---


def _open_metric_milestones(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status = 'open' AND metric IS NOT NULL ORDER BY due, id", params
    ).fetchall()


def _evidence(m: Metric, reading: Reading, target: int, books: Books) -> str:
    moment = from_iso(reading.at).astimezone(books.clock.tz).strftime("%Y-%m-%d %H:%M")
    source = f"Etsy's numbers read {moment}" if m.etsy else f"{SOURCES[m.source]} at {moment}"
    return f"{m.name} {amount(m, reading.value)}{reading.detail}, target {target_text(m, target)} ({source})"


def grade(conn: sqlite3.Connection, scope: AgentScope, books: Books) -> list[str]:
    """Check every open metric milestone (no model call): record where it stands (``progress``, ``checked_at``, and
    the day's observation), close it done once met and missed once its date has passed without it (a ceiling: missed
    once passed, done at its date). A killed venture's milestones were dropped with it (agent/stages.py). 0.14.0: past
    its date, Etsy's numbers read only after it (a sync gap) don't meet it: its last reading by then decides, and
    without one it is missed (a bar met days late was graded done). Returns what happened, for the events."""
    now = to_iso(books.clock.now())
    today = books.clock.today()
    happened = []
    for row in _open_metric_milestones(conn, scope):
        m = CATALOGUE.get(row["metric"])
        reading = read(conn, scope, row, books, now)
        if m is None or isinstance(reading, Unread):
            continue
        target = int(row["target"])
        past = today.isoformat() > row["due"]
        judged: Reading | None = reading
        if m.etsy and past and books.clock.local_day(reading.at).isoformat() > row["due"]:
            # 0.14.0: Etsy read only after its date (a sync gap): its last reading by then decides; a later one can
            # only show a miss (a bar met days late was graded done). Orders carry their own date: those placed by
            # the end of its due day count, however late a sync fetched them
            if m.name in ("orders_observed", "orders_total"):
                judged = _orders_by_its_date(conn, scope, row, books, reading)
            else:
                judged = _by_its_date(row, books) or (reading if reading.value < target else None)
        if judged is reading:
            conn.execute(
                "UPDATE milestones SET progress = ?, checked_at = ? WHERE id = ?",
                (reading.value, reading.at, row["id"]),
            )
            conn.execute(
                "INSERT INTO observations (mode, session, day, observed_at, subject, subject_id, metric, value)"
                " VALUES (?, ?, ?, ?, 'milestone', ?, ?, ?) ON CONFLICT DO NOTHING",
                (scope.mode, scope.session, today.isoformat(), now, row["id"], m.name, reading.value),
            )
        if judged is None:
            status: str | None = "missed"
        elif m.ceiling:
            status = "missed" if judged.value > target else "done" if past else None
        else:
            status = "done" if judged.value >= target else "missed" if past else None
        if status is None:
            continue
        evidence = (
            _evidence(m, judged, target, books)
            if judged is not None
            else f"{m.name} was not read by its date, target {target_text(m, target)}"
        )
        if status == "done":
            result = f"Ember's code checked it: {evidence}."
        elif m.ceiling:
            result = f"Ember's code checked it: {evidence}: over the limit."
        else:
            result = f"Ember's code checked it after its date: {evidence}."
            if m.sample and judged is not None and judged.sample is not None and judged.sample < m.min_sample:
                result += f" Too little to judge: {judged.sample:,} {m.sample} in all, fewer than {m.min_sample:,}."
        conn.execute(
            "UPDATE milestones SET status = ?, result = ?, closed_at = ?, closed_by = 'code', updated_at = ?,"
            " proposed_due = NULL, proposed_note = NULL, proposed_at = NULL, proposed_cycle_id = NULL"
            " WHERE id = ? AND status = 'open'",
            (status, result[:600], now, now, row["id"]),
        )
        happened.append(f"Ember's code closed milestone #{row['id']} {status}: {evidence}")
    return happened


def _by_its_date(row: Mapping[str, Any], books: Books) -> Reading | None:
    """0.14.0: a milestone's last reading on or before its due day, the owner's local day (None: none by then)."""
    at = row["checked_at"]
    if row["progress"] is None or not at or books.clock.local_day(str(at)).isoformat() > row["due"]:
        return None
    return Reading(int(row["progress"]), str(at), " (its last reading by its date)")


def _orders_by_its_date(
    conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any], books: Books, reading: Reading
) -> Reading:
    """0.14.0: the milestone's Etsy orders placed by the end of its due day, the owner's local day."""
    rows = listings(conn, scope, row["project_id"], row["venture_id"])
    since = str(row["created_at"]) if row["metric"] == "orders_observed" else ""
    until = books.clock.day_bounds(date.fromisoformat(str(row["due"])))[1]
    found = orders_of(conn, scope, {int(r["listing_id"]) for r in rows}, since, until)
    detail = f" (receipts {_ids(found)}, ordered by its date)" if found else ""
    return Reading(len(found), reading.at, detail, sample=reading.sample)


def grade_all(db: Database, scope: AgentScope, ledger: Any, clock: Clock, history: bool) -> list[str]:
    """``grade`` in its own transaction, with when the shop was last read (``ledger``: the books' scope); what
    happened becomes events."""
    synced = db.get_meta(etsy_publisher.meta_key(scope.mode, "last_sync_at"))
    with db.transaction() as conn:
        happened = grade(conn, scope, Books(ledger, clock, synced, history))
    for line in happened:
        events.record(db, "info", "agent", line[:300])
    return happened
