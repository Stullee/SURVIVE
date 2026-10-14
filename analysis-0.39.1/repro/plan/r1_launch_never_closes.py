"""R1: a live Etsy product whose critic hasn't passed keeps its launch stage open forever: its weekly pins/posts
(maintain's recurring steps) are never laid out, and the day-7 "marketing comes first" push has no step to push."""
import h
from datetime import timedelta
from app.agent import plan, weights
from tests.test_fixes_0353 import launched, critic

agent, line, listing = launched(h.DATA)
print("listing", listing, "line", line)
# the launch's first marketing is out: close its pin/post/blog steps as their checks would (2 pins, 2 posts, 1 blog live)
with agent.db.transaction() as conn:
    for s in plan.nodes(conn, agent.scope(), "project_id = ? AND level = 'step' AND stage = 'launch' AND status = 'open'"
                        " AND check_kind IN ('pin','post','blog')", (line,)):
        plan._close(conn, s["id"], h.now(agent), "done", "Ember's code: its check passed")
critic(agent, line, listing, 5)  # the critic: improve, score 5 (live: 10 checks scored 5 or 6, none passed)
for day in (3, 8, 15, 30):
    agent.clock.advance(days=day - (0 if day == 3 else prev))
    prev = day
    said = h.keep(agent)
    prod = h.rows(agent, "SELECT live_since, decide_by, pushed_until FROM plan_nodes WHERE level='product' AND project_id=?", (line,))[0]
    stages = {r["stage"]: r["status"] for r in h.rows(agent, "SELECT stage, status FROM plan_nodes WHERE level='stage' AND project_id=?", (line,))}
    opened = [(r["id"], r["title"], r["status"]) for r in h.rows(agent, "SELECT id, title, status FROM plan_nodes WHERE level='step' AND project_id=? AND status='open'", (line,))]
    recurring = h.rows(agent, "SELECT COUNT(*) AS n FROM plan_nodes WHERE project_id=? AND template LIKE 'recurring/%' AND template <> 'recurring/critic'", (line,))[0]["n"]
    print(f"\n== day {day} live; today {agent.clock.today()} ==")
    print("  keep said:", [s for s in said if f"#{line}" in s or "product" in s][:4])
    print("  product:", prod)
    print("  stages:", stages)
    print("  open steps:", opened)
    print("  recurring pin/post/blog steps ever laid out:", recurring)
    cands = [c for c in h.candidates(agent) if c.step.product == line]
    for c in cands:
        print(f"   cand #{c.step.id} {c.step.title[:40]!r} waiting={c.waiting} kind={c.step.kind} urgency={c.step.urgency}"
              f" weight={weights.weigh(c.step).total:.2f}")
