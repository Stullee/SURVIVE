"""Dashboard warning 'A workshop run ... now keeps $X of the day ..., more than the daily spend cap, so the workshop
can't run' (Economy.warnings) vs 0.33.0's hold, which keeps no more than the day has left: the run goes ahead."""
import tempfile
from pathlib import Path
from app.agent import prompts
from app.agent.fake_llm import FakeTransport, Overrun
from app.agent.service import Agent
from app.agent.workshop import Workshop
from app.config import LoadedSettings, Settings
from app.economy.costs import micros_to_usd
from app.economy.metering import WORKSHOP
from tests.economy_helpers import FakeClock, make_economy

settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=2, cycle_spend_cap_usd=1)
clock = FakeClock()
economy = make_economy(Path(tempfile.mkdtemp()), settings, clock)
fake = FakeTransport(script=[Overrun()], clock=lambda: clock.current.timestamp())
agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
agent.recover()
meter = agent.meter
first = meter.open_cycle("test")
r = meter.call(first, WORKSHOP, prompts.workshop_request(settings, "Draw a chart of 1,2,3 as chart.png", []))
meter.close_cycle(first)
print(f"a run like #423 cost ${micros_to_usd(r.cost_micros):.2f}")
print("dashboard:", [w for w in economy.warnings() if "workshop" in w])
clock.advance(days=1)  # a new day: nothing spent yet
cycle = meter.open_cycle("owner")
workspace, _ = agent.roots()
run = Workshop(economy.db, clock, settings, meter, agent.scope(), workspace).run(cycle, "Draw a chart of 1,2,3 as chart.png", [], None, None)
with economy.db.connection() as conn:
    held = conn.execute("SELECT estimate_micros, status FROM llm_calls WHERE purpose='workshop' ORDER BY id DESC LIMIT 1").fetchone()
print(f"next day's run: failure={run.failure!r}, calls={run.calls}, kept={run.kept}, held ${micros_to_usd(held[0]):.2f} ({held[1]})")
