"""Dry run, many cycles with the fake model: where the Etsy leg's research budget goes, and what is refused."""
import h, json
from app.agent.fake_llm import FakeTransport
from app.agent import ventures

fake = FakeTransport()
agent = h.fresh("r8b", fake=fake, cycles=0)
for day in range(6):
    for _ in range(6):
        agent.run_cycle("schedule")
    agent.clock.advance(hours=24)
with agent.db.connection() as conn:
    leg = ventures.get(conn, agent.scope(), 1)
    print("venture #1 stage:", leg["stage"], "| research spent:", leg["research_spent"], "| researched:", leg["researched"])
    rows = conn.execute("SELECT r.cycle_id, y.venture, y.venture_id, y.project_id, r.cost_micros, substr(r.question,1,60) q"
                        " FROM venture_research r JOIN cycles y ON y.id = r.cycle_id WHERE r.venture_id = 1").fetchall()
    for r in rows:
        print("  cycle", r["cycle_id"], "venture cycle" if r["venture"] else "ordinary/marketing", "focus v", r["venture_id"], "line", r["project_id"], r["cost_micros"], r["q"])
    refused = conn.execute("SELECT cycle_id, substr(result,1,160) r FROM tool_calls WHERE tool='research' AND status='error'").fetchall()
    print("refused research:", [dict(x) for x in refused][:5])
    picks = conn.execute("SELECT y.id, y.venture, y.venture_id FROM cycles y WHERE y.venture = 1").fetchall()
    print("venture cycles and their venture:", [(p["id"], p["venture_id"]) for p in picks])
