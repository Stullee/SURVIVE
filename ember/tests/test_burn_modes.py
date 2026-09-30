"""0.12.0: burn modes set by Ember's code from the net runway. How fast the agent spent was a line in its prompt ("the
daily cap is there to be spent"), whatever the runway said. Now Ember's code sets the mode: explore above 30 days of net
runway, focus from 15 to 30 (the tests already running, no brainstorms), maintenance below 15 (one scheduled cycle a
day of at most $0.40, no venture cycles), and dormant once the last will is written and the runway is critical (no
model calls until money comes in). A mode moves up only 20% past its threshold, and the Etsy sync goes on while the
agent is paused or dormant."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import ventures
from app.agent.fake_llm import FakeTransport, ToolCalls
from app.economy import burn
from app.economy.life import LifeStatus, Runway
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_roadmap import planner_texts
from tests.test_ventures import DROPSHIPPING, JOURNAL, VENTURING, plan


def status(net: float | None, will: bool = False, critical: bool = False) -> LifeStatus:
    return LifeStatus(
        mode="live",
        life_id=1,
        state="critical" if critical else "alive",
        reason="",
        critical_since="2026-09-30T00:00:00Z" if critical else None,
        last_will_at="2026-09-30T00:00:00Z" if will else None,
        runway=Runway(net, None, net_days=net),
    )


def test_the_mode_follows_the_net_runway_and_moves_up_only_past_a_margin() -> None:
    assert [burn.settle(None, status(n)) for n in (None, 45, 25, 10)] == ["explore", "explore", "focus", "maintenance"]
    assert burn.settle(None, status(1, will=True, critical=True)) == "dormant"
    assert burn.settle(None, status(1, critical=True)) == "maintenance"  # its last will comes first
    assert burn.settle("explore", status(29)) == "focus"  # down at once
    assert burn.settle("focus", status(33)) == "focus" and burn.settle("focus", status(37)) == "explore"
    assert burn.settle("maintenance", status(17)) == "maintenance" and burn.settle("maintenance", status(19)) == "focus"
    assert burn.settle("maintenance", status(40)) == "explore"  # past both margins
    assert burn.settle("maintenance", status(None)) == "explore"  # it earns what it spends
    assert (
        burn.Burn("maintenance", 9.0).cycle_cap(500_000) == 400_000
        and burn.Burn("focus", 20.0).cycle_cap(500_000) == 500_000
    )


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
    assert owner(agent).decide_venture(DROPSHIPPING, {"action": "back"}, "Stefan").status == 200  # a test runs
    agent.run_cycle("schedule")
    assert rows(agent, "SELECT venture FROM cycles ORDER BY id")[-1] == {"venture": 1}
    [refused] = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'brainstorm'")
    assert refused["status"] == "error" and "brainstorming isn't available right now" in refused["result"]


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
