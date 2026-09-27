"""Turning Anthropic API usage into money.

Amounts are integer micro-USD ("micros", 1 USD = 1,000,000 micros) so sums are
exact. Prices in the options are USD per million tokens, which makes a price
exactly the cost of one token in micros. Costs are computed with Decimal and
rounded up to whole micros, so rounding never makes a call look cheaper.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..config import ModelPrice

MICROS_PER_USD = 1_000_000


def dec(value: float | int | str | Decimal) -> Decimal:
    """Exact decimal for a price or amount (2.5 stays 2.5, not 2.4999...)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def usd_to_micros(amount: Decimal | str | float) -> int:
    """Convert a USD amount to micros. Raises ValueError for non-finite or sub-micro precision."""
    try:
        value = dec(amount)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {amount!r}") from exc
    if not value.is_finite():
        raise ValueError("amount must be a finite number")
    micros = value * MICROS_PER_USD
    if micros != micros.to_integral_value():
        raise ValueError("amounts can have at most 6 decimal places")
    return int(micros)


def micros_to_usd(micros: int) -> float:
    """For display and JSON only; never compute with the result."""
    return float((Decimal(micros) / MICROS_PER_USD).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Usage:
    """Token and server-tool counts of one API response, normalised."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"usage field {name} must be a non-negative integer, got {value!r}")

    @classmethod
    def from_api(cls, usage: Mapping[str, Any]) -> Usage:
        """Build from the ``usage`` object of a Messages API response (as a dict).

        Every field may be missing or null. Cache writes are split into 5-minute
        and 1-hour writes when the response says so; any part of
        ``cache_creation_input_tokens`` the breakdown doesn't cover is charged at
        the 5-minute rate (streamed responses can under-report the breakdown).
        """

        def count(mapping: Mapping[str, Any] | None, key: str) -> int:
            value = (mapping or {}).get(key)
            return int(value) if value else 0

        creation = usage.get("cache_creation") or {}
        write_5m = count(creation, "ephemeral_5m_input_tokens")
        write_1h = count(creation, "ephemeral_1h_input_tokens")
        remainder = count(usage, "cache_creation_input_tokens") - write_5m - write_1h
        if remainder > 0:
            write_5m += remainder
        server = usage.get("server_tool_use") or {}
        return cls(
            input_tokens=count(usage, "input_tokens"),
            output_tokens=count(usage, "output_tokens"),
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
            cache_read_tokens=count(usage, "cache_read_input_tokens"),
            web_search_requests=count(server, "web_search_requests"),
            web_fetch_requests=count(server, "web_fetch_requests"),
        )

    def plus(self, other: Usage) -> Usage:
        """Sum of two usages (e.g. a response and its pause_turn continuation)."""
        return Usage(**{k: v + getattr(other, k) for k, v in asdict(self).items()})


def cost_micros(
    usage: Usage,
    price: ModelPrice,
    web_search_usd_per_1000: float | Decimal,
    multiplier: Decimal = Decimal(1),
) -> int:
    """What Anthropic charges for ``usage``, in micros, rounded up.

    ``multiplier`` covers price modifiers that apply to all tokens (for example
    1.1 for US-only inference). Web fetch has no per-request charge.
    """
    tokens = (
        usage.input_tokens * dec(price.input)
        + usage.cache_write_5m_tokens * dec(price.cache_write_5m)
        + usage.cache_write_1h_tokens * dec(price.cache_write_1h)
        + usage.cache_read_tokens * dec(price.cache_read)
        + usage.output_tokens * dec(price.output)
    )
    # USD per 1,000 searches -> micros per search = price * 1,000,000 / 1,000.
    searches = usage.web_search_requests * dec(web_search_usd_per_1000) * 1000
    total = tokens * dec(multiplier) + searches
    return int(total.to_integral_value(rounding=ROUND_CEILING))
