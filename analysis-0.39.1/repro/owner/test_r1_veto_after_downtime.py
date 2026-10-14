"""OWNER repro: a veto window that runs out while the app is DOWN (not paused) approves its request in the very
first scheduler round after the restart, and the executor sends it in the same round: the owner had 1 hour of the
12-hour window to see it. 0.37.3 restarts the window after a pause or a wait for money only (windows_paused_at is
set only by a round that runs while executor_blocked())."""
from __future__ import annotations

from pathlib import Path

from app.agent import policy
from app.agent.service import Agent
from app.config import LoadedSettings
from tests.economy_helpers import restart
from tests.test_fixes_0140_unlock_safety import status_of
from tests.test_fixes_0373 import held_reply, scheduler_round


def test_window_runs_out_while_app_is_down(data_dir: Path) -> None:
    agent, request = held_reply(data_dir)
    agent.clock.advance(hours=1)
    scheduler_round(agent)  # the app runs for an hour: the reply is held, its card says "approved in 11 h"
    assert status_of(agent, request)["status"] == "pending"
    print("\nbefore the stop:", status_of(agent, request))
    # The app is stopped (host power cut, HA update, the owner stops it) and starts 12 hours later.
    agent.clock.advance(hours=policy.VETO_HOURS)
    economy = restart(agent.economy)
    fresh = Agent(agent.db, LoadedSettings(agent.settings), economy, transport=agent.transport, cycles_enabled=True)
    fresh.recover()  # startup: nothing restarts the windows
    fresh.run_policy()  # the scheduler's FIRST round after the start
    after = status_of(fresh, request)
    print("first round after the start:", after)
    sent = fresh.execute_approved()  # same round
    print("executor in that round:", sent)
    assert after["status"] == "approved" and after["decided_by"] == policy.POLICY_BY
    assert sent == [(request, "simulated")]


def test_same_with_a_pause_restarts_the_window(data_dir: Path) -> None:
    agent, request = held_reply(data_dir)
    agent.clock.advance(hours=1)
    scheduler_round(agent)
    agent.economy.set_paused(True, "Stefan")
    agent.run_policy()
    agent.clock.advance(hours=policy.VETO_HOURS)
    agent.economy.set_paused(False, "Stefan")
    agent.run_policy()
    print("\npause instead of downtime:", status_of(agent, request))
    assert status_of(agent, request)["status"] == "pending"
