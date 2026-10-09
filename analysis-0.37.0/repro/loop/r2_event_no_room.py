"""R2b: an event wakes the agent when the day's rest covers only the plan (opening_cost), not a working cycle: the
scheduled wake waits for midnight in the same state, while the event cycle pays for its plan and ends with no work
step (NO_STEP)."""

from harness import cleanup, fresh_dir, no_ventures

data = fresh_dir("r2b")
no_ventures()
from app.agent.fake_llm import FakeTransport
from app.config import Settings
from app.economy.costs import micros_to_usd
from app.economy.pricing import opening_cost, working_cycle_cost
from tests.test_agent import rows
from tests.test_fixes_0140_wakes import urgent_milestone
from tests.test_life import spend
from tests.test_loop_shapes import run

settings = Settings(owner_user_ids=("8f14e45fceea167a5a36dedd4bea2543",))  # the default caps: $1.50 a day, $0.50 a cycle
agent, _ = run(data, FakeTransport(seed=3), cycles=0, settings=settings)
import sys
REST = float(sys.argv[1]) if len(sys.argv) > 1 else 0.294
for _ in range(3):
    spend(agent.economy, 40_000)
extra = int(round((1.206 - (1.5 - REST)) * -100_000))
if extra > 0:
    spend(agent.economy, extra)
today = agent.economy.books.cap_spend_on(agent.economy.life.scope(), agent.clock.today())
print(f"spent today ${micros_to_usd(today):.4f} of $1.50; rest ${1.5 - micros_to_usd(today):.4f}")
print(f"opening_cost ${micros_to_usd(opening_cost(settings, agent.db, 'dry_run')):.4f},"
      f" working_cycle_cost ${micros_to_usd(working_cycle_cost(settings, agent.db, 'dry_run')):.4f}")
agent.clock.advance(minutes=6)
d = agent.decide()
print(f"no event: decide -> run={d.run} trigger={d.trigger} reason={d.reason!r} wait_until={d.wait_until}")
agent.clock.advance(minutes=3)
d = agent.decide()
print(f"no event, first wake due: decide -> run={d.run} trigger={d.trigger} reason={d.reason!r} wait_until={d.wait_until}")
urgent_milestone(agent)
d = agent.decide()
print(f"with an urgent event: decide -> run={d.run} trigger={d.trigger} reason={d.reason!r}")
if d.run:
    end = agent.run_cycle(d.trigger)
    print(f"event cycle: status={end.status} note={end.note!r}")
    for r in rows(agent, f"SELECT purpose, status, cost_micros FROM llm_calls WHERE cycle_id = {end.cycle_id}"):
        print(f"  call {r['purpose']} {r['status']} ${micros_to_usd(r['cost_micros']):.4f}")
    print("  next wake:", agent._meta_time("next_wake_at"), agent.db.get_meta(agent._key("next_wake_reason")))
cleanup()
