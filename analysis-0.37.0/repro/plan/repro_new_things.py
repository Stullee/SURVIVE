"""Finding: "nothing new starts" is kept by Ember's code only for project_create without a venture_id while the owner
holds new things. (a) With the hold, naming any unparked venture (even an idea) lets a new product line through.
(b) Without the hold, project_create is refused neither outside the explore burn mode nor while steps are ready:
it only needs a cycle without a line yet (a venture cycle, an Owner-project step's cycle, a reactive cycle)."""

from harness import fresh_dir, keep, lined, rows, plan, now, steer, describe
from tests.test_etsy import call
from tests.test_fixes_0280 import working
from tests.test_fixes_0360 import exploring_agent
from tests.test_ventures import DROPSHIPPING

# the ventures laid out as in an exploring test (no conftest patch here)
d = fresh_dir("newthings")
agent = exploring_agent(d)
with agent.db.connection() as conn:
    top = plan.ventures_node(conn, agent.scope())
with agent.db.transaction() as conn:
    plan.owner_hold(conn, agent.scope(), top["id"], "nothing new this week", "Owner", now(agent))
print("venture", DROPSHIPPING, "stage:", rows(agent, f"SELECT stage FROM ventures WHERE id = {DROPSHIPPING}"))
plain = call(working(agent), "project_create", {"title": "A planner", "hypothesis": "Someone pays 5 EUR", "status": "active"})
print("(a) hold, no venture_id :", plain.ok, plain.text[:100])
named = call(working(agent), "project_create", {"title": "Dropshipping phone cases", "hypothesis": "Someone pays 5 EUR",
                                                 "status": "active", "venture_id": DROPSHIPPING})
print("(a) hold, venture_id=%d:" % DROPSHIPPING, named.ok, named.text[:100])
keep(agent)
print("    laid out:", rows(agent, "SELECT id, project_id, title FROM plan_nodes WHERE level = 'product'"))

# (b) a venture cycle's tools (no hold now): project_create is offered and works
with agent.db.transaction() as conn:
    plan.owner_resume(conn, agent.scope(), top["id"], "Owner", now(agent))
ctx = working(agent, None, one_line=False)
ctx.venture = True
v = call(ctx, "project_create", {"title": "Bauhaus posters", "hypothesis": "People hang posters", "status": "active"})
print("(b) in a venture cycle (steps ready:", bool(steer(agent, exploring=True).pick.ranked), "):", v.ok, v.text[:90])
