"""0.14.0: Etsy's money booked right, listings missing from Etsy's answer, and orders and ledger paging.

* The ledger's fee model left out the USD 0.20 listing fee each sale renews and the VAT on fees, which the venture
  cases count (15-29% too little), and the listing fees Ember's listings cost were never booked at all.
* A refund of revenue Ember's code recorded was held back when it would leave Ember without money, so the agent went on
  spending money the buyer got back. A refund is a fact now: it is written, and Ember's code pauses the agent instead.
* A partial refund of an order the owner recorded never asked for a correction, and the order left the list.
* A listing Etsy's batch answer left out stayed live in Ember's records for good.
* An order with many lines failed the items CHECK and stopped every sync for 30 days, unseen.
* Orders were read for the last 30 days only, so a longer gap lost them.
* The Ledger tab skipped entries once new ones came in after "Show older entries".
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import econ  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import discover_migrations, migrate  # noqa: E402
from app.economy import life  # noqa: E402
from app.economy.clock import from_iso, to_iso  # noqa: E402
from app.economy.ledger import PreparedEntry  # noqa: E402
from app.economy.service import Recorded  # noqa: E402
from app.integrations import etsy, etsy_live, etsy_publisher, etsy_revenue  # noqa: E402
from tests.economy_helpers import make_economy  # noqa: E402
from tests.economy_helpers import owner as owner_entry  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import a_change, call, listed, live_shop, shop_context  # noqa: E402
from tests.test_etsy import proposed as etsy_proposed  # noqa: E402
from tests.test_etsy_revenue import LINE, LISTING, order, shop_with, turned_on  # noqa: E402
from tests.test_life import spend  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_printify import proposed as printify_proposed  # noqa: E402

APP_JS = Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js"


def listing_fees(agent: Any) -> list[dict[str, Any]]:
    return rows(
        agent,
        "SELECT type, amount_micros, source, note, project_id FROM ledger WHERE created_by = 'etsy'"
        " AND source LIKE 'Etsy listing %' ORDER BY id",
    )


def rows_of(economy: Any, sql: str) -> list[dict[str, Any]]:
    with economy.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def stored(agent: Any) -> dict[str, Any]:
    return rows(agent, "SELECT state, ends_at, auto_renew FROM etsy_listings")[0]


# --- FIX NOW 17: Etsy's fees, refunds and corrections ----------------------------------------------------------------


@pytest.mark.parametrize("price_cents", [300, 600, 2_490])
def test_an_orders_fees_are_the_ones_the_venture_cases_count(price_cents: int) -> None:
    lines = [{"listing_id": LISTING, "title": "t", "quantity": 1, "price_cents": price_cents}]
    sale = etsy.Order(71, "2026-09-02T10:00:00Z", price_cents, "EUR", lines, items_cents=price_cents)
    processing = round(econ.PROCESSING_SHARE * price_cents + econ.PROCESSING_EUR * 100)  # what the payment says
    expected = round(econ.fees("etsy_digital", price_cents / 100, econ.DEFAULT_USD_PER_EUR) * 100)
    assert etsy.fees_share(sale, lines, processing) == expected  # 0.13.0: 62 against 87 cents at EUR 3.00
    assert etsy.fees_share(sale, lines, processing, 1.25) < expected  # the listing fee is USD 0.20
    two = [{**lines[0], "quantity": 2}]
    double = etsy.Order(72, "2026-09-02T10:00:00Z", 2 * price_cents, "USD", two, items_cents=2 * price_cents)
    listing_part = etsy.fees_share(double, two, 0) - round(2 * price_cents * econ.TRANSACTION_SHARE * 1.19)
    assert abs(listing_part - 2 * 20 * 1.19) <= 1  # USD 0.20 for each unit sold, with its VAT
    script = APP_JS.read_text(encoding="utf-8")
    assert etsy_revenue.FEES_NOTE.split("{receipt}")[1] in script  # the owner's button books the same parts


def test_the_listing_fees_embers_listings_cost_are_booked_once(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    assert agent.publisher.sync(force=True) is None
    [listed_fee] = listing_fees(agent)  # whatever etsy_auto_record_revenue says
    project = rows(agent, "SELECT project_id FROM approvals WHERE executor = 'etsy_listing'")[0]["project_id"]
    assert listed_fee == {
        "type": "expense",
        "amount_micros": 200_000,
        "source": f"Etsy listing {listing_id}",
        "note": f"Etsy's listing fee for listing {listing_id}: it went live",
        "project_id": project,
    }
    # It expires, and Ember renews it with an approved change: a fee.
    agent.clock.advance(days=etsy.LISTING_DAYS + 1)
    assert agent.publisher.sync(force=True) is None and stored(agent)["state"] == "expired"
    request = a_change(agent, shop_context(agent), listing_id, state="renew")
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "done")]
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    assert [f["note"] for f in listing_fees(agent)][1:] == [
        f"Etsy's listing fee for listing {listing_id}: Ember renewed it (request #{request})"
    ]
    # Etsy renews it itself at its end: a fee for each renewal.
    agent.etsy.shop().set_auto_renew(listing_id, True)
    assert agent.publisher.sync(force=True) is None
    agent.clock.advance(days=etsy.LISTING_DAYS + 1)
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    fees = listing_fees(agent)
    assert len(fees) == 3 and fees[2]["note"].startswith(f"Etsy's listing fee for listing {listing_id}: Etsy renewed")
    assert not rows(agent, f"SELECT key FROM meta WHERE key LIKE '{etsy_publisher.FEE_DUE_KEY}%'")  # none waits


def test_the_listing_fees_of_printifys_listings_are_booked_too(data_dir: Path) -> None:
    agent, _, request = printify_proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    pod = rows(agent, "SELECT listing_id FROM printify_products")[0]["listing_id"]
    shop = agent.etsy.shop()
    batch = shop.listings
    ends = [to_iso(agent.clock.now() + timedelta(days=etsy.LISTING_DAYS))]

    def with_pod(ids: list[int]) -> list[etsy.RemoteListing]:  # the dry run's fake shop doesn't know Printify's
        return [*batch(ids), etsy.RemoteListing(pod, "active", "Poster", etsy.listing_url(pod), 3, 1, ends[0], True)]

    shop.listings = with_pod
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    project = rows(agent, f"SELECT project_id FROM approvals WHERE id = {request}")[0]["project_id"]
    fees = [f for f in listing_fees(agent) if f["source"] == f"Etsy listing {pod}"]
    assert [(f["note"], f["project_id"]) for f in fees] == [  # 0.13.0: none
        (f"Etsy's listing fee for listing {pod}: Printify published it", project)
    ]
    assert rows(agent, "SELECT ends_at, auto_renew FROM printify_products") == [{"ends_at": ends[0], "auto_renew": 1}]
    # Etsy renews it at its end: a fee.
    agent.clock.advance(days=etsy.LISTING_DAYS + 1)
    ends[0] = to_iso(from_iso(ends[0]) + timedelta(days=etsy.LISTING_DAYS))
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    fees = [f["note"] for f in listing_fees(agent) if f["source"] == f"Etsy listing {pod}"]
    assert fees[1:] == [f"Etsy's listing fee for listing {pod}: Etsy renewed it until {ends[0][:10]}"]


def test_the_owner_publishing_a_draft_of_embers_books_its_listing_fee(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = etsy_proposed(data_dir)

    def refuse(self: Any, listing_id: int, name: str, data: bytes, rank: int) -> None:
        raise etsy.NotSent("HTTP 400: file too large")

    monkeypatch.setattr(etsy.FakeShop, "upload_file", refuse)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "draft")]
    assert agent.publisher.sync(force=True) is None and listing_fees(agent) == []
    shop = agent.etsy.shop()  # the owner adds the file and publishes it at Etsy
    item = shop.state["listings"]["900000001"]
    item["state"], item["live_since"] = "active", to_iso(agent.clock.now())
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    assert [f["note"] for f in listing_fees(agent)] == [  # 0.14.0 at first: never booked
        "Etsy's listing fee for listing 900000001: the owner published it"
    ]


def test_a_listing_fee_the_ledger_refuses_waits_for_the_next_sync(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    record = agent.economy.record_integration
    agent.economy.record_integration = lambda prepared, fact=False: Recorded()  # an IntegrityError, say
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    assert listing_fees(agent) == []
    assert len(rows(agent, f"SELECT key FROM meta WHERE key LIKE '{etsy_publisher.FEE_DUE_KEY}%'")) == 1  # 0.13.0: lost
    told = [e["message"] for e in agent.db.recent_events(limit=50) if e["message"].startswith("The ledger refused")]
    assert told == [
        f"The ledger refused Etsy's listing fee for listing {listing_id}: it went live ($0.20). Ember's"
        " code tries again at each sync."
    ]  # once
    agent.economy.record_integration = record
    assert agent.publisher.sync(force=True) is None
    assert [f["note"] for f in listing_fees(agent)] == [f"Etsy's listing fee for listing {listing_id}: it went live"]
    assert not rows(agent, f"SELECT key FROM meta WHERE key LIKE '{etsy_publisher.FEE_DUE_KEY}%'")


def test_printifys_listings_keep_their_end_after_the_upgrade(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 58], backup_dir=tmp_path / "backups")
    old = sqlite3.connect(db_file)
    with old:
        old.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        old.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', 'then', 'completed', 'schedule', 0, 1)"
        )
        old.execute(
            "INSERT INTO approvals (id, mode, session, life_id, cycle_id, created_at, type, title, description,"
            " payload, payload_sha256, expected_cost, expected_benefit, status, closed_at, closed_by, version,"
            " executor, action) VALUES (1, 'live', 0, 1, 1, 'then', 'sell', 'Printify product: Poster', 'd', 'p',"
            " 'h', 'c', 'b', 'done', 'now', 'Ember', 2, 'printify_product', '{}')"
        )
        old.execute(
            "INSERT INTO printify_products (mode, session, approval_id, listing_id, title, currency, status,"
            " started_at, finished_at, state, synced_at) VALUES ('live', 0, 1, 800000001, 'Poster', 'EUR', 'active',"
            " 'then', 'then', 'active', 'then')"
        )
    old.close()
    [ours] = [m for m in discover_migrations() if m.name == "etsy_money"]
    assert ours.version in migrate(db_file, backup_dir=tmp_path / "backups")
    upgraded = sqlite3.connect(db_file)
    upgraded.row_factory = sqlite3.Row
    [row] = [dict(r) for r in upgraded.execute("SELECT listing_id, state, ends_at, auto_renew FROM printify_products")]
    assert row == {"listing_id": 800000001, "state": "active", "ends_at": None, "auto_renew": None}
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute("UPDATE printify_products SET auto_renew = 2")
    upgraded.close()


def test_a_refund_is_recorded_even_when_it_leaves_the_agent_without_money(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    turned_on(agent)
    shop_with(agent, [order(agent, 71)])
    assert agent.publisher.sync(force=True) is None
    balance = agent.economy.status().balance
    owner_entry(agent.economy, "expense", f"{(balance - 1_000_000) / 1_000_000:.2f}", test_money=True)
    assert agent.economy.status().state in ("alive", "critical")  # about $1 left
    shop_with(agent, [order(agent, 71, status="fully refunded", refunded_cents=450)])
    assert agent.publisher.sync(force=True) is None
    [refund] = rows(agent, "SELECT id, amount_micros FROM ledger WHERE created_by = 'etsy' AND corrects_id IS NOT NULL")
    assert refund["amount_micros"] == -4_950_000  # 0.13.0: held back, and the agent kept spending it
    # Test money: the test life's balance decides; the switch, which the live agent shares, stays off.
    status = agent.economy.status()
    assert status.balance < 0 and status.state == "dead"
    assert agent.db.get_meta(life.PAUSED_KEY) != "1" and not agent.db.get_meta(life.MONEY_PAUSE_KEY)


def a_fact(economy: Any) -> PreparedEntry:
    """A listing fee from Etsy's numbers, real money, a dollar more than the agent has."""
    return PreparedEntry(
        type="expense",
        amount_micros=economy.status().balance + 1_000_000,
        simulated=False,
        source="Etsy listing 1",
        note="Etsy's listing fee for listing 1: it went live",
        occurred_on=economy.clock.today().isoformat(),
        day_given=True,
        idempotency_key=uuid.uuid4().hex,
        created_by="etsy",
    )


