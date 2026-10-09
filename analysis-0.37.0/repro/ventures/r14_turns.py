"""Debug: do venture cycles and new products take turns while no product step is ready?"""
import h
from collections import Counter
from app.agent.fake_llm import FakeTransport
from app.agent import plan, ventures
from app.economy.clock import to_iso

fake = FakeTransport()
agent = h.fresh("r14", fake=fake, cycles=0)
for i in range(24):
    with agent.db.connection() as conn:
        last = conn.execute("SELECT id, venture FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
    agent.run_cycle("schedule")
    with agent.db.connection() as conn:
        cyc = conn.execute("SELECT id, venture, project_id FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
        pick = conn.execute("SELECT kind, node_id, ranked FROM plan_picks WHERE cycle_id = ?", (cyc["id"],)).fetchone()
        # what the scorer saw for this cycle (re-run with the same 'before')
        _, found = plan.choose(conn, agent.scope(), to_iso(agent.clock.now()), agent.clock.today(),
                               plan.channels_from(agent.settings), exploring=True, before=cyc["id"])
        prod_ready = [c for c in found if c.step.product is not None and c.waiting is None]
        reasons = Counter(c.waiting for c in found if c.stage == "venture")
    print(cyc["id"], "V" if cyc["venture"] else "O", "| prev venture:", bool(last and last["venture"]),
          "| product steps ready now:", len(prod_ready), "| venture reasons:", dict(reasons))
    agent.clock.advance(hours=2)
