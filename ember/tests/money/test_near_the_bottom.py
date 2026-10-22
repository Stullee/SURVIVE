"""0.37.2 (analysis 0.37.0, 3.4): near the bottom of the balance a workshop run could still kill Ember below zero,
without its last will.

0.21.0 made a workshop or research call need SERVER_TOOL_ROOM (5) times what it holds left above the last will's
reserve, after live run #423 cost 5.3 times its quote. 0.33.0 let a run hold no more than the day has left, but
clamped its hold to the money room, which already holds a fifth of what is left above that reserve: near the bottom
the hold shrank to that fifth, and five times it fit by construction. After a run that cost $1.76 (a hold of $2.64:
5 times that is $13.22) a run went ahead with $3.10 left above the reserve, holding $0.62; it cost $3.21, and Ember
died at -$0.08 without its last will. The 0.21.0 test (test_fixes_0140_money_guard) calls the guard without hold=, so
it never took the workshop's own path. A research call's hold was never cut that way (the last test).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.agent import prompts
from app.agent.fake_llm import FakeTransport, Overrun, Turn
from app.agent.service import Agent
from app.agent.workshop import Workshop
from app.config import LoadedSettings, Settings
from app.economy import pricing
from app.economy.costs import micros_to_usd
from app.economy.metering import RESEARCH, WORKSHOP, CallRefused, Completed, usd_cap_to_micros
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message, metered, owner

# The owner's caps of 0.13.0, and the cap per run's default ($0.75)
OWNER = Settings(starting_balance_usd=20, daily_spend_cap_usd=7, cycle_spend_cap_usd=1)
TASK = "Draw a chart of 1,2,3 as chart.png"
COSTLY = Overrun(cache_write=1_000_000, cache_read=3_500_000)  # 5.5 times its quote, as #423 cost 5.3 times its own


def an_agent(data_dir: Path, settings: Settings, script: list[Turn]) -> tuple[Agent, FakeTransport, FakeClock]:
    """An agent whose first workshop run, sent here through the guard, is one like #423 ($1.76): what recent runs cost
    makes the next one hold $2.64."""
    clock = FakeClock()
    economy = make_economy(data_dir, settings, clock)
    fake = FakeTransport(script=[Overrun(), *script], clock=lambda: clock.current.timestamp())
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    cycle = agent.meter.open_cycle("test")
    agent.meter.call(cycle, WORKSHOP, prompts.workshop_request(settings, TASK, []))
    agent.meter.close_cycle(cycle)
    return agent, fake, clock


def near_the_bottom(data_dir: Path, script: list[Turn]) -> tuple[Agent, FakeTransport, int]:
    """The next day, after the owner's Reset estimates (what runs cost still counts), with $3.10 left above the last
    will's reserve. Returns the agent, its fake model and that reserve."""
    agent, fake, clock = an_agent(data_dir, OWNER, script)
    pricing.reset_safety_factors(agent.db, "dry_run")
    clock.advance(days=1)
    reserve = pricing.last_will_reserve(OWNER, agent.db, "dry_run") or 0
    spent = agent.economy.life.evaluate().balance - reserve - 3_100_000
    owner(agent.economy, "adjustment", f"{spent / 1e6:.2f}", direction="subtract", test_money=True)
    return agent, fake, reserve


def workshop(agent: Agent, settings: Settings = OWNER) -> Workshop:
    return Workshop(agent.db, agent.clock, settings, agent.meter, agent.scope(), agent.roots()[0])


def workshop_calls(agent: Agent) -> list[dict[str, Any]]:
    with agent.db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_calls WHERE purpose = 'workshop' ORDER BY id")]


