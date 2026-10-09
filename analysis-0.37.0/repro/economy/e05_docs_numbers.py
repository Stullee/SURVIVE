"""DOCS 'Choosing models' and 'Options' numbers against what the guard prices now."""
import tempfile
from pathlib import Path
from app.config import Settings
from app.economy.costs import micros_to_usd
from app.economy.pricing import opening_cost, working_cycle_cost, workshop_run_cost
from tests.economy_helpers import make_economy

def econ(**kw):
    s = Settings(dry_run=False, **kw)
    return s, make_economy(Path(tempfile.mkdtemp()), s)

s, e = econ()
print("DOCS: 'A working cycle ... can cost up to about 0.25 USD with the default models' -> now $%.3f" % micros_to_usd(working_cycle_cost(s, e.db, "live")))
s, e = econ(planner_model="claude-opus-5-5", cycle_spend_cap_usd=0.60)
print("DOCS: Opus plan 'at most about $0.16' -> now $%.3f" % micros_to_usd(opening_cost(s, e.db, "live")))
print("DOCS: Opus 'working cycle can then cost up to about $0.38, so set the cycle cap to 0.60' -> now $%.3f" % micros_to_usd(working_cycle_cost(s, e.db, "live")))
print("   dashboard warnings at cycle cap 0.60:", e.warnings())
s, e = econ(workshop_model="claude-opus-5-5", workshop_run_cap_usd=1.00)
print("DOCS: Opus workshop 'first call may then cost up to about $0.68, so raise Workshop cap per run to 1.00' -> now $%.3f"
      % micros_to_usd(workshop_run_cost(s, e.db, "live", scaled=False)))
print("   dashboard warnings at cap per run 1.00:", e.warnings())
