"""Repro: a "stop" written below the quote (Thunderbird's default reply position) is read by the 0.37.3 gate but not
recognised, so the approved email goes out to the person who asked to stop, and their stop becomes an inquiry."""
from __future__ import annotations
from email.message import EmailMessage
from pathlib import Path
from tests.conftest import data_dir, ventures_in_the_plan, helper_databases  # noqa: F401 fixtures
from tests.test_mail import imap, FakeIMAP  # noqa: F401
from tests.test_executor import smtp, FakeSMTP, live_approved  # noqa: F401
from tests.test_agent import rows
from app.integrations import mailstore


def stop_below_quote() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Ann <ann@example.org>"
    msg["To"] = "ember@mail.example"
    msg["Subject"] = "Re: Hello?"
    msg["Date"] = "Mon, 28 Sep 2026 09:00:00 +0200"
    msg["Message-ID"] = "<stop-1@example.org>"
    msg["In-Reply-To"] = "<m1@example.org>"
    msg["Authentication-Results"] = "mx.mail.example; dkim=pass header.d=example.org; dmarc=pass header.from=example.org"
    msg.set_content(
        "Am 28.09.26 um 08:00 schrieb Ember (AI agent of Stefan):\n"
        "> Hi Ann,\n>\n> not yet, but I'll tell you when it is.\n>\n> -- \n"
        "> This email was written by Ember, an AI agent, on behalf of Stefan, and approved by them before sending.\n"
        "> Reply \"stop\" and Ember won't write to you again.\n\n"
        "Bitte keine E-Mails mehr.\n\nAnn\n"
    )
    return msg.as_bytes()


def test_bottom_posted_stop_is_read_but_not_honoured(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    agent, approval_id = live_approved(data_dir)   # the owner approved an email to ann@example.org
    agent.clock.advance(minutes=10)
    imap.mails[3] = stop_below_quote()             # Ann's stop arrives before the round sends
    out = agent.execute_approved()                 # 0.37.3 gate: reads the mailbox first, then sends
    print("execute_approved ->", out)
    print("stored in-mail uids:", [r["uid"] for r in rows(agent, "SELECT uid FROM emails WHERE direction='in' ORDER BY id")])
    print("suppressions:", rows(agent, "SELECT address, reason FROM email_suppressions"))
    print("sent to:", [c.calls[1][3] for c in smtp.instances])
    with agent.db.connection() as conn:
        print("inquiries waiting for an answer:", [(r["uid"], r["subject"]) for r in mailstore.inquiries(conn, agent.scope())])


def stop_with_photo() -> bytes:
    """A top-posted, plainly worded stop, sent from a phone with a photo attached (1.2 MB): over MAX_MESSAGE_BYTES."""
    msg = EmailMessage()
    msg["From"] = "Ann <ann@example.org>"
    msg["To"] = "ember@mail.example"
    msg["Subject"] = "Re: Hello?"
    msg["Date"] = "Mon, 28 Sep 2026 09:00:00 +0200"
    msg["Message-ID"] = "<stop-2@example.org>"
    msg["In-Reply-To"] = "<m1@example.org>"
    msg.set_content("Please stop emailing me. This is what your last mail looked like on my phone.\n\nAnn\n")
    msg.add_attachment(b"\xff\xd8\xff\xe0" + bytes(900_000), maintype="image", subtype="jpeg", filename="IMG_2041.jpg")
    return msg.as_bytes()


def test_a_stop_with_a_photo_attached_is_not_read(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    agent, approval_id = live_approved(data_dir)
    agent.clock.advance(minutes=10)
    imap.mails[3] = stop_with_photo()
    print("stop email size:", len(imap.mails[3]), "bytes")
    out = agent.execute_approved()
    print("execute_approved ->", out)
    print("stored body of the stop:", rows(agent, "SELECT body FROM emails WHERE uid = 3"))
    print("suppressions:", rows(agent, "SELECT address FROM email_suppressions"))
    print("sent to:", [c.calls[1][3] for c in smtp.instances])
