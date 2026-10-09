"""R4: the email executor against crafted inputs: lists, injected headers, the owner's edit, two executors at once,
a crash between the record and the send, and an action stored around the tool."""

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, ".")
data = Path(os.environ["EMBER_DATA_DIR"])

from app.agent import store  # noqa: E402
from app.agent.owner import Owner  # noqa: E402
from app.integrations import executor, mail  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_mail import mail_cycle  # noqa: E402

print("--- email_action refuses ---")
for to, subject, extra in (
    ("a@example.org, b@example.org", "Hi", {}),
    ("Ann <a@example.org>", "Hi", {}),
    ("a@example.org\r\nBcc: c@example.org", "Hi", {}),
    ("a@example.org", "Hi\r\nBcc: c@example.org", {}),
    ("a@example.org", "Hi Bcc: c@example.org", {}),
    ("a@example.org", "Hi\x85Bcc: c@example.org", {}),
    ("a@example.org", "Hi", {"in_reply_to": "<x@y>\r\nBcc: c@example.org"}),
    ("a@example.org", "Hi", {"references": "<x@y> <z@w>\nBcc: c@example.org"}),
):
    try:
        mail.email_action(to, subject, "Body", extra.get("in_reply_to"), extra.get("references"))
        print("ACCEPTED", repr(to), repr(subject), extra)
    except ValueError as exc:
        print("refused:", repr(to)[:40], repr(subject)[:30], "->", exc)

agent, transport = mail_cycle(data, calls(("email_inbox", {})))
cycle_id = rows(agent, "SELECT MAX(id) AS c FROM cycles")[0]["c"]
owner = Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)


def request(action: dict, title: str = "Email") -> int:
    with agent.db.transaction() as conn:
        return store.insert_approval(
            conn, agent.scope(), cycle_id, "2026-09-28T08:00:00Z", type="contact", title=title, description="d",
            payload=f"{title}: {json.dumps(action)}", expected_cost="none", expected_benefit="b", executor="email",
            action=store.canonical(action),
        )


print("--- an action stored around the tool (a list of recipients, an injected header) ---")
bad = request({"to": "a@example.org, b@example.org", "subject": "Hi", "body": "x"}, "bad list")
bad2 = request({"to": "a@example.org", "subject": "Hi\nBcc: c@example.org", "body": "x"}, "bad subject")
for r in (bad, bad2):
    owner.decide(r, {"decision": "approve"}, "Stefan (8f14)")
print(agent.executor.run())
print(rows(agent, f"SELECT id, status, result_note FROM approvals WHERE id IN ({bad}, {bad2})"))
print("handed to the mailbox:", len(agent.mailbox.sent))

print("--- the owner's edit is what is sent; recipient and subject stay ---")
edited = request(mail.email_action("lena.hoffmann@example.org", "Re: planner", "Agent's text"), "edited")
said = owner.decide(edited, {"decision": "approve_with_changes", "final_payload": "Owner's own text\nline 2"},
                    "Stefan (8f14)")
print("decide:", said.status, said.body["approval"]["status"])
print(agent.executor.run())
sent = agent.mailbox.sent[-1]
print("To:", sent["To"], "| Subject:", sent["Subject"])
print("Body:", repr(sent.get_content()[:200]))

print("--- two executors at once, a slow send ---")
twins = [request(mail.email_action("lena.hoffmann@example.org", f"Twin {n}", f"t{n}"), f"twin{n}") for n in (1, 2)]
for r in twins:
    owner.decide(r, {"decision": "approve"}, "Stefan (8f14)")
original = agent.mailbox.send
calls_made = []


def slow(message, to):
    calls_made.append(message["Subject"])
    time.sleep(0.3)
    return original(message, to)


agent.mailbox.send = slow
other = executor.Executor(agent.db, agent.clock, agent.settings, agent.scope, agent.mailbox)
results = []
threads = [threading.Thread(target=lambda e=e: results.append(e.run())) for e in (agent.executor, other)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("results:", results)
print("sends:", calls_made)
agent.mailbox.send = original

print("--- a crash between the record and the send ---")
agent.clock.advance(days=1)  # a new day: room under the daily limit
crash = request(mail.email_action("lena.hoffmann@example.org", "Crash", "c"), "crash")
owner.decide(crash, {"decision": "approve"}, "Stefan (8f14)")


def dies(message, to):
    raise SystemExit("the app stopped")


agent.mailbox.send = dies
try:
    agent.executor.run()
except SystemExit:
    print("the process 'died' during the send")
agent.executor._lock = threading.Lock()  # a new process
agent.mailbox.send = original
print("before recover:", rows(agent, f"SELECT status FROM email_actions WHERE approval_id = {crash}"))
print("recover() ->", agent.executor.recover())
print("after recover:", rows(agent, f"SELECT x.status, a.status AS request, a.result_note FROM email_actions x JOIN approvals a ON a.id = x.approval_id WHERE x.approval_id = {crash}"))
before = len(agent.mailbox.sent)
print("run again ->", agent.executor.run(), "| sent again:", len(agent.mailbox.sent) - before)
print("sent today counted:", executor.sent_today(agent.db._open(), agent.clock, agent.scope()))
