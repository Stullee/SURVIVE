"""Default money numbers: opening cost, working cycle, last will reserve, workshop run."""
import tempfile
from pathlib import Path
from app.config import Settings
from app.economy.pricing import opening_cost, working_cycle_cost, last_will_reserve, workshop_run_cost
from app.economy.costs import micros_to_usd
from tests.economy_helpers import make_economy

for label, s in [("default", Settings(dry_run=False)), ("opus planner", Settings(dry_run=False, planner_model="claude-opus-5-5"))]:
    d = Path(tempfile.mkdtemp())
    e = make_economy(d, s)
    print(label, "opening", micros_to_usd(opening_cost(s, e.db, "live")),
          "working", micros_to_usd(working_cycle_cost(s, e.db, "live")),
          "will reserve", micros_to_usd(last_will_reserve(s, e.db, "live")),
          "workshop run", micros_to_usd(workshop_run_cost(s, e.db, "live")))
s = Settings(dry_run=False, workshop_model="claude-opus-5-5")
d = Path(tempfile.mkdtemp())
e = make_economy(d, s)
print("opus workshop run", micros_to_usd(workshop_run_cost(s, e.db, "live")), "unscaled", micros_to_usd(workshop_run_cost(s, e.db, "live", scaled=False)))
s = Settings(dry_run=False, planner_model="claude-opus-5-5", cycle_spend_cap_usd=0.6)
d = Path(tempfile.mkdtemp())
e = make_economy(d, s)
print("opus planner + cycle cap 0.60 warnings:", e.warnings())
s = Settings(dry_run=False)
d = Path(tempfile.mkdtemp())
e = make_economy(d, s)
print("default warnings:", e.warnings())
