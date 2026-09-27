"""What the owner wrote or decided reaches every step of the next cycle, and lessons aren't noted twice."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.agent import context
from app.config import Settings
from tests.test_agent import make_agent, plan, rows, text, tools
from tests.test_owner_loop import cycle_with_approval, owner

QUESTION = "will you create them by image generation?"
JOURNAL = tools(("write_journal", {"summary": "Answered my owner", "entry": "Said what I can do."}))


def first_text(request: dict[str, Any]) -> str:
    """The brief of a work or reflect request, or the context of a plan or will request."""
    return request["messages"][0]["content"][0]["text"]


def section(text_: str, title: str) -> str | None:
    match = re.search(rf"(?:^|\n)== {title} ==\n(.*?)(?:\n\n== |\Z)", text_, re.DOTALL)
    return match.group(1) if match else None


def message_line(agent: Any, text_: str) -> str:
    created = rows(agent, "SELECT created_at FROM messages WHERE sender = 'owner' ORDER BY id DESC")[0]["created_at"]
    line = f"Message from your owner ({created}): {json.dumps(text_, ensure_ascii=False)}"
    assert re.fullmatch(r'Message from your owner \(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\): ".*"', line)
    return line


def test_the_owners_question_reaches_the_brief_of_every_step(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            plan(steps=["Answer my owner's question with message_owner"]),
            tools(("message_owner", {"text": "No, I can't make images; I could write the prompts for you."})),
            text("Answered my owner."),
            JOURNAL,
            plan(steps=["look around"]),
            text("Looked."),
            text("Reflected."),
        ],
    )
    assert owner(agent).send_message({"text": QUESTION}, "Stefan").status == 201
    line = message_line(agent, QUESTION)
    assert agent.run_cycle("schedule").status == "completed"

    planned, *steps = transport.sent
    assert [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls ORDER BY id")] == [
        "plan",
        "work",
        "work",
        "reflect",
    ]
    assert line in (section(first_text(planned), "SINCE YOUR LAST WAKE") or "").splitlines()
    briefs = [first_text(r) for r in steps]
    assert len(set(briefs)) == 1  # the cached prefix stays the same for the whole cycle, reflection included
    brief = briefs[0]
    assert section(brief, "FROM YOUR OWNER") == line
    assert brief.index("== PLAN ==") < brief.index("== FROM YOUR OWNER ==") < brief.index("== FOCUS ==")
    assert re.search(r"\n\n== PLAN ==\n[^=]*\n\n== FROM YOUR OWNER ==\n", brief)
    assert steps[-1]["messages"][-1]["content"][-1]["text"].startswith("REFLECT PHASE")
    assert rows(agent, "SELECT text FROM messages WHERE sender = 'agent'")[0]["text"].startswith("No, I can't")

    # Once planned with, the message is not shown again.
    agent.run_cycle("schedule")
    assert all("FROM YOUR OWNER" not in first_text(r) for r in transport.sent[4:])
    assert QUESTION not in json.dumps(transport.sent[4:], ensure_ascii=False)


def test_an_approval_with_changes_reaches_the_brief(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            *cycle_with_approval(),
            plan(steps=["Hand my owner the approved text"]),
            text("Nothing to do: my owner posts it."),
            JOURNAL,
        ],
    )
    agent.run_cycle("schedule")
    approval_id = rows(agent, "SELECT id FROM approvals")[0]["id"]
    who = owner(agent)
    changed = {"final_payload": "Hi there, written by an AI.", "comment": "shorter"}
    assert who.decide(approval_id, {"decision": "approve_with_changes", **changed}, "Stefan").status == 200
    assert who.send_message({"text": "Posting it tonight."}, "Stefan").status == 201
    line = message_line(agent, "Posting it tonight.")
    sent_before = len(transport.sent)
    agent.run_cycle("schedule")

    planned, work, reflect = transport.sent[sent_before:]
    decided = (
        f'Request #{approval_id} (publish) "Post the guide": approved with changes. Use the owner\'s version, not '
        'yours: "Hi there, written by an AI.". Owner\'s comment: "shorter". Your owner will carry it out and report '
        "back."
    )
    for request in (work, reflect):
        assert section(first_text(request), "FROM YOUR OWNER") == f"{decided}\n{line}"
    assert f"{decided}\n{line}" in (section(first_text(planned), "SINCE YOUR LAST WAKE") or "")


def test_the_last_will_hears_the_owner(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=0.03, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    agent, transport = make_agent(data_dir, [text("I tried a guide. Lesson: ask people first.")], settings)
    assert agent.run_cycle("schedule").status == "refused"  # starving: the will is due
    assert owner(agent).send_message({"text": "What should I do with your drafts?"}, "Stefan").status == 201
    line = message_line(agent, "What should I do with your drafts?")
    assert agent.run_cycle("last_will").status == "completed"
    will = first_text(transport.sent[-1])
    assert section(will, "FROM YOUR OWNER") == line
    assert will.index("== STATUS ==") < will.index("== FROM YOUR OWNER ==") < will.index("== PROJECTS ==")


def test_a_quiet_owner_adds_no_section(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [plan(steps=["look around"]), text("Looked."), text("Reflected.")])
    agent.run_cycle("schedule")
    assert len(transport.sent) == 3
    assert all("FROM YOUR OWNER" not in first_text(r) for r in transport.sent)


def test_a_long_owner_section_is_cut_without_squeezing_the_brief(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [plan(steps=["read my owner's notes"]), text("Read."), text("Done.")])
    who = owner(agent)
    for letter in "abc":
        assert who.send_message({"text": f"{letter} " + "ä" * 1_900}, "Stefan").status == 201
    agent.run_cycle("schedule")
    brief = first_text(transport.sent[1])
    owners = section(brief, "FROM YOUR OWNER") or ""
    assert context.json_bytes(owners) <= context.OWNER_BUDGET and re.search(r"…\[\d+ bytes cut\]$", owners)
    assert owners.startswith("Message from your owner (")
    assert "== LIMITS ==\nAt most" in brief and "bytes cut]" not in brief.split("== FOCUS ==")[1]
    assert context.json_bytes(brief) <= context.BRIEF_BUDGET


def test_a_lesson_is_noted_once(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)] * 3)
    for _ in range(3):  # idle cycles #1 to #3, for the lines' cycle tags
        agent.run_cycle("schedule")
    memory = agent.memory()

    def append(content: str, cycle_id: int) -> str:
        with agent.db.transaction() as conn:
            return memory.update(conn, "lessons", "append", content, cycle_id, "2026-09-01T12:00:00Z")

    def versions() -> int:
        return len(rows(agent, "SELECT id FROM memory_versions WHERE file = 'lessons'"))

    assert append("Ask people before building.", 1).startswith("lessons.md: appended (")
    before, count = memory.read("lessons"), versions()

    result = append("  ask PEOPLE   before building. \n- Ask people before building.", 2)
    assert result == "already noted: every line is already in lessons.md; nothing was added"
    assert memory.read("lessons") == before and versions() == count

    result = append("Ask people before building.\nPrice it low at first.\nprice it low at first.", 3)
    assert result.startswith("lessons.md: appended, 2 line(s) already noted (")
    assert memory.read("lessons") == before + "- [#c3] Price it low at first.\n"
    assert versions() == count + 1


def test_the_agent_hears_already_noted_as_a_tool_result(data_dir: Path) -> None:
    lesson = ("memory_update", {"file": "lessons", "mode": "append", "content": "Ask people first."})
    agent, _ = make_agent(
        data_dir,
        [plan(steps=["note it"]), tools(lesson), tools(lesson), text("Noted."), text("Reflected.")],
    )
    agent.run_cycle("schedule")
    results = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'memory_update' ORDER BY id")
    assert results[0]["status"] == "ok" and "appended" in results[0]["result"]
    assert results[1]["status"] == "ok" and results[1]["result"].startswith("already noted")
    assert agent.memory().read("lessons").count("Ask people first.") == 1
