"""0.12.0: money and time on a milestone, and a waiting state. A milestone had no budget, whole cycles were charged to
it (their plans too), the planner never saw its notes, and a milestone waiting on the owner turned overdue and asked
for paid date moves. Now a milestone carries what it may cost (API spending, the owner's cash and hours), the plan
shows what its work spent of that (plans, reviews and brainstorms are overhead), and a milestone can wait: it isn't
flagged overdue until its check is due, and the agent wakes on that morning. 0.35.0: milestone_plan retired (the
agent's own milestones are the plan tree's steps); the owner's and Ember's code's keep their costs and waits."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.agent import roadmap
from app.agent.fake_llm import FakeTransport, Plan, Reply
from app.agent.service import Agent
from tests.roadmap_helpers import set_milestone
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_money_goal import call
from tests.test_roadmap import JOURNAL, create, day, plan


def roadmap_text(agent: Agent, spent: dict[int, int] | None = None) -> str:
    """The open milestones' lines, each in full (YOUR PLAN lists them shorter)."""
    with agent.db.connection() as conn:
        rows_ = roadmap.open_milestones(conn, agent.scope())
        spent = spent if spent is not None else {k: v for k, (_, v) in roadmap.effort(conn, agent.scope()).items()}
    return "\n".join(roadmap.milestone_line(m, agent.clock.today(), True, spent=spent) for m in rows_)


def test_a_milestone_carries_what_it_may_cost(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    step = set_milestone(
        agent, "Planner bundle live", day(10), measure="One bundle listing is live", budget_micros=1_000_000,
        cash_cents=2_000, owner_minutes=90,
    )  # fmt: skip
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="may cost is fixed"):
        conn.execute("UPDATE milestones SET budget_micros = 5000000 WHERE id = ?", (step,))
    text = roadmap_text(agent, spent={step: 1_200_000})
    assert "spent $1.20 of $1.00 · needs 20.00 EUR and 1.5 h from your owner" in text


def test_its_work_is_charged_to_it_and_the_plans_are_overhead(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake)
    step = create(agent, title="First sale", measure="Owner records revenue", due=day(3))
    fake.script.extend([plan(steps=["work toward it"], milestone=step), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    calls = rows(agent, f"SELECT purpose, cost_micros FROM llm_calls WHERE cycle_id = {cycle}")
    work = sum(c["cost_micros"] for c in calls if c["purpose"] in roadmap.WORK_PURPOSES)
    assert work > 0 and any(c["purpose"] == "plan" for c in calls)
    with agent.db.connection() as conn:
        assert roadmap.effort(conn, agent.scope())[step] == (1, work)
        everything = conn.execute("SELECT SUM(cost_micros) FROM llm_calls").fetchone()[0]
        assert roadmap.overhead(conn, agent.scope()) == everything - work  # the plans, and the first cycle's all


def test_a_waiting_milestone_is_not_overdue_until_its_check(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    step = set_milestone(agent, "Bundle approved", day(1), measure="My request approved")
    agent.clock.advance(days=2)
    assert "(1 day late)" in roadmap_text(agent)
    check = (agent.clock.today() + timedelta(days=3)).isoformat()
    for args, error in (
        ({"wait_for": "your owner's decision"}, "a wait needs both"),
        ({"wait_for": "x", "check_at": (agent.clock.today() + timedelta(days=20)).isoformat()}, "at most 14 days"),
        ({"wait_for": "x", "check_at": check, "status": "missed", "result": "Late."}, "not both"),
    ):
        refused = call(agent, "milestone_update", milestone_id=step, **args)
        assert not refused.ok and error in refused.text, refused.text
    waited = call(agent, "milestone_update", milestone_id=step, wait_for="your owner's decision on it", check_at=check)
    assert waited.ok and f"waits for your owner's decision on it until {check}" in waited.text, waited.text
    text = roadmap_text(agent)
    assert f'waiting for "your owner\'s decision on it" until {check}' in text
    assert f'last note: "[#c1] (waits for your owner\'s decision on it until {check})"' in text  # the notes, seen
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="a wait names"):
        conn.execute("UPDATE milestones SET wait_for = NULL WHERE id = ?", (step,))
    agent.clock.advance(days=3)
    assert "its check is due: it waited for" in roadmap_text(agent)
    ended = call(agent, "milestone_update", milestone_id=step, wait_for="nothing")
    assert ended.ok and "no longer waits" in ended.text
    assert rows(agent, f"SELECT wait_for, check_at FROM milestones WHERE id = {step}")[0] == {
        "wait_for": None,
        "check_at": None,
    }
    later = (agent.clock.today() + timedelta(days=5)).isoformat()
    assert call(agent, "milestone_update", milestone_id=step, wait_for="buyers", check_at=later).ok
    closed = call(agent, "milestone_update", milestone_id=step, status="missed", result="Never approved.")
    assert closed.ok and rows(agent, f"SELECT wait_for FROM milestones WHERE id = {step}")[0]["wait_for"] is None


def test_the_agent_wakes_on_the_morning_a_check_is_due(data_dir: Path) -> None:
    long = Plan({**plan(steps=[]).plan, "sleep_minutes": 1_440})  # type: ignore[dict-item]
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[]), long]))
    step = set_milestone(agent, "Buyers came", day(9), measure="1 order")
    tomorrow = agent.clock.today() + timedelta(days=1)
    assert call(agent, "milestone_update", milestone_id=step, wait_for="buyers", check_at=tomorrow.isoformat()).ok
    agent.run_cycle("schedule")  # it chose to sleep a whole day
    morning = agent.clock.day_start(tomorrow) + timedelta(hours=8)
    assert agent._meta_time("next_wake_at") == morning
    assert agent.agent_fields()["next_wake_reason"].endswith(f"; waking for the check of milestone #{step}")
