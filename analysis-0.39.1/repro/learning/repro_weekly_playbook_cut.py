"""LEARN: the weekly look's view puts THIS WEEK'S CASES before YOUR PLAYBOOK and is cut from its end until the request
fits (VIEW_STEPS); a busy week's cases push the playbook out, so the only step that merges, disputes and retires
principles by id can't see them."""
import os, pathlib, tempfile
d = pathlib.Path(tempfile.mkdtemp(dir=os.environ.get("SCR")))
os.environ.update(EMBER_DATA_DIR=str(d), EMBER_SCHEDULER="off", EMBER_FAKE_DELAY_MS="0")
from app.agent import context, learning, prompts, store, ventures, weekly
from app.agent.loop import WEEKLY_INPUT_TOKENS
from app.economy.clock import to_iso
from tests.test_etsy import started

agent, _ = started(d)
scope = agent.scope()
now = to_iso(agent.clock.now())
why = "Few views because no pins or posts pointed at the listing during the bet window; the cover was fine."
lesson = "Before betting on views, schedule at least two pins and one Bluesky post for the listing in the window."
with agent.db.transaction() as conn:
    cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
    for i in range(8):  # open product lines
        store.create_project(conn, scope, cycle_id=cycle, title=f"Product line {i} for German buyers",
                             hypothesis="Buyers pay 5 EUR for it", next_step="", status="active", now=now)
    for i in range(20):  # ideas from brainstorms, half of them parked
        vid = ventures.create(conn, scope, title=f"Brainstormed idea number {i}", pitch="An idea. " * 20,
                              stage="idea", now=now)
        if i % 2:
            ventures.update(conn, vid, now, stage="parked", parked_by="agent", notes="x")
    items = [learning.Settled(f"venture #{n}", "parked", None, n) for n in range(1, 31)]
    retros = [{"subject": f"venture #{n}", "expected": "a test", "happened": "parked", "why": why,
               "cause": "wrong_idea", "sure": "medium", "lesson": lesson + f" ({n})"} for n in range(1, 31)]
    learning.save_cases(conn, scope, None, retros, items, now)  # 30 cases this week (about 4 a day)
    for i in range(25):
        conn.execute("INSERT INTO principles (mode, session, text, supports, against, confidence, created_at,"
                     " confirmed_at) VALUES (?, ?, ?, '[1]', '[]', 'hypothesis', ?, ?)",
                     (scope.mode, scope.session, f"Principle {i}: listings with a price in the cover sell more", now, now))
    whole = weekly.view(conn, scope, agent.clock.now(), "MONEY\nBalance $107.07.", "S" * 1500,
                        "\n".join(f'{n}. "rule {n} ' + "r" * 120 + '"' for n in range(1, 11)))
for name in ("YOUR STRATEGY NOW", "PROJECTS", "VENTURES", "THIS WEEK'S CASES", "YOUR PLAYBOOK", "EARLIER CASES"):
    print(f"{name:20} starts at char {whole.find(name):>6}")
print("whole view:", len(whole), "chars")
for chars in weekly.VIEW_STEPS:
    text = weekly.cut(whole, chars)
    if context.fits(prompts.weekly_request(agent.settings, text), WEEKLY_INPUT_TOKENS):
        break
print("the cut the loop sends:", chars, "chars; playbook in it:", "YOUR PLAYBOOK" in text,
      "| principles shown:", text.count("(principle #"))
