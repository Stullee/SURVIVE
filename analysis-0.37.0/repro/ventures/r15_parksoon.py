"""desk.item/stage_rule announce a park the rule won't make (a venture with an open project isn't parked), and make
its step urgent (PARK_SOON) for that: the seeded Etsy leg with its product lines."""
import h
from datetime import timedelta
from app.agent import desk, stages, ventures, store
from tests.test_ventures import plan as plan_reply, JOURNAL
from app.agent.fake_llm import FakeTransport

agent = h.fresh("r15", fake=FakeTransport(script=[plan_reply(steps=[]), JOURNAL]), cycles=1)
with agent.db.transaction() as conn:
    ventures.add_research(conn, 1, 1, None, "q", None, 2, 20_000, h.now(agent))
    pid = store.create_project(conn, agent.scope(), cycle_id=1, title="Printable planners", hypothesis="h",
                               next_step="", status="active", now=h.now(agent), venture_id=1)
agent.clock.advance(days=15)
with agent.db.connection() as conn:
    v = ventures.get(conn, agent.scope(), 1)
    it = desk.item(conn, v, today=agent.clock.today(), cash_eur=20.0)
    print("day 15:", it.tier, "|", it.text)
    print("stage rule:", ventures.stage_rule(v))
agent.clock.advance(days=7)
with agent.db.transaction() as conn:
    print("day 22 keep:", stages.keep(conn, agent.scope(), agent.clock.today(), h.now(agent)))
    v = ventures.get(conn, agent.scope(), 1)
    print("stage:", v["stage"], "| item:", desk.item(conn, v, today=agent.clock.today(), cash_eur=20.0).tier)
