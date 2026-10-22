"""0.12.0: a refund of API costs corrects the day it refunds, not the week it was recorded in, and isn't new money. A
refund of old charges recorded today cancelled out the last week's spending: the runway jumped from 1.3 to 249 days
and the critical state ended. 0.40.3 (analysis-0.40.1 codebase, E1): it still leaves the week's spending, but a refund
of charges made before a critical state began ends it as money in does, once the runway is back to 4 days."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from tests.economy_helpers import make_economy, owner
from tests.money.test_life import spend

LIVE = Settings(dry_run=False, starting_balance_usd=4.2, daily_spend_cap_usd=5, cycle_spend_cap_usd=5)


def test_a_refund_of_old_charges_leaves_the_burn_and_ends_the_critical_state_they_came_before(data_dir: Path) -> None:
    economy = make_economy(data_dir, LIVE)
    spend(economy, output_tokens=99_800, calls=3)  # 3.00 USD on the first day
    first_day = economy.clock.today()
    economy.clock.advance(days=10)
    spend(economy, output_tokens=99_800)  # 1.00 USD today: 0.20 left
    status = economy.tick()
    assert status.state == "critical" and status.runway.window_spend == 1_000_000
    assert status.runway.days == pytest.approx(0.20 / (1.00 / 7))
    owner(economy, "api-correction", "2", direction="decrease", day=first_day.isoformat())  # refunded, recorded today
    status = economy.tick()
    assert status.runway.window_spend == 1_000_000  # the refund belongs to its day, ten days ago
    assert status.runway.days == pytest.approx(2.20 / (1.00 / 7))  # only the balance grew
    # 0.40.3 (E1): it refunds charges made before the critical state began: the money was there (until 0.40.3 the
    # episode went on until a grant)
    assert status.state == "alive" and status.critical_since is None


def test_a_refund_of_this_weeks_charges_lowers_its_burn(data_dir: Path) -> None:
    economy = make_economy(data_dir, LIVE.model_copy(update={"starting_balance_usd": 20}))
    spend(economy, output_tokens=99_800, calls=2)  # 2.00 USD today
    owner(economy, "api-correction", "0.50", direction="decrease")  # half a call was overcharged
    status = economy.status()
    assert status.runway.window_spend == 1_500_000
    assert status.runway.days == pytest.approx(18.50 / 1.50)
