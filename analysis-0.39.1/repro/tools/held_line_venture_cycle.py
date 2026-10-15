"""4.4 after 0.39.3: a request about a product line you hold is refused in an ordinary cycle, and filed in a venture
cycle. tools._line checks your hold only after its early return for a cycle that isn't one line's (a venture cycle,
loop.py:935). On 0.39.1 both cycles file it."""
import harness
from app.agent import plan, store
from app.economy.clock import to_iso

made, _ = harness.agent()
now = to_iso(made.clock.now())
with made.db.transaction() as conn:
    line = store.create_project(conn, made.scope(), cycle_id=1, title="Bewerbungs-Tracker",
                                hypothesis="Someone pays 5 EUR for it", next_step="make it", status="active", now=now)
    plan.keep(conn, made.scope(), now, made.clock.today(), {"pinterest": True, "bluesky": True, "blog": True})
    [product] = plan.nodes(conn, made.scope(), "level = 'product' AND project_id = ?", (line,))
    plan.owner_hold(conn, made.scope(), product["id"], "I look at it next week", "owner", now)
print("product line held:", harness.rows(made, "SELECT hold_by, hold_reason FROM plan_nodes WHERE level = 'product'"))

ask = {"type": "publish", "title": "Publish the tracker", "description": "The tracker is ready to sell.",
       "expected_cost": "none", "expected_benefit": "sales", "project_id": line}
for label, venture in (("ordinary cycle", False), ("venture cycle", True)):
    ctx = harness.context(made, venture=venture)
    ctx.state.one_line = not venture  # as loop.py sets it
    harness.show(label, harness.call(ctx, "request_approval", {**ask, "payload": f"tracker v2, {label}"}))
print("requests about the held line:", harness.rows(made, "SELECT id, title FROM approvals WHERE project_id = ?", (line,)))
