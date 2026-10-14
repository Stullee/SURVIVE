"""LOOP: an urgent event wakes a reactive cycle when the day's rest covers only the plan (opening_cost), not a work step
and the reflection (the schedule asks for max(opening, working)). The cycle pays for its plan, is refused its work
step (NO_STEP), its event is marked woke (and seen if the plan listed it) and the agent backs off."""
from harness import fresh_dir, make_agent, plan, tools, text, rows
from app.agent import agenda
from app.economy.clock import to_iso
from app.economy.metering import usd_cap_to_micros
from app.economy.pricing import opening_cost, working_cycle_cost

d = fresh_dir("t05")
agent, transport = make_agent(d, [plan(steps=[], sleep=60)])
transport.tokens = 18_000   # a realistic prompt size for count_tokens (live plans: 16,000-19,600 input tokens)
agent.decide(); agent.clock.advance(minutes=3); agent.run_cycle(agent.decide().trigger)   # one idle cycle
agent.clock.advance(hours=9)          # 21:00 UTC: past 20:00, no event reserve any more (TZ=UTC here)
daily = usd_cap_to_micros(agent.settings.daily_spend_cap_usd)
opening = opening_cost(agent.settings, agent.db, agent.mode) or 0
working = working_cycle_cost(agent.settings, agent.db, agent.mode) or 0
left = opening + 20_000               # the day's rest pays for a plan, not for a work step and the reflection
print(f"opening {opening} working {working} left {left} (micro-USD)")
real = agent.economy.books.cap_spend_on
spent_before = real(agent.economy.life.scope(), agent.clock.today())
agent.economy.books.cap_spend_on = lambda scope, day: daily - left + (real(scope, day) - spent_before)
with agent.db.transaction() as conn:
    agenda._add(conn, agent.scope(), "inquiry", "4242", 'Email #4242 from a.buyer@example.org writes to you: "Custom size?"', to_iso(agent.clock.now()))
decision = agent.decide()
print("decide():", decision.run, decision.trigger, "|", decision.reason[:90])
transport.outcomes[:] = [plan(steps=["answer the email"]), text("unused")]
end = agent.run_cycle(decision.trigger)
print("cycle:", end.status, "|", end.note)
print("calls:", rows(agent, "SELECT purpose, status, cost_micros FROM llm_calls WHERE cycle_id = ?", (end.cycle_id,)))
print("agenda:", rows(agent, "SELECT kind, woke_at IS NOT NULL AS woke, seen_cycle_id FROM agenda"))
print("next wake:", agent._meta_time("next_wake_at"), agent.db.get_meta(agent._key("next_wake_reason")))
