"""Every request the wake cycle sends is one the real API accepts, also on the awkward paths.

The fake model checks each request like the API would (``validate_request``) and
records a rejected one in its trace as ``(n, "invalid", reason)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agent.fake_llm import SCENARIOS, Fail, FakeTransport, Plan, Raw, Reply, ToolCalls
from app.agent.service import Agent
from app.config import LoadedSettings
from app.economy.metering import Rejected
from tests.economy_helpers import make_economy
from tests.test_agent import ROOMY

PLAN = {"assessment": "ok", "goal": "Look around", "focus_project_id": None, "steps": ["look"], "sleep_minutes": 120}
JOURNAL = ToolCalls([("write_journal", {"summary": "Looked around", "entry": "Nothing yet."})])


def empty(stop: str = "end_turn") -> Raw:
    return Raw(
        {
            "id": "msg_empty",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 500, "output_tokens": 1},
        }
    )


def run(data_dir: Path, fake: FakeTransport, cycles: int = 1) -> tuple[Agent, list[Any]]:
    economy = make_economy(data_dir, ROOMY)
    agent = Agent(economy.db, LoadedSettings(ROOMY), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    ends = [agent.run_cycle("schedule") for _ in range(cycles)]
    invalid = [t for t in fake.trace if t[1] == "invalid"]
    assert invalid == [], invalid
    return agent, ends


def test_a_reply_cut_off_without_a_tool_call(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[Plan(PLAN), Reply("A long answer that got cut", "max_tokens"), Reply("Done."), JOURNAL]
    )
    _, ends = run(data_dir, fake)
    assert ends[0].status == "completed"


def test_empty_replies_after_tool_results(data_dir: Path) -> None:
    fake = FakeTransport(script=[Plan(PLAN), ToolCalls([("workspace_list", {})]), empty(), empty(), JOURNAL])
    _, ends = run(data_dir, fake)
    assert ends[0].status == "completed"


def test_no_reflection_when_the_first_work_call_failed(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.agent.loop.RETRY_DELAY_SECONDS", 0)
    refused = Fail(Rejected(400, "invalid_request_error | nope"))
    fake = FakeTransport(script=[Plan(PLAN), refused, refused, JOURNAL])  # the free failure is retried once
    agent, ends = run(data_dir, fake)
    with agent.db.connection() as conn:
        purposes = [r[0] for r in conn.execute("SELECT purpose FROM llm_calls ORDER BY id")]
        journal = conn.execute("SELECT author, summary FROM journal").fetchall()
    assert purposes == ["plan", "work", "work"]  # nothing ran, so there is nothing to reflect on
    assert ends[0].status == "failed" and ends[0].note and "nope" in ends[0].note
    assert [tuple(r) for r in journal] == [("system", f"Cycle ended failed: {ends[0].note}")]


def test_reflecting_when_the_first_reply_was_empty(data_dir: Path) -> None:
    # No act turn can be sent back, so the reflect prompt joins the brief's turn.
    fake = FakeTransport(script=[Plan(PLAN), empty(), JOURNAL])
    agent, ends = run(data_dir, fake)
    with agent.db.connection() as conn:
        purposes = [r[0] for r in conn.execute("SELECT purpose FROM llm_calls ORDER BY id")]
        journal = conn.execute("SELECT summary FROM journal").fetchall()
    assert purposes == ["plan", "work", "reflect"] and ends[0].status == "completed"
    assert [r[0] for r in journal] == ["Looked around"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_sends_only_valid_requests(data_dir: Path, scenario: str) -> None:
    run(data_dir, FakeTransport(seed=7, scenario=scenario), cycles=4)
