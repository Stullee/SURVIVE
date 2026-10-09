"""'A scaled-up estimate comes down by half of what it is above 1 after 3 calls of its kind IN A ROW that didn't need
it' (DOCS Money). A call that needed the raised factor (cost above the unscaled worst case, within the scaled one)
should break the streak; it doesn't."""
import tempfile
from decimal import Decimal
from pathlib import Path
from app.config import Settings
from app.economy import pricing
from app.economy.metering import Completed
from tests.economy_helpers import ScriptedTransport, make_economy, message, metered, request

economy = make_economy(Path(tempfile.mkdtemp()), Settings(starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5))
M = "claude-sonnet-5"
pricing.raise_safety_factor(economy.db, M, 24_000, 12_000, "dry_run", "work")  # 2.2
print("factor after an overrun:", pricing.safety_factor(economy.db, M, "dry_run", "work"))
# request(max_tokens=1000) with 1,000 prompt tokens: unscaled worst case 1000*2 + 1000*10 = 12,000 micros
cheap = Completed(message(1_000, 200))     # 4,000 micros: didn't need the factor
needy = Completed(message(1_000, 1_800))   # 20,000 micros: above the unscaled 12,000, within the scaled 26,400
model, transport = metered(economy, ScriptedTransport(outcomes=[cheap, cheap, needy, cheap]))
cycle = model.open_cycle("test")
for label in ("didn't need it", "didn't need it", "NEEDED it (cost > unscaled worst case)", "didn't need it"):
    r = model.call(cycle, "work", request(max_tokens=1_000))
    print(f"  call #{r.call_id} cost {r.cost_micros} ({label}), overrun={r.overrun}; factor now",
          pricing.safety_factor(economy.db, M, "dry_run", "work"))
