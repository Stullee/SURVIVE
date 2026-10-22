"""Life states: unfunded, alive, critical (with hysteresis), paused, dead, revival, dry-run sessions."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.economy.ledger import Scope
from app.economy.life import KILLED_KEY
from app.economy.metering import CallRefused, Completed
from app.economy.service import Economy
from tests.economy_helpers import FakeClock, make_economy, message, metered, owner, request, restart


def spend(economy: Economy, output_tokens: int, calls: int = 1, purpose: str = "work") -> None:
    """Charge ``calls`` model calls of 1,000 input and ``output_tokens`` output tokens (dry run prices)."""
    transport_outcomes = [Completed(message(1_000, output_tokens)) for _ in range(calls)]
    model, transport = metered(economy)
    transport.outcomes = transport_outcomes
    cycle = model.open_cycle("test")
    for _ in range(calls):
        model.call(cycle, purpose, request(max_tokens=max(output_tokens, 1)))
    model.close_cycle(cycle)


def states(economy: Economy) -> list[tuple[str, str]]:
    with economy.db.connection() as conn:
        return [tuple(r) for r in conn.execute("SELECT from_state, to_state FROM life_transitions ORDER BY id")]


def test_new_install_is_alive_with_the_starting_balance(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    status = economy.status()
    assert (status.state, status.mode, status.balance) == ("alive", "dry_run", 20_000_000)
    assert status.runway.days is None and status.runway.note == "No spending yet"
    assert states(economy) == [("new", "alive")]


def test_unfunded_until_the_first_grant(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=0))
    assert economy.status().state == "unfunded"
    model, _ = metered(economy)
    with pytest.raises(CallRefused, match="unfunded"):
        model.open_cycle("test")
    owner(economy, "grant", "5")
    assert economy.status().state == "alive"
    assert states(economy) == [("new", "unfunded"), ("unfunded", "alive")]


def test_runway_uses_the_last_seven_days_of_active_time(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=20, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    spend(economy, output_tokens=99_800)  # 2,000 + 998,000 micros = 1.00 USD
    status = economy.status()
    # Younger than a day: the average is over one day.
    assert status.runway.days == pytest.approx(19.0)
    economy.clock.advance(days=3)
    assert economy.status().runway.days == pytest.approx(19.0 / (1.0 / 3))
    # Paused time doesn't count as active time.
    economy.set_paused(True)
    economy.clock.advance(days=2)
    economy.set_paused(False)
    assert economy.status().runway.days == pytest.approx(19.0 / (1.0 / 3), rel=1e-3)


def test_critical_hysteresis_and_last_will(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=3, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    spend(economy, output_tokens=99_800)  # 1.00 USD in the first day: runway 2 days
    economy.clock.advance(hours=1)
    assert economy.tick().state == "alive"  # exactly 2 days is not critical
    spend(economy, output_tokens=9_800)  # another 0.10 USD: runway (1.90 / 1.10) < 2 days
    status = economy.tick()
    assert status.state == "critical" and status.last_will_due
    assert status.reason == "Runway is under 2 days"
    # A little money that doesn't bring runway back to 4 days keeps it critical.
    owner(economy, "grant", "1")
    assert economy.tick().state == "critical"
    # Enough money after the episode started ends it.
    owner(economy, "grant", "2")
    status = economy.tick()
    assert status.state == "alive" and not status.last_will_due
    assert ("alive", "critical") in states(economy) and ("critical", "alive") in states(economy)


def test_runway_alone_does_not_end_critical_without_new_money(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=2.5, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    spend(economy, output_tokens=99_800)
    assert economy.tick().state == "critical"
    economy.clock.advance(days=30)  # spending stopped; runway is unknown now
    assert economy.status().runway.days is None
    assert economy.tick().state == "critical"


def test_death_when_the_balance_runs_out_and_revival(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=1, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    spend(economy, output_tokens=9_800)  # 0.10 USD
    reply = economy.record("expense", {"amount": "0.95", "note": "domain", "idempotency_key": uuid.uuid4().hex})
    assert reply.status == 409 and reply.body["code"] == "would_change_state"
    assert reply.body["state_after"] == "dead"
    assert economy.status().state != "dead"  # nothing was written
    owner(economy, "expense", "0.95", note="domain")
    status = economy.status()
    assert status.state == "dead" and "ran out of money" in status.reason
    assert status.revive_needed is not None and status.revive_needed > 0
    model, _ = metered(economy)
    with pytest.raises(CallRefused, match="dead"):
        model.open_cycle("test")
    dashboard = economy.dashboard()
    assert dashboard["memorial"]["reason"] == status.reason
    assert dashboard["agent"]["revive"]["needed_usd"] > 0
    # Resuming never revives; too small a grant doesn't either.
    economy.set_paused(False)
    owner(economy, "grant", "0.01")
    assert economy.tick().state == "dead"
    needed = economy.status().revive_needed / 1_000_000
    owner(economy, "grant", f"{needed:.2f}")
    status = economy.tick()
    assert status.state == "alive"
    assert status.life_id != dashboard["agent"]["life_id"]
    lives = economy.dashboard()["lives"]
    assert lives[0]["reason"].startswith("ran out of money")
    with economy.db.connection() as conn:
        new = conn.execute("SELECT * FROM lives ORDER BY id DESC LIMIT 1").fetchone()
    assert new["revived_from_life_id"] == dashboard["agent"]["life_id"]
    assert "revived by owner grant" in new["started_reason"]


def test_starvation_then_last_will_then_death(data_dir: Path) -> None:
    # Enough for small calls but not for a worst-case planning call plus the last-will reserve.
    economy = make_economy(data_dir, Settings(starting_balance_usd=0.05, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    model, _ = metered(economy)
    cycle = model.open_cycle("wake")
    with pytest.raises(CallRefused) as refused:
        model.call(cycle, "plan", request(max_tokens=3_000))
    assert refused.value.category == "balance" and refused.value.state == "critical"
    status = economy.status()
    assert status.state == "critical" and status.last_will_due and "Starving" in status.reason
    # The last will may use the reserve; if even that can't be paid, the agent dies.
    with pytest.raises(CallRefused) as refused:
        model.call(cycle, "last_will", request(max_tokens=100_000))
    assert refused.value.state == "dead"
    assert economy.status().reason.startswith("starved")


def test_starvation_after_the_last_will_is_death(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=0.05, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    with economy.db.connection() as conn:
        conn.execute("UPDATE lives SET last_will_at = '2026-09-01T12:00:00Z'")
    model, _ = metered(economy)
    cycle = model.open_cycle("wake")
    with pytest.raises(CallRefused):
        model.call(cycle, "plan", request(max_tokens=10_000))
    assert economy.status().state == "dead"


def test_pause_and_kill_switch(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    assert economy.set_paused(True).state == "paused"
    model, _ = metered(economy)
    with pytest.raises(CallRefused, match="paused"):
        model.open_cycle("test")
    assert economy.set_paused(False).state == "alive"
    economy.life.set_switch(KILLED_KEY, True)
    assert economy.status().state == "killed"


def test_dry_run_sessions_and_the_dormant_live_life(data_dir: Path) -> None:
    clock = FakeClock()
    dry = make_economy(data_dir, clock=clock)
    first_session = dry.status().life_id
    spend(dry, output_tokens=1_000)
    clock.advance(minutes=1)
    live = restart(dry, Settings(dry_run=False))
    assert live.mode == "live" and live.status().state == "alive"
    live_id = live.status().life_id
    clock.advance(minutes=1)
    dry_again = restart(live, Settings(dry_run=True))
    assert dry_again.status().life_id not in (first_session, live_id)
    # The simulated costs of the first session are gone; real money is shared.
    assert dry_again.books.balance(dry_again.life.scope()) == 20_000_000
    with dry_again.db.connection() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM lives")}
    assert rows[first_session]["state"] == "ended"
    assert rows[live_id]["state"] == "dormant"
    # A restart in the same mode keeps the session.
    assert restart(dry_again).status().life_id == dry_again.status().life_id
    back = restart(dry_again, Settings(dry_run=False))
    assert back.status().life_id == live_id and back.status().state == "alive"


def test_dry_run_death_leaves_the_live_life_alone(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=0.2, daily_spend_cap_usd=5, cycle_spend_cap_usd=5))
    spend(economy, output_tokens=5_000)
    owner(economy, "adjustment", "0.30", direction="subtract", test_money=True)
    assert economy.status().state == "dead"
    assert economy.dashboard()["memorial"]["simulated"] is True
    live = restart(economy, Settings(dry_run=False, starting_balance_usd=0.2))
    assert live.status().state == "alive"
    assert live.books.balance(Scope("live")) == 200_000
