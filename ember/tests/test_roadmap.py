"""The roadmap (0.11.0): milestones the agent plans ahead with, keeps honest, and aims every cycle at. 0.35.0: the plan
tree takes the roadmap's place (agent/plan.py, YOUR PLAN): Ember lays out no milestones of her own any more
(milestone_plan retired), and keeps the ones still open (a venture's first test, the owner's own) honest."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import news, prompts, review, roadmap, tools
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls, request_kind
from app.agent.service import Agent
from app.config import Settings
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_api import post
from tests.test_owner_loop import owner

TODAY = date(2026, 9, 1)  # the test clock's day, a Tuesday
NOW = "2026-09-01T10:00:00Z"
JOURNAL = ToolCalls([("write_journal", {"summary": "Planned ahead", "entry": "Laid out my roadmap."})])


@pytest.fixture(autouse=True)
def no_money_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests lay out a roadmap from empty: the money goal Ember's code keeps at its root (0.12.0) stays out of
    them; tests/test_money_goal.py has it."""
    monkeypatch.setattr(roadmap, "keep_money_goal", lambda *args, **kwargs: [])


def day(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def plan(steps: list[str] | None = None, milestone: int | None = None) -> Plan:
    return Plan(
        {
            "assessment": "ok",
            "goal": "Plan ahead",
            "money_path": "A roadmap that leads to income",
            "focus_project_id": None,
            "focus_venture_id": None,
            "focus_milestone_id": milestone,
            "steps": ["lay out my roadmap"] if steps is None else steps,
            "sleep_minutes": 120,
        }
    )


def tool_results(agent: Agent, tool: str) -> list[dict[str, Any]]:
    return rows(agent, f"SELECT status, summary, result FROM tool_calls WHERE tool = '{tool}' ORDER BY id")


def milestone(agent: Agent, milestone_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM milestones WHERE id = {milestone_id}")[0]


def planner_texts(fake: FakeTransport) -> list[str]:
    return [r["messages"][0]["content"][0]["text"] for r in fake.sent if request_kind(r) == "plan"]


def section(text: str, title: str) -> str:
    return text.split(f"== {title} ==\n", 1)[1].split("\n\n== ", 1)[0]


def create(agent: Agent, **fields: Any) -> int:
    with agent.db.transaction() as conn:
        return roadmap.create(conn, agent.scope(), now=NOW, **fields)


def row(**fields: Any) -> dict[str, Any]:
    """A milestone as a row, for the text functions."""
    base = {
        "id": 1,
        "parent_id": None,
        "venture_id": None,
        "project_id": None,
        "title": "Goal",
        "measure": "Measure",
        "first_due": day(40),
        "due": day(40),
        "moves": 0,
        "status": "open",
        "result": "",
        "closed_at": None,
        "closed_by": None,
        "notes": "",
        "created_by": "agent",
        "owner_action": None,
        "owner_comment": None,
        "proposed_due": None,
    }
    return {**base, **fields}


# --- dates ---


@pytest.mark.parametrize(
    ("days", "kind", "said"),
    [
        (-2, "overdue", "2 days late"),
        (-1, "overdue", "1 day late"),
        (0, "week", "today"),
        (1, "week", "tomorrow"),
        (6, "week", "in 6 days"),
        (7, "month", "in 7 days"),
        (30, "month", "in 30 days"),
        (31, "quarter", "in 31 days"),
        (91, "quarter", "in 91 days"),
        (92, "later", "in 92 days"),
    ],
)
def test_a_milestone_falls_in_a_horizon_by_the_days_until_it_is_due(days: int, kind: str, said: str) -> None:
    due = TODAY + timedelta(days=days)
    assert roadmap.horizon(due, TODAY) == kind and roadmap.when(due, TODAY) == said


def test_only_real_dates_are_read() -> None:
    assert roadmap.parse_day(" 2026-10-01 ") == date(2026, 10, 1)
    for text in ("2026-13-01", "2026-02-30", "1 October", "2026-10-1", None, 20261001):
        assert roadmap.parse_day(text) is None


# --- the tools ---


def test_the_agent_cant_drop_a_milestone_its_owner_added(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(),
            ToolCalls(
                [
                    ("milestone_update", {"milestone_id": 1, "status": "dropped", "result": "Not mine."}),
                    ("milestone_update", {"milestone_id": 1, "status": "missed", "result": "No time; next week."}),
                ]
            ),
            Reply("Done."),
            JOURNAL,
        ]
    )

    def owners(agent: Agent) -> None:
        assert (
            owner(agent).add_milestone({"title": "Pinterest live", "measure": "10 pins", "due": day(9)}, None).status
            == 201
        )

    agent, _ = run(data_dir, fake, before=owners)
    dropped, missed = tool_results(agent, "milestone_update")
    assert dropped["status"] == "error"
    assert "only they can drop it. Ask them (message_owner), or propose a new date" in dropped["result"]
    # 0.12.0 (FIX NOW 7): not "missed" before its date either (the error suggested exactly that way out).
    assert (
        missed["status"] == "error"
        and (
            "milestone #1 is due 2026-09-10 (in 9 days): it is missed only once that day has passed. Until then, reach "
            "it, or propose a new date (due, with why in note), or ask your owner to drop it"
        )
        in missed["result"]
    )
    assert milestone(agent, 1)["status"] == "open"


def test_what_a_milestone_promised_and_how_it_ended_are_final(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=0)
    mid = create(agent, title="First sale", measure="Revenue recorded", due=day(5))
    with agent.db.connection() as conn:
        for sql in (
            "UPDATE milestones SET title = 'Other'",
            "UPDATE milestones SET measure = 'Other'",
            "UPDATE milestones SET first_due = '2026-09-09'",
            "UPDATE milestones SET status = 'done'",  # closed needs closed_at
            "UPDATE milestones SET due = 'soon'",
            "DELETE FROM milestones",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    with agent.db.transaction() as conn:
        roadmap.update(conn, mid, NOW, status="done", result="Sold one.", closed_at=NOW, closed_by="agent")
    with agent.db.connection() as conn:
        for sql in (
            "UPDATE milestones SET status = 'open', closed_at = NULL",
            "UPDATE milestones SET due = '2026-09-09'",
            "UPDATE milestones SET result = 'Sold two.'",
            "UPDATE milestones SET parent_id = NULL, venture_id = 1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    with agent.db.transaction() as conn:  # notes and the owner's word stay open
        roadmap.update(conn, mid, NOW, notes="A second sale came later.")
        roadmap.owner_word(conn, mid, NOW, "note", "Well done", "Stefan")
    assert milestone(agent, mid)["owner_version"] == 1


# --- planning ---


def test_a_cycle_aims_at_a_milestone_and_the_brief_says_what_done_means(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(steps=[]),
            plan(steps=["work toward it"], milestone=2),
            Reply("Done."),
            JOURNAL,
            plan(steps=["again"], milestone=99),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, cycles=0)
    agent.run_cycle("schedule")  # no goal, no product, no milestone: YOUR PLAN says so
    first = section(planner_texts(fake)[0], "YOUR PLAN")
    assert first == "Today: Tuesday 2026-09-01.\nYour plan has no products yet: your first one starts it."
    goal = create(agent, title="Two legs", measure="30 EUR a month", due=day(60))
    create(agent, title="First sale", measure="Owner records revenue", due=day(3), parent_id=goal)
    agent.run_cycle("schedule")
    agent.run_cycle("schedule")
    assert rows(agent, "SELECT milestone_id FROM cycles ORDER BY id") == [
        {"milestone_id": None},
        {"milestone_id": 2},
        {"milestone_id": None},  # no such milestone: the plan's focus is dropped
    ]
    assert json.loads(rows(agent, "SELECT plan FROM cycles WHERE id = 3")[0]["plan"])["focus_milestone_id"] is None
    still = section(planner_texts(fake)[1], "YOUR PLAN")
    assert '\nMilestones still open:\n#2 "First sale" · due Fri 2026-09-04 (in 3 days)' in still
    brief = next(r for r in fake.sent if request_kind(r) == "work")["messages"][0]["content"][0]["text"]
    focus = section(brief, "FOCUS").split("\n")
    assert focus[:4] == [
        'Focus milestone: #2 "First sale" [open] · due Fri 2026-09-04 (in 3 days)',
        'Measure of done: "Owner records revenue"',
        "Measure met: close it done, with the evidence. Out of reach by its date: move it (why; twice at most, and "
        "your owner decides on theirs), or close it missed once the date has passed.",
        'Leads to: #1 "Two legs" (due 2026-10-31, open) · 0% (0 of 1 step done, on pace)',  # 0.29.0: how far it got
    ]


def test_the_rules_point_at_the_plan_and_milestone_plan_is_gone() -> None:
    assert "focus_milestone_id" in prompts.PLAN_SCHEMA["required"]
    assert "YOUR PLAN is the tree under your owner's goal" in prompts.OPERATING_RULES
    assert "change its steps (plan_step)" in prompts.PLANNER_RULES and "roadmap" not in prompts.PLANNER_RULES
    assert "roadmap" in prompts.REVIEW_SCHEMA["required"] and "Check your roadmap" in prompts.REVIEW_RULES
    assert "your plan" in prompts.reflect_prompt()
    assert tools.SPECS["milestone_update"].reflect and "milestone_plan" not in tools.SPECS
    assert {"milestone_update", "plan_step"} <= {d["name"] for d in tools.definitions()}


# --- the owner's word ---


def test_the_owner_adds_notes_and_drops_milestones_and_the_agent_hears_it(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=[])])
    agent, _ = run(data_dir, fake, cycles=0)
    who = owner(agent)
    goal = {"title": "Pinterest live", "measure": "10 pins linking to the shop", "due": day(14)}
    added = who.add_milestone(goal, "Stefan")
    assert added.status == 201 and added.body == {"id": 1}
    step = {"title": "Account made", "measure": "Profile done", "due": day(3), "parent_id": 1}
    assert who.add_milestone(step, "Stefan").body == {"id": 2}
    assert who.add_milestone({**goal, "title": "pinterest  LIVE"}, None).status == 409
    for wrong, field in (
        ({**goal, "title": "Other", "due": "2026-13-01"}, "due"),
        ({**goal, "title": "Other", "due": day(-1)}, "due"),
        ({**goal, "title": "Other", "due": day(400)}, "due"),
        ({**goal, "title": "Other", "due": day(20), "parent_id": 1}, "due"),  # after the milestone it leads to
        ({**goal, "title": "Two\nlines"}, "title"),
        ({"title": "Other", "due": day(3)}, "measure"),
        ({**goal, "title": "Other", "owner": "me"}, "owner"),
    ):
        assert who.add_milestone(wrong, None).body["field"] == field, wrong
    assert who.add_milestone({**goal, "title": "Other", "parent_id": 99}, None).status == 404
    noted = who.decide_milestone(2, {"action": "note", "comment": "I made the account"}, "Stefan")
    assert noted.status == 200 and noted.body == {"id": 2, "status": "open"}
    assert who.decide_milestone(2, {"action": "note"}, None).body["field"] == "comment"
    dropped = who.decide_milestone(1, {"action": "drop", "comment": "Not now"}, "Stefan")
    assert dropped.body == {"id": 1, "status": "dropped", "dropped_with": [2]}  # 0.12.0: its open steps with it
    assert who.decide_milestone(1, {"action": "drop"}, None).status == 409
    assert who.decide_milestone(99, {"action": "note", "comment": "x"}, None).status == 404
    assert who.decide_milestone(2, {"action": "note", "comment": "x", "expected_version": 9}, None).status == 409
    assert who.decide_milestone(2, {"action": "dance"}, None).body["field"] == "action"
    assert who.decide_milestone(2, {"action": "note", "comment": "Still, well done"}, None).status == 200
    gone = milestone(agent, 1)
    assert (gone["status"], gone["result"], gone["closed_cycle_id"]) == (
        "dropped",
        "Dropped by your owner: Not now",
        None,
    )
    assert (gone["owner_version"], gone["owner_by"], gone["created_by"]) == (2, "Stefan", "owner")
    step = milestone(agent, 2)
    assert (step["status"], step["result"], step["closed_by"]) == (
        "dropped",
        "Dropped by your owner with #1: Not now",
        "owner",
    )

    agent.run_cycle("schedule")
    text = planner_texts(fake)[0]
    assert (
        'Your owner dropped milestone #1 "Pinterest live": stop working toward it. Owner\'s comment: "Not now".' in text
    )
    assert 'Your owner wrote a note on milestone #2 "Account made". Owner\'s comment: "Still, well done".' in text
    assert "Milestones still open" not in section(text, "YOUR PLAN")  # both dropped
    assert rows(agent, "SELECT id, seen_cycle_id FROM milestones ORDER BY id") == [
        {"id": 1, "seen_cycle_id": 1},
        {"id": 2, "seen_cycle_id": 1},
    ]
    agent.run_cycle("schedule")
    assert "Your owner dropped milestone" not in planner_texts(fake)[1]  # news once


def test_a_milestone_the_owner_adds_is_news_until_a_plan_shows_it() -> None:
    added = row(id=7, title="Pinterest live", measure="10 pins", due=day(14), parent_id=3, owner_action="added")
    assert roadmap.news_line(added) == (
        'Your owner put milestone #7 "Pinterest live" on your roadmap, leading to #3, due 2026-09-15: done when '
        '"10 pins". Plan toward it.'
    )
    shown = news.News(milestones=[{**added, "owner_version": 1}])  # type: ignore[list-item]
    assert shown.items() == [("milestone", 7, 1)] and shown.venture_lines() == [roadmap.news_line(added)]


# --- the review ---


def test_the_review_reads_the_roadmap(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))  # an idle cycle for the review to belong to
    scope = agent.scope()
    assert "Your roadmap is empty" in _review_text(agent)
    goal = create(agent, title="Two legs", measure="30 EUR a month", due=day(60))
    create(agent, title="Late one", measure="x", due=day(-2), parent_id=goal)
    create(agent, title="This week", measure="y", due=day(4), parent_id=goal)
    done = create(agent, title="Photos", measure="z", due=day(-5))
    with agent.db.transaction() as conn:
        roadmap.update(conn, done, NOW, status="done", result="Five photos made.", closed_at=NOW, closed_by="agent")
    create(agent, title="This month", measure="m", due=day(20), parent_id=goal)  # 0.19.2: it was left out
    assert _review_text(agent).split("\n") == [
        "ROADMAP (4 open, 1 overdue; in the period: 1 done)",
        '- overdue: #2 "Late one" (2026-08-30)',
        '- due in the next 7 days: #3 "This week" (2026-09-05)',
        '- due later this month: #5 "This month" (2026-09-21)',
        '- planned beyond this month: #1 "Two legs" (2026-10-31)',
        '- closed in the period: #4 "Photos" done (self-reported): "Five photos made."',  # 0.12.0: its own word
    ]
    parsed = review.parse(json.dumps({"verdicts": [], "roadmap": "Close #2 honestly."}), set())
    assert parsed is not None and parsed.roadmap == "Close #2 honestly."
    with agent.db.transaction() as conn:
        card = review.Scorecard("card")
        review.save(conn, scope, 1, NOW, TODAY, card, parsed)
        saved = conn.execute("SELECT * FROM reviews").fetchone()
        assert saved["roadmap"] == "Close #2 honestly."
        assert "\nRoadmap: Close #2 honestly." in review.planner_text(conn, saved)


