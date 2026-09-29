"""What the owner wrote or decided reaches every step of the next cycle, and lessons aren't noted twice."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app import paths
from app.agent import context, loop, news, store, views
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


def message_line(agent: Any, text_: str, waiting: bool = False) -> str:
    """The line of the owner's newest message (``waiting``: shown before and not answered yet)."""
    newest = rows(agent, "SELECT id, created_at FROM messages WHERE sender = 'owner' ORDER BY id DESC")[0]
    note = ", not answered yet" if waiting else ""
    quoted = json.dumps(text_, ensure_ascii=False)
    line = f"Message #{newest['id']} from your owner ({newest['created_at']}{note}): {quoted}"
    assert re.fullmatch(
        r'Message #\d+ from your owner \(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ(, not answered yet)?\): ".*"', line
    )
    return line


def head_of(number: int, waiting: bool = False) -> str:
    """How the line of message ``number`` of snapshot_with begins."""
    return f"Message #{number} from your owner (2026-09-28T08:00:00Z{', not answered yet' if waiting else ''}): "


def shortened(text_: str, chars: int) -> str:
    """How a quoted text looks when it's cut to ``chars`` characters: its start and its end, the note between."""
    tail = chars * 2 // 5
    return (
        f"{json.dumps(text_[: chars - tail] + '…', ensure_ascii=False)} ({len(text_) - chars:,} more characters; "
        f"your owner has the full text) {json.dumps('…' + text_[len(text_) - tail :], ensure_ascii=False)}"
    )


def chars_shown(line: str, text_: str) -> int:
    """How many characters of ``text_`` the first shortened quote in ``line`` shows."""
    left_out = re.search(r"\(([\d,]+) more characters;", line)
    assert left_out is not None, line
    return len(text_) - int(left_out[1].replace(",", ""))


def snapshot_with(
    messages: list[str],
    workspace: list[str] | None = None,
    decided: list[dict[str, Any]] | None = None,
    research: list[dict[str, Any]] | None = None,
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
        owner_messages=[
            {"id": i, "created_at": "2026-09-28T08:00:00Z", "text": m}  # type: ignore[misc]
            for i, m in enumerate(messages, 1)
        ],
        workspace=workspace or [],
        news=News(decided=decided or []),  # type: ignore[arg-type]
        research=research or [],  # type: ignore[arg-type]
    )


