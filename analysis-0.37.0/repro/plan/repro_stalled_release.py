"""Finding: once a product's listing request was made (pending), its "Propose the listing" step closes done for good;
if that request then expires, is withdrawn, or is rejected and Ember closes the decision, nothing in the plan asks for
a new proposal: the release stage's only open step is the owner's "You approve it, and it goes live", which waits on
the owner although nothing waits for them. No step of the product is ever ready again, so no cycle is steered to it."""

from harness import fresh_dir, keep, lined, no_ventures, project, rows, steer, describe, plan, now, run_cycle
from tests.test_fixes_0340 import TRACKER, request as pending_request
from app.agent import obligations

no_ventures()
for ending in ("expired", "withdrawn", "rejected"):
    d = fresh_dir("stall")
    agent, _ = lined(d, titles=("Planner",))
    tracker = project(agent, *TRACKER)
    keep(agent)
    asked = pending_request(agent, tracker, "etsy_listing")
    keep(agent)  # the request is pending: the create stage and "Propose the listing" close (done is final)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?", (ending, now(agent), asked))
        if ending == "rejected":
            obligations.keep(conn, agent.scope(), now(agent), "2000-01-01T00:00:00Z")
    keep(agent)
    if ending == "rejected":  # Ember reacts to the decision and closes it (it said it would re-propose later)
        [ob] = rows(agent, "SELECT id FROM obligations WHERE kind = 'decision'")
        with agent.db.transaction() as conn:
            obligations.close_one(conn, ob["id"], "noted message #1, will redo the photos", "agent", None, now(agent))
        keep(agent)
    print(f"=== request #{asked} {ending}")
    for r in rows(agent, f"SELECT id, stage, title, status, kind FROM plan_nodes WHERE project_id = {tracker}"
                         " AND level = 'step' ORDER BY id"):
        print(f"   #{r['id']:<3} {r['stage']:8} {r['status']:7} {r['kind']:6} {r['title'][:60]}")
    s = steer(agent)
    mine = [c for c in s.found if c.step.product == tracker]
    print("   tracker's candidates:", [(c.step.id, c.waiting) for c in mine])
    print("   ready steps of the tracker:", [c.step.id for c in mine if c.waiting is None])
    with agent.db.connection() as conn:
        print("   YOUR PLAN:", [l for l in plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today()).splitlines()
                                 if "Waiting on your owner" in l or "Bewerbungs" in l])
    taken = sum(1 for _ in range(10) if (run_cycle(agent).line == tracker) or agent.clock.advance(hours=3))
    print("   cycles on the tracker in the next 10:", taken)
