"""0.12.0: research bound to a venture, with a research budget. "Decide every venture within about $3" was a line in
the prompt that nothing checked (DECIDE_USD was only displayed). Now a venture that isn't backed has a research budget
(RESEARCH_BUDGET_USD of research calls, from its start or since the owner last asked for research on it): once it is
spent, Ember's code refuses more research for it until the owner grants more, and FOCUS and the Ventures tab say what
is left. A venture cycle's research is always a venture's, and a question asked again within 30 days (the same words,
site or page) is answered from before: free, and it doesn't count as research for a venture."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.agent import ventures, views
from app.agent.fake_llm import FakeTransport, Reply, ToolCalls, request_kind
from app.config import Settings
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_ventures import DROPSHIPPING, JOURNAL, VENTURING, found, plan, tool_results

ORDINARY = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, venture_share=0)


def research(question: str, **more: str | int) -> tuple[str, dict[str, str | int]]:
    return ("research", {"question": question, **more})


def test_research_stops_at_the_budget_until_the_owner_grants_more(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ventures, "RESEARCH_BUDGET_USD", 0.02)  # a found() call costs about $0.014
    fake = FakeTransport(
        script=[
            plan(venture=DROPSHIPPING),
            ToolCalls([research("Who sells phone cases?"), research("At what price?"), research("How many a month?")]),
            found("https://example.invalid/sellers"),
            found("https://example.invalid/prices"),  # the budget is spent now: the third is refused
            Reply("Done."),
            JOURNAL,
            plan(venture=DROPSHIPPING),
            ToolCalls([research("How many a month?", venture_id=DROPSHIPPING)]),
            found("https://example.invalid/sales"),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    brief = next(r for r in fake.sent if request_kind(r) == "work")["messages"][0]["content"][0]["text"]
    assert "(scores need 1, a business case 2); research budget: $0.02 of $0.02 left\n" in brief
    [refused] = [r for r in tool_results(agent, "research") if r["status"] == "error"]
    assert refused["result"] == (
        f"Error: venture #{DROPSHIPPING} has used its research budget ($0.03 of $0.02): decide it now: its business "
        "case (stage proposed) or parked, with why in its note. Only your owner grants more research for it (Research "
        "more on the Ventures tab)."
    )
    item = next(v for v in views.ventures_view(agent)["items"] if v["id"] == DROPSHIPPING)
    assert (item["research_spent_usd"], item["research_left_usd"]) == (pytest.approx(0.0256, abs=0.002), 0)
    refused_grant = pytest.raises(sqlite3.IntegrityError, match="only the owner grants more research")
    with refused_grant, agent.db.transaction() as conn:  # only the owner's word starts a new budget
        conn.execute("UPDATE ventures SET research_granted_at = '2026-09-02T00:00:00Z' WHERE id = ?", (DROPSHIPPING,))
    agent.clock.advance(minutes=1)
    assert owner(agent).decide_venture(DROPSHIPPING, {"action": "research"}, "Stefan").status == 200
    item = next(v for v in views.ventures_view(agent)["items"] if v["id"] == DROPSHIPPING)
    assert (item["research_spent_usd"], item["research_left_usd"]) == (0, 0.02)  # a new budget
    agent.run_cycle("schedule")
    assert [r["status"] for r in tool_results(agent, "research")] == ["ok", "ok", "error", "ok"]
    assert rows(agent, "SELECT COUNT(*) AS n FROM venture_research WHERE venture_id = 3") == [{"n": 3}]


def test_a_backed_venture_has_no_research_budget() -> None:
    row = {"id": 1, "stage": "building", "research_spent": 10**9}
    assert ventures.research_left(row) is None and ventures.research_refusal(row) == ""
    assert ventures.budget_text(row) == ""
    parked = {"id": 1, "stage": "parked", "research_spent": 10**9}
    assert ventures.research_refusal(parked).startswith(
        "venture #1 has used its research budget ($1000.00 of $0.60). Only"
    )


def test_a_repeated_question_is_answered_from_before(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(steps=["research"]),  # an ordinary cycle: 3 research calls
            ToolCalls([research("What do phone cases sell for?")]),
            found("https://example.invalid/prices"),
            ToolCalls(
                [
                    research("what do Phone Cases  sell for"),  # the same words: answered from before
                    research("What do phone cases sell for?", site="example.invalid"),  # another search: a call
                    research("Which cases sell best?"),  # the third call: a repeat didn't count toward the limit
                ]
            ),
            found("https://example.invalid/site-prices"),
            found(),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, settings=ORDINARY)
    calls = tool_results(agent, "research")
    assert [c["status"] for c in calls] == ["ok"] * 4
    assert calls[1]["summary"] == "research (repeated) of call #1: what do Phone Cases  sell for"
    assert calls[1]["result"].startswith(
        "Not researched again: you asked this on 2026-09-01 (cycle #1), so this is that answer (free; a repeat"
        ' doesn\'t count as research for a venture: ask what it left open).\n<data src="research" id="'
    )
    nonce = calls[1]["result"].split('<data src="research" id="')[1].split('"')[0]
    assert calls[1]["result"].endswith(f'</data id="{nonce}">\nSources:\n- https://example.invalid/prices')
    assert f'id="{nonce}"' in calls[0]["result"]  # this cycle's nonce: the same cycle
    assert rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'research'") == [{"n": 3}]


def test_a_venture_cycles_research_is_a_ventures(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[plan(steps=["Brainstorm"]), ToolCalls([research("What sells in Germany?")]), Reply("Done."), JOURNAL]
    )
    agent, _ = run(data_dir, fake, settings=VENTURING)
    assert rows(agent, "SELECT venture, venture_id FROM cycles") == [{"venture": 1, "venture_id": None}]
    [refused] = tool_results(agent, "research")
    assert refused["status"] == "error"
    assert "name the venture it researches (venture_id): a venture cycle's research is a venture's" in refused["result"]
