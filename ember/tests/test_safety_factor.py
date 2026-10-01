"""0.12.0: the safety factor is kept per model and purpose, comes down after calls that didn't need it, and the owner
can reset it. One research call's overrun used to raise every estimate of its model, planning included, until the
scheduled wake-ups stopped."""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.economy import pricing
from app.economy.metering import Completed
from app.economy.pricing import SAFETY_DECAY_AFTER, safety_factor, working_cycle_cost
from tests.economy_helpers import ScriptedTransport, make_economy, message, metered, request
from tests.test_owner_api import post

GENEROUS = Settings(starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)
MODEL = "claude-sonnet-5"


def test_an_overrun_raises_only_the_estimates_of_its_purpose(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    working = working_cycle_cost(economy.settings, economy.db, "dry_run")
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 5_000))]))
    assert model.call(model.open_cycle("test"), "research", request(max_tokens=1_000)).overrun
    assert safety_factor(economy.db, MODEL, "dry_run", "research") == Decimal("4.77")  # 52,000 / 12,000 x 1.1
    assert [safety_factor(economy.db, MODEL, "dry_run", p) for p in ("plan", "work", "reflect")] == [1, 1, 1]
    assert working_cycle_cost(economy.settings, economy.db, "dry_run") == working  # the wake-ups go on
    assert model.quote(request(max_tokens=1_000), "research") == math.ceil(
        4.77 * model.quote(request(max_tokens=1_000))
    )
    events = [e["message"] for e in economy.db.recent_events(limit=10)]
    assert any("estimates for research calls on claude-sonnet-5 are now scaled by 4.77" in m for m in events)


def test_a_raised_factor_comes_down_after_calls_that_did_not_need_it(data_dir: Path) -> None:
    economy_db = make_economy(data_dir, GENEROUS).db
    assert pricing.raise_safety_factor(economy_db, MODEL, 13_200, 12_000, "live", "work") == Decimal("1.21")
    for _ in range(SAFETY_DECAY_AFTER - 1):
        assert pricing.note_accurate_call(economy_db, MODEL, "live", "work") is None
    # 0.15.0: half of the excess comes off (after 3 calls; 0.05 after 25 before)
    assert pricing.note_accurate_call(economy_db, MODEL, "live", "work") == Decimal("1.11")
    pricing.raise_safety_factor(economy_db, MODEL, 12_000, 12_000, "live", "work")  # an overrun starts the count over
    for _ in range(SAFETY_DECAY_AFTER - 1):
        assert pricing.note_accurate_call(economy_db, MODEL, "live", "work") is None
    assert safety_factor(economy_db, MODEL, "live", "work") == Decimal("1.22")  # 12,000 / 12,000 x 1.11 x 1.1
    economy_db.set_meta("economy.factor.live.work.claude-sonnet-5", "1.03")
    for _ in range(SAFETY_DECAY_AFTER):
        pricing.note_accurate_call(economy_db, MODEL, "live", "work")
    assert safety_factor(economy_db, MODEL, "live", "work") == 1  # never below 1
    assert pricing.note_accurate_call(economy_db, MODEL, "live", "work") is None  # nothing to bring down


def test_accurate_calls_bring_a_factor_down_in_the_meter(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    pricing.raise_safety_factor(economy.db, MODEL, 13_200, 12_000, "dry_run", "work")  # 1.21
    outcomes = [Completed(message(1_000, 200)) for _ in range(SAFETY_DECAY_AFTER)]
    model, _ = metered(economy, ScriptedTransport(outcomes=outcomes))
    cycle = model.open_cycle("test")
    for _ in range(SAFETY_DECAY_AFTER):
        result = model.call(cycle, "work", request(max_tokens=1_000))
        assert result.cost_micros == 4_000 and not result.overrun  # well within the unscaled 12,000
    assert safety_factor(economy.db, MODEL, "dry_run", "work") == Decimal("1.11")
    events = [e["message"] for e in economy.db.recent_events(limit=5)]
    assert any("Estimates for work calls on claude-sonnet-5 are now scaled by 1.11" in m for m in events)


def test_the_owner_resets_every_estimate(data_dir: Path) -> None:
    economy = make_economy(data_dir, GENEROUS)
    pricing.raise_safety_factor(economy.db, MODEL, 24_000, 12_000, "dry_run", "research")  # 2.2
    economy.db.set_meta("economy.safety.dry_run.claude-sonnet-5", "3")  # the factor before 0.12.0: no longer read
    pricing.raise_safety_factor(economy.db, MODEL, 24_000, 12_000, "live", "plan")  # another mode's stays
    assert safety_factor(economy.db, MODEL, "dry_run", "work") == 1
    factors = economy.dashboard()["agent"]["estimate_factors"]
    assert factors == [{"purpose": "research", "model": MODEL, "factor": 2.2}]
    reply = economy.reset_estimates("Stefan")
    assert (reply.status, reply.body) == (200, {"reset": 1})
    assert economy.dashboard()["agent"]["estimate_factors"] == []
    assert economy.db.get_meta("economy.safety.dry_run.claude-sonnet-5") is None
    assert safety_factor(economy.db, MODEL, "live", "plan") == Decimal("2.2")
    events = [e["message"] for e in economy.db.recent_events(limit=5)]
    assert "Stefan reset the cost estimates: 1 scaled-up estimate(s) back to 1" in events


def test_the_reset_is_an_owner_action_on_the_dashboard(ingress_client: TestClient) -> None:
    assert post(ingress_client, "api/economy/estimates/reset", {}, headers={}).status_code == 403  # a request header
    reply = post(ingress_client, "api/economy/estimates/reset", {})
    assert reply.status_code == 200 and reply.json() == {"reset": 0}
    assert ingress_client.get("api/dashboard").json()["agent"]["estimate_factors"] == []
