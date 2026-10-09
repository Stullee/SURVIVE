"""Finding: a channel's own product (template 'channel', e.g. a "Pinterest account" line) gets the maintain stage's
weekly pin / Bluesky post steps once its setup is done. Their check counts pins/posts that link the product's OWN
listings (reach.funnels), and a channel product has none, so they can never pass: they stay open, age, and steer
marketing cycles onto a line with nothing to link."""

from harness import fresh_dir, keep, lined, no_ventures, project, rows, steer, describe, plan, now, run_cycle

no_ventures()
d = fresh_dir("channel")
agent, _ = lined(d, titles=())
line = project(agent, "Pinterest account", "Pins bring buyers to the shop's listings")
keep(agent)
[prod] = rows(agent, f"SELECT id, template, audience FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
print("product:", prod)
[setup] = rows(agent, f"SELECT id, title, check_kind FROM plan_nodes WHERE project_id = {line} AND level = 'step'")
print("setup step:", setup)
with agent.db.transaction() as conn:
    print(plan.step_done(conn, agent.scope(), line, setup["id"], "The owner connected the account", None, now(agent)))
print(keep(agent))
steps = rows(agent, f"SELECT id, title, check_kind, check_spec, due, status FROM plan_nodes WHERE project_id = {line}"
                    " AND level = 'step' AND status = 'open'")
for s in steps:
    print("  open:", s)
for day in range(0, 22, 3):
    s = run_cycle(agent)
    print(f"day {day:2d}: {describe(s)}")
    agent.clock.advance(days=3)
print("still open after 3 weeks:", rows(agent, f"SELECT id, title, status FROM plan_nodes WHERE project_id = {line}"
                                              " AND level = 'step' AND status = 'open'"))
