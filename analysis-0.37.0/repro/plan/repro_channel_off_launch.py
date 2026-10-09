"""Finding: the launch stage lays out a step for every channel of the product's audience whether or not the owner uses
that channel (templates.steps_for filters by audience only). A step of a channel that isn't set up waits ('channel')
forever, so the launch stage (no check of its own: done when all its steps are) never closes, and the maintain stage's
recurring marketing (plan._recurring needs every stage before maintain closed) never comes, in the channels that ARE
set up too."""

from harness import fresh_dir, keep, no_ventures, rows, steer, describe, plan, now
from tests.test_etsy import started

no_ventures()
ONLY_BLUESKY = {"pinterest": False, "bluesky": True, "blog": False}
for channels in (ONLY_BLUESKY, {"pinterest": True, "bluesky": True, "blog": True}):
    d = fresh_dir("chanoff")
    agent, line = started(d)
    keep(agent, channels)
    [prod] = rows(agent, f"SELECT id, audience FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    launch = rows(agent, f"SELECT id, title, channel, status FROM plan_nodes WHERE project_id = {line} AND stage = 'launch'"
                         " AND level = 'step'")
    print(f"=== channels {channels}; audience {prod['audience']}")
    # the launch steps of the channels in use pass (simulated: 2 Bluesky posts live, the critic passes)
    with agent.db.transaction() as conn:
        for s in launch:
            if s["channel"] is None or channels.get(s["channel"]):
                conn.execute("UPDATE plan_nodes SET status = 'done', closed_by = 'code', closed_at = ?, result = 'sim'"
                             " WHERE id = ?", (now(agent), s["id"]))
    for day in (0, 7, 14, 21):
        keep(agent, channels)
        agent.clock.advance(days=7)
    st = rows(agent, f"SELECT stage, status FROM plan_nodes WHERE project_id = {line} AND level = 'stage' ORDER BY seq")
    print("   stages:", [(r["stage"], r["status"]) for r in st])
    print("   launch steps:", [(r["title"][:30], r["status"]) for r in rows(agent, f"SELECT title, status FROM plan_nodes"
          f" WHERE project_id = {line} AND stage = 'launch' AND level = 'step'")])
    s = steer(agent, channels=channels)
    print("   waits:", [(c.step.title[:30], c.waiting) for c in s.found if c.step.product == line and c.waiting])
    rec = rows(agent, f"SELECT title, status FROM plan_nodes WHERE project_id = {line} AND template LIKE 'recurring/%'")
    print("   recurring maintain steps after 4 weeks:", rec)
