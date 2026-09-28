"""What the owner wrote or decided reaches every step of the next cycle, and lessons aren't noted twice."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.agent import context
from app.agent.news import News
from app.config import Settings
from app.economy.life import LifeStatus
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


def shortened(text_: str, chars: int) -> str:
    """How a quoted text looks when it's cut to ``chars`` characters."""
    return (
        f"{json.dumps(text_[:chars] + '…', ensure_ascii=False)} ({len(text_) - chars:,} more characters; your owner "
        "has the full text)"
    )


def snapshot_with(
    messages: list[str], workspace: list[str] | None = None, decided: list[dict[str, Any]] | None = None
) -> context.Snapshot:
    """Just what the owner's section and the brief read, with dicts for rows."""
    return context.Snapshot(
        status=LifeStatus(mode="live", life_id=1, state="alive", reason=""),
        local_time="Monday 2026-09-28 10:00 CEST",
        version="0.4.0",
        agent_name="Ember",
        today_spend=0,
        daily_cap=5.0,
        cycle_cap=1.0,
        owner_messages=[{"created_at": "2026-09-28T08:00:00Z", "text": m} for m in messages],  # type: ignore[arg-type]
        workspace=workspace or [],
        news=News(decided=decided or []),  # type: ignore[arg-type]
    )


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
    for request in (work, reflect):  # messages first: they are what the agent must answer
        assert section(first_text(request), "FROM YOUR OWNER") == f"{line}\n{decided}"
    assert f"{line}\n{decided}" in (section(first_text(planned), "SINCE YOUR LAST WAKE") or "")


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


def test_a_message_at_the_owners_limit_reaches_every_step_whole(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [plan(steps=["Answer my owner"]), text("Answered."), text("Reflected.")])
    words = ("I read your guide twice, and here is what I think of it. " * 40)[: 1_999 - len(QUESTION)]
    message = f"{words} {QUESTION}"
    assert len(message) == 2_000 and owner(agent).send_message({"text": message}, "Stefan").status == 201
    line = message_line(agent, message)
    agent.run_cycle("schedule")
    planned, work, reflect = transport.sent
    assert section(first_text(planned), "SINCE YOUR LAST WAKE") == line
    for request in (work, reflect):
        assert section(first_text(request), "FROM YOUR OWNER") == line


def test_a_long_payload_leaves_room_for_the_owners_question(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir, [*cycle_with_approval(), plan(steps=["Answer my owner"]), text("Answered."), JOURNAL]
    )
    agent.run_cycle("schedule")
    approval_id = rows(agent, "SELECT id FROM approvals")[0]["id"]
    who = owner(agent)
    payload = " ".join(["Hier ist die neue Fassung, von einer KI geschrieben."] * 150)
    decision = {"decision": "approve_with_changes", "final_payload": payload, "comment": "Kürzer."}
    assert who.decide(approval_id, decision, "Stefan").status == 200
    assert who.send_message({"text": "Can you also make a German version?"}, "Stefan").status == 201
    line = message_line(agent, "Can you also make a German version?")
    sent_before = len(transport.sent)
    agent.run_cycle("schedule")

    planned, work, reflect = transport.sent[sent_before:]
    decided = (
        f'Request #{approval_id} (publish) "Post the guide": approved with changes. Use the owner\'s version, not '
        f'yours: {shortened(payload, context.QUOTE_CAP)}. Owner\'s comment: "Kürzer.". Your owner will carry it out '
        "and report back."
    )
    assert (section(first_text(planned), "SINCE YOUR LAST WAKE") or "").endswith(f"\n{line}\n{decided}")
    for request in (work, reflect):
        assert section(first_text(request), "FROM YOUR OWNER") == f"{line}\n{decided}"


def test_long_messages_share_the_owners_section(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [plan(steps=["read my owner's notes"]), text("Read."), text("Done.")])
    who = owner(agent)
    for letter in "abc":
        assert who.send_message({"text": f"{letter} " + "ä" * 1_900}, "Stefan").status == 201
    agent.run_cycle("schedule")
    planned, work, _ = transport.sent
    messages = rows(agent, "SELECT created_at, text FROM messages WHERE sender = 'owner' ORDER BY id")
    brief = first_text(work)
    owners = section(brief, "FROM YOUR OWNER") or ""
    chars = len(owners.split('"')[1]) - 1  # the characters shown of each message
    assert owners == "\n".join(
        f"Message from your owner ({m['created_at']}): {shortened(m['text'], chars)}" for m in messages
    )
    assert context.OWNER_BUDGET - 30 < context.json_bytes(owners) <= context.OWNER_BUDGET
    assert "== LIMITS ==\nAt most" in brief and "bytes cut]" not in brief
    news = (section(first_text(planned), "SINCE YOUR LAST WAKE") or "").splitlines()
    assert len(news) == 3 and all(line.endswith("more characters; your owner has the full text)") for line in news)


@pytest.mark.parametrize("letter", ["a", "ä", "你", "😀"])
def test_the_owners_section_keeps_its_budget_and_comes_on_top_of_the_brief(letter: str) -> None:
    message = letter * 2_000
    owners = context.owner_text(snapshot_with([message]), context.OWNER_BUDGET)
    head = "Message from your owner (2026-09-28T08:00:00Z): "
    if letter == "a":  # plain text: the whole message fits
        assert owners == f'{head}"{message}"'
    else:
        assert owners == head + shortened(message, len(owners.split('"')[1]) - 1)
        assert context.OWNER_BUDGET - 10 < context.json_bytes(owners) <= context.OWNER_BUDGET

    big_plan = {"goal": "g" * 300, "steps": ["s" * 200] * 6}
    workspace = [f"drafts/{'w' * 140}-{i}.md (1,234 B)" for i in range(20)]
    quiet = context.brief(snapshot_with([], workspace), False, big_plan, None, 12)
    loud = context.brief(snapshot_with([message], workspace), False, big_plan, None, 12)
    assert re.search(r"…\[\d+ bytes cut\]$", quiet) and context.json_bytes(quiet) <= context.BRIEF_BUDGET
    assert loud.replace(f"\n\n== FROM YOUR OWNER ==\n{owners}", "") == quiet  # the rest is as in a quiet cycle


def test_the_last_lines_are_cut_only_when_nothing_else_helps() -> None:
    decided = [
        {
            "id": i,
            "type": "publish",
            "title": f"Post {i}",
            "status": "approved_with_changes",
            "final_payload": "p" * 8_000,
            "decision_comment": "c" * 2_000,
        }
        for i in range(1, 11)
    ]
    message = "😀" * 2_000
    lines = context.owner_text(snapshot_with([message] * 8, decided=decided), context.OWNER_BUDGET).splitlines()
    assert context.json_bytes("\n".join(lines)) <= context.OWNER_BUDGET
    assert all(line.endswith(shortened(message, context.SHORTEST_QUOTE)) for line in lines[:8])
    assert re.fullmatch(r"…\[\d+ bytes cut\]", lines[-1]) and not any("Message" in line for line in lines[8:])


@pytest.mark.parametrize("letter", ["a", "ä", "你", "😀"])
def test_one_long_line_is_cut_to_its_budget(letter: str) -> None:
    cut = context.cut(letter * 3_000, 1_000)
    assert 950 < context.json_bytes(cut) <= 1_000 and re.fullmatch(rf"{letter}+\n…\[\d+ bytes cut\]", cut)


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
