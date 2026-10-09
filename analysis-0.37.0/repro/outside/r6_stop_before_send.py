"""R6: a "stop" that arrived while Ember wasn't reading its mailbox (kill switch on), and the first round after the
reset. The scheduler's round (agent/scheduler.py:_run) is: run_policy, execute_approved, sync_shop, publish_live,
check_events (which reads the mailbox). Live mode, with the in-process IMAP/SMTP fakes of the tests."""

import os
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, ".")
data = Path(os.environ["EMBER_DATA_DIR"])

from app.agent import plan as plan_tree  # noqa: E402
from app.agent.owner import Owner, apply_kill_switch_reset, kill  # noqa: E402
from app.integrations import mail  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_executor import REPLY, FakeSMTP, FakeSMTPSSL, proposing  # noqa: E402
from tests.test_mail import FakeIMAP, live_agent  # noqa: E402

VERIFIED = "mx1.mail.example; dkim=pass header.d=example.org; dmarc=pass header.from=example.org"


def raw(uid: int, subject: str, body: str, reply_to: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["Authentication-Results"] = VERIFIED
    msg["From"] = "Ann <ann@example.org>"
    msg["To"] = "ember@mail.example"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 28 Sep 2026 08:00:00 +0200"
    msg["Message-ID"] = f"<m{uid}@example.org>"
    if reply_to:
        msg["In-Reply-To"] = reply_to
    msg.set_content(body)
    return msg.as_bytes()


plan_tree._ventures = lambda conn, scope, now: []  # as tests/conftest.py does for every test
FakeIMAP.instances, FakeIMAP.validity = [], 7
FakeIMAP.mails = {1: raw(1, "Hello?", "Do you have the planner in German?")}
mail.imaplib.IMAP4_SSL = FakeIMAP
FakeSMTP.instances, FakeSMTP.fail = [], {}
mail.smtplib.SMTP_SSL, mail.smtplib.SMTP = FakeSMTPSSL, FakeSMTP

agent = live_agent(data, proposing({**REPLY, "subject": "Re: Hello?"}))
assert agent.run_cycle("schedule").status == "completed"
[approval_id] = [r["id"] for r in rows(agent, "SELECT id FROM approvals")]
owner = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)
assert owner.decide(approval_id, {"decision": "approve"}, "Stefan (8f14)").status == 200
apply_kill_switch_reset(agent.db, agent.economy, 0)  # the option's value at the start
print("kill:", kill(agent.db, agent.economy, agent.settings.agent_name, {"confirm_name": "Ember"}, "Stefan").body)

FakeIMAP.mails[2] = raw(2, "Re: Hello?", "Stop. Please don't email me.", "<m1@example.org>")  # Ann asks to stop
for _ in range(3):  # scheduler rounds while the kill switch is on (a day passes)
    agent.run_policy(), agent.execute_approved(), agent.sync_shop(), agent.check_events()
    agent.clock.advance(hours=8)
print("while killed: suppressions =", rows(agent, "SELECT address FROM email_suppressions"),
      "| IMAP reads:", len(FakeIMAP.instances) - 1)

print("reset:", apply_kill_switch_reset(agent.db, agent.economy, 1), agent.economy.life.evaluate().state)
# the first round after the reset, in the scheduler's order
agent.run_policy()
print("execute_approved ->", agent.execute_approved())
agent.sync_shop()
agent.check_events()
sent = [c for s in FakeSMTP.instances for c in s.calls if c[0] == "send_message"]
print("SMTP sends to:", [c[3] for c in sent])
print("suppressions now:", rows(agent, "SELECT address, reason, since FROM email_suppressions"))
print("email_actions:", rows(agent, "SELECT approval_id, status, started_at FROM email_actions"))