def _review_text(agent: Agent) -> str:
    with agent.db.connection() as conn:
        return roadmap.review_text(conn, agent.scope(), TODAY, "2026-08-25T00:00:00Z")


def test_the_daily_review_gets_the_roadmap_in_its_scorecard(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    agent, _ = run(data_dir, FakeTransport(seed=5, scenario="founder"), cycles=2, settings=settings)
    goal = create(agent, title="Two legs", measure="30 EUR a month", due=day(84))
    month = create(agent, title="First sale", measure="Owner records revenue", due=day(25), parent_id=goal)
    create(agent, title="Listing ready", measure="Proposed to my owner", due=day(5), parent_id=month)
    agent.clock.advance(days=1)
    agent.run_cycle("schedule")
    made = rows(agent, "SELECT scorecard, roadmap FROM reviews")[0]
    assert "\nROADMAP (3 open, 0 overdue; in the period: none closed)" in made["scorecard"]
    assert made["roadmap"] == "Close what is overdue honestly and keep one small milestone due this week."


# --- the dashboard ---


def test_the_roadmap_tab(ingress_client: TestClient) -> None:
    empty = ingress_client.get("api/roadmap").json()
    assert empty["items"] == [] and empty["total"] == 0 and empty["mode"] == "dry_run"
    assert [h["key"] for h in empty["horizons"]] == ["overdue", "week", "month", "quarter", "later"]
    assert empty["limits"] == {
        "title": 100,
        "measure": 300,
        "comment": 1_000,
        "ahead_days": 366,
        "open": 20,
        "owner_slots": 4,
        "moves": 2,
        "goal_min_days": 7,  # 0.29.0: the owner's goal
        "goal_max_usd": 100_000.0,
        "window_days": 30,
    }
    assert empty["goal"] is None and empty["last_goal"] is None  # 0.29.0: no goal stands before the first plan
    today = date.fromisoformat(empty["today"])
    dashboard = ingress_client.get("api/dashboard").json()
    assert dashboard["roadmap"] == {"stamp": empty["stamp"], "overdue": 0, "proposals": 0, "goal": None}
    due = (today + timedelta(days=10)).isoformat()
    added = post(ingress_client, "api/roadmap", {"title": "Pinterest live", "measure": "10 pins", "due": due})
    assert added.status_code == 201 and added.json() == {"id": 1}
    wrong = post(ingress_client, "api/roadmap", {"title": "Past", "measure": "x", "due": "2020-01-01"})
    assert wrong.status_code == 422 and wrong.json()["field"] == "due"
    noted = post(ingress_client, "api/roadmap/1/decide", {"action": "note", "comment": "Use the shop's colours"})
    assert noted.status_code == 200
    assert post(ingress_client, "api/roadmap/99/decide", {"action": "drop"}).status_code == 404
    [item] = ingress_client.get("api/roadmap").json()["items"]
    assert (item["title"], item["due"], item["first_due"], item["moves"]) == ("Pinterest live", due, due, 0)
    assert (item["horizon"], item["days"], item["status"], item["cycles"], item["spent_usd"]) == (
        "month",
        10,
        "open",
        0,
        0,
    )
    assert (item["owner_action"], item["owner_comment"], item["created_by"]) == (
        "note",
        "Use the shop's colours",
        "owner",
    )
    assert ingress_client.get("api/dashboard").json()["roadmap"]["stamp"] != empty["stamp"]
    html = ingress_client.get("/").text
    # 0.35.0: the milestones are on the Plan tab (the Roadmap tab retired)
    assert 'id="tab-roadmap"' not in html and 'id="rm-card"' in html and 'id="roadmap"' in html


# --- the dry run ---


def test_the_fake_keeps_the_milestones_still_open_honest(data_dir: Path) -> None:
    """0.35.0: the fake lays out no roadmap (milestone_plan retired); it aims at the milestone YOUR PLAN lists due
    first, moves one overdue a week once, then closes it."""
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    fake = FakeTransport(seed=3, scenario="founder")

    def milestones(agent: Agent) -> None:
        goal = create(agent, title="Two legs", measure="30 EUR a month", due=day(84))
        month = create(agent, title="First sale", measure="Owner records revenue", due=day(25), parent_id=goal)
        create(agent, title="Listing ready", measure="Proposed to my owner", due=day(5), parent_id=month)

    agent, ends = run(data_dir, fake, cycles=1, settings=settings, before=milestones)
    assert [e.status for e in ends] == ["completed"]
    assert rows(agent, "SELECT milestone_id FROM cycles ORDER BY id") == [{"milestone_id": 3}]
    agent.clock.advance(days=7)  # the week's milestone is overdue now: moved a week, then closed
    agent.run_cycle("schedule")
    week = milestone(agent, 3)
    assert (week["moves"], week["due"], week["status"]) == (1, day(14), "open")
    agent.clock.advance(days=8)
    agent.run_cycle("schedule")
    assert milestone(agent, 3)["status"] in ("done", "missed")


def test_a_done_needs_its_evidence_and_is_shown_as_the_agents_word(data_dir: Path) -> None:
    """0.12.0 (FIX NOW 6): "3 listings live" was closed with "Done." while no listing existed, and the daily review
    then showed it among YOUR NUMBERS (from Ember's records: exact)."""
    fake = FakeTransport(
        script=[
            plan(),
            ToolCalls(
                [
                    ("milestone_update", {"milestone_id": 1, "status": "done", "result": "Done."}),
                    ("milestone_update", {"milestone_id": 1, "status": "done", "result": "All up, as planned."}),
                    ("milestone_update", {"milestone_id": 1, "status": "done", "result": "3 live: #901, #902, #903"}),
                ]
            ),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(
        data_dir, fake, before=lambda a: create(a, title="3 listings live", measure="3 listings on Etsy", due=day(5))
    )
    bare, vague, done = tool_results(agent, "milestone_update")
    assert bare["status"] == vague["status"] == "error" and done["status"] == "ok"
    assert "a number (3 listings live, 12 views) or a reference (#123, a link or a workspace file)" in bare["result"]
    assert milestone(agent, 1)["closed_by"] == "agent"
    assert '- closed in the period: #1 "3 listings live" done (self-reported): "3 live: #901, #902, #903"' in (
        _review_text(agent)
    )
    [shown] = agent.roadmap()["items"]
    assert shown["closed_by"] == "agent"
    with pytest.raises(sqlite3.IntegrityError, match="who closed a milestone is final"), agent.db.transaction() as c:
        c.execute("UPDATE milestones SET closed_by = 'code'")


def test_what_the_owner_drops_is_theirs_and_every_closed_milestone_names_its_closer(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    assert owner(agent).add_milestone({"title": "Pinterest", "measure": "10 pins", "due": day(9)}, None).status == 201
    assert owner(agent).decide_milestone(1, {"action": "drop"}, "Stefan").status == 200
    assert milestone(agent, 1)["closed_by"] == "owner"
    goal = create(agent, title="Goal", measure="x", due=day(30))
    with pytest.raises(sqlite3.IntegrityError, match="names who closed it"), agent.db.transaction() as conn:
        conn.execute(f"UPDATE milestones SET status = 'done', closed_at = 'now' WHERE id = {goal}")


def test_the_fake_model_never_calls_a_milestone_done() -> None:
    """It can't check a measure; its done "to show the flow" taught the dry run that one sentence closes one. Its one
    done (0.12.0) closes a decision point Ember's code set, with its decision: no measure is claimed there."""
    import inspect

    from app.agent import fake_llm

    source = inspect.getsource(fake_llm)
    assert source.count('"status": "done"') == 1 and "to show the flow" not in source
    decide = source.index('if step[1] == "decide at":')
    assert decide < source.index('"status": "done"') < source.index('if step[1] == "move":')


def test_your_plan_keeps_the_goal_first_at_every_scale() -> None:
    """0.12.0 (FIX NOW 8): with 18 milestones, the ROADMAP's cut took all 3 goals at every budget scale. 0.35.0: YOUR
    PLAN in its place keeps its floor at every scale, the goal first and each product on a line of its own."""
    import dataclasses

    from app.agent import context
    from app.agent.loop import PLANNER_SCALES
    from tests.test_agent_requests import overflowing_snapshot

    snap = overflowing_snapshot()
    goal = 'Your owner\'s goal: #1 "Earn $100 a month" · due Sun 2026-11-15 (in 46 days) · 3%: $3 of $100'
    products = [f"Etsy: #{i} Product {i} (launch, 1 of 4 steps done · {i} views)" for i in range(1, 60)]
    snap = dataclasses.replace(snap, plan="\n".join(["Today: Wednesday 2026-09-30.", goal, *products]))
    for scale in PLANNER_SCALES:
        planner, _ = context.planner_context(snap, dry_run=True, scale=scale)
        shown = section(planner, "YOUR PLAN")
        assert shown.startswith(f"Today: Wednesday 2026-09-30.\n{goal}\nEtsy: #1 Product 1"), scale
        assert len(shown.encode()) >= context.PLAN_FLOOR - 200, scale
