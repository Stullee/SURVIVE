"""VENT: the Pinterest venture's first test (pin_clicks >= 10, set by Ember's code when the owner backs it) counts the
clicks of every active pin ever made, for any product line and before the backing; so it is met the moment it is
set, and the agent can then move the venture live: its first test never ran."""
import os, pathlib, tempfile
d = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("SCR")))
os.environ.update(EMBER_DATA_DIR=str(d), EMBER_SCHEDULER="off", EMBER_FAKE_DELAY_MS="0")
from app.agent import metrics
from app.agent.owner import Owner
from app.economy.clock import to_iso
from tests.test_pinterest import pinned
from tests.test_etsy import shop_context, call
from tests.test_agent import rows

agent, fake, request = pinned(d)
print("pins before the backing:", rows(agent, "SELECT id, approval_id, status, clicks FROM pinterest_pins"))
# Pinterest's sync reads the product line's pin: 12 outbound clicks over the weeks before the venture is backed
with agent.db.transaction() as conn:
    conn.execute("UPDATE pinterest_pins SET clicks = 12")
agent.clock.advance(days=20)
[v] = rows(agent, "SELECT id, title, stage FROM ventures WHERE channel = 'pinterest'")
print("venture:", v)
boss = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name, ready=("pinterest",))
r = boss.decide_venture(v["id"], {"action": "back", "confirm": True, "comment": "Try Pinterest properly"}, "Stefan")
print("owner backs it:", r.status, r.body if hasattr(r, "body") else "")
print("first test:", rows(agent, f"SELECT id, metric, target, status, due, created_at FROM milestones WHERE venture_id = {v['id']}"))
# the next plan: Ember's code grades the metric milestones (no new click since the backing)
agent.clock.advance(minutes=10)
print(metrics.grade_all(agent.db, agent.scope(), agent.economy.life.scope(), agent.clock, False))
print("first test now:", rows(agent, f"SELECT id, status, closed_by, result FROM milestones WHERE venture_id = {v['id']}"))
out = call(shop_context(agent), "venture_update", {"venture_id": v["id"], "stage": "live"})
print("agent sets it live:", out.ok, out.text[:120])
print(rows(agent, f"SELECT id, stage FROM ventures WHERE id = {v['id']}"))

# --- part 2: 60 days live; the pins bring a buyer to the shop's listing (an Etsy order of the line's listing) ---
from app.agent import stages, ventures
from tests.test_pinterest import LISTING
agent.clock.advance(days=30)
with agent.db.transaction() as conn:
    conn.execute(
        "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, total, total_cents, currency, items, synced_at,"
        " status) VALUES (?, ?, 4242, ?, '4.50 EUR', 450, 'EUR', ?, ?, 'paid')",
        (agent.scope().mode, agent.scope().session, to_iso(agent.clock.now()),
         f'[{{"listing_id": {LISTING}, "quantity": 1}}]', to_iso(agent.clock.now())))
print("pin clicks now:", rows(agent, "SELECT clicks FROM pinterest_pins"))
print("orders:", rows(agent, "SELECT receipt_id, items FROM etsy_orders"))
agent.clock.advance(days=31)
with agent.db.transaction() as conn:
    happened = stages.keep(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()), ("pinterest",))
print("stages.keep:", happened)
print(rows(agent, f"SELECT id, stage, parked_by FROM ventures WHERE id = {v['id']}"))
