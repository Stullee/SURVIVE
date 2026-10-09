"""The owner's Plan tab says a venture is knocked out for 'cash beyond the budget' when its cash is within it."""
import h
from app.agent import desk, econ, evidence, knockouts, plan, ventures
from tests.test_ventures import plan as plan_reply, JOURNAL
from app.agent.fake_llm import FakeTransport

fake = FakeTransport(script=[plan_reply(steps=[]), JOURNAL])
agent = h.fresh("r2", fake=fake, cycles=1)
V = h.DROPSHIPPING
now = h.now(agent)
PAGE = "https://example.invalid/forum/sales"
with agent.db.transaction() as conn:
    cyc = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
    for sources in (1, 2):
        ventures.add_research(conn, V, cyc, None, "q", None, sources, 10, now)
    ventures.update(conn, V, now, stage="researching", scores_by="research", **h.SCORES, **h.CASE)
    evidence.record_sources(conn, agent.scope(), None, None, [PAGE], now)
    evidence.add(conn, agent.scope(), V, None, "Shops sell 200 a month.", "sales", 200, 200, "orders", "DE", PAGE, now)
    made = econ.Case(channel="etsy_digital", price_eur=4.9, unit_cost_eur=0.0, monthly_costs_eur=0.0,
                     sales=(2, 10, 30), setup_eur=10.0, owner_hours=2.0, first_sale_days=30, api_usd=3.0)
    ventures.add_case(conn, V, None, made, econ.compute(made), now, "")
print("venture_cash_eur option:", agent.settings.venture_cash_eur)
with agent.db.connection() as conn:
    row = ventures.get(conn, agent.scope(), V)
    print("knock-outs at the owner's budget:", [k.rule for k in knockouts.active(knockouts.check(conn, row, cash_eur=agent.settings.venture_cash_eur, net_days=None))])
    print("desk.item as YOUR STEP calls it (cash given):", desk.item(conn, row, today=agent.clock.today(), cash_eur=agent.settings.venture_cash_eur).text)
    print("desk.item as the Plan tab calls it (no cash):  ", desk.item(conn, row, today=agent.clock.today()).text)
view = agent.plan()
for v in (view.get("ventures") or {}).get("ventures", []):
    if v["venture"] == V:
        print("Plan tab row for venture #%d: Now: %s" % (V, v["needs"]))
