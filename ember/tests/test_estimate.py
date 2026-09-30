"""Worst-case estimates: every request must be priced from what it can make Anthropic bill."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import DEFAULT_PRICE_TABLE
from app.economy.estimate import SEARCH_RESULT_ALLOWANCE_TOKENS, Unpriceable, plan_request, worst_case_micros

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
    # Up to 10 samplings, each writing up to max_tokens (0.14.0: it limits each sampling, not the loop); sampling k
    # re-reads the prompt, every result (16,000 tokens each since 0.12.0) and what the k-1 before it wrote.
    assert SEARCH_RESULT_ALLOWANCE_TOKENS == 16_000
    later = 9 * (1_000 + 2 * 16_000) + 1_000 * (1 + 2 + 3 + 4 + 5 + 6 + 7 + 8 + 9)
    assert worst_case_micros(plan, PRICE, 10) == (1_000 + later) * 2 + 10 * 1_000 * 10 + 2 * 10_000


def test_server_tool_loops_with_caching() -> None:
    body = base(tools=[search(1)], system=[{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}])
    plan = plan_request(body, input_tokens=1_000)
    reread = 9 * (1_000 + 16_000) + 1_000 * 45
    grown = 16_000 + 10 * 1_000  # every result and every sampling's output, written once
    tokens = 1_000 * 2.5 + reread * 0.2 + grown * 2.5 + 1_000 * (2.5 - 0.2)
    assert worst_case_micros(plan, PRICE, 10) == round(tokens) + 10 * 1_000 * 10 + 10_000


def test_one_search_is_priced_at_the_api_loop_limit() -> None:
    """Attempts beyond max_uses come back as error results and the loop keeps sampling (up to 10 times)."""
    body = base(max_tokens=2_000, tools=[search(1)])
    plan = plan_request(body, input_tokens=20_000)
    reviewed_real_bill = 577_600  # 20k prompt, one search, then retries until the loop limit
    assert worst_case_micros(plan, PRICE, 10) >= reviewed_real_bill


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


def test_server_calls_deferred_behind_client_tools_are_paid_by_the_next_request() -> None:
    messages = [
        {"role": "user", "content": "find it and note it"},
        {
            "role": "assistant",
            "content": [
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "a"}},
                {"type": "tool_use", "id": "toolu_1", "name": "workspace_write", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
    ]
    plan = plan_request(base(messages=messages), 500)
    assert plan.pending_searches == 1
    assert worst_case_micros(plan, PRICE, 10) > 500 * 2 + 1_000 * 10 + 10_000


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
        base(tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 0}]),
        base(tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 10**18}]),
        base(max_tokens=10**18),
        base(tools=["web_search"]),
        base(messages=[{"role": "user", "content": [{"type": "document", "source": {"type": "url", "url": "x"}}]}]),
        base(messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "file", "file_id": "f"}}]}]),
        base(system=[{"type": "text", "text": "x"}, {"type": "document", "source": {"type": "url", "url": "y"}}]),
    ],
)
def test_unpriceable_requests(body: dict) -> None:
    with pytest.raises(Unpriceable):
        plan_request(body, 100)


def test_inline_content_is_accepted() -> None:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR"}}
    plan = plan_request(base(messages=[{"role": "user", "content": [image]}]), 1_500)
    assert plan.input_tokens == 1_500


def test_direct_only_new_tools_are_accepted() -> None:
    plan = plan_request(base(tools=[search(1, version="web_search_20260209", allowed_callers=["direct"])]), 100)
    assert plan.search_uses == 1


def test_client_tools_cost_nothing_extra() -> None:
    tool = {"name": "workspace_read", "description": "read", "input_schema": {"type": "object"}}
    plan = plan_request(base(tools=[tool]), 100)
    assert worst_case_micros(plan, PRICE, 10) == 100 * 2 + 1_000 * 10
