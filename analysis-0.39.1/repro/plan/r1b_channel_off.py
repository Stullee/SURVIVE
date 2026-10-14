"""R1b: the same gate from the other side: with one of its channels off (here Bluesky), a live product whose critic
passed and whose launch pins are out still never leaves launch ("Post it twice on Bluesky" waits 'channel' forever),
so it never gets the weekly pin either, though Pinterest is on."""
import h
from app.agent import plan, weights
from tests.test_fixes_0353 import launched, critic

agent, line, listing = launched(h.DATA)
CH = {"pinterest": True, "bluesky": False, "blog": True}
with agent.db.transaction() as conn:
    for s in plan.nodes(conn, agent.scope(), "project_id = ? AND level = 'step' AND stage = 'launch' AND status = 'open'"
                        " AND check_kind IN ('pin','blog')", (line,)):
        plan._close(conn, s["id"], h.now(agent), "done", "Ember's code: its check passed")
critic(agent, line, listing, 8)  # the critic passes it
for _ in range(15):
    agent.clock.advance(days=1)
    h.keep(agent, CH)
print("audience:", h.rows(agent, "SELECT audience FROM plan_nodes WHERE level='product' AND project_id=?", (line,)))
print("stages:", {r["stage"]: r["status"] for r in h.rows(agent, "SELECT stage, status FROM plan_nodes WHERE level='stage' AND project_id=?", (line,))})
print("open steps:", [(r["id"], r["title"]) for r in h.rows(agent, "SELECT id, title FROM plan_nodes WHERE level='step' AND project_id=? AND status='open'", (line,))])
print("weekly steps ever laid out:", h.rows(agent, "SELECT COUNT(*) AS n FROM plan_nodes WHERE project_id=? AND template LIKE 'recurring/%'", (line,))[0]["n"])
print("candidates:", [(c.step.id, c.step.title[:35], c.waiting) for c in h.candidates(agent, CH) if c.step.product == line])
