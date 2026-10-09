"""A paid order of Ember's whose payment Etsy has none of (payment_fees -> None: a free order with a 100% coupon, say)
keeps fees_cents NULL for good: every hourly sync reads Etsy's receipts back to that order's day (up to a year, and
from day 31 on with the catch-up's 100 pages), and asks for its payment again."""

from __future__ import annotations

from datetime import timedelta

import harness  # noqa: F401

from app.economy.clock import from_iso, to_iso
from app.integrations import etsy
from tests.test_etsy import listed

agent, listing_id = listed(harness.DATA)
fake = agent.etsy.shop()
seen: list[tuple[str, int]] = []
asked: list[int] = []
free = etsy.Order(
    receipt_id=1234,
    ordered_at=to_iso(agent.clock.now()),
    total_cents=0,
    currency="EUR",
    items=[{"listing_id": listing_id, "title": "x", "quantity": 1, "price_cents": 490}],
    status="paid",
    items_cents=490,
    discount_cents=490,  # a 100% coupon
)


class Shop:
    def __init__(self, inner):
        self.inner = inner
        self.simulated = inner.simulated

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def orders(self, since):
        days = (agent.clock.now() - since).days
        seen.append((to_iso(since)[:10], days))
        return [free] if from_iso(free.ordered_at) >= since else []

    def payment_fees(self, receipt_id):
        asked.append(receipt_id)
        return None if receipt_id == 1234 else self.inner.payment_fees(receipt_id)


agent.publisher.shop = lambda: Shop(fake)
for day in range(0, 120, 30):
    agent.clock.advance(days=30 if day else 0, hours=1)
    agent.publisher.sync(force=True)
print("each sync's `since` (day, days back):", seen)
print("payment_fees asked for receipt 1234:", asked.count(1234), "times in", len(seen), "syncs")
print(harness.rows(agent, "SELECT receipt_id, status, total_cents, fees_cents FROM etsy_orders"))
print("days back > 31 means the catch-up's", 100, "pages (etsy_live.orders: CATCH_UP_PAGES) at each hourly sync")
