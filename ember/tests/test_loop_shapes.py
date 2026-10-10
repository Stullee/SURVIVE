"""Every request the wake cycle sends is one the real API accepts, also on the awkward paths.

The fake model checks each request like the API would (``validate_request``) and
records a rejected one in its trace as ``(n, "invalid", reason)``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from app.agent import plan as plan_tree
from app.agent.fake_llm import SCENARIOS, Fail, FakeTransport, Plan, Raw, Reply, ToolCalls
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
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


def run(
    data_dir: Path,
    fake: FakeTransport,
    cycles: int = 1,
    before: Callable[[Agent], None] | None = None,
    settings: Settings = ROOMY,
    brake: bool = True,
) -> tuple[Agent, list[Any]]:
    """Run ``cycles`` cycles against ``fake``; ``before`` prepares the new agent (files, rows) first. ``brake`` False:
    without the plan's brake (``unbraked``), for a test that builds its state with the fake model's founder routine."""
    economy = make_economy(data_dir, settings)
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
    agent.recover()
    if before is not None:
        before(agent)
    with unbraked() if not brake else nullcontext():
        ends = [agent.run_cycle("schedule") for _ in range(cycles)]
    invalid = [t for t in fake.trace if t[1] == "invalid"]
    assert invalid == [], invalid
    return agent, ends


@contextmanager
def unbraked() -> Iterator[None]:
    """0.37.6: cycles without the plan's brake (a step a cycle took with nothing moving waits until tomorrow; only steps
    a cycle can advance cut the sleep), as until 0.37.5. For tests that build their state with the fake model's founder
    routine, which works on its line by its cycle's number rather than doing what YOUR STEP asks: under the brake its
    line's step waits after the first cycle that didn't advance it (the fourth cycle's listing, then, has no line to
    belong to). The brake's own tests run consecutive cycles with it (test_fixes_0376)."""

    def busy(conn: Any, scope: Any, steered: plan_tree.Steer, now: str, cycle_id: int | None = None) -> bool:
        return bool(steered.pick.ranked)  # 0.35.1: any step ready cuts the sleep

    with (
        mock.patch.object(plan_tree, "_stale", lambda facts, row, picks: None),
        mock.patch.object(plan_tree, "busy", busy),
    ):
        yield


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
    overloaded = Fail(Rejected(529, "overloaded_error | busy"))
    fake = FakeTransport(script=[Plan(PLAN), overloaded, overloaded, JOURNAL])  # the free failure is retried once
    agent, ends = run(data_dir, fake)
    with agent.db.connection() as conn:
        purposes = [r[0] for r in conn.execute("SELECT purpose FROM llm_calls ORDER BY id")]
        journal = conn.execute("SELECT author, summary FROM journal").fetchall()
    assert purposes == ["plan", "work", "work"]  # nothing ran, so there is nothing to reflect on
    assert ends[0].status == "failed" and ends[0].note and "busy" in ends[0].note
    assert [tuple(r) for r in journal] == [("system", f"Cycle ended failed: {ends[0].note}")]


def test_a_request_the_api_rejects_is_not_sent_again(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 0.10.1: a search limited to a site that blocks Anthropic's web tools was sent twice; it can never pass as it is.
    monkeypatch.setattr("app.agent.loop.RETRY_DELAY_SECONDS", 0)
    refused = Fail(Rejected(400, "invalid_request_error | nope"))
    agent, ends = run(data_dir, FakeTransport(script=[Plan(PLAN), refused, JOURNAL]))
    with agent.db.connection() as conn:
        purposes = [r[0] for r in conn.execute("SELECT purpose FROM llm_calls ORDER BY id")]
    assert purposes == ["plan", "work"]
    assert ends[0].status == "failed" and ends[0].note and "nope" in ends[0].note


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
