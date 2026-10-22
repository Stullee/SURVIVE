"""0.40.3 (analysis-0.40.1 codebase): the economy's fixes.

E1. The runway, and so the critical state, goes by the balance and the spending with the answered calls whose bill is
    uncertain at what they are known to cost, and a refund of API costs charged before a critical state began ends it
    as money in does.
E2. A system clock more than a day behind the last call is taken as right, with a warning, instead of refusing every
    call until the real time passes that call.
E3. The runway's window counts its first day's corrections in the share of that day's charges it holds.
E4. The reflection's reserve reads what the recent reflections are known to cost, and is at most its worst case.

Tests from 0.40.3 (formerly test_fixes_0403_economy.py)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.economy import burn
from app.economy.clock import to_iso
from app.economy.metering import CallFailed, CallRefused, Completed, Interrupted
from app.economy.service import Economy
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message, metered, owner, request, restart

LIVE = Settings(dry_run=False, starting_balance_usd=20, daily_spend_cap_usd=6, cycle_spend_cap_usd=6)


def calls(economy: Economy, outcomes: list[Completed | Interrupted], max_tokens: int, tokens: int = 1_000) -> None:
    """One cycle of model calls answered with ``outcomes``."""
    transport = ScriptedTransport(simulated=economy.mode == "dry_run", outcomes=outcomes, tokens=tokens)
    model, _ = metered(economy, transport)
    cycle = model.open_cycle("test")
    for _ in range(len(outcomes)):
        with contextlib.suppress(CallFailed):  # an answer that broke off
            model.call(cycle, "work", request(max_tokens=max_tokens))
    model.close_cycle(cycle)


def floors(economy: Economy) -> int:
    with economy.db.connection() as conn:
        return int(conn.execute("SELECT SUM(floor_micros) FROM llm_calls WHERE status = 'ok'").fetchone()[0])


# --- E1 ---


def test_answers_charged_at_their_worst_case_dont_make_ember_critical_on_money_that_is_there(data_dir: Path) -> None:
    economy = make_economy(data_dir, LIVE)
    for day in range(2):
        # each answer carries a usage field Ember's code doesn't know: booked at its worst case, $0.20, for $0.045
        unknown = [Completed(message(20_000, 500, some_new_field="x"), "r") for _ in range(22)]
        calls(economy, unknown, max_tokens=16_000, tokens=20_000)
        if day == 0:
            economy.clock.advance(days=1)
    status = economy.tick()
    # judged on what the answers are known to cost (until 0.40.3: $11.20 at $8.80 a day, 1.27 days, critical)
    assert status.state == "alive" and status.critical_since is None and not status.last_will_due
    assert status.balance == 11_200_000  # booked: the guard still holds the worst case
    assert status.runway.balance == status.settled_balance == 18_020_000
    assert status.runway.window_spend == floors(economy) == 1_980_000
    assert status.runway.days == pytest.approx(18.02 / 1.98)
    # the dashboard and the sensors show the runway the state is judged on
    assert economy.dashboard()["agent"]["runway_days"] == round(status.runway.days, 1)
    assert economy.sensors()["runway_days"] == round(status.runway.days, 1)
    [held] = [w for w in economy.warnings() if "charged at the worst case" in w]
    assert held.startswith("$6.82 of") and "The runway counts $6.82 of it" in held  # why it isn't the balance's


def test_a_refund_of_the_charges_that_made_ember_critical_ends_it_and_its_dormancy(data_dir: Path) -> None:
    economy = make_economy(data_dir, LIVE.model_copy(update={"starting_balance_usd": 5}))
    # three answers broke off: each charged its worst case, $1.00, and counted at that charge (it likely cost money)
    calls(economy, [Interrupted("the stream broke", None, "r") for _ in range(3)], max_tokens=99_800)
    status = economy.tick()
    assert status.runway.window_spend == 3_000_000 and status.balance == 2_000_000
    assert status.state == "critical" and status.last_will_due
    with economy.db.transaction() as conn:
        assert economy.life.record_last_will(conn, status.life_id, to_iso(economy.clock.now()))
    status = economy.tick()
    assert burn.peek(economy.db, status).mode == burn.DORMANT
    # the owner checks the Console: they cost $0.01, and refunds the rest on their day
    owner(economy, "api-correction", "2.99", direction="decrease", day=economy.clock.today().isoformat())
    status = economy.tick()
    assert status.runway.days is not None and status.runway.days >= 4
    assert status.state == "alive" and status.critical_since is None  # until 0.40.3: critical until a grant
    assert burn.peek(economy.db, status).mode == burn.EXPLORE


def test_a_refund_of_charges_made_since_ember_became_critical_doesnt_end_it(data_dir: Path) -> None:
    economy = make_economy(data_dir, LIVE.model_copy(update={"starting_balance_usd": 2.5}))
    calls(economy, [Completed(message(1_000, 99_800))], max_tokens=99_800)  # $1.00 in a day: 1.5 days of runway
    assert economy.tick().state == "critical"
    economy.clock.advance(days=30)  # no spending since: no runway figure
    calls(economy, [Interrupted("the stream broke", None, "r")], max_tokens=49_800)  # charged $0.50
    owner(economy, "api-correction", "0.49", direction="decrease", day=economy.clock.today().isoformat())
    status = economy.tick()
    assert status.runway.days is None or status.runway.days >= 4
    assert status.balance - status.pending >= (economy.life.revive_threshold() or 0)  # it could plan again
    assert status.state == "critical"  # it corrects a charge made since: not what made it critical
    owner(economy, "grant", "1")
    assert economy.tick().state == "alive"


# --- E2 ---


def test_a_clock_more_than_a_day_behind_the_last_call_is_taken_as_right_with_a_warning(data_dir: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 21, 9, 0, tzinfo=UTC))
    economy = make_economy(data_dir, Settings(starting_balance_usd=50), clock)
    model, _ = metered(economy)

    def plan() -> None:
        cycle = model.open_cycle("message")
        try:
            model.call(cycle, "plan", request())
        finally:
            model.close_cycle(cycle)

    clock.current = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # six hours ahead for a moment
    plan()
    clock.current = datetime(2026, 10, 21, 9, 5, tzinfo=UTC)
    with pytest.raises(CallRefused, match="the system clock went backwards"):  # within a day: it waits, as before
        plan()
    clock.current = datetime(2027, 10, 21, 9, 0, tzinfo=UTC)  # a year ahead for a moment
    plan()
    clock.current = datetime(2026, 10, 21, 9, 10, tzinfo=UTC)
    plan()  # until 0.40.3: refused until 2027-10-21
    [warning] = [w for w in economy.warnings() if "system clock" in w]
    assert "365 days behind the last model call (stamped 2027-10-21T09:00:00Z)" in warning
    assert any("took the clock as right" in e["message"] for e in economy.db.recent_events(limit=5))
    economy = restart(economy)
    model, _ = metered(economy)
    plan()  # after a restart too
    clock.current = datetime(2027, 10, 21, 8, 0, tzinfo=UTC)  # the real time an hour before the stamp it set aside
    plan()
    clock.current = datetime(2027, 10, 21, 7, 0, tzinfo=UTC)  # and a clock that goes back again is caught again
    with pytest.raises(CallRefused, match="the system clock went backwards"):
        plan()
    assert not [w for w in economy.warnings() if "system clock" in w]  # the warning is for a week


def test_a_clock_within_the_tolerance_is_no_jump(data_dir: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 21, 9, 0, tzinfo=UTC))
    economy = make_economy(data_dir, Settings(starting_balance_usd=50), clock)
    model, _ = metered(economy)
    cycle = model.open_cycle("message")
    model.call(cycle, "plan", request())
    clock.current -= timedelta(seconds=30)
    model.call(cycle, "work", request())
    model.close_cycle(cycle)
    assert not [w for w in economy.warnings() if "system clock" in w]


# --- E3 ---


def test_the_runway_counts_the_first_days_refund_in_the_share_of_its_charges_it_holds(data_dir: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 1, 6, 0, tzinfo=UTC))
    economy = make_economy(data_dir, LIVE.model_copy(update={"starting_balance_usd": 20}), clock)
    calls(economy, [Completed(message(1_000, 99_800))], max_tokens=99_800)  # $1.00 at 06:00
    clock.advance(hours=12)
    calls(economy, [Completed(message(1_000, 99_800))], max_tokens=99_800)  # $1.00 at 18:00
    owner(economy, "api-correction", "1", direction="decrease", day="2026-09-01")
    clock.current = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)  # the window holds the 18:00 call, half of the day's
    status = economy.status()
    # until 0.40.3 the whole $1.00 refund counted against it: no spending in the last 7 days
    assert status.runway.window_spend == 500_000
    assert status.runway.days == pytest.approx(19.0 / (0.50 / 7))
    clock.current = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)  # the whole day in the window: the whole refund
    assert economy.status().runway.window_spend == 1_000_000


# --- E4 ---


def test_the_reflections_reserve_reads_what_they_cost_and_is_at_most_its_worst_case(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=50, cycle_spend_cap_usd=50))
    model, transport = metered(economy, ScriptedTransport(simulated=True, tokens=1_000))
    cycle = model.open_cycle("test")
    ask = request(max_tokens=2_000)
    worst = model.quote(ask, "reflect")
    assert worst == 22_000
    # five reflections of 6,000 each, booked at their worst case for an unknown usage field
    transport.outcomes = [Completed(message(1_000, 400, some_new_field="x"), "r") for _ in range(5)]
    for _ in range(5):
        assert model.call(cycle, "reflect", ask).cost_micros == worst
    assert model.reflection_reserve(1_000, "claude-sonnet-5", worst) == 9_000  # until 0.40.3: 33,000
    transport.outcomes = [Completed(message(1_000, 1_900), "r") for _ in range(20)]  # 21,000 each
    for _ in range(20):
        model.call(cycle, "reflect", ask)
    assert model.reflection_reserve(1_000, "claude-sonnet-5") == 31_500
    assert model.reflection_reserve(1_000, "claude-sonnet-5", worst) == worst  # its worst case always pays for it
    model.close_cycle(cycle)


def test_research_is_expected_to_cost_what_it_is_known_to_cost(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    model, transport = metered(economy, ScriptedTransport(simulated=True, tokens=1_000))
    search = request(max_tokens=1_000, tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}])
    cycle = model.open_cycle("test")
    transport.outcomes = [Completed(message(1_000, 400, some_new_field="x"), "r") for _ in range(5)]  # 6,000 each
    for _ in range(5):
        model.call(cycle, "research", search)
    assert model.expected(search, "research", cycle) == 9_000  # 1.5 times what they cost, not what they were booked at
    model.close_cycle(cycle)
