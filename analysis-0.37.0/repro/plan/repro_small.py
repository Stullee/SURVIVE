"""Smaller findings, one block each:
J. YOUR PLAN's "Waiting on your owner" lists every product's template "You approve it" step, also of products still
   in research (nothing was asked), at most 6, ahead of real waits.
K. The day-28 retry of the day-14 bar checks only views (30), not the 2 favorites.
L. The scale step _scale lays out "hers to split" can't be split (source 'code' is fixed)."""

import json
from datetime import timedelta

from harness import fresh_dir, keep, lined, no_ventures, project, rows, steer, describe, plan, now
from tests.test_etsy import started

no_ventures()

# --- J ---
d = fresh_dir("waitowner")
agent, line = started(d)  # one live product (#1)
for title in ("Budget planner", "Meal planner", "Habit tracker", "Wedding planner", "Study planner", "Fitness tracker"):
    project(agent, title, "Someone pays 5 EUR for the Etsy download")
keep(agent)
with agent.db.transaction() as conn:  # the live product reaches a decide-by date: the owner decides keep or drop
    [p] = plan.nodes(conn, agent.scope(), "level = 'product' AND project_id = ?", (line,))
    plan._owner_decides(conn, agent.scope(), p, now(agent), "day 21, no order yet")
with agent.db.connection() as conn:
    text = plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today())
print("J:", [l for l in text.splitlines() if l.startswith("Waiting on your owner")])
print("   real owner steps:", rows(agent, "SELECT id, project_id, title FROM plan_nodes WHERE template = 'decide/owner'"))
print("   stages of the new lines:", sorted({r["stage"] for r in rows(agent, "SELECT n.stage FROM plan_nodes n JOIN"
      " plan_nodes p ON p.id = n.parent_id WHERE n.level='step' AND n.kind='owner' AND n.project_id > 1")}),
      "| current stage of line 2:", plan.current_stage_name(conn, agent.scope(), 2) if False else "")
with agent.db.connection() as conn:
    print("   line 2 is in:", plan.current_stage_name(conn, agent.scope(), 2))

# --- K ---
d = fresh_dir("day28")
agent, line = started(d)
start = (agent.clock.today() - timedelta(days=28)).isoformat()
with agent.db.transaction() as conn:
    conn.execute("UPDATE plan_nodes SET live_since = ?, decide_by = ? WHERE level = 'product' AND project_id = ?",
                 (start, json.dumps({"day7": "missed", "day14": "retry", "day21": "decide"}), line))
    conn.execute("UPDATE etsy_listings SET views = 35, favorites = 0")
keep(agent)
[p] = rows(agent, f"SELECT decide_by FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
print("K: day 28 with 35 views and 0 favorites ->", p["decide_by"])

# --- L ---
d = fresh_dir("scale")
agent, line = started(d)
with agent.db.transaction() as conn:
    [p] = plan.nodes(conn, agent.scope(), "level = 'product' AND project_id = ?", (line,))
    print("L:", plan._scale(conn, agent.scope(), p, now(agent)))
    [s] = plan.nodes(conn, agent.scope(), "template = 'decide/scale'")
    try:
        plan.split_step(conn, agent.scope(), line, s["id"], [{"title": "Variant A", "kind": "create"},
                        {"title": "Variant B", "kind": "create"}], "split the scale step", None, now(agent))
        print("   split ok")
    except plan.PlanError as e:
        print("   split refused:", e)
    print("   _scale docstring:", plan._scale.__doc__.split(",")[1].strip()[:40])
