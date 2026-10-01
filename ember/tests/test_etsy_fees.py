"""0.12.0: Etsy's fees were never booked, so an order's revenue counted in full. The sync reads each paid order's
payment once (its processing fee) and adds Etsy's transaction fee on Ember's lines; the owner records that share as an
expense of the same project next to the order's revenue, and the plan sees the week's orders less their fees."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.integrations import etsy, etsy_publisher  # noqa: E402
from tests.economy_helpers import owner as owner_entry  # noqa: E402
from tests.test_etsy import listed, live_shop  # noqa: E402

LINES = [
    {"listing_id": 900_000_001, "title": "Ember's planner", "quantity": 1, "price_cents": 450},
    {"listing_id": 1, "title": "The owner's own", "quantity": 1, "price_cents": 450},
]


def test_embers_share_of_the_fees() -> None:
    order = etsy.Order(71, "2026-09-02T10:00:00Z", 900, "EUR", LINES, items_cents=900)
    # half the processing fee; 0.14.0: 6.5% of 4.50 and USD 0.20 (at 1.10), with 19% VAT on both (29.25 + 18.18)
    assert etsy.fees_share(order, LINES[:1], 48) == 24 + 56
    coupon = etsy.Order(72, "2026-09-02T10:00:00Z", 800, "EUR", LINES, items_cents=900, discount_cents=100)
    assert etsy.fees_share(coupon, LINES[:1], 48) == 24 + 53  # on the 4.00 the line earned after its coupon share


def recorded(agent: Any) -> dict[str, Any]:
    [order] = agent.integrations()["etsy"]["orders"]
    return order


def test_an_orders_fees_are_read_once_and_recorded_as_an_expense(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    shop = agent.etsy.shop()
    reads: list[int] = []

    def fees(receipt_id: int) -> int:
        reads.append(receipt_id)
        return 48  # 4% of 9.00 EUR and 0.30

    shop.payment_fees = fees  # type: ignore[method-assign]
    shop.orders = lambda since: [etsy.Order(71, "2026-09-02T10:00:00Z", 900, "EUR", LINES, items_cents=900)]  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    order = recorded(agent)
    assert order["fees_cents"] == 80 and not order["fees_recordable"]  # the revenue comes first
    assert agent.publisher.sync(force=True) is None and reads == [71]  # a payment is read once
    with agent.db.connection() as conn:
        text = etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "EmberTestShop", 3)
    assert "Orders in the last 7 days: 1 (4.50 EUR less 0.80 of Etsy's fees)" in text
    owner_entry(agent.economy, "revenue", "4.50", idempotency_key=order["revenue_key"], test_money=True)
    order = recorded(agent)
    assert order["fees_recordable"] and not order["fees_recorded"]
    owner_entry(agent.economy, "expense", "0.80", idempotency_key=order["fee_key"], test_money=True)
    order = recorded(agent)
    assert order["fees_recorded"] and not order["fees_recordable"]
    script = (Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "Record Etsy's fees" in script and "idKey: String(o.fee_key" in script


def test_a_payment_that_cant_be_read_never_stops_the_sync(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    shop = agent.etsy.shop()

    def refuse(receipt_id: int) -> int:
        raise etsy.NotSent("HTTP 403: insufficient scope")

    shop.payment_fees = refuse  # type: ignore[method-assign]
    shop.orders = lambda since: [etsy.Order(71, "2026-09-02T10:00:00Z", 900, "EUR", LINES, items_cents=900)]  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    order = recorded(agent)
    assert order["fees_cents"] is None and order["total"] == "4.50 EUR"  # stored all the same, the fees later


def test_the_live_shop_reads_a_payments_fees(tmp_path: Path) -> None:
    path = "/v3/application/shops/777/receipts/{}/payments"
    fee = {"amount": 48, "divisor": 100, "currency_code": "EUR"}
    shop, server = live_shop(
        tmp_path,
        {
            ("GET", path.format(5)): {"count": 1, "results": [{"payment_id": 9, "amount_fees": fee}]},
            ("GET", path.format(6)): {
                "count": 1,
                "results": [{"amount_fees": fee, "adjusted_fees": {"amount": 30, "divisor": 100}}],  # refunded in part
            },
            ("GET", path.format(7)): {"count": 0, "results": []},
        },
    )
    assert (shop.payment_fees(5), shop.payment_fees(6), shop.payment_fees(7)) == (48, 30, None)
    assert [r.method for r in server.requests] == ["GET", "GET", "GET"]
