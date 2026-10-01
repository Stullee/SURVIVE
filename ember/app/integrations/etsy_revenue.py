"""Ember's Etsy orders in the ledger, from Etsy's own numbers (0.12.0).

Revenue only ever came from the owner, so a sale counted nowhere (the balance, the runway, the money goal, a
venture's earnings) until the owner recorded it by hand. When the owner turns on ``etsy_auto_record_revenue``, every
Etsy sync that worked records, for each order with Ember's listings:

* its revenue once it is paid: Ember's lines with their shipping (0.14.0), net of tax, the coupon and refunds (what
  the sync stores);
* Etsy's fees on it, as an expense of the same project, once the sync has read them from the order's payment;
* its refunds: when an order whose revenue Ember's code recorded earns less now (partly or fully refunded, or
  cancelled), a correction of that entry down to what the order earns now.

It records the orders placed from the day the owner turned it on: the ones before keep their buttons (the owner may
have booked them otherwise, as one sum, say). Each entry carries the request key of the owner's button for it, so an
order is recorded once, whoever comes first, and an order the owner recorded stays theirs (its fees and refunds too).
An order in EUR needs the owner's exchange
rate (``etsy_usd_per_eur``), and its fees and refunds keep the rate its revenue was recorded at; other currencies are
the owner's to convert. Etsy's fees on an order refunded later stay recorded: what Etsy credits back is the owner's to
correct. Fees that would kill the agent or leave it unfunded wait for the owner, who is told once
(``Economy.record_integration``). 0.14.0: a refund is a fact, so it is recorded all the same (held back, it left the
agent spending money the buyer got back); if it leaves the agent without money, Ember's code pauses it with the reason.

0.14.0: whatever the option, each sync also records the listing fees Etsy charged for Ember's listings (USD 0.20: for
publishing one, for a renewal Ember made or one Etsy made), noted where they happened
(``etsy_publisher.listing_fee_due``). They are Ember's own spending, like its API calls, and were never recorded; they
are facts too.

In dry run the orders and listings are the fake shop's, so their entries are test money.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from .. import events
from ..agent import econ
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock
from ..economy.costs import micros_to_usd
from ..economy.ledger import MAX_BACKDATE_DAYS, PreparedEntry, decimal_text, dollars_to_micros
from . import etsy
from .etsy_publisher import FEE_DUE_KEY, fee_key, order_project, revenue_key

if TYPE_CHECKING:
    from ..economy.service import Economy

log = logging.getLogger(__name__)

OPTION_KEY = "integrations.etsy.auto_revenue"  # the option as last seen, for the audit of its changes
SINCE_KEY = "integrations.etsy.auto_revenue_since"  # the owner's day it was turned on: its orders from then on
HELD_KEY = "integrations.etsy.revenue_held."  # + an entry's key: the owner was told it waits for them
REVENUE_NOTE = "Ember's lines of the order with their shipping, net of tax, the coupon and refunds (Etsy's numbers)"
FEES_NOTE = (  # the owner's button's too (0.14.0: the listing fee a sale renews and the VAT on fees were left out)
    "Etsy's fees on order {receipt}: payment processing, the 6.5% transaction fee, USD 0.20 a unit sold and 19% VAT"
    " on those two"
)
LISTING_FEE_NOTE = "Etsy's listing fee for listing {listing}: {why}"  # 0.14.0


@dataclass
class Done:
    """What one run recorded (the entries' ids) and what waits for the owner (the entries' keys)."""

    recorded: list[int] = field(default_factory=list)
    held: list[str] = field(default_factory=list)


def refund_key(receipt_id: int, left_micros: int) -> str:
    """The request key of the correction that brings an order's revenue down to ``left_micros``: one per level, so a
    second partial refund is a second correction, and the next sync writes nothing new."""
    return hashlib.sha256(f"etsy-refund-{receipt_id}-{left_micros}".encode()).hexdigest()[:32]


def record(db: Database, clock: Clock, economy: Economy, scope: AgentScope, settings: Settings) -> Done:
    """What Etsy's numbers say about the orders with Ember's listings, in the ledger (see the module's text): after
    each sync that worked, when the owner turned it on (0.14.0: the listing fees whatever the option)."""
    done = Done()
    _listing_fees(db, clock, economy, scope, done)
    if not settings.etsy_auto_record_revenue:
        return done
    rate = Decimal(str(settings.etsy_usd_per_eur)) if settings.etsy_usd_per_eur else None
    since = db.get_meta(SINCE_KEY) or ""
    if not since:  # turned on without a restart's audit (tests): from today
        since = clock.today().isoformat()
        db.set_meta(SINCE_KEY, since)
    where, params = scope.where()
    with db.connection() as conn:
        # An order from before 0.12.0 has no status: its total is the whole receipt's, so it stays the owner's.
        orders = conn.execute(
            f"SELECT * FROM etsy_orders WHERE {where} AND status IS NOT NULL ORDER BY ordered_at, id", params
        ).fetchall()
    simulated = scope.mode == "dry_run"
    for order in orders:  # what came in first, so what goes out is weighed against it
        prepared = _revenue(db, clock, scope, order, rate, simulated, since)
        if prepared is not None:
            _write(db, economy, prepared, done)
    for order in orders:
        for step in (_fees, _refund):
            prepared = step(db, order)
            if prepared is not None:
                _write(db, economy, prepared, done, fact=step is _refund)
    if done.recorded or done.held:
        log.info(
            "Etsy's numbers: %d ledger entries recorded, %d waiting for the owner", len(done.recorded), len(done.held)
        )
    return done


def audit(db: Database, clock: Clock, settings: Settings) -> None:
    """An event whenever the owner turns the automatic recording on or off or changes its exchange rate (at start:
    the options only change with a restart). Turned on, it records the orders from that day on."""
    on = settings.etsy_auto_record_revenue
    rate = settings.etsy_usd_per_eur
    now = f"on {rate:g}" if on else "off"
    before = db.get_meta(OPTION_KEY)
    if before == now:
        return
    db.set_meta(OPTION_KEY, now)
    was_on = (before or "").startswith("on")
    if not on:
        db.set_meta(SINCE_KEY, "")  # turned on again later, it starts from that day
    elif not was_on or not db.get_meta(SINCE_KEY):
        db.set_meta(SINCE_KEY, clock.today().isoformat())
    if before is None and not on:
        return  # never turned on: nothing to say
    if not on:
        text = "Automatic recording of Etsy revenue is off: you record the revenue of Ember's Etsy orders yourself"
    else:
        euros = f"orders in EUR at {rate:g} USD per EUR" if rate else "orders in EUR are left to you (no exchange rate)"
        text = (
            "Automatic recording of Etsy revenue is on: at each Etsy sync, Ember's code records the revenue of Ember's"
            f" orders placed from {db.get_meta(SINCE_KEY)} on, Etsy's fees and refunds in the ledger; {euros}"
        )
    events.record(db, "info", "config", text)


def _revenue(
    db: Database,
    clock: Clock,
    scope: AgentScope,
    order: sqlite3.Row,
    rate: Decimal | None,
    simulated: bool,
    since: str,
) -> PreparedEntry | None:
    """The revenue of a paid order placed since the owner's day ``since`` that nobody recorded yet, or None."""
    if order["status"] not in etsy.PAID_ORDERS or order["total_cents"] <= 0:
        return None
    try:
        if clock.local_day(order["ordered_at"]).isoformat() < since:
            return None  # from before it was turned on: the owner's
    except ValueError:
        return None
    amount = _usd(order["total_cents"], order["currency"], rate)
    key = revenue_key(order["receipt_id"])
    if amount is None:
        return None
    with db.connection() as conn:
        if _entry(conn, key) is not None:
            return None
        project_id, venture_id = order_project(conn, scope, json.loads(order["items"] or "[]"))
    micros, euros, fx = amount
    return PreparedEntry(
        type="revenue",
        amount_micros=micros,
        simulated=simulated,
        source=f"Etsy order {order['receipt_id']}",
        note=REVENUE_NOTE,
        occurred_on=_day(clock, order["ordered_at"]),
        day_given=True,
        idempotency_key=key,
        orig_amount=euros,
        orig_currency="EUR" if euros is not None else None,
        fx_rate=fx,
        project_id=project_id,
        venture_id=venture_id,
        created_by="etsy",
    )


def _fees(db: Database, order: sqlite3.Row) -> PreparedEntry | None:
    """Etsy's fees on an order whose revenue Ember's code recorded, once the sync has read them, or None."""
    if not order["fees_cents"] or order["status"] in etsy.DEAD_ORDERS:
        return None
    key = fee_key(order["receipt_id"])
    with db.connection() as conn:
        revenue = _entry(conn, revenue_key(order["receipt_id"]))
        if revenue is None or revenue["created_by"] != "etsy" or _entry(conn, key) is not None:
            return None
    amount = _usd(order["fees_cents"], order["currency"], _rate(revenue))
    if amount is None:
        return None
    micros, euros, fx = amount
    return PreparedEntry(
        type="expense",
        amount_micros=micros,
        simulated=bool(revenue["simulated"]),
        source=f"Etsy order {order['receipt_id']}",
        note=FEES_NOTE.format(receipt=order["receipt_id"]),
        occurred_on=revenue["occurred_on"],
        day_given=True,
        idempotency_key=key,
        orig_amount=euros,
        orig_currency="EUR" if euros is not None else None,
        fx_rate=fx,
        project_id=revenue["project_id"],
        venture_id=revenue["venture_id"],
        created_by="etsy",
    )


def _refund(db: Database, order: sqlite3.Row) -> PreparedEntry | None:
    """A correction of the revenue Ember's code recorded for an order that earns less now, down to what it earns (at
    the rate it was recorded at), or None."""
    with db.connection() as conn:
        revenue = _entry(conn, revenue_key(order["receipt_id"]))
        if revenue is None or revenue["created_by"] != "etsy":
            return None
        corrected = conn.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE corrects_id = ?", (revenue["id"],)
        ).fetchone()[0]
    left = revenue["amount_micros"] + int(corrected)
    dead = order["status"] in etsy.DEAD_ORDERS
    if dead:
        earns = 0
    else:
        amount = _usd(order["total_cents"], order["currency"], _rate(revenue))
        if amount is None:
            return None
        earns = amount[0]
    if earns >= left:
        return None
    key = refund_key(order["receipt_id"], earns)
    with db.connection() as conn:
        if _entry(conn, key) is not None:
            return None
    receipt = order["receipt_id"]
    why = (
        f"Etsy order {receipt} was {order['status']}"
        if dead
        else f"Etsy order {receipt} was partly refunded: Ember's lines earn {order['total']} now"
    )
    return PreparedEntry(
        type="revenue",
        amount_micros=earns - left,
        simulated=bool(revenue["simulated"]),
        source=None,
        note=why,
        occurred_on=revenue["occurred_on"],
        day_given=True,
        idempotency_key=key,
        corrects_id=revenue["id"],
        project_id=revenue["project_id"],  # a correction belongs where the entry it corrects belongs
        venture_id=revenue["venture_id"],
        created_by="etsy",
    )


