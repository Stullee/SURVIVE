"""The owner's say over their milestones, kept by Ember's code (0.12.0, FIX NOW 7).

The agent moved an owner's milestone five times, to a year out, and then closed it "missed" at once (the error that
refused its drop suggested exactly that), and a goal the owner dropped left its steps open, looking like goals of
their own. Now the agent's new date for an owner's milestone is a proposal the owner accepts or rejects, a date moves
twice at most, "missed" waits for the date, a drop takes the open steps with it, and the last 4 open places are the
owner's.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.agent import fake_llm, roadmap, tools
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from app.db import Database, discover_migrations, migrate
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_roadmap import NOW, TODAY, create, day, milestone, no_money_goal, plan, row  # noqa: F401

# no_money_goal: an autouse fixture (the money goal Ember's code keeps stays out of these tests)


def agent_with_a_cycle(data_dir: Path) -> Agent:
    """An agent after one idle cycle (#1), for the tool calls to belong to."""
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    return agent


def call(agent: Agent, tool: str, **args: Any) -> str:
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=1,
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )
    with agent.db.transaction() as conn:
        return tools.HANDLERS[tool](ctx, args, conn).text


def refused(agent: Agent, tool: str, **args: Any) -> str:
    with pytest.raises(tools.ToolError) as caught:
        call(agent, tool, **args)
    return str(caught.value)


def add(agent: Agent, **fields: Any) -> int:
    reply = owner(agent).add_milestone(fields, "Stefan")
    assert reply.status == 201, reply.body
    return int(reply.body["id"])


def decide(agent: Agent, milestone_id: int, **body: Any) -> Any:
    return owner(agent).decide_milestone(milestone_id, body, "Stefan")


def test_the_agents_new_date_for_an_owners_milestone_is_a_proposal_they_decide(data_dir: Path) -> None:
    agent = agent_with_a_cycle(data_dir)
    mid = add(agent, title="Pinterest live", measure="10 pins linking to the shop", due=day(10))
    assert "say in note why the date moves" in refused(agent, "milestone_update", milestone_id=mid, due=day(20))
    why = "Pinterest needs a business account first."
    assert call(agent, "milestone_update", milestone_id=mid, due=day(20), note=why) == (
        "Milestone #1: you proposed moving it to 2026-09-21. It is your owner's milestone, so the date moves only if "
        "they accept; until then it stays due 2026-09-11."
    )
    row = milestone(agent, mid)
    assert (row["due"], row["moves"], row["proposed_due"], row["proposed_note"], row["proposed_cycle_id"]) == (
        day(10),
        0,
        day(20),
        why,
        1,
    )
    assert "you proposed 2026-09-21 already" in refused(
        agent, "milestone_update", milestone_id=mid, due=day(20), note="."
    )
    assert "· you proposed moving it to 2026-09-21 (your owner decides)" in roadmap.milestone_line(row, TODAY, False)
    [shown] = agent.roadmap()["items"]
    assert (shown["proposed_due"], shown["proposed_note"]) == (day(20), why)

    # The owner answers the date they saw, and only a proposal there is.
    assert decide(agent, mid, action="accept").body["field"] == "proposed_due"
    stale = decide(agent, mid, action="accept", proposed_due=day(15))
    assert stale.status == 409 and "proposed another date meanwhile (2026-09-21)" in stale.body["error"]
    accepted = decide(agent, mid, action="accept", proposed_due=day(20), comment="Fair enough")
    assert accepted.status == 200 and accepted.body == {"id": mid, "status": "open", "due": day(20)}
    row = milestone(agent, mid)
    assert (row["due"], row["first_due"], row["moves"], row["proposed_due"], row["owner_action"]) == (
        day(20),
        day(10),
        1,
        None,
        "accept",
    )
    assert roadmap.news_line(row) == (
        'Your owner accepted your proposed date: milestone #1 "Pinterest live" is due 2026-09-21 now. Owner\'s '
        'comment: "Fair enough".'
    )
    assert decide(agent, mid, action="accept", proposed_due=day(20)).status == 409  # nothing proposed now

    call(agent, "milestone_update", milestone_id=mid, due=day(25), note="Pins take longer.")
    kept = decide(agent, mid, action="reject", comment="No, keep it")
    assert kept.status == 200 and milestone(agent, mid)["due"] == day(20)
    assert roadmap.news_line(milestone(agent, mid)) == (
        'Your owner kept the date of milestone #1 "Pinterest live": it stays due 2026-09-21. Owner\'s comment: '
        '"No, keep it".'
    )

    call(agent, "milestone_update", milestone_id=mid, due=day(22), note="Two days for the last pins.")
    assert decide(agent, mid, action="accept", proposed_due=day(22)).status == 200
    assert milestone(agent, mid)["moves"] == 2
    assert refused(agent, "milestone_update", milestone_id=mid, due=day(30), note="More.") == (
        "milestone #1 has moved 2 times (first due 2026-09-11): a date moves 2 times at most. Reach it by "
        "2026-09-23, or ask your owner to drop it; if it isn't reached, close it missed once that day has passed"
    )

    # The database holds the line too: only the owner's accept moves their date, and only they drop it.
    with pytest.raises(sqlite3.IntegrityError, match="only the owner moves"), agent.db.transaction() as conn:
        conn.execute(f"UPDATE milestones SET due = '{day(40)}', moves = moves + 1 WHERE id = {mid}")
    with pytest.raises(sqlite3.IntegrityError, match="only the owner drops"), agent.db.transaction() as conn:
        conn.execute(
            f"UPDATE milestones SET status = 'dropped', closed_at = 'now', closed_by = 'agent' WHERE id = {mid}"
        )


