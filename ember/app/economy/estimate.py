"""Worst-case cost of a model request, derived from the request itself.

The budget guard reserves this amount before a call is sent, so it must be an
upper bound for everything the request can make Anthropic bill. Anything in a
request that this module can't bound is refused rather than guessed at.

Web tools: each server tool use can make the API run the model again over the
prompt plus the results so far (a server-side loop), so a request with U tool
uses is priced as up to U + 2 sampling iterations (one spare for a tool error),
at most 10 (the API's own loop limit). Each tool result is assumed to add at
most an allowance of tokens; web_fetch is bounded by its ``max_content_tokens``
(required), web search results by ``SEARCH_RESULT_ALLOWANCE_TOKENS``. When the
request uses prompt caching, the API caches tool results itself, so in later
iterations the earlier context is priced as cache reads and only the new
results as cache writes. The remaining estimation risk (a search returning
more text than the allowance, a cache miss inside one loop) is detected after
the call: the actual cost is compared with the estimate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any

from ..config import ModelPrice
from .costs import dec

MAX_SERVER_ITERATIONS = 10
SEARCH_RESULT_ALLOWANCE_TOKENS = 8_000

# Request fields the estimate understands. Anything else could change the price
# (fallback models, fast mode, priority tier, US-only inference, MCP servers,
# code containers, compaction, ...) and is refused.
_ALLOWED_FIELDS = frozenset(
    {
        "model",
        "max_tokens",
        "messages",
        "system",
        "tools",
        "tool_choice",
        "thinking",
        "output_config",
        "metadata",
        "stop_sequences",
        "cache_control",
        "stream",
    }
)
_SEARCH_TOOLS = frozenset({"web_search_20250305", "web_search_20260209"})
_FETCH_TOOLS = frozenset({"web_fetch_20250910", "web_fetch_20260209"})
# The newer web tools can call code execution behind the scenes (extra, unbounded
# iterations) unless they are restricted to direct calls.
_DIRECT_ONLY = frozenset({"web_search_20260209", "web_fetch_20260209"})


class Unpriceable(ValueError):
    """The request contains something whose cost can't be bounded."""


@dataclass(frozen=True)
class Plan:
    """What the estimate needs to know about one request."""

    model: str
    input_tokens: int
    max_output_tokens: int
    cache_ttls: tuple[str, ...] = ()
    search_uses: int = 0
    fetch_uses: int = 0
    fetch_allowance_tokens: int = 0
    pending_searches: int = 0
    pending_fetches: int = 0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def tool_uses(self) -> int:
        return self.search_uses + self.fetch_uses + self.pending_searches + self.pending_fetches


def plan_request(request: Mapping[str, Any], input_tokens: int) -> Plan:
    """Check that ``request`` can be priced and describe it. Raises Unpriceable."""
    unknown = sorted(set(request) - _ALLOWED_FIELDS)
    if unknown:
        raise Unpriceable(f"request fields the budget guard can't price: {', '.join(unknown)}")
    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise Unpriceable("request has no model")
    max_tokens = request.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise Unpriceable("request needs a positive integer max_tokens")
    if not isinstance(input_tokens, int) or input_tokens < 0:
        raise Unpriceable("input token count is missing")

    search_uses = fetch_uses = fetch_allowance = 0
    for tool in request.get("tools") or []:
        kind = tool.get("type")
        if kind in (None, "custom"):
            continue  # a tool Ember runs itself: no extra charge from Anthropic
        if kind not in _SEARCH_TOOLS | _FETCH_TOOLS:
            raise Unpriceable(f"tool type {kind!r} is not supported by the budget guard")
        if kind in _DIRECT_ONLY and tool.get("allowed_callers") != ["direct"]:
            raise Unpriceable(f"{kind} must set allowed_callers ['direct']")
        uses = tool.get("max_uses")
        if not isinstance(uses, int) or isinstance(uses, bool) or uses < 1:
            raise Unpriceable(f"{kind} needs max_uses of at least 1")
        if kind in _SEARCH_TOOLS:
            search_uses += uses
        else:
            content = tool.get("max_content_tokens")
            if not isinstance(content, int) or isinstance(content, bool) or content <= 0:
                raise Unpriceable(f"{kind} needs max_content_tokens")
            fetch_uses += uses
            fetch_allowance = max(fetch_allowance, content)

    pending_searches, pending_fetches = _unresolved_server_tool_uses(request.get("messages") or [])
    if pending_fetches and not fetch_allowance:
        raise Unpriceable("an unfinished web_fetch continues in this request, but no web_fetch tool bounds it")
    return Plan(
        model=model,
        input_tokens=input_tokens,
        max_output_tokens=max_tokens,
        cache_ttls=tuple(sorted(_cache_ttls(request))),
        search_uses=search_uses,
        fetch_uses=fetch_uses,
        fetch_allowance_tokens=fetch_allowance,
        pending_searches=pending_searches,
        pending_fetches=pending_fetches,
    )


