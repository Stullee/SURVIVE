"""R2: an email reply held in a veto window while the owner pauses Ember for 8 days.

DOCS: "A request you don't decide expires (emails and posts after 7 days ...)". Expiry runs only at the start of a wake
cycle (loop._expire_requests); the scheduler's round runs run_policy and execute_approved before it decides on a cycle.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
data = Path(os.environ["EMBER_DATA_DIR"])

from app.agent import store  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_fixes_0140_unlock_safety import answer, unlock  # noqa: E402
from tests.test_mail import mail_cycle  # noqa: E402
from tests.test_policy import a_milestone  # noqa: E402

agent, transport = mail_cycle(data, calls(("email_inbox", {})))  # the reader's verified email #1
goal = a_milestone(agent)  # a milestone of no project: it covers email replies
unlock(agent, goal, "email_reply", "veto_window")
made = answer(agent, transport, goal)
print("request:", made)
agent.economy.set_paused(True, "Stefan")
print("paused; state:", agent.economy.life.evaluate().state)
agent.clock.advance(days=8)
agent.run_policy()
print("after 8 days paused, status:", rows(agent, f"SELECT status FROM approvals WHERE id = {made['id']}")[0])
agent.economy.set_paused(False, "Stefan")
print("resumed; state:", agent.economy.life.evaluate().state)
with agent.db.connection() as conn:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (made["id"],)).fetchone()
    print("created_at", row["created_at"], "expires_at", store.expires_at(row), "now", to_iso(agent.clock.now()))
# the scheduler's round: run_policy, then execute_approved, before any cycle
agent.run_policy()
print("after run_policy:", rows(agent, f"SELECT status, decided_by, decision_comment FROM approvals WHERE id = {made['id']}"))
print("execute_approved ->", agent.execute_approved())
print("sent:", [m["Subject"] for m in agent.mailbox.sent])
