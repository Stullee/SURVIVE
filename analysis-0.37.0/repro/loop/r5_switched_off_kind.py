"""R5: a wake kept for one kind of the owner's news (an approval, switched on) fires for news of a kind the owner
switched off (a message), once a cycle has seen the approval: _wake_for_waiting_message asks _unread(), which counts
any unseen message or decision, not the kinds the wake is for (waiting_kinds)."""

from datetime import timedelta

from harness import cleanup, fresh_dir, no_ventures

no_ventures()
data = fresh_dir("r5")
from app import web
from tests.test_agent import ROOMY, make_agent, plan, rows
from tests.test_autonomy import request_for, send
from tests.test_decision_wakes import asks_and_sleeps
from tests.test_wake_switches import roomy, routed

settings = roomy(wake_on_message=False)  # the owner: my messages don't wake Ember, my approvals do
agent, _ = make_agent(data, [*asks_and_sleeps(720), plan(steps=[], sleep=720), plan(steps=[], sleep=720)], settings)
assert agent.run_cycle("schedule").status == "completed"  # cycle #1 asks for request #1
request, pokes = routed(agent, settings)
t0 = agent.clock.now()
decided = web.decide_approval(request, 1, {"decision": "approve", "expected_version": 0})
print("approve ->", decided.status_code, "wake kinds:", agent.waiting_kinds, "message_waiting:", agent.message_waiting)
agent.clock.current = t0 + timedelta(minutes=1)
# a scheduled cycle comes first (the docs: "a cycle that comes first reads them too") and sees the approval
agent.db.set_meta(agent._key("next_wake_at"), "2026-09-01T00:00:00Z")
d = agent.decide()
print("+1 decide ->", d.run, d.trigger, d.reason)
end = agent.run_cycle(d.trigger)
print("   ran", d.trigger, "cycle:", end.status, "| approval seen by cycle",
      rows(agent, "SELECT seen_cycle_id FROM approvals WHERE id = 1")[0]["seen_cycle_id"])
# while that cycle ran (after its plan), the owner wrote: switched off, so no wake of its own
send(agent, "FYI, no need to answer")
print("message ->", web._wake_for_message(request), "(None: wake_on_message is off)")
agent.clock.current = t0 + timedelta(minutes=2)
d = agent.decide()
print("+2 decide ->", d.run, d.trigger, d.reason, d.wait_until)
agent.clock.current = t0 + timedelta(minutes=5)
d = agent.decide()
print("+5 decide ->", d.run, d.trigger, d.reason)
print("events:", [e["message"] for e in agent.db.recent_events(5) if "woke" in e["message"]])
cleanup()
