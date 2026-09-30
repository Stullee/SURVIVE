"""Worst-case cost of a model request, derived from the request itself.

The budget guard reserves this amount before a call is sent, so it must be an
upper bound for everything the request can make Anthropic bill. Anything in a
request that this module can't bound is refused rather than guessed at.

Web tools: each server tool use can make the API run the model again over the
prompt plus the results so far (a server-side loop). Tool attempts beyond
``max_uses`` come back as error results and the loop keeps going, so the only
hard limit on the number of samplings is the API's own (10, then
``pause_turn``). A request with server tools is therefore priced as 10
samplings, each re-reading at most the prompt, every tool result and all output
so far. Each tool result is assumed to add at most an allowance of tokens:
web_fetch is bounded by its ``max_content_tokens`` (required), web search
results by ``SEARCH_RESULT_ALLOWANCE_TOKENS``. When the request uses prompt
caching, the API caches the loop's context itself, so later samplings read it
from the cache and what is new is written once. Assumption (to verify with a
live call in phase 5): ``max_tokens`` bounds the output of the whole loop. The
remaining estimation risk (a result larger than its allowance, a cache miss
inside one loop) is detected after the call: the actual cost is compared with
the estimate.

Code execution (the workshop, 0.7.0) has no ``max_uses``: every sampling of the
loop may run code, so a request is priced as 10 samplings with one run each,
every run adding at most ``CODE_RESULT_ALLOWANCE_TOKENS`` (its output, a file
view). Its container is billed by time, not tokens: the worst case adds
``CONTAINER_ALLOWANCE_MINUTES`` at the owner's price per hour, which covers the
longest call the transport lets run plus the idle minutes before the container
is put away. A request may name the container of the call it continues.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any

from ..config import ModelPrice
from .costs import container_micros, dec

MAX_SERVER_ITERATIONS = 10
SEARCH_RESULT_ALLOWANCE_TOKENS = 16_000  # 0.12.0: 8,000 was exceeded live, and its overrun raised every estimate
CODE_RESULT_ALLOWANCE_TOKENS = 4_000
CONTAINER_MINIMUM_MINUTES = 5  # Anthropic bills container time with a 5-minute minimum
CONTAINER_ALLOWANCE_MINUTES = 35  # the transport's 30-minute limit per call, plus 5 idle minutes
# Sanity bounds: anything larger is a bug in the caller, not a request to price.
MAX_OUTPUT_TOKENS = 1_000_000
MAX_INPUT_TOKENS = 10_000_000
MAX_TOOL_USES = 100
# Content sources whose size is in the request itself (a URL or an uploaded file is billed by what it points to).
_INLINE_SOURCES = frozenset({"base64", "text", "content"})

# Request fields the estimate understands. Anything else could change the price
# (fallback models, fast mode, priority tier, US-only inference, MCP servers,
# compaction, ...) and is refused. A container is only accepted with the code
# execution tool, whose container time the estimate includes.
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
        "container",
    }
)
_SEARCH_TOOLS = frozenset({"web_search_20250305", "web_search_20260209"})
_CODE_TOOLS = frozenset({"code_execution_20250825", "code_execution_20260120", "code_execution_20260521"})
_CODE_TOOL_NAMES = frozenset({"bash_code_execution", "text_editor_code_execution", "code_execution"})
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
    code_execution: bool = False
    pending_code_runs: int = 0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def tool_uses(self) -> int:
        return self.search_uses + self.fetch_uses + self.pending_searches + self.pending_fetches

    @property
    def code_runs(self) -> int:
        """Code runs the loop can make: one per sampling, and those a paused turn left to run."""
        return (MAX_SERVER_ITERATIONS if self.code_execution else 0) + self.pending_code_runs


def plan_request(request: Mapping[str, Any], input_tokens: int) -> Plan:
    """Check that ``request`` can be priced and describe it. Raises Unpriceable."""
    unknown = sorted(set(request) - _ALLOWED_FIELDS)
    if unknown:
        raise Unpriceable(f"request fields the budget guard can't price: {', '.join(unknown)}")
    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise Unpriceable("request has no model")
    max_tokens = request.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 0 < max_tokens <= MAX_OUTPUT_TOKENS:
        raise Unpriceable(f"request needs max_tokens between 1 and {MAX_OUTPUT_TOKENS:,}")
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or not 0 <= input_tokens <= MAX_INPUT_TOKENS:
        raise Unpriceable("the prompt's token count is missing or implausible")
    _check_sources(request.get("system"))
    _check_sources(request.get("messages"))

    container = request.get("container")
    if container is not None and (not isinstance(container, str) or not 0 < len(container) <= 200):
        raise Unpriceable("container must be the id of an earlier call's container")
    search_uses = fetch_uses = fetch_allowance = 0
    code_execution = False
    tools = request.get("tools") or []
    if not isinstance(tools, list):
        raise Unpriceable("tools must be a list")
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise Unpriceable("every tool must be an object")
        kind = tool.get("type")
        if kind in (None, "custom"):
            continue  # a tool Ember runs itself: no extra charge from Anthropic
        if kind in _CODE_TOOLS:
            if tool.get("name") != "code_execution" or set(tool) - {"type", "name", "cache_control"}:
                raise Unpriceable(f"{kind} must be the plain code_execution tool")
            code_execution = True
            continue
        if kind not in _SEARCH_TOOLS | _FETCH_TOOLS:
            raise Unpriceable(f"tool type {kind!r} is not supported by the budget guard")
        if kind in _DIRECT_ONLY and tool.get("allowed_callers") != ["direct"]:
            raise Unpriceable(f"{kind} must set allowed_callers ['direct']")
        uses = tool.get("max_uses")
        if not isinstance(uses, int) or isinstance(uses, bool) or not 1 <= uses <= MAX_TOOL_USES:
            raise Unpriceable(f"{kind} needs max_uses between 1 and {MAX_TOOL_USES}")
        if kind in _SEARCH_TOOLS:
            search_uses += uses
        else:
            content = tool.get("max_content_tokens")
            if not isinstance(content, int) or isinstance(content, bool) or not 0 < content <= MAX_OUTPUT_TOKENS:
                raise Unpriceable(f"{kind} needs max_content_tokens")
            fetch_uses += uses
            fetch_allowance = max(fetch_allowance, content)

    pending_searches, pending_fetches, pending_code = _unresolved_server_tool_uses(request.get("messages") or [])
    if pending_fetches and not fetch_allowance:
        raise Unpriceable("an unfinished web_fetch continues in this request, but no web_fetch tool bounds it")
    if container is not None and not code_execution:
        raise Unpriceable("a container is only used with the code execution tool")
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
        code_execution=code_execution,
        pending_code_runs=pending_code,
    )


def worst_case_micros(
    plan: Plan,
    price: ModelPrice,
    web_search_usd_per_1000: float | Decimal,
    multiplier: Decimal = Decimal(1),
    container_usd_per_hour: float | Decimal = 0,
) -> int:
    """Upper bound of what ``plan`` can cost, in micros (rounded up)."""
    first_rate = dec(price.input)
    if "5m" in plan.cache_ttls:
        first_rate = max(first_rate, dec(price.cache_write_5m))
    if "1h" in plan.cache_ttls:
        first_rate = max(first_rate, dec(price.cache_write_1h))
    prompt = plan.input_tokens
    output = plan.max_output_tokens
    tokens = prompt * first_rate + output * dec(price.output)
    if plan.tool_uses or plan.code_runs:
        allowance = max(
            SEARCH_RESULT_ALLOWANCE_TOKENS if plan.search_uses or plan.pending_searches else 0,
            plan.fetch_allowance_tokens,
        )
        # Everything a later sampling can see beyond the prompt: all tool results (they may all
        # arrive at once, in parallel) and all output written so far.
        grown = plan.tool_uses * allowance + plan.code_runs * CODE_RESULT_ALLOWANCE_TOKENS + output
        later = MAX_SERVER_ITERATIONS - 1
        if plan.cache_ttls:
            read, write = dec(price.cache_read), dec(price.cache_write_5m)
            # Later samplings read the context from the cache; what's new is written once; and if the
            # prompt's own entry misses once inside the loop, it is written again.
            tokens += later * (prompt + grown) * read + grown * write + prompt * (write - read)
        else:
            tokens += later * (prompt + grown) * first_rate
    searches = (plan.search_uses + plan.pending_searches) * dec(web_search_usd_per_1000) * 1000
    total = tokens * dec(multiplier) + searches
    if plan.code_runs:
        total += Decimal(container_micros(CONTAINER_ALLOWANCE_MINUTES, container_usd_per_hour))
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def expected_micros(
    plan: Plan,
    price: ModelPrice,
    cached_tokens: int,
    multiplier: Decimal = Decimal(1),
    output_tokens: int | None = None,
) -> tuple[int, int]:
    """0.12.0: (what a request without server tools is expected to cost, what one cache miss would add), in micros.

    The part of its prompt the calls before it in the same conversation cached (``cached_tokens``) is read from the
    cache at the read rate; the rest is written at the write rate (at the input rate without caching); the output is
    ``output_tokens`` (what such calls wrote lately), at most the whole ``max_tokens``. Only the cycle cap and the
    envelopes count this: live, the worst case priced later steps at 6.1 times their cost and reflections at 3.8
    times. The daily cap, the balance and the last-will reserve still count the worst case."""
    if plan.tool_uses or plan.code_runs:
        raise ValueError("a request with server tools has no expected cost of its own")
    write = dec(price.input)
    if "5m" in plan.cache_ttls:
        write = max(write, dec(price.cache_write_5m))
    if "1h" in plan.cache_ttls:
        write = max(write, dec(price.cache_write_1h))
    cached = min(max(cached_tokens, 0), plan.input_tokens) if plan.cache_ttls else 0
    read = dec(price.cache_read)
    output = plan.max_output_tokens if output_tokens is None else min(max(output_tokens, 0), plan.max_output_tokens)
    tokens = cached * read + (plan.input_tokens - cached) * write + output * dec(price.output)
    miss = cached * (write - read)
    scale = dec(multiplier)
    return (
        int((tokens * scale).to_integral_value(rounding=ROUND_CEILING)),
        int((miss * scale).to_integral_value(rounding=ROUND_CEILING)),
    )


def _check_sources(node: Any) -> None:
    """Refuse images and documents given by URL or file id: their size isn't in the request."""
    if isinstance(node, Mapping):
        source = node.get("source")
        if isinstance(source, Mapping) and source.get("type") not in _INLINE_SOURCES:
            raise Unpriceable(f"content from a {source.get('type')!r} source can't be sized before it is sent")
        for value in node.values():
            _check_sources(value)
    elif isinstance(node, list):
        for value in node:
            _check_sources(value)


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


