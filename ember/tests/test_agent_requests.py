"""Every request the agent builds can be priced by the guard and fits the money reserved for it."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent import context, loop, prompts, tools
from app.agent.news import CHANGELOG_LIMIT, News
from app.config import Settings
from app.economy.estimate import plan_request
from app.economy.life import LifeStatus, Runway
from app.economy.metering import rough_token_count
from app.economy.pricing import LAST_WILL, PLANNER_OPENING, REFLECT, WORK

SETTINGS = Settings(agent_name="X" * 40)
HEADINGS = {"news": "SINCE YOUR LAST WAKE", "research": context.RESEARCH_HEADING, "lessons": "LESSONS (newest last)"}


def filler(budget: int) -> str:
    # Multibyte text is the worst case per byte for the guard's token count.
    return context.cut(("ä" * 40 + "\n") * (budget // 20), budget)


def biggest_planner_context() -> str:
    # Every section at its budget, RECENT RESEARCH included.
    parts = [f"== {HEADINGS.get(k, k.upper())} ==\n{filler(v)}" for k, v in context.PLANNER_BUDGETS.items()]
    return "\n\n".join([*parts, "== TASK ==\nPlan this wake cycle. Reply with the JSON plan only."])


def first_step_and_reflection(brief: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The first work step, and the reflection after it with the room the loop keeps for one step's growth."""
    grown = [{"role": "assistant", "content": [{"type": "text", "text": "x" * loop.STEP_GROWTH_BYTES}]}]
    return prompts.work_request(SETTINGS, brief, []), prompts.reflect_request(SETTINGS, brief, grown, [])


def test_profiles_cover_the_biggest_requests_without_much_slack() -> None:
    planner = rough_token_count(prompts.plan_request(SETTINGS, biggest_planner_context()))
    will = rough_token_count(prompts.will_request(SETTINGS, filler(context.WILL_BUDGET)))
    assert planner <= PLANNER_OPENING.input_tokens <= planner * 1.15
    assert will <= LAST_WILL.input_tokens <= will * 1.15
    assert PLANNER_OPENING.max_tokens >= prompts.PLAN_MAX_TOKENS
    assert LAST_WILL.max_tokens >= prompts.WILL_MAX_TOKENS
    for request, profile in zip(first_step_and_reflection(filler(context.BRIEF_MAX)), (WORK, REFLECT), strict=True):
        tokens = rough_token_count(request)
        assert tokens <= profile.input_tokens <= tokens * 1.15
        assert profile.max_tokens >= request["max_tokens"]
        assert plan_request(request, tokens).cache_ttls == profile.cache_ttls  # priced like the real request


def overflowing_snapshot() -> context.Snapshot:
    """Every section the planner and the brief show, far over its budget (rows as dicts)."""
    long = "\n".join(["ä" * 99] * 40)
    research = {
        "input": json.dumps({"question": "ä" * 500}),
        "result": f'<data src="research" id="0a1b2c">\n{"ä" * 2_000}\n</data id="0a1b2c">\n(cost $0.0100)',
    }
    decided = {
        "type": "publish",
        "title": "ä" * 120,
        "status": "approved_with_changes",
        "final_payload": "ä" * 8_000,
        "decision_comment": "ä" * 2_000,
    }
    project = {"status": "active", "title": "ä" * 80, "next_step": "ä" * 200, "hypothesis": "ä" * 400}
    return context.Snapshot(
        status=LifeStatus(
            mode="live", life_id=1, state="critical", reason="", last_will_due=True, runway=Runway(1, None)
        ),
        local_time="Wednesday 2026-09-30 10:00 CEST",
        version="10.10.10",
        agent_name=SETTINGS.agent_name,
        today_spend=10**9,
        daily_cap=1_000,
        cycle_cap=1_000,
        projects=[{"id": 1_000 + i, **project} for i in range(8)],  # type: ignore[misc]
        owner_messages=[{"id": i, "created_at": "2026-09-30T08:00:00Z", "text": "ä" * 2_000} for i in range(8)],  # type: ignore[misc]
        pending=[{"id": 1_000 + i, "type": "create_account", "title": "ä" * 120} for i in range(20)],  # type: ignore[misc]
        last_cycle={"id": 10_000, "status": "completed", "note": "ä" * 300},  # type: ignore[arg-type]
        last_journal={"summary": "ä" * 240},  # type: ignore[arg-type]
        memory={"strategy": long, "identity": long, "lessons": long},
        workspace=[f"{'ä' * 190}/{i}.md (65,536 B)" for i in range(40)],
        news=News(
            decided=[{"id": i, "version": 3, **decided} for i in range(10)],  # type: ignore[misc]
            changelog="ä" * CHANGELOG_LIMIT,
            running_version="10.10.10",
        ),
        research=[{"cycle_id": 10_000 + i, **research} for i in range(context.RESEARCH_CALLS)],  # type: ignore[misc]
    )


