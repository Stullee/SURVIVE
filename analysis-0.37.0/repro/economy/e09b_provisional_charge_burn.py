"""Same root cause, burn modes (owner's stance conserve): a restart mid-workshop-run moves the mode down on the
provisional charge; the owner's correction of that charge can't move it back up (only money in can)."""
import tempfile, uuid
from pathlib import Path
from app.agent import prompts
from app.config import Settings
from app.economy import burn
from app.economy.costs import micros_to_usd
from app.economy.metering import WORKSHOP, Completed
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message, request, restart

settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=5, dry_run=False, spending_stance="conserve")
clock = FakeClock()
economy = make_economy(Path(tempfile.mkdtemp()), settings, clock)
model = economy.metered(ScriptedTransport(simulated=False, outcomes=[Completed(message(1_000, 239_800))]))
c = model.open_cycle("test")
model.call(c, "work", request(max_tokens=240_000))  # $2.40 of work today
model.close_cycle(c)
clock.advance(hours=2)
s = economy.life.evaluate_and_persist()
print(f"before: net runway {s.runway.net_days:.1f} days, burn mode {burn.current(economy.db, s).mode}")
c = model.open_cycle("test")
res = model.reserve(c, WORKSHOP, prompts.workshop_request(settings, "Draw a chart as chart.png", []))
economy = restart(economy)  # the app restarts mid-run: the hold is charged until the owner corrects it
s = economy.life.evaluate_and_persist()
print(f"after the restart: charged ${micros_to_usd(res.estimate):.2f}; net runway {s.runway.net_days:.1f} days, burn mode {burn.current(economy.db, s).mode}")
r = economy.record("api-correction", {"amount": f"{micros_to_usd(res.estimate) - 0.10:.2f}", "direction": "decrease",
                                      "note": "Console: $0.10", "idempotency_key": uuid.uuid4().hex})
s = economy.life.evaluate_and_persist()
b = burn.current(economy.db, s)
print(f"after the owner's correction ({r.status}): net runway {s.runway.net_days:.1f} days, burn mode {b.mode} ({b.held})")
