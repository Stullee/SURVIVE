"""LEARN: bets.settle reads the funnel as the last Etsy sync left it, however old: with the sync failing for days, a
bet is settled 'lost' on numbers from before most of its window. metrics.read refuses the same numbers (FRESH_HOURS)."""
import os, pathlib, tempfile
d = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("SCR")))
os.environ.update(EMBER_DATA_DIR=str(d), EMBER_SCHEDULER="off", EMBER_FAKE_DELAY_MS="0")
from app.agent import bets, metrics
from app.economy.clock import to_iso
from app.integrations import etsy_publisher
from tests.test_etsy import started, shop_context, call
from tests.test_agent import rows

agent, project = started(d)
scope = agent.scope()
with agent.db.transaction() as conn:
    conn.execute("UPDATE etsy_listings SET views = 40, favorites = 0")
print(call(shop_context(agent), "project_update",
           {"project_id": project, "bet": "+2 favorites in 7 days: the new cover photo"}).text[:100])
# the Etsy sync works once more (an hour later), then fails for the rest of the week
agent.clock.advance(hours=1)
agent.db.set_meta(etsy_publisher.meta_key(scope.mode, "last_sync_at"), to_iso(agent.clock.now()))
agent.clock.advance(days=7)
print("last Etsy sync:", agent.db.get_meta(etsy_publisher.meta_key(scope.mode, "last_sync_at")), "now:", to_iso(agent.clock.now()))
with agent.db.transaction() as conn:
    print(bets.settle(conn, scope, agent.clock.today(), to_iso(agent.clock.now())))
    row = {"metric": "favorites_total", "project_id": project, "venture_id": None,
           "created_at": "2026-09-01T00:00:00Z", "target": 2}
    books = metrics.Books(agent.economy.life.scope(), agent.clock,
                          agent.db.get_meta(etsy_publisher.meta_key(scope.mode, "last_sync_at")))
    print("a milestone on the same numbers:", metrics.read(conn, scope, row, books, to_iso(agent.clock.now())))
