"""0.40.3: the mail fixes of analysis-0.40.1 (codebase), M1 and M2; its reproductions are
integrations/r1_optout_phrases.py, test_r2_unread_reply_send.py, r4_mime_optout.py and test_r5_stale_approval.py.

- M1: an approved email went out although its recipient had answered after the request, unread by the agent, in words
  the opt-out check missed ("ich möchte Sie bitten, mich nicht mehr zu kontaktieren"). Now it waits until the agent
  has read that email; the check knows more phrasings, judges a quoted instruction by its sentence, strips a mail
  gateway's subject tag, reads the HTML part when the plain-text part only stands in for it, and reads 8-bit text that
  isn't UTF-8 as Windows-1252.
- M2: an approved email held by a pause went out weeks later on Resume. Now one not begun within APPROVED_DAYS of its
  approval is closed unsent, with why.
"""

from __future__ import annotations

import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import pytest

from app.agent.service import Agent
from app.integrations import executor
from app.integrations.mail import parse_message
from app.integrations.optout import opt_out
from tests.channels.test_approval_timing import ANN, VERIFIED, from_ann
from tests.cycle.test_agent import make_agent, plan, rows, text
from tests.cycle.test_agent import tools as calls
from tests.mail.test_executor import REPLY, FakeSMTP, approval, approve, dashboard_row, ids, proposing
from tests.mail.test_executor import smtp as smtp  # noqa: F401 - the fixture
from tests.mail.test_mail import JOURNAL, FakeIMAP, live_agent, tool_results
from tests.mail.test_mail import imap as imap  # noqa: F401 - the fixture

# Ann's answer after the request, in words the opt-out check doesn't know: only the agent can read it as a stop
UNKNOWN_STOP = "Guten Tag,\n\nbitte verschonen Sie mich künftig mit Ihren Angeboten.\n\nAnn"


def asked_and_approved(data_dir: Path, imap: type[FakeIMAP], *later: list[Any]) -> tuple[Agent, int]:
    """A live agent whose reply to Ann's question is approved; ``later``: the outcomes of its next cycles."""
    imap.mails = {1: from_ann(1, "Hello?", "Do you have the planner in German?")}
    agent = live_agent(data_dir, [*proposing({**REPLY, "subject": "Re: Hello?"}), *(o for c in later for o in c)])
    assert agent.run_cycle("schedule").status == "completed"
    [request] = ids(agent)
    approve(agent, request)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    return agent, request


def reading(*turns: Any) -> list[Any]:
    return [plan(steps=["read my mail"]), *turns, text("Done."), JOURNAL]


def in_thread(uid: int, sender: str, body: str, reply_to: str) -> bytes:
    raw = from_ann(uid, "Re: Hello?", body, reply_to)
    return raw.replace(f"Ann <{ANN}>".encode(), sender.encode())


# --- M1: the real net: an approved email waits while its recipient's newer email is unread ---


