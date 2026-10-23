"""0.47.0 (vision/generations.md, "The estate"): what the second generation sees of the products her predecessor
handed on. A product passes, its line never does: a passive one stays live and synced but isn't hers to work on (her
plan names it once, her tools leave it as it is, its sale goes to the owner to give her); an adopted one is her line
with the owner's first test, and a missed test holds the line until the owner keeps or drops it, which she can't lift
herself: no rescue spending on a product handed on without a buyer's signal."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.agent import agenda, estate, plan_changes, plan_keep, store, tools_lines  # noqa: E402
from app.agent.plan_changes import PlanError  # noqa: E402
from app.agent.plan_model import nodes  # noqa: E402
from app.agent.tools_context import ToolError  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import etsy, etsy_publisher  # noqa: E402
from tests.agent_helpers import rows  # noqa: E402
from tests.etsy_helpers import call, shop_context  # noqa: E402
from tests.generation_helpers import handed_on  # noqa: E402


def test_her_plan_names_a_passive_listing_once_and_leaves_it_out_of_her_shop(data_dir: Path) -> None:
    agent, listing = handed_on(data_dir, "passive")
    scope = agent.scope()
    with agent.db.connection() as conn:
        aside = frozenset(estate.not_hers_ids(conn, scope))
        shop = etsy_publisher.shop_text(conn, scope, agent.clock, "EmberTestShop", 5, aside=aside)
        line = estate.shop_line(conn, scope)
        context = store.plan_context(conn, rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"])
    assert aside == {listing} and f"#{listing}" not in shop
    assert line.startswith(f"Handed on by your predecessor and not yours to work on: 1 listing (#{listing})")
    assert context is not None and line[:80] in context


def test_her_tools_leave_a_passive_listing_as_it_is(data_dir: Path) -> None:
    agent, listing = handed_on(data_dir, "passive")
    ctx = shop_context(agent)
    for args in ({"price": "3.90"}, {"state": "deactivate"}):
        made = call(ctx, "propose_etsy_edit", {"listing_id": listing, "reason": "Fix the listing.", **args})
        assert not made.ok and "came from your predecessor and is passive" in made.text
    with agent.db.transaction() as conn:
        line = store.create_project(
            conn,
            ctx.scope,
            cycle_id=ctx.cycle_id,
            title="Résumés",
            hypothesis="Buyers search for them.",
            next_step="",
            status="active",
            now=ctx.now(),
        )
    moved = call(ctx, "project_update", {"project_id": line, "listing_id": listing, "note": "It sells best here."})
    assert not moved.ok and "came from your predecessor" in moved.text  # adopting it is the owner's
    with agent.db.connection() as conn:  # the guard of pins, Bluesky posts, blog posts and Printify changes
        for what in ("pin", "post", "blog post recommending it", "change"):
            with pytest.raises(ToolError, match="came from your predecessor and is passive"):
                tools_lines._listing_stopped(conn, ctx.scope, listing, what)
    listed = call(ctx, "etsy_listing", {})
    assert listed.ok and f"#{listing}" not in listed.text.split("Not live")[0]


def test_a_sale_of_a_passive_listing_is_the_owners_to_give_her(data_dir: Path) -> None:
    agent, listing = handed_on(data_dir, "passive")
    shop = agent.etsy.shop()
    line = {"listing_id": listing, "title": "Ember's planner", "quantity": 1, "price_cents": 450}
    ordered = to_iso(agent.clock.now() - timedelta(minutes=5))
    shop.orders = lambda since: [etsy.Order(81, ordered, 450, "EUR", [line], items_cents=450, status="paid")]  # type: ignore[method-assign]
    shop.payment_fees = lambda receipt_id: 48  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None  # the world's sync still reads its orders
    since = to_iso(agent.clock.now() - timedelta(hours=1))
    with agent.db.transaction() as conn:
        [noted] = agenda._orders(conn, agent.scope(), since, to_iso(agent.clock.now()))
        sold = estate.shop_line(conn, agent.scope())
    assert "came from your predecessor and isn't yours to work on: tell your owner it sold" in noted
    assert "1 sold since the hand-over" in sold


def test_an_adopted_products_missed_test_holds_her_line_until_the_owner_decides(data_dir: Path) -> None:
    agent, listing = handed_on(data_dir, "adopt")  # its first test: a favorite in 4 weeks
    scope = agent.scope()
    with agent.db.connection() as conn:
        [received] = estate.received(conn, scope)
    line = int(received["project_id"])
    agent.clock.advance(days=29)
    now, today = to_iso(agent.clock.now()), agent.clock.today()
    with agent.db.transaction() as conn:
        said = plan_keep._estate_tests(conn, scope, now, today)
        [received] = estate.received(conn, scope)
        [product] = nodes(conn, scope, "level = 'product' AND project_id = ?", (line,))
        [asked] = nodes(
            conn, scope, "level = 'step' AND status = 'open' AND template = 'decide/owner' AND project_id = ?", (line,)
        )
    assert received["test_result"] == "missed" and any("missed its first test" in s for s in said)
    assert product["hold_by"] == "agent" and product["hold_reason"].startswith(plan_keep.ESTATE_HOLD)
    assert "0 of 1 favorites" in product["hold_reason"]
    with agent.db.transaction() as conn:
        with pytest.raises(PlanError, match="your owner keeps it or drops it"):
            plan_changes.resume(conn, scope, line, "One more photo will do it.", None, now)
        plan_changes.owner_keep(conn, scope, int(asked["id"]), "Give it the season.", "Owner", now)
        [product] = nodes(conn, scope, "level = 'product' AND project_id = ?", (line,))
    assert product["hold_reason"] is None  # the owner's keep lifts it
    with agent.db.transaction() as conn:
        assert plan_keep._estate_tests(conn, scope, now, today) == []  # decided once


def test_an_adopted_products_test_is_met_by_its_signal_since_the_hand_over(data_dir: Path) -> None:
    agent, listing = handed_on(data_dir, "adopt", {"metric": "views", "target": 5, "due": None})
    scope = agent.scope()
    with agent.db.connection() as conn:
        [received] = estate.received(conn, scope)
    before = json.loads(received["numbers"])["views"]
    now, today = to_iso(agent.clock.now()), agent.clock.today()
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = ? WHERE listing_id = ?", (before + 4, listing))
        assert plan_keep._estate_tests(conn, scope, now, today) == []  # 4 of 5: not yet, not due yet
        conn.execute("UPDATE etsy_listings SET views = ? WHERE listing_id = ?", (before + 6, listing))
        [said] = plan_keep._estate_tests(conn, scope, now, today)
        [received] = estate.received(conn, scope)
    assert received["test_result"] == "met" and "6 of 5 views" in said


def a_book(title: str) -> dict[str, object]:
    """A KDP package as propose_kdp_book makes it (its files as their uploads)."""
    upload = {"path": "books/interior.pdf", "sha256": "0" * 64, "bytes": 2_000}
    return {
        "format": "paperback",
        "title": title,
        "subtitle": "",
        "author": "",
        "description": "A household book.",
        "keywords": ["haushaltsbuch"],
        "categories": ["Budgeting"],
        "language": "German",
        "price": "7.99",
        "manuscript": upload,
        "cover": {**upload, "path": "books/cover.pdf"},
        "trim": "6x9",
        "pages": 83,
    }


def test_a_book_handed_on_shows_in_her_kdp_section_as_her_line(data_dir: Path) -> None:
    from tests.agent_helpers import owner
    from tests.generation_helpers import decide_all, settled
    from tests.generations.test_world import request

    agent, _ = settled(data_dir)
    book = request(agent, agent.scope(), "kdp_package", a_book("Haushaltsbuch 2027"))
    owner(agent).decide(book["id"], {"decision": "approve"}, "Owner")
    link = "https://www.amazon.de/dp/B0TEST"
    owner(agent).close(book["id"], {"outcome": "done", "result_note": "Published.", "result_link": link}, "Owner")
    with agent.db.transaction() as conn:
        rows_ = estate.refresh(conn, agent.scope(), agent.clock)
    kinds = {r["kind"]: r for r in rows_}
    assert kinds["kdp_book"]["suggested"] == "adopt" and kinds["kdp_book"]["link"] == link
    decide_all(agent, "passive")
    with agent.db.transaction() as conn:
        estate.decide(conn, agent.scope(), agent.clock, "kdp_book", str(book["id"]), "adopt", "Owner")
    agent.begin_generation("Owner")
    agent.run_cycle("schedule")
    with agent.db.connection() as conn:
        lines = estate.book_lines(conn, agent.scope())
        [line] = [r for r in estate.received(conn, agent.scope()) if r["kind"] == "kdp_book"]
    assert line["project_id"] is not None
    assert f'#{book["id"]} "Haushaltsbuch 2027" at {link}: your line #{line["project_id"]}, first test 1 orders by' in (
        lines
    )
