"""A channel venture the owner parked and backed again while its channel isn't set up keeps its old first test,
which the park dropped as the owner's: the agent can then set it live with no first test met."""
import h
from app.agent import ventures, stages
from app.agent.owner import Owner
from tests.test_ventures import plan as plan_reply, JOURNAL, PINTEREST
from app.agent.fake_llm import FakeTransport

agent = h.fresh("r13", fake=FakeTransport(script=[plan_reply(steps=[]), JOURNAL]), cycles=1)
ready = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name, ready=("pinterest",))
off = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name, ready=())
print("back (Pinterest set up):", ready.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Owner").body)
with agent.db.connection() as conn:
    v = ventures.get(conn, agent.scope(), PINTEREST); test = v["test_milestone_id"]
    print("first test:", test, dict(conn.execute("SELECT status, closed_by FROM milestones WHERE id = ?", (test,)).fetchone()))
print("park:", ready.decide_venture(PINTEREST, {"action": "park", "comment": "Pause"}, "Owner").body)
print("back again (Pinterest switched off meanwhile):", off.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Owner").body)
with agent.db.connection() as conn:
    v = ventures.get(conn, agent.scope(), PINTEREST)
    print("stage", v["stage"], "| test", v["test_milestone_id"], dict(conn.execute("SELECT status, closed_by, result FROM milestones WHERE id = ?", (v["test_milestone_id"],)).fetchone()))
with agent.db.transaction() as conn:
    print("keep:", stages.keep(conn, agent.scope(), agent.clock.today(), h.now(agent), ready=(), unset=("pinterest",)))
c = h.ctx(agent, cycle_id=1)
print(h.call(agent, "venture_update", {"venture_id": PINTEREST, "stage": "live"}, c))
