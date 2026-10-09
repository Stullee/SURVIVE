"""A restart during a workshop run charges its hold provisionally (DOCS: 'until you correct it'). Death is judged on the
settled balance, but the critical state on the charged one: the provisional charge alone makes the agent critical (its
one last will falls due), and the owner's correction of that very charge can't end it (a refund isn't money in)."""
import tempfile, uuid
from pathlib import Path
from app.agent import prompts
from app.config import Settings
from app.economy.costs import micros_to_usd
from app.economy.metering import WORKSHOP, Completed
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message, request, restart

settings = Settings(starting_balance_usd=8.30, daily_spend_cap_usd=5, cycle_spend_cap_usd=5, dry_run=False)
clock = FakeClock()
economy = make_economy(Path(tempfile.mkdtemp()), settings, clock)
model = economy.metered(ScriptedTransport(simulated=False, outcomes=[Completed(message(1_000, 249_800))]))
c = model.open_cycle("test")
model.call(c, "work", request(max_tokens=250_000))  # $2.50 of work today
model.close_cycle(c)
clock.advance(hours=2)
s = economy.life.evaluate_and_persist()
print(f"before: {s.state}, balance ${micros_to_usd(s.balance):.2f}, runway {s.runway.days:.2f} days")
c = model.open_cycle("test")
res = model.reserve(c, WORKSHOP, prompts.workshop_request(settings, "Draw a chart as chart.png", []))  # sent; the app restarts
print(f"workshop call holds ${micros_to_usd(res.estimate):.2f}; the app restarts mid-run")
economy = restart(economy)
s = economy.life.evaluate_and_persist()
print(f"after the restart: {s.state} ({s.reason}), balance ${micros_to_usd(s.balance):.2f} (settled ${micros_to_usd(s.settled_balance):.2f}),"
      f" runway {s.runway.days:.2f} days, last will due: {s.last_will_due}")
# The owner checks the Console: the run cost $0.10; records the difference as the dashboard asks
refund = micros_to_usd(res.estimate) - 0.10
r = economy.record("api-correction", {"amount": f"{refund:.2f}", "direction": "decrease", "note": "Console: $0.10",
                                      "idempotency_key": uuid.uuid4().hex})
s = economy.life.evaluate_and_persist()
print(f"after the owner's correction ({r.status}): {s.state}, balance ${micros_to_usd(s.balance):.2f}, runway {s.runway.days:.2f} days,"
      f" last will due: {s.last_will_due}")
clock.advance(days=1)
s = economy.life.evaluate_and_persist()
print(f"a day later (nothing spent): {s.state}, runway {s.runway.days if s.runway.days is None else round(s.runway.days, 2)}")
from app.economy import burn
print("burn mode once the will is written:", burn.settle(None, __import__("dataclasses").replace(s, last_will_at="2026-09-02T15:00:00Z")))
