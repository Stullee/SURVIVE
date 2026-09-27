"""Ledger math and the owner's entries."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.economy.ledger import Scope
from tests.economy_helpers import FakeClock, make_economy, metered, owner, request


def key() -> str:
    return uuid.uuid4().hex


def usd(micros: int) -> float:
    return micros / 1_000_000


def test_balance_math(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=20))
    owner(economy, "grant", "5")
    owner(economy, "revenue", "3.50")
    owner(economy, "expense", "1,25")
    owner(economy, "adjustment", "0.75", direction="subtract")
    owner(economy, "adjustment", "0.10", direction="add")
    owner(economy, "api-correction", "0.40", direction="increase")
    owner(economy, "api-correction", "0.15", direction="decrease")
    # 20 + 5 + 3.50 - 1.25 - 0.75 + 0.10 - 0.40 + 0.15
    assert usd(economy.books.balance(Scope("live"))) == pytest.approx(26.35)
    totals = economy.books.totals(Scope("live"))
    assert totals["api_cost"] == 250_000
    assert totals["grant"] == 25_000_000


def test_api_costs_reduce_the_dry_run_balance_only(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    result = model.call(cycle, "plan", request())
    assert result.cost_micros == 1_000 * 2 + 200 * 10
    assert economy.books.balance(economy.life.scope()) == 20_000_000 - result.cost_micros
    assert economy.books.balance(Scope("live")) == 20_000_000
    dashboard = economy.dashboard()
    assert dashboard["agent"]["real_balance_usd"] == 20.0
    assert dashboard["agent"]["today_spend_usd"] == usd(result.cost_micros)
    assert dashboard["economy"]["days"][-1]["api_cost_usd"] == usd(result.cost_micros)


def test_test_money_only_in_dry_run_and_only_in_its_session(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    owner(economy, "grant", "100", test_money=True)
    assert economy.books.balance(economy.life.scope()) == 120_000_000
    live = make_economy(data_dir, Settings(dry_run=False), clock=economy.clock)
    reply = live.record("grant", {"amount": "5", "test_money": True, "idempotency_key": key()})
    assert reply.status == 422 and reply.body["field"] == "test_money"
    assert live.books.balance(live.life.scope()) == 20_000_000
    # Back to dry run: a new test session starts without the old test money.
    economy.clock.advance(seconds=5)
    again = make_economy(data_dir, Settings(dry_run=True), clock=economy.clock)
    assert again.books.balance(again.life.scope()) == 20_000_000


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        (12.5, "as text"),
        ("1.000", "for one thousand type 1000"),
        ("1,000", "for one thousand type 1000"),
        ("12.345", "at most 2 decimals"),
        ("-5", "digits"),
        ("1e3", "digits"),
        ("NaN", "digits"),
        ("", "digits"),
        ("0", "can't be zero"),
        ("0.00", "can't be zero"),
        ("1234567", "digits"),
        ("١٢", "digits"),  # non-ASCII digits
    ],
)
def test_amount_validation(data_dir: Path, amount: object, message: str) -> None:
    economy = make_economy(data_dir)
    reply = economy.record("grant", {"amount": amount, "idempotency_key": key()})
    assert reply.status == 422
    assert reply.body["field"] == "amount"
    assert message in reply.body["error"]


@pytest.mark.parametrize(
    ("kind", "body", "field"),
    [
        ("revenue", {"amount": "5"}, "source"),
        ("expense", {"amount": "5"}, "note"),
        ("adjustment", {"amount": "5", "note": "x"}, "direction"),
        ("adjustment", {"amount": "5", "note": "x", "direction": "up"}, "direction"),
        ("api-correction", {"amount": "5", "note": "x", "direction": "add"}, "direction"),
        ("grant", {"amount": "5", "direction": "add"}, "direction"),
        ("grant", {"amount": "5", "currency": "GBP"}, "currency"),
        ("grant", {"amount": "5", "currency": "EUR"}, "fx_rate"),
        ("grant", {"amount": "5", "currency": "EUR", "fx_rate": "9"}, "fx_rate"),
        ("grant", {"amount": "5", "fx_rate": "1.1"}, "fx_rate"),
        ("grant", {"amount": "5", "day": "2026-09-02"}, "day"),
        ("grant", {"amount": "5", "day": "2025-08-30"}, "day"),
        ("grant", {"amount": "5", "day": "01.09.2026"}, "day"),
        ("grant", {"amount": "5", "day": "2026-02-30"}, "day"),
        ("grant", {"amount": "5", "note": "two\nlines"}, "note"),
        ("grant", {"amount": "5", "note": "x" * 501}, "note"),
        ("grant", {"amount": "5", "surprise": 1}, "surprise"),
        ("grant", {"amount": "5", "test_money": "yes"}, "test_money"),
        ("api-correction", {"amount": "5", "note": "x", "direction": "decrease", "test_money": True}, "test_money"),
    ],
)
def test_entry_validation(data_dir: Path, kind: str, body: dict, field: str) -> None:
    economy = make_economy(data_dir)
    reply = economy.record(kind, {**body, "idempotency_key": key()})
    assert (reply.status, reply.body["field"]) == (422, field), reply.body


@pytest.mark.parametrize("bad_key", [None, "", "abc", "A" * 32, "g" * 32, uuid.uuid4().hex + "0"])
def test_idempotency_key_format(data_dir: Path, bad_key: object) -> None:
    economy = make_economy(data_dir)
    reply = economy.record("grant", {"amount": "5", "idempotency_key": bad_key})
    assert (reply.status, reply.body["field"]) == (422, "idempotency_key")


def test_replays_and_key_reuse(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    body = {"amount": "5", "note": "for testing", "idempotency_key": key()}
    first = economy.record("grant", body)
    again = economy.record("grant", dict(body))
    assert (first.status, again.status) == (201, 200)
    assert again.body["entry"]["id"] == first.body["entry"]["id"]
    assert economy.books.balance(Scope("dry_run")) == 25_000_000
    # A retry after midnight without an explicit day is still the same entry.
    economy.clock.advance(days=1)
    assert economy.record("grant", dict(body)).status == 200
    changed = economy.record("grant", {**body, "amount": "6"})
    assert (changed.status, changed.body["code"]) == (409, "duplicate_key_mismatch")


def test_euro_entries_keep_the_original(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    entry = owner(economy, "revenue", "9.99", currency="EUR", fx_rate="1,0850")["entry"]
    assert entry["amount_usd"] == 10.84  # 9.99 * 1.085 = 10.839 -> 10.84
    assert (entry["orig_amount"], entry["fx_rate"]) == ("9.99", "1.085")


def test_backdated_entries_land_on_their_day(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    owner(economy, "revenue", "4", day="2026-08-25")
    days = {d["date"]: d for d in economy.dashboard()["economy"]["days"]}
    assert days["2026-08-25"]["revenue_usd"] == 4.0
    # The balance line already includes it on that day (the starting grant came later, today).
    assert days["2026-08-25"]["balance_usd"] == 4.0
    assert days["2026-09-01"]["balance_usd"] == 24.0


def test_corrections(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    grant = owner(economy, "grant", "10")["entry"]
    assert grant["can_correct"] is True and grant["remaining_usd"] == 10.0
    reply = economy.correct(grant["id"], {"amount": "4", "note": "typo", "idempotency_key": key()})
    assert reply.status == 201
    correction = reply.body["entry"]
    assert correction["amount_usd"] == -4.0 and correction["corrects_id"] == grant["id"]
    assert correction["occurred_on"] == grant["occurred_on"]
    assert economy.books.entry(grant["id"])["corrected_usd"] == -4.0
    too_much = economy.correct(grant["id"], {"amount": "6.01", "note": "x", "idempotency_key": key()})
    assert (too_much.status, too_much.body["field"]) == (422, "amount")
    no_note = economy.correct(grant["id"], {"amount": "1", "idempotency_key": key()})
    assert no_note.body["field"] == "note"
    # Corrections themselves, API costs and adjustments can't be corrected this way.
    assert economy.correct(correction["id"], {"amount": "1", "note": "x", "idempotency_key": key()}).status == 422
    adjustment = owner(economy, "adjustment", "1", direction="add")["entry"]
    assert adjustment["can_correct"] is False
    assert economy.correct(adjustment["id"], {"amount": "1", "note": "x", "idempotency_key": key()}).status == 422
    assert economy.correct(999, {"amount": "1", "note": "x", "idempotency_key": key()}).status == 404
    economy.correct(
        grant["id"], {"amount": "6", "note": "void", "idempotency_key": key(), "confirm_state_change": "dead"}
    )
    assert economy.books.entry(grant["id"])["can_correct"] is False
    assert economy.books.balance(Scope("dry_run")) == 21_000_000


def test_unusually_large_amounts_need_confirmation(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    reply = economy.record("grant", {"amount": "150", "idempotency_key": key()})
    assert (reply.status, reply.body["code"]) == (409, "unusually_large")
    assert reply.body["typical_usd"] is None
    assert economy.record("grant", {"amount": "99", "idempotency_key": key()}).status == 201
    # Above the 100 USD floor, up to ten times the usual amount is fine; more is asked about.
    huge = economy.record("grant", {"amount": "991", "idempotency_key": key()})
    assert huge.body["code"] == "unusually_large" and huge.body["typical_usd"] == 99.0
    assert economy.record("grant", {"amount": "990", "idempotency_key": key()}).status == 201
    confirmed = economy.record("grant", {"amount": "9999", "idempotency_key": key(), "confirm_large": True})
    assert confirmed.status == 201


def test_day_series_uses_owner_local_days(data_dir: Path) -> None:
    from zoneinfo import ZoneInfo

    from tests.economy_helpers import START

    # 23:30 in Berlin on 1 September is 21:30 UTC.
    clock = FakeClock(START.replace(hour=21, minute=30), ZoneInfo("Europe/Berlin"))
    economy = make_economy(data_dir, clock=clock)
    clock.advance(hours=1)  # now 00:30 on 2 September in Berlin
    owner(economy, "revenue", "2")
    days = economy.dashboard()["economy"]["days"]
    assert days[-1]["date"] == "2026-09-02" and days[-1]["revenue_usd"] == 2.0
    assert days[-2]["date"] == "2026-09-01" and days[-2]["grant_usd"] == 20.0


def test_starting_balance_is_recorded_once_and_never_in_safe_mode(data_dir: Path) -> None:
    economy = make_economy(data_dir, safe_mode=True)
    assert economy.books.balance(Scope("live")) == 0
    assert economy.status().state == "unfunded"
    fine = make_economy(data_dir, clock=economy.clock)
    again = make_economy(data_dir, clock=economy.clock)
    assert fine.books.balance(Scope("live")) == again.books.balance(Scope("live")) == 20_000_000

    other = data_dir / "other"
    other.mkdir()
    assert make_economy(other, Settings(starting_balance_usd=0)).status().state == "unfunded"
