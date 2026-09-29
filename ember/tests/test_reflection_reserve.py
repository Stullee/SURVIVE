"""The reflection's reserve (0.12.0, FIX NOW 9): research, brainstorms and workshop runs spent the money a cycle kept
for its reflection, and the cycle then ended without reflecting."""

from __future__ import annotations

from pathlib import Path

from app.agent import loop, tools
from app.agent.memory import Memory
from app.config import Settings
from app.economy.metering import WORKSHOP
from tests.test_agent import make_agent, plan, rows


def runner_and_cycle(agent):  # type: ignore[no-untyped-def]
    """A cycle runner as a wake cycle builds it, with an open cycle and its tool context."""
    scope = agent.scope()
    workspace, memory_root = agent.roots()
    runner = loop.CycleRunner(
        agent.db,
        agent.settings,
        agent.clock,
        agent.economy,
        agent.meter,
        scope,
        workspace,
        Memory(agent.db, memory_root, scope),
        agent.stop,
        None,
        None,
        None,
    )
    cycle_id = agent.meter.open_cycle("schedule")
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=scope,
        cycle_id=cycle_id,
        workspace=workspace,
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )
    return runner, cycle_id, ctx


def test_what_the_reflection_needs_is_kept_from_the_other_calls(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    _, cycle_id, _ = runner_and_cycle(agent)
    meter = agent.meter
    room = meter.headroom(cycle_id)  # the cycle cap binds (ROOMY: $1 a cycle, $5 a day)
    assert meter.headroom(cycle_id, keep=250_000) == room - 250_000
    # A workshop run has its own cap, which isn't the reflection's: only the day's room and the balance keep it.
    assert meter.headroom(cycle_id, WORKSHOP, keep=250_000) == meter.headroom(cycle_id, WORKSHOP)
    assert meter.headroom(cycle_id, WORKSHOP, keep=4_900_000) < meter.headroom(cycle_id, WORKSHOP)


def test_research_leaves_the_reflections_money(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    runner, cycle_id, ctx = runner_and_cycle(agent)
    research = runner._research_fn(ctx)
    ctx.state.reflect_reserve = agent.meter.headroom(cycle_id)  # all that is left is the reflection's
    refused = research("Who buys meal planners?", None, cycle_id, None)
    assert not refused.ok and refused.summary == "refused: budget"
    assert "more than this cycle has left after the $1.000 kept for your reflection" in refused.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose = 'research'")[0]["n"] == 0


def test_why_the_work_ended_is_kept_when_the_reflection_cant_be_paid(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.04)
    agent, _ = make_agent(data_dir, [plan()], settings)
    assert agent.run_cycle("schedule").note == loop.NO_STEP
    assert rows(agent, "SELECT act_end_reason FROM cycles")[0]["act_end_reason"] == loop.NO_STEP
