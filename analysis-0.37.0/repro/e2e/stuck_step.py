"""Model-independent reproduction: a ready step the cycle can't advance is taken every cycle, and every cycle's sleep
is cut to the shortest sleep, until the daily cap stops it.

State: two Etsy products. #A is live (its launch "Pin it twice" step ready, urgency REACH); the agent already proposed
its two pins and they wait for the owner (a realistic live state: pins need the owner's approval). #B is a new product
in research with ready steps. We then run the real keeper and steering for 16 consecutive "cycles" 30 minutes apart,
recording each pick as the loop does (plan.record), and ask slack.sleep what each cycle sleeps.

Usage: python stuck_step.py <data_dir>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

data_dir = Path(sys.argv[1]).resolve()
data_dir.mkdir(parents=True, exist_ok=True)
os.environ["EMBER_DATA_DIR"] = str(data_dir)
os.environ["EMBER_SCHEDULER"] = "off"
os.environ["EMBER_FAKE_DELAY_MS"] = "0"
sys.path.insert(0, ".")

from app.agent import plan, slack, store  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now  # noqa: E402
from tests.test_fixes_0340 import ALL, keep, project, request  # noqa: E402

agent, _ = lined(data_dir, titles=())
a = project(agent, "Bewerbungs-Tracker", "Eine Vorlage für Bewerbungen und Absagen")
b = project(agent, "Meal planner printable", "Busy parents pay 4 EUR for a weekly meal planner")
keep(agent)

# Product A goes live: its listing request was approved and Ember's code listed it.
listing_req = request(agent, a, "etsy_listing")
with agent.db.transaction() as conn:
    conn.execute("UPDATE approvals SET status = 'approved', decided_at = ? WHERE id = ?", (now(agent), listing_req))
    conn.execute("UPDATE approvals SET status = 'done', closed_at = ? WHERE id = ?", (now(agent), listing_req))
    conn.execute(
        "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, listing_id, title,"
        " state, views, favorites, synced_at) VALUES (?, ?, ?, ?, ?, 'active', 900000123, 'Tracker', 'active', 0, 0, ?)",
        (agent.scope().mode, agent.scope().session, listing_req, now(agent), now(agent), now(agent)),
    )
said = keep(agent)
print("keeper said:", [s for s in said if "stage" in s or "live" in s][:6])

# The agent proposed the two pins the step asks for; they wait for the owner.
def pin_request(n: int) -> int:
    with agent.db.transaction() as conn:
        return store.insert_approval(
            conn, agent.scope(), 1, now(agent), project_id=a, type="publish", title=f"Pin {n}",
            description="a pin for the tracker", payload=f'{{"pin": {n}}}', expected_cost="nothing",
            expected_benefit="buyers", executor="pinterest_pin", action=f'{{"pin": {n}}}',
        )


pins = [pin_request(1), pin_request(2)]
print("pending pin requests:", pins)

taken = []
for i in range(16):
    keep(agent)
    with agent.db.transaction() as conn:
        steered = plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
        cid = cycle(agent, steered.line)
        plan.record(conn, agent.scope(), cid, now(agent), steered)
    step = steered.step
    busy = bool(steered.pick.ranked)
    slept = slack.sleep(240, busy, 30, "explore")
    taken.append((i + 1, steered.kind, steered.pick.decided, step.title if step else None, step.product if step else None,
                  round(steered.pick.weight or 0, 2) if hasattr(steered.pick, "weight") else None, slept))
    agent.clock.advance(minutes=30)

for t in taken:
    print(t)
b_steps = rows(agent, f"SELECT title, status, waiting FROM plan_nodes WHERE project_id = {b} AND level = 'step'")
print("product B's steps:", [(s["title"][:40], s["status"], s["waiting"]) for s in b_steps])
pending = rows(agent, "SELECT id, executor, status FROM approvals WHERE status = 'pending'")
print("still pending:", pending)