def test_an_approved_email_waits_until_the_agent_has_read_its_recipients_newer_email(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    assert opt_out("Re: Hello?", UNKNOWN_STOP) is None  # the check alone can't catch it
    agent, request = asked_and_approved(
        data_dir,
        imap,
        reading(calls(("email_read", {"email_id": 2})), calls(("mark_opt_out", {"email_id": 2, "reason": "spare me"}))),
    )
    imap.mails[2] = from_ann(2, "Re: Hello?", UNKNOWN_STOP, "<m1@example.org>")
    assert agent.execute_approved() == [(request, executor.WAITING_REPLY)]  # before: sent
    assert smtp.instances == [] and rows(agent, "SELECT * FROM email_actions") == []
    assert agent.execute_approved() == [(request, executor.WAITING_REPLY)]  # it keeps waiting, round after round
    shown = dashboard_row(agent, request)["execution"]
    assert shown == {
        "status": "waiting",
        "started_at": None,
        "finished_at": None,
        "result": executor.REPLIED.format(email_id=2),
        "error": None,
    }
    assert agent.run_cycle("schedule").status == "completed"  # the agent reads it, and marks the stop
    planned = json.dumps([r["messages"] for r in agent.transport.sent], ensure_ascii=False)  # type: ignore[attr-defined]
    assert f'Your approved email of request #{request} to \\"{ANN}\\" waits until you read email #2' in planned
    [read] = tool_results(agent, "email_read")
    assert f"Your approved email of request #{request} waited for you to read this" in read["result"]
    assert agent.execute_approved() == [(request, "failed")]
    assert smtp.instances == [] and approval(agent, request)["result_note"] == f"Not sent: {executor.SUPPRESSED}"


def test_once_the_agent_has_read_an_ordinary_answer_the_approved_email_goes_out(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    agent, request = asked_and_approved(data_dir, imap, reading(calls(("email_read", {"email_id": 2}))))
    imap.mails[2] = from_ann(2, "Re: Hello?", "Thanks, I'll stop by on Monday.", "<m1@example.org>")
    assert agent.execute_approved() == [(request, executor.WAITING_REPLY)]
    assert agent.run_cycle("schedule").status == "completed"
    assert agent.execute_approved() == [(request, "sent")] and len(smtp.instances) == 1
    assert dashboard_row(agent, request)["execution"]["status"] == "sent"


def test_an_answer_in_its_thread_from_another_address_holds_it_too(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    agent, request = asked_and_approved(data_dir, imap)
    imap.mails[2] = in_thread(2, "Office <office@example.org>", "Wir haben kein Interesse.", "<m1@example.org>")
    assert agent.execute_approved() == [(request, executor.WAITING_REPLY)] and smtp.instances == []


def test_an_unrelated_email_or_ember_s_own_holds_nothing(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    agent, request = asked_and_approved(data_dir, imap)
    imap.mails[2] = in_thread(2, "Bob <bob@example.org>", "A question about your shop.", "<other@example.org>")
    imap.mails[3] = in_thread(3, "Ember <ember@mail.example>", "A copy of Ember's own.", "<m1@example.org>")
    assert agent.execute_approved() == [(request, "sent")] and len(smtp.instances) == 1


# --- M1: the opt-out check knows more phrasings, judged by their sentence ---

ASKS = [
    # analysis-0.40.1, M1: each missed until 0.40.3
    "Guten Tag,\n\nich möchte Sie bitten, mich nicht mehr zu kontaktieren.\n\nMfG\nK. Weber",
    "Ich bitte Sie, mir nicht mehr zu schreiben.",
    "Bitte unterlassen Sie künftig jede weitere Kontaktaufnahme.",
    "Kein Interesse. Bitte keine weitere Kontaktaufnahme.",
    "Bitte sehen Sie von weiteren E-Mails ab.",
    "Please do not reach out to me again.",
    "Not interested, please remove.",
    "I'd prefer not to receive any further emails from you.",
    "Thanks, but we are not interested. Please don't email me again. Our policy: https://example.com/privacy",
    "Please stop emailing me. You got my address from [some list].",
    # and their neighbours
    "Bitte mich nicht mehr anzuschreiben.",
    "Unterlassen Sie bitte weitere E-Mails an mich.",
    "Ich bitte Sie, weitere Kontaktaufnahmen zu unterlassen.",
    "Ich bitte Sie, von weiteren Kontaktaufnahmen abzusehen.",
    "Sehen Sie bitte künftig von Nachrichten an mich ab.",
    "Sehen Sie bitte davon ab, mir weiter zu schreiben.",
    "Bitte keine Zusendungen mehr.",
    "Ich wünsche keinen weiteren Kontakt.",
    "Please don't reach out again.",
    "Do not reach out to us.",
    "Please stop reaching out.",
    "We would like not to receive more messages.",
    "I'd rather not receive further emails.",
    "Please remove us.",
    "Not interested, please remove us from your records",
    "Please cease all contact.",
]
ORDINARY = [
    "Thanks, I'll stop by on Monday",
    "Stop the press: we love it",
    "Bitte schreiben Sie mir die Details",
    "Don't stop reaching out, we love hearing from you!",
    "Don't reach out to me before Monday, I'm away.",
    "Please remove the old photo.",
    "This will take us off track for a week.",
    "Ich hatte keinen weiteren Kontakt mit dem Verkäufer.",
    "Es gab keine weitere Kommunikation mit DHL dazu.",
    "Ich habe nichts mehr zu schreiben, danke!",
    "If the problem doesn't cease, contact me.",
    "I'd like not to receive the parcel before Friday.",
    "Sehen Sie bitte davon ab, mir die Rechnung zu schicken; ich zahle bar.",
    "Wir freuen uns über Ihre Kontaktaufnahme.",
    # a newsletter's footer still doesn't ask: its link is in the sentence that says how to unsubscribe
    "You get this because you subscribed. Unsubscribe here: https://news.example/u/1",
    "If you no longer wish to receive these emails, unsubscribe at https://news.example/u/1. Thanks for reading.",
]


@pytest.mark.parametrize("body", ASKS)
def test_more_ways_of_asking_to_stop_are_understood(body: str) -> None:
    assert opt_out("Re: Ihr Angebot", body) is not None


@pytest.mark.parametrize("body", ORDINARY)
def test_ordinary_replies_near_the_new_phrases_don_t_ask(body: str) -> None:
    assert opt_out("Re: Ihr Angebot", body) is None


@pytest.mark.parametrize(
    "subject", ["[EXTERNAL] STOP", "[Ext] Re: unsubscribe", "EXTERN: Stopp", "[Extern] AW: Abmelden", "[STOP]"]
)
def test_a_mail_gateway_s_tag_before_the_subject_doesn_t_hide_a_stop(subject: str) -> None:
    assert opt_out(subject, "Sent from my iPhone") == subject


def test_a_tag_before_an_ordinary_subject_asks_nothing() -> None:
    assert opt_out("[EXTERNAL] Re: Your planner", "Sent from my iPhone") is None
    assert opt_out("Caution: stop the press", "Sent from my iPhone") is None


# --- M1: the right part, in the right charset ---


def alternative(plain: str, html: str) -> bytes:
    message = MIMEMultipart("alternative")
    message["From"] = f"Ann <{ANN}>"
    message["To"] = "ember@mail.example"
    message["Subject"] = "Re: Ihr Angebot"
    message["Message-ID"] = "<x@example.org>"
    message.attach(MIMEText(plain, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))
    return message.as_bytes()


def eight_bit(charset: str | None, body: bytes) -> bytes:
    label = f"; charset={charset}" if charset else ""
    return (
        f"Authentication-Results: {VERIFIED}\nFrom: Ann <{ANN}>\nTo: ember@mail.example\nSubject: Re: Angebot\n"
        f"Message-ID: <y@example.org>\nMIME-Version: 1.0\nContent-Type: text/plain{label}\n"
        "Content-Transfer-Encoding: 8bit\n\n"
    ).encode() + body


@pytest.mark.parametrize(
    "plain",
    [
        "Diese Nachricht enthält HTML. / This message contains HTML.",  # a mail program's stub
        "",
        "Hi",  # much shorter
    ],
)
def test_a_stop_only_in_the_html_part_is_read_when_the_plain_part_stands_in_for_it(plain: str) -> None:
    html = "<p>Hallo,</p><p>bitte keine E-Mails mehr.</p><p>Viele Grüße<br>Ann Weber, Weber &amp; Söhne GmbH</p>"
    mail = parse_message(alternative(plain, html), 1)
    assert "bitte keine E-Mails mehr." in mail.body  # before: the stub, or "[This email has no text.]"
    assert opt_out(mail.subject, mail.body) == "bitte keine E-Mails mehr."


def test_a_real_plain_text_part_is_kept() -> None:
    plain = "Hello Ember,\n\nthanks, I'll stop by on Monday.\n\nAnn"
    html = "<p>Hello Ember,</p><p>thanks, I'll stop by on <b>Monday</b>.</p><p>Ann</p>"
    assert parse_message(alternative(plain, html), 1).body == plain


@pytest.mark.parametrize("charset", ["x-unknown", None, "us-ascii", "utf-8"])
def test_latin_1_bytes_under_a_wrong_charset_are_read_as_windows_1252(charset: str | None) -> None:
    mail = parse_message(eight_bit(charset, b"Bitte h\xf6ren Sie auf, mir zu schreiben.\n"), 1)
    assert mail.body == "Bitte hören Sie auf, mir zu schreiben."  # before: "h�ren", and no stop
    assert opt_out(mail.subject, mail.body) is not None


def test_utf_8_bytes_under_an_unknown_charset_stay_utf_8() -> None:
    mail = parse_message(eight_bit("x-unknown", "Bitte hören Sie auf.\n".encode()), 1)
    assert mail.body == "Bitte hören Sie auf."


# --- M2: an approved email that never began doesn't go out weeks later ---


def test_an_approved_email_held_through_a_long_pause_goes_back_instead_of_out(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, proposing(REPLY))
    agent.run_cycle("schedule")
    [request] = ids(agent)
    approve(agent, request)
    agent.economy.set_paused(True)  # the owner pauses Ember before the round sends it
    assert agent.execute_approved() == []
    agent.clock.advance(days=21)  # type: ignore[attr-defined]
    agent.economy.set_paused(False)
    agent.run_policy()  # the first round after Resume, in the scheduler's order
    assert agent.execute_approved() == [(request, "failed")]  # before: "simulated", 21 days late
    assert agent.mailbox.sent == []  # type: ignore[union-attr]
    closed = approval(agent, request)
    assert (closed["status"], closed["closed_by"]) == ("failed", "Ember")
    assert closed["result_note"] == f"Not sent: {executor.STALE.format(days=executor.APPROVED_DAYS)}"
    assert rows(agent, "SELECT * FROM email_actions") == []  # nothing began
    assert closed["seen_cycle_id"] is None  # the agent hears it at its next wake


def test_an_approved_email_within_its_days_still_goes_out(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, proposing(REPLY))
    agent.run_cycle("schedule")
    [request] = ids(agent)
    approve(agent, request)
    agent.economy.set_paused(True)
    agent.clock.advance(days=executor.APPROVED_DAYS - 1)  # type: ignore[attr-defined]
    agent.economy.set_paused(False)
    agent.run_policy()
    assert agent.execute_approved() == [(request, "simulated")]
