"""DOCS: 'research keeps back at least 1.5 times the costliest research call of the last 14 days' (CHANGELOG 0.21.0
too). The code takes the costliest of the last 20 research calls within 14 days (metering._workshop_tail's LIMIT):
20 cheaper calls an hour later forget a costly one."""
import tempfile
from pathlib import Path
from app.agent import prompts
from app.config import Settings
from app.economy import pricing
from app.economy.costs import micros_to_usd
from app.economy.metering import Completed
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message

settings = Settings(starting_balance_usd=500, daily_spend_cap_usd=100, cycle_spend_cap_usd=50)
clock = FakeClock()
economy = make_economy(Path(tempfile.mkdtemp()), settings, clock)
costly = Completed(message(1_000, 30_000, server_tool_use={"web_search_requests": 1}))  # about $0.31: a big result
cheap = Completed(message(1_000, 200, server_tool_use={"web_search_requests": 1}))      # about $0.014
model = economy.metered(ScriptedTransport(outcomes=[costly] + [cheap] * 20))
req = prompts.research_request(settings, "Who buys meal planners?", None)
cycle = model.open_cycle("test")
worst = model.quote(req, "research")
r = model.call(cycle, "research", req)
pricing.reset_safety_factors(economy.db, "dry_run")  # the owner's Reset estimates; the tail stays (as documented)
model.close_cycle(cycle)
cycle = model.open_cycle("test")
print(f"research worst case ${micros_to_usd(worst):.4f}; costly call cost ${micros_to_usd(r.cost_micros):.4f}")
print(f"hold after it: ${micros_to_usd(model.reservation(req, 'research')):.4f} (1.5 x the costly call)")
for i in range(20):
    clock.advance(minutes=3)
    model.call(cycle, "research", req)
print(f"hold after 20 cheaper research calls, one hour later: ${micros_to_usd(model.reservation(req, 'research')):.4f}")
