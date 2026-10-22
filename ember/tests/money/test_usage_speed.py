"""usage.speed: an answer served at the standard speed is charged what it cost, and the calls charged at their
worst case for that field alone are settled at startup.

Tests from 0.39.8 (formerly test_fixes_0398.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.config import Settings  # noqa: E402
from app.economy import metering  # noqa: E402
from app.economy.metering import Completed  # noqa: E402
from app.economy.service import Economy  # noqa: E402
from tests.economy_helpers import (  # noqa: E402
    FakeClock,
    ScriptedTransport,
    make_economy,
    message,
    owner,
    request,
    restart,
)

# --- From 0.39.8 (formerly test_fixes_0398.py) ---


KNOWN = 1_000 * 2 + 200 * 10  # what each call below costs at the standard price: 1,000 in and 200 out on Sonnet 5


LIVE = Settings(dry_run=False, starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)


# --- 1. usage.speed ---


def live_economy(data_dir: Path) -> Economy:
    return make_economy(data_dir, LIVE, clock=FakeClock())


def speed_calls(economy: Economy, count: int, speed: str = "standard") -> list[metering.CallResult]:
    outcomes = [Completed(message(1_000, 200, speed=speed)) for _ in range(count)]
    model = economy.metered(ScriptedTransport(simulated=False, outcomes=outcomes))
    cycle = model.open_cycle("test")
    results = [model.call(cycle, "work", request()) for _ in range(count)]
    model.close_cycle(cycle)
    return results


def test_an_answer_at_the_standard_speed_is_charged_what_it_cost(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    [result] = speed_calls(economy, 1)
    assert not result.billing_uncertain
    assert result.cost_micros == KNOWN  # the price table's standard price
    with economy.db.connection() as conn:
        assert conn.execute("SELECT error FROM llm_calls").fetchone()[0] is None


def test_an_answer_at_another_speed_stays_uncertain(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    [result] = speed_calls(economy, 1, speed="fast")
    assert result.billing_uncertain and result.cost_micros == result.estimate_micros
    with economy.db.connection() as conn:
        assert conn.execute("SELECT error FROM llm_calls").fetchone()[0] == "speed fast"


def booked_as_before(economy: Economy, monkeypatch: pytest.MonkeyPatch, count: int) -> list[metering.CallResult]:
    """Calls answered with usage.speed "standard" as 0.39.7 charged them: at their worst case."""
    with monkeypatch.context() as patch:
        patch.setattr(metering, "_KNOWN_USAGE_KEYS", metering._KNOWN_USAGE_KEYS - {"speed"})  # noqa: SLF001
        results = speed_calls(economy, count)
    assert all(r.billing_uncertain and r.cost_micros == r.estimate_micros for r in results)
    return results


def refunds(economy: Economy) -> list[tuple[str, int, str]]:
    with economy.db.connection() as conn:
        return [
            (r["occurred_on"], r["amount_micros"], r["created_by"])
            for r in conn.execute(
                "SELECT occurred_on, amount_micros, created_by FROM ledger WHERE type = 'api_cost_correction'"
                " ORDER BY id"
            )
        ]


def test_calls_charged_at_their_worst_case_for_the_speed_alone_are_settled_at_startup(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    economy = live_economy(data_dir)
    first = booked_as_before(economy, monkeypatch, 2)
    economy.clock.advance(days=1)
    second = booked_as_before(economy, monkeypatch, 1)
    scope = economy.life.scope()
    excess = sum(r.cost_micros - KNOWN for r in first + second)
    assert economy.books.provisional_excess(scope) == excess
    cap_before = economy.books.cap_spend_on(scope, economy.clock.today())
    balance_before = economy.books.balance(scope)

    fresh = restart(economy)
    scope = fresh.life.scope()
    days = sorted({row[0] for row in refunds(fresh)})
    assert len(days) == 2  # one refund for each day the calls were made on
    assert sum(amount for _, amount, _ in refunds(fresh)) == -excess
    assert all(who == "system" for _, _, who in refunds(fresh))
    assert fresh.books.provisional_excess(scope) == 0
    assert fresh.books.balance(scope) == balance_before + excess
    assert fresh.books.cap_spend_on(scope, fresh.clock.today()) == cap_before  # the cap counted what they cost
    assert fresh.status().settled_balance == fresh.status().balance  # the System warning goes away
    assert any("settled at what they cost" in e["message"] for e in fresh.db.recent_events(limit=5))
    assert metering.settle_usage(fresh.db, fresh.clock, "speed", "standard") == 0  # once
    restart(fresh)
    assert len(refunds(fresh)) == 2


def test_the_owners_own_refund_since_then_counts_first(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    economy = live_economy(data_dir)
    results = booked_as_before(economy, monkeypatch, 3)
    excess = sum(r.cost_micros - KNOWN for r in results)
    owner(economy, "api-correction", "0.01", direction="decrease")  # the System warning asked for it
    balance = economy.books.balance(economy.life.scope())
    fresh = restart(economy)
    system = [amount for _, amount, who in refunds(fresh) if who == "system"]
    assert sum(system) == -(excess - 10_000)
    assert fresh.books.balance(fresh.life.scope()) == balance + excess - 10_000
    assert fresh.books.provisional_excess(fresh.life.scope()) == 0


def test_a_refund_larger_than_the_excess_leaves_nothing_to_settle(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    economy = live_economy(data_dir)
    [result] = booked_as_before(economy, monkeypatch, 1)
    assert result.cost_micros - KNOWN <= 10_000 <= result.cost_micros
    owner(economy, "api-correction", "0.01", direction="decrease")
    fresh = restart(economy)
    assert [who for _, _, who in refunds(fresh)] == ["owner"]
    assert metering.settle_usage(fresh.db, fresh.clock, "speed", "standard") == 0
