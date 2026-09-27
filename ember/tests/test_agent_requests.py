"""Every request the agent builds can be priced by the guard and fits the money reserved for it."""

from __future__ import annotations

import pytest

from app.agent import context, prompts, tools
from app.config import Settings
from app.economy.estimate import plan_request
from app.economy.metering import rough_token_count
from app.economy.pricing import LAST_WILL, PLANNER_OPENING

SETTINGS = Settings(agent_name="X" * 40)


def filler(budget: int) -> str:
    # Multibyte text is the worst case per byte for the guard's token count.
    return context.cut(("ä" * 40 + "\n") * (budget // 20), budget)


def biggest_planner_context() -> str:
    parts = [f"== {k.upper()} ==\n{filler(v)}" for k, v in context.PLANNER_BUDGETS.items()]
    return "\n\n".join([*parts, "== TASK ==\nPlan this wake cycle. Reply with the JSON plan only."])


def test_profiles_cover_the_biggest_requests_without_much_slack() -> None:
    planner = rough_token_count(prompts.plan_request(SETTINGS, biggest_planner_context()))
    will = rough_token_count(prompts.will_request(SETTINGS, filler(context.WILL_BUDGET)))
    assert planner <= PLANNER_OPENING.input_tokens <= planner * 1.15
    assert will <= LAST_WILL.input_tokens <= will * 1.15
    assert PLANNER_OPENING.max_tokens >= prompts.PLAN_MAX_TOKENS
    assert LAST_WILL.max_tokens >= prompts.WILL_MAX_TOKENS


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
