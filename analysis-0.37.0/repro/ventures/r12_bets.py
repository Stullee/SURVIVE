"""bets.settle: a bet 'by 2026-09-08' is settled lost at the first cycle of 2026-09-08 (its last day not counted);
a metric milestone due that day is judged only after it."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from tests.test_learning_loop import started, views, place, settle
d = Path(os.environ["EMBER_DATA_DIR"]) / "r12"; d.mkdir(parents=True, exist_ok=True)
for f in d.iterdir(): f.unlink() if f.is_file() else None
agent, project = started(d)
views(agent, 20)
today = agent.clock.today()
due = (today.replace(day=today.day + 7)).isoformat()
print("now:", agent.clock.now().isoformat(), "| placing: +10 views by", due)
print(place(agent, project, f"+10 views by {due}: the pins bring buyers").text)
agent.clock.advance(days=7)
agent.clock.advance(hours=-agent.clock.now().hour + 0, minutes=-agent.clock.now().minute + 5)  # 00:05 on the due day
views(agent, 26)
print("now:", agent.clock.now().isoformat(), "| settle:", settle(agent))
