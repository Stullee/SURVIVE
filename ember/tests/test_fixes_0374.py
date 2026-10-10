"""0.37.4 (analysis 0.37.0, section 5, Money): the research model's check skipped the server-tool hold and the 5x rule.

While the owner's research model is checked (research_model, loop._research_model), each of the agent's research
questions is asked of it too (loop._compare_research), with research's own request (prompts.research_request) but as a
research_check call. 0.21.0 made a workshop or research call need SERVER_TOOL_ROOM (5) times what it keeps back left
above the last will's reserve, and research keep back at least 1.5 times the costliest recent research, because a
search can cost several times its worst case. A research_check call had neither (metering.SERVER_TOOL_PURPOSES,
MeteredModel._held): near the bottom of the balance a check whose quote fit once above the reserve was sent, and could
cost more than was left. Research and its check now keep back what either cost on their model
(metering.RESEARCH_PURPOSES): they send the same request.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent import prompts
from app.config import Settings
from app.economy import pricing
from app.economy.costs import micros_to_usd
from app.economy.metering import RESEARCH, RESEARCH_CHECK, CallRefused, Completed, server_tool_room
from app.economy.service import Economy
from tests.economy_helpers import ScriptedTransport, make_economy, message, metered, owner
from tests.test_agent import make_agent, rows
from tests.test_reflection_reserve import runner_and_cycle

HAIKU = "claude-haiku-4-5"
# The owner's daily cap of 0.13.0 with a cycle cap as large, so only the balance refuses; Haiku 4.5 is being checked
SETTINGS = Settings(starting_balance_usd=20, daily_spend_cap_usd=7, cycle_spend_cap_usd=7, research_model=HAIKU)
QUESTION = "Who buys weekly meal planners?"
CHECK = prompts.research_request(SETTINGS, QUESTION, None, None, HAIKU)  # what loop._compare_research sends
WORKER = prompts.research_request(SETTINGS, QUESTION, None)  # the agent's own research, on the worker model


def costly(input_tokens: int, output_tokens: int) -> Completed:
    """A search on Haiku that read far more than its worst case allows for."""
    return Completed(message(input_tokens, output_tokens, model=HAIKU, server_tool_use={"web_search_requests": 5}))


def above_the_reserve(economy: Economy, left: int) -> int:
    """Spend the balance down to ``left`` above the last will's reserve (to the cent); returns what is left."""
    reserve = pricing.last_will_reserve(SETTINGS, economy.db, "dry_run") or 0
    spent = economy.life.evaluate().balance - reserve - left
    owner(economy, "adjustment", f"{spent / 1e6:.2f}", direction="subtract", test_money=True)
    return economy.life.evaluate().balance - reserve


