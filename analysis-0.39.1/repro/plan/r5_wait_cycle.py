"""R5: plan_step wait refuses a step waiting on itself (0.39.1) but not a cycle: X waits on Y, Y waits on X. Neither
is ever lifted (_lift_waits only lifts a wait whose other step closed), the stage can't close, and nothing the owner
can do on the Plan tab (pin) makes either ready."""
import h
from app.agent import plan
from tests.test_fixes_0350 import plan_step

agent, _ = h.new_agent()
line = h.project(agent, "Sticker sheets", "Someone pays 5 EUR for a sticker sheet")
h.keep(agent)
# research done (a demand note), so the generic product's create stage ("Make it", Ember's own check) is current
with agent.db.transaction() as conn:
    conn.execute("INSERT INTO demand_notes (mode, session, cycle_id, project_id, created_at, keywords, demand, source)"
                 " VALUES (?, ?, 1, ?, ?, 'stickers', 'Etsy lists 4000 sticker sheets', 'etsy')",
                 (agent.scope().mode, agent.scope().session, line, h.now(agent)))
h.keep(agent)
added = plan_step(agent, line, action="add", steps=[{"title": "Draw the sheet", "kind": "create"},
                                                    {"title": "Print a test sheet", "kind": "create"}])
print(added.text)
x, y = [r["id"] for r in h.rows(agent, "SELECT id FROM plan_nodes WHERE project_id = ? AND source = 'agent' ORDER BY id", (line,))]
print(plan_step(agent, line, action="wait", step_id=x, on="step", ref=y, why="needs the test print").text)
print(plan_step(agent, line, action="wait", step_id=y, on="step", ref=x, why="needs the drawing").text)
[make] = h.rows(agent, "SELECT id FROM plan_nodes WHERE project_id = ? AND title = 'Make it'", (line,))
print(plan_step(agent, line, action="done", step_id=make["id"], why="made the first version").text)
with agent.db.transaction() as conn:  # the owner pins X on the Plan tab
    plan.pin(conn, agent.scope(), x, True, "owner", h.now(agent))
for _ in range(20):
    agent.clock.advance(days=1)
    h.keep(agent)
print("after 20 days:")
h.show(h.candidates(agent), only_line=line)
print("stages:", {r["stage"]: r["status"] for r in h.rows(agent, "SELECT stage, status FROM plan_nodes WHERE level='stage' AND project_id=?", (line,))})
s = h.steer(agent)
print("steer:", s.pick.decided, s.step)
