"""End to end: an ordinary cycle on the Etsy line researches; the Etsy leg's budget refuses it."""
import h
from app.agent import ventures, plan
from app.agent.fake_llm import FakeTransport, ToolCalls, Reply
from tests.test_ventures import plan as plan_reply, JOURNAL
from tests.test_owner_loop import owner

fake = FakeTransport()
agent = h.fresh("r8c", fake=fake, cycles=6)
with agent.db.transaction() as conn:
    cyc = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
    for _ in range(12):
        ventures.add_research(conn, 1, cyc, None, "etsy keywords", None, 3, 50_000, h.now(agent))
    top = plan.ventures_node(conn, agent.scope())
print("hold new things:", owner(agent).hold_product(int(top["id"]), {"why": "only products this week"}, "Owner").status)
for i in range(4):
    fake.script.extend([plan_reply(steps=["research demand"]),
                        ToolCalls([("research", {"question": f"What do German buyers search for planner set {i}?"})]),
                        Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
with agent.db.connection() as conn:
    for r in conn.execute("SELECT t.cycle_id, y.project_id, y.venture, t.status, substr(t.result,1,170) AS r FROM tool_calls t"
                          " JOIN cycles y ON y.id = t.cycle_id WHERE t.tool = 'research' AND t.cycle_id > ?", (cyc,)):
        print(dict(r))
    print("line #1's venture:", conn.execute("SELECT venture_id FROM projects WHERE id = 1").fetchone()[0])
