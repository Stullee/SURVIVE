"""LEARN: a bet 'by <day>' (or 'in N days') is settled at the first plan of that day: the named day itself never
counts, while a milestone due that day is judged only after it (metrics.grade: past = today > due)."""
import os, pathlib, tempfile
d = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("SCR")))
os.environ.update(EMBER_DATA_DIR=str(d), EMBER_SCHEDULER="off", EMBER_FAKE_DELAY_MS="0")
from datetime import datetime, timedelta
from app.agent import bets
from app.economy.clock import to_iso
from tests.test_etsy import started, shop_context, call
from tests.test_agent import rows

agent, project = started(d)
with agent.db.transaction() as conn:
    conn.execute("UPDATE etsy_listings SET views = 40, favorites = 0")
print("tz:", agent.clock.tz, "placed at", agent.clock.now().astimezone(agent.clock.tz))
due_day = agent.clock.today() + timedelta(days=3)
out = call(shop_context(agent), "project_update",
           {"project_id": project, "bet": f"+2 favorites by {due_day.isoformat()}: the new cover photo"})
print(out.text[:160])
# the first plan of the due day: 00:05 local time
local = datetime.combine(due_day, datetime.min.time(), tzinfo=agent.clock.tz) + timedelta(minutes=5)
agent.clock.advance(seconds=(local - agent.clock.now()).total_seconds())
print("settling at", agent.clock.now().astimezone(agent.clock.tz), "today =", agent.clock.today())
with agent.db.transaction() as conn:
    print(bets.settle(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now())))
# two favorites arrive later that same day: too late, the bet is settled
with agent.db.transaction() as conn:
    conn.execute("UPDATE etsy_listings SET favorites = 2")
print(rows(agent, "SELECT id, status, due, settled_at FROM bets"))