def _unresolved_server_tool_uses(messages: Iterable[Mapping[str, Any]]) -> tuple[int, int, int]:
    """Server tool calls the API started but hasn't run yet: (searches, fetches, code runs). They run at the start
    of this request.

    That happens after ``pause_turn`` (the assistant turn is the last message), and
    when a server tool and a client tool were called in parallel: the answer then
    stops for the client tool, and the deferred server call runs once the tool
    results are sent (the last message is a user turn of only tool results).
    """
    messages = list(messages)
    if not messages:
        return 0, 0, 0
    last = messages[-1]
    if last.get("role") == "user" and len(messages) >= 2 and _only_tool_results(last.get("content")):
        last = messages[-2]
    if last.get("role") != "assistant":
        return 0, 0, 0
    content = last.get("content")
    if not isinstance(content, list):
        return 0, 0, 0
    answered = {block.get("tool_use_id") for block in content if isinstance(block, Mapping)}
    searches = fetches = code = 0
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "server_tool_use":
            continue
        if block.get("id") in answered:
            continue
        if block.get("name") == "web_fetch":
            fetches += 1
        elif block.get("name") in _CODE_TOOL_NAMES:
            code += 1
        else:
            searches += 1
    return searches, fetches, code


def _only_tool_results(content: Any) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, Mapping) and block.get("type") == "tool_result" for block in content)
    )
