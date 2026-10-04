"""0.19.1: no limit on open projects. project_create refused a ninth open project ("you already have 8 open
projects; close one first"), and the owner can't change that in the options. Now the agent opens as many as it needs,
and none drops out of sight: the plan shows the 8 it updated last in full and names every other one first, so a cut
section still lists them, and the daily review lists every open project (the others in a line each) so each can get
a verdict."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent import review, store
from app.agent.fake_llm import FakeTransport
from app.economy.clock import to_iso
from tests.test_agent import rows
from tests.test_etsy import call, shop_context
from tests.test_loop_shapes import run

OPEN = 12


def many_projects(data_dir: Path) -> tuple[Any, list[int]]:
    """An agent with OPEN open projects, the last one made with the tool past the old limit of 8."""
    agent, _ = run(data_dir, FakeTransport(), cycles=1)
    scope = agent.scope()
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    with agent.db.transaction() as conn:
        for n in range(len(store.open_projects(conn, scope)), OPEN - 1):
            agent.clock.advance(minutes=1)
            store.create_project(
                conn,
                scope,
                cycle_id=cycle,
                title=f"Product line {n}",
                hypothesis=f"People buy product {n}.",
                next_step=f"List product {n}",
                status="idea",
                now=to_iso(agent.clock.now()),
            )
    made = call(
        shop_context(agent),
        "project_create",
        {"title": "One more line", "hypothesis": "It sells too.", "next_step": "Draft it", "status": "idea"},
    )
    assert made.ok, made.text
    with agent.db.connection() as conn:
        ids = [int(p["id"]) for p in store.open_projects(conn, scope)]
    assert len(ids) == OPEN
    return agent, ids


def test_the_agent_opens_as_many_projects_as_it_needs(data_dir: Path) -> None:
    agent, ids = many_projects(data_dir)
    from app.agent import tools  # noqa: PLC0415

    assert "At most" not in tools.SPECS["project_create"].description
    same = call(
        shop_context(agent),
        "project_create",
        {"title": "one more LINE", "hypothesis": "x", "next_step": "y", "status": "idea"},
    )
    assert not same.ok and "an open project already has this title" in same.text  # the title check stays


def test_the_plan_names_every_open_project(data_dir: Path) -> None:
    agent, ids = many_projects(data_dir)
    plan = agent.planner_preview()
    projects = plan.split("== OPEN PROJECTS ==\n", 1)[1].split("\n== ", 1)[0]
    first = projects.split("\n", 1)[0]
    assert first.startswith(f"{OPEN} open projects: the 8 you updated last in full below; also open: ")
    for pid in ids[8:]:  # named in the first line, which a cut section keeps
        assert f"#{pid} " in first
    assert f"#{ids[0]} [idea] One more line · next: Draft it" in projects  # the newest in full


def test_the_daily_review_lists_every_open_project(data_dir: Path) -> None:
    agent, ids = many_projects(data_dir)
    agent.clock.advance(days=1)
    with agent.db.connection() as conn:
        card = review.scorecard(
            conn,
            agent.scope(),
            agent.clock,
            agent.economy.books,
            agent.economy.life.scope(),
            agent.economy.life.evaluate(),
            dry_run=True,
        )
    assert card.project_ids == set(ids)
    assert "the first 8 in full, the others in a line each" in card.text
    for pid in ids[8:]:
        assert f"#{pid} [" in card.text
    assert len(card.text) <= review.SCORECARD_MAX
    last = ids[-1]
    answer = {
        "verdicts": [{"project_id": last, "verdict": "stop", "why": "No demand."}],
        "working": "",
        "not_working": "",
        "owner_feedback": "",
        "lesson": "Fewer lines at once.",
        "focus": "The best line.",
    }
    parsed = review.parse(json.dumps(answer), card.project_ids)
    assert parsed is not None and [v.project_id for v in parsed.verdicts] == [last]


def test_project_list_shows_every_open_project_with_its_number(data_dir: Path) -> None:
    """Ember's upgrade request: project_create refused at 8 open projects, and no tool showed their numbers, so it
    couldn't close the stale ones itself (venture #12's first test waited two cycles)."""
    from app.agent import tools  # noqa: PLC0415

    agent, ids = many_projects(data_dir)
    for venture in (False, True):  # a venture cycle starts first-test projects too
        assert "project_list" in {d["name"] for d in tools.definitions(venture=venture)}
    assert tools.SPECS["project_list"].reflect is False  # a read: nothing reads its answer after the reflection
    shown = call(shop_context(agent), "project_list", {})
    assert shown.ok and shown.text.startswith(f"{OPEN} open projects, the one changed last first:\n")
    listed = [line for line in shown.text.split("\n")[1:] if line]
    assert [int(line.split(" ", 1)[0][1:]) for line in listed] == ids
    assert listed[0].startswith(f"#{ids[0]} [idea] One more line · changed ")
    assert listed[0].endswith(" · next: Draft it")
    closed = call(shop_context(agent), "project_update", {"project_id": ids[-1], "status": "abandoned"})
    assert closed.ok, closed.text
    assert f"#{ids[-1]} " not in call(shop_context(agent), "project_list", {}).text