def test_near_the_bottom_the_workshop_refuses_a_run_its_recent_runs_say_could_kill_ember(data_dir: Path) -> None:
    agent, fake, reserve = near_the_bottom(data_dir, [COSTLY])
    asked = prompts.workshop_request(OWNER, TASK, [])
    first = workshop_calls(agent)[0]["cost_micros"]
    whole, quote = agent.meter.reservation(asked, WORKSHOP), agent.meter.quote(asked, WORKSHOP)
    assert whole == -(-first * 3 // 2) > quote  # $2.64: 1.5 times the $1.76 run (its quote is $0.58)
    before = agent.economy.life.evaluate()
    above = before.balance - reserve
    assert 5 * quote <= above < 5 * whole  # $3.10: the quote fits 5 times, the hold doesn't ($13.22)
    cycle = agent.meter.open_cycle("owner")
    run = workshop(agent).run(cycle, TASK, [], None, None)
    # 0.37.0 sent it holding $0.62, a fifth of what was left: it cost $3.21, and Ember died at -$0.08 with no last will
    assert run.failure is not None and run.calls == 0 and len(fake.sent) == 1
    assert f"the run keeps back ${micros_to_usd(whole):.3f}" in run.failure
    assert "it needs several times its hold above the last will's reserve" in run.failure
    after = agent.economy.life.evaluate_and_persist()
    assert after.state == before.state == "critical" and after.balance == before.balance
    assert after.last_will_at is None  # its reserve is whole: the last will can still be written
    assert len(workshop_calls(agent)) == 1  # refused before the guard: no refusal booked against the run either


def test_the_guard_counts_the_whole_hold_five_times_whatever_the_workshop_holds_of_the_day(data_dir: Path) -> None:
    agent, fake, _ = near_the_bottom(data_dir, [COSTLY])
    asked = prompts.workshop_request(OWNER, TASK, [])
    whole = agent.meter.reservation(asked, WORKSHOP)
    cycle = agent.meter.open_cycle("owner")
    shrunk = agent.meter.rooms(cycle, WORKSHOP)[1]  # 0.37.0's hold: the money room, a fifth of what is left
    assert agent.meter.reservation(asked, WORKSHOP, room=shrunk) == shrunk < whole
    held = re.escape(f"(${micros_to_usd(whole):.4f}), so it needs 5.0 times that left above the last will's reserve")
    with pytest.raises(CallRefused, match=f"can cost more than it holds {held}"):
        agent.meter.call(cycle, WORKSHOP, asked, hold=shrunk)
    assert len(fake.sent) == 1 and agent.economy.life.evaluate().state != "dead"


def test_late_in_the_day_a_run_still_holds_no_more_than_the_day_has_left(data_dir: Path) -> None:
    """0.33.0, on the workshop's own path: with plenty of money, a run late in the day holds what is left of the day
    (live, a $2.76 hold refused every run once the day's spending passed about $3)."""
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=4, cycle_spend_cap_usd=1, workshop_run_cap_usd=1.5)
    agent, _, _ = an_agent(data_dir, settings, [])
    asked = prompts.workshop_request(settings, TASK, [])
    left = usd_cap_to_micros(settings.daily_spend_cap_usd) - workshop_calls(agent)[0]["cost_micros"]
    assert agent.meter.quote(asked, WORKSHOP) <= left < agent.meter.reservation(asked, WORKSHOP)
    cycle = agent.meter.open_cycle("test")
    run = workshop(agent, settings).run(cycle, TASK, [], None, None)
    assert run.calls == 1 and [(r["status"], r["estimate_micros"]) for r in workshop_calls(agent)][1:] == [("ok", left)]


def test_a_research_call_s_whole_hold_counts_five_times_near_the_bottom_too(data_dir: Path) -> None:
    """Research holds 1.5 times the costliest recent research (0.21.0), and no room ever cut that: its pre-check and the
    guard count 5 times all of it."""
    settings = Settings(starting_balance_usd=20, daily_spend_cap_usd=7, cycle_spend_cap_usd=7)
    economy = make_economy(data_dir, settings)
    costly = Completed(message(300_000, 20_000, server_tool_use={"web_search_requests": 5}))
    model, transport = metered(economy, ScriptedTransport(outcomes=[costly]))
    first = model.open_cycle("test")
    asked = prompts.research_request(settings, "What sells?", None)
    model.call(first, RESEARCH, asked)  # $0.85, 3.5 times its worst case
    model.close_cycle(first)
    cycle = model.open_cycle("test")
    quote, whole = model.quote(asked, RESEARCH), model.reservation(asked, RESEARCH)
    assert model.reservation(asked, RESEARCH, room=10_000) == whole > quote  # a room cuts only a workshop run's hold
    reserve = pricing.last_will_reserve(settings, economy.db, "dry_run") or 0
    spent = economy.life.evaluate().balance - reserve - (5 * quote + 5 * whole) // 2  # 5 times the quote fits
    owner(economy, "adjustment", f"{spent / 1e6:.2f}", direction="subtract", test_money=True)
    assert model.affordable(asked, RESEARCH, cycle)[0] is False
    with pytest.raises(CallRefused, match=re.escape(f"can cost more than it holds (${micros_to_usd(whole):.4f})")):
        model.call(cycle, RESEARCH, asked)
    assert len(transport.sent) == 1
