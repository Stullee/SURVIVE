"""The daily review in the plan: its change is a step of its line, its stop holds the line until the owner
keeps it, and stopping a line is the owner's decision.

Tests from 0.39.1, 0.39.6 and 0.40.2 (formerly test_fixes_0391.py, test_fixes_0396.py, test_fixes_0402.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from typing import Any  # noqa: E402

from app.agent import (  # noqa: E402
    plan,
    review,
    weights,
)
from app.agent.service import Agent  # noqa: E402
from tests.agent_helpers import rows  # noqa: E402
from tests.learning.test_weekly_look import judged  # noqa: E402
from tests.plan.test_cycle_line import lined, now  # noqa: E402
from tests.plan.test_marketing_order import launched  # noqa: E402
from tests.plan.test_plan_tree import (  # noqa: E402
    ALL,
    TRACKER,
    keep,
    project,
)
from tests.plan.test_promises import promise  # noqa: E402

# --- From 0.39.1 (formerly test_fixes_0391.py) ---


# --- 5. the review's stop ---


def test_a_reviews_stop_is_the_owners_decision(data_dir: Path) -> None:
    """Live, the review told the agent to close line #10 with project_update, which can't since 0.35.0; three cycles
    tried, and the next review said the stop wasn't carried out."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    judged(agent, (tracker, "stop", "demand"))
    day = agent.clock.today().isoformat()[5:]
    said = keep(agent)
    assert (
        f"Plan tree: line #{tracker} has a review's stop (your review of {day} says stop: the numbers say so): your"
        " owner decides on it."
    ) in said
    asked = f"SELECT id, kind, status, title FROM plan_nodes WHERE project_id = {tracker} AND template = 'decide/owner'"
    [decision] = rows(agent, asked)
    assert decision["kind"] == "owner" and decision["title"].startswith(f"You decide: keep line #{tracker} or drop it")
    assert keep(agent) == []  # asked once
    with agent.db.connection() as conn:
        assert plan.owner_decision(conn, agent.scope(), tracker, now(agent)) == "deciding"
        today = review.of_day(conn, agent.scope(), agent.clock.today())
        assert today is not None
        assert "Act on it: a stop is your owner's decision (only they close a line)" in review.planner_text(conn, today)
    agent.clock.advance(days=1)
    with agent.db.connection() as conn:
        last = review._last_review(conn, agent.scope(), agent.clock.today())
    assert f"- #{tracker} stop: the numbers say so → your owner decides on it (their Plan tab)" in last
    with agent.db.transaction() as conn:  # the owner keeps it
        plan.owner_keep(conn, agent.scope(), decision["id"], "one more month", "Stefan", now(agent))
    with agent.db.connection() as conn:
        last = review._last_review(conn, agent.scope(), agent.clock.today())
    assert f"- #{tracker} stop: the numbers say so → your owner kept it" in last
    judged(agent, (tracker, "stop", "demand"))  # the next review says stop again
    assert keep(agent) == [] and len(rows(agent, asked)) == 1  # not within a week of their decision
    agent.clock.advance(days=plan.STOP_ASK_DAYS)
    judged(agent, (tracker, "stop", "demand"))
    assert any(f"line #{tracker} has a review's stop" in s for s in keep(agent))


# --- From 0.39.6 (formerly test_fixes_0396.py) ---


# --- 4. the review's change is a step of its line ---


def test_a_reviews_change_is_a_step_of_its_line_due_today(data_dir: Path) -> None:
    """Live, the reviews of 10-13, 10-14 and 10-15 each decided on a small Etsy Ads test; no request was made."""
    agent, line, _ = launched(data_dir)
    judged(agent, (line, "change", "reach"))
    day = agent.clock.today().isoformat()
    said = keep(agent)
    assert f"Plan tree: line #{line}'s change from today's review is a step of its own." in said
    made = f"SELECT id, kind, status, title, due, check_kind FROM plan_nodes WHERE template = '{plan.REVIEW_CHANGE}'"
    [step] = rows(agent, made)
    assert (step["kind"], step["status"], step["due"], step["check_kind"]) == ("fix", "open", day, "agent")
    assert step["title"] == f"Review {day[5:]}: the numbers say so"
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
        today = review.of_day(conn, agent.scope(), agent.clock.today())
        assert today is not None and "A change is a step of its line's" in review.planner_text(conn, today)
    [candidate] = [c for c in found if c.step.id == step["id"]]
    assert candidate.waiting is None and candidate.step.urgency == weights.RECURRING_DUE
    assert keep(agent) == [] and len(rows(agent, made)) == 1  # once a day
    agent.clock.advance(days=1)
    judged(agent, (line, "change", "reach"))  # the next review decides anew: it takes the open one's place
    keep(agent)
    old, new = rows(agent, made)
    assert (old["status"], new["status"]) == ("dropped", "open")


