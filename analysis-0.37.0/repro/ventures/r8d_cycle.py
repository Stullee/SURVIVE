"""End to end: an ordinary cycle on Etsy line #1 (its step pinned by the owner) researches; the Etsy leg's spent
research budget refuses it."""
import h
from app.agent import ventures, plan
from app.agent.fake_llm import FakeTransport, ToolCalls, Reply
from tests.test_ventures import plan as plan_reply, JOURNAL
from tests.test_owner_loop import owner

fake = FakeTransport()
agent = h.fresh("r8d", fake=fake, cycles=6)
with agent.db.transaction() as conn:
    cyc = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
    for _ in range(12):
        ventures.add_research(conn, 1, cyc, None, "etsy keywords", None, 3, 50_000, h.now(agent))
    product = conn.execute("SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = 1").fetchone()
    steps = conn.execute("SELECT n.id, n.title FROM plan_nodes n WHERE n.level = 'step' AND n.status = 'open'"
                         " AND n.kind <> 'owner' AND n.parent_id IN (SELECT id FROM plan_nodes WHERE parent_id = ? OR id = ?)",
                         (product["id"], product["id"])).fetchall()
print("open steps of product #1:", [tuple(s) for s in steps][:3])
print("pin:", owner(agent).pin_step(int(steps[0]["id"]), {"pinned": True}, "Owner").status)
fake.script.extend([plan_reply(steps=["research demand"]),
                    ToolCalls([("research", {"question": "What do German seniors pay for smartphone guides?"})]),
                    Reply("Done."), JOURNAL])
agent.run_cycle("schedule")
with agent.db.connection() as conn:
    for r in conn.execute("SELECT t.cycle_id, y.project_id, y.venture, t.status, substr(t.result,1,220) AS r FROM tool_calls t"
                          " JOIN cycles y ON y.id = t.cycle_id WHERE t.tool = 'research' AND t.cycle_id > ?", (cyc,)):
        print(dict(r))
