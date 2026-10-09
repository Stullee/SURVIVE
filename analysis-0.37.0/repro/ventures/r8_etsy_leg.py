"""On a new install (no live listing when the tree is planted) the Etsy leg starts 'researching' and nothing ever moves
it on: its product lines' research counts against its $0.60 research budget and is refused once that is spent, and
the plan's Ventures asks venture cycles to appraise the shop itself."""
import h
from app.agent import ventures, tools
from app.agent.fake_llm import FakeTransport

fake = FakeTransport()
agent = h.fresh("r8", fake=fake, cycles=6)
with agent.db.connection() as conn:
    leg = ventures.get(conn, agent.scope(), 1)
    print("venture #1:", leg["title"], "| stage:", leg["stage"])
    print("projects:", [dict(r) for r in conn.execute("SELECT id, title, status, venture_id FROM projects")])
    print("research rows for #1:", [dict(r) for r in conn.execute(
        "SELECT cycle_id, sources, cost_micros, substr(question,1,50) AS q FROM venture_research WHERE venture_id = 1")])
    print("budget text:", ventures.budget_text(leg))
for v in (agent.plan().get("ventures") or {}).get("ventures", []):
    if v["venture"] == 1:
        print("Plan tab, Ventures:", v["title"], "| step:", (v["step"] or {}).get("title"), "| now:", v["needs"])
# the budget spent (as 12 research calls at $0.05 would): an ordinary cycle's research on its Etsy line
with agent.db.transaction() as conn:
    cyc = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
    for _ in range(12):
        ventures.add_research(conn, 1, cyc, None, "etsy keywords for planners", None, 3, 50_000, h.now(agent))
with agent.db.connection() as conn:
    line = conn.execute("SELECT id FROM projects WHERE venture_id = 1 ORDER BY id LIMIT 1").fetchone()
c = h.ctx(agent, cycle_id=cyc)
c.state.one_line = True
c.state.focus_project_id = int(line["id"]) if line else None
c.research = lambda *a, **k: tools.Outcome(True, "researched", "r")
try:
    print("research on line #%s ->" % c.state.focus_project_id, tools._research(c, {"question": "What do German buyers search for printable planners?"}).text)
except tools.ToolError as exc:
    print("research on line #%s -> REFUSED: %s" % (c.state.focus_project_id, exc))
