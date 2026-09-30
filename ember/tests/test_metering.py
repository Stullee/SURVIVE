"""The budget guard: refusals, settling costs, interrupted calls, recovery."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.economy.metering import (
    CallFailed,
    CallRefused,
    Completed,
    Interrupted,
    NotSent,
    ProcessLock,
    Rejected,
    rough_token_count,
)
from app.economy.pricing import safety_factor
from app.economy.service import Economy
from tests.economy_helpers import ScriptedTransport, make_economy, message, metered, request, restart

GENEROUS = Settings(starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)


def calls(economy: Economy) -> list[dict]:
    with economy.db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id")]


def api_costs(economy: Economy) -> list[int]:
    with economy.db.connection() as conn:
        return [r[0] for r in conn.execute("SELECT amount_micros FROM ledger WHERE type = 'api_cost' ORDER BY id")]


def test_a_completed_call_is_charged_what_it_cost(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    usage = {"cache_creation_input_tokens": 3_000, "cache_read_input_tokens": 10_000}
    model, transport = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 500, **usage))]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "plan", request(max_tokens=2_000))
    # 1,000 x 2 + 3,000 x 2.5 + 10,000 x 0.2 + 500 x 10 micros
    assert result.cost_micros == 2_000 + 7_500 + 2_000 + 5_000
    assert result.estimate_micros == 1_000 * 2 + 2_000 * 10
    assert not result.billing_uncertain and not result.overrun
    assert api_costs(economy) == [result.cost_micros]
    row = calls(economy)[0]
    assert (row["status"], row["simulated"], row["cache_write_5m_tokens"]) == ("ok", 1, 3_000)
    assert len(transport.sent) == 1


def test_the_request_that_is_sent_is_the_one_that_was_priced(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    body = request()
    cycle = model.open_cycle("test")
    model.call(cycle, "plan", body)
    body["max_tokens"] = 1_000_000  # changing the caller's dict afterwards changes nothing
    assert transport.sent[0]["max_tokens"] == 1_000


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"model": "claude-sonnet-5", "messages": []}, "max_tokens"),
        (request(model="claude-opus-9"), "price table"),
        (request(speed="fast"), "can't price"),
        (request(tools=[{"type": "web_search_20250305", "name": "web_search"}]), "max_uses"),
        (request(tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 1}]), "direct"),
        (request(tools=[{"type": "bash_20250124", "name": "bash"}]), "not supported"),
        (request(tools=[{"type": "code_execution_20250825", "name": "run"}]), "plain code_execution tool"),
        (request(container="container_1"), "only used with the code execution tool"),
        (request(tools=[{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1}]), "max_content_tokens"),
    ],
)
def test_unpriceable_requests_are_refused(data_dir: Path, body: dict, reason: str) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    with pytest.raises(CallRefused, match=reason) as refused:
        model.call(cycle, "work", body)
    assert refused.value.category == "request"
    assert transport.sent == []
    assert calls(economy)[0]["status"] == "refused"


def test_cycle_cap(data_dir: Path) -> None:
    economy = make_economy(
        data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=0.05)
    )
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    model.call(cycle, "work", request(max_tokens=2_000))  # reserves 0.022, costs 0.004
    model.call(cycle, "work", request(max_tokens=4_000))  # 0.004 spent + 0.042 = 0.046 fits
    with pytest.raises(CallRefused, match="cycle cap") as refused:
        model.call(cycle, "work", request(max_tokens=5_000))  # 0.008 spent + 0.052 doesn't
    assert refused.value.category == "cap"
    assert len(transport.sent) == 2
    assert economy.status().state == "alive"  # caps never starve the agent


def test_daily_cap_counts_pending_calls_and_resets_at_midnight(data_dir: Path) -> None:
    economy = make_economy(
        data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=0.05, cycle_spend_cap_usd=0.05)
    )
    model, _ = metered(economy)
    first = model.open_cycle("test")
    held = model.reserve(first, "work", request(max_tokens=4_000))  # 0.042 pending, not settled yet
    model.close_cycle(first)
    second = model.open_cycle("test")
    with pytest.raises(CallRefused, match="daily cap"):
        model.call(second, "plan", request(max_tokens=1_000))
    model.finalize(held, Completed(message(1_000, 200)))
    model.close_cycle(second)
    economy.clock.advance(days=1)
    third = model.open_cycle("test")
    assert model.call(third, "plan", request(max_tokens=4_000)).status == "ok"


def test_balance_and_last_will_reserve(data_dir: Path) -> None:
    # 0.10 USD: a 0.082 call fits the balance but would leave less than the 0.023 last-will reserve.
    economy = make_economy(data_dir, Settings(starting_balance_usd=0.10, daily_spend_cap_usd=1, cycle_spend_cap_usd=1))
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    with pytest.raises(CallRefused, match="last will") as refused:
        model.call(cycle, "work", request(max_tokens=8_000))
    assert refused.value.category == "balance"
    assert economy.status().state == "alive"  # a work call being refused is not starvation
    with pytest.raises(CallRefused, match="not enough money"):
        model.call(cycle, "work", request(max_tokens=10_000))
    # The last will itself may use the reserve.
    assert model.call(cycle, "last_will", request(max_tokens=7_500)).status == "ok"


def test_nothing_runs_in_a_closed_or_foreign_cycle(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    with pytest.raises(CallRefused, match="still running"):
        model.open_cycle("test")
    model.close_cycle(cycle)
    with pytest.raises(CallRefused, match="completed"):
        model.call(cycle, "work", request())
    with pytest.raises(CallRefused, match="unknown wake cycle"):
        model.call(9999, "work", request())


def test_transport_must_match_the_mode(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    with pytest.raises(ValueError, match="mode"):
        economy.metered(ScriptedTransport(simulated=False))
    live = restart(economy, Settings(dry_run=False, starting_balance_usd=50))
    with pytest.raises(ValueError, match="mode"):
        live.metered(ScriptedTransport(simulated=True))


@pytest.mark.parametrize(
    ("outcome", "status", "charged"),
    [
        (NotSent("no route to host"), "failed", False),
        (Rejected(529, "overloaded"), "failed", False),
        (Interrupted("read timeout"), "interrupted", True),
    ],
)
def test_failed_calls(data_dir: Path, outcome: object, status: str, charged: bool) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy, ScriptedTransport(outcomes=[outcome]))
    cycle = model.open_cycle("test")
    with pytest.raises(CallFailed) as failed:
        model.call(cycle, "work", request())
    assert failed.value.result.status == status
    row = calls(economy)[0]
    assert row["status"] == status
    if charged:
        # Charged at the worst case; for death, only the known minimum (the prompt) counts.
        assert api_costs(economy) == [row["estimate_micros"]] and row["billing_uncertain"] == 1
        assert row["floor_micros"] == 1_000 * 2
        assert economy.books.provisional_excess(economy.life.scope()) == row["estimate_micros"] - 2_000
    else:
        assert api_costs(economy) == [] and row["cost_micros"] == 0


def test_a_transport_crash_is_an_interrupted_call(data_dir: Path) -> None:
    class Broken(ScriptedTransport):
        def send(self, request):  # noqa: ANN001, ANN201
            raise RuntimeError("bug")

    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy, Broken())
    cycle = model.open_cycle("test")
    with pytest.raises(CallFailed):
        model.call(cycle, "work", request())
    assert calls(economy)[0]["status"] == "interrupted"


@pytest.mark.parametrize(
    "usage",
    [
        {"server_tool_use": {"tool_search_requests": 2}},
        {"service_tier": "priority"},
        {"inference_geo": "eu"},
        {"brand_new_counter": 5},
        {"iterations": [{"type": "compaction", "input_tokens": 10, "output_tokens": 5}]},
    ],
)
def test_uncertain_bills_are_charged_at_the_worst_case(data_dir: Path, usage: dict) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 200, **usage))]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "work", request())
    assert result.billing_uncertain
    assert result.cost_micros == result.estimate_micros


def test_a_different_answering_model_is_uncertain(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    outcome = Completed(message(1_000, 200, model="claude-opus-5"))
    model, _ = metered(economy, ScriptedTransport(outcomes=[outcome]))
    cycle = model.open_cycle("test")
    assert model.call(cycle, "work", request()).billing_uncertain


def test_us_only_inference_costs_more_from_then_on(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    outcomes = [Completed(message(1_000, 200, inference_geo="us")), Completed(message(1_000, 200))]
    model, _ = metered(economy, ScriptedTransport(outcomes=outcomes))
    cycle = model.open_cycle("test")
    first = model.call(cycle, "work", request())
    assert first.cost_micros == 4_400 and not first.billing_uncertain
    second = model.call(cycle, "work", request())
    assert second.estimate_micros == 13_200  # 12,000 x 1.1


def test_an_overrun_stops_the_cycle_and_raises_the_safety_factor(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    # The call reports far more output than max_tokens allows (a broken estimate).
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 5_000))]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "work", request(max_tokens=1_000))
    assert result.overrun and result.cost_micros == 52_000
    assert safety_factor(economy.db, "claude-sonnet-5", "dry_run") == Decimal("4.77")  # 52,000 / 12,000 x 1.1
    with pytest.raises(CallRefused, match="stopped"):
        model.call(cycle, "work", request())
    events = economy.db.recent_events(limit=10)
    assert any("more than its worst-case estimate" in e["message"] for e in events)


def test_recovery_after_a_crash_charges_the_worst_case(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    held = model.reserve(cycle, "work", request())
    # The process dies here. The next start finds the pending call and the running cycle.
    fresh = restart(economy)
    row = calls(fresh)[0]
    assert (row["status"], row["cost_micros"], row["billing_uncertain"]) == ("interrupted", held.estimate, 1)
    assert api_costs(fresh) == [held.estimate]
    with fresh.db.connection() as conn:
        assert conn.execute("SELECT status FROM cycles").fetchone()[0] == "interrupted"
    # The old process can't settle it again.
    model.finalize(held, Completed(message()))
    assert api_costs(fresh) == [held.estimate]
    assert economy.health.broken is not None


def test_a_second_process_can_not_spend(data_dir: Path) -> None:
    first = ProcessLock(data_dir / "ember.lock")
    assert first.acquire()
    economy = make_economy(data_dir, GENEROUS)
    economy.lock = ProcessLock(data_dir / "ember.lock")
    economy.health.lock_held = economy.lock.acquire()
    assert economy.health.lock_held is False
    model, _ = metered(economy)
    with pytest.raises(CallRefused, match="another Ember process"):
        model.open_cycle("test")
    assert any("Another Ember process" in w for w in economy.warnings())
    first.release()


def test_a_backwards_clock_is_refused(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    model.call(cycle, "work", request())
    economy.clock.advance(minutes=-5)
    with pytest.raises(CallRefused, match="clock"):
        model.call(cycle, "work", request())


def test_thinking_token_details_are_not_an_unknown_charge(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    outcome = Completed(message(1_000, 200, output_tokens_details={"thinking_tokens": 150}))
    model, _ = metered(economy, ScriptedTransport(outcomes=[outcome]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "work", request())
    assert not result.billing_uncertain and result.cost_micros == 4_000


def test_a_failed_token_count_falls_back_to_a_rough_count(data_dir: Path) -> None:
    class NoCounting(ScriptedTransport):
        def count_tokens(self, request):  # noqa: ANN001, ANN201
            raise RuntimeError("rate limited")

    economy = make_economy(data_dir, GENEROUS)
    model, _ = metered(economy, NoCounting())
    cycle = model.open_cycle("test")
    result = model.call(cycle, "plan", request())
    assert result.status == "ok"
    assert result.estimate_micros == (rough_token_count(request()) * 2) + 1_000 * 10


def test_rough_token_count_is_generous() -> None:
    body = request()
    body["messages"] = [{"role": "user", "content": "word " * 1_000}]
    assert rough_token_count(body) >= 1_000 + 600


def test_only_two_modules_write_money() -> None:
    app_dir = Path(__file__).resolve().parent.parent / "app"
    writers = sorted(
        str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py") if "INSERT INTO ledger" in p.read_text("utf-8")
    )
    assert writers == ["economy/ledger.py", "economy/metering.py"]


def test_an_uncertain_charge_counts_what_it_is_known_to_cost_toward_the_caps(data_dir: Path) -> None:
    # 0.12.0: a 5xx before any reply was charged at the worst case against the caps and the venture share.
    economy = make_economy(data_dir, GENEROUS)
    scope = economy.life.scope()
    before = economy.books.balance(scope)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Interrupted("HTTP 502: bad gateway")]))
    cycle = model.open_cycle("test")
    with pytest.raises(CallFailed) as failed:
        model.call(cycle, "work", request(max_tokens=1_000))
    result = failed.value.result
    assert result.cost_micros == 12_000 and result.billing_uncertain  # the worst case: 1,000 in and 1,000 out
    assert economy.books.balance(scope) == before - 12_000  # the balance keeps the worst case
    assert economy.books.cycle_spend(cycle) == (12_000, 0)  # and so do the cycle's reports
    # the caps count what it is known to cost: its prompt, 1,000 tokens at $2 per million
    assert economy.books.cycle_spend(cycle, outside_cap=False) == (2_000, 0)
    assert economy.books.cap_spend_on(scope, economy.clock.today()) == 2_000
