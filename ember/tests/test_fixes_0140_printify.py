"""0.15.0: Printify's money records, currency, reconciliation and metrics (FIX NOW 16, X22).

A POD sale was booked lopsidedly: the margin check used a looser fee model than econ's and no VAT, the shipping a buyer
paid was dropped from revenue while Printify's shipping was a cost, and Printify's bill was overhead while the sale
went to the venture. Amounts carried the option's currency, not Printify's. A publish that timed out left a live
listing Ember never tracked, and one that never reached Etsy stayed 'publishing'. Printify's listings, and product
lines without a venture, counted nowhere in the metrics, the listing test and the ventures' rules. The agent couldn't
know a product's cost before proposing it. And a product that missed its margin was said to be deleted when its delete
failed."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from app.agent import econ, gates, metrics, never, predictions, stages, tools, ventures  # noqa: E402
from app.agent.store import canonical, create_project, insert_approval  # noqa: E402
from app.db import Database, discover_migrations, migrate  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import etsy, etsy_live, printify, printify_publisher  # noqa: E402
from app.integrations.printify import Gone, NotSent, Unclear  # noqa: E402
from tests.economy_helpers import owner as owner_entry  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_printify import (  # noqa: E402
    LARGE,
    POSTER,
    SENSARIA,
    SMALL,
    a_proposal,
    listed,
    live,
    made_rows,
    pod_context,
    proposed,
    read_catalog,
)

APP = Path(__file__).parents[1] / "app"


def approved(data_dir: Path, **args: Any) -> tuple[Any, int]:
    agent, _, request = proposed(data_dir, **args)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    return agent, request


def events(agent: Any) -> list[str]:
    return [r["message"] for r in rows(agent, "SELECT message FROM events ORDER BY id")]


def project_of(agent: Any, request: int) -> int:
    return rows(agent, f"SELECT project_id FROM approvals WHERE id = {request}")[0]["project_id"]


def venture_titled(agent: Any, title: str) -> int:
    return rows(agent, f"SELECT id FROM ventures WHERE title = '{title}'")[0]["id"]


# --- the margin check: econ's fee model, with VAT -------------------------------------------------------------------


def test_the_margin_check_uses_econ_s_fee_model_with_vat() -> None:
    # One fee model: econ's (the listing fee at the rate, 6.5%, processing 4% and 0.30, VAT on Etsy's fees).
    assert printify.fees(2500) == Decimal(str(econ.fees("etsy_physical", 25.0, econ.DEFAULT_USD_PER_EUR))) * 100
    assert printify.fees(2500, "EUR", 1.25) == Decimal(str(econ.fees("etsy_physical", 25.0, 1.25))) * 100
    assert printify.fees(2500, "USD") == Decimal(str(econ.fees("etsy_physical", 25.0, 1.0))) * 100  # USD 0.20 itself
    # The owner's live case (EUR 25, shipping 6.09): 0.13.0 passed a making cost of 12.04 as keeping 15%. With econ's
    # fees and VAT on Printify's bill that sale keeps nothing.
    assert printify.kept(2500, 1204, 609) == round(2500 - printify.fees(2500) - Decimal(1813) * Decimal("1.19"))
    assert printify.kept(2500, 1204, 609) < 0 and not printify.keeps(2500, 1204, 609)
    least = printify.least_price(1204, 609)
    assert least % 10 == 0 and printify.keeps(least, 1204, 609) and not printify.keeps(least - 10, 1204, 609)
    assert least > 2500
    assert printify.least_price(900, 609, printify.Terms("EUR", 1.10)) == printify.least_price(900, 609)


def test_a_price_that_keeps_too_little_says_so_with_the_owner_s_rate(data_dir: Path) -> None:
    agent, request = approved(data_dir, prices=f"{SMALL}: 17.90")
    agent.settings = agent.pod.settings = agent.settings.model_copy(update={"etsy_usd_per_eur": 1.25})
    assert agent.execute_approved() == [(request, "failed")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    least = printify.money(printify.least_price(790, 450, printify.Terms("EUR", 1.25)), "EUR")
    assert "(the price alone pays the shipping; Printify's bill plus 19%)" in note and f"at least {least}" in note
    assert json.loads(made_rows(agent)[0]["prices"]) == [
        [SMALL, 1790, 790, 450, printify.kept(1790, 790, 450, printify.Terms("EUR", 1.25))]
    ]


def test_the_approval_card_says_what_the_check_assumes_without_a_legal_word(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    [row] = rows(agent, f"SELECT * FROM approvals WHERE id = {request}")
    assert "Published only if each price keeps 15% after Etsy's fees, making and shipping" in row["payload"]
    with agent.db.connection() as conn:
        assert never.reasons(conn, row) == ["first_publication"]  # not 'legal' too


def test_who_pays_shipping_and_vat_on_the_bill_are_the_owner_s_options(data_dir: Path) -> None:
    # The live case (making 9.00, shipping 6.09): the price alone pays the shipping by default, with VAT on the bill.
    free, buyer = printify.Terms(), printify.Terms(buyer_ships=True)
    assert printify.least_price(900, 609, free) == 2530
    # When the buyer pays the shipping, it is revenue with Etsy's fees on it, and 15% is of price and shipping.
    least = printify.least_price(900, 609, buyer)
    assert least == 1920 and least < 2530
    paid = Decimal(least + 609)
    bill = Decimal(1509) * Decimal("1.19")
    assert printify.kept(least, 900, 609, buyer) == round(paid - printify.fees(least + 609) - bill)
    assert printify.keeps(least, 900, 609, buyer) and not printify.keeps(least - 10, 900, 609, buyer)
    # Without VAT on Printify's bill (a seller who reclaims it), less is needed.
    assert printify.least_price(900, 609, printify.Terms(buyer_ships=True, bill_vat=False)) < least
    # The options reach the catalog, the proposal and the execution.
    agent, _ = listed(data_dir)
    settings = agent.settings.model_copy(update={"printify_buyer_pays_shipping": True, "printify_bill_vat": False})
    assert printify.terms(settings) == printify.Terms("EUR", settings.etsy_usd_per_eur, True, False)
    assert (
        "the buyer pays the shipping; Printify's bill as billed"
        in printify.Terms(buyer_ships=True, bill_vat=False).said()
    )
    ctx = pod_context(agent)
    ctx.printify = dataclasses.replace(ctx.printify, buyer_ships=True)
    read_catalog(ctx)
    with agent.db.transaction() as conn:
        printify_publisher.keep_costs(
            conn, agent.scope().mode, POSTER, SENSARIA, {SMALL: 790}, to_iso(agent.clock.now())
        )
    least = printify.least_price(790, 450, printify.Terms(buyer_ships=True))
    assert least < printify.least_price(790, 450)
    made = a_proposal(agent, ctx, prices=f"{SMALL}: {least / 100:.2f}")
    assert made.ok, made.text
    [request] = rows(agent, "SELECT id, payload FROM approvals WHERE executor = 'printify_product'")
    assert "(the buyer pays the shipping; Printify's bill plus 19%)" in request["payload"]
    assert owner(agent).decide(request["id"], {"decision": "approve"}, "Owner").status == 200
    agent.settings = agent.pod.settings = agent.settings.model_copy(update={"printify_buyer_pays_shipping": True})
    assert agent.execute_approved() == [(request["id"], "active")]


def test_a_margin_in_another_currency_than_eur_or_usd_is_refused(data_dir: Path) -> None:
    # Etsy's USD listing fee was converted at the EUR rate for any currency.
    with pytest.raises(printify.PrintifyError, match="can't check a margin in GBP"):
        printify.fees(2500, "GBP", 1.1)
    with pytest.raises(printify.PrintifyError):
        printify.least_price(900, 609, printify.Terms("GBP"))
    agent, _ = listed(data_dir)
    ctx = pod_context(agent)
    read_catalog(ctx)
    ctx.printify = dataclasses.replace(ctx.printify, currency="GBP")
    shop = ctx.etsy
    ctx.etsy = tools.EtsyAccess(shop_name="EmberTestShop", currency="GBP", daily_limit=3, categories=shop.categories)
    refused = a_proposal(agent, ctx)
    assert not refused.ok and "checks a margin in EUR or USD only, not GBP" in refused.text


# --- the shipping a buyer pays is revenue ---------------------------------------------------------------------------


def test_the_shipping_a_buyer_pays_counts_as_revenue() -> None:
    receipt = {
        "receipt_id": 7,
        "status": "paid",
        "is_paid": True,
        "created_timestamp": 1_790_000_000,
        "grandtotal": {"amount": 2940, "divisor": 100, "currency_code": "EUR"},
        "total_price": {"amount": 2490, "divisor": 100, "currency_code": "EUR"},
        "transactions": [
            {
                "listing_id": 800000003,
                "title": "Poster",
                "quantity": 1,
                "price": {"amount": 2490, "divisor": 100, "currency_code": "EUR"},
                "shipping_cost": {"amount": 450, "divisor": 100, "currency_code": "EUR"},
            }
        ],
    }
    order = etsy_live._order(receipt)
    assert order is not None and order.items[0]["shipping_cents"] == 450
    assert etsy.order_net(order, order.items) == 2940  # 0.13.0: 2490, while Printify's 4.50 shipping was a cost
    # A digital line has no shipping; a refund still takes its share.
    digital = etsy.Order(8, "t", 490, "EUR", [{"listing_id": 1, "quantity": 1, "price_cents": 490}])
    assert etsy.order_net(digital, digital.items) == 490
    refunded = etsy.Order(9, "t", 2940, "EUR", order.items, items_cents=2490, refunded_cents=1000)
    assert etsy.order_net(refunded, refunded.items) == 1940


# --- Printify's bill belongs to the product's project and venture, with its tax and currency -----------------------


def test_a_printify_order_s_cost_belongs_to_the_product_s_project_and_venture(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    agent.execute_approved()
    account = agent.printify.account()
    [product_id] = [p for p in account.state["products"]]
    account.sell(product_id)
    account.state["orders"][0]["tax_cents"] = 236  # what Printify bills as tax on it
    assert agent.pod.sync(force=True) is None
    project = project_of(agent, request)
    venture = rows(agent, f"SELECT venture_id FROM projects WHERE id = {project}")[0]["venture_id"]
    [order] = agent.integrations()["printify"]["orders"]
    assert (order["project_id"], order["venture_id"]) == (project, venture) and venture is not None
    assert order["cost_cents"] == 790 + 450 + 236
    script = (APP / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    cell = script[script.index("function printifyCostCell") : script.index("function renderTransitions")]
    assert "projectId: o.project_id" in cell and "ventureId: o.venture_id" in cell


def test_orders_carry_their_tax_share_and_the_currency_printify_states() -> None:
    order = {
        "id": "o1",
        "status": "fulfilled",
        "currency": "usd",
        "total_tax": 300,
        "created_at": "2026-09-30 10:00:00+00:00",
        "line_items": [
            {"product_id": "p1", "quantity": 1, "cost": 1000, "shipping_cost": 500},
            {"product_id": "p2", "quantity": 1, "cost": 500, "shipping_cost": 0},
        ],
    }
    account, _ = live({("GET", "/v1/shops/77/orders.json"): {"data": [order]}})
    lines = account.orders(77)
    assert [(o.product_id, o.tax_cents, o.currency) for o in lines] == [("p1", 225, "USD"), ("p2", 75, "USD")]


# --- the currency Printify states -----------------------------------------------------------------------------------


def test_an_amount_in_another_currency_is_converted_at_the_owner_s_rate_or_refused() -> None:
    assert printify.convert(450, "", "EUR") == printify.convert(450, "EUR", "EUR") == 450  # none stated: the option's
    assert printify.convert(450, "USD", "EUR", 1.25) == 360
    assert printify.convert(452, "USD", "EUR", 1.10) == 411  # 410.9, rounded up: it is a cost
    assert printify.convert(450, "EUR", "USD", 1.10) == 495
    with pytest.raises(printify.PrintifyError, match=r"in USD, not EUR .*no exchange rate \(etsy_usd_per_eur\)"):
        printify.convert(450, "USD", "EUR")
    with pytest.raises(printify.PrintifyError, match="in GBP, not EUR .*can't convert it"):
        printify.convert(450, "GBP", "EUR", 1.10)


def test_the_catalog_keeps_printify_s_currency_and_a_proposal_converts_or_refuses_it(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = pod_context(agent)
    read_catalog(ctx)
    with agent.db.connection() as conn:  # the currency Printify states is kept with the variants (0.13.0: dropped)
        kept = printify_publisher.variants(conn, agent.scope().mode, POSTER, SENSARIA)
    assert kept is not None and {v.currency for v in kept} == {"EUR"}
    with agent.db.transaction() as conn:  # Printify states its shipping in USD for this account
        printify_publisher._store(
            conn,
            agent.scope().mode,
            "variants",
            f"{POSTER}:{SENSARIA}",
            [[SMALL, "12x18 in", 3600, 5400, 450, "USD"], [LARGE, "24x36 in", 7200, 10800, 650, "USD"]],
            to_iso(agent.clock.now()),
        )
    shown = call(ctx, "printify_catalog", {"blueprint_id": POSTER, "provider_id": SENSARIA})
    assert "shipping to Germany 4.50 USD" in shown.text
    assert (
        "Shipping: Printify states it in USD, not EUR (printify_currency), and your owner set no exchange rate "
        "(etsy_usd_per_eur), so no product of it can be proposed." in shown.text
    )
    refused = a_proposal(agent, ctx)
    assert not refused.ok and "its shipping: Printify states it in USD, not EUR (printify_currency)" in refused.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM approvals WHERE executor = 'printify_product'")[0]["n"] == 0
    # The shop selling in another currency than the option is refused.
    shop = ctx.etsy
    ctx.etsy = tools.EtsyAccess(shop_name="EmberTestShop", currency="USD", daily_limit=3, categories=shop.categories)
    refused = a_proposal(agent, ctx)
    assert not refused.ok and "the Etsy shop sells in USD, but printify_currency is EUR" in refused.text
    ctx.etsy = shop
    # With the owner's rate it is converted: shown, checked and proposed in EUR.
    catalog = printify_publisher.Catalog(agent.db, agent.clock, agent.scope().mode, agent.printify.account)
    ctx.catalog = lambda search, blueprint, provider: catalog.answer(search, blueprint, provider, "EUR", 1.25)
    ctx.usd_per_eur = 1.25
    shown = call(ctx, "printify_catalog", {"blueprint_id": POSTER, "provider_id": SENSARIA})
    assert "shipping to Germany 3.60 EUR (4.50 USD)" in shown.text and "Shipping:" not in shown.text
    made = a_proposal(agent, ctx)
    assert made.ok, made.text
    [request] = rows(agent, "SELECT action, payload FROM approvals WHERE executor = 'printify_product'")
    assert json.loads(request["action"])["shipping"] == [[SMALL, 360]] and "(shipping 3.60 EUR)" in request["payload"]


def test_what_making_costs_is_converted_like_the_shipping(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = pod_context(agent)
    read_catalog(ctx)
    mode, now = agent.scope().mode, to_iso(agent.clock.now())
    with agent.db.transaction() as conn:  # Printify states this account's amounts in USD
        key = f"{POSTER}:{SENSARIA}"
        printify_publisher._store(conn, mode, "variants", key, [[SMALL, "12x18 in", 3600, 5400, 450, "USD"]], now)
        printify_publisher.keep_costs(conn, mode, POSTER, SENSARIA, {SMALL: 790}, now)
    catalog = printify_publisher.Catalog(agent.db, agent.clock, mode, agent.printify.account)
    ctx.catalog = lambda search, blueprint, provider: catalog.answer(search, blueprint, provider, "EUR", 1.25)
    ctx.usd_per_eur = 1.25
    least = printify.least_price(632, 360, printify.Terms("EUR", 1.25))  # USD 7.90 and 4.50 at 1.25 a euro
    shown = call(ctx, "printify_catalog", {"blueprint_id": POSTER, "provider_id": SENSARIA})
    assert f"making 6.32 EUR, least price {printify.money(least, 'EUR')}" in shown.text
    refused = a_proposal(agent, ctx, prices=f"{SMALL}: {(least - 10) / 100:.2f}")
    assert not refused.ok and f"at least {printify.money(least, 'EUR')}" in refused.text
    assert a_proposal(agent, ctx, prices=f"{SMALL}: {least / 100:.2f}").ok
    [request] = rows(agent, "SELECT id, action FROM approvals WHERE executor = 'printify_product'")
    assert json.loads(request["action"])["billed_in"] == "USD"
    # Carried out, the costs Printify reads back are converted too: it keeps the margin and is published.
    assert owner(agent).decide(request["id"], {"decision": "approve"}, "Owner").status == 200
    agent.settings = agent.pod.settings = agent.settings.model_copy(update={"etsy_usd_per_eur": 1.25})
    assert agent.execute_approved() == [(request["id"], "active")]
    assert json.loads(made_rows(agent)[0]["prices"])[0][2] == 632


def test_a_product_whose_currency_changed_after_approval_is_not_made(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    agent.settings = agent.pod.settings = agent.settings.model_copy(update={"printify_currency": "USD"})
    assert agent.execute_approved() == [(request, "failed")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert "its prices are in EUR, but printify_currency is USD now" in note
    account = agent.printify.account()
    assert account.state["products"] == {} and account.state["images"] == {}  # nothing reached Printify


# --- reconciling what the sync finds --------------------------------------------------------------------------------


def test_an_unclear_publish_is_taken_up_when_the_listing_is_live(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    account = agent.printify.account()
    publish = account.publish

    def lost(shop: int, product_id: str) -> None:
        publish(shop, product_id)
        raise Unclear("the connection to Printify broke (ReadTimeout)")

    account.publish = lost
    assert agent.execute_approved() == [(request, "unclear")]
    [product_id] = list(account.state["products"])
    assert made_rows(agent)[0]["product_id"] == product_id
    assert agent.pod.sync(force=True) is None
    [made] = made_rows(agent)
    assert made["status"] == "active" and made["listing_id"] == account.state["products"][product_id]["listing_id"]
    entries = rows(agent, f"SELECT status, subject, undo FROM action_journal WHERE approval_id = {request} ORDER BY id")
    assert [e["status"] for e in entries] == ["unclear", "simulated"]
    assert json.loads(entries[1]["undo"]) == {"action": "delete_product", "product_id": product_id}
    with agent.db.connection() as conn:
        assert printify_publisher.listing_ids(conn, agent.scope()) == [made["listing_id"]]
    assert any("live in the shop" in e for e in events(agent))
    # The next product isn't a first publication any more.
    ctx = pod_context(agent)
    assert a_proposal(agent, ctx, title="Minimalist lake poster", image="shop/lake.png").ok
    [second] = rows(agent, f"SELECT * FROM approvals WHERE executor = 'printify_product' AND id > {request}")
    with agent.db.connection() as conn:
        assert never.reasons(conn, second) == []


def test_a_publish_that_never_reaches_etsy_fails_with_its_reason(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    account = agent.printify.account()
    account.publish = lambda shop, product_id: None  # accepted, but no Etsy listing comes
    assert agent.execute_approved() == [(request, "publishing")]
    agent.clock.advance(hours=printify_publisher.PUBLISH_HOURS - 1)
    agent.pod.sync(force=True)
    assert made_rows(agent)[0]["status"] == "publishing"
    agent.clock.advance(hours=1)
    agent.pod.sync(force=True)
    [made] = made_rows(agent)
    assert (made["status"], made["error"]) == ("failed", printify_publisher.STALE)
    [product_id] = list(account.state["products"])
    assert any(f"Printify didn't publish it to Etsy within 24 hours: product {product_id}" in e for e in events(agent))
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "printify.create_product")
    assert entry["undo"]["why_not"] is None  # still at Printify: Undo deletes it
    ctx = pod_context(agent)
    assert a_proposal(agent, ctx, title="Minimalist lake poster", image="shop/lake.png").ok
    [second] = rows(agent, f"SELECT * FROM approvals WHERE executor = 'printify_product' AND id > {request}")
    with agent.db.connection() as conn:
        assert never.reasons(conn, second) == ["first_publication"]  # it never reached the shop
    # Published later after all (the owner fixed it at Printify): the sync takes the listing.
    account.state["products"][product_id].update(listing_id=800_000_123, visible=True)
    agent.pod.sync(force=True)
    assert (made_rows(agent)[0]["status"], made_rows(agent)[0]["listing_id"]) == ("active", 800_000_123)


def test_an_unclear_product_never_published_offers_the_undo_that_deletes_it(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    account = agent.printify.account()

    def lost(shop: int, product_id: str) -> None:
        raise Unclear("the connection to Printify broke (ReadTimeout)")  # nothing was published

    account.publish = lost
    assert agent.execute_approved() == [(request, "unclear")]
    [product_id] = list(account.state["products"])
    agent.clock.advance(hours=printify_publisher.PUBLISH_HOURS)
    agent.pod.sync(force=True)
    [made] = made_rows(agent)
    assert (made["status"], made["error"]) == ("failed", printify_publisher.STALE)
    assert any("or Undo deletes it" in e for e in events(agent))
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "printify.create_product")
    assert entry["undo"] == {"label": "Delete the product", "why_not": None, "request": None}
    reply = owner(agent).undo(entry["id"], "Stefan")
    assert reply.status == 200, reply.body
    assert agent.execute_approved() == [(int(reply.body["approval_id"]), "done")]
    assert account.state["products"] == {} and made_rows(agent)[0]["status"] == "deleted"


def test_one_product_read_failing_keeps_the_orders_and_old_stale_rows_rest(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    account = agent.printify.account()
    account.publish = lambda shop, product_id: None  # accepted, but no Etsy listing comes
    assert agent.execute_approved() == [(request, "publishing")]
    [product_id] = list(account.state["products"])
    account.sell(product_id)
    read = account.product

    def busy(shop: int, wanted: str) -> Any:
        raise printify.PrintifyError("Printify answered 429")

    account.product = busy
    error = agent.pod.sync(force=True)
    assert error is not None and "429" in error
    assert len(rows(agent, "SELECT * FROM printify_orders")) == 1  # 0.13.0: the whole sync stopped
    account.product = read
    agent.clock.advance(hours=printify_publisher.PUBLISH_HOURS)
    assert agent.pod.sync(force=True) is None
    assert made_rows(agent)[0]["error"] == printify_publisher.STALE
    # A STALE product is read again for STALE_DAYS, then no more.
    reads = []
    account.product = lambda shop, wanted: reads.append(wanted) or read(shop, wanted)
    agent.pod.sync(force=True)
    assert reads == [product_id]
    agent.clock.advance(days=printify_publisher.STALE_DAYS)
    agent.pod.sync(force=True)
    assert reads == [product_id]


def test_an_unclear_product_gone_at_printify_is_deleted_and_a_crash_keeps_its_number(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    account = agent.printify.account()

    def crash(shop: int, product_id: str) -> None:
        raise RuntimeError("the app stopped")

    account.publish = crash
    assert agent.execute_approved() == [(request, "unclear")]
    [product_id] = list(account.state["products"])
    assert made_rows(agent)[0]["product_id"] == product_id  # kept as soon as Printify made it
    account.delete(4242, product_id)
    agent.pod.sync(force=True)
    assert made_rows(agent)[0]["status"] == "deleted"


# --- Printify's listings and product lines without a venture count ------------------------------------------------


def pod_line(agent: Any, venture_id: int | None, listing_id: int = 800_000_123) -> int:
    """A product line with only a Printify listing, live at Etsy with its numbers, and an Etsy order of it."""
    scope, now = agent.scope(), to_iso(agent.clock.now())
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    with agent.db.transaction() as conn:
        project = create_project(
            conn,
            scope,
            cycle_id=cycle,
            title="Posters",
            hypothesis="Posters sell",
            next_step="",
            status="active",
            now=now,
            venture_id=venture_id,
        )
        request = insert_approval(
            conn,
            scope,
            cycle,
            now,
            project_id=project,
            payload="A poster",
            action=canonical({"title": "Poster"}),
            type="sell",
            title="Printify product: Poster",
            description="A poster",
            expected_cost="-",
            expected_benefit="-",
            executor="printify_product",
        )
        conn.execute(
            "INSERT INTO printify_products (mode, session, approval_id, product_id, listing_id, title, currency,"
            " status, started_at, finished_at, state, views, favorites, synced_at) VALUES (?, ?, ?, 'abc123', ?,"
            " 'Poster', 'EUR', 'active', ?, ?, 'active', 40, 3, ?)",
            (scope.mode, scope.session, request, listing_id, now, now, now),
        )
        conn.execute(
            "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, currency, total, total_cents, items,"
            " status, synced_at) VALUES (?, ?, 555, ?, 'EUR', '24.90 EUR', 2490, ?, 'paid', ?)",
            (scope.mode, scope.session, now, json.dumps([{"listing_id": listing_id, "quantity": 1}]), now),
        )
    return project


def test_printify_listings_count_in_the_metrics_the_listing_test_and_the_venture_rules(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    pod = venture_titled(agent, "Print on demand in the Etsy shop")
    project = pod_line(agent, pod)
    agent.clock.advance(minutes=5)
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        assert [r["listing_id"] for r in metrics.listings(conn, scope, project, None)] == [800_000_123]
        assert [r["listing_id"] for r in metrics.listings(conn, scope, None, pod)] == [800_000_123]
        books = metrics.Books(ledger=None, clock=agent.clock, synced_at=now)
        start = to_iso(agent.clock.now() - timedelta(hours=1))
        for name, value in (("views_total", 40), ("favorites_total", 3), ("orders_total", 1), ("listings_live", 1)):
            row = {"metric": name, "target": 1, "baseline": 0, "project_id": project, "venture_id": None}
            reading = metrics.read(conn, scope, {**row, "created_at": start}, books, now)
            assert isinstance(reading, metrics.Reading) and reading.value == value, (name, reading)
        qa_row = {"metric": "qa_clean", "target": 1, "baseline": 0, "project_id": project, "venture_id": None}
        assert metrics.read(conn, scope, {**qa_row, "created_at": start}, books, now).value == 1  # Printify's photos
        assert project in gates._live_projects(conn, scope)
        began = gates.keep(conn, scope, agent.clock.today(), now)
        assert any(f"began the listing test of project #{project}" in line for line in began)
        assert stages.sold(conn, scope, pod, ventures.Money())
        evidence = predictions._sold(conn, scope, books, pod, start, agent.clock.today().isoformat())
        assert "an Etsy order of its listings" in evidence
        # Live at Etsy as the Etsy sync read it: an expired listing isn't counted live.
        assert printify_publisher.totals(conn, scope)[0] == 1
        conn.execute("UPDATE printify_products SET state = 'expired'")
        assert printify_publisher.totals(conn, scope)[0] == 0


def test_a_product_line_without_a_venture_joins_the_etsy_leg_and_its_sale_saves_it(data_dir: Path) -> None:
    agent, _ = listed(data_dir)  # its Etsy listing's project had no venture
    leg = venture_titled(agent, "Etsy digital products")
    request = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")[0]["id"]
    project = project_of(agent, request)
    assert rows(agent, f"SELECT venture_id FROM projects WHERE id = {project}")[0]["venture_id"] == leg
    [result] = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'propose_etsy_listing' AND status = 'ok'")
    assert f"Project #{project} is part of venture #{leg} now: its sales count there." in result["result"]
    # The ventures review's case: the leg is live, the owner records a sale of that product line on day 10, and the
    # leg isn't parked 60 days after it went live.
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE ventures SET stage = 'live', updated_at = ? WHERE id = ?", (to_iso(agent.clock.now()), leg)
        )
    agent.clock.advance(days=10)
    owner_entry(agent.economy, "revenue", "6.90", project_id=project, test_money=True)
    agent.clock.advance(days=stages.LIVE_DAYS)
    with agent.db.transaction() as conn:
        happened = stages.keep(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()))
    assert not any(f"parked venture #{leg}" in line for line in happened), happened
    assert rows(agent, f"SELECT stage FROM ventures WHERE id = {leg}")[0]["stage"] == "live"


def test_a_printify_product_s_line_joins_the_print_on_demand_venture_never_a_parked_one(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    pod = venture_titled(agent, "Print on demand in the Etsy shop")
    scope, now = agent.scope(), to_iso(agent.clock.now())
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    with agent.db.transaction() as conn:
        made = [
            create_project(
                conn, scope, cycle_id=cycle, title=f"Line {i}", hypothesis="x", next_step="", status="active", now=now
            )
            for i in range(3)
        ]
        assert ventures.adopt(conn, scope, made[0], cycle, "printify", now) == pod
        assert ventures.adopt(conn, scope, made[0], cycle, "etsy", now) is None  # it has one
        conn.execute("UPDATE ventures SET stage = 'parked', parked_by = 'owner' WHERE id = ?", (pod,))
        assert ventures.adopt(conn, scope, made[1], cycle, "printify", now) is None
        # A venture cycle's product line joins that venture.
        dropshipping = venture_titled(agent, "Dropshipping store")
        venturing = conn.execute(
            "INSERT INTO cycles (life_id, boot_id, started_at, status, trigger, simulated, cap_micros, session,"
            " venture_id) SELECT life_id, boot_id, ?, 'completed', 'schedule', simulated, cap_micros, session, ?"
            " FROM cycles WHERE id = ?",
            (now, dropshipping, cycle),
        ).lastrowid
        assert ventures.adopt(conn, scope, made[2], venturing, "etsy", now) == dropshipping


# --- a free cost probe ----------------------------------------------------------------------------------------------


def probing(agent: Any) -> tools.ToolContext:
    ctx = pod_context(agent)
    catalog = printify_publisher.Catalog(
        agent.db, agent.clock, agent.scope().mode, agent.printify.account, agent.printify.shop_id
    )
    ctx.catalog = lambda search, blueprint, provider: catalog.answer(search, blueprint, provider, "EUR")
    return ctx


def test_the_catalog_shows_what_making_costs_from_a_probe_never_published(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = probing(agent)
    account = agent.printify.account()

    def never_publish(shop: int, product_id: str) -> None:
        raise AssertionError("a cost probe is never published")

    account.publish = never_publish
    made: list[str] = []
    create = account.create

    def watched(shop: int, product: Any, image_id: str) -> Any:
        found = create(shop, product, image_id)
        made.append(found.product_id)
        return found

    account.create = watched
    assert call(ctx, "printify_catalog", {"search": "poster"}).ok
    assert call(ctx, "printify_catalog", {"blueprint_id": POSTER}).ok
    shown = call(ctx, "printify_catalog", {"blueprint_id": POSTER, "provider_id": SENSARIA})
    least = printify.money(printify.least_price(790, 450), "EUR")
    assert (
        f"#{SMALL}: 12x18 in: print area 3600 x 5400 pixels, shipping to Germany 4.50 EUR, making 7.90 EUR, least "
        f"price {least}" in shown.text
    )
    assert "making isn't known" not in shown.text
    assert len(made) == 1 and account.state["products"] == {}  # made, read and deleted at once
    again = call(ctx, "printify_catalog", {"blueprint_id": POSTER, "provider_id": SENSARIA})
    assert again.ok and len(made) == 1  # the costs are kept
    # A price below the least is refused before it costs the owner an approval; one above it is proposed.
    refused = a_proposal(agent, ctx, prices=f"{SMALL}: 12.90")
    assert not refused.ok and f"variant {SMALL} at 12.90 EUR: at least {least}" in refused.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM approvals WHERE executor = 'printify_product'")[0]["n"] == 0
    assert a_proposal(agent, ctx).ok


def test_a_probe_left_at_printify_is_deleted_by_the_next_sync(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = probing(agent)
    account = agent.printify.account()
    delete = account.delete

    def refused(shop: int, product_id: str) -> None:
        raise NotSent("HTTP 429")

    account.delete = refused
    read_catalog(ctx)
    [left] = list(account.state["products"])
    key = printify_publisher.meta_key(agent.scope().mode, "probe")
    assert json.loads(agent.db.get_meta(key)) == {"product_id": left}
    account.delete = delete
    assert agent.pod.sync(force=True) is None
    assert account.state["products"] == {} and not agent.db.get_meta(key)


def test_the_tools_say_the_catalog_has_the_costs() -> None:
    catalog = next(d for d in tools.definitions(etsy=True, printify=True) if d["name"] == "printify_catalog")
    assert "what making costs and the least price" in catalog["description"]
    guide = tools.guide_text("printify")
    assert "least price" in guide and "keep 15% of" in guide and "{" not in guide


# --- a product that missed its margin, and whose delete failed ----------------------------------------------------


def test_a_failed_delete_is_said_and_the_product_s_number_kept(data_dir: Path) -> None:
    agent, request = approved(data_dir, prices=f"{SMALL}: 12.90")
    account = agent.printify.account()

    def refused(shop: int, product_id: str) -> None:
        raise NotSent("HTTP 429")

    account.delete = refused
    assert agent.execute_approved() == [(request, "failed")]
    [product_id] = list(account.state["products"])
    [made] = made_rows(agent)
    assert (made["status"], made["product_id"]) == ("failed", product_id)
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert "was deleted" not in note
    assert (
        f"Ember's code couldn't delete the unpublished product {product_id} at Printify (HTTP 429): delete it" in note
    )
    # What the product costs to make is kept in the catalog for the next proposal (no probe was made).
    with agent.db.connection() as conn:
        assert printify_publisher.costs_of(conn, agent.scope().mode, POSTER, SENSARIA) == {SMALL: 790}
    # Gone at Printify already is what the delete wanted.
    agent2_note = printify_publisher.Publisher._discard(_Gone(), 1, "x")
    assert agent2_note == "The unpublished product was deleted at Printify."


class _Gone:
    def delete(self, shop: int, product_id: str) -> None:
        raise Gone("no such product")


# --- the migration --------------------------------------------------------------------------------------------------


def test_the_migration_keeps_the_catalog_adds_order_tax_and_links_product_lines(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 58], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    then = "2026-09-20T10:00:00Z"
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', ?, 'born', 'alive')",
            (then,),
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', ?, 'completed', 'schedule', 0, 1)",
            (then,),
        )
        venture = (
            "INSERT INTO ventures (id, mode, session, life_id, created_by, created_at, updated_at, title, pitch, stage,"
            " channel, parent_id) VALUES (?, 'live', 0, 1, ?, ?, ?, ?, 'x', ?, ?, NULL)"
        )
        conn.execute(venture, (1, "agent", then, then, "Etsy digital products", "live", None))
        conn.execute(venture, (4, "owner", then, then, "Print on demand in the Etsy shop", "building", "printify"))
        project = (
            "INSERT INTO projects (id, mode, session, life_id, created_cycle_id, created_at, updated_at, title,"
            " hypothesis, status, venture_id) VALUES (?, 'live', 0, 1, 1, ?, ?, ?, 'x', ?, ?)"
        )
        conn.execute(project, (7, then, then, "Nebenkostenabrechnung", "active", None))  # an Etsy line, no venture
        conn.execute(project, (8, then, then, "Posters", "active", None))  # a Printify line, no venture
        conn.execute(project, (9, then, then, "Old planner", "abandoned", None))  # closed: final
        conn.execute(project, (10, then, then, "Linked", "active", 4))
        conn.execute(project, (11, then, then, "Nothing listed", "active", None))
        request = (
            "INSERT INTO approvals (id, mode, session, life_id, cycle_id, project_id, created_at, type, title,"
            " description, payload, payload_sha256, expected_cost, expected_benefit, executor, action, status)"
            " VALUES (?, 'live', 0, 1, 1, ?, ?, 'sell', 't', 'd', ?, ?, 'c', 'b', ?, '{}', 'done')"
        )
        for number, project_id, executor in (
            (1, 7, "etsy_listing"),
            (2, 8, "printify_product"),
            (3, 9, "etsy_listing"),
            (4, 10, "etsy_listing"),
        ):
            conn.execute(request, (number, project_id, then, f"p{number}", f"{number:064x}", executor))
        catalog = "INSERT INTO printify_catalog (mode, kind, key, data, fetched_at) VALUES ('live', ?, ?, ?, ?)"
        conn.execute(catalog, ("blueprints", "", json.dumps([[282, "Poster", "X"]]), then))
        conn.execute(catalog, ("variants", "282:2", json.dumps([[43135, "12x18 in", 3600, 5400, 450]]), then))
        conn.execute(
            "INSERT INTO printify_orders (mode, session, order_id, product_id, quantity, cost_cents, shipping_cents,"
            " currency, status, created_at, synced_at) VALUES ('live', 0, 'o1', 'p1', 1, 790, 450, 'EUR', 'fulfilled',"
            " ?, ?)",
            (then, then),
        )
    old.close()
    applied = migrate(db_file, backup_dir=tmp_path / "backups")
    assert applied == [m.version for m in discover_migrations() if m.version > 58]
    new = Database(db_file)
    with new.transaction() as conn:
        linked = dict(conn.execute("SELECT id, venture_id FROM projects ORDER BY id").fetchall())
        assert linked == {7: 1, 8: 4, 9: None, 10: 4, 11: None}
        kinds = [tuple(r) for r in conn.execute("SELECT kind, key FROM printify_catalog ORDER BY kind")]
        assert kinds == [("blueprints", "")]  # the variants are read again, with Printify's currency
        conn.execute(
            "INSERT INTO printify_catalog (mode, kind, key, data, fetched_at) VALUES ('live', 'costs', '282:2', ?, ?)",
            (json.dumps({"probed": "", "costs": [[43135, 790]]}), then),
        )
        assert conn.execute("SELECT tax_cents FROM printify_orders").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE printify_orders SET tax_cents = -1")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- review round 2 --------------------------------------------------------------------------------------------------


def test_a_printify_only_line_that_misses_day_7_is_owed_what_it_can_do(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    pod = venture_titled(agent, "Print on demand in the Etsy shop")
    project = pod_line(agent, pod)
    shop_line = project_of(agent, rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")[0]["id"])
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        gates.keep(conn, scope, agent.clock.today(), now)
        [bar] = gates.started(conn, scope)[project]
        said = gates._owe(conn, scope, bar, gates.BY_KEY["day7_views"], "Posters", now)
        assert gates._printify_only(conn, scope, project) and not gates._printify_only(conn, scope, shop_line)
    # propose_etsy_edit refuses the listings Printify made: the obligation names what the agent can do.
    [what] = [
        r["what"] for r in rows(agent, f"SELECT what FROM obligations WHERE milestone_id = {bar['milestone_id']}")
    ]
    assert "message_owner" in what and "propose_etsy_edit" not in what
    assert said.startswith("Obligation: ask your owner once")


def test_a_closed_line_s_printify_listing_still_counts_for_the_print_on_demand_venture(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    pod = venture_titled(agent, "Print on demand in the Etsy shop")
    project = pod_line(agent, None)
    with agent.db.transaction() as conn:  # closed: the upgrade can't link it (a closed project is final)
        conn.execute(f"UPDATE projects SET status = 'succeeded' WHERE id = {project}")
    with agent.db.connection() as conn:
        assert [r["listing_id"] for r in metrics.listings(conn, agent.scope(), None, pod)] == [800_000_123]
        assert stages.sold(conn, agent.scope(), pod, ventures.Money())


# 0.15.0 (ventures): the agent can no longer create a venture live (venture_create takes idea or researching only), so
# "Ember earns somewhere" (tools._earning, which an active Printify listing would have met) is gone.


def test_the_approval_card_names_etsy_s_fees_and_offsite_ads(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    [row] = rows(agent, f"SELECT * FROM approvals WHERE id = {request}")
    assert "Etsy's fees counted: the 0.20 USD listing fee, 6.5%, 4% + 0.30 for payments" in row["payload"]
    assert "Not counted: Offsite Ads" in row["payload"]
    with agent.db.connection() as conn:
        assert never.reasons(conn, row) == ["first_publication"]


def test_a_stale_product_whose_prices_were_never_checked_isn_t_said_to_be_publishable(data_dir: Path) -> None:
    agent, request = approved(data_dir)

    def crash(product: Any, costs: Any) -> Any:
        raise RuntimeError("the app stopped")

    agent.pod._margins = crash  # made at Printify, then nothing checked its prices
    assert agent.execute_approved() == [(request, "unclear")]
    agent.clock.advance(hours=printify_publisher.PUBLISH_HOURS)
    agent.pod.sync(force=True)
    [made] = made_rows(agent)
    assert (made["status"], made["error"]) == ("failed", printify_publisher.STALE)
    [why] = [e for e in events(agent) if printify_publisher.STALE in e]
    assert "never checked against the margin: Undo deletes it" in why and "and publish it" not in why


def test_a_stale_product_gone_at_printify_offers_no_undo(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    account = agent.printify.account()
    account.publish = lambda shop, product_id: None  # accepted, but no Etsy listing comes
    assert agent.execute_approved() == [(request, "publishing")]
    agent.clock.advance(hours=printify_publisher.PUBLISH_HOURS)
    agent.pod.sync(force=True)
    [product_id] = list(account.state["products"])
    account.delete(4242, product_id)  # the owner deleted it at Printify
    agent.pod.sync(force=True)
    [made] = made_rows(agent)
    assert (made["status"], made["error"]) == ("deleted", printify_publisher.STALE)
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "printify.create_product")
    assert entry["undo"]["why_not"] == "the product isn't at Printify anymore"


def test_printify_s_bill_is_recorded_with_the_revenue_option(data_dir: Path) -> None:
    agent, request = approved(data_dir)
    agent.execute_approved()
    account = agent.printify.account()
    [product_id] = list(account.state["products"])
    account.sell(product_id)
    account.state["orders"][0]["tax_cents"] = 236
    assert agent.pod.sync(force=True) is None
    scope = agent.scope()
    off = agent.settings.model_copy(update={"etsy_auto_record_revenue": False})
    assert printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, off) == []
    on = agent.settings.model_copy(update={"etsy_auto_record_revenue": True, "etsy_usd_per_eur": 1.10})
    agent._etsy_numbers(scope, on)  # after each Etsy sync, with the orders' revenue
    project = project_of(agent, request)
    venture = rows(agent, f"SELECT venture_id FROM projects WHERE id = {project}")[0]["venture_id"]
    [entry] = rows(agent, "SELECT * FROM ledger WHERE source LIKE 'Printify order %'")
    assert (entry["type"], entry["project_id"], entry["venture_id"]) == ("expense", project, venture)
    assert (entry["orig_amount"], entry["orig_currency"], entry["simulated"]) == ("14.76", "EUR", 1)
    assert entry["amount_micros"] == 16_240_000  # 14.76 EUR at 1.10
    [order] = agent.integrations()["printify"]["orders"]
    assert order["recorded"] and order["key"] == entry["idempotency_key"]
    assert printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on) == []  # once
