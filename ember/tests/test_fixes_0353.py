"""0.35.3: the plan tree puts a live product's first buyers before the critic's suggestions.

Live on 2026-10-09 the owner's first order for the week was pins for every live listing, and Ember's own review said
the same: nobody saw the products. Yet no cycle pinned: every critic verdict that said improve (scores 4 to 6) counted
as a defect, so six products' polish weighed about 7 while their launch pins weighed under 1.5, and Pinterest's factor
sat at its floor because four pins, hours old, had no click yet. A missed views bar, said to put the product's
marketing first, added no more than one of those suggestions. Now a defect the critic found (a low score) comes first,
then a missed bar's marketing, then a live product's launch marketing, then the critic's suggestions, and a channel is
judged only on pins and posts live long enough to have results.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402

from app import diagnostics  # noqa: E402
from app.agent import plan, quality, store, weights  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.config import LoadedSettings  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_diagnostics import section  # noqa: E402
from tests.test_etsy import started  # noqa: E402
from tests.test_fixes_0280 import cycle, now  # noqa: E402
from tests.test_fixes_0340 import ALL, keep  # noqa: E402
from tests.test_fixes_0350 import decide, steered  # noqa: E402
from tests.test_fixes_0351 import promise  # noqa: E402
from tests.test_ventures import VENTURING  # noqa: E402
from tests.test_workshop import result, workshop_cycle  # noqa: E402

PINS = "Pin it twice, with different pictures"
CRITIC = "The critic passes it"

# --- the weights: the cycles of 2026-10-09 ---


def _october_9() -> list[weights.Step]:
    """The live products of 2026-10-09 (worth 2, ready a day): each with its launch pins and the critic's suggestions
    (product 5's pins were done), and product 8, which missed its day-7 views bar, with a Bluesky post too."""
    steps = []
    for product, pins, critic in ((3, 12, 14), (4, 26, 29), (6, 56, 59), (7, 71, 74), (8, 87, 89)):
        urgency = weights.MISSED_BAR if product == 8 else weights.REACH
        steps.append(weights.Step(pins, product, f"pins #{product}", "market", 2.0, urgency=urgency, age_days=1.2))
        steps.append(weights.Step(critic, product, f"critic #{product}", "fix", 2.0, urgency=weights.IMPROVE))
    steps.append(weights.Step(88, 8, "post #8", "market", 2.0, channel=0.74, urgency=weights.MISSED_BAR))
    steps.append(weights.Step(44, 5, "critic #5", "fix", 2.0, urgency=weights.IMPROVE))
    return steps


def test_october_9_every_live_products_first_pins_come_before_the_critics_suggestions() -> None:
    left, last, streak, taken = _october_9(), None, 0, []
    while left:
        pick = weights.choose(left, last, streak)
        assert pick.step is not None
        taken.append(pick.step.title)
        streak = streak + 1 if pick.step.product == last else 1
        last = pick.step.product
        left = [s for s in left if s.id != pick.step.id]
    first_critic = next(i for i, title in enumerate(taken) if title.startswith("critic"))
    assert taken[:2] == ["pins #8", "post #8"]  # the missed bar's marketing first
    assert sorted(taken[:first_critic]) == ["pins #3", "pins #4", "pins #6", "pins #7", "pins #8", "post #8"]


def test_a_defect_the_critic_found_comes_before_the_products_marketing() -> None:
    defect = weights.Step(1, 3, "critic", "fix", 2.0, urgency=weights.DEFECT)
    pins = weights.Step(2, 3, "pins", "market", 2.0, urgency=weights.MISSED_BAR, age_days=1)
    others = weights.Step(3, 4, "pins of another", "market", 2.0, urgency=weights.REACH, age_days=1)
    assert weights.choose([pins, others, defect]).step == defect
    assert weights.DEFECT > weights.MISSED_BAR > weights.REACH > weights.IMPROVE


# --- the tree: the urgencies Ember's code reads from the records ---


def launched(data_dir: Path) -> tuple[Agent, int, int]:
    """A product line whose first listing is live, its stages before launch done: (agent, line, listing)."""
    agent, line = started(data_dir)
    [listing] = [int(r["listing_id"]) for r in rows(agent, "SELECT listing_id FROM etsy_listings")]
    with agent.db.transaction() as conn:
        for stage in plan.nodes(conn, agent.scope(), "project_id = ? AND level = 'stage'", (line,)):
            if stage["stage"] in ("research", "create", "release"):
                for step in plan.nodes(conn, agent.scope(), "parent_id = ? AND status = 'open'", (stage["id"],)):
                    plan._close(conn, step["id"], now(agent), "done", "test", by="owner")
                plan._close(conn, stage["id"], now(agent), "done", "test", by="owner")
    keep(agent)
    return agent, line, listing


def urgencies(agent: Agent, line: int) -> dict[str, float]:
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    return {c.step.title: c.step.urgency for c in found if c.step.product == line and c.waiting is None}


def critic(agent: Agent, line: int, listing: int, score: int) -> None:
    verdict = "pass" if score >= quality.MIN_PASS else "improve"
    answer = {"score": score, "verdict": verdict, "fixes": "Retitle it; show the filled sheet on the cover."}
    agent.clock.advance(minutes=1)
    with agent.db.transaction() as conn:
        quality.save(conn, agent.scope(), line, None, answer, None, now(agent), listing)


def test_a_live_products_launch_pins_come_before_the_critics_suggestions_and_after_a_defect(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    assert urgencies(agent, line)[PINS] == weights.REACH  # live: its first buyers
    critic(agent, line, listing, 5)
    found = urgencies(agent, line)
    assert (found[PINS], found[CRITIC]) == (weights.REACH, weights.IMPROVE)  # 0.35.2: DEFECT, the pins 0
    assert steered(agent).step.title == PINS
    critic(agent, line, listing, 3)  # a defect: fixed before more buyers see the listing
    assert urgencies(agent, line)[CRITIC] == weights.DEFECT
    assert steered(agent).step.title == CRITIC
    critic(agent, line, listing, 8)  # it passes: the step closes at the next keep
    keep(agent)
    assert CRITIC not in urgencies(agent, line)


def test_a_missed_views_bar_puts_the_products_marketing_above_its_launch_and_its_suggestions(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    critic(agent, line, listing, 5)
    decide(agent, line, 7)  # day 7 with no view: its marketing comes first for a week
    found = urgencies(agent, line)
    assert (found[PINS], found[CRITIC]) == (weights.MISSED_BAR, weights.IMPROVE)


# --- a channel is judged on results old enough to exist ---


def approved(agent: Agent, line: int, executor: str) -> int:
    """A request of line ``line`` for ``executor`` (each with its own payload: no two requests are the same)."""
    scope = agent.scope()
    with agent.db.transaction() as conn:
        made = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        return store.insert_approval(
            conn,
            scope,
            1,
            now(agent),
            project_id=line,
            type="publish",
            title="A request",
            description="for the owner",
            payload=json.dumps({"n": made}),
            expected_cost="nothing",
            expected_benefit="buyers",
            executor=executor,
            action="{}",
        )


def pin(agent: Agent, line: int, days_live: float, clicks: int = 0) -> None:
    approval = approved(agent, line, "pinterest_pin")
    live = to_iso(agent.clock.now() - timedelta(days=days_live))
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO pinterest_pins (mode, session, approval_id, title, link, status, started_at, finished_at,"
            " clicks) VALUES (?, ?, ?, 'A pin', 'https://www.etsy.com/listing/1', 'active', ?, ?, ?)",
            (scope.mode, scope.session, approval, live, live, clicks),
        )


def post(agent: Agent, line: int, days_live: float, likes: int = 0) -> None:
    approval = approved(agent, line, "bluesky_post")
    live = to_iso(agent.clock.now() - timedelta(days=days_live))
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bluesky_posts (mode, session, approval_id, text, status, sent, started_at, finished_at,"
            " likes) VALUES (?, ?, ?, 'A post', 'active', 1, ?, ?, ?)",
            (scope.mode, scope.session, approval, live, live, likes),
        )


def factors(agent: Agent) -> dict[str, Any]:
    with agent.db.connection() as conn:
        return plan._channel_factors(conn, agent.scope(), now(agent))


def test_pins_and_posts_too_new_to_have_results_dont_judge_their_channel(data_dir: Path) -> None:
    agent, line, _ = launched(data_dir)
    for _ in range(plan.FRESH_PINS):
        pin(agent, line, 0.2)  # live: four pins hours old, no click yet
    for _ in range(plan.FRESH_POSTS):
        post(agent, line, 1)
    assert (factors(agent)["pinterest"], factors(agent)["bluesky"]) == (1.0, 1.0)  # 0.35.2: 0.2 and 0.2
    agent.clock.advance(days=plan.RESULT_DAYS)  # a week on, still nothing: now it says so
    assert (factors(agent)["pinterest"], factors(agent)["bluesky"]) == (weights.CHANNEL_MIN, weights.CHANNEL_MIN)
    for _ in range(plan.FRESH_PINS):
        pin(agent, line, plan.RESULT_DAYS + 1, clicks=1)  # eight seasoned pins, four clicks: half a click each
    assert factors(agent)["pinterest"] == round(4 / 8 / plan.CLICKS_PER_PIN, 2)


# --- the report: one answer for the next cycle ---


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_the_reports_scheduler_planner_and_plan_tree_name_the_same_next_cycle(
    client_factory: Callable[..., Iterator[TestClient]],
) -> None:
    """Live, the scheduler said the next cycle was a venture cycle (the ventures' share alone), the planner's preview
    an ordinary one (the owner's decision came first) and the plan tree's ranking "weight" (it never asked whether it
    was the ventures' turn)."""
    with client_factory(LoadedSettings(VENTURING)) as client:
        agent = client.app.state.ember.agent
        keep(agent)  # 0.36.0: the plan lays out its Ventures; their Explore step is its only step
        venture = client.get("api/diagnostics").text
        cycle(agent)  # the cycle the promise was made in
        # 0.37.0: due tomorrow, a step of the plan's Owner project, weighed: heavier than the ventures' steps. Laid out
        # by the next cycle's keeper, which the report runs and takes back (until then the Explore step waited)
        promise(agent, "Send the owner the pin report", days=1)
        ordinary = client.get("api/diagnostics").text
        assert rows(agent, "SELECT COUNT(*) AS n FROM plan_nodes WHERE obligation_id IS NOT NULL") == [{"n": 0}]
    for text, kind, takes in ((venture, "venture", "weight"), (ordinary, "ordinary", "weight")):
        scheduler = json.loads(section(text, "SCHEDULER"))["next_cycle"]
        assert (scheduler["kind"], scheduler["venture"], scheduler["decided"]) == (kind, kind == "venture", takes)
        assert section(text, diagnostics.PLANNER_TITLE).startswith(
            f"\n(the next cycle is {'a' if kind == 'venture' else 'an'} {kind} cycle)\n"
        )
        assert f"-- plan tree: the ranking now (the next cycle takes: {takes})" in section(text, "AGENT RECORDS")


# --- the workshop: a run that named its files and kept none ---


def test_a_run_that_named_its_files_and_kept_none_is_not_told_to_name_them(data_dir: Path) -> None:
    """Live, runs #28 to #33 named every file ("books/v3-preview-page4.png, 900x1350 pixels") and were told to name
    the files they need; the agent reworded its tasks for three cycles."""
    agent, _ = workshop_cycle(data_dir, {}, {"task": "Make a price chart: price-chart.png, 1200 x 800 pixels."})
    answer = result(agent)
    said = "none of the files the task names (price-chart.png): its answer above says what happened."
    assert answer["status"] == "error" and said in answer["result"]
    assert "name the files you need" not in answer["result"]
    [run] = rows(agent, "SELECT status, summary FROM workshop_runs")
    assert run == {"status": "nothing", "summary": "Made the chart."}  # the helper's answer: the report shows it
    records = diagnostics._agent(SimpleNamespace(agent=agent, db=agent.db), full=False)
    shown = records.split("-- workshop_runs\n", 1)[1].split("\n--", 1)[0].splitlines()
    assert shown[0].endswith(" | task | summary") and shown[1].endswith(" | Made the chart.")
