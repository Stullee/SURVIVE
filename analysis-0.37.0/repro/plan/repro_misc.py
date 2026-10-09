"""Smaller checks:
W. two of Ember's steps that wait on each other (plan_step wait on 'step') wait forever: _lift_waits lifts a wait only
   once the other step is closed, and neither can be taken.
O. a promise step closed with its product (the owner drops the product) leaves the obligation open and never a step
   again (the keepers skip any obligation that has a node, open or not)."""

from harness import fresh_dir, keep, lined, no_ventures, project, promise, rows, steer, plan, now
from datetime import timedelta

no_ventures()
d = fresh_dir("misc")
agent, _ = lined(d, titles=("Planner",))
keep(agent)
with agent.db.transaction() as conn:
    print(plan.add_steps(conn, agent.scope(), 1, [{"title": "Draft the cover", "kind": "create"},
                         {"title": "Draft the inner pages", "kind": "create"}], "split the work", None, now(agent)))
[a, b] = rows(agent, "SELECT id FROM plan_nodes WHERE source = 'agent' ORDER BY id")
with agent.db.transaction() as conn:
    print(plan.wait_step(conn, agent.scope(), 1, a["id"], "step", b["id"], None, "needs the pages", None, now(agent),
                         agent.clock.today()))
    print(plan.wait_step(conn, agent.scope(), 1, b["id"], "step", a["id"], None, "needs the cover", None, now(agent),
                         agent.clock.today()))
for _ in range(3):
    agent.clock.advance(days=5)
    keep(agent)
s = steer(agent)
print("W: after 15 days:", rows(agent, f"SELECT id, waiting, wait_ref, status FROM plan_nodes WHERE id IN ({a['id']}, {b['id']})"))

# O
line = project(agent, "Haushaltsbuch 2027 (KDP paperback)", "Ein Haushaltsbuch")
keep(agent)
pid = promise(agent, "Send the KDP book's package", days=3, line=line)
keep(agent)
[prod] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
with agent.db.transaction() as conn:
    plan.end_product(conn, agent.scope(), prod["id"], False, "not a book after all", "Owner", now(agent))
keep(agent)
print("O: obligation:", rows(agent, f"SELECT id, status FROM obligations WHERE id = {pid}"),
      "| its steps:", rows(agent, f"SELECT id, status, parent_id FROM plan_nodes WHERE obligation_id = {pid}"),
      "| a candidate now:", any(c.step.title.startswith(f"Keep promise #{pid}") for c in steer(agent).found))
