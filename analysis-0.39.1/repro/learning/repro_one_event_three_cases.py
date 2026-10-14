"""LEARN: one owner decision (killing a venture that has two product lines) settles three subjects; the daily review's
retrospectives of them become three cases, and learning.adopt counts them as three agreeing cases: an 'established'
principle the same day, from a single event."""
import json, os, pathlib, tempfile
d = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("SCR")))
os.environ.update(EMBER_DATA_DIR=str(d), EMBER_SCHEDULER="off", EMBER_FAKE_DELAY_MS="0")
from app.agent import learning, review, store, ventures
from app.economy.clock import to_iso
from tests.test_etsy import started
from tests.test_owner_loop import owner

agent, _ = started(d)
scope = agent.scope()
now = lambda: to_iso(agent.clock.now())
with agent.db.transaction() as conn:
    vid = ventures.create(conn, scope, title="Teacher planners", pitch="Printable planners for teachers.",
                          stage="researching", now=now(), created_by="agent")
    cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
    lines = [store.create_project(conn, scope, cycle_id=cycle, title=f"Teacher planner {x}", hypothesis="teachers buy",
                                  next_step="", status="active", now=now(), venture_id=vid) for x in "AB"]
agent.clock.advance(minutes=1)
since = now()
agent.clock.advance(minutes=5)
r = owner(agent).decide_venture(vid, {"action": "kill", "comment": "Teachers don't buy planners on Etsy."}, "Stefan")
print("owner kill:", r.status)
agent.clock.advance(hours=12)
with agent.db.connection() as conn:
    items = learning.settled(conn, scope, since)
print("settled:", [i.subject for i in items])
lesson = "Teacher printables don't sell on Etsy: drop the niche, the owner killed it"
answer = {"verdicts": [], "working": "", "not_working": "", "owner_feedback": "", "lesson": "", "focus": "x",
          "retros": [{"subject": i.subject, "expected": "sales", "happened": "killed by the owner",
                      "why": "the owner saw no demand", "cause": "wrong_idea", "sure": "high", "lesson": lesson}
                     for i in items]}
parsed = review.parse(json.dumps(answer), set(), {i.subject for i in items})
with agent.db.transaction() as conn:
    made = learning.save_cases(conn, scope, None, parsed.retros, items, now())
    happened = learning.adopt(conn, scope, now())
print("cases:", made)
for h in happened:
    print(" ", h)
with agent.db.connection() as conn:
    for p in learning.principles(conn, scope):
        print("principle:", learning.principle_line(p))
