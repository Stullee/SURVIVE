"""0.12.0: burn modes set by Ember's code from the net runway. How fast the agent spent was a line in its prompt ("the
daily cap is there to be spent"), whatever the runway said. Now Ember's code sets the mode: explore above 30 days of net
runway, focus from 15 to 30 (the tests already running, no brainstorms), maintenance below 15 (one scheduled cycle a
day of at most $0.40, no venture cycles), and dormant once the last will is written and the runway is critical (no
model calls until money comes in). A mode moves up only 20% past its threshold, and the Etsy sync goes on while the
agent is paused or dormant.

Review of 0.16.1 (bug 6): a lower mode spends less, so the week's spending fell and the net runway grew past the margin
within days: maintenance went back to focus and full spending, then down again. A mode below explore now moves up only
once money came in since it began, judged at the API spending of the week before it moved down."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent import ventures
from app.agent.fake_llm import FakeTransport, ToolCalls
from app.config import Settings
from app.economy import burn
from app.economy.life import LifeStatus, Runway
from app.economy.metering import Completed, MeteredModel
from app.economy.service import Economy
from tests import economy_helpers
from tests.economy_helpers import START, FakeClock, ScriptedTransport, make_economy, message, metered, request
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_roadmap import planner_texts
from tests.test_ventures import DROPSHIPPING, JOURNAL, VENTURING, plan


def status(
    net: float | None,
    will: bool = False,
    critical: bool = False,
    money_in: int = 0,
    stance: str = "conserve",  # 0.18.0: these tests are the burn modes as they were (the owner's conserve)
    **runway: Any,
) -> LifeStatus:
    return LifeStatus(
        mode="live",
        life_id=1,
        state="critical" if critical else "alive",
        reason="",
        critical_since="2026-09-30T00:00:00Z" if critical else None,
        last_will_at="2026-09-30T00:00:00Z" if will else None,
        runway=Runway(net, None, net_days=net, **runway),
        money_in_id=money_in,
        balance=80_000_000,
        stance=stance,
    )


def test_the_mode_follows_the_net_runway_and_moves_up_only_past_a_margin() -> None:
    assert [burn.settle(None, status(n)) for n in (None, 45, 25, 10)] == ["explore", "explore", "focus", "maintenance"]
    assert burn.settle(None, status(1, will=True, critical=True)) == "dormant"
    assert burn.settle(None, status(1, critical=True)) == "maintenance"  # its last will comes first
    assert burn.settle("explore", status(29)) == "focus"  # down at once
    began = burn.Since(money_in=0, spend=0)  # the mode began before ledger entry #1, which brought money in
    assert burn.settle("focus", status(33, money_in=1), began) == "focus"
    assert burn.settle("focus", status(37, money_in=1), began) == "explore"
    assert burn.settle("maintenance", status(17, money_in=1), began) == "maintenance"
    assert burn.settle("maintenance", status(19, money_in=1), began) == "focus"
    assert burn.settle("maintenance", status(40, money_in=1), began) == "explore"  # past both margins
    assert burn.settle("maintenance", status(None, money_in=1), began) == "explore"  # it earns what it spends
    for previous, net in (("focus", 37), ("maintenance", 19), ("maintenance", 40), ("maintenance", None)):
        assert burn.settle(previous, status(net), began) == previous  # 0.16.2: no money came in since it began
        assert burn.settle(previous, status(net, money_in=1)) == previous  # nor is it known when it began
    assert (
        burn.Burn("maintenance", 9.0).cycle_cap(500_000) == 400_000
        and burn.Burn("focus", 20.0).cycle_cap(500_000) == 500_000
    )


def test_a_move_up_is_judged_at_the_spending_from_before_the_mode_moved_down() -> None:
    # $80 left; $5.50 a day before maintenance, $0.40 a day since: 200 days at today's spending, 14.5 at the old
    week = {"window_spend": 2_800_000, "active_days": 7.0}
    low = status(200.0, money_in=2, **week)
    since = burn.Since(money_in=1, spend=5_500_000)
    assert burn.judged(low, since) == pytest.approx(80 / 5.5)
    assert burn.settle("maintenance", low, since) == "maintenance"
    # $7 of sales in the week: $80 at $5.50 less $1 a day lasts 17.8 days; $14 make it 22.9, enough for focus
    assert burn.settle("maintenance", status(None, money_in=2, window_net_in=7_000_000, **week), since) == "maintenance"
    assert burn.settle("maintenance", status(None, money_in=2, window_net_in=14_000_000, **week), since) == "focus"
    # sales of $5.50 a day pay for the spending from before: back to explore
    assert burn.judged(status(None, money_in=2, window_net_in=38_500_000, **week), since) is None
    assert burn.settle("maintenance", status(None, money_in=2, window_net_in=38_500_000, **week), since) == "explore"
    # spending as much now as before: today's net runway, as until 0.16.2
    same = status(19.0, money_in=2, window_spend=38_500_000, active_days=7.0)
    assert burn.judged(same, since) == 19.0 and burn.settle("maintenance", same, since) == "focus"


WEEK = Settings(starting_balance_usd=100, daily_spend_cap_usd=7, cycle_spend_cap_usd=7, spending_stance="conserve")


def a_day(model: MeteredModel, transport: ScriptedTransport, clock: FakeClock, micros: int) -> str:
    """A day with one cycle that spends ``micros`` (one planning call, 1,000 tokens in); the burn mode it opened in."""
    out = (micros - 2_000) // 10  # 1,000 tokens in at $2 and the rest out at $10 per million
    transport.outcomes.append(Completed(message(1_000, out)))
    cycle = model.open_cycle("test")
    assert model.call(cycle, "plan", request(max_tokens=out)).cost_micros == micros
    model.close_cycle(cycle)
    clock.advance(days=1)
    with model.db.connection() as conn:
        return str(conn.execute("SELECT burn_mode FROM cycles WHERE id = ?", (cycle,)).fetchone()[0])


def burn_events(economy: Economy) -> list[str]:
    with economy.db.connection() as conn:
        found = conn.execute("SELECT message FROM events WHERE message LIKE 'Burn mode:%' ORDER BY id").fetchall()
    return [str(r[0]).split(" (")[0] for r in found]


def test_maintenance_stays_down_through_a_week_of_its_own_spending(data_dir: Path) -> None:
    # Bug 6, as on the owner's ledger: maintenance's $0.40 a day took the week's spending down, its net runway past 18
    # days two days later and the mode back to focus and full spending; at the $7 cap 11 more flips followed.
    clock = FakeClock()
    economy = make_economy(data_dir, WEEK, clock)
    model, transport = metered(economy)
    assert [a_day(model, transport, clock, 5_500_000) for _ in range(4)] == ["explore", "focus", "focus", "focus"]
    week = [a_day(model, transport, clock, 380_000) for _ in range(8)]  # $78 lasts 14.2 days at $5.50: maintenance
    assert week == ["maintenance"] * 8  # before: focus again on its third day (17.3, then 20.4 days)
    assert burn_events(economy) == ["Burn mode: focus", "Burn mode: maintenance"]
    status_now = economy.life.evaluate()
    assert status_now.runway.net_days is not None and status_now.runway.net_days > 36  # at $0.38 a day
    shown = burn.peek(economy.db, status_now)
    assert shown.mode == "maintenance" and "; up again only once money comes in): one cycle a day" in shown.text()
    assert economy.dashboard()["agent"]["burn_mode"] == "maintenance"
    # A $3 sale: money came in, but at the $5.50 a day from before, less the sale, $78 lasts 15.4 days
    economy_helpers.owner(economy, "revenue", "3", test_money=True)
    shown = burn.peek(economy.db, economy.life.evaluate())
    assert shown.mode == "maintenance" and "; 15.4 days at the spending from before it moved down): " in shown.text()
    assert a_day(model, transport, clock, 380_000) == "maintenance"  # before: explore, as it earned what it spent
    # A grant of $30 lifts it as far as that spending allows: 21.2 days, focus
    economy_helpers.owner(economy, "grant", "30", test_money=True)
    assert a_day(model, transport, clock, 380_000) == "focus"
    assert burn_events(economy)[-1] == "Burn mode: focus"


def test_maintenance_runs_one_small_cycle_a_day(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.MAINTENANCE)
    agent, ends = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    assert ends[0].status == "idle"
    assert rows(agent, "SELECT cap_micros, venture FROM cycles") == [{"cap_micros": 400_000, "venture": 0}]
    assert agent.db.get_meta(agent._key("next_wake_reason")) == "the burn mode is maintenance: one cycle a day"
    status_text = planner_texts(agent.transport)[0]  # type: ignore[arg-type]
    assert "Burn mode, set by Ember's code: maintenance (net runway: " in status_text
    assert agent.economy.dashboard()["agent"]["burn_mode"] == "maintenance"
    assert agent.economy.sensors()["burn_mode"] == "maintenance"


def test_focus_goes_on_with_the_tests_already_running(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.FOCUS)
    brainstorm = ToolCalls([("brainstorm", {})])
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=["Brainstorm"]), brainstorm, JOURNAL])
    agent, _ = run(data_dir, fake, settings=VENTURING)
    assert rows(agent, "SELECT venture FROM cycles") == [{"venture": 0}]  # nothing backed or live: no venture cycle
    with agent.db.transaction() as conn:
        ventures.update(conn, DROPSHIPPING, "2026-09-01T12:00:00Z", first_test="Sell 3 stores' worth of samples")
    assert (
        owner(agent).decide_venture(DROPSHIPPING, {"action": "back", "confirm": True}, "Stefan").status == 200
    )  # a test runs
    agent.run_cycle("schedule")
    # 0.19.3: the backed venture's test is its project's work, in ordinary cycles: no venture cycle runs in focus
    assert rows(agent, "SELECT venture FROM cycles ORDER BY id")[-1] == {"venture": 0}
    [project] = rows(agent, f"SELECT status, next_step FROM projects WHERE venture_id = {DROPSHIPPING}")
    assert project["status"] == "active" and project["next_step"].startswith("Run its first test (milestone #")
    [refused] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'brainstorm'")
    assert refused["status"] == "error"


def test_dormant_makes_no_model_calls_until_money_comes_in(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    monkeypatch.setattr(burn, "_raw", lambda status: burn.DORMANT)
    agent.clock.advance(days=1)
    decision = agent.decide()
    assert not decision.run and decision.reason.startswith("Dormant: no model calls until money comes in")
    events = [e["message"] for e in agent.db.recent_events(limit=20)]
    assert any(m.startswith("Burn mode: dormant (net runway: ") for m in events)
    agent.wake_requested = True  # the owner's Wake now still runs a cycle
    assert agent.decide().run


# --- 0.18.0: the owner's spending stance ---


def test_invest_keeps_exploring_until_the_last_will() -> None:
    for net in (None, 45, 25, 10, 1):
        assert burn.settle(None, status(net, stance="invest")) == "explore", net
    assert burn.settle(None, status(1, will=True, critical=True, stance="invest")) == "dormant"
    assert burn.settle("maintenance", status(10, stance="invest")) == "explore"  # the owner's choice lifts it at once
    assert burn.settle("dormant", status(10, stance="invest")) == "explore"  # the will is past: as conserve does
    assert burn.projected(burn.Burn(burn.EXPLORE, 40.0, stance=burn.INVEST), START) is None  # no change to come
    short = burn.Burn(burn.EXPLORE, 12.0, stance=burn.INVEST)
    assert short.fight and "fastest honest path to a first euro" in short.text()
    assert not burn.Burn(burn.EXPLORE, 16.0, stance=burn.INVEST).fight
    assert not burn.Burn(burn.EXPLORE, 12.0, stance=burn.CONSERVE).fight


def test_steady_goes_no_lower_than_focus() -> None:
    assert [burn.settle(None, status(n, stance="steady")) for n in (45, 25, 10)] == ["explore", "focus", "focus"]
    assert burn.settle("maintenance", status(10, stance="steady")) == "focus"
    assert burn.projected(burn.Burn(burn.FOCUS, 18.0, stance=burn.STEADY), START) is None
    now = START
    assert burn.projected(burn.Burn(burn.EXPLORE, 40.0, stance=burn.STEADY), now) == (
        burn.FOCUS,
        now + timedelta(days=10),
    )
    assert burn.settle(None, status(10, stance="unknown")) == "explore"  # an unknown stance counts as invest


def test_invest_warns_the_owner_once_under_15_days(data_dir: Path) -> None:
    clock = FakeClock()
    economy = make_economy(data_dir, Settings(starting_balance_usd=100, daily_spend_cap_usd=7), clock)
    for net, mode in ((12.0, "explore"), (11.0, "explore"), (20.0, "explore"), (10.0, "explore")):
        assert burn.current(economy.db, status(net, stance="invest")).mode == mode
    with economy.db.connection() as conn:
        warned = [
            str(r[0]) for r in conn.execute("SELECT message FROM events WHERE message LIKE 'Net runway%' ORDER BY id")
        ]
    assert len(warned) == 2  # once, and again after it was 20% past 15 days
    assert warned[0].startswith("Net runway 12.0 days: your spending stance is invest") and "grant" in warned[0]


def test_the_stance_is_the_owner_s_option() -> None:
    assert Settings().spending_stance == "invest"
    assert Settings(spending_stance="conserve").spending_stance == "conserve"
    with pytest.raises(ValueError):
        Settings(spending_stance="lavish")
