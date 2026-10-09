"""Design check: a product given "one more try until day 28" at day 14 (too little reach) is still put to the owner
at day 21 (no order), in the middle of its retry, and if they keep it, again at day 28."""

import json
from datetime import date, timedelta

from harness import fresh_dir, keep, no_ventures, rows, plan, now
from tests.test_etsy import started

no_ventures()
d = fresh_dir("retry")
agent, line = started(d)
start = agent.clock.today() - timedelta(days=21)
with agent.db.transaction() as conn:
    conn.execute("UPDATE plan_nodes SET live_since = ?, decide_by = ?, pushed_until = ? WHERE level = 'product'"
                 " AND project_id = ?", (start.isoformat(), json.dumps({"day7": "missed", "day14": "retry"}),
                                         (start + timedelta(days=28)).isoformat(), line))
print("day 21:", [s for s in keep(agent) if "decide" in s or "day" in s])
[p] = rows(agent, f"SELECT decide_by, pushed_until FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
print("   ", p)
[step] = rows(agent, "SELECT id, title FROM plan_nodes WHERE template = 'decide/owner' AND status = 'open'")
print("    owner step:", step)
with agent.db.transaction() as conn:
    plan.owner_keep(conn, agent.scope(), step["id"], "give it its retry", "Owner", now(agent))
agent.clock.advance(days=7)
print("day 28:", [s for s in keep(agent) if "decide" in s or "day" in s])
print("    open owner steps:", rows(agent, "SELECT id, title FROM plan_nodes WHERE template = 'decide/owner' AND status = 'open'"))
