"""The scripted agent the tests share: a dry-run agent with scripted model replies (make_agent, plan, tools,
text, reply), a cycle run on the fake model (run), the database's rows, and the owner's side (owner)."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from unittest import mock

from app.agent import plan as plan_tree
from app.agent.fake_llm import FakeTransport, ToolCalls
from app.agent.owner import Owner
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.metering import Completed
from tests.economy_helpers import ScriptedTransport, make_economy

_ids = itertools.count(1)


# 0.15.0: an owner named in owner_user_ids, as the owner's unlocks act only then.
ROOMY = Settings(
    starting_balance_usd=50,
    daily_spend_cap_usd=5,
    cycle_spend_cap_usd=1,
    owner_user_ids=("8f14e45fceea167a5a36dedd4bea2543",),
)


def reply(content: list[dict[str, Any]], stop: str, output_tokens: int = 100) -> Completed:
    body = {
        "id": f"msg_{next(_ids)}",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": 900, "output_tokens": output_tokens},
    }
    return Completed(body, f"req_{next(_ids)}")


def plan(steps: list[str] | None = None, focus: int | None = None, sleep: int = 120) -> Completed:
    data = {
        "assessment": "Fresh start.",
        "goal": "Try one idea",
        "focus_project_id": focus,
        "steps": ["look around", "start a project"] if steps is None else steps,
        "sleep_minutes": sleep,
    }
    return reply([{"type": "text", "text": json.dumps(data)}], "end_turn")


def tools(*calls: tuple[str, dict[str, Any]], stop: str = "tool_use") -> Completed:
    return reply([{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": n, "input": i} for n, i in calls], stop)


def text(words: str) -> Completed:
    return reply([{"type": "text", "text": words}], "end_turn")


def make_agent(data_dir: Path, outcomes: list[Any], settings: Settings = ROOMY) -> tuple[Agent, ScriptedTransport]:
    economy = make_economy(data_dir, settings)
    transport = ScriptedTransport(simulated=True, outcomes=outcomes)
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=transport, cycles_enabled=True)
    agent.recover()
    return agent, transport


def check_conversations(sent: list[dict[str, Any]]) -> None:
    """What the real API requires: alternating roles, and every tool_use answered first thing, in order."""
    for request in sent:
        messages = request["messages"]
        roles = [m["role"] for m in messages]
        assert roles[0] == "user" and all(a != b for a, b in itertools.pairwise(roles)), roles
        for before, after in itertools.pairwise(messages):
            if before["role"] != "assistant":
                continue
            uses = [b["id"] for b in before["content"] if b.get("type") == "tool_use"]
            if not uses:
                continue
            results = [b for b in after["content"] if b.get("type") == "tool_result"]
            assert [r["tool_use_id"] for r in results] == uses
            assert after["content"][: len(results)] == results


def rows(agent: Agent, sql: str) -> list[dict[str, Any]]:
    with agent.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


PLAN = {"assessment": "ok", "goal": "Look around", "focus_project_id": None, "steps": ["look"], "sleep_minutes": 120}


JOURNAL = ToolCalls([("write_journal", {"summary": "Looked around", "entry": "Nothing yet."})])


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


APPROVAL = {
    "type": "publish",
    "title": "Post the guide",
    "description": "Publish the guide on my blog.",
    "payload": "Hello world, written by an AI.",
    "expected_cost": "none",
    "expected_benefit": "first readers",
}


def owner(agent: Agent) -> Owner:
    return Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)


def sent_text(request: dict[str, Any]) -> str:
    return json.dumps(request["messages"], ensure_ascii=False)


def cycle_with_approval() -> list[Any]:
    return [
        plan(steps=["ask to publish"]),
        tools(("request_approval", APPROVAL), ("message_owner", {"text": "Please look at request 1."})),
        text("Asked."),
        tools(("write_journal", {"summary": "Asked to publish", "entry": "Waiting."})),
    ]
