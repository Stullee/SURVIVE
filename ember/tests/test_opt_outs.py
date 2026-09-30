"""0.12.0: an email that asks not to be emailed again is caught in several languages and anywhere in the sender's own
words (only "stop", "unsubscribe" and "abmelden" alone on the first line were), the agent marks what the check misses
(mark_opt_out), the owner adds any address, and no new email is dropped (beyond 20 per check, the older ones were)."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.service import Agent
from app.integrations import mail, mailstore
from app.integrations.optout import opt_out
from tests.test_agent import make_agent, plan, rows
from tests.test_agent import tools as calls
from tests.test_mail import mail_cycle, raw_mail, tool_results
from tests.test_owner_api import post

ASKS = [
    ("Re: hi", "Stop"),
    ("Re: hi", "\n  UNSUBSCRIBE!\nplease"),
    ("", "Please stop."),
    ("", "Please remove me from your list."),
    ("", "Hi,\n\nplease take me off your mailing list.\n\nThanks, Jo"),
    ("", "Don't email me again."),
    ("", "Stop emailing me"),
    ("", "Please stop sending me these emails"),
    ("", "No more emails please"),
    ("Unsubscribe", ""),
    ("AW: Unsubscribe", "Danke"),
    ("", "I object to the processing of my data."),
    ("", "Bitte abmelden."),
    ("", "Bitte keine E-Mails mehr."),
    ("", "Schreiben Sie mich bitte nicht mehr an."),
    ("", "Ich widerspreche der Nutzung meiner Daten."),
    ("", "Löschen Sie meine Daten!"),
    ("", "Stopp bitte"),
    ("", "Merci de me désinscrire."),
    ("", "Ne m’écrivez plus."),
    ("", "Por favor, darme de baja."),
    ("", "No me escribas más."),
    ("", "Cancellami dalla lista"),
    ("", "Non scrivetemi più"),
    ("", "Graag afmelden"),
    ("", "Geen e-mails meer aub"),
    ("", "Não quero mais receber"),
    ("", "Proszę mnie wypisać"),
]
DOESNT = [
    ("", "Stop sending me drafts late at night"),
    ("", "Don't stop writing these!"),
    (
        "Re: planner",
        'Thanks!\n\nOn Fri, 2 Oct 2026 at 19:40, Ember wrote:\n> Reply "stop" and Ember won\'t write again.',
    ),
    (
        "Re: planner",
        "I'll buy it.\n\n-- \nThis email was written by Ember. Reply \"stop\" and Ember won't write again.",
    ),
    ("", "Great!\n\nFrom: Ember <ember@example.org>\nSent: Friday\nTo unsubscribe from our newsletter click here"),
    ("", "Is the planner available in German?"),
    ("Question", "Does the download include the stopwatch template?"),
    ("", "Where can I stop by to pick it up?"),
    ("", "Five tips.\n\nYou get this because you subscribed. Unsubscribe [https://news.example/unsubscribe]"),
    ("", "To unsubscribe, click here."),
    ("", "Unsubscribe: https://news.example/u/123"),
]


@pytest.mark.parametrize(("subject", "body"), ASKS)
def test_asking_to_stop_is_understood_in_many_ways(subject: str, body: str) -> None:
    assert opt_out(subject, body) is not None


@pytest.mark.parametrize(("subject", "body"), DOESNT)
def test_other_words_and_quoted_text_dont_count(subject: str, body: str) -> None:
    assert opt_out(subject, body) is None


def test_the_reason_quotes_the_words_that_asked() -> None:
    assert opt_out("Re: Planner", "Hello,\n\nBitte keine E-Mails mehr.\n\nGrüße") == "Bitte keine E-Mails mehr."
    assert opt_out("Unsubscribe", "") == "Unsubscribe"


@pytest.fixture
def db_agent(data_dir: Path) -> Agent:
    agent, _ = make_agent(data_dir, [plan(steps=[])])
    agent.run_cycle("schedule")  # the fake mailbox delivers the reader's email (#1)
    return agent


class Flood(mail.FakeMailbox):
    """A mailbox that got ``total`` new emails at once; one of them asks, in German, not to get any more."""

    def __init__(self, session: int, total: int) -> None:
        super().__init__(session)
        self.total = total

    def fetch_new(self, after_uid: int, limit: int = 20, uidvalidity: Any = None, deadline: Any = None) -> Any:
        uids = list(range(after_uid + 1, self.total + 1))[:limit]
        mails = [
            mail.parse_message(
                raw_mail(u, sender=f"p{u}@example.org", body="Keine Werbung mehr, danke." if u == 57 else f"Q {u}"), u
            )
            for u in uids
        ]
        last = uids[-1] if uids else after_uid
        return mail.FetchResult(mails, last, self.uidvalidity, self.total - last)


def test_a_flood_of_mail_is_read_over_a_few_checks_never_dropped(db_agent: Agent) -> None:
    scope = db_agent.scope()
    box = Flood(scope.session, 131)  # 130 new ones: the reader's email was the first
    first = mailstore.fetch(db_agent.db, db_agent.clock, scope, box)
    assert len(first.stored) == 100 and first.waiting == 30  # five fetches of 20, the oldest first
    assert first.suppressed == ["p57@example.org"]
    events = [e["message"] for e in db_agent.db.recent_events(limit=10)]
    assert "30 more new email(s) wait for the next check of the mailbox" in events
    second = mailstore.fetch(db_agent.db, db_agent.clock, scope, box)
    assert len(second.stored) == 30 and second.waiting == 0
    stored = rows(db_agent, "SELECT uid FROM emails WHERE direction = 'in' AND from_addr LIKE 'p%@example.org'")
    assert sorted(r["uid"] for r in stored) == list(range(2, 132))
    reason = rows(db_agent, "SELECT reason FROM email_suppressions")[0]["reason"]
    assert reason == 'replied "Keine Werbung mehr, danke."'


def test_a_newsletter_or_an_automatic_reply_never_asks(db_agent: Agent) -> None:
    scope = db_agent.scope()
    found = []
    for n, (name, value) in enumerate(
        (("List-Unsubscribe", "<https://news.example/u>"), ("Precedence", "bulk"), ("Auto-Submitted", "auto-replied"))
    ):
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = f"n{n}@news.example", "ember@example.invalid", "Stop"
        message["Message-ID"], message[name] = f"<b{n}@news.example>", value
        message.set_content("Stop\n\nPlease unsubscribe me.")
        found.append(mail.parse_message(message.as_bytes(), 80 + n))
    assert all(m.bulk for m in found)
    assert not mail.parse_message(raw_mail(90, body="Stop"), 90).bulk

    class Box(mail.FakeMailbox):
        def fetch_new(self, after_uid: int, limit: int = 20, uidvalidity: Any = None, deadline: Any = None) -> Any:
            return mail.FetchResult(found, 82, self.uidvalidity)

    fetched = mailstore.fetch(db_agent.db, db_agent.clock, scope, Box(scope.session))
    assert len(fetched.stored) == 3 and fetched.suppressed == []


def test_the_fake_mailbox_reads_the_oldest_first() -> None:
    box = mail.FakeMailbox(1)
    first = box.fetch_new(0, limit=2)
    assert [m.uid for m in first.mails] == [1, 2] and first.waiting == 1 and first.last_uid == 2
    assert [m.uid for m in box.fetch_new(first.last_uid).mails] == [3]


def test_the_agent_marks_an_opt_out_the_check_missed(data_dir: Path) -> None:
    mark = {"email_id": 1, "reason": "She asked me not to write to her again."}
    reply = {"reply_to_email_id": 1, "subject": "Re: Planner", "body": "Sorry!", "reason": "r"}
    agent, _ = mail_cycle(
        data_dir,
        calls(("mark_opt_out", mark), ("mark_opt_out", mark), ("mark_opt_out", {**mark, "email_id": 99})),
        calls(("propose_email", reply), ("email_read", {"email_id": 1})),
    )
    marked = tool_results(agent, "mark_opt_out")
    assert [m["status"] for m in marked] == ["ok", "ok", "error"]
    assert marked[0]["result"] == "The sender of email #1 is never emailed again."
    assert marked[1]["result"] == "The sender of email #1 is already never emailed."
    assert "there is no email #99" in marked[2]["result"]
    [row] = rows(agent, "SELECT address, reason, email_id FROM email_suppressions")
    assert row["reason"] == "asked in email #1: She asked me not to write to her again." and row["email_id"] == 1
    refused = tool_results(agent, "propose_email")[0]
    assert refused["status"] == "error" and "asked not to get emails from you" in refused["result"]
    assert "This sender asked not to get emails" in tool_results(agent, "email_read")[0]["result"]


def test_the_owner_adds_an_address_ember_never_emails(ingress_client: TestClient) -> None:
    added = post(ingress_client, "api/email/suppressions", {"address": "Lena.H@Example.org"})
    assert added.status_code == 201 and added.json() == {"address": "lena.h@example.org", "added": True}
    again = post(ingress_client, "api/email/suppressions", {"address": "lena.h@example.org"})
    assert again.status_code == 200 and again.json()["added"] is False
    for body, field in (({"address": "Lena <lena@example.org>"}, "address"), ({"email": "x@example.org"}, "email")):
        refused = post(ingress_client, "api/email/suppressions", body)
        assert refused.status_code == 422 and refused.json()["field"] == field
    assert post(ingress_client, "api/email/suppressions", {"address": "a@example.org"}, headers={}).status_code == 403
    email = ingress_client.get("api/dashboard").json()["integrations"]["email"]
    assert email["suppressed_count"] == 1
    [shown] = email["suppressed"]
    assert shown["address"] == "lena.h@example.org" and shown["reason"].endswith(" added it")
    script = (Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert '"api/email/suppressions"' in script and "Never emailed" in script