def test_the_owners_question_reaches_the_brief_of_every_step(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            plan(steps=["Answer my owner's question with message_owner"]),
            tools(
                (
                    "message_owner",
                    {"text": "No, I can't make images; I could write the prompts for you.", "answers": "1"},
                )
            ),
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

    # Once answered, the message is not shown again.
    assert rows(agent, "SELECT answered_by FROM messages WHERE sender = 'owner'")[0]["answered_by"] == 2
    agent.run_cycle("schedule")
    assert all("== FROM YOUR OWNER ==" not in first_text(r) for r in transport.sent[4:])
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
    # The section, not the name: the release notes of the first wake mention it.
    assert all("== FROM YOUR OWNER ==" not in first_text(r) for r in transport.sent)


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


def test_a_message_longer_than_the_section_gets_its_room_and_the_others_follow(data_dir: Path) -> None:
    cycle = [plan(steps=["read my owner's notes"]), text("Read."), text("Done.")]
    agent, transport = make_agent(data_dir, cycle * 3)
    who = owner(agent)
    for letter in "abc":  # 3,800 bytes each: none fits the section whole
        assert who.send_message({"text": f"{letter} " + "ä" * 1_900}, "Stefan").status == 201
    messages = rows(agent, "SELECT id, created_at, text FROM messages WHERE sender = 'owner' ORDER BY id")
    for number, m in enumerate(messages):
        agent.run_cycle("schedule")
        planned, work, _ = transport.sent[-3:]
        head = f"Message #{m['id']} from your owner ({m['created_at']}): "
        # The oldest new message gets the room (three times what it had when they all shared it); the others wait,
        # the ones not answered yet too (0.9.1).
        first, *rest = (section(first_text(work), "FROM YOUR OWNER") or "").split("\n")
        assert (
            first == head + shortened(m["text"], chars_shown(first, m["text"]))
            and chars_shown(first, m["text"]) > 1_000
        )
        assert context.json_bytes("\n".join([first, *rest])) <= context.OWNER_BUDGET
        assert [bool(re.fullmatch(r"…\[\d+ bytes cut\]", line)) for line in rest] == [True]
        since = (section(first_text(planned), "SINCE YOUR LAST WAKE") or "").split("\n")
        news_ = [line for line in since if line.startswith("Message #")]  # the planner has less room
        assert news_ == [head + shortened(m["text"], chars_shown(news_[0], m["text"]))]
        assert seen_cycles(agent) == [1, 2, 3][: number + 1] + [None] * (2 - number)


@pytest.mark.parametrize("letter", ["a", "ä", "你", "😀"])
def test_the_owners_section_keeps_its_budget_and_comes_on_top_of_the_brief(letter: str) -> None:
    message = letter * 2_000
    owners = context.owner_text(snapshot_with([message]), context.OWNER_BUDGET)
    head = head_of(1)
    if letter == "a":  # plain text: the whole message fits
        assert owners == f'{head}"{message}"'
    else:
        assert owners == head + shortened(message, chars_shown(owners, message))
        assert context.OWNER_BUDGET - 10 < context.json_bytes(owners) <= context.OWNER_BUDGET

    big_plan = {"goal": "g" * 300, "steps": ["s" * 200] * 6}
    workspace = [f"drafts/{'w' * 140}-{i}.md (1,234 B)" for i in range(20)]
    venture = "\n".join(["v" * 99] * 30)  # a venture cycle's focus (0.10.0), cut to its own budget
    milestone = "\n".join(["m" * 99] * 15)  # a milestone's focus (0.11.0), cut to its own budget
    focus = {"venture_focus": venture, "milestone_focus": milestone}
    quiet, _ = context.brief(snapshot_with([], workspace), False, big_plan, None, 12, **focus)
    loud, shown = context.brief(snapshot_with([message], workspace), False, big_plan, None, 12, **focus)
    assert shown.items == {("message", 1, None)}
    assert re.search(r"…\[\d+ bytes cut\]$", quiet) and context.json_bytes(quiet) <= context.BRIEF_BUDGET
    assert loud.replace(f"\n\n== FROM YOUR OWNER ==\n{owners}", "") == quiet  # the rest is as in a quiet cycle


@pytest.mark.parametrize("letter", ["a", "😀"])
def test_the_oldest_message_keeps_its_room_and_the_last_lines_are_cut(letter: str) -> None:
    decided = [
        {
            "id": i,
            "version": 1,
            "type": "publish",
            "title": f"Post {i}",
            "status": "approved_with_changes",
            "final_payload": "p" * 8_000,
            "decision_comment": "c" * 2_000,
        }
        for i in range(1, 11)
    ]
    message = letter * 2_000
    snap = snapshot_with([message] * 8, decided=decided)
    lines = context.owner_text(snap, context.OWNER_BUDGET).splitlines()
    assert context.json_bytes("\n".join(lines)) <= context.OWNER_BUDGET
    head = head_of(1)
    if letter == "a":  # plain text: the oldest message fits whole
        assert lines[0] == f'{head}"{message}"' and len(lines) == 3
    else:  # as much of it as the section holds
        assert lines[0] == head + shortened(message, chars_shown(lines[0], message)) and len(lines) == 2
    assert all(  # as far as they fit
        line == head_of(number) + shortened(message, context.SHORTEST_QUOTE)
        for number, line in enumerate(lines[1:-1], 2)
    )
    assert re.fullmatch(r"…\[\d+ bytes cut\]", lines[-1])
    _, shown = context.brief(snap, False, {"goal": "g", "steps": ["s"]}, None, 12)
    assert shown.items == {("message", 1, None)}  # a shortened preview doesn't count: the rest stays news


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


# --- only what the agent was shown is marked seen ---


def owner_lines(text_: str) -> list[str]:
    """The whole lines of a section (a cut leaves a marker line instead of the rest)."""
    return [line for line in text_.splitlines() if not re.fullmatch(r"…\[\d+ bytes cut\]", line)]


def seen_cycles(agent: Any) -> list[int | None]:
    """When the owner's decisions, then their messages, were marked seen."""
    decided = rows(agent, "SELECT seen_cycle_id FROM approvals WHERE status <> 'pending' ORDER BY id")
    messages = rows(agent, "SELECT seen_cycle_id FROM messages WHERE sender = 'owner' ORDER BY id")
    return [r["seen_cycle_id"] for r in [*decided, *messages]]


@pytest.mark.parametrize("scale", loop.PLANNER_SCALES)
def test_a_long_message_then_a_long_payload_are_shown_and_marked_at_every_planner_scale(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, scale: float
) -> None:
    monkeypatch.setattr(loop, "PLANNER_SCALES", (scale,))
    answer = tools(("message_owner", {"text": "Yes, in German too.", "answers": "2"}))
    cycle = [plan(steps=["Answer my owner"]), answer, text("Answered."), JOURNAL]
    agent, transport = make_agent(data_dir, [*cycle_with_approval(), *cycle, *cycle])
    agent.run_cycle("schedule")
    approval_id = rows(agent, "SELECT id FROM approvals")[0]["id"]
    who = owner(agent)
    payload = ("Hier ist die neue Fassung, von einer KI geschrieben. " * 160)[:8_000]
    decision = {"decision": "approve_with_changes", "final_payload": payload, "comment": "Kürzer."}
    assert who.decide(approval_id, decision, "Stefan").status == 200
    message = f"{('I read your guide twice. ' * 80)[: 1_999 - len(QUESTION)]} {QUESTION}"
    assert len(message) == 2_000 and who.send_message({"text": message}, "Stefan").status == 201
    agent.run_cycle("schedule")

    planned, work, _, _ = transport.sent[-4:]
    since = owner_lines(section(first_text(planned), "SINCE YOUR LAST WAKE") or "")
    owners = owner_lines(section(first_text(work), "FROM YOUR OWNER") or "")
    assert context.json_bytes("\n".join(since)) <= context.PLANNER_BUDGETS["news"] * scale
    assert owners == [message_line(agent, message)]  # whole; the decision didn't fit beside it
    assert since[-1].startswith("Message #2 from your owner (") and since[-1].endswith(f' {QUESTION}"')
    assert seen_cycles(agent) == [None, 2]

    agent.run_cycle("schedule")  # the decision was left out: it is news for the next cycle; the message was answered
    planned, work, _, _ = transport.sent[-4:]
    since = owner_lines(section(first_text(planned), "SINCE YOUR LAST WAKE") or "")
    owners = owner_lines(section(first_text(work), "FROM YOUR OWNER") or "")
    for decided in (since[-1], *owners):
        assert decided.startswith(f'Request #{approval_id} (publish) "Post the guide": approved with changes.')
        assert decided.endswith("Your owner will carry it out and report back.")
    assert seen_cycles(agent) == [3, 2]


def test_what_the_plan_listed_and_the_brief_showed_in_full_is_marked_seen(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(context.PLANNER_BUDGETS, "news", 800)  # the plan has room for fewer than the brief
    agent, transport = make_agent(
        data_dir, [plan(steps=["Read my owner's notes"]), text("Read."), text("Done."), plan(steps=[], sleep=600)]
    )
    who = owner(agent)
    notes = [f"Note {number}: " + "x" * 300 for number in range(1, 7)]
    for note in notes:
        assert who.send_message({"text": note}, "Stefan").status == 201
    agent.run_cycle("schedule")

    planned, work, _ = transport.sent
    made = rows(agent, "SELECT id, created_at FROM messages ORDER BY id")
    owners = owner_lines(section(first_text(work), "FROM YOUR OWNER") or "")
    assert owners == [
        f"Message #{m['id']} from your owner ({m['created_at']}): {json.dumps(n)}"
        for m, n in zip(made, notes, strict=True)
    ]
    since = owner_lines(section(first_text(planned), "SINCE YOUR LAST WAKE") or "")
    assert since[0] == owners[0] and 1 < len(since) < 6  # the oldest whole, previews of the next, the rest cut
    assert all("more characters; your owner has the full text" in line for line in since[1:])
    assert seen_cycles(agent) == [1] * len(since) + [None] * (6 - len(since))  # the brief showed them whole
    listed = len(since)

    agent.run_cycle("schedule")  # a plan with nothing to do marks only what it showed whole
    head, *since = owner_lines(section(first_text(transport.sent[-1]), "SINCE YOUR LAST WAKE") or "")
    assert head.startswith("Last cycle #1")  # 0.12.0: its journal is in YOUR LAST CYCLE now
    whole = [line.endswith(json.dumps(note)) for line, note in zip(since, notes[listed:], strict=False)]
    assert whole[0] and not all(whole)
    assert seen_cycles(agent) == [1] * listed + [2] * sum(whole) + [None] * (6 - listed - sum(whole))


def test_a_shortened_message_stays_news_until_a_cycle_shows_it_whole(data_dir: Path) -> None:
    cycle = [plan(steps=["Answer my owner"]), text("Answered."), JOURNAL]
    agent, transport = make_agent(data_dir, cycle * 2)
    who = owner(agent)
    for letter in "AB":  # each fits the section whole, not both together
        assert who.send_message({"text": letter * 1_499 + "?"}, "Stefan").status == 201
    messages = rows(agent, "SELECT id, created_at, text FROM messages ORDER BY id")
    whole = [f"Message #{m['id']} from your owner ({m['created_at']}): {json.dumps(m['text'])}" for m in messages]
    agent.run_cycle("schedule")
    first, second = (section(first_text(transport.sent[1]), "FROM YOUR OWNER") or "").split("\n")
    assert first == whole[0] and second.endswith(
        shortened(messages[1]["text"], chars_shown(second, messages[1]["text"]))
    )
    assert seen_cycles(agent) == [1, None]  # the middle of the second wasn't shown yet
    agent.run_cycle("schedule")
    new, waiting = (section(first_text(transport.sent[-2]), "FROM YOUR OWNER") or "").split("\n")
    assert new == whole[1]  # the new one first, whole; the first follows: it wasn't answered (0.9.1)
    first = messages[0]
    assert waiting.startswith(f"Message #{first['id']} from your owner ({first['created_at']}, not answered yet): ")
    assert seen_cycles(agent) == [1, 2]


def test_a_plan_with_less_room_lists_a_long_message_but_leaves_it_to_the_brief() -> None:
    message = f"{('I read your guide twice. ' * 80)[: 1_999 - len(QUESTION)]} {QUESTION}"
    snap = snapshot_with([message])
    snap.last_cycle = {"id": 7, "status": "completed", "note": "n" * 240}  # type: ignore[assignment]  # news share it
    planner, planned = context.planner_context(snap, False)
    _, briefed = context.brief(snap, False, {"goal": "g", "steps": ["s"]}, None, 12)
    item = ("message", 1, None)
    assert "more characters; your owner has the full text" in (section(planner, "SINCE YOUR LAST WAKE") or "")
    assert (planned.listed, planned.items) == ({item}, set())  # a plan with nothing to do leaves it news
    assert briefed.items == {item}  # a work step marks it: the brief showed it whole


def test_a_question_at_the_end_of_a_long_message_stays_readable() -> None:
    messages = [f"{letter} " + "x" * 1_500 + f" {QUESTION}" for letter in "abc"]
    # The brief: the oldest whole, previews of the others; a plan at half scale: as much of the oldest as fits.
    for budget, count in ((context.OWNER_BUDGET, 3), (context.PLANNER_BUDGETS["news"] // 2, 1)):
        text_ = context.owner_text(snapshot_with(messages), budget)
        lines = owner_lines(text_)
        assert len(lines) == count and context.json_bytes(text_) <= budget
        for number, (message, line) in enumerate(zip(messages, lines, strict=False)):
            head = head_of(number + 1)
            if number == 0 and count == 3:
                assert line == f"{head}{json.dumps(message)}"
            else:
                assert line == head + shortened(message, chars_shown(line, message))  # its start and its end
            assert line.startswith(f'{head}"{message[:4]}') and line.endswith(f' {QUESTION}"')


def test_a_cut_changelog_is_not_marked_read(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text("## 0.4.0\n\n" + "\n".join(f"- Change {i}: you can do more." for i in range(40)), encoding="utf-8")
    monkeypatch.setattr(paths, "CHANGELOG_PATH", path)
    monkeypatch.setattr("app.agent.loop.app_version", lambda: "0.4.0")
    monkeypatch.setitem(context.PLANNER_BUDGETS, "software", 400)
    agent, transport = make_agent(data_dir, [plan(steps=[], sleep=600)] * 3)
    agent.db.set_meta(news.changelog_key("dry_run"), "0.3.0")
    agent.run_cycle("schedule")
    software = section(first_text(transport.sent[-1]), "YOUR SOFTWARE") or ""
    assert software.startswith("Your software was upgraded from 0.3.0 to 0.4.0") and software.endswith("bytes cut]")
    assert agent.db.get_meta(news.changelog_key("dry_run")) == "0.3.0"  # not read whole: shown again

    monkeypatch.setitem(context.PLANNER_BUDGETS, "software", news.CHANGELOG_LIMIT)
    agent.run_cycle("schedule")
    assert section(first_text(transport.sent[-1]), "YOUR SOFTWARE") == news.changelog_news(path, "0.3.0", "0.4.0")
    assert agent.db.get_meta(news.changelog_key("dry_run")) == "0.4.0"
    agent.run_cycle("schedule")
    assert "YOUR SOFTWARE" not in first_text(transport.sent[-1])


def test_a_long_changelog_keeps_its_newest_lines_and_fits_the_planner_whole(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    versions = [
        f"## 0.{minor}.0\n\n" + "\n".join(f"- Änderung {i} in 0.{minor}." for i in range(60)) for minor in (9, 8)
    ]
    path.write_text("\n\n".join(versions), encoding="utf-8")
    text_ = news.changelog_news(path, "0.7.0", "0.9.0")
    assert text_.startswith("Your software was upgraded from 0.7.0 to 0.9.0. What changed:\n\n## 0.9.0\n")
    assert text_.endswith(" in 0.8.\n…(older changes cut)") and "Änderung 59 in 0.9." in text_
    assert news.CHANGELOG_LIMIT - 40 < context.json_bytes(text_) <= news.CHANGELOG_LIMIT
    snap = snapshot_with([])
    snap.news = News(changelog=text_, running_version="0.9.0")
    planner, shown = context.planner_context(snap, False)
    assert section(planner, "YOUR SOFTWARE") == text_ and shown.changelog
    assert not context.planner_context(snap, False, 0.5)[1].changelog


def test_an_upgrade_names_its_version_quoted() -> None:
    upgrade = {"id": 2, "title": "RSS", "status": "released", "released_version": "0.4.0", "owner_note": None}
    lines = News(upgrades=[upgrade]).upgrade_lines()  # type: ignore[list-item]
    assert lines == ['Upgrade request #2 "RSS": released in version "0.4.0".']


# --- a message stays in the plans until the agent answers it (0.9.1) ---


def test_an_unanswered_message_stays_until_an_answer_names_it(data_dir: Path) -> None:
    # In live use a cycle ended before its reply, and the owner's questions were never shown again.
    busy = [plan(steps=["Answer my owner", "Build the planner"]), text("Built the planner."), JOURNAL]
    answer = tools(("message_owner", {"text": "No images: I make PDF and Word files.", "answers": "#1, 99"}))
    quiet = [plan(steps=["look around"]), text("Looked."), text("Reflected.")]
    agent, transport = make_agent(
        data_dir, [*busy, plan(steps=["Answer my owner"]), answer, text("Done."), JOURNAL, *quiet]
    )
    assert owner(agent).send_message({"text": QUESTION}, "Stefan").status == 201
    agent.run_cycle("schedule")  # shown, not answered
    assert rows(agent, "SELECT seen_cycle_id, answered_by FROM messages") == [{"seen_cycle_id": 1, "answered_by": None}]

    agent.run_cycle("schedule")  # shown again, as waiting for an answer, and answered
    planned, work, *_ = transport.sent[3:7]
    line = message_line(agent, QUESTION, waiting=True)
    assert line in (section(first_text(planned), "SINCE YOUR LAST WAKE") or "").splitlines()
    assert section(first_text(work), "FROM YOUR OWNER") == line
    result = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'message_owner'")[0]["result"]
    assert result == (
        "Message #2 is in your owner's inbox. It answers #1: they leave FROM YOUR OWNER. Not an open message from your "
        "owner that you were shown, so still as it was: #99."
    )
    assert rows(agent, "SELECT id, answered_by FROM messages ORDER BY id") == [
        {"id": 1, "answered_by": 2},
        {"id": 2, "answered_by": None},
    ]
    agent.run_cycle("schedule")  # answered: gone
    assert all("== FROM YOUR OWNER ==" not in first_text(r) for r in transport.sent[7:])

    inbox = {m["id"]: m for m in views.dashboard(agent)["inbox"]}
    assert (inbox[1]["answered_by"], inbox[2]["answered_by"]) == (2, None)
    with pytest.raises(sqlite3.IntegrityError, match="an answer is final"), agent.db.transaction() as conn:
        conn.execute("UPDATE messages SET answered_by = NULL WHERE id = 1")


def test_only_open_messages_the_agent_was_shown_can_be_answered(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)])
    who = owner(agent)
    scope = agent.scope()
    assert who.send_message({"text": "First"}, "Stefan").status == 201
    agent.run_cycle("schedule")  # a plan with nothing to do showed it whole: seen
    assert who.send_message({"text": "Second"}, "Stefan").status == 201
    with agent.db.transaction() as conn:
        reply = store.insert_message(conn, scope, 1, "My answer.", "2026-09-01T12:00:00Z")  # the agent's, cycle #1
        # #2 wasn't shown yet, #3 is the agent's own, #404 doesn't exist.
        assert store.mark_answered(conn, scope, [1, 2, reply, 404], reply) == [1]
        assert store.mark_answered(conn, scope, [1], reply) == []  # answered once, for good
        assert [r["id"] for r in store.open_messages(conn, scope)] == [2]
        conn.execute(
            "UPDATE messages SET removed_at = 'now', removed_by = 'Stefan', text = '[removed by the owner]'"
            " WHERE id = 2"
        )
        assert [r["id"] for r in store.open_messages(conn, scope)] == [2]  # removed: shown once all the same
        conn.execute("UPDATE messages SET seen_cycle_id = 1 WHERE id = 2")
        assert store.open_messages(conn, scope) == []  # then it needs no answer


def test_a_0_9_database_keeps_only_the_questions_that_were_never_answered(tmp_path: Path) -> None:
    from app.db import Database, discover_migrations, migrate  # noqa: PLC0415

    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 11], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        for cycle in (1, 2, 3):
            conn.execute(
                "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
                " VALUES (?, 1, 'b', 'then', 'completed', 'schedule', 0, 1)",
                (cycle,),
            )
        insert = (
            "INSERT INTO messages (id, mode, session, life_id, created_at, sender, cycle_id, text, seen_cycle_id)"
            " VALUES (?, 'live', 0, 1, 'then', ?, ?, ?, ?)"
        )
        conn.execute(insert, (1, "owner", None, "AdSense?", 1))  # seen in cycle 1, answered in cycle 2
        conn.execute(insert, (2, "agent", 2, "Here is my answer.", None))
        conn.execute(insert, (3, "owner", None, "More photos, please.", 3))  # seen in the cycle cut before its reply
        conn.execute(insert, (4, "owner", None, "Limit is 5 a day now.", None))  # not seen yet
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
    upgraded = Database(db_file)
    with upgraded.connection() as conn:
        answered = {r["id"]: r["answered_by"] for r in conn.execute("SELECT id, answered_by FROM messages")}
        assert answered == {1: 2, 2: None, 3: None, 4: None}
        scope = store.AgentScope(mode="live", session=0, life_id=1)
        assert [r["id"] for r in store.open_messages(conn, scope)] == [4, 3]  # the new one first
    upgraded.close()


def test_the_dry_runs_fake_answers_and_names_the_message(data_dir: Path) -> None:
    from app.agent.fake_llm import FakeTransport  # noqa: PLC0415
    from tests.test_loop_shapes import run  # noqa: PLC0415

    def asked(agent: Any) -> None:
        assert owner(agent).send_message({"text": QUESTION}, "Stefan").status == 201

    agent, _ = run(data_dir, FakeTransport(seed=1), before=asked)
    [question] = rows(agent, "SELECT answered_by FROM messages WHERE sender = 'owner'")
    reply = rows(agent, "SELECT id FROM messages WHERE sender = 'agent' ORDER BY id")[0]["id"]
    assert question["answered_by"] == reply
