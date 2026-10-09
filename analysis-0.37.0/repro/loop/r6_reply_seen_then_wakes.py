"""R6: a reply to Ember's email that a cycle fetched itself (its start-of-cycle mail read) and showed in its MAIL section
is noted as an urgent agenda event only after that cycle (agenda.note runs between cycles only), and wakes a reactive
cycle for news the last cycle already read."""

from datetime import timedelta

from harness import cleanup, fresh_dir, no_ventures

no_ventures()
data = fresh_dir("r6")
from app.agent.fake_llm import request_kind
from tests.test_agenda import replied
from tests.test_agent import rows
from tests.test_etsy import listed

agent, _ = listed(data)
agent.check_events()  # the agenda begins
agent.clock.advance(minutes=40)
agent.check_events()  # a mailbox read between cycles: nothing urgent
print("agenda before:", rows(agent, "SELECT kind, urgent FROM agenda WHERE baseline = 0"))
# The reply reaches the mail server a few minutes before the next scheduled cycle, which reads the mailbox first
# (received_at = the moment Ember's code fetched it, as mailstore.store_incoming writes it).
replied(agent, 7)
agent.db.set_meta(agent._key("next_wake_at"), "2026-09-01T00:00:00Z")  # the scheduled wake is due
d = agent.decide()
print("decide (no check_events in between) ->", d.run, d.trigger, d.reason)
before = len(agent.transport.sent)
end = agent.run_cycle(d.trigger)
plan_req = [r for r in list(agent.transport.sent)[before:] if request_kind(r) == "plan"][0]
text = plan_req["messages"][0]["content"][0]["text"]
mail = text.split("== MAIL ==", 1)[1].split("\n== ", 1)[0] if "== MAIL ==" in text else "(no MAIL section)"
print(f"scheduled cycle #{end.cycle_id}: {end.status}; its plan's MAIL section shows the reply:",
      "Re: Your planner" in mail)
print("   MAIL excerpt:", " | ".join(line for line in mail.splitlines() if "Re: Your planner" in line)[:200])
agent.clock.advance(minutes=1)
agent.check_events()  # the next scheduler round
print("agenda after:", rows(agent, "SELECT kind, urgent, woke_at, seen_cycle_id FROM agenda WHERE baseline = 0"))
agent.db.set_meta(agent._key("next_wake_at"), "2026-09-02T00:00:00Z")  # the schedule far away
d = agent.decide()
print("decide ->", d.run, d.trigger, d.reason)
cleanup()
