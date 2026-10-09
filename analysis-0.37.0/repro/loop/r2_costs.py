"""R2a: the worst cases the wake decisions compare with the day's rest, at the default options."""

from harness import cleanup, fresh_dir

data = fresh_dir("r2costs")
from app.config import Settings
from app.economy.costs import micros_to_usd
from app.economy.pricing import opening_cost, working_cycle_cost
from tests.economy_helpers import make_economy

for settings in (Settings(), Settings(daily_spend_cap_usd=5, cycle_spend_cap_usd=1)):
    economy = make_economy(data, settings)
    for mode in ("dry_run", "live"):
        o = opening_cost(settings, economy.db, mode)
        w = working_cycle_cost(settings, economy.db, mode)
        print(f"daily={settings.daily_spend_cap_usd} mode={mode}: opening_cost=${micros_to_usd(o):.4f}"
              f" working_cycle_cost=${micros_to_usd(w):.4f}")
cleanup()
