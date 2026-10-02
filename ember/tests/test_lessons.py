"""0.12.0: lessons the owner pins, and their daily consolidation. Appending to a full lessons file dropped its oldest
lines, data-backed no's among them ("Dropshipping ruled out by data: margins under 5%"), and a rewrite could drop
anything. Now the owner pins a lesson: it is never dropped, a rewrite must keep it, and every plan and work step shows
it first. After each daily review, a call of its own merges the lessons that say the same and retires those newer ones
contradict; Ember's code checks its answer, keeps what it doesn't account for, and never lets it drop a pinned lesson
or one with numbers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.agent import context, memory
from app.agent.fake_llm import FakeTransport, request_kind
from app.agent.service import Agent
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_review import next_day
from tests.test_roadmap import plan, planner_texts, section

NO = "Dropshipping ruled out by data: margins under 5%."
PINNED = "Never hand my owner design work."


def lessons(*lines: str, first: int = 1) -> str:
    return "# Lessons\n\n" + "".join(f"- [#c{i}] {line}\n" for i, line in enumerate(lines, first))


def write_lessons(agent: Agent, text: str) -> None:
    agent.roots()[1].write("lessons.md", text)


def pin(agent: Agent, line: str) -> int:
    reply = owner(agent).pin_lesson({"text": line}, "Stefan", agent.memory().read("lessons"))
    assert reply.status == 201, reply.body
    return int(reply.body["id"])  # type: ignore[index]


def test_a_pinned_lesson_is_never_dropped_and_a_rewrite_keeps_it(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    write_lessons(agent, lessons(PINNED, NO, "Ask people first."))
    pinned = pin(agent, f"- [#c1] {PINNED}")
    refused = owner(agent).pin_lesson({"text": "Something it never learned"}, "Stefan", agent.memory().read("lessons"))
    assert refused.status == 409 and "that isn't one of the lessons now" in str(refused.body)
    mem, now = agent.memory(), "2026-09-30T12:00:00Z"
    with agent.db.transaction() as conn:
        for i in range(90):  # the file fills: its oldest lines go, the pinned one and a no with numbers stay
            name = chr(97 + i // 26) + chr(97 + i % 26)
            mem.update(conn, "lessons", "append", f"Lesson {name} about something that happened today.", 1, now)
        text = mem.read("lessons")
        assert f"] {PINNED}" in text and NO in text and "Ask people first." not in text and "Lesson aa " not in text
        with pytest.raises(memory.MemoryError_, match="keep the lessons your owner pinned, word for word"):
            mem.update(conn, "lessons", "replace", "# Lessons\n\n- Only this now.", 1, now)
        mem.update(conn, "lessons", "replace", f"# Lessons\n\n- {PINNED}\n- Only this now.", 1, now)
    assert owner(agent).unpin_lesson(pinned, "Stefan").status == 200
    assert owner(agent).unpin_lesson(pinned, "Stefan").status == 404  # unpinning is final
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="unpinning it is final"):
        conn.execute("UPDATE lesson_pins SET unpinned_at = NULL")
    events = [e["message"] for e in agent.db.recent_events(limit=10)]
    assert f"Stefan pinned a lesson: {PINNED}" in events and f"Stefan unpinned a lesson: {PINNED}" in events


def test_every_plan_and_work_step_shows_the_pinned_lessons_first(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[]), plan(steps=[])])
    agent, _ = run(data_dir, fake)
    old = [f"Old lesson {i} with nothing in it worth keeping for long." for i in range(40)]
    write_lessons(agent, lessons(PINNED, *old))
    pin(agent, PINNED)
    agent.run_cycle("schedule")
    shown = section(planner_texts(fake)[-1], context.LESSONS_HEADING)
    assert shown.startswith(f"{context.PINS_HEADING}\n- {PINNED}\n\n")
    assert shown.count(PINNED) == 1 and "Old lesson 39" in shown  # the newest others follow
    assert agent.dashboard()["mind"]["lesson_pins"][0]["text"] == PINNED  # the owner's Mind tab


def test_the_daily_review_consolidates_the_lessons(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    same = ["Ask people before building.", "ask people   before building.", "Ask people before building."]
    rest = [f"Rule {chr(65 + i)} about listings and photos." for i in range(10)]
    write_lessons(agent, lessons(PINNED, NO, *same, *rest))
    pin(agent, PINNED)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    kinds = [request_kind(r) for r in list(fake.sent)[before:]]
    assert kinds[:3] == ["review", "consolidate", "plan"]
    [version] = rows(agent, "SELECT content FROM memory_versions WHERE source = 'consolidation'")
    _, kept = memory.lesson_lines(version["content"])
    # 0.18.0: the review's own lesson was added first (16 lessons)
    assert len(kept) == 14 and sum("Ask people before building." in k for k in kept) == 1  # 3 said the same
    assert kept[0] == f"- [#c1] {PINNED}" and kept[1] == f"- [#c2] {NO}"  # newest last, as before
    [call] = rows(agent, "SELECT purpose, overhead FROM llm_calls WHERE purpose = 'consolidate'")
    assert call["overhead"] == 1  # like the review: the day's, not a milestone's
    events = [e["message"] for e in agent.db.recent_events(limit=40)]
    assert "Ember's code consolidated the lessons: 16 lessons became 14 (2 merged into others, 0 dropped)" in events


def test_a_short_file_needs_no_consolidation(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    write_lessons(agent, lessons("Ask people first.", "Keep it short."))
    agent.run_cycle("schedule")
    assert "consolidate" not in [request_kind(r) for r in fake.sent]


def test_the_consolidation_never_drops_a_pinned_lesson_or_one_with_numbers() -> None:
    text = lessons(PINNED, NO, "Old rule about a tool that is gone.", "Ask people first.")
    everything = {
        "keep": [{"text": "All rules are fine.", "from": [1]}],
        "drop": [{"line": i, "why": "old"} for i in (1, 2, 3, 4)],
    }
    new, summary = memory.consolidate(text, everything, {memory.lesson_key(PINNED)}, 4_000) or ("", "")
    assert memory.lesson_lines(new)[1] == [f"- [#c1] {PINNED}", f"- [#c2] {NO}"]
    assert summary == (
        '4 lessons became 2 (0 merged into others, 2 dropped): dropped "Old rule about a tool that is gone." (old); '
        '"Ask people first." (old)'
    )
    assert memory.consolidate(text, "not a dict", set(), 4_000) is None
    assert memory.consolidate(text, {"keep": [], "drop": []}, set(), 4_000) is None  # nothing changes
