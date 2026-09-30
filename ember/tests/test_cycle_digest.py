"""0.12.0: a digest of every wake cycle, built by Ember's code from its records. The journal was the model's last
reply: it could be lost, or claim work that never happened (live: cycle #33 recorded two file writes as written after
both had been skipped at the reply's token limit), and the reflection was never shown which calls failed or were
skipped. Now every cycle, failed and unreflected ones too, gets a digest; the next plans see the last two, the work
brief the newest one of its focus milestone and venture, and the reflection what its cycle did not do."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.agent import views
from app.agent.fake_llm import Fail, FakeTransport, Reply, ToolCalls, request_kind
from app.agent.prompts import REFLECT_MARKER
from app.economy.metering import Rejected
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_roadmap import JOURNAL, create, day, plan, planner_texts, section

WRITE = ("workspace_write", {"path": "drafts/planner.md", "mode": "create", "content": "Page 1"})


def reflect_text(fake: FakeTransport) -> str:
    [request] = [r for r in fake.sent if REFLECT_MARKER in json.dumps(r, ensure_ascii=False)]
    return request["messages"][-1]["content"][-1]["text"]


def test_the_reflection_and_the_next_plan_see_what_was_not_done(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(steps=["draft the planner"]),
            ToolCalls([("workspace_list", {})] * 4 + [WRITE]),  # the fifth call of a reply is skipped
            Reply("Done."),
            JOURNAL,
            plan(steps=[]),
        ]
    )
    agent, ends = run(data_dir, fake, cycles=2)
    assert ends[0].status == "completed"
    told = reflect_text(fake)
    assert told.startswith(REFLECT_MARKER)
    assert "Not done in this cycle (refused, failed, skipped or cut off; never write or save them as done):" in told
    assert "workspace_write drafts/planner.md (skipped: " in told
    [first] = rows(agent, "SELECT text, undone FROM cycle_digests WHERE cycle_id = 1")
    lines = first["text"].split("\n")
    assert lines[0].startswith("Cycle #1 completed · $") and first["undone"] == 1
    assert lines[1] == 'Goal: "Plan ahead"'
    assert lines[2].startswith("Not done (1): workspace_write drafts/planner.md (skipped: ")
    assert lines[3].startswith("Done (4): workspace_list (")
    assert lines[-2:] == ["Work ended: the agent ended it.", "Reflection: yes; journal by the agent."]
    shown = section(planner_texts(fake)[1], "YOUR LAST CYCLE")
    assert "What your last cycles did, from Ember's records (newest first):\nCycle #1 completed" in shown
    assert "Not done (1): workspace_write drafts/planner.md (skipped: " in shown
    assert views.cycle_detail(agent, 1)["digest"] == first["text"]  # type: ignore[index]
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="never changes"):
        conn.execute("UPDATE cycle_digests SET text = 'All done.' WHERE cycle_id = 1")


def test_a_cycle_that_failed_before_it_planned_gets_one_too(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.agent.loop.RETRY_DELAY_SECONDS", 0)
    broken = Fail(Rejected(400, "invalid_request_error | bad request"))
    agent, ends = run(data_dir, FakeTransport(script=[broken]))
    assert ends[0].status == "failed"
    [digest] = rows(agent, "SELECT text, undone FROM cycle_digests")
    lines = digest["text"].split("\n")
    assert lines[0].startswith("Cycle #1 failed (") and lines[1] == "No plan was made."
    assert lines[-1] == "Reflection: none; journal by Ember's code."


def test_the_brief_shows_the_last_cycle_aimed_at_its_focus(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            plan(steps=[]),
            plan(steps=["draft it"], milestone=4),
            ToolCalls([WRITE]),
            Reply("Done."),
            JOURNAL,
            plan(steps=["draft more"], milestone=4),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake)
    assert create(agent, title="Planner drafted", measure="3 pages in drafts/", due=day(5)) == 4
    agent.run_cycle("schedule")
    agent.run_cycle("schedule")
    briefs = [r["messages"][0]["content"][0]["text"] for r in fake.sent if request_kind(r) == "work"]
    assert "Its last cycle" not in section(briefs[0], "FOCUS")  # the first cycle on it
    focus = section(briefs[-1], "FOCUS")
    assert "Its last cycle (Ember's code's digest): Cycle #2 completed · $" in focus
    assert "· for milestone #4 / Goal: " in focus and "Done (1): workspace_write (" in focus
    [card] = [m for m in views.roadmap_view(agent)["items"] if m["id"] == 4]
    assert card["last_digest"].startswith("Cycle #3 completed")  # the Roadmap card's, the newest
