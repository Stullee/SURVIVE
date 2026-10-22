"""Cost accounting: API usage to exact micro-dollars."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import ModelPrice
from app.economy.costs import Usage, cost_micros, micros_to_usd, usd_to_micros

SONNET = ModelPrice(
    model="claude-sonnet-5", input=2.0, output=10.0, cache_write_5m=2.5, cache_write_1h=4.0, cache_read=0.2
)


def test_plain_call() -> None:
    # 10,000 input tokens at $2/MTok = $0.02; 1,000 output at $10/MTok = $0.01.
    assert cost_micros(Usage(input_tokens=10_000, output_tokens=1_000), SONNET, 10) == 30_000


def test_cache_writes_reads_and_searches() -> None:
    usage = Usage(
        input_tokens=1_000,  # 2,000
        cache_write_5m_tokens=2_000,  # 5,000
        cache_write_1h_tokens=1_000,  # 4,000
        cache_read_tokens=10_000,  # 2,000
        output_tokens=500,  # 5,000
        web_search_requests=3,  # 3 x $0.01 = 30,000
        web_fetch_requests=2,  # free
    )
    assert cost_micros(usage, SONNET, 10) == 2_000 + 5_000 + 4_000 + 2_000 + 5_000 + 30_000


def test_rounds_up_never_down() -> None:
    cheap = ModelPrice(model="m", input=0.3, output=0.3, cache_write_5m=0.3, cache_write_1h=0.3, cache_read=0.03)
    assert cost_micros(Usage(cache_read_tokens=1), cheap, 10) == 1  # 0.03 micros -> 1
    assert cost_micros(Usage(input_tokens=3), cheap, 10) == 1  # 0.9 micros -> 1
    assert cost_micros(Usage(), cheap, 10) == 0


def test_no_binary_float_drift() -> None:
    odd = ModelPrice(model="m", input=0.1, output=0.7, cache_write_5m=0.125, cache_write_1h=0.2, cache_read=0.01)
    # With binary floats 0.1 * 3 != 0.3; the result here must be exact.
    assert cost_micros(Usage(input_tokens=3, output_tokens=10), odd, 0) == 8  # 0.3 + 7 = 7.3 -> 8
    assert cost_micros(Usage(input_tokens=30, output_tokens=10), odd, 0) == 10  # 3 + 7 = 10 exactly


def test_multiplier_applies_to_tokens_not_searches() -> None:
    usage = Usage(input_tokens=10_000, web_search_requests=1)
    assert cost_micros(usage, SONNET, 10, multiplier=Decimal("1.1")) == 22_000 + 10_000


def test_usage_from_api_response() -> None:
    usage = Usage.from_api(
        {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_creation_input_tokens": 5000,
            "cache_read_input_tokens": 800,
            "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 3000},
            "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
            "service_tier": "standard",
        }
    )
    # 1,000 tokens of cache writes aren't in the breakdown: charged at the 5-minute rate.
    assert usage == Usage(
        input_tokens=1200,
        output_tokens=300,
        cache_write_5m_tokens=2000,
        cache_write_1h_tokens=3000,
        cache_read_tokens=800,
        web_search_requests=2,
        web_fetch_requests=1,
    )


def test_usage_from_api_with_nulls() -> None:
    usage = Usage.from_api(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation": None,
            "server_tool_use": None,
        }
    )
    assert usage == Usage(input_tokens=10, output_tokens=5)
    # No breakdown at all: every cache write is charged at the 5-minute rate.
    assert Usage.from_api({"cache_creation_input_tokens": 700}).cache_write_5m_tokens == 700


def test_usage_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        Usage(input_tokens=-1)
    with pytest.raises(ValueError):
        Usage(output_tokens=1.5)  # type: ignore[arg-type]


def test_usage_plus() -> None:
    a = Usage(input_tokens=1, output_tokens=2, web_search_requests=1)
    b = Usage(input_tokens=10, cache_read_tokens=5)
    assert a.plus(b) == Usage(input_tokens=11, output_tokens=2, cache_read_tokens=5, web_search_requests=1)


@pytest.mark.parametrize(
    ("amount", "micros"),
    [("12.50", 12_500_000), ("0.000001", 1), (Decimal("3"), 3_000_000), ("-2.5", -2_500_000), (0.1, 100_000)],
)
def test_usd_to_micros(amount, micros: int) -> None:
    assert usd_to_micros(amount) == micros


@pytest.mark.parametrize("bad", ["0.0000001", "abc", "NaN", "Infinity", ""])
def test_usd_to_micros_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        usd_to_micros(bad)


def test_micros_to_usd() -> None:
    assert micros_to_usd(12_345_678) == 12.345678
    assert micros_to_usd(-1) == -0.000001
