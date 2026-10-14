"""LOOP: a reactive (event) cycle has no plan-tree step (steered is None), so _cut_sleep never cuts its chosen sleep, and
Agent._after overwrites the scheduled wake with it: the scheduled cycle due in 20 minutes (its sleep cut to 30 because
the plan has steps ready) moves out by the reactive cycle's choice, up to the longest sleep."""
from harness import fresh_dir, make_agent, plan, tools, text, rows
from app.agent import agenda
from app.economy.clock import to_iso

d = fresh_dir("t08")
agent, transport = make_agent(d, [
    plan(sleep=90),
    tools(("workspace_write", {"path": "a.md", "mode": "create", "content": "# A\n"})),
    text("Done."),
    tools(("write_journal", {"summary": "S", "entry": "E"})),
])
agent.decide(); agent.clock.advance(minutes=3)
end = agent.run_cycle(agent.decide().trigger)
print(f"scheduled cycle: {end.status}, asked {end.asked_minutes}, kept {end.sleep_minutes} ({end.sleep_cut})")
print(f"  next wake {agent._meta_time('next_wake_at'):%H:%M} | {agent.db.get_meta(agent._key('next_wake_reason'))}")
agent.clock.advance(minutes=10)
with agent.db.transaction() as conn:
    agenda._add(conn, agent.scope(), "reply", "77", 'Email #77 from buyer@example.org answers one you sent: "Re: size"', to_iso(agent.clock.now()))
decision = agent.decide()
print(f"{agent.clock.now():%H:%M} decide: {decision.trigger} | {decision.reason[:70]}")
transport.outcomes[:] = [
    plan(steps=["answer the buyer's email"], sleep=720),
    text("Answered."),
    tools(("write_journal", {"summary": "Answered the buyer", "entry": "E"})),
]
end = agent.run_cycle(decision.trigger)
print(f"reactive cycle: {end.status}, sleep {end.sleep_minutes}, cut: {end.sleep_cut}")
print(f"  next wake {agent._meta_time('next_wake_at'):%Y-%m-%d %H:%M} | {agent.db.get_meta(agent._key('next_wake_reason'))}")
