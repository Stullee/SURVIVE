"""Finding: a promise of pins/posts whose words name no single product becomes an Owner-project step without its
channel, so steer() makes it an ORDINARY cycle with no line, whose tools refuse propose_pin/propose_bluesky_post
(marketing_apart). The promise can't be kept in the cycles the plan gives it, and at worth 5 x urgency floor it keeps
taking them."""

from harness import fresh_dir, keep, lined, no_ventures, promise, rows, steer, describe, plan
from app.agent import tools

no_ventures()
d = fresh_dir("ownerpins")
agent, _ = lined(d)
keep(agent)
pid = promise(agent, "Pin each live listing twice on Pinterest this week", days=3)
keep(agent)
[step] = rows(agent, f"SELECT id, parent_id, kind, channel, project_id FROM plan_nodes WHERE obligation_id = {pid}")
print("promise step:", step, "| promise_channel(words) =", plan.promise_channel("Pin each live listing twice on Pinterest this week"))
s = steer(agent)
print("steer:", describe(s))
with agent.db.connection() as conn:
    apart = not plan.on_channel(conn, agent.scope(), s.step)
print("cycle kind:", s.kind, "| line:", s.line, "| marketing_apart (loop.py:520):", apart)
print("propose_pin offered in this cycle:", tools.offered("propose_pin", mail=False, workshop=False, etsy=True,
      venture=False, library=False, pinterest=True, marketing=s.kind == "marketing",
      marketing_apart=apart and s.kind == "ordinary"))