def _listing_fees(db: Database, clock: Clock, economy: Economy, scope: AgentScope, done: Done) -> None:
    """0.14.0: the listing fees Etsy charged for Ember's listings in this mode and session, as expenses of the listing's
    project (one of an ended dry-run session is dropped)."""
    with db.connection() as conn:
        due = conn.execute(
            "SELECT key, value FROM meta WHERE substr(key, 1, ?) = ? ORDER BY key",
            (len(FEE_DUE_KEY), FEE_DUE_KEY),
        ).fetchall()
    for row in due:
        fee = json.loads(row["value"])
        if fee["mode"] != scope.mode:
            continue  # recorded when that mode runs again
        if fee["session"] == scope.session:
            with db.connection() as conn:
                project_id, venture_id = order_project(conn, scope, [{"listing_id": fee["listing_id"]}])
            prepared = PreparedEntry(
                type="expense",
                amount_micros=dollars_to_micros(Decimal(str(econ.LISTING_FEE_USD)) * int(fee["fees"])),
                simulated=scope.mode == "dry_run",
                source=f"Etsy listing {fee['listing_id']}",
                note=LISTING_FEE_NOTE.format(listing=fee["listing_id"], why=fee["why"]),
                occurred_on=_day(clock, fee["at"]),
                day_given=True,
                idempotency_key=row["key"][len(FEE_DUE_KEY) :],
                project_id=project_id,
                venture_id=venture_id,
                created_by="etsy",
            )
            _write(db, economy, prepared, done, fact=True)
            with db.connection() as conn:
                refused = _entry(conn, prepared.idempotency_key) is None
            if refused:  # kept for the next sync; the owner is told once
                told = HELD_KEY + prepared.idempotency_key
                if not db.get_meta(told):
                    db.set_meta(told, "refused")
                    events.record(
                        db,
                        "warning",
                        "ledger",
                        f"The ledger refused {prepared.note} (${micros_to_usd(prepared.amount_micros):.2f}). Ember's"
                        " code tries again at each sync.",
                    )
                continue
        with db.transaction() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (row["key"],))


