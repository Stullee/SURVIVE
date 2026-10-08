"""0.12.0: a demand note before a new product line, and Etsy's market probe. Ember made generic templates without
checking that anyone searched for them. Now every listing belongs to a product line (a project), and a product line's
first listing needs a demand note from the last 14 days: the keywords buyers type, what shows they buy and its source
(a page from the research results or a document of the owner's library). With the owner's market probe on (off by
default: Etsy's API terms first), the note also reads Etsy's search of active listings for the keywords and keeps only
aggregates: how many match, and the price quartiles of the first ones."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent import demand, tools
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls
from app.agent.service import Agent
from app.economy.clock import to_iso
from app.integrations import etsy
from tests.test_agent import ROOMY, rows
from tests.test_etsy import live_shop
from tests.test_loop_shapes import JOURNAL, PLAN, run
from tests.test_ventures import found

LISTING = {
    "title": "Weekly Planner",
    "description": "A weekly planner.",
    "price": "4.50",
    "tags": "planner",
    "category_id": 1,
    "files": "shop/p.pdf",
    "photos": "shop/p.png",
    "reason": "A first test.",
}
PROJECT = {
    "title": "Planners",
    "hypothesis": "People buy printable planners.",
    "status": "active",
}


def made(agent: Agent) -> None:
    agent.roots()[0].write_bytes("shop/p.pdf", b"%PDF-1.7 planner")
    agent.roots()[0].write_bytes("shop/p.png", b"\x89PNG photo")


def results(agent: Agent, tool: str) -> list[dict[str, Any]]:
    return rows(agent, f"SELECT status, result FROM tool_calls WHERE tool = '{tool}' ORDER BY id")


def test_the_probe_keeps_only_aggregates(tmp_path: Path) -> None:
    def listing(cents: int, currency: str = "USD") -> dict[str, Any]:
        return {
            "title": "Someone's planner",
            "shop_id": 5,
            "price": {"amount": cents, "divisor": 100, "currency_code": currency},
        }

    page = [listing(c) for c in (300, 450, 499, 650, 900)] + [listing(1_200, "EUR"), {"price": "?"}]
    shop, server = live_shop(tmp_path, {("GET", "/v3/application/listings/active"): {"count": 48_210, "results": page}})
    got = shop.market("weekly planner")
    assert got == etsy.Market(48_210, 5, "USD", 4.5, 4.99, 6.5)  # the most common currency's prices only
    assert "Someone" not in repr(got)  # nothing of another seller's listing goes further
    [request] = server.requests
    assert dict(request.url.params) == {
        "keywords": "weekly planner",
        "limit": "100",
        "sort_on": "score",
        "sort_order": "desc",
    }
    assert "authorization" not in request.headers and request.headers["x-api-key"]  # the app's key only
    assert etsy.market(None, None) == etsy.Market(0, 0, "", 0.0, 0.0, 0.0)
    assert got.text() == (
        "48,210 active listings on Etsy match; 5 of the first by relevance, priced in USD: 4.50 (lower quartile), "
        "4.99 (median), 6.50 (upper quartile)"
    )


def test_a_new_product_line_needs_a_demand_note(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            Plan(PLAN),  # no focus project
            ToolCalls([("research", {"question": "Who buys printable planners?"})]),
            found("https://example.invalid/planners"),
            ToolCalls([("propose_etsy_listing", LISTING), ("project_create", PROJECT)]),  # no project yet, then one
            ToolCalls(
                [
                    ("propose_etsy_listing", {**LISTING, "project_id": 1}),
                    ("demand_note", {"project_id": 1, "keywords": "weekly planner"}),
                    (
                        "demand_note",
                        {
                            "project_id": 1,
                            "keywords": "weekly planner",
                            "demand": "Many sell.",
                            "source": "https://x.invalid/y",
                        },
                    ),
                    (
                        "demand_note",
                        {
                            "project_id": 1,
                            "keywords": "weekly planner",
                            "demand": "Top listings show 1,000+ sales at 3 to 5 EUR.",
                            "source": "https://example.invalid/planners",
                        },
                    ),
                ]
            ),
            ToolCalls([("propose_etsy_listing", {**LISTING, "project_id": 1})]),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, before=made)
    proposals = results(agent, "propose_etsy_listing")
    assert [p["status"] for p in proposals] == ["error", "error", "ok"]
    assert "name its project (project_id): each listing belongs to a product line" in proposals[0]["result"]
    assert (
        "project #1 has no listing yet, and a new product line needs a demand note from the last 14 days first"
        in proposals[1]["result"]
    )
    notes = results(agent, "demand_note")
    assert [n["status"] for n in notes] == ["error", "error", "ok"]
    assert "give demand and its source: your owner's Etsy market probe is off" in notes[0]["result"]
    assert "source must be a page from your research results" in notes[1]["result"]
    assert notes[2]["result"].startswith("Saved demand note #1 for project #1")
    assert rows(agent, "SELECT project_id FROM approvals WHERE executor = 'etsy_listing'") == [{"project_id": 1}]
    with agent.db.connection() as conn:
        now = agent.clock.now()
        assert demand.listed(conn, 1)  # its next listing needs no note
        assert demand.recent(conn, 1, to_iso(now)) is not None
        assert demand.recent(conn, 1, to_iso(now + timedelta(days=15))) is None  # older than 14 days
    refused = pytest.raises(sqlite3.IntegrityError, match="a note never changes")
    with refused, agent.db.transaction() as conn:
        conn.execute("UPDATE demand_notes SET keywords = 'x'")


def test_the_market_probe_reads_etsy_for_the_note(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            Plan(PLAN),
            ToolCalls([("project_create", PROJECT), ("demand_note", {"project_id": 1, "keywords": "weekly planner"})]),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, before=made, settings=ROOMY.model_copy(update={"etsy_market_probe": True}))
    [note] = results(agent, "demand_note")
    expected = etsy.FakeShop(agent.clock).market("weekly planner")
    assert note["status"] == "ok" and f"Etsy's market probe for 'weekly planner': {expected.text()}." in note["result"]
    [row] = rows(
        agent, "SELECT keywords, demand, source, listings, sampled, currency, low, median, high FROM demand_notes"
    )
    assert row == {
        "keywords": "weekly planner",
        "demand": None,
        "source": None,
        "listings": expected.listings,
        "sampled": expected.sampled,
        "currency": expected.currency,
        "low": expected.low,
        "median": expected.median,
        "high": expected.high,
    }


def test_demand_notes_come_with_the_shop_in_ordinary_cycles() -> None:
    assert "demand_note" in {d["name"] for d in tools.definitions(etsy=True)}
    assert "demand_note" not in {d["name"] for d in tools.definitions(etsy=False)}
    assert "demand_note" not in {d["name"] for d in tools.definitions(etsy=True, venture=True)}
