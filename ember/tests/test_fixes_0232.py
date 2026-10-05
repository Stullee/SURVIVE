"""0.23.2: what the owner's park or kill of a venture stops, nothing carries on. A plan could still focus on a project
waiting for the owner, the agent could plan milestones for the parked venture and its projects (an unlock of one
carried their work on), its own milestones of the venture's projects stayed open, and listings and products could
still join a waiting project."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import context, policy, roadmap, stages, store, tools, ventures  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call, listed, shop_context  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner, sent_text  # noqa: E402
from tests.test_ventures import plan  # noqa: E402


def due(agent: Any, days: int = 30) -> str:
    return (agent.clock.today() + timedelta(days=days)).isoformat()


def a_line(agent: Any) -> tuple[int, int, int, int, int]:
    """Ventures A and B, both backed, each with a project; A's project has the agent's milestone and a step leading to
    it. (A, B, A's project, its milestone, the step)"""
    scope, now = agent.scope(), to_iso(agent.clock.now())
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    with agent.db.transaction() as conn:
        a, b = (ventures.create(conn, scope, title=t, pitch="p.", stage="building", now=now) for t in ("A", "B"))
        project = store.create_project(
            conn, scope, cycle_id=cycle, title="Under A", hypothesis="h", next_step="List it", status="active",
            now=now, venture_id=a,
        )  # fmt: skip
        goal = roadmap.create(
            conn, scope, title="Ten sales of A", measure="10 orders", due=due(agent), now=now, project_id=project
        )
        step = roadmap.create(
            conn, scope, title="A first listing", measure="1 listing", due=due(agent, 10), now=now, parent_id=goal
        )
    return a, b, project, goal, step


def status(agent: Any, *ids: int) -> list[tuple[str, str | None]]:
    marks = ", ".join(str(i) for i in ids)
    found = {r["id"]: r for r in rows(agent, f"SELECT id, status, closed_by FROM milestones WHERE id IN ({marks})")}
    return [(found[i]["status"], found[i]["closed_by"]) for i in ids]  # in the order asked


def test_the_owner_s_park_takes_the_milestones_of_its_projects_with_it(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    a, b, _, goal, step = a_line(agent)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:  # B's project gets a milestone too, and Ember's code parks B by its rule
        other = store.create_project(
            conn, agent.scope(), cycle_id=1, title="Under B", hypothesis="h", next_step="n", status="active",
            now=now, venture_id=b,
        )  # fmt: skip
        kept = roadmap.create(conn, agent.scope(), title="Sales of B", measure="3", due=due(agent), now=now,
                              project_id=other)  # fmt: skip
        stages.park(conn, agent.scope(), ventures.get(conn, agent.scope(), b), now, "its first test was missed")
    assert status(agent, kept) == [("open", None)]  # Ember's code's park leaves the agent's own (as before)
    assert owner(agent).decide_venture(a, {"action": "park", "comment": "Not now."}, "Stefan").status == 200
    assert status(agent, goal, step) == [("dropped", "owner"), ("dropped", "owner")]


def test_no_milestone_links_to_a_parked_venture_or_a_project_it_stopped(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    a, b, project, _, _ = a_line(agent)
    assert owner(agent).decide_venture(a, {"action": "park"}, "Stefan").status == 200
    ctx = shop_context(agent)
    item = {"title": "Back to A", "measure": "a sale", "due": due(agent)}
    linked = call(ctx, "milestone_plan", {"milestones": [{**item, "venture_id": a}]})
    assert not linked.ok and (
        f"your owner parked venture #{a}: no milestone is linked to it while it is parked; it is theirs to take up "
        "again" in linked.text
    )
    linked = call(ctx, "milestone_plan", {"milestones": [{**item, "project_id": project}]})
    assert not linked.ok and (
        f"project #{project} belongs to venture #{a}, which your owner parked: its projects wait until they take it "
        "up again" in linked.text
    )
    free = call(ctx, "milestone_plan", {"milestones": [{**item, "title": "A free goal"}]})
    assert free.ok, free.text
    mid = int(rows(agent, "SELECT MAX(id) AS id FROM milestones")[0]["id"])
    ctx = shop_context(agent)
    moved = call(ctx, "milestone_update", {"milestone_id": mid, "venture_id": a})
    assert not moved.ok and "no milestone is linked to it while it is parked" in moved.text
    moved = call(ctx, "milestone_update", {"milestone_id": mid, "project_id": project})
    assert not moved.ok and f"project #{project} belongs to venture #{a}, which your owner parked" in moved.text
    with agent.db.transaction() as conn:
        ventures.update(conn, b, to_iso(agent.clock.now()), stage="parked", parked_by="agent")
    own = call(shop_context(agent), "milestone_plan", {"milestones": [{**item, "venture_id": b}]})
    assert not own.ok and (
        f"you parked venture #{b}: no milestone is linked to it while it is parked; take it up again first" in own.text
    )
    assert rows(agent, f"SELECT venture_id, project_id FROM milestones WHERE id = {mid}") == [
        {"venture_id": None, "project_id": None}
    ]


def test_no_listing_or_product_joins_a_project_the_owner_s_park_stopped(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    a, _, project, _, _ = a_line(agent)
    ctx = shop_context(agent)
    with agent.db.connection() as conn:
        assert tools._product_line(ctx, conn, {"project_id": project}, "listing") == project
    assert owner(agent).decide_venture(a, {"action": "park"}, "Stefan").status == 200
    with agent.db.connection() as conn, pytest.raises(tools.ToolError, match="which your owner parked"):
        tools._product_line(ctx, conn, {"project_id": project}, "listing")
    assert owner(agent).decide_venture(a, {"action": "back", "confirm": True}, "Stefan").status == 200
    with agent.db.connection() as conn:
        assert tools._product_line(ctx, conn, {"project_id": project}, "product") == project


def test_a_plan_can_t_focus_on_a_project_the_owner_s_park_stopped(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1)
    a, _, project, _, _ = a_line(agent)
    assert owner(agent).decide_venture(a, {"action": "park"}, "Stefan").status == 200
    fake.script.append(plan(steps=["list the next product"], project=project))
    before = len(fake.sent)
    agent.run_cycle("schedule")
    cycle = rows(agent, "SELECT id, project_id FROM cycles ORDER BY id DESC LIMIT 1")[0]
    assert cycle["project_id"] != project  # (the fake's work made a project of its own)
    work = next(r for r in list(fake.sent)[before:] if request_kind(r) == "work")
    assert (
        f"No focus project: your plan's #{project} waits, because your owner parked venture #{a}. Work on what "
        "doesn't need it, until they take the venture up again." in sent_text(work)
    )
    assert f"Focus project: #{project}" not in sent_text(work)


def test_milestones_the_owner_s_earlier_parks_left_open_are_dropped_at_the_upgrade(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    a, b, project, goal, step = a_line(agent)
    now = to_iso(agent.clock.now())
    scope = agent.scope()
    with agent.db.transaction() as conn:
        working = store.create_project(
            conn, scope, cycle_id=1, title="Under B", hypothesis="h", next_step="n", status="active", now=now,
            venture_id=b,
        )  # fmt: skip
        kept = roadmap.create(conn, scope, title="Sales of B", measure="3", due=due(agent), now=now, project_id=working)
        # as parks before 0.23.2 left them: the venture parked, its project waiting, the agent's milestones open, a
        # milestone linked to the parked venture after the park (0.23.1 allowed it), and an open step below a done one
        ventures.update(conn, a, now, stage="parked", parked_by="owner")
        conn.execute("UPDATE projects SET status = 'waiting' WHERE venture_id = ?", (a,))
        later = roadmap.create(conn, scope, title="A again", measure="1", due=due(agent), now=now, venture_id=a)
        done = roadmap.create(conn, scope, title="A step done", measure="1", due=due(agent, 5), now=now,
                              parent_id=goal)  # fmt: skip
        conn.execute(
            "UPDATE milestones SET status = 'done', result = 'ok', closed_at = ?, closed_by = 'agent' WHERE id = ?",
            (now, done),
        )
        below = roadmap.create(conn, scope, title="Below the done step", measure="1", due=due(agent, 3), now=now,
                               parent_id=done)  # fmt: skip
        sql = (paths.APP_DIR / "migrations" / "0079_owner_parks_milestones.sql").read_text(encoding="utf-8")
        conn.execute(sql[sql.index("WITH RECURSIVE") :])
    assert status(agent, goal, step, later, done, below, kept) == [
        ("dropped", "owner"),
        ("dropped", "owner"),
        ("dropped", "owner"),
        ("done", "agent"),  # closed ones stay as they were
        ("dropped", "owner"),
        ("open", None),
    ]
    [result] = rows(agent, f"SELECT result FROM milestones WHERE id = {below}")
    assert result["result"].startswith("Dropped at the upgrade to 0.23.2: your owner parked or killed the venture")
    assert project


def test_the_owner_s_park_stops_what_the_unlocks_of_its_milestones_approved_at_once(data_dir: Path) -> None:
    """What an unlock of a milestone the park drops approved and Ember's code hasn't begun waits for the owner, before
    any executor runs (it waited for the next policy run)."""
    from tests.test_fixes_0140_unlock_safety import a_small_cut, status_of, unlock  # noqa: PLC0415
    from tests.test_policy import a_milestone, price_of  # noqa: PLC0415

    agent, listing_id = listed(data_dir)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        a = ventures.create(conn, agent.scope(), title="A", pitch="p.", stage="building", now=now)
        line = conn.execute("SELECT project_id FROM approvals WHERE executor = 'etsy_listing' ORDER BY id").fetchone()
        conn.execute("UPDATE projects SET venture_id = ? WHERE id = ?", (a, line[0]))
    goal = a_milestone(agent)
    unlock(agent, goal, "price_change")
    ran = a_small_cut(agent, goal, listing_id)
    assert agent.execute_approved() == [(ran["id"], "done")]
    waiting = a_small_cut(agent, goal, listing_id)  # approved at once, not carried out yet (the shop's daily limit)
    assert status_of(agent, waiting["id"])["decided_by"] == policy.POLICY_BY
    assert owner(agent).decide_venture(a, {"action": "park", "comment": "Stop."}, "Stefan").status == 200
    assert status(agent, goal) == [("dropped", "owner")]
    after = status_of(agent, waiting["id"])
    assert (after["status"], after["decided_by"]) == ("pending", None)
    assert f"taken back (your owner parked venture #{a})" in after["decision_comment"]
    price = price_of(agent, listing_id)
    agent.clock.advance(hours=25)
    assert agent.execute_approved() == [] and price_of(agent, listing_id) == price


def test_a_goal_the_owner_s_park_dropped_is_planned_again_without_a_move(data_dir: Path) -> None:
    """After the owner takes the venture up again, the agent sets the goal its park dropped again: the owner's drop is
    no move of the agent's (two moves already, and replacing it was refused)."""
    agent, _ = listed(data_dir)
    a, b, project, goal, _ = a_line(agent)
    with agent.db.transaction() as conn:  # the agent moved its date twice, all it may
        for days in (35, 40):
            conn.execute("UPDATE milestones SET due = ?, moves = moves + 1 WHERE id = ?", (due(agent, days), goal))
    assert owner(agent).decide_venture(a, {"action": "park"}, "Stefan").status == 200
    assert owner(agent).decide_venture(a, {"action": "back", "confirm": True}, "Stefan").status == 200
    again = {"title": "Ten sales of A", "measure": "10 orders", "due": due(agent, 45), "replaces": goal}
    made = call(shop_context(agent), "milestone_plan", {"milestones": [again]})
    assert made.ok, made.text
    [row] = rows(agent, f"SELECT moves, project_id FROM milestones WHERE replaces_id = {goal}")
    assert row == {"moves": 2, "project_id": project}  # its link to the project, working again, is kept


def test_a_replacement_doesn_t_inherit_a_link_the_park_stopped(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    a, b, project, goal, _ = a_line(agent)
    assert owner(agent).decide_venture(a, {"action": "park"}, "Stefan").status == 200
    elsewhere = {"title": "Ten sales of A", "measure": "10 orders", "due": due(agent), "replaces": goal}
    made = call(shop_context(agent), "milestone_plan", {"milestones": [{**elsewhere, "venture_id": b}]})
    assert made.ok, made.text  # it was refused for the project it would have inherited, which it never named
    [row] = rows(agent, f"SELECT venture_id, project_id FROM milestones WHERE replaces_id = {goal}")
    assert row == {"venture_id": b, "project_id": None}
    assert project


def test_the_set_aside_focus_doesn_t_cut_the_venture_s_pitch() -> None:
    from tests.test_owner_news import section, snapshot_with  # noqa: PLC0415

    row = {
        "id": 4, "parent_id": 1, "stage": "researching", "title": "T" * 80, "pitch": "p " * 200,
        "next_question": "q " * 200, "notes": "n " * 200, **{name: "c " * 200 for name, _, _ in ventures.CASE},
        **{score.name: 3 for score in ventures.SCORES}, "scores_by": "research", "owner_action": "note",
        "owner_comment": "o " * 200, "owner_at": "2026-09-29T08:00:00Z", "created_by": "agent",
        "created_at": "2026-09-27T08:00:00Z", "researched": 3, "research_from": "2026-09-27T08:00:00Z",
    }  # fmt: skip
    text = ventures.focus_text(
        row,  # type: ignore[arg-type]
        ventures.Money(),
        2_300,
        [],
        ["ventures/4-t.md"],
        evidence="Evidence: 9 claims (3 independent, 3 marketing, 3 unchecked); the newest: " + "e " * 200,
        numbers="Numbers (case #7, 2026-09-29): " + "n " * 150,
        knocked="Knock-outs (Ember's code; it isn't proposed while one stands): " + "k " * 300,
        critic=("Critic (a separate call on case #7): test; fatal flaw: " + "f " * 150, "Critic's numbers: x"),
    )
    text = f"Decision desk: you took appraise #4: knocked out: fix its case (cash, slow)\n{text}"
    note = "No focus project: your plan's #123 waits, because your owner parked venture #45."
    brief, _ = context.brief(
        snapshot_with([], []), False, {"goal": "g", "steps": ["s"]}, None, 12, venture_focus=text, set_aside=note
    )
    focus = section(brief, "FOCUS") or ""
    assert focus.startswith(note) and "Pitch: " in focus
