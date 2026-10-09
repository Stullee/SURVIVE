"""Dry run with the fake model, cycles 2 hours apart for 5 days: venture cycles aimed at the seeded Etsy leg."""
import h
from app.agent.fake_llm import FakeTransport
from app.agent import ventures

fake = FakeTransport()
agent = h.fresh("r8e", fake=fake, cycles=0)
for _ in range(60):
    agent.run_cycle("schedule")
    agent.clock.advance(hours=2)
with agent.db.connection() as conn:
    leg = ventures.get(conn, agent.scope(), 1)
    total = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    vc = conn.execute("SELECT id, venture_id FROM cycles WHERE venture = 1").fetchall()
    print("cycles:", total, "| venture cycles:", len(vc), "| aimed at #1 (Etsy leg):", [r["id"] for r in vc if r["venture_id"] == 1])
    print("venture #1:", leg["stage"], "| researched", leg["researched"], "| research spent $%.3f" % (leg["research_spent"] / 1e6))
    print("projects:", [tuple(r) for r in conn.execute("SELECT id, venture_id, status FROM projects")])