def test_the_real_contexts_stay_within_what_the_profiles_measure() -> None:
    snap = overflowing_snapshot()
    planner, _ = context.planner_context(snap, dry_run=True)
    assert f"== {context.RESEARCH_HEADING} ==" in planner and "== FROM YOUR OWNER ==" not in planner
    assert rough_token_count(prompts.plan_request(SETTINGS, planner)) <= PLANNER_OPENING.input_tokens
    plan = {"goal": "ä" * 300, "steps": ["ä" * 200] * 6}
    focus = {"id": 1_000, "title": "ä" * 80, "status": "active", "hypothesis": "ä" * 400, "next_step": "ä" * 200}
    brief, _ = context.brief(snap, True, plan, {**focus, "notes": "ä" * 2_000}, 100)  # type: ignore[arg-type]
    assert "== FROM YOUR OWNER ==" in brief and brief.endswith("bytes cut]")
    # The owner's and the research sections' room comes on top, even when the research itself is cut at the end.
    assert context.BRIEF_BUDGET < context.json_bytes(brief) <= context.BRIEF_MAX
    for request, profile in zip(first_step_and_reflection(brief), (WORK, REFLECT), strict=True):
        assert rough_token_count(request) <= profile.input_tokens


@pytest.mark.parametrize(
    "request_",
    [
        prompts.plan_request(SETTINGS, "context"),
        prompts.work_request(SETTINGS, "brief", []),
        prompts.work_request(SETTINGS, "brief", [], final=True),
        prompts.reflect_request(SETTINGS, "brief", [], []),
        prompts.will_request(SETTINGS, "context"),
        prompts.research_request(SETTINGS, "question", None),
        prompts.research_request(SETTINGS, "question", "https://example.com/page"),
    ],
)
def test_every_request_can_be_priced(request_: dict) -> None:
    plan = plan_request(request_, rough_token_count(request_))
    assert plan.model == "claude-sonnet-5"


def test_cache_breakpoints_stay_within_the_api_limit() -> None:
    def markers(node: object) -> int:
        if isinstance(node, dict):
            return ("cache_control" in node) + sum(markers(v) for v in node.values())
        if isinstance(node, list):
            return sum(markers(v) for v in node)
        return 0

    request = prompts.reflect_request(SETTINGS, "brief", [], [])
    assert markers(request) <= 4


def test_the_constitution_is_the_owners_text_with_the_name_filled_in() -> None:
    text = prompts.constitution(Settings(agent_name="Nova {x}"))
    assert text.startswith("You are Nova {x}, an autonomous AI agent")
    assert "MINDSET: SOLUTIONS, NOT OBSTACLES" in text


def test_tool_definitions_match_the_validation() -> None:
    for definition in tools.definitions():
        spec = tools.SPECS[definition["name"]]
        schema = definition["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(spec.fields)
        assert set(schema["required"]) == {n for n, f in spec.fields.items() if f.required}


def test_the_owners_knowledge_is_in_every_plan_and_work_step() -> None:
    known = prompts.knowledge()
    assert known.startswith("WHAT YOUR OWNER HAS LEARNED ABOUT THE OUTSIDE WORLD")
    for request in (prompts.plan_request(SETTINGS, "context"), prompts.work_request(SETTINGS, "brief", [])):
        assert any(block["text"] == known for block in request["system"])
    work = prompts.work_request(SETTINGS, "brief", [])["system"]
    assert "cache_control" in work[-1] and "cache_control" not in work[1]  # cached together with the rules