def test_a_fact_that_leaves_the_live_agent_without_money_pauses_it(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(dry_run=False))
    spend(economy, 1_000)
    assert economy.status().state in ("alive", "critical")
    entry_id = economy.record_integration(a_fact(economy), fact=True).entry_id
    assert entry_id is not None  # 0.13.0: held back
    status = economy.status()
    assert status.balance < 0 and status.state == "paused"
    assert status.reason.startswith(f"Paused by Ember's code: entry #{entry_id} from Etsy's numbers left it")
    assert rows_of(economy, "SELECT ended_at FROM lives WHERE mode = 'live'") == [{"ended_at": None}]  # not dead
    warnings = [e["message"] for e in economy.db.recent_events(limit=20) if e["level"] == "warning"]
    assert any(m.startswith("Paused by Ember's code") for m in warnings), warnings
    # Dry run keeps the owner's switch, but not the reason, and the test life's balance decides.
    dry = make_economy(data_dir, Settings(dry_run=True), clock=economy.clock)
    assert (dry.status().state, dry.status().reason) == ("paused", life.REASONS["paused"])
    # Back in live, the owner decides: a grant, then resuming it.
    owner_entry(economy, "grant", "10.00")
    assert economy.status().state == "paused"
    assert economy.set_paused(False).state in ("alive", "critical")
    assert not economy.db.get_meta(life.MONEY_PAUSE_KEY)