def test_an_accept_checks_the_dates_it_would_break(data_dir: Path) -> None:
    agent = agent_with_a_cycle(data_dir)
    goal = add(agent, title="Pinterest brings buyers", measure="3 orders from Pinterest", due=day(30))
    step = add(agent, title="Pinterest live", measure="10 pins", due=day(10), parent_id=goal)
    call(agent, "milestone_update", milestone_id=step, due=day(25), note="The account takes a while.")
    call(agent, "milestone_update", milestone_id=goal, due=day(20), note="Sooner is possible.")
    assert decide(agent, goal, action="accept", proposed_due=day(20)).status == 200
    late = decide(agent, step, action="accept", proposed_due=day(25))
    assert late.status == 409 and late.body["error"] == "it leads to milestone #1, which is due earlier (2026-09-21)"
    agent.clock.advance(days=26)  # the proposal outlived its date
    assert decide(agent, step, action="accept", proposed_due=day(25)).body["error"] == (
        "the proposed date (2026-09-26) has passed: keep the date instead"
    )
    assert decide(agent, step, action="reject", proposed_due=day(25)).status == 200


def test_a_date_moves_twice_at_most_and_missed_waits_for_the_date(data_dir: Path) -> None:
    agent = agent_with_a_cycle(data_dir)
    mid = create(agent, title="Listing ready", measure="Proposed to my owner", due=day(5))
    assert refused(agent, "milestone_update", milestone_id=mid, status="missed", result="No time.") == (
        "milestone #1 is due 2026-09-06 (in 5 days): it is missed only once that day has passed. Until then, reach "
        "it, or move its date (due, with why in note), or drop it (why)"
    )
    assert call(agent, "milestone_update", milestone_id=mid, due=day(7), note="Photos.").endswith("moved 1 time).")
    assert call(agent, "milestone_update", milestone_id=mid, due=day(9), note="More photos.").endswith("2 times).")
    assert refused(agent, "milestone_update", milestone_id=mid, due=day(12), note="Again.") == (
        "milestone #1 has moved 2 times (first due 2026-09-06): a date moves 2 times at most. Reach it by "
        "2026-09-10, or drop it (why); if it isn't reached, close it missed once that day has passed"
    )
    agent.clock.advance(days=9)  # its day: still not missed
    assert "it is missed only once that day has passed. Until then, reach it, or drop it (why)" in refused(
        agent, "milestone_update", milestone_id=mid, status="missed", result="No time."
    )
    agent.clock.advance(days=1)
    assert refused(agent, "milestone_update", milestone_id=mid, due=day(20), note="Once more.") == (
        "milestone #1 has moved 2 times (first due 2026-09-06): a date moves 2 times at most. Close it now: done if "
        "its measure is met (with the evidence), missed if not (why, and what now); or drop it (why)"
    )
    assert call(agent, "milestone_update", milestone_id=mid, status="missed", result="No time; smaller step next.") == (
        "Milestone #1: missed."
    )


def test_dropping_a_milestone_drops_the_open_ones_leading_to_it(data_dir: Path) -> None:
    agent = agent_with_a_cycle(data_dir)
    goal = create(agent, title="Two legs that earn", measure="30 EUR a month", due=day(80))
    month = create(agent, title="First sale", measure="Revenue recorded", due=day(25), parent_id=goal)
    week = create(agent, title="Listing ready", measure="Proposed", due=day(5), parent_id=month)
    photos = create(agent, title="Photos made", measure="5 photos", due=day(3), parent_id=goal)
    call(agent, "milestone_update", milestone_id=photos, status="done", result="5 photos in photos/planner/")
    theirs = add(agent, title="Shop banner", measure="Banner uploaded", due=day(4), parent_id=week)
    assert refused(agent, "milestone_update", milestone_id=goal, status="dropped", result="Too far out.") == (
        f"your owner's milestone #{theirs} leads to it: dropping it would drop theirs too. Link theirs to another "
        "milestone first (parent_id), or ask your owner"
    )
    other = create(agent, title="Pinterest", measure="10 pins", due=day(40))
    call(agent, "milestone_update", milestone_id=theirs, parent_id=other)
    assert call(agent, "milestone_update", milestone_id=goal, status="dropped", result="Too far out.") == (
        f"Milestone #{goal}: dropped. Dropped with it, as they led to it: #{week}, #{month}."
    )
    for mid in (week, month):
        row = milestone(agent, mid)
        assert (row["status"], row["result"], row["closed_by"], row["closed_cycle_id"]) == (
            "dropped",
            "Dropped with #1: Too far out.",
            "agent",
            1,
        )
    assert milestone(agent, photos)["status"] == "done"  # what was closed stays as it was

    # The owner's drop takes the steps too, theirs and the agent's.
    step = create(agent, title="Pins drafted", measure="10 drafts", due=day(2), parent_id=theirs)
    dropped = decide(agent, other, action="drop")
    assert dropped.status == 200 and dropped.body["dropped_with"] == [step, theirs]
    assert milestone(agent, step)["result"] == f"Dropped by your owner with #{other}."
    assert {milestone(agent, i)["closed_by"] for i in (other, theirs, step)} == {"owner"}


