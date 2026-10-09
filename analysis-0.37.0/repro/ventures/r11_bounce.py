"""The 21-day research rule counts from the first research call since the stage last changed: moving a venture back to
idea and on to researching again (two venture_update calls) restarts it."""
import h
from datetime import timedelta
from app.agent import ventures, stages
from tests.test_ventures import plan as plan_reply, JOURNAL
from app.agent.fake_llm import FakeTransport

agent = h.fresh("r11", fake=FakeTransport(script=[plan_reply(steps=[]), JOURNAL]), cycles=1)
V = h.DROPSHIPPING
c = h.ctx(agent, cycle_id=1); c.venture = True; c.state.focus_venture_id = V
print(h.call(agent, "venture_update", {"venture_id": V, "stage": "researching"}, c))
with agent.db.transaction() as conn:
    ventures.add_research(conn, V, 1, None, "q", None, 2, 40_000, h.now(agent))
agent.clock.advance(days=20)
with agent.db.connection() as conn:
    print("rule before:", ventures.stage_rule(ventures.get(conn, agent.scope(), V)))
print(h.call(agent, "venture_update", {"venture_id": V, "stage": "idea"}, c))
agent.clock.advance(minutes=1)
print(h.call(agent, "venture_update", {"venture_id": V, "stage": "researching"}, c))
agent.clock.advance(days=2)
with agent.db.transaction() as conn:
    ventures.add_research(conn, V, 1, None, "q2", None, 2, 40_000, h.now(agent))
    print("keep at day 22:", stages.keep(conn, agent.scope(), agent.clock.today(), h.now(agent)))
    print("rule after:", ventures.stage_rule(ventures.get(conn, agent.scope(), V)))
