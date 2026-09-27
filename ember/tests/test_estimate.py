"""Worst-case estimates: every request must be priced from what it can make Anthropic bill."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import DEFAULT_PRICE_TABLE
from app.economy.estimate import Unpriceable, plan_request, worst_case_micros

PRICE = DEFAULT_PRICE_TABLE[0]  # claude-sonnet-5: 2 / 10, cache write 2.5 / 4, read 0.2


def base(**extra: object) -> dict:
    return {"model": "claude-sonnet-5", "max_tokens": 1_000, "messages": [], **extra}


def search(max_uses: int, version: str = "web_search_20250305", **extra: object) -> dict:
    return {"type": version, "name": "web_search", "max_uses": max_uses, **extra}


def test_plain_request() -> None:
    plan = plan_request(base(), input_tokens=2_000)
    assert worst_case_micros(plan, PRICE, 10) == 2_000 * 2 + 1_000 * 10


def test_cache_writes_are_priced_at_the_highest_ttl() -> None:
    body = base(system=[{"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
    plan = plan_request(body, input_tokens=2_000)
    assert plan.cache_ttls == ("1h",)
    assert worst_case_micros(plan, PRICE, 10) == 2_000 * 4 + 1_000 * 10


def test_server_tool_loops_without_caching() -> None:
    plan = plan_request(base(tools=[search(2)]), input_tokens=1_000)
    # 2 uses -> up to 4 iterations; each later one re-reads the prompt plus 8,000 tokens per earlier result.
    tokens = 1_000 + (1_000 + 8_000) + (1_000 + 16_000) + (1_000 + 24_000)
    assert worst_case_micros(plan, PRICE, 10) == tokens * 2 + 1_000 * 10 + 2 * 10_000


def test_server_tool_loops_with_caching() -> None:
    body = base(tools=[search(1)], system=[{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}])
    plan = plan_request(body, input_tokens=1_000)
    first = 1_000 * 2.5
    later = sum((1_000 + (k - 1) * 8_000) * 0.2 + 8_000 * 2.5 for k in (1, 2))
    assert worst_case_micros(plan, PRICE, 10) == int(first + later) + 1_000 * 10 + 10_000


def test_iterations_are_capped_at_the_api_limit() -> None:
    few = plan_request(base(tools=[search(8)]), 100)
    many = plan_request(base(tools=[search(50)]), 100)
    assert worst_case_micros(many, PRICE, 10) - 50 * 10_000 == worst_case_micros(few, PRICE, 10) - 8 * 10_000


def test_unfinished_server_tool_calls_are_paid_by_the_next_request() -> None:
    messages = [
        {"role": "user", "content": "find it"},
        {
            "role": "assistant",
            "content": [
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "a"}},
                {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
                {"type": "server_tool_use", "id": "srvtoolu_2", "name": "web_search", "input": {"query": "b"}},
            ],
        },
    ]
    plan = plan_request(base(messages=messages), 500)
    assert (plan.pending_searches, plan.pending_fetches) == (1, 0)


def test_multiplier_applies_to_tokens_not_searches() -> None:
    plan = plan_request(base(tools=[search(1)]), 1_000)
    plain = worst_case_micros(plan, PRICE, 10)
    assert worst_case_micros(plan, PRICE, 10, multiplier=Decimal("1.1")) == pytest.approx(
        (plain - 10_000) * 1.1 + 10_000, abs=1
    )


@pytest.mark.parametrize(
    "body",
    [
        base(max_tokens=0),
        base(max_tokens=True),
        base(model=""),
        base(inference_geo="us"),
        base(service_tier="priority"),
        base(container="c"),
        base(mcp_servers=[]),
        base(context_management={}),
        base(tools=[search(1, version="web_search_20260209")]),
        base(tools=[{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1}]),
        base(tools=[{"type": "bash_20250124", "name": "bash"}]),
        base(tools=[{"type": "web_search_20250305", "name": "web_search"}]),
    ],
)
def test_unpriceable_requests(body: dict) -> None:
    with pytest.raises(Unpriceable):
        plan_request(body, 100)


def test_direct_only_new_tools_are_accepted() -> None:
    plan = plan_request(base(tools=[search(1, version="web_search_20260209", allowed_callers=["direct"])]), 100)
    assert plan.search_uses == 1


def test_client_tools_cost_nothing_extra() -> None:
    tool = {"name": "workspace_read", "description": "read", "input_schema": {"type": "object"}}
    plan = plan_request(base(tools=[tool]), 100)
    assert worst_case_micros(plan, PRICE, 10) == 100 * 2 + 1_000 * 10
