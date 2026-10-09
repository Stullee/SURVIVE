"""weekly.apply refuses a strategy that names a parked venture by its title: a parked idea's generic title (a product
category) blocks a strategy about a live line of the same kind."""
import h, json
from app.agent import ventures, stages, weekly, obligations
from tests.test_fixes_0300 import looked

agent = h.fresh("r10")
with agent.db.connection() as conn:
    print("projects:", [r["title"] for r in conn.execute("SELECT title FROM projects")])
    print("tree:", [(r["id"], r["title"], r["stage"]) for r in ventures.all_ventures(conn, agent.scope())])
now = h.now(agent)
with agent.db.transaction() as conn:
    vid = ventures.create(conn, agent.scope(), title="Printable planners", pitch="p", stage="idea", now=now, cycle_id=None,
                          scores={"revenue": 2, "doability": 4, "difficulty": 2, "risk": 2, "speed": 4, "cost": 1}, scores_by="brainstorm")
    print(stages.park(conn, agent.scope(), ventures.get(conn, agent.scope(), vid), now, "no one took the idea up within 30 days (triage)"))
    strategy = "Grow the Etsy shop: finish the printable planners for teachers line and pin each listing twice a week."
    print("stale_strategy:", obligations.stale_strategy(conn, agent.scope(), strategy))
