"""0.21.0: 'a workshop or research call needs 5 times what it keeps back left above the last will's reserve' (a run's
hold: its cap per run, its raised estimate, 1.5 x the costliest recent run). 0.33.0 clamps the hold to the 'money' room
(MeteredModel.rooms), which already contains (balance - reserve) / 5: near the bottom the hold shrinks to a fifth of what
is left, so the 5x rule is met by construction and only the quote (~$0.60) is multiplied, not what runs were seen to
cost. The 0.21.0 test calls the guard without hold= and so never takes this path."""
import tempfile, uuid
from datetime import timedelta
from pathlib import Path
from app.agent import prompts
from app.agent.fake_llm import FakeTransport, Overrun
from app.agent.service import Agent
from app.agent.workshop import Workshop
from app.config import LoadedSettings, Settings
from app.economy import pricing
from app.economy.costs import micros_to_usd
from app.economy.metering import WORKSHOP, CallRefused, server_tool_room
from app.economy.pricing import last_will_reserve
from tests.economy_helpers import FakeClock, make_economy

settings = Settings(starting_balance_usd=20, daily_spend_cap_usd=7, cycle_spend_cap_usd=1)  # the owner's caps of 0.13.0
clock = FakeClock()
economy = make_economy(Path(tempfile.mkdtemp()), settings, clock)
big = Overrun(cache_write=1_000_000, cache_read=3_500_000)  # a run 5.3x its quote, as #423 was of its quote
fake = FakeTransport(script=[Overrun(), big], clock=lambda: clock.current.timestamp())
agent = Agent(economy.db, LoadedSettings(settings), economy, transport=fake, cycles_enabled=True)
agent.recover()
meter = agent.meter
task = "Draw a chart of 1,2,3 as chart.png"
c = meter.open_cycle("test")
r1 = meter.call(c, WORKSHOP, prompts.workshop_request(settings, task, []))
meter.close_cycle(c)
pricing.reset_safety_factors(economy.db, "dry_run")  # the owner's Reset estimates (it keeps the runs' tail, as documented)
clock.advance(days=1)
reserve = last_will_reserve(settings, economy.db, "dry_run")
target = 3_100_000 + reserve  # $3.10 above the last will's reserve
bal = economy.life.evaluate().balance
rep = economy.record("adjustment", {"amount": f"{(bal - target) / 1e6:.2f}", "direction": "subtract", "note": "spent down",
                              "test_money": True, "idempotency_key": uuid.uuid4().hex, "confirm_large": True,
                              "confirm_state_change": "dead"})
print("spend-down entry:", rep.status, rep.body.get("error"))
s = economy.life.evaluate()
req = prompts.workshop_request(settings, task, [])
full = meter.reservation(req, WORKSHOP)
print(f"run #1 cost ${micros_to_usd(r1.cost_micros):.2f}; the 5x room factor is {server_tool_room(economy.db, clock, True)}")
print(f"balance ${micros_to_usd(s.balance):.2f}, ${micros_to_usd(s.balance - reserve):.2f} above the reserve;"
      f" a run's documented hold ${micros_to_usd(full):.2f} (quote ${micros_to_usd(meter.quote(req, WORKSHOP)):.2f})"
      f" -> 5x needs ${micros_to_usd(5 * full):.2f}")
c = meter.open_cycle("owner")
try:
    meter.reserve(c, WORKSHOP, req)  # the guard as the 0.21.0 test calls it (no hold=)
except CallRefused as exc:
    print("guard without hold= (the 0.21.0 test's path): refused:", exc.reason[:90])
workspace, _ = agent.roots()
run = Workshop(economy.db, clock, settings, meter, agent.scope(), workspace).run(c, task, [], None, None)
with economy.db.connection() as conn:
    held = conn.execute("SELECT estimate_micros, cost_micros FROM llm_calls WHERE purpose='workshop' AND status='ok' ORDER BY id DESC LIMIT 1").fetchone()
s = economy.life.evaluate_and_persist()
print(f"the workshop's own path: admitted, held ${micros_to_usd(held[0]):.2f}, cost ${micros_to_usd(held[1]):.2f}")
print(f"-> state {s.state} ({s.reason}), balance ${micros_to_usd(s.balance):.2f}, last will written: {s.last_will_at is not None}")
