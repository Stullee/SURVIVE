"""0.12.0: the budget at expected cost. Every quote assumed the worst case, the whole prompt at the cache-write rate
and the whole max_tokens (live, later steps were estimated at 6.1 times their cost and reflections at 3.8 times), so
cycles stopped with 34-40% of their cap unused, and small caps ran no work at all. Now the cycle cap counts what a
call is expected to cost: the part of the prompt the conversation already cached at the read rate, the output that
such calls wrote lately, research by its recent costs; the reflection's reserve is at least 1.5 times the 95th
percentile of the recent reflections, and a reflection may overdraw the cycle cap by one cache miss. The daily cap, the
balance and the last-will reserve still count the worst case."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.fake_llm import FakeTransport, ToolCalls
from app.config import Settings
from app.economy.estimate import expected_micros, plan_request, worst_case_micros
from app.economy.metering import CallRefused, Completed
from tests.economy_helpers import ScriptedTransport, make_economy, message, metered, request
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_roadmap import JOURNAL, plan

CACHED = {"cache_control": {"type": "ephemeral"}}
SONNET = Settings().price_for("claude-sonnet-5")


def test_a_later_call_of_a_conversation_reads_what_the_earlier_ones_cached() -> None:
    assert SONNET is not None
    ask = plan_request(request(max_tokens=2_000, **CACHED), 31_000)
    # 30,000 read at 0.20, 1,000 written at 2.50, 2,000 of output at 10 (USD per million tokens); a miss would write
    # the 30,000 at 2.50 instead of reading them at 0.20
    assert expected_micros(ask, SONNET, 30_000) == (6_000 + 2_500 + 20_000, 69_000)
    assert expected_micros(ask, SONNET, 30_000, output_tokens=300) == (6_000 + 2_500 + 3_000, 69_000)
    assert expected_micros(ask, SONNET, 0)[0] == worst_case_micros(ask, SONNET, 10) == 97_500  # nothing cached yet
    plain = plan_request(request(max_tokens=2_000), 31_000)  # without caching every token is new
    assert expected_micros(plain, SONNET, 30_000) == (31_000 * 2 + 20_000, 0)


def test_the_cycle_cap_counts_expected_costs_and_a_reflection_one_miss_more(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.2))
    model, transport = metered(economy, ScriptedTransport(simulated=True, tokens=30_000))
    transport.outcomes = [Completed(message(30_000, 200), "r") for _ in range(4)]  # 62,000 each
    cycle = model.open_cycle("test")
    ask = request(max_tokens=2_000, **CACHED)
    assert model.expected(ask, "work", cycle) == model.quote(ask, "work") == 95_000  # nothing cached yet
    for _ in range(3):
        model.call(cycle, "work", ask)  # the third fits because it reads what the second cached: 124,000 + 26,000
    assert model.expected(ask, "work", cycle) == 26_000
    with pytest.raises(CallRefused, match=r"cycle cap of \$0\.20 would be exceeded .* this call about \$0\.0260"):
        model.call(cycle, "work", ask)  # 186,000 + 26,000
    assert model.affordable(ask, "reflect", cycle) == (True, 26_000, 95_000)  # one miss (69,000) more
    assert model.call(cycle, "reflect", ask).status == "ok"
    economy.clock.advance(minutes=5)
    assert model.expected(ask, "work", cycle) == 95_000  # a cache entry this old may be gone


def test_the_daily_cap_and_the_balance_still_count_the_worst_case(data_dir: Path) -> None:
    economy = make_economy(
        data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=0.2, cycle_spend_cap_usd=0.2)
    )
    model, transport = metered(economy, ScriptedTransport(simulated=True, tokens=30_000))
    transport.outcomes = [Completed(message(30_000, 200), "r") for _ in range(3)]
    cycle = model.open_cycle("test")
    ask = request(max_tokens=2_000, **CACHED)
    model.call(cycle, "work", ask)
    model.call(cycle, "work", ask)
    assert model.rooms(cycle) == (76_000, 76_000)
    assert model.affordable(ask, "work", cycle) == (False, 26_000, 95_000)  # the cycle cap would take it
    with pytest.raises(CallRefused, match="daily cap"):
        model.call(cycle, "work", ask)  # 124,000 + 95,000


def test_research_and_reflections_are_expected_to_cost_what_they_did_lately(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    model, transport = metered(economy, ScriptedTransport(simulated=True, tokens=1_000))
    search = request(max_tokens=1_000, tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}])
    cycle = model.open_cycle("test")
    worst = model.quote(search, "research")
    assert model.expected(search, "research", cycle) == worst  # no history yet
    costs = [4_000, 5_000, 6_000, 7_000, 12_000]
    transport.outcomes = [Completed(message(1_000, (c - 2_000) // 10), "r") for c in costs]
    for _ in costs:
        model.call(cycle, "research", search)
    assert model.expected(search, "research", cycle) == 18_000  # 1.5 times the 95th percentile (12,000)
    assert model.reflection_reserve(1_000, "claude-sonnet-5") == 1_000  # no reflections yet
    transport.outcomes = [Completed(message(1_000, 400), "r") for _ in range(5)]  # 6,000 each
    for _ in range(5):
        model.call(cycle, "reflect", request(max_tokens=2_000))
    assert model.reflection_reserve(1_000, "claude-sonnet-5") == 9_000
    assert model.reflection_reserve(20_000, "claude-sonnet-5") == 20_000  # never less than it is expected to cost


def test_a_small_cap_runs_the_work_the_worst_case_left_no_room_for(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.15)
    steps = [ToolCalls([("workspace_list", {})]) for _ in range(20)]
    agent, ends = run(data_dir, FakeTransport(script=[plan(steps=["look"] * 3), *steps, JOURNAL]), settings=settings)
    purposes = [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls ORDER BY id")]
    assert purposes.count("work") >= 10 and purposes[-1] == "reflect"  # the worst case refused the first step
    [cycle] = rows(agent, "SELECT cap_micros FROM cycles")
    spent = sum(r["cost_micros"] for r in rows(agent, "SELECT cost_micros FROM llm_calls"))
    assert spent <= cycle["cap_micros"]
