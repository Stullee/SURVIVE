"""Finding: the owner's own hold on a product (0.36.0, "its steps wait until you Resume it") doesn't stop the product's
promise or decision steps: the next cycles are still steered onto the held product line."""

from harness import fresh_dir, keep, lined, no_ventures, project, promise, rows, steer, describe, plan, now
from tests.test_fixes_0280 import request
from tests.test_fixes_0340 import TRACKER

no_ventures()
d = fresh_dir("hold")
agent, _ = lined(d)
tracker = project(agent, *TRACKER)
keep(agent)
asked = request(agent, tracker, "etsy_listing", status="rejected")  # the owner rejected its listing: a decision
pid = promise(agent, "Send the Bewerbungs-Tracker files", days=4, line=tracker)
keep(agent)
[product] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {tracker}")
with agent.db.transaction() as conn:
    plan.owner_hold(conn, agent.scope(), product["id"], "Not this week, I am rethinking it", "Owner", now(agent))
s = steer(agent)
print("after the owner's hold on line", tracker)
print(" step:", describe(s))
for c in s.found:
    if c.step.product == tracker:
        print(f"   cand #{c.step.id:<3} waiting={c.waiting!s:6} {c.step.title[:70]}")
with agent.db.connection() as conn:
    print(" YOUR PLAN line:", [l for l in plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today()).splitlines()
                               if f"#{tracker} " in l])
