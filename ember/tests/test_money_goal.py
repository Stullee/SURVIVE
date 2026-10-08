"""0.12.0: the roadmap is never empty. Live, it stayed empty for 39 cycles after 0.11.0. Ember's code now keeps a money
goal at its root (earn at least what you spend, over the last 30 days), with decision points at a quarter and at half
of the runway, and settles it from the books: done once met (the next asks for twice as much), missed after its date.
The agent can't move, drop or close the goal; it closes a decision point with its decision. 0.35.0: the decision
points retired (each product's decide-by dates in the plan tree take their place)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.agent import tools
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from tests.economy_helpers import owner as owner_entry
from tests.roadmap_helpers import led_to_goal, set_milestone
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_roadmap import day, plan


def call(agent: Agent, tool: str, **args: Any) -> tools.Outcome:
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )
    llm_call = rows(agent, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, tool, led_to_goal(ctx, tool, args), f"toolu_{tool}", llm_call, "act")


def milestones(agent: Agent) -> list[dict[str, Any]]:
    return rows(agent, "SELECT id, parent_id, created_by, title, due, status, closed_by FROM milestones ORDER BY id")


def test_the_first_plan_already_has_a_money_goal(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake)
    assert [(m["id"], m["parent_id"], m["created_by"], m["title"], m["due"]) for m in milestones(agent)] == [
        (1, None, "code", "Earn as much as you spend", day(90)),  # 0.35.0: no decision points under it
    ]
    planned = fake.sent[0]["messages"][0]["content"][0]["text"]
    assert (
        "\nThe goal (Ember's code's, until your owner sets theirs): #1 \"Earn as much as you spend\" · due Mon"
        in planned
    )
    assert "Your plan has no products yet: your first one starts it." in planned  # 0.35.0: YOUR PLAN
    events = [e["message"] for e in agent.db.recent_events(limit=20)]
    assert "Ember's code set the money goal #1 (Earn as much as you spend, due 2026-11-30)" in events


def test_the_agent_works_toward_the_goal_but_cant_bend_it(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    for args, error in (
        ({"milestone_id": 1, "due": day(120), "note": "later"}, "Ember's code set the date of milestone #1"),
        ({"milestone_id": 1, "status": "dropped", "result": "Too hard."}, "only your owner drops it"),
        ({"milestone_id": 1, "status": "done", "result": "Earned 5 EUR."}, "Ember's code closes the money goal"),
        ({"milestone_id": 1, "status": "missed", "result": "Not yet."}, "Ember's code closes the money goal"),
    ):
        refused = call(agent, "milestone_update", **args)
        assert not refused.ok and error in refused.text, refused.text
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="closes the money goal"):
        conn.execute(
            "UPDATE milestones SET status = 'done', result = 'x', closed_at = 'now', closed_by = 'agent' WHERE id = 1"
        )


def test_a_met_goal_closes_done_and_the_next_asks_for_more(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=[])])
    agent, _ = run(data_dir, fake)
    set_milestone(agent, "First Etsy sale", day(20), measure="1 order of a listing", parent_id=1)
    owner_entry(agent.economy, "revenue", "5", test_money=True)
    agent.run_cycle("schedule")
    after = milestones(agent)
    assert (after[0]["status"], after[0]["closed_by"]) == ("done", "code")
    result = rows(agent, "SELECT result FROM milestones WHERE id = 1")[0]["result"]
    assert result.startswith("Over the last 30 days: revenue less expenses $5.00, API spending $0.")
    assert (after[1]["title"], after[1]["parent_id"], after[1]["status"]) == ("First Etsy sale", 3, "open")  # moved
    assert (after[2]["id"], after[2]["title"], after[2]["created_by"]) == (3, "Earn twice what you spend", "code")
    assert len(after) == 3  # 0.35.0: no decision points


def test_a_goal_past_its_date_is_missed_and_set_again(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=[])])
    agent, _ = run(data_dir, fake)
    agent.clock.advance(days=91)
    agent.run_cycle("schedule")
    after = milestones(agent)
    assert (after[0]["status"], after[0]["closed_by"]) == ("missed", "code")
    assert after[1]["title"] == "Earn as much as you spend" and after[1]["status"] == "open"  # the same goal again


def test_once_the_owner_drops_the_goal_the_roadmap_is_theirs(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=[])])
    agent, _ = run(data_dir, fake)
    reply = owner(agent).decide_milestone(1, {"action": "drop", "comment": "I set my own goals."}, "Stefan")
    assert reply.status == 200, reply.body
    assert [(m["status"], m["closed_by"]) for m in milestones(agent)] == [("dropped", "owner")]
    agent.run_cycle("schedule")
    assert len(milestones(agent)) == 1  # none set again


def test_the_fake_agent_plans_with_the_money_goal(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=6)
    failed = rows(agent, "SELECT tool, result FROM tool_calls WHERE result LIKE 'Error: the tool failed%'")
    assert failed == []
    assert milestones(agent)[0]["title"] == "Earn as much as you spend"
