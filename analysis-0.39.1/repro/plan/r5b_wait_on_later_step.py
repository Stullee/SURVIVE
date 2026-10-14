"""R5b: one plan_step call is enough: "Propose the listing" made to wait on the owner's "You approve it, and it goes
live", which can only close once the listing is proposed and live. Nothing lifts it, and _again adds no new proposal
while the waiting step of Ember's is open: the product never has a step ready again."""
import h
from app.agent import plan
from tests.test_fixes_0350 import plan_step

agent, _ = h.new_agent()
line = h.project(agent, "Bewerbungs-Tracker", "Eine Vorlage für Bewerbungen und Absagen")
h.keep(agent)
with agent.db.transaction() as conn:
    for st in plan.nodes(conn, agent.scope(), "project_id = ? AND level = 'stage' AND stage IN ('research','create')", (line,)):
        for s in plan.nodes(conn, agent.scope(), "parent_id = ? AND status = 'open'", (st["id"],)):
            plan._close(conn, s["id"], h.now(agent), "done", "test", by="owner")
        plan._close(conn, st["id"], h.now(agent), "done", "test", by="owner")
h.keep(agent)
[propose] = h.rows(agent, "SELECT id FROM plan_nodes WHERE project_id = ? AND title = 'Propose the listing'", (line,))
[approve] = h.rows(agent, "SELECT id FROM plan_nodes WHERE project_id = ? AND title LIKE 'You approve it%'", (line,))
print(plan_step(agent, line, action="wait", step_id=propose["id"], on="step", ref=approve["id"],
                why="the listing waits for my owner's approval").text)
for _ in range(30):
    agent.clock.advance(days=1)
    h.keep(agent)
print("30 days on:", [(c.step.id, c.step.title[:35], c.waiting) for c in h.candidates(agent) if c.step.product == line])
print("steer:", h.steer(agent).pick.decided)
