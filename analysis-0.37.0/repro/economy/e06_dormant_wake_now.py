"""Dormant (last will written, runway critical) with less than one planning call's worst case left: the owner's Wake
now (which DOCS says 'still runs a cycle') starves the planning call, and a starving agent whose will is written dies."""
import tempfile
from pathlib import Path
from app.agent import fake_llm
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy import burn
from app.economy.costs import micros_to_usd
from app.economy.pricing import opening_cost
from tests.economy_helpers import FakeClock, make_economy, owner

settings = Settings(starting_balance_usd=0.02, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
clock = FakeClock()
economy = make_economy(Path(tempfile.mkdtemp()), settings, clock)
agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake_llm.FakeTransport(clock=lambda: clock.current.timestamp()), cycles_enabled=True)
agent.recover()
# the agent turned critical and wrote its will earlier (as after starving): set it up directly
with economy.db.connection() as conn:
    conn.execute("UPDATE lives SET critical_since = '2026-09-01T11:00:00Z', critical_mark = 1, last_will_at = '2026-09-01T11:30:00Z'")
status = economy.life.evaluate_and_persist()
print("state:", status.state, "| burn mode:", burn.current(economy.db, status).mode,
      "| balance $%.4f, one planning call up to $%.4f" % (micros_to_usd(status.balance), micros_to_usd(opening_cost(settings, economy.db, 'dry_run'))))
print("decide (scheduled):", agent.decide().reason)
print("Wake now:", agent.request_wake())
d = agent.decide()
print("decide:", d.run, d.trigger, d.reason)
end = agent.run_cycle(d.trigger)
print("cycle:", end.status, end.note)
s = economy.life.evaluate()
print("state after Wake now:", s.state, "-", s.reason)
