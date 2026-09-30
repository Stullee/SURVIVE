"""Regression tests for the phase-2 review findings."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.economy.clock import owner_timezone
from app.economy.ledger import Scope
from app.economy.metering import CallFailed, CallRefused, Completed, Interrupted, ProcessLock
from app.economy.pricing import safety_factor
from app.economy.service import Economy
from tests.economy_helpers import (
    ScriptedTransport,
    make_economy,
    message,
    metered,
    owner,
    request,
    restart,
)

ROOMY = Settings(starting_balance_usd=50, daily_spend_cap_usd=0.05, cycle_spend_cap_usd=0.05)


def key() -> str:
    return uuid.uuid4().hex


def spend(economy: Economy, calls: int = 1, output: int = 200) -> None:
    model, transport = metered(economy)
    transport.outcomes = [Completed(message(1_000, output)) for _ in range(calls)]
    cycle = model.open_cycle("test")
    for _ in range(calls):
        model.call(cycle, "work", request(max_tokens=max(output, 1_000)))
    model.close_cycle(cycle)


# --- refunds and the daily cap ---


def test_a_refund_never_adds_room_under_the_daily_cap(data_dir: Path) -> None:
    economy = make_economy(data_dir, ROOMY)
    spend(economy, calls=2)  # 0.008 USD today
    # Refunds are limited to the API cost recorded on their day...
    too_big = economy.record(
        "api-correction", {"amount": "5", "direction": "decrease", "note": "x", "idempotency_key": key()}
    )
    assert (too_big.status, too_big.body["field"]) == (422, "amount")
    assert "only $0.00" in too_big.body["error"]
    # ...and even an allowed refund doesn't lower what counts toward today's cap.
    live = restart(
        economy, Settings(dry_run=False, starting_balance_usd=50, daily_spend_cap_usd=0.05, cycle_spend_cap_usd=0.05)
    )
    model, _ = metered(live)
    cycle = model.open_cycle("test")
    model.call(cycle, "work", request(max_tokens=1_000))
    today = live.books.cap_spend_on(live.life.scope(), live.clock.today())
    reply = live.record(
        "api-correction", {"amount": "0.01", "direction": "decrease", "note": "x", "idempotency_key": key()}
    )
    assert reply.status == 422  # 0.004 recorded today, a 0.01 refund doesn't fit
    assert live.books.cap_spend_on(live.life.scope(), live.clock.today()) == today


def test_cap_spend_ignores_refunds_but_counts_increases(data_dir: Path) -> None:
    economy = make_economy(
        data_dir, Settings(dry_run=False, starting_balance_usd=50, daily_spend_cap_usd=1, cycle_spend_cap_usd=1)
    )
    spend(economy, calls=3)  # 0.012 USD
    owner(economy, "api-correction", "0.01", direction="decrease")
    owner(economy, "api-correction", "0.02", direction="increase")
    day = economy.clock.today()
    assert economy.books.api_spend_on(Scope("live"), day) == 12_000 - 10_000 + 20_000
    assert economy.books.cap_spend_on(Scope("live"), day) == 12_000 + 20_000
    assert economy.dashboard()["agent"]["today_spend_usd"] == 0.032


# --- settling ---


def test_interrupted_call_known_to_cost_more_than_its_estimate_is_an_overrun(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    outcome = Interrupted("stream broke", partial_usage={"input_tokens": 50_000, "output_tokens": 1_000})
    model, _ = metered(economy, ScriptedTransport(outcomes=[outcome]))
    cycle = model.open_cycle("test")
    with pytest.raises(CallFailed) as failed:
        model.call(cycle, "work", request(max_tokens=1_000))
    result = failed.value.result
    assert result.cost_micros == 110_000 and result.overrun
    assert safety_factor(economy.db, "claude-sonnet-5", "dry_run") > 1
    with pytest.raises(CallRefused, match="stopped"):
        model.call(cycle, "work", request())


@pytest.mark.parametrize("usage", [None, {}, {"output_tokens": 5}, {"input_tokens": 0, "output_tokens": 5}])
def test_a_missing_usage_block_is_charged_the_worst_case(data_dir: Path, usage: object) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    body = message()
    body["usage"] = usage
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(body)]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "work", request())
    assert result.billing_uncertain and result.cost_micros == result.estimate_micros


def test_a_dated_snapshot_answer_is_the_same_model(data_dir: Path) -> None:

    settings = Settings(  # 0.12.0: claude-haiku-4-5 is in the default price table
        starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5, worker_model="claude-haiku-4-5"
    )
    economy = make_economy(data_dir, settings)
    answers = [
        Completed(message(1_000, 200, model="claude-haiku-4-5-20251001")),
        Completed(message(model="claude-haiku-4-5-x")),
    ]
    model, _ = metered(economy, ScriptedTransport(outcomes=answers))
    cycle = model.open_cycle("test")
    assert not model.call(cycle, "work", request(model="claude-haiku-4-5")).billing_uncertain
    assert model.call(cycle, "work", request(model="claude-haiku-4-5")).billing_uncertain


def test_learning_us_inference_is_not_an_overrun(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    outcome = Completed(message(1_000, 1_000, stop_reason="max_tokens", inference_geo="us"))
    model, _ = metered(economy, ScriptedTransport(outcomes=[outcome]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "work", request(max_tokens=1_000))
    assert result.cost_micros == 13_200 and not result.overrun
    assert safety_factor(economy.db, "claude-sonnet-5", "dry_run") == 1


def test_model_calls_refuse_to_run_inside_a_transaction(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    model, transport = metered(economy)
    cycle = model.open_cycle("test")
    with economy.db.transaction(), pytest.raises(RuntimeError, match="transaction"):
        model.call(cycle, "work", request())
    assert transport.sent == []


def test_a_stopped_economy_can_not_spend(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    economy.lock = ProcessLock(data_dir / "ember.lock")
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    economy.stop()
    with pytest.raises(CallRefused, match="another Ember process"):
        model.call(cycle, "work", request())


# --- starvation, uncertainty and critical ---


def test_worst_case_charges_alone_do_not_starve_the_agent(data_dir: Path) -> None:
    settings = Settings(dry_run=False, starting_balance_usd=0.10, daily_spend_cap_usd=5, cycle_spend_cap_usd=5)
    economy = make_economy(data_dir, settings)
    with economy.db.connection() as conn:
        conn.execute("UPDATE lives SET last_will_at = '2026-09-01T12:00:00Z'")
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    model.reserve(cycle, "work", request(max_tokens=7_000))  # 0.072 worst case, then the app restarts
    fresh = restart(economy)
    status = fresh.status()
    assert status.balance == 28_000 and status.settled_balance == 100_000
    model, _ = metered(fresh)
    cycle = model.open_cycle("wake")
    with pytest.raises(CallRefused, match="may still be refunded") as refused:
        model.call(cycle, "plan", request(max_tokens=3_000))
    assert refused.value.state != "dead" and fresh.status().state != "dead"
    assert any("charged at the worst case" in w for w in fresh.warnings())
    # The owner checks the Console and records the refund: that much of the uncertainty is settled.
    owner(fresh, "api-correction", "0.07", direction="decrease")
    status = fresh.status()
    assert status.settled_balance - status.balance == 72_000 - 70_000


def test_a_tiny_grant_does_not_end_starvation(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=0.02, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    model, _ = metered(economy)
    cycle = model.open_cycle("wake")
    with pytest.raises(CallRefused):
        model.call(cycle, "plan", request(max_tokens=3_000))
    assert economy.status().state == "critical"
    owner(economy, "grant", "0.01")
    status = economy.tick()
    assert status.state == "critical" and status.last_will_due
    owner(economy, "grant", "1")
    assert economy.tick().state == "alive"


# --- owner entries ---


def test_a_confirmation_covers_only_the_state_it_names(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=1, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    spend(economy, output=9_800)  # 0.10 USD
    body = {"amount": "0.95", "note": "domain", "idempotency_key": key()}
    first = economy.record("expense", body)
    assert first.body["state_after"] == "dead"
    critical_only = economy.record("expense", {**body, "confirm_state_change": "critical"})
    assert critical_only.status == 409 and critical_only.body["state_after"] == "dead"
    assert economy.record("expense", {**body, "confirm_state_change": True}).body["field"] == "confirm_state_change"
    assert economy.record("expense", {**body, "confirm_state_change": "dead"}).status == 201


def test_real_entries_in_dry_run_are_checked_against_the_live_agent(data_dir: Path) -> None:
    live_settings = Settings(dry_run=False, starting_balance_usd=1, daily_spend_cap_usd=5, cycle_spend_cap_usd=5)
    live = make_economy(data_dir, live_settings)
    spend(live, output=9_800)  # the live agent has spent money: 0.90 USD left
    live.clock.advance(minutes=1)
    dry = restart(live, Settings(dry_run=True, starting_balance_usd=1, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    owner(dry, "grant", "10", test_money=True)
    body = {"amount": "0.95", "note": "forgot to tick test money", "idempotency_key": key()}
    reply = dry.record("expense", body)
    assert reply.status == 409
    assert (reply.body["mode"], reply.body["state_after"]) == ("live", "dead")
    assert dry.record("expense", {**body, "confirm_state_change": "dead"}).status == 201
    with dry.db.connection() as conn:
        row = conn.execute("SELECT state, end_reason FROM lives WHERE mode = 'live'").fetchone()
    assert row["state"] == "dead"  # recorded when it happened, not at the next live start
    # Test money never asks about the live agent.
    assert (
        dry.record("expense", {"amount": "5", "note": "x", "test_money": True, "idempotency_key": key()}).status == 201
    )


def test_retrying_a_full_void_replays_it(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    grant = owner(economy, "grant", "10")["entry"]
    body = {"amount": "10", "note": "void", "idempotency_key": key(), "confirm_state_change": "dead"}
    assert economy.correct(grant["id"], body).status == 201
    again = economy.correct(grant["id"], dict(body))
    assert again.status == 200 and again.body["replay"] is True


def test_concurrent_corrections_get_a_conflict_not_an_error(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    grant = owner(economy, "grant", "5")["entry"]
    first = economy.books.prepare_correction(
        grant["id"], {"amount": "5", "note": "a", "idempotency_key": key()}, economy.life.scope()
    )
    second = economy.books.prepare_correction(
        grant["id"], {"amount": "5", "note": "b", "idempotency_key": key()}, economy.life.scope()
    )
    assert economy._write(first, "dead", True).status == 201
    reply = economy._write(second, "dead", True)
    assert (reply.status, reply.body["code"]) == (409, "conflict")


def test_test_money_does_not_raise_the_bar_for_real_typos(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    for _ in range(3):
        owner(economy, "grant", "5000", test_money=True)
    reply = economy.record("grant", {"amount": "40000", "idempotency_key": key()})
    assert reply.body["code"] == "unusually_large"


@pytest.mark.parametrize("direction", [["add"], {"add": 1}, 1])
def test_a_non_text_direction_is_a_validation_error(data_dir: Path, direction: object) -> None:
    economy = make_economy(data_dir)
    reply = economy.record("adjustment", {"amount": "1", "direction": direction, "note": "x", "idempotency_key": key()})
    assert (reply.status, reply.body["field"]) == (422, "direction")


def test_dry_run_sessions_do_not_depend_on_the_clock(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    owner(economy, "grant", "50", test_money=True)
    economy.clock.advance(minutes=1)
    live = restart(economy, Settings(dry_run=False))
    live.clock.advance(hours=-1)  # the clock is set back before the next dry run
    again = restart(live, Settings(dry_run=True))
    assert again.books.balance(again.life.scope()) == 20_000_000


# --- web and startup ---


def test_non_ascii_owner_names_survive(client_factory) -> None:  # noqa: ANN001
    with client_factory() as client:
        body = {"amount": "5", "idempotency_key": key()}
        # The Supervisor sends the name as UTF-8 bytes.
        headers = {"X-Ember-Request": "1", "X-Remote-User-Display-Name": "Jürgen Gulyás".encode()}
        entry = client.post("api/ledger/grant", json=body, headers=headers).json()["entry"]
    assert entry["entered_by"] == "Jürgen Gulyás"


def test_an_unusable_time_zone_falls_back_to_utc_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from datetime import UTC

    for name in ("America", "x" * 300, "Europe/Berln"):
        monkeypatch.setenv("TZ", name)
        with caplog.at_level(logging.ERROR, logger="app.economy.clock"):
            assert owner_timezone() is UTC
    assert len([r for r in caplog.records if "Unknown time zone" in r.getMessage()]) == 3


def test_an_economy_that_fails_to_start_keeps_the_dashboard_up(client_factory, monkeypatch) -> None:  # noqa: ANN001
    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Economy, "__init__", explode)
    with client_factory() as client:
        data = client.get("api/dashboard").json()
        assert data["agent"] is None and "disk I/O error" in data["system"]["economy_error"]
        assert client.get("api/sensors").json()["state"] == "unknown"


# --- hooks for the agent loop ---


def test_safety_factor_is_per_mode(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 5_000))]))
    cycle = model.open_cycle("test")
    assert model.call(cycle, "work", request(max_tokens=1_000)).overrun
    assert safety_factor(economy.db, "claude-sonnet-5", "dry_run") > 1
    assert safety_factor(economy.db, "claude-sonnet-5", "live") == 1  # a fake overrun says nothing about the API


def test_quote_and_headroom(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=1, cycle_spend_cap_usd=0.25))
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    assert model.quote(request(max_tokens=1_000)) == 12_000
    assert model.headroom(cycle) == 250_000
    model.call(cycle, "work", request(max_tokens=1_000))  # costs 4,000
    assert model.headroom(cycle) == 246_000
    with pytest.raises(Exception, match="max_tokens"):
        model.quote({"model": "claude-sonnet-5", "messages": []})


def test_cycles_only_end_with_known_statuses(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    with pytest.raises(ValueError, match="unknown cycle status"):
        model.close_cycle(cycle, "running")
    assert model.close_cycle(cycle, "idle") is True
    assert model.close_cycle(cycle, "completed") is False


def test_failed_calls_are_events_and_request_ids_are_kept(data_dir: Path) -> None:
    from app.economy.metering import Rejected

    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    outcomes = [Completed(message(), "req_ok"), Rejected(529, "overloaded", "req_529")]
    model, _ = metered(economy, ScriptedTransport(outcomes=outcomes))
    cycle = model.open_cycle("test")
    model.call(cycle, "work", request())
    with pytest.raises(CallFailed):
        model.call(cycle, "work", request())
    with economy.db.connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT request_id FROM llm_calls ORDER BY id")]
    assert ids == ["req_ok", "req_529"]
    assert any("failed: HTTP 529" in e["message"] for e in economy.db.recent_events(limit=10))


def test_recording_the_last_will(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    life = economy.status().life_id
    with economy.db.transaction() as conn:
        assert economy.life.record_last_will(conn, life, "2026-09-01T12:00:00Z") is True
        assert economy.life.record_last_will(conn, life, "2026-09-01T12:00:01Z") is False  # only once per life
    status = economy.status()
    assert status.last_will_at == "2026-09-01T12:00:00Z" and not status.last_will_due