def worst_case_micros(
    plan: Plan,
    price: ModelPrice,
    web_search_usd_per_1000: float | Decimal,
    multiplier: Decimal = Decimal(1),
) -> int:
    """Upper bound of what ``plan`` can cost, in micros (rounded up)."""
    uses = plan.tool_uses
    iterations = 1 if uses == 0 else min(MAX_SERVER_ITERATIONS, uses + 2)
    first_rate = dec(price.input)
    if "5m" in plan.cache_ttls:
        first_rate = max(first_rate, dec(price.cache_write_5m))
    if "1h" in plan.cache_ttls:
        first_rate = max(first_rate, dec(price.cache_write_1h))
    allowance = max(
        SEARCH_RESULT_ALLOWANCE_TOKENS if plan.search_uses or plan.pending_searches else 0,
        plan.fetch_allowance_tokens,
    )
    prompt = plan.input_tokens
    tokens = prompt * first_rate
    for k in range(1, iterations):
        if plan.cache_ttls:
            tokens += (prompt + (k - 1) * allowance) * dec(price.cache_read) + allowance * dec(price.cache_write_5m)
        else:
            tokens += (prompt + k * allowance) * first_rate
    tokens += plan.max_output_tokens * dec(price.output)
    searches = (plan.search_uses + plan.pending_searches) * dec(web_search_usd_per_1000) * 1000
    total = tokens * dec(multiplier) + searches
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def _cache_ttls(request: Mapping[str, Any]) -> set[str]:
    ttls: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            marker = node.get("cache_control")
            if isinstance(marker, Mapping):
                ttls.add("1h" if marker.get("ttl") == "1h" else "5m")
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(request)
    return ttls


def _unresolved_server_tool_uses(messages: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    """Server tool calls the API started but hasn't run yet; they run at the start of this request.

    That happens after ``pause_turn`` (the assistant turn is the last message), and
    when a server tool and a client tool were called in parallel: the answer then
    stops for the client tool, and the deferred server call runs once the tool
    results are sent (the last message is a user turn of only tool results).
    """
    messages = list(messages)
    if not messages:
        return 0, 0
    last = messages[-1]
    if last.get("role") == "user" and len(messages) >= 2 and _only_tool_results(last.get("content")):
        last = messages[-2]
    if last.get("role") != "assistant":
        return 0, 0
    content = last.get("content")
    if not isinstance(content, list):
        return 0, 0
    answered = {block.get("tool_use_id") for block in content if isinstance(block, Mapping)}
    searches = fetches = 0
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "server_tool_use":
            continue
        if block.get("id") in answered:
            continue
        if block.get("name") == "web_fetch":
            fetches += 1
        else:
            searches += 1
    return searches, fetches


def _only_tool_results(content: Any) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, Mapping) and block.get("type") == "tool_result" for block in content)
    )