def test_the_last_four_open_places_are_the_owners(data_dir: Path) -> None:
    agent = agent_with_a_cycle(data_dir)
    for n in range(16):
        create(agent, title=f"Step {n}", measure="x", due=day(10))
    assert refused(agent, "milestone_plan", milestones=[dict(title="One more", measure="x", due=day(10))]) == (
        "16 milestones are open already, and the other 4 of the 20 places are kept for your owner: close or drop one "
        "first"
    )
    for n in range(4):
        add(agent, title=f"Owner's {n}", measure="x", due=day(10))
    full = owner(agent).add_milestone({"title": "Fifth", "measure": "x", "due": day(10)}, None)
    assert full.status == 409 and full.body["error"] == "20 milestones are open already"


def test_a_0_11_roadmap_comes_through_the_rebuild(tmp_path: Path) -> None:
    """Migration 0021 rebuilds the milestones table (to allow the owner's accept and reject): every row, link and
    guard stays."""
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 20], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', 'then', 'running', 'schedule', 0, 1)"
        )
        insert = (
            "INSERT INTO milestones (id, mode, session, life_id, parent_id, created_cycle_id, created_by, created_at,"
            " updated_at, title, measure, first_due, due, moves, status, result, closed_at, closed_by, owner_action,"
            " owner_version) VALUES (?, 'live', 0, 1, ?, ?, ?, 'then', 'then', ?, 'x', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        conn.execute(insert, (1, None, 1, "agent", "Goal", day(60), day(60), 0, "open", "", None, None, None, 0))
        conn.execute(insert, (2, 1, 1, "agent", "Step", day(5), day(19), 5, "open", "", None, None, None, 0))
        conn.execute(
            insert, (3, 1, None, "owner", "Theirs", day(9), day(9), 0, "dropped", "No.", "then", "owner", "drop", 2)
        )
        conn.execute("UPDATE cycles SET milestone_id = 2 WHERE id = 1")
        conn.execute("UPDATE cycles SET status = 'completed', ended_at = 'then' WHERE id = 1")
        before = [tuple(r) for r in conn.execute("SELECT * FROM milestones ORDER BY id")]
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == list(range(21, 48))
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        after = [tuple(r)[: len(before[0])] for r in conn.execute("SELECT * FROM milestones ORDER BY id")]
        assert after == before  # moved five times before 0.12.0: kept as it was
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT milestone_id FROM cycles").fetchone()[0] == 2
        for sql, guard in (
            ("DELETE FROM milestones WHERE id = 1", "cannot be deleted"),
            ("UPDATE milestones SET title = 'Other' WHERE id = 1", "identity is fixed"),
            ("UPDATE milestones SET result = 'Yes.' WHERE id = 3", "is final"),
            (f"UPDATE milestones SET due = '{day(70)}' WHERE id = 1", "counted, once"),
            ("UPDATE milestones SET status = 'done', closed_at = 'now' WHERE id = 1", "names who closed it"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=guard):
                conn.execute(sql)
        roadmap.owner_word(conn, 2, NOW, "note", "Keep going", "Stefan")
        conn.execute(
            "UPDATE milestones SET owner_action = 'accept', owner_version = owner_version + 1 WHERE id = 2"
        )  # an owner action the old table refused
    upgraded.close()


def test_the_dry_runs_fake_proposes_once_then_closes_an_owners_overdue_milestone() -> None:
    """It moved an overdue milestone a week once, then closed it; for the owner's, the move is a proposal that stays
    open, so without this it proposed a new date every cycle."""
    late = row(id=4, due=day(-2), created_by="owner")
    ask = fake_llm.roadmap_plan(f"== ROADMAP ==\n{roadmap.planner_text([late], [], TODAY)}")
    assert ask == ([fake_llm.MOVE_MILESTONE_STEP.format(id=4)], 4)
    waiting = {**late, "proposed_due": day(5)}
    ask = fake_llm.roadmap_plan(f"== ROADMAP ==\n{roadmap.planner_text([waiting], [], TODAY)}")
    assert ask == ([fake_llm.CLOSE_MILESTONE_STEP.format(id=4)], 4)
