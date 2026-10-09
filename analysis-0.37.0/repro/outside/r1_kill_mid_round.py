"""R1: the kill switch pressed while the scheduler's round is sending approved emails.

Three emails the owner approved wait. The fake mailbox's send() presses the kill switch (as the owner would, from
the dashboard, in another thread) while the first one is being handed over. Does the round stop?
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
data = Path(os.environ["EMBER_DATA_DIR"])

from app.agent import store  # noqa: E402
from app.agent.owner import Owner, kill  # noqa: E402
from app.integrations import mail  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_mail import mail_cycle  # noqa: E402

agent, transport = mail_cycle(data, calls(("email_inbox", {})))  # the reader's email #1 arrives
cycle_id = rows(agent, "SELECT MAX(id) AS c FROM cycles")[0]["c"]
owner = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)
made = []
with agent.db.transaction() as conn:
    for n, to in enumerate(("lena.hoffmann@example.org", "a@example.org", "b@example.org"), 1):
        action = mail.email_action(to, f"Hello {n}", f"Text {n}")
        made.append(
            store.insert_approval(
                conn, agent.scope(), cycle_id, "2026-09-28T08:00:00Z",
                type="contact", title=f"Email {n}", description="d", payload=f"To: {to}\n\nText {n}",
                expected_cost="none", expected_benefit="b", executor="email", action=store.canonical(action),
            )
        )
for approval_id in made:
    assert owner.decide(approval_id, {"decision": "approve"}, "Stefan (8f14e45f)").status == 200

original = agent.mailbox.send
pressed = []


def send(message, to):
    if not pressed:  # the owner presses the kill switch while the first email is handed over
        reply = kill(agent.db, agent.economy, agent.settings.agent_name, {"confirm_name": agent.settings.agent_name},
                     "Stefan (8f14e45f)")
        pressed.append(reply.body)
    return original(message, to)


agent.mailbox.send = send
print("daily limit:", agent.settings.email_daily_limit)
print("execute_approved ->", agent.execute_approved())
print("kill reply:", pressed)
print("life state now:", agent.economy.life.evaluate().state)
print("emails handed to the mailbox after the kill was pressed:", len(agent.mailbox.sent) - 1, "of", len(made))
print("email_actions:", rows(agent, "SELECT approval_id, status FROM email_actions ORDER BY approval_id"))
print("executor_blocked() now:", agent.executor_blocked())
