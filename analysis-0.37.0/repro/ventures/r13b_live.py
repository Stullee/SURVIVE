import h
from app.agent import ventures, stages
from app.agent.owner import Owner
from tests.test_ventures import plan as plan_reply, JOURNAL, PINTEREST
from app.agent.fake_llm import FakeTransport

agent = h.fresh("r13b", fake=FakeTransport(script=[plan_reply(steps=[]), JOURNAL]), cycles=1)
ready = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name, ready=("pinterest",))
off = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name, ready=())
ready.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Owner")
ready.decide_venture(PINTEREST, {"action": "park", "comment": "Pause"}, "Owner")
off.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Owner")
agent.clock.advance(days=3)
with agent.db.transaction() as conn:  # Pinterest is set up again
    print("keep with Pinterest ready:", stages.keep(conn, agent.scope(), agent.clock.today(), h.now(agent), ready=("pinterest",)))
    v = ventures.get(conn, agent.scope(), PINTEREST)
    print("stage", v["stage"], "| test", v["test_milestone_id"], "| open first tests:",
          conn.execute("SELECT COUNT(*) FROM milestones WHERE venture_id = ? AND status = 'open'", (PINTEREST,)).fetchone()[0])
    print("stage rule:", ventures.stage_rule(v))
