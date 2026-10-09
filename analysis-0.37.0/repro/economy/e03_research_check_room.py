"""A research_check call is the same web-search request as research (prompts.research_request), but its purpose isn't in
SERVER_TOOL_PURPOSES: no 'needs 5 times its hold above the last will's reserve' and no tail hold of recent research."""
import tempfile
from pathlib import Path
from app.agent import prompts
from app.config import Settings
from app.economy.costs import micros_to_usd
from app.economy.metering import CallRefused, RESEARCH_CHECK, SERVER_TOOL_PURPOSES
from app.economy.pricing import last_will_reserve
from tests.economy_helpers import ScriptedTransport, make_economy, message, metered, owner
from app.economy.metering import Completed

settings = Settings(starting_balance_usd=0.20, daily_spend_cap_usd=10, cycle_spend_cap_usd=5, research_model="claude-haiku-4-5")
economy = make_economy(Path(tempfile.mkdtemp()), settings)
model, transport = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 200, model="claude-haiku-4-5"))]))
req = prompts.research_request(settings, "Who buys meal planners?", None, None, "claude-haiku-4-5")
cycle = model.open_cycle("test")
worst = model.quote(req, "research")
reserve = last_will_reserve(settings, economy.db, "dry_run")
bal = economy.life.evaluate().balance
print("SERVER_TOOL_PURPOSES =", SERVER_TOOL_PURPOSES)
print(f"balance ${micros_to_usd(bal):.4f}, last-will reserve ${micros_to_usd(reserve):.4f}, research worst case ${micros_to_usd(worst):.4f}"
      f" (5x = ${micros_to_usd(5*worst):.4f})")
print("affordable as research:      ", model.affordable(req, "research", cycle)[0])
print("affordable as research_check:", model.affordable(req, RESEARCH_CHECK, cycle)[0])
try:
    model.call(cycle, "research", req)
    print("research call: admitted")
except CallRefused as exc:
    print("research call refused:", exc.reason)
r = model.call(cycle, RESEARCH_CHECK, req)
print(f"research_check call: admitted, held ${micros_to_usd(r.estimate_micros):.4f}")
