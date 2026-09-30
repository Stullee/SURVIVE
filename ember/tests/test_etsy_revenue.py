"""0.12.0: Ember's Etsy orders in the ledger, from Etsy's own numbers. Revenue only ever came from the owner, so a
sale counted nowhere (the balance, the runway, the money goal, a venture's earnings) until the owner recorded it by
hand. Once the owner turns it on, each sync records the revenue of paid orders with Ember's listings, Etsy's fees on
them and their refunds, as entries made by 'etsy', with the keys of the owner's buttons: an order is recorded once,
an order the owner recorded stays theirs, and an entry that would kill the agent waits for the owner."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.config import Settings  # noqa: E402
from app.db import Database, discover_migrations, migrate  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import etsy, etsy_publisher, etsy_revenue  # noqa: E402
from tests.economy_helpers import owner as owner_entry  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402

LISTING = 900_000_001
LINE = {"listing_id": LISTING, "title": "Ember's planner", "quantity": 1, "price_cents": 450}


def order(agent: Any, receipt: int, **changes: Any) -> etsy.Order:
    fields: dict[str, Any] = dict(items_cents=450, status="paid")
    fields.update(changes)
    ordered = to_iso(agent.clock.now() - timedelta(minutes=5))
    return etsy.Order(receipt, ordered, 450, fields.pop("currency", "EUR"), [LINE], **fields)


def shop_with(agent: Any, orders: list[etsy.Order], processing: int = 48) -> None:
    shop = agent.etsy.shop()
    shop.orders = lambda since: orders  # type: ignore[method-assign]
    shop.payment_fees = lambda receipt_id: processing  # type: ignore[method-assign]


def turned_on(agent: Any, rate: float = 1.1) -> None:
    agent.publisher.settings = agent.settings.model_copy(
        update={"etsy_auto_record_revenue": True, "etsy_usd_per_eur": rate}
    )


def entries(agent: Any) -> list[dict[str, Any]]:
    return rows(
        agent,
        "SELECT id, type, amount_micros, simulated, source, note, corrects_id, orig_amount, orig_currency, fx_rate,"
        " created_by, project_id, venture_id FROM ledger WHERE created_by = 'etsy' ORDER BY id",
    )


def test_a_paid_order_and_its_fees_are_recorded_once(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    shop_with(agent, [order(agent, 71)])
    assert agent.publisher.sync(force=True) is None
    assert entries(agent) == []  # off until the owner turns it on
    before = agent.economy.status().balance
    turned_on(agent)
    assert agent.publisher.sync(force=True) is None
    [shown] = agent.integrations()["etsy"]["orders"]
    revenue, fees = entries(agent)
    assert revenue == {
        "id": revenue["id"],
        "type": "revenue",
        "amount_micros": 4_950_000,  # 4.50 EUR at 1.1
        "simulated": 1,  # the fake shop's order: test money
        "source": "Etsy order 71",
        "note": etsy_revenue.REVENUE_NOTE,
        "corrects_id": None,
        "orig_amount": "4.50",
        "orig_currency": "EUR",
        "fx_rate": "1.1",
        "created_by": "etsy",
        "project_id": shown["project_id"],  # the project whose listing sold
        "venture_id": shown["venture_id"],
    }
    assert shown["project_id"] is not None
    assert (fees["type"], fees["amount_micros"], fees["orig_amount"]) == ("expense", 850_000, "0.77")  # 0.48 + 6.5%
    assert fees["project_id"] == shown["project_id"] and fees["note"].startswith("Etsy's fees on order 71")
    assert agent.economy.status().balance == before + 4_950_000 - 850_000
    assert shown["recorded"] and shown["recorded_by"] == "etsy" and shown["fees_recorded"]
    assert not shown["recordable"] and not shown["fees_recordable"]  # nothing left for the owner's buttons
    events = [e["message"] for e in agent.db.recent_events(limit=20)]
    assert any(
        m.startswith(
            f"Ember's code recorded revenue of $4.95 from Etsy's numbers: Etsy order 71 (test money) (entry #"
            f"{revenue['id']})"
        )
        for m in events
    ), events
    assert agent.publisher.sync(force=True) is None
    assert len(entries(agent)) == 2  # never twice
    late = agent.economy.record(  # the owner's button, from a page that hadn't seen it yet
        "revenue",
        {
            "amount": "4.50",
            "currency": "EUR",
            "fx_rate": "1.1",
            "source": "Etsy order 71",
            "test_money": True,
            "idempotency_key": shown["revenue_key"],
        },
    )
    assert (
        late.status == 409
        and late.body["code"] == "already_recorded"
        and late.body["error"] == (f"Ember's code already recorded this from Etsy's numbers (entry #{revenue['id']})")
    )
    with agent.db.connection() as conn:
        text = etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "Shop", 3, auto_revenue=True)
    assert "revenue counts once Ember's code records it from Etsy's numbers." in text


def test_refunds_correct_what_embers_code_recorded_and_nothing_else(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    turned_on(agent)
    theirs = order(agent, 72)
    owner_entry(agent.economy, "revenue", "4.00", idempotency_key=etsy_publisher.revenue_key(72), test_money=True)
    shop_with(agent, [order(agent, 71), theirs])
    assert agent.publisher.sync(force=True) is None
    revenue, fees = entries(agent)
    assert revenue["source"] == "Etsy order 71"  # order 72 is the owner's: no revenue, no fees
    shop_with(
        agent,
        [
            order(agent, 71, status="partially refunded", refunded_cents=150),
            order(agent, 72, status="fully refunded", refunded_cents=450),
        ],
    )
    assert agent.publisher.sync(force=True) is None
    partly = entries(agent)[2]
    assert (partly["corrects_id"], partly["amount_micros"]) == (revenue["id"], -1_650_000)  # it earns 3.30 now
    assert partly["note"] == "Etsy order 71 was partly refunded: Ember's lines earn 3.00 EUR now"
    assert partly["project_id"] == revenue["project_id"]
    shop_with(agent, [order(agent, 71, status="fully refunded", refunded_cents=450)])
    assert agent.publisher.sync(force=True) is None
    assert agent.publisher.sync(force=True) is None
    fully = entries(agent)[3:]
    assert [(e["corrects_id"], e["amount_micros"], e["note"]) for e in fully] == [
        (revenue["id"], -3_300_000, "Etsy order 71 was fully refunded")
    ]
    assert rows(agent, "SELECT COUNT(*) AS n FROM ledger WHERE corrects_id IS NOT NULL AND created_by = 'owner'") == [
        {"n": 0}
    ]  # the owner's order 72 waits for the owner's correction
    shown = {o["receipt_id"]: o for o in agent.integrations()["etsy"]["orders"]}
    assert shown[71]["corrected_in_full"] and not shown[72]["corrected_in_full"]
    assert shown[72]["recorded_by"] == "owner"
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="correct only the entries"):
        conn.execute(
            "INSERT INTO ledger (ts, occurred_on, type, amount_micros, simulated, corrects_id, note, created_by,"
            " idempotency_key) SELECT ts, occurred_on, 'revenue', -100, simulated, id, 'x', 'etsy', 'k' FROM ledger"
            " WHERE idempotency_key = ?",
            (etsy_publisher.revenue_key(72),),
        )


def test_orders_in_euro_need_the_owners_rate(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    turned_on(agent, rate=0)
    shop_with(agent, [order(agent, 71), order(agent, 73, currency="USD")])
    assert agent.publisher.sync(force=True) is None
    [usd] = [e for e in entries(agent) if e["type"] == "revenue"]
    assert (usd["source"], usd["amount_micros"], usd["orig_currency"], usd["fx_rate"]) == (
        "Etsy order 73",
        4_500_000,
        None,
        None,
    )
    shown = {o["receipt_id"]: o for o in agent.integrations()["etsy"]["orders"]}
    assert shown[71]["recordable"] and not shown[71]["recorded"]  # the owner's button is still there


def test_an_entry_that_would_kill_the_agent_waits_for_the_owner(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    turned_on(agent)
    balance = agent.economy.status().balance
    shop_with(agent, [order(agent, 71)], processing=balance // 1_000 + 100_000)  # fees beyond all it has
    assert agent.publisher.sync(force=True) is None
    assert [e["type"] for e in entries(agent)] == ["revenue"]  # the fees wait
    assert agent.economy.status().state != "dead"
    assert agent.publisher.sync(force=True) is None
    warnings = [e["message"] for e in agent.db.recent_events(limit=30) if e["message"].startswith("Ember's code did")]
    assert len(warnings) == 1 and "it would kill the agent" in warnings[0], warnings  # told once
    [shown] = agent.integrations()["etsy"]["orders"]
    assert shown["fees_recordable"]  # the owner's button decides, with its question


def test_turning_it_on_and_off_is_audited(data_dir: Path) -> None:
    agent, _ = listed(data_dir)

    def said() -> list[str]:
        return [e["message"] for e in agent.db.recent_events(limit=50) if "Automatic recording" in e["message"]]

    assert said() == []  # never on: nothing to say
    today = agent.clock.today().isoformat()
    etsy_revenue.audit(agent.db, agent.clock, Settings(etsy_auto_record_revenue=True, etsy_usd_per_eur=1.08))
    agent.clock.advance(days=1)
    etsy_revenue.audit(agent.db, agent.clock, Settings(etsy_auto_record_revenue=True, etsy_usd_per_eur=1.08))
    assert len(said()) == 1 and "orders in EUR at 1.08 USD per EUR" in said()[0]
    assert f"orders placed from {today} on" in said()[0] and agent.db.get_meta(etsy_revenue.SINCE_KEY) == today
    etsy_revenue.audit(agent.db, agent.clock, Settings(etsy_auto_record_revenue=True))
    assert "orders in EUR are left to you" in said()[0]
    assert agent.db.get_meta(etsy_revenue.SINCE_KEY) == today  # still on: still from that day
    etsy_revenue.audit(agent.db, agent.clock, Settings())
    assert said()[0].startswith("Automatic recording of Etsy revenue is off") and len(said()) == 3
    assert not agent.db.get_meta(etsy_revenue.SINCE_KEY)


def test_orders_from_before_it_was_turned_on_stay_the_owners(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    etsy_revenue.audit(agent.db, agent.clock, Settings(etsy_auto_record_revenue=True, etsy_usd_per_eur=1.1))
    turned_on(agent)
    earlier = etsy.Order(70, to_iso(agent.clock.now() - timedelta(days=2)), 450, "EUR", [LINE], items_cents=450)
    shop_with(agent, [earlier, order(agent, 71)])
    assert agent.publisher.sync(force=True) is None
    assert [e["source"] for e in entries(agent) if e["type"] == "revenue"] == ["Etsy order 71"]
    shown = {o["receipt_id"]: o for o in agent.integrations()["etsy"]["orders"]}
    assert shown[70]["recordable"] and not shown[70]["recorded"]  # its button is still there


def test_the_rate_must_be_one_the_ledger_takes() -> None:
    with pytest.raises(ValueError, match="between 0.5 and 3"):
        Settings(etsy_usd_per_eur=0.2)
    assert Settings(etsy_usd_per_eur=1.08).etsy_usd_per_eur == 1.08


def test_the_ledger_keeps_its_rows_and_takes_etsys_numbers(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 34], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        insert = (
            "INSERT INTO ledger (id, ts, occurred_on, type, amount_micros, note, corrects_id, created_by,"
            " idempotency_key) VALUES (?, 'then', '2026-09-01', ?, ?, 'x', ?, 'owner', ?)"
        )
        conn.execute(insert, (5, "revenue", 1_000_000, None, "a"))
        conn.execute(insert, (9, "revenue", -1_000, 5, "b"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert.replace("'owner'", "'etsy'"), (10, "revenue", 1_000, None, "c"))
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == list(range(35, 53))
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        assert [tuple(r) for r in conn.execute("SELECT id, amount_micros, corrects_id FROM ledger")] == [
            (5, 1_000_000, None),
            (9, -1_000, 5),
        ]
        conn.execute(insert.replace("'owner'", "'etsy'"), (10, "expense", 1_000, None, "c"))
        for kind in ("owner_grant", "adjustment"):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                conn.execute(insert.replace("'owner'", "'etsy'"), (11, kind, 1_000, None, "d"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE ledger SET amount_micros = 1 WHERE id = 5")
