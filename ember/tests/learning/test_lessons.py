"""0.12.0: lessons the owner pins, and their daily consolidation. Appending to a full lessons file dropped its oldest
lines, data-backed no's among them ("Dropshipping ruled out by data: margins under 5%"), and a rewrite could drop
anything. Now the owner pins a lesson: it is never dropped, a rewrite must keep it, and every plan and work step shows
it first. After each daily review, a call of its own merges the lessons that say the same and retires those newer ones
contradict; Ember's code checks its answer, keeps what it doesn't account for, and never lets it drop a pinned lesson
or one with numbers.

Also tests from 0.19.2 and 0.24.0 (formerly test_fixes_0192.py, test_fixes_0240.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.agent import context, memory, news, tools
from app.agent.fake_llm import FakeTransport, request_kind
from app.agent.service import Agent
from app.economy.clock import to_iso
from tests.agent_helpers import owner, rows, run
from tests.learning.test_review import next_day
from tests.plan.test_roadmap import plan, planner_texts, section
from tests.plan.test_roadmap import plan as fake_plan

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
    assert kinds[:4] == ["review", "consolidate", "weekly", "plan"]  # 0.18.0: the weekly look after the review
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


# --- From 0.19.2 (formerly test_fixes_0192.py) ---


# --- lessons ----------------------------------------------------------------------------------------------------------


TOOLS = tuple(tools.SPECS)


def test_a_full_lessons_file_drops_tool_limits_before_the_review_s_lesson() -> None:
    lessons = (
        "# Lessons\n\n"
        "- [#c68] Printify blueprint 443 costs: making 6.28 EUR + shipping 5.39 EUR.\n"
        "- [#c89] A decided action that isn't sent the same day costs a full day of runway for nothing.\n"
        "- [#c93] workspace_write caps content at 2,500 chars per call.\n"
        "- [#c96] project_create fails at 8 open projects.\n"
    )
    cap = len(lessons.encode()) - 40
    kept, dropped = memory._drop_oldest(lessons, cap, set(), TOOLS)
    assert dropped == 1 and "workspace_write" not in kept and "A decided action" in kept and "project_create" in kept
    # Without the tools' names, the lesson without a number went first (0.12.0's rule alone).
    before, _ = memory._drop_oldest(lessons, cap, set())
    assert "A decided action" not in before
    # A word that is also a tool's name (research, draft, look) doesn't make a business lesson a tool's.
    assert memory.identifiers(["research", "look", "project_create"]) == {"project_create"}


def test_an_upgrade_marks_the_lessons_about_the_tools_its_notes_name_to_re_check() -> None:
    """0.21.0 (analysis 0.20.1, FIX NOW 14): they were deleted, each upgrade, also "photos must be visibly distinct"."""
    lessons = (
        "# Lessons\n\n"
        "- [#c96] project_create fails at 8 open projects; fold new work into an existing project.\n"
        "- [#c93] workspace_write caps content at 2,500 chars per call.\n"
        "- [#c89] A decided action that isn't sent the same day costs a day of runway.\n"
        "- [#c37] Pinned: project_create a project per product line.\n"
    )
    notes = "## 0.19.1\n- project_create no longer refuses a ninth open project. project_list shows them."
    pinned = {memory.lesson_key("Pinned: project_create a project per product line.")}
    new, marked, retired = memory.recheck_for_release(lessons, notes, TOOLS, pinned, "0.19.1") or ("", [], [])
    assert marked == ["project_create fails at 8 open projects; fold new work into an existing project."]
    assert retired == []
    assert (
        "- [#c96] (re-check: 0.19.1 changed project_create) project_create fails at 8 open projects; fold new work"
        " into an existing project.\n" in new
    )
    assert "workspace_write" in new and "A decided action" in new and "- [#c37] Pinned: project_create" in new
    assert memory.recheck_for_release(lessons, "## 0.19.2\n- research is cheaper.", TOOLS, set(), "0.19.2") is None
    again, _, _ = memory.recheck_for_release(new, notes, TOOLS, pinned, "0.20.0") or ("", [], [])
    assert "(re-check: 0.20.0 changed project_create) project_create fails" in again and "0.19.1" not in again
    # A marked lesson still is itself: for duplicates and pins (0.23.0, the review of 0.21.0)
    marked_line = next(line for line in again.splitlines() if "(re-check:" in line)
    assert memory.lesson_key(marked_line) == memory.lesson_key("- [#c1] project_create fails at 8 open projects;"
                                                               " fold new work into an existing project.")  # fmt: skip
    assert memory.lesson_text(marked_line).startswith("(re-check: 0.20.0 changed project_create) project_create")
    seen = memory.consolidation_input(again, pinned, TOOLS)  # the consolidation sees the mark, so it keeps it
    assert "(re-check: 0.20.0 changed project_create) project_create fails" in seen


def test_a_full_lessons_file_gets_short_marks_and_says_what_it_retired() -> None:
    """0.23.0 (review of 0.21.0): on a full file the marks deleted the oldest marked lessons, a pinned one marked by
    an earlier upgrade too, and the event said they were only marked. Now: shorter marks first; only the oldest
    unpinned ones the marks have no room for are retired, and said so."""
    notes = "## 0.21.0\n- project_create changed."
    pinned_line = "- [#c0] (re-check: 0.20.0 changed project_create) project_create: keep one per product line."
    roomy = "# Lessons\n\n" + pinned_line + "\n- [#c99] project_create needs a title.\n"
    for i in range(1, 100):  # filled to within a long mark of the cap, with room for a short one
        line = f"- [#c{i}] Lesson {i} about prices: {'x' * 20}\n"
        if len((roomy + line).encode()) > memory.CAPS["lessons"] - 15:
            break
        roomy += line
    text, marked, retired = memory.recheck_for_release(
        roomy, notes, TOOLS, {memory.lesson_key(pinned_line)}, "0.21.0"
    ) or ("", [], [])
    assert len(roomy.encode()) <= memory.CAPS["lessons"] < len(roomy.encode()) + 45  # a long mark doesn't fit
    assert "- [#c99] (re-check) project_create needs a title.\n" in text and retired == []
    assert len(text.encode()) <= memory.CAPS["lessons"]
    assert marked == ["project_create needs a title."] and pinned_line in text
    many = (
        "# Lessons\n\n"
        + pinned_line
        + "\n"
        + "".join(f"- [#c{i}] project_create note {i}: {'x' * 40}\n" for i in range(1, 60))
    )
    many = many[: many.rindex("\n", 0, memory.CAPS["lessons"]) + 1]  # just within the cap
    text, marked, retired = memory.recheck_for_release(
        many, notes, TOOLS, {memory.lesson_key(pinned_line)}, "0.21.0"
    ) or ("", [], [])
    assert len(text.encode()) <= memory.CAPS["lessons"] and pinned_line in text  # the pinned one stays
    assert retired and retired[0].startswith("project_create note 1:") and len(marked) + len(retired) >= 50


def test_the_first_cycle_after_an_upgrade_marks_them_once(data_dir: Path) -> None:
    def before(agent: Any) -> None:
        agent.db.set_meta(news.changelog_key(agent.scope().mode), "0.19.0")
        lessons = "# Lessons\n\n- [#c96] project_create fails at 8 open projects.\n- [#c9] Keep the owner informed.\n"
        with agent.db.transaction() as conn:
            agent.memory().rewrite(conn, "lessons", lessons, "consolidation", "2026-09-01T08:00:00Z")

    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), before=before)
    text = agent.memory().read("lessons")
    assert "] (re-check: " in text and "changed project_create) project_create fails" in text
    assert "Keep the owner informed." in text
    events = [e["message"] for e in agent.db.recent_events(30)]
    assert any(e.startswith("Ember's code marked 1 lesson(s) to re-check (tools ") for e in events)
    assert agent.db.get_meta(f"agent.{agent.scope().mode}.lessons_version")


# --- From 0.24.0 (formerly test_fixes_0240.py) ---


def now(agent: Any) -> str:
    return to_iso(agent.clock.now())


# --- lessons: the one just added stays ------------------------------------------------------------------------------


def test_a_lesson_just_added_isn_t_dropped_by_its_own_call_and_the_answer_names_what_went(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[fake_plan(steps=[])]))
    new = "Before any propose_bluesky_post, call bluesky_posts first to see what is live."
    old, i = "# Lessons\n\n", 0
    while len(old.encode()) + len(new) + 12 <= memory.CAPS["lessons"]:  # full: the new lesson doesn't fit
        i += 1
        old += f"- [#c{i}] A lesson about buyers number {i}, kept for a while longer.\n"
    agent.roots()[1].write("lessons.md", old)
    mem = agent.memory()
    with agent.db.transaction() as conn:
        said = mem.update(conn, "lessons", "append", new, 1, now(agent), tuple(tools.SPECS))
    assert new in mem.read("lessons")
    assert "line(s) dropped to make room (" in said and '"A lesson about buyers number 1, kept' in said


def test_the_lessons_just_added_go_last() -> None:
    text = "# Lessons\n\n- [#c1] Buyers want A4.\n- [#c2] Use propose_bluesky_post only after bluesky_posts.\n"
    cap = len(text.encode()) - 10
    gone: list[str] = []
    fresh = {memory.lesson_key("Use propose_bluesky_post only after bluesky_posts.")}
    kept, dropped = memory._drop_oldest(text, cap, set(), tuple(tools.SPECS), fresh, gone)
    assert dropped == 1 and gone == ["- [#c1] Buyers want A4."] and "propose_bluesky_post" in kept
    before, _ = memory._drop_oldest(text, cap, set(), tuple(tools.SPECS))  # not fresh: the one naming a tool goes
    assert "Buyers want A4." in before and "propose_bluesky_post" not in before
