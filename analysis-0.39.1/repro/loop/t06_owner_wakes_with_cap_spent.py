"""LOOP: with the day's cap spent, each owner message/decision still wakes a cycle (never held back); each is refused
at its plan, counts as a failure, and the back-off doubles: after four such wakes in the evening the next wake lands
hours after midnight, when the cap is back."""
from datetime import timedelta
from harness import fresh_dir, make_agent, plan, rows
from app.agent import store
from app.economy.clock import to_iso
from app.economy.metering import usd_cap_to_micros

d = fresh_dir("t06")
agent, transport = make_agent(d, [plan(steps=[], sleep=60)])
agent.decide(); agent.clock.advance(minutes=3); agent.run_cycle(agent.decide().trigger)
agent.clock.advance(hours=8, minutes=57)          # 21:00 local (TZ=UTC)
daily = usd_cap_to_micros(agent.settings.daily_spend_cap_usd)
agent.economy.books.cap_spend_on = lambda scope, day: daily if day == agent.clock.today() else 0   # today's cap is spent
for n in range(4):
    with agent.db.transaction() as conn:
        store.insert_message(conn, agent.scope(), None, f"Question {n + 1}: how are the listings doing?", to_iso(agent.clock.now()))
    print(f"{agent.clock.now():%H:%M} owner writes ->", agent.wake_for_message())
    agent.clock.advance(minutes=6)                 # OWNER_QUIET passed
    dec = agent.decide()
    end = agent.run_cycle(dec.trigger) if dec.run else None
    print(f"   {agent.clock.now():%H:%M} decide: {dec.trigger or dec.reason} -> {end.status if end else '-'};"
          f" failures={agent.db.get_meta(agent._key('failures'))}; next wake {agent._meta_time('next_wake_at'):%H:%M}"
          f" ({agent.db.get_meta(agent._key('next_wake_reason'))})")
    agent.clock.advance(minutes=34)                # OWNER_GAP: 30 min between such wakes
# after midnight the cap is back: when does the next cycle run?
agent.clock.current = agent.clock.day_start(agent.clock.today() + timedelta(days=1)) + timedelta(minutes=6)
dec = agent.decide()
print(f"{agent.clock.now():%Y-%m-%d %H:%M} decide: run={dec.run} | {dec.reason} | wait until {dec.wait_until}")
