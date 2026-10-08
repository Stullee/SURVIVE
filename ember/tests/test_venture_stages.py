"""0.12.0: a venture's stages with rules, kept by Ember's code. Research could go on without ever reaching a business
case, the business case's first test never became a milestone, a killed venture's milestones stayed open, and only
the prompt said that the owner alone backs or kills a venture. Now research without a business case after 21 days is
parked, a backed venture's first test is a milestone it must meet before it goes live, a missed first test parks it,
and the owner alone takes up what Ember's code parked."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent import roadmap, stages, tools, ventures
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from app.db import Database, discover_migrations, migrate
from app.economy.clock import to_iso
from tests.roadmap_helpers import (
    led_to_goal,
    set_milestone,  # noqa: E402
)
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_ventures import DROPSHIPPING, ETSY, VENTURING, plan, venture


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


def keep(agent: Agent) -> list[str]:
    with agent.db.transaction() as conn:
        return stages.keep(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()))


def milestone(agent: Agent, milestone_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM milestones WHERE id = {milestone_id}")[0]


def backed(data_dir: Path) -> tuple[Agent, int]:
    """An agent whose owner backed the dropshipping venture, and the milestone of its first test."""
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    with agent.db.transaction() as conn:
        ventures.update(conn, DROPSHIPPING, "2026-09-01T12:00:00Z", first_test="Sell 3 stores' worth of samples")
    reply = owner(agent).decide_venture(DROPSHIPPING, {"action": "back", "comment": "Go.", "confirm": True}, "Stefan")
    assert reply.status == 200, reply.body
    test = venture(agent, DROPSHIPPING)["test_milestone_id"]
    assert test is not None
    return agent, int(test)


def test_a_backed_ventures_first_test_is_a_milestone_it_meets_before_it_goes_live(data_dir: Path) -> None:
    agent, test = backed(data_dir)
    row = milestone(agent, test)
    with agent.db.connection() as conn:
        goal = roadmap.money_goal(conn, agent.scope())
    assert (row["created_by"], row["kind"], row["venture_id"], row["parent_id"]) == (
        "code",
        "first_test",
        3,
        goal["id"],
    )
    assert row["title"] == "First test: Dropshipping store"
    assert row["measure"] == "Its first test is met: Sell 3 stores' worth of samples"
    assert row["due"] == (agent.clock.today() + timedelta(days=stages.FIRST_TEST_DAYS)).isoformat()
    refused = call(agent, "venture_update", venture_id=DROPSHIPPING, stage="live")
    assert (
        not refused.ok and f"goes live once its first test (milestone #{test} on your roadmap) is met" in refused.text
    )
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="once its first test is met"):
        conn.execute("UPDATE ventures SET stage = 'live' WHERE id = ?", (DROPSHIPPING,))
    for args, error in (
        ({"due": (agent.clock.today() + timedelta(days=40)).isoformat(), "note": "later"}, "doesn't move"),
        ({"status": "dropped", "result": "Not needed."}, "only your owner drops it"),
    ):
        refused = call(agent, "milestone_update", milestone_id=test, **args)
        assert not refused.ok and error in refused.text, refused.text
    met = call(agent, "milestone_update", milestone_id=test, status="done", result="3 samples sold, receipts #71-#73")
    assert not met.ok and "your owner confirms it" in met.text, met.text  # 0.15.0: not on the agent's word
    assert owner(agent).decide_milestone(test, {"action": "drop", "comment": "Met: 3 sold."}, "Stefan").status == 200
    launched = call(agent, "venture_update", venture_id=DROPSHIPPING, stage="live")
    assert launched.ok, launched.text
    assert venture(agent, DROPSHIPPING)["stage"] == "live"


def test_a_missed_first_test_parks_the_venture_until_the_owner_takes_it_up(data_dir: Path) -> None:
    agent, test = backed(data_dir)
    agent.clock.advance(days=stages.FIRST_TEST_DAYS + 3)
    assert keep(agent) == []  # a few days late: the agent may still close it
    agent.clock.advance(days=stages.FIRST_TEST_GRACE_DAYS)
    [happened] = keep(agent)
    assert happened.startswith(
        f"Ember's code parked venture #3 (Dropshipping store): its first test (milestone #{test})"
    )
    assert (milestone(agent, test)["status"], milestone(agent, test)["closed_by"]) == ("missed", "code")
    parked = venture(agent, DROPSHIPPING)
    assert (parked["stage"], parked["parked_by"]) == ("parked", "code")
    assert parked["notes"].endswith(f"Parked by Ember's code: its first test (milestone #{test}) was missed.")
    refused = call(agent, "venture_update", venture_id=DROPSHIPPING, stage="researching", note="One more try.")
    assert "Not done (the rest is saved): Ember's code parked venture #3 by its stage's rule: only your owner" in (
        refused.text
    )  # 0.32.0: its note is kept, its stage isn't changed
    kept = venture(agent, DROPSHIPPING)
    assert (kept["stage"], kept["parked_by"]) == ("parked", "code") and kept["notes"].endswith("One more try.")
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="out of their park"):
        conn.execute("UPDATE ventures SET stage = 'researching' WHERE id = ?", (DROPSHIPPING,))
    again = owner(agent).decide_venture(
        DROPSHIPPING, {"action": "back", "comment": "Once more.", "confirm": True}, "Stefan"
    )
    assert again.status == 200 and again.body["stage"] == "building"
    fresh = venture(agent, DROPSHIPPING)["test_milestone_id"]
    assert fresh != test and milestone(agent, fresh)["status"] == "open"  # a new first test


def researched(agent: Agent, venture_id: int) -> None:
    """A research call for the venture that found something."""
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    with agent.db.transaction() as conn:
        ventures.add_research(
            conn, venture_id, cycle, None, "Who sells it?", None, 2, 40_000, to_iso(agent.clock.now())
        )


def test_research_without_a_business_case_is_parked_after_three_weeks(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    assert call(agent, "venture_update", venture_id=DROPSHIPPING, stage="researching").ok
    agent.clock.advance(days=5)  # the clock starts with its research, not with the stage
    researched(agent, DROPSHIPPING)
    due = (agent.clock.today() + timedelta(days=30)).isoformat()  # set by the agent before 0.35.0
    set_milestone(agent, "Case for dropshipping", due, measure="Its business case is proposed", venture_id=DROPSHIPPING)
    theirs = owner(agent).add_milestone(
        {"title": "Ask me first", "measure": "We talked", "due": "2026-10-20"}, "Stefan"
    )
    with agent.db.transaction() as conn:
        conn.execute("UPDATE milestones SET venture_id = ? WHERE id = ?", (DROPSHIPPING, theirs.body["id"]))
    agent.clock.advance(days=stages.RESEARCH_DAYS - 1)
    assert keep(agent) == []
    agent.clock.advance(days=1)
    happened = keep(agent)
    assert any(
        h.startswith("Ember's code parked venture #3 (Dropshipping store): no business case 21 days after its research")
        for h in happened
    )
    assert venture(agent, DROPSHIPPING)["parked_by"] == "code"
    linked = rows(
        agent, f"SELECT title, status, closed_by FROM milestones WHERE venture_id = {DROPSHIPPING} ORDER BY id"
    )
    assert linked == [
        {"title": "Case for dropshipping", "status": "dropped", "closed_by": "code"},
        {"title": "Ask me first", "status": "open", "closed_by": None},  # the owner's stays theirs
    ]
    etsy = venture(agent, ETSY)
    assert etsy["stage"] == "researching" and etsy["parked_by"] is None  # no one researched it: it waits
    more = owner(agent).decide_venture(DROPSHIPPING, {"action": "research", "comment": "Look again."}, "Stefan")
    assert more.status == 200 and venture(agent, DROPSHIPPING)["stage_at"] == to_iso(agent.clock.now())
    researched(agent, DROPSHIPPING)
    agent.clock.advance(days=stages.RESEARCH_DAYS - 1)
    assert keep(agent) == []  # the owner's word started the clock again


def test_only_the_owner_backs_or_kills_and_a_kill_drops_its_milestones(data_dir: Path) -> None:
    agent, test = backed(data_dir)
    for stage in ("killed", "building"):
        with (
            agent.db.transaction() as conn,
            pytest.raises(sqlite3.IntegrityError, match="only the owner backs or kills"),
        ):
            conn.execute("UPDATE ventures SET stage = ? WHERE id = ?", (stage, ETSY))
    killed = owner(agent).decide_venture(DROPSHIPPING, {"action": "kill", "comment": "No margin."}, "Stefan")
    assert killed.status == 200
    row = milestone(agent, test)
    assert (row["status"], row["closed_by"], row["result"]) == ("dropped", "owner", "Your owner killed venture #3.")


def test_the_plan_shows_the_stage_rules(data_dir: Path) -> None:
    agent, test = backed(data_dir)
    researched(agent, ETSY)
    with agent.db.connection() as conn:
        rows_ = ventures.all_ventures(conn, agent.scope())
        text = ventures.planner_lines(rows_, ventures.money(conn, agent.scope()), full=False)
        focus = ventures.focus_text(ventures.get(conn, agent.scope(), DROPSHIPPING), ventures.Money(), None, [])
    assert (
        "#1 [researching] Etsy digital products · not scored yet · researched since 2026-09-01: no business case by"
        " 2026-09-22 parks it" in text
    )
    assert f"#3 [building] Dropshipping store · not scored yet · first test: milestone #{test}; it goes live" in text
    assert f"Stage rule (Ember's code keeps it): first test: milestone #{test}; it goes live once that is met" in focus


def test_a_met_money_goal_keeps_the_first_test(data_dir: Path) -> None:
    agent, test = backed(data_dir)
    with agent.db.transaction() as conn:
        goal = roadmap.money_goal(conn, agent.scope())
        roadmap.keep_money_goal(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()), 5_000_000, 1, 60)
        after = roadmap.money_goal(conn, agent.scope())
    assert after["id"] != goal["id"]
    row = milestone(agent, test)
    assert (row["status"], row["parent_id"]) == ("open", after["id"])  # moved to the next goal, not dropped


def test_the_0_11_tree_and_roadmap_come_through_the_rebuild(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 29], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        conn.execute(
            "INSERT INTO ventures (id, mode, session, life_id, created_by, created_at, updated_at, title, pitch, stage,"
            " notes, parked_by) VALUES (1, 'live', 0, 1, 'owner', '2026-09-01T10:00:00Z', '2026-09-02T10:00:00Z',"
            " 'Fiverr', 'Services', 'parked', 'On hold', 'owner')"
        )
        insert = (
            "INSERT INTO milestones (id, mode, session, life_id, parent_id, created_by, created_at, updated_at, title,"
            " measure, first_due, due)"
            " VALUES (?, 'live', 0, 1, ?, ?, 'then', 'then', ?, 'x', '2026-12-01', '2026-12-01')"
        )
        conn.execute(insert, (1, None, "code", "Earn as much as you spend"))
        conn.execute(insert, (2, 1, "code", roadmap.DECISION_TITLE))
        conn.execute(insert, (3, 1, "agent", "Mine"))
        before = [tuple(r) for r in conn.execute("SELECT * FROM ventures")]
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == [
        m.version for m in discover_migrations() if m.version >= 30
    ]
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        after = [tuple(r)[: len(before[0])] for r in conn.execute("SELECT * FROM ventures")]
        assert after == before
        assert conn.execute("SELECT stage_at FROM ventures").fetchone()[0] == "2026-09-02T10:00:00Z"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM milestones ORDER BY id")]
        assert kinds == ["money_goal", "decision", None]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError, match="out of their park"):
            conn.execute("UPDATE ventures SET stage = 'researching' WHERE id = 1")
    upgraded.close()