def test_resuming_without_money_lets_the_agent_end(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(dry_run=False))
    spend(economy, 1_000)
    assert economy.record_integration(a_fact(economy), fact=True).entry_id is not None
    assert economy.status().state == "paused"
    assert economy.set_paused(False).state == "dead"


def test_a_partial_refund_of_an_order_the_owner_recorded_asks_for_a_correction(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    older = to_iso(agent.clock.now() - timedelta(days=2))
    first = etsy.Order(71, older, 450, "EUR", [LINE], items_cents=450)
    # Order 72 was partly refunded before the owner recorded it: the owner recorded what it earns.
    second = etsy.Order(72, older, 450, "EUR", [LINE], items_cents=450, status="partially refunded", refunded_cents=300)
    newer = [order(agent, 100 + n) for n in range(25)]
    shop_with(agent, [first, second, *newer])
    assert agent.publisher.sync(force=True) is None
    for receipt, euros in [(71, "4.50"), (72, "1.50"), *[(o.receipt_id, "4.50") for o in newer]]:
        body = {"currency": "EUR", "fx_rate": "1.1", "idempotency_key": etsy_publisher.revenue_key(receipt)}
        owner_entry(agent.economy, "revenue", euros, test_money=True, **body)
    partly = etsy.Order(71, older, 450, "EUR", [LINE], items_cents=450, status="partially refunded", refunded_cents=300)
    shop_with(agent, [partly, second, *newer])
    assert agent.publisher.sync(force=True) is None
    shown = {o["receipt_id"]: o for o in agent.integrations()["etsy"]["orders"]}
    assert 71 in shown and shown[71]["correction_due"]  # 0.13.0: "Recorded", and gone after 20 newer orders
    assert shown[71]["total"] == "1.50 EUR" and shown[71]["status"] == "partially refunded"
    assert 72 not in shown  # its entry says what it earns: nothing to correct
    assert [o["receipt_id"] for o in shown.values() if o["correction_due"]] == [71]
    correction = {"amount": "3.30", "note": "partly refunded", "idempotency_key": uuid.uuid4().hex}
    reply = agent.economy.correct(
        shown[71]["entry_id"], {**correction, "confirm_state_change": "dead", "confirm_large": True}
    )
    assert reply.status == 201, reply.body
    shown = {o["receipt_id"]: o for o in agent.integrations()["etsy"]["orders"]}
    assert 71 not in shown  # corrected: it leaves the list like any other recorded order
    assert "o.correction_due ?" in APP_JS.read_text(encoding="utf-8")


# --- X8: listings missing from Etsy's batch answer -------------------------------------------------------------------


def test_a_listing_the_batch_leaves_out_is_read_on_its_own(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    ends = stored(agent)["ends_at"]
    shop = agent.etsy.shop()
    shop.listings = lambda ids: []  # type: ignore[method-assign]
    reads: list[int] = []

    def expired(one: int) -> etsy.RemoteListing:
        reads.append(one)
        return etsy.RemoteListing(one, "expired", "Planner", etsy.listing_url(one), 30, 2, ends, False)

    shop.listing = expired  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    assert reads == [listing_id] and stored(agent)["state"] == "expired"  # 0.13.0: 'active' for good
    ctx = shop_context(agent)
    assert call(ctx, "etsy_listing", {}).text.startswith("You have no live listings. Not live at Etsy: ")


def test_a_listing_etsy_answers_for_in_neither_way_stops_counting_as_live(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    shop = agent.etsy.shop()
    shop.listings = lambda ids: []  # type: ignore[method-assign]

    def unreadable(one: int) -> etsy.RemoteListing:
        raise etsy.NotSent("HTTP 503: try later")

    shop.listing = unreadable  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    assert stored(agent)["state"] == "unknown"  # read again at the next sync
    with agent.db.connection() as conn:
        assert etsy_publisher.live_listings(conn, agent.scope()) == []
        [row] = etsy_publisher.idle_listings(conn, agent.scope())
        assert etsy_publisher.state_text(row) == "not in Etsy's last answer"
    reads: list[int] = []

    def gone(one: int) -> None:
        reads.append(one)
        return None

    shop.listing = gone  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    assert stored(agent)["state"] == "removed" and reads == [listing_id]  # Etsy has none: not asked every hour


def test_an_unread_listing_past_its_end_counts_as_expired(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    shop = agent.etsy.shop()
    shop.listings = lambda ids: []  # type: ignore[method-assign]

    def unreadable(one: int) -> etsy.RemoteListing:
        raise etsy.NotSent("HTTP 429: too many requests")

    shop.listing = unreadable  # type: ignore[method-assign]
    agent.clock.advance(days=etsy.LISTING_DAYS + 1)
    assert agent.publisher.sync(force=True) is None
    assert stored(agent)["state"] == "expired"  # its end passed, and it doesn't renew itself


def test_the_live_shop_reads_one_listing_whatever_its_state(tmp_path: Path) -> None:
    shop, server = live_shop(
        tmp_path,
        {
            ("GET", "/v3/application/listings/5"): {"listing_id": 5, "state": "expired", "title": "Planner"},
            ("GET", "/v3/application/listings/7"): {"results": []},
        },
    )
    found = shop.listing(5)
    assert found is not None and (found.listing_id, found.state) == (5, "expired")
    assert shop.listing(6) is None  # HTTP 404: Etsy has none
    with pytest.raises(etsy.NotSent, match="no listing"):
        shop.listing(7)  # an answer without one isn't taken for "there is none"
    assert server.requests[0].headers["authorization"].startswith("Bearer ")  # as the shop's owner


def test_the_state_etsy_answers_with_is_recorded(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    agent.clock.advance(days=etsy.LISTING_DAYS + 1)
    assert agent.publisher.sync(force=True) is None and stored(agent)["state"] == "expired"
    monkeypatch.setattr(etsy.FakeShop, "set_state", lambda self, listing_id, state: "inactive")
    request = a_change(agent, shop_context(agent), listing_id, state="renew")
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "done")]
    assert stored(agent)["state"] == "inactive"  # 0.13.0: 'active', as asked


# --- FIX 26f: an order with many lines ------------------------------------------------------------------------------


def test_an_order_with_many_lines_is_stored_and_one_bad_order_never_stops_the_sync(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    line = {"listing_id": listing_id, "title": "A planner " + "x" * 120, "quantity": 1, "price_cents": 450}
    many = order(agent, 71, items_cents=24 * 450)
    many.items[:] = [dict(line) for _ in range(24)]  # 0.13.0: cut mid-JSON, and every sync failed for 30 days
    more = order(agent, 72, items_cents=80 * 450)
    more.items[:] = [dict(line) for _ in range(80)]
    bad = order(agent, 73, currency="EURO")  # the table refuses it
    shop_with(agent, [many, bad, more, order(agent, 74)])
    assert agent.publisher.sync(force=True) is None
    saved = {r["receipt_id"]: json.loads(r["items"]) for r in rows(agent, "SELECT receipt_id, items FROM etsy_orders")}
    assert sorted(saved) == [71, 72, 74]
    assert len(saved[71]) == 24 and "title" not in saved[71][0]  # the titles are the listing's
    assert saved[72] == [{"listing_id": listing_id, "quantity": 80, "price_cents": 450}]  # one line per listing
    with agent.db.connection() as conn:
        assert etsy_publisher.sold_counts(conn, agent.scope()) == {listing_id: 24 + 80 + 1}
    error = agent.integrations()["etsy"]["last_error"]
    assert error.startswith("Etsy order 73 couldn't be stored")  # on the dashboard, not only in the log
    shop_with(agent, [order(agent, 74)])
    assert agent.publisher.sync(force=True) is None and agent.integrations()["etsy"]["last_error"] is None


def test_a_sync_that_cant_store_its_numbers_says_so(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    last = agent.db.get_meta(etsy_publisher.meta_key("dry_run", "last_sync_at"))

    def broken(*args: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(etsy_publisher, "observe", broken)
    agent.clock.advance(hours=2)
    error = agent.publisher.sync(force=True)
    assert error == "Etsy's numbers couldn't be stored (OperationalError)"
    assert agent.integrations()["etsy"]["last_error"] == error
    assert agent.db.get_meta(etsy_publisher.meta_key("dry_run", "last_sync_at")) == last  # the next one reads again


# --- FIX 26g: orders since the last sync that worked ----------------------------------------------------------------


def test_orders_are_read_back_to_the_last_sync_that_worked(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    worked = agent.clock.now()
    agent.clock.advance(days=40)  # dead, killed or offline meanwhile
    placed = etsy.Order(71, to_iso(worked + timedelta(days=5)), 450, "EUR", [LINE], items_cents=450)
    asked: list[Any] = []

    def orders(since: Any) -> list[etsy.Order]:
        asked.append(since)
        return [placed] if since <= from_iso(placed.ordered_at) else []

    shop = agent.etsy.shop()
    shop.orders = orders  # type: ignore[method-assign]
    shop.payment_fees = lambda receipt_id: 48  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    assert asked[-1] == worked - timedelta(days=1)  # with a day's overlap
    assert [r["receipt_id"] for r in rows(agent, "SELECT receipt_id FROM etsy_orders")] == [71]  # 0.13.0: lost
    assert agent.publisher.sync(force=True) is None
    assert asked[-1] == agent.clock.now() - timedelta(days=etsy_publisher.ORDER_DAYS)  # after a sync that worked
    agent.clock.advance(days=500)
    assert agent.publisher.sync(force=True) is None
    assert asked[-1] == agent.clock.now() - timedelta(days=etsy_publisher.CATCH_UP_DAYS)  # at most a year


def test_a_catch_up_reads_the_fees_of_every_order_it_stores(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    worked = agent.clock.now()
    agent.clock.advance(days=40)
    placed = [
        etsy.Order(500 + n, to_iso(worked + timedelta(days=2)), 450, "EUR", [LINE], items_cents=450) for n in range(15)
    ]
    shop = agent.etsy.shop()
    shop.orders = lambda since: [o for o in placed if since <= from_iso(o.ordered_at)]  # type: ignore[method-assign]
    shop.payment_fees = lambda receipt_id: 48  # type: ignore[method-assign]
    for _ in range(3):
        assert agent.publisher.sync(force=True) is None
    fees = rows(agent, "SELECT receipt_id, fees_cents FROM etsy_orders ORDER BY receipt_id")
    assert len(fees) == 15 and all(f["fees_cents"] for f in fees)  # 0.14.0 at first: five kept no fees for good
    asked: list[Any] = []
    shop.orders = lambda since: asked.append(since) or []  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    assert asked == [agent.clock.now() - timedelta(days=etsy_publisher.ORDER_DAYS)]  # all read: 30 days again


def test_a_catch_up_reads_every_page_of_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(etsy_live, "RECEIPT_PAUSE", 0)
    pages: list[int] = []

    def receipts(request: Any) -> dict[str, Any]:
        pages.append(int(request.url.params["offset"]))
        count = etsy_live.PAGE if len(pages) < 7 else 3
        start = 1_000 * len(pages)
        return {"results": [{"receipt_id": start + n, "status": "paid"} for n in range(count)]}

    shop, _ = live_shop(tmp_path, {("GET", "/v3/application/shops/777/receipts"): receipts})
    found = shop.orders(shop.clock.now() - timedelta(days=40))
    assert len(pages) == 7 and len(found) == 6 * etsy_live.PAGE + 3  # 0.13.0: five pages at most
    pages.clear()
    assert len(shop.orders(shop.clock.now() - timedelta(days=30))) == 5 * etsy_live.PAGE  # an hourly sync: five


# --- FIX 26h: the Ledger tab's pages --------------------------------------------------------------------------------


def ledger_join() -> str:
    script = APP_JS.read_text(encoding="utf-8")
    found = re.search(r"\n  function ledgerJoin\(page, kept, size\) \{\n.*?\n  \}\n", script, re.DOTALL)
    assert found is not None, "app.js has no ledgerJoin"
    return found.group(0)


def test_the_ledger_tab_neither_skips_nor_repeats_entries() -> None:
    source = ledger_join()
    script = APP_JS.read_text(encoding="utf-8")
    assert "var entries = ledgerJoin(newest, arr(ui.ledgerOlder), LEDGER_NEWEST);" in script
    assert "fillLedgerGap(newest);" in script
    node = shutil.which("node")
    if node is None:
        pytest.skip("node isn't installed")
    driver = source + (
        "\nfunction ids(a, b) { var r = []; for (var i = a; i >= b; i--) r.push({ id: i }); return r; }"
        "\nfunction span(list) { return list.map(function (e) { return e.id; }); }"
        # The owner loaded older entries (181-200 shown, 81-180 loaded); a cycle then booked 30 more.
        "\nvar kept = ids(200, 81);"
        "\nvar gap = ledgerJoin(ids(230, 211), kept, 20);"
        # The page under the newest one closes the gap.
        "\nvar filled = ids(230, 211).concat(ledgerJoin(ids(210, 111), kept, 100));"
        "\nvar close = ledgerJoin(ids(215, 196), kept, 20);"
        "\nvar far = ledgerJoin(ids(500, 401), kept, 100);"
        "\nconsole.log(JSON.stringify({gap: gap, filled: span(filled), close: span(close), far: far}));"
    )
    done = subprocess.run([node, "-e", driver], capture_output=True, text=True, timeout=60, check=True)  # noqa: S603
    result = json.loads(done.stdout)
    assert result["gap"] is None  # 0.13.0: 230-211 then 180-81, without 210-181
    assert result["filled"] == list(range(230, 80, -1))  # every entry once
    assert result["close"] == list(range(215, 80, -1))
    assert result["far"] is None  # beyond a page: the older ones go, the button loads them again


def test_summed_lines_keep_each_price() -> None:
    lines = [
        {"listing_id": 5, "title": "x" * 130, "quantity": 1, "price_cents": 450 + 100 * (n % 2)} for n in range(90)
    ]
    stored_lines = json.loads(etsy_publisher.stored_items(lines))
    assert sorted((i["price_cents"], i["quantity"]) for i in stored_lines) == [(450, 45), (550, 45)]