def test_near_the_bottom_a_check_needs_five_times_its_hold_above_the_reserve(data_dir: Path) -> None:
    economy = make_economy(data_dir, SETTINGS)
    model, transport = metered(economy, ScriptedTransport(outcomes=[costly(300_000, 20_000)]))
    first = model.open_cycle("test")
    cost = model.call(first, RESEARCH_CHECK, CHECK).cost_micros  # $0.45, 3.5 times its worst case
    model.close_cycle(first)  # the overrun stopped it
    cycle = model.open_cycle("test")
    quote, hold = model.quote(CHECK, RESEARCH_CHECK), -(-cost * 3 // 2)  # $0.49 (raised), $0.675 (1.5 times $0.45)
    above = above_the_reserve(economy, (5 * quote + 5 * hold) // 2)
    assert 5 * quote <= above < 5 * hold  # the quote fits 5 times above the reserve, the hold doesn't
    # until 0.37.4 the check was judged on its quote, once: the pre-check let it through and the guard sent it
    assert model.affordable(CHECK, RESEARCH_CHECK, cycle)[0] is False
    held = re.escape(f"(${micros_to_usd(hold):.4f}), so it needs 5.0 times that left above the last will's reserve")
    with pytest.raises(CallRefused, match=f"a research_check call can cost more than it holds {held}"):
        model.call(cycle, RESEARCH_CHECK, CHECK)
    assert len(transport.sent) == 1
    status = economy.life.evaluate()
    assert status.can_run and status.last_will_at is None  # the reserve is whole: the last will can still be written


def test_near_the_bottom_the_agent_s_question_is_answered_and_its_check_waits(data_dir: Path) -> None:
    """The research tool's own path: the worker model answers the agent, then loop._compare_research asks the research
    model only if meter.affordable lets it, and a comparison it can't pay for waits for another question."""
    answer = Completed(message(1_000, 200, server_tool_use={"web_search_requests": 1}))
    agent, transport = make_agent(data_dir, [costly(300_000, 20_000), answer], SETTINGS)
    first = agent.meter.open_cycle("test")
    cost = agent.meter.call(first, RESEARCH_CHECK, CHECK).cost_micros
    agent.meter.close_cycle(first)
    quote, hold = agent.meter.quote(CHECK, RESEARCH_CHECK), -(-cost * 3 // 2)
    own = agent.meter.reservation(WORKER, RESEARCH)
    above = above_the_reserve(agent.economy, (5 * quote + 5 * hold) // 2)
    assert 5 * own < 5 * quote <= above < 5 * hold  # the agent's search fits; the check's quote does, its hold doesn't
    runner, cycle_id, ctx = runner_and_cycle(agent)
    found = runner._research_fn(ctx)(QUESTION, None, cycle_id, None)
    assert found.ok and found.summary == f"research: {QUESTION}"
    # until 0.37.4 the check was sent too, and its comparison booked
    calls = rows(agent, "SELECT purpose FROM llm_calls ORDER BY id")
    assert [c["purpose"] for c in calls] == [RESEARCH_CHECK, RESEARCH] and len(transport.sent) == 2
    assert rows(agent, "SELECT COUNT(*) AS n FROM research_checks")[0]["n"] == 0


def test_research_and_its_check_keep_back_what_either_cost_on_their_model(data_dir: Path) -> None:
    """One tail for research's request on a model, whichever purpose sent it. Until the research model takes over, its
    check's calls are the only research on it, so its first research keeps back what they cost; a check keeps back
    what research on its model cost (while it was the worker, say). Another model's calls don't count."""
    economy = make_economy(data_dir, SETTINGS)
    model, _ = metered(economy, ScriptedTransport(outcomes=[costly(300_000, 20_000), costly(600_000, 40_000)]))
    first = model.open_cycle("test")
    check = model.call(first, RESEARCH_CHECK, CHECK).cost_micros  # $0.45
    model.close_cycle(first)
    on_haiku = CHECK  # research, once Haiku took over, sends the check's very request
    assert model.quote(on_haiku, RESEARCH) < -(-check * 3 // 2) == model.reservation(on_haiku, RESEARCH)
    assert model.reservation(WORKER, RESEARCH) == model.quote(WORKER, RESEARCH)  # nothing seen on the worker model
    cycle = model.open_cycle("test")
    research = model.call(cycle, RESEARCH, on_haiku).cost_micros  # $0.85
    assert model.reservation(CHECK, RESEARCH_CHECK) == -(-research * 3 // 2) > model.quote(CHECK, RESEARCH_CHECK)


def test_a_check_that_cost_more_than_five_times_its_hold_raises_what_every_such_call_needs(data_dir: Path) -> None:
    """0.21.0: once a workshop or research call cost more than 5 times what it held, each needs that many times its
    hold above the last will's reserve (server_tool_room). A check of the research model's counts too."""
    economy = make_economy(data_dir, SETTINGS)
    model, _ = metered(economy, ScriptedTransport(outcomes=[costly(600_000, 40_000)]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, RESEARCH_CHECK, CHECK)
    times = Decimal(result.cost_micros) / Decimal(result.estimate_micros)
    assert times > 6  # $0.85, held at its worst case of $0.128
    assert server_tool_room(economy.db, economy.clock, True) == times
