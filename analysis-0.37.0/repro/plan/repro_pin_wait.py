"""Finding: Ember can make the step the owner pinned wait (plan_step wait, up to 14 days), and the owner's pin then
does nothing; hold() refuses with a pinned step and split/replace refuse it, wait doesn't."""

from datetime import timedelta

from harness import fresh_dir, keep, lined, no_ventures, rows, steer, describe, plan, now
from tests.test_etsy import call
from tests.test_fixes_0280 import working

no_ventures()
d = fresh_dir("pinwait")
agent, _ = lined(d)
keep(agent)
[step] = rows(agent, "SELECT id FROM plan_nodes WHERE project_id = 2 AND level = 'step' AND status = 'open' ORDER BY id LIMIT 1")
with agent.db.transaction() as conn:
    plan.pin(conn, agent.scope(), step["id"], True, "Owner", now(agent))
s = steer(agent)
print("pinned:", describe(s))
ctx = working(agent, 2)
until = (agent.clock.today() + timedelta(days=14)).isoformat()
out = call(ctx, "plan_step", {"action": "wait", "step_id": step["id"], "on": "date", "until": until,
                               "why": "I would rather do line 1 first"})
print("plan_step wait on the pinned step:", out.ok, out.text)
for action in ("split", "replace"):
    o = call(ctx, "plan_step", {"action": action, "step_id": step["id"], "steps": [{"title": "a", "kind": "create"},
                                {"title": "b", "kind": "create"}], "why": "x"})
    print(f"plan_step {action}:", o.ok, o.text[:90])
o = call(ctx, "plan_step", {"action": "hold", "why": "x"})
print("plan_step hold:", o.ok, o.text[:90])
s = steer(agent)
print("next cycle:", describe(s), "| pinned step waits on:",
      [c.waiting for c in s.found if c.step.id == step["id"]], "| pinned flag:",
      rows(agent, f"SELECT pinned FROM plan_nodes WHERE id = {step['id']}"))
