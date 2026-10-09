"""The daily review tells the agent to close what it stopped with project_update, which refuses since 0.35.0; the next
review then holds it to a stop it can't carry out."""
import h
from app.agent import review
from app.agent.fake_llm import FakeTransport
from tests.test_fixes_0300 import judged

fake = FakeTransport()
agent = h.fresh("r3", fake=fake, cycles=2)  # the fake starts line #1
judged(agent, (1, "stop", "demand"))
with agent.db.connection() as conn:
    today = review.of_day(conn, agent.scope(), agent.clock.today())
    print("PLANNER TEXT:\n" + review.planner_text(conn, today))
for status in ("abandoned", "done", "failed", "succeeded"):
    print(status, "->", h.call(agent, "project_update", {"project_id": 1, "status": status}))
agent.run_cycle("schedule")  # the fake holds the product (what 0.35.0 lets it do)
print([dict(r) for r in agent.db.connection().__enter__().execute("SELECT id, status FROM projects")])
agent.clock.advance(days=1)
from tests.test_review import scorecard
text = scorecard(agent).text
i = text.find("YOUR LAST REVIEW")
print(text[i:i + 200])