def test_a_reviews_change_before_the_line_is_live_adds_no_step(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=("Planner",))
    keep(agent)
    judged(agent, (1, "change", "quality"))
    assert not any("change from today's review" in s for s in keep(agent))


# --- From 0.40.2 (formerly test_fixes_0402.py) ---


# --- 2. the review's stop holds its line ---


def held(agent: Agent, line: int) -> dict[str, Any]:
    [product] = rows(
        agent, f"SELECT id, hold_by, hold_reason FROM plan_nodes WHERE level = 'product' AND project_id = {line}"
    )
    return product


def ready(agent: Agent, line: int) -> dict[str, str | None]:
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    return {c.step.title: c.waiting for c in found if c.step.product == line}


def test_a_line_the_review_says_stop_waits_on_hold_until_the_owner_keeps_it(data_dir: Path) -> None:
    """Live on 2026-10-21 two cycles ($0.37) took the monthly blog post of a line whose review had said stop, only to
    hold the line, and each cut the sleep before it to 30 minutes."""
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    keep(agent)
    assert any(waiting is None for waiting in ready(agent, tracker).values())
    judged(agent, (tracker, "stop", "demand"))
    day = agent.clock.today().isoformat()[5:]
    said = keep(agent)
    reason = f"{plan.REVIEW_HOLD} {day} says stop: the numbers say so"
    assert f"Plan tree: line #{tracker} is on hold until your owner keeps or drops it: {reason}" in said
    product = held(agent, tracker)
    assert (product["hold_by"], product["hold_reason"]) == ("agent", reason)
    assert set(ready(agent, tracker).values()) == {"hold"}  # its steps wait, the decision too
    assert rows(
        agent, f"SELECT actor, action FROM plan_changes WHERE node_id = {product['id']} AND action = 'hold'"
    ) == [{"actor": "code", "action": "hold"}]
    assert keep(agent) == []  # once
    with agent.db.connection() as conn:
        today = review.of_day(conn, agent.scope(), agent.clock.today())
        assert today is not None
        assert "until they decide, Ember's code holds the line: its steps wait" in review.planner_text(conn, today)
    [decision] = rows(agent, f"SELECT id FROM plan_nodes WHERE project_id = {tracker} AND template = 'decide/owner'")
    with agent.db.transaction() as conn:  # the owner keeps it: the hold ends with their decision
        plan.owner_keep(conn, agent.scope(), decision["id"], "one more month", "Stefan", now(agent))
    assert held(agent, tracker)["hold_reason"] is None
    assert any(waiting is None for waiting in ready(agent, tracker).values())
    judged(agent, (tracker, "stop", "demand"))  # the next review says stop again, within a week of their decision
    assert keep(agent) == [] and held(agent, tracker)["hold_reason"] is None


def test_a_review_hold_the_agent_lifted_stays_lifted_and_a_line_with_a_promise_isnt_held(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    planner = project(agent, "Wochenplaner")
    keep(agent)
    promise(agent, "Report the planner's first views", days=5, line=planner)
    keep(agent)
    judged(agent, (tracker, "stop", "demand"), (planner, "stop", "demand"))
    said = keep(agent)
    reason = held(agent, tracker)["hold_reason"]
    assert [s for s in said if "is on hold until your owner" in s] == [
        f"Plan tree: line #{tracker} is on hold until your owner keeps or drops it: {reason}"
    ]
    assert held(agent, planner)["hold_reason"] is None  # its work keeps the promise
    with agent.db.transaction() as conn:  # plan_step resume
        plan.resume(conn, agent.scope(), tracker, "The owner asked for one more listing first", None, now(agent))
    agent.clock.advance(minutes=5)
    keep(agent)
    assert held(agent, tracker)["hold_reason"] is None  # not held again for the same review
