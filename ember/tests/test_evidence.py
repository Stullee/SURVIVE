"""0.12.0: evidence with source quality. A venture's case was prose: a number and a link in it counted the same whether
the page was a search result, a vendor selling the very tool it praised, or nothing the agent ever read. Now each claim
is saved as evidence (a metric, a low and a high value, a unit, a region and its page), and Ember's code grades the
page: independent, marketing (a vendor's or an affiliate's) or unchecked (not a page the research tool returned). A
claim and its grade never change; FOCUS shows a venture's evidence by grade, the Ventures tab lists it."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.agent import evidence, tools, views
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls, request_kind
from app.agent.service import Agent
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_ventures import DROPSHIPPING, JOURNAL, VENTURING, found, plan, tool_results

NOW = "2026-09-30T12:00:00Z"
PAGES = (
    "https://www.example.invalid/forum/prices?utm_source=news&id=7",  # an independent page, with tracking
    "https://www.shopify.com/blog/dropshipping-profit",  # a vendor's
    "https://example.invalid/best-tools?ref=ember123",  # an affiliate's link
)


def claim(url: str, **more: str | int) -> tuple[str, dict[str, Any]]:
    args = {"claim": "Stores sell phone cases for 15 to 25 EUR.", "metric": "price", "low": "15", "high": "25"}
    return ("evidence", {**args, "unit": "EUR", "region": "DE", "url": url, **more})


def save(agent: Agent, url: str, low: float = 20, high: float = 30) -> tuple[int, str]:
    with agent.db.transaction() as conn:
        return evidence.add(
            conn,
            agent.scope(),
            venture_id=DROPSHIPPING,
            cycle_id=None,
            claim="Margins run 20 to 30%.",
            metric="margin",
            low=low,
            high=high,
            unit="%",
            region="EU",
            url=url,
            now=NOW,
        )


def researched(agent: Agent, *urls: str) -> None:
    with agent.db.transaction() as conn:
        evidence.record_sources(conn, agent.scope(), None, None, list(urls), NOW)


def test_a_page_is_matched_without_its_tracking() -> None:
    key = evidence.url_key("https://www.Example.invalid/forum/prices/?utm_source=news&id=7&fbclid=x#top")
    assert key == "example.invalid/forum/prices?id=7" == evidence.url_key("http://example.invalid/forum/prices?id=7")
    assert evidence.url_key("https://example.invalid") == "example.invalid"


def test_a_claim_is_graded_by_where_its_page_came_from(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls([("research", {"question": "What do dropshipping stores charge for phone cases?"})]),
            found(*PAGES),
            ToolCalls(
                [
                    claim("https://example.invalid/forum/prices/?id=7"),  # the first page, without its tracking
                    claim(PAGES[1], metric="margin", low="20", high="30", unit="%", region="EU"),
                    claim(PAGES[2], low="1,200", high="1,200.5"),
                    claim("https://example.invalid/never-searched", low="40", high=""),  # a single value
                ]
            ),
            ToolCalls(
                [
                    claim(PAGES[0], low="4,5"),
                    claim(PAGES[0], low="30", high="20"),
                    claim("ftp://example.invalid/prices"),
                    claim(PAGES[0], venture_id=99),
                ]
            ),
            ToolCalls([("guide", {"topic": "documents"})]),  # a venture cycle's guide has the ventures manual only
            Reply("Done."),
            JOURNAL,
            plan(venture=DROPSHIPPING),  # the next cycle's FOCUS shows it
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, ends = run(data_dir, fake, cycles=2, settings=VENTURING)
    assert [e.status for e in ends] == ["completed", "completed"]
    saved = rows(agent, "SELECT venture_id, cycle_id, low, high, source FROM evidence ORDER BY id")
    assert [(r["source"], r["low"], r["high"]) for r in saved] == [
        ("independent", 15.0, 25.0),
        ("marketing", 20.0, 30.0),
        ("marketing", 1200.0, 1200.5),
        ("unchecked", 40.0, 40.0),
    ]
    assert {(r["venture_id"], r["cycle_id"]) for r in saved} == {(DROPSHIPPING, 1)}  # the focus venture's
    results = tool_results(agent, "evidence")
    assert [r["status"] for r in results] == ["ok"] * 4 + ["error"] * 4
    assert results[0]["result"] == (
        f"Saved evidence #1 for venture #{DROPSHIPPING}. Its source is independent: a page from your research results."
    )
    assert "it sells what it describes, so weigh it lightly" in results[1]["result"]
    assert "your word only until research finds it" in results[3]["result"]
    assert "low must be a plain number like 1200 or 4.5" in results[4]["result"]  # which comma is the decimal one?
    assert "high must be at least low" in results[5]["result"]
    assert "url must be a web address" in results[6]["result"]
    assert "there is no venture #99" in results[7]["result"]
    [guide] = tool_results(agent, "guide")
    assert guide["status"] == "error" and "topic must be one of ventures" in guide["result"]
    sources = rows(agent, "SELECT cycle_id, llm_call_id, url, url_key FROM research_sources ORDER BY id")
    assert [s["url"] for s in sources] == list(PAGES)
    assert {(s["cycle_id"], s["llm_call_id"] is not None) for s in sources} == {(1, True)}
    assert sources[0]["url_key"] == "example.invalid/forum/prices?id=7"
    work = [r for r in fake.sent if request_kind(r) == "work"]
    assert {"evidence", "research", "venture_update"} <= {t["name"] for t in work[0]["tools"]}
    brief = work[-1]["messages"][0]["content"][0]["text"]
    assert (
        "\nEvidence: 4 claims (1 independent, 2 marketing, 1 unchecked); the newest: #4 price: 40 EUR (DE) "
        "[unchecked]; #3 price: 1,200–1,200.5 EUR (DE) [marketing]; #2 margin: 20–30 % (EU) [marketing]\nPitch: "
    ) in brief


def test_a_claim_never_changes(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    researched(agent, "", PAGES[0])  # an empty address isn't kept
    assert save(agent, PAGES[0]) == (1, "independent")
    for statement in (
        "UPDATE evidence SET source = 'marketing'",
        "DELETE FROM evidence",
        "UPDATE research_sources SET url_key = 'x'",
        "DELETE FROM research_sources",
    ):
        refused = pytest.raises(sqlite3.IntegrityError, match="never change|cannot be deleted|cannot change")
        with refused, agent.db.transaction() as conn:
            conn.execute(statement)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        save(agent, PAGES[0], low=30, high=20)
    assert rows(agent, "SELECT COUNT(*) AS n FROM research_sources") == [{"n": 1}]


def test_the_ventures_tab_lists_a_ventures_evidence(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0, settings=VENTURING)
    before = views.ventures_view(agent)
    researched(agent, PAGES[1])
    save(agent, PAGES[1])
    save(agent, PAGES[0])  # not a page research found
    after = views.ventures_view(agent)
    assert after["stamp"] != before["stamp"]  # a claim saved during a cycle shows at once
    item = next(v for v in after["items"] if v["id"] == DROPSHIPPING)
    assert item["evidence"]["counts"] == {"independent": 0, "marketing": 1, "unchecked": 1}
    assert [(e["id"], e["value"], e["source"], e["url"]) for e in item["evidence"]["items"]] == [
        (2, "margin: 20–30 % (EU)", "unchecked", PAGES[0]),
        (1, "margin: 20–30 % (EU)", "marketing", PAGES[1]),
    ]
    empty = {"counts": {"independent": 0, "marketing": 0, "unchecked": 0}, "items": []}
    assert all(v["evidence"] == empty for v in after["items"] if v["id"] != DROPSHIPPING)


def test_a_venture_cycle_has_its_own_tools_and_texts() -> None:
    # 0.12.0: evidence is a venture's; a venture cycle's guide offers the ventures manual only, and its workspace_write
    # speaks of neither draft nor the make_ tools (it has neither).
    ordinary = {d["name"]: d for d in tools.definitions(mail=True, etsy=True, venture=False)}
    venture = {d["name"]: d for d in tools.definitions(mail=True, etsy=True, venture=True)}
    assert "evidence" in venture and "evidence" not in ordinary
    assert venture["guide"]["input_schema"]["properties"]["topic"]["enum"] == ["ventures"]
    # 0.13.0: a channel's manual comes with the channel
    channels = ("pinterest", "printify")
    topics = [topic for topic in tools.GUIDES if topic not in channels]
    assert ordinary["guide"]["input_schema"]["properties"]["topic"]["enum"] == topics
    every = {d["name"]: d for d in tools.definitions(mail=True, etsy=True, pinterest=True, printify=True)}
    assert every["guide"]["input_schema"]["properties"]["topic"]["enum"] == list(tools.GUIDES)
    writing = venture["workspace_write"]["description"]
    assert "make_" not in writing and "draft" not in writing and "delete works for any file" in writing
    assert "with draft, or in parts" in ordinary["workspace_write"]["description"]
    assert tools.spec_of("guide", venture=True) is tools.VENTURE_VARIANTS["guide"]
    assert tools.spec_of("guide", venture=False) is tools.SPECS["guide"]
