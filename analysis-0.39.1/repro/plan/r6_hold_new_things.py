"""R6 (analysis-0.37.0 6.7): the owner's Hold new things ("nothing new") lets project_create through when the new line
names a venture, any open one that isn't parked (an idea the agent just made counts)."""
import h
from app.agent import plan, ventures
from tests.test_etsy import call
from tests.test_fixes_0280 import working

agent, _ = h.new_agent()
h.keep(agent)
with agent.db.transaction() as conn:
    top = plan.ventures_node(conn, agent.scope())
    plan.owner_hold(conn, agent.scope(), top["id"], "nothing new this week", "owner", h.now(agent))
    print("held:", plan.new_things_held(conn, agent.scope()))
    vs = [(v["id"], v["stage"], v["title"][:40]) for v in ventures.all_ventures(conn, agent.scope())]
print("ventures:", vs)
ctx = working(agent)  # a cycle with no line yet (no step ready)
refused = call(ctx, "project_create", {"title": "Wedding planner", "hypothesis": "Couples pay 9 EUR", "status": "active"})
print("without venture_id:", refused.ok, refused.text[:120])
idea = next(v for v in vs if v[1] == "idea")
ctx = working(agent)
made = call(ctx, "project_create", {"title": "Wedding planner", "hypothesis": "Couples pay 9 EUR", "status": "active",
                                    "venture_id": idea[0]})
print(f"with venture_id #{idea[0]} ({idea[1]}):", made.ok, made.text[:120])
print(h.keep(agent))
