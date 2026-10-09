"""One rescored score marks all six 'from research': a brainstorm idea with five guessed scores passes the proposal
gate's 'all six scores from research', and its node is drawn as researched."""
import h
from app.agent import econ, evidence, ventures, views
from tests.test_ventures import plan as plan_reply, JOURNAL
from app.agent.fake_llm import FakeTransport

agent = h.fresh("r6", fake=FakeTransport(script=[plan_reply(steps=[]), JOURNAL]), cycles=1)
now = h.now(agent)
guess = {"revenue": 3, "doability": 5, "difficulty": 1, "risk": 1, "speed": 5, "cost": 1}  # a brainstorm's guesses
PAGE = "https://example.invalid/forum/sales"
with agent.db.transaction() as conn:
    vid = ventures.create(conn, agent.scope(), title="Printable wedding planners", pitch="p", stage="idea", now=now,
                          cycle_id=1, scores=guess, scores_by="brainstorm")
    for sources in (3, 4):
        ventures.add_research(conn, vid, 1, None, "q", None, sources, 10, now)
    ventures.update(conn, vid, now, stage="researching", **h.CASE)
    evidence.record_sources(conn, agent.scope(), None, None, [PAGE], now)
    evidence.add(conn, agent.scope(), vid, None, "200 sales a month.", "sales", 200, 200, "orders", "DE", PAGE, now)
    made = econ.Case(channel="etsy_digital", price_eur=4.9, unit_cost_eur=0.0, monthly_costs_eur=0.0,
                     sales=(2, 10, 30), setup_eur=0.0, owner_hours=1.0, first_sale_days=30, api_usd=1.0)
    ventures.add_case(conn, vid, None, made, econ.compute(made), now, "")
c = h.ctx(agent, cycle_id=1)
c.venture = True
c.state.focus_venture_id = vid
c.venture_cash_eur = 20.0
print(h.call(agent, "venture_update", {"venture_id": vid, "revenue": 4, "stage": "proposed"}, c))
row = [r for r in views.ventures_view(agent)["items"] if r["id"] == vid][0] if "items" in views.ventures_view(agent) else None
with agent.db.connection() as conn:
    v = ventures.get(conn, agent.scope(), vid)
    print("stage", v["stage"], "scores_by", v["scores_by"], {k: v[k] for k in ventures.SCORE_FIELDS})
    print(ventures.scores_text(v))