def _write(db: Database, economy: Economy, prepared: PreparedEntry, done: Done, fact: bool = False) -> None:
    result = economy.record_integration(prepared, fact)
    if result.entry_id is not None:
        done.recorded.append(result.entry_id)
        return
    if result.held is None:
        return
    done.held.append(prepared.idempotency_key)
    told = HELD_KEY + prepared.idempotency_key
    if db.get_meta(told):
        return
    db.set_meta(told, result.held)
    state = "kill the agent" if result.held == "dead" else "leave the agent without money to run"
    events.record(  # only an order's fees wait for the owner (0.14.0: refunds and listing fees are facts)
        db,
        "warning",
        "ledger",
        f"Ember's code did not record Etsy's fees on an order (${micros_to_usd(abs(prepared.amount_micros)):.2f},"
        f" {prepared.source or prepared.note}): it would {state}. Record it yourself when you decide: System tab,"
        " Etsy orders.",
    )


def _entry(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, created_by, amount_micros, simulated, occurred_on, fx_rate, project_id, venture_id FROM ledger"
        " WHERE idempotency_key = ?",
        (key,),
    ).fetchone()


def _rate(entry: sqlite3.Row) -> Decimal | None:
    return Decimal(entry["fx_rate"]) if entry["fx_rate"] else None


def _usd(cents: int, currency: str, rate: Decimal | None) -> tuple[int, str | None, str | None] | None:
    """(micros, the amount in EUR, the rate) of ``cents`` in an order's currency; None when it can't be converted."""
    if currency == "USD":
        return dollars_to_micros(Decimal(cents) / 100), None, None
    if currency == "EUR" and rate is not None:
        euros = Decimal(cents) / 100
        usd = (euros * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return dollars_to_micros(usd), f"{euros:.2f}", decimal_text(rate)
    return None


def _day(clock: Clock, ordered_at: str) -> str:
    """The owner's calendar day of an order: never after today, nor before what the ledger takes."""
    today = clock.today()
    try:
        day = clock.local_day(ordered_at)
    except ValueError:
        return today.isoformat()
    return max(min(day, today), today - timedelta(days=MAX_BACKDATE_DAYS)).isoformat()
