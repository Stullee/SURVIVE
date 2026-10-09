"""R8: a hold decide() stores as next_wake_at outlives its premise. (a) the 20:00 event-reserve hold after the owner
switches event wake-ups off (an option change restarts the app); (b) "waiting for the daily cap to reset" after the
owner raises the daily cap; (c) maintenance's one-cycle-a-day wake after money came in and the mode moved up."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from harness import cleanup, fresh_dir, no_ventures

no_ventures()
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.economy import burn, metering
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, owner
from tests.test_agent import ROOMY, make_agent, plan

BERLIN = ZoneInfo("Europe/Berlin")


def restarted(agent, settings):
    again = Agent(agent.db, LoadedSettings(settings), agent.economy, transport=agent.transport, cycles_enabled=True)
    again.settings = settings
    again.economy.settings = settings
    again.economy.loaded = LoadedSettings(settings)
    again.recover()
    return again


def show(tag, agent):
    d = agent.decide()
    local = d.wait_until.astimezone(BERLIN).strftime("%m-%d %H:%M") if d.wait_until else None
    print(f"   {tag}: run={d.run} trigger={d.trigger} reason={d.reason!r} wait_until(local)={local}")


# (a) 20:00 hold, then wake_on_events off
data = fresh_dir("r8a")
clock = FakeClock(datetime(2026, 10, 20, 15, 0, tzinfo=BERLIN).astimezone(UTC), tz=BERLIN)
economy = make_economy(data, ROOMY, clock=clock)
agent = Agent(economy.db, LoadedSettings(ROOMY), economy, transport=ScriptedTransport(), cycles_enabled=True)
agent.recover()
daily = metering.usd_cap_to_micros(ROOMY.daily_spend_cap_usd)
spent = daily - int(daily * 0.2) - 100_000  # the rest less the evening share can't pay a working cycle
agent.economy.books.cap_spend_on = lambda scope, today: spent
agent._set_time("next_wake_at", clock.now() - timedelta(minutes=1))
print("(a) 15:00 local, $%.2f of $%.2f spent today" % (spent / 1e6, daily / 1e6))
show("decide", agent)
off = Settings(**{**ROOMY.model_dump(), "wake_on_events": False})
print("   reserve kept with wake_on_events off:", metering.event_reserve(off, clock, "schedule", 1))
agent2 = restarted(agent, off)
agent2.economy.books.cap_spend_on = lambda scope, today: spent
clock.advance(minutes=5)
show("after the owner switched event wake-ups off (restart), 15:05", agent2)
cleanup()

# (b) daily cap reached, then raised
data = fresh_dir("r8b")
agent, _ = make_agent(data, [], ROOMY)
daily = metering.usd_cap_to_micros(ROOMY.daily_spend_cap_usd)
agent.economy.books.cap_spend_on = lambda scope, today: daily - 50_000
agent._set_time("next_wake_at", agent.clock.now() - timedelta(minutes=1))
print("(b) 12:00 UTC, $%.2f of the $5.00 cap spent" % ((daily - 50_000) / 1e6))
show("decide", agent)
raised = Settings(**{**ROOMY.model_dump(), "daily_spend_cap_usd": 10})
agent2 = restarted(agent, raised)
agent2.economy.books.cap_spend_on = lambda scope, today: daily - 50_000
agent2.clock.advance(minutes=5)
show("after the owner raised the daily cap to $10 (restart)", agent2)
cleanup()

# (c) maintenance, then a grant lifts the mode
data = fresh_dir("r8c")
original = burn._raw
burn._raw = lambda status: burn.MAINTENANCE
agent, _ = make_agent(data, [plan(steps=[], sleep=60)], ROOMY)
assert agent.run_cycle("schedule").status == "idle"
print("(c) maintenance: an idle cycle at", agent.clock.now(), "->", agent._meta_time("next_wake_at"),
      agent.db.get_meta(agent._key("next_wake_reason")))
burn._raw = original
owner(agent.economy, "grant", "100")
agent.clock.advance(minutes=30)
print("   after a $100 grant: burn mode now", burn.current(agent.db, agent.economy.life.evaluate()).mode)
show("decide 30 min later", agent)
cleanup()
