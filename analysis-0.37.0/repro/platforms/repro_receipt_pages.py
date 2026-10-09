"""The live shop's orders(): a sync within 31 days reads at most 5 pages of 100 receipts (newest first); in a shop with
more receipts changed in that window, an older one changed lately (a refund of one of Ember's orders) is never read."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import harness  # noqa: F401

import httpx2

from app.integrations import etsy_live
from tests.test_etsy import live_shop

etsy_live.time.sleep = lambda s: None  # the 1.1 s pause between pages
TOTAL = 650  # receipts changed in the last 30 days (the owner's own products and Ember's)
REFUNDED = 600  # Ember's order, 600th newest, refunded yesterday


def receipts(request):
    offset = int(request.url.params["offset"])
    page = []
    for n in range(offset, min(offset + 100, TOTAL)):
        page.append(
            {
                "receipt_id": 10_000 + n,
                "status": "Fully Refunded" if n == REFUNDED else "Completed",
                "is_paid": True,
                "created_timestamp": 1_790_000_000 - n * 3600,
                "grandtotal": {"amount": 490, "divisor": 100, "currency_code": "EUR"},
                "transactions": [{"listing_id": 900_000_001 if n == REFUNDED else 5, "quantity": 1,
                                  "price": {"amount": 490, "divisor": 100, "currency_code": "EUR"}}],
            }
        )
    return httpx2.Response(200, json={"count": TOTAL, "results": page})


shop, server = live_shop(Path(harness.DATA), {("GET", "/v3/application/shops/777/receipts"): receipts})
orders = shop.orders(shop.clock.now() - timedelta(days=30))
print("receipts changed in the window:", TOTAL, "| read:", len(orders), "| requests:", len(server.requests))
print("Ember's refunded order 600 read:", any(o.receipt_id == 10_000 + REFUNDED for o in orders))
