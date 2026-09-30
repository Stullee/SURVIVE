"""0.12.0: a milestone that replaces a dropped or missed one names it. Dropping a milestone and creating it again reset
its moved count and let its measure soften without a trace (the analysis's reproduction). Now a new milestone much
like one dropped or missed lately must name it in replaces; the replacement keeps its first date and moves (one more
for a dropped one, never beyond the limit), serves what it served, and the plan shows what it replaces."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.agent import roadmap, tools
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_money_goal import call
from tests.test_roadmap import day, plan


def made(outcome: tools.Outcome) -> int:
    assert outcome.ok, outcome.text
    return int(outcome.text.split("#", 1)[1].split(" ", 1)[0])


def create(agent: Agent, **fields: Any) -> tools.Outcome:
    return call(agent, "milestone_plan", milestones=[fields])


def milestone(agent: Agent, milestone_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM milestones WHERE id = {milestone_id}")[0]


def test_dropping_and_recreating_keeps_the_moves(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    old = made(create(agent, title="Three listings live", measure="3 listings on Etsy", due=day(5), parent="1"))
    assert call(agent, "milestone_update", milestone_id=old, due=day(8), note="Photos take longer.").ok
    dropped = call(agent, "milestone_update", milestone_id=old, status="dropped", result="Starting over.")
    assert dropped.ok, dropped.text
    refused = create(agent, title="Three listings live soon", measure="Some listings on Etsy", due=day(20))
    assert not refused.ok and f'milestone #{old} "Three listings live" was dropped on' in refused.text
    assert "name it in replaces" in refused.text
    new = create(agent, title="Three listings live soon", measure="Some listings on Etsy", due=day(20), replaces=old)
    assert new.ok, new.text
    assert (
        f'It replaces #{old} (dropped; its measure was "3 listings on Etsy"), first due {day(5)}, moved 2 times.'
        in (new.text)
    )
    row = milestone(agent, made(new))
    assert (row["replaces_id"], row["first_due"], row["moves"], row["parent_id"]) == (old, day(5), 2, 1)
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="is fixed"):
        conn.execute("UPDATE milestones SET replaces_id = NULL WHERE id = ?", (row["id"],))
    with agent.db.connection() as conn:
        text = roadmap.planner_text(roadmap.open_milestones(conn, agent.scope()), [], agent.clock.today())
    assert f"moved 2 times (first due {day(5)}) · replaces #{old}" in text
    # dropped at its limit: a replacement would move it beyond
    assert call(agent, "milestone_update", milestone_id=row["id"], status="dropped", result="Again.").ok
    beyond = create(agent, title="Three listings live at last", measure="x", due=day(30), replaces=row["id"])
    assert not beyond.ok and "replacing it would move it once more, beyond 2" in beyond.text


def test_a_missed_one_is_tried_again_honestly(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    old = made(create(agent, title="First sale", measure="1 order", due=day(1)))
    agent.clock.advance(days=2)
    assert call(agent, "milestone_update", milestone_id=old, status="missed", result="No buyer; cheaper test.").ok
    again = create(agent, title="First sale", measure="1 order at a lower price", due=day(20), replaces=old)
    assert again.ok, again.text
    again_id = made(again)
    assert milestone(agent, again_id)["moves"] == 0  # the miss is on record: a new attempt starts fresh
    refused = create(agent, title="Something else", measure="x", due=day(20), replaces=again_id)
    assert not refused.ok and "is open: a milestone replaces one that was dropped or missed" in refused.text
    twice = create(agent, title="Another first sale", measure="x", due=day(20), replaces=old)
    assert not twice.ok and f"replaces #{old} already" in twice.text
    theirs = create(agent, title="Not mine", measure="x", due=day(20), replaces=1)
    assert not theirs.ok and "is open" in theirs.text  # the money goal is open (and Ember's code's)


def test_a_title_of_its_own_needs_nothing(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    old = made(create(agent, title="Pinterest board", measure="10 pins", due=day(5)))
    assert call(agent, "milestone_update", milestone_id=old, status="dropped", result="No account.").ok
    assert create(agent, title="Newsletter signup page", measure="A page live", due=day(9)).ok
