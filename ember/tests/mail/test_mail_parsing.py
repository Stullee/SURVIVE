"""0.39.5: three defects of Ember's mailbox (analysis-0.39.1, 4.2, 4.3 and 4.12; its reproductions are
repro/mail/stop_below_quote.py, test_stop_below_quote_and_large_mail.py, test_8bit_header_blocks_mailbox.py and
css_comment_timing.py).

1. A "stop" Ember's code couldn't see. Only the words above a quote's first line ("Am ... schrieb ...:") counted, so a
   reply written below the quote (Thunderbird's default) or into it was never recognised; an HTML reply's quote had
   no ">" to mark it; and an email over 1 MB kept its headers only, so a stop sent with a photo wasn't read at all.
   0.37.3's read before sending stored the stop, and the approved email went out anyway (§ 7 UWG).
2. One email stopped the mailbox and every executor. A sender's name in raw 8-bit bytes kept surrogates, SQLite
   refused them, the read's whole batch rolled back (a stop in it too) and every read after it failed the same way,
   unseen. The emails' executor read the mailbox first, so while an approved email waited no listing, pin, post,
   Printify product or upload to the website went out either.
3. The CSS-comment pattern of the hidden-text filter took time growing with the square of a run of unclosed comments,
   and the 100,000-character cap came after it: one HTML email of 1 MB held the scheduler for 24 minutes.

Tests from 0.39.5 and 0.39.7 (formerly test_fixes_0395.py, test_fixes_0397.py)."""

from __future__ import annotations

import imaplib
import time
from collections.abc import Callable
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from app.agent import agenda
from app.integrations import executor, mail, mailstore
from app.integrations.optout import opt_out
from tests.agent_helpers import rows
from tests.mail.test_executor import FakeSMTP, approval, dashboard_row, live_approved
from tests.mail.test_executor import smtp as smtp  # noqa: F401 - the fixture
from tests.mail.test_mail import LIVE, FakeIMAP, raw_mail
from tests.mail.test_mail import imap as imap  # noqa: F401 - the fixture

ANN = "ann@example.org"
VERIFIED = "mx.mail.example; dkim=pass header.d=example.org; dmarc=pass header.from=example.org"
FOOTER_QUOTED = (
    "> -- \n"
    "> This email was written by Ember, an AI agent, on behalf of Stefan, and approved by them before sending.\n"
    '> Reply "stop" and Ember won\'t write to you again.\n'
)
QUOTE_DE = (
    "Am 14.10.2026 um 10:12 schrieb Ember <ember@example.org>:\n> Hallo Ann,\n> darf ich Ihnen den Planer zeigen?\n"
)
QUOTE_EN = "On Tue, Oct 14, 2026 at 10:12 Ember <ember@example.org> wrote:\n> Hi Ann,\n> may I show you our planner?\n"
STOP_DE = "Bitte keine E-Mails mehr."


def from_ann(uid: int, body: str, *, subject: str = "Re: Hello?", html: bool = False) -> EmailMessage:
    """An answer of Ann's to Ember's email, as her mail provider hands it over."""
    msg = EmailMessage()
    msg["From"] = f"Ann <{ANN}>"
    msg["To"] = "ember@mail.example"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 28 Sep 2026 09:00:00 +0200"
    msg["Message-ID"] = f"<stop-{uid}@example.org>"
    msg["In-Reply-To"] = "<m1@example.org>"
    msg["Authentication-Results"] = VERIFIED
    msg.set_content(body, subtype="html" if html else "plain")
    return msg


def sent_to(smtp: type[FakeSMTP]) -> list[Any]:
    return [c.calls[1][3] for c in smtp.instances]


# --- 1. a "stop" below the quote, in an HTML quote, and in an email over 1 MB (4.2) ---


@pytest.mark.parametrize(
    ("body", "words"),
    [
        # the repro's three layouts: above the quote counted before; below it, in German and in English, now
        (f"{STOP_DE}\n\n{QUOTE_DE}> Ember\n", STOP_DE),
        (f"{QUOTE_DE}> Ember\n\n{STOP_DE}\n", STOP_DE),
        (QUOTE_EN + "\nPlease stop emailing me.\n", "Please stop emailing me."),
        # Thunderbird's default: the quote with Ember's footer, then the answer and a signature
        (f"{QUOTE_DE}{FOOTER_QUOTED}\n{STOP_DE}\n\nAnn\n-- \nAnn Beispiel\n", STOP_DE),
        # a blank line between the quote's first line and its lines, and a nested quote
        (QUOTE_EN.replace("wrote:\n", "wrote:\n\n") + ">> earlier\n>\n\nStop\n", "Stop"),
        # a lone "stop" on the first line below the quote, after a greeting
        (QUOTE_DE + "\nHallo,\n\nStopp.\n\nAnn\n", "Stopp."),
        # an answer written into the quote: "No. Stop." after Ember's second question
        (
            QUOTE_EN + "\nYes, in German please.\n\n> And may I send you our newsletter?\n\nNo. Stop.\n",
            "No. Stop.",
        ),
        # below a quote longer than the 2,000 characters read before: only the sender's words count toward them
        (
            f"{QUOTE_EN}> {'Our planner has many pages. ' * 120}\n\nRemove me from your list.\n",
            "Remove me from your list.",
        ),
        # a short stop counts among the first lines below the quote, whatever came before it: here the two lines of a
        # quote's first line as Gmail wraps it, and the answers to Ember's earlier questions
        (
            "On Mon, Sep 28, 2026 at 8:00 AM Ember (AI agent of Stefan) <\nember@mail.example> wrote:\n"
            "> Hi Ann,\n> may I show you our planner?\n\nHi,\nthanks, but no.\nStop.\n",
            "Stop.",
        ),
        (
            QUOTE_EN + "> Q1?\nA1.\n> Q2?\nA2 is longer than four words.\n> Q3?\nA3, in words.\n> More?\nStop.\n",
            "Stop.",
        ),
    ],
    ids=[
        "top",
        "bottom-de",
        "bottom-en",
        "thunderbird",
        "nested",
        "after-greeting",
        "inline",
        "long-quote",
        "gmail-wrapped",
        "after-answers",
    ],
)
def test_a_stop_written_below_or_into_the_quote_counts(body: str, words: str) -> None:
    assert opt_out("Re: Ihr Planer", body) == words


@pytest.mark.parametrize(
    "body",
    [
        # an ordinary answer below the quote: Ember's quoted footer ('Reply "stop" ...') still never counts
        QUOTE_DE + FOOTER_QUOTED + "\nDanke, ich bestelle den Planer.\n\nAnn\n",
        # what the sender quoted of their own stop doesn't count again
        QUOTE_EN + ">> Please stop emailing me.\n\nSorry, I changed my mind: send me the planner.\n",
        # a quote its writer's program didn't mark: what follows its first line may be the quoted email itself
        "Thanks!\n\n" + QUOTE_EN.replace("\n> ", "\n") + 'Reply "stop" and we won\'t write. Unsubscribe here.\n',
        # a signature below the answer ends the sender's words
        QUOTE_EN + "\nThanks, I'll buy it.\n-- \nAnn\nPlease don't email me at work.\n",
        # an email forwarded or quoted without ">" (Outlook) ends them too
        "Great!\n\nFrom: Ember <ember@example.org>\nSent: Friday\n\nPlease stop sending me drafts? Answer below.\n",
    ],
    ids=["ordinary-below", "own-stop-quoted", "unmarked-quote", "signature", "outlook"],
)
def test_quoted_words_still_never_count(body: str) -> None:
    assert opt_out("Re: Ihr Planer", body) is None


THUNDERBIRD_HTML = (
    '<html><head><meta http-equiv="content-type" content="text/html; charset=UTF-8"></head><body>'
    '<div class="moz-cite-prefix">Am 28.09.26 um 08:00 schrieb Ember (AI agent of Stefan):<br></div>'
    '<blockquote type="cite" cite="mid:m1@example.org"><p>Hi Ann,</p><p>not yet, but I\'ll tell you when it is.</p>'
    "<p>-- <br>This email was written by Ember, an AI agent, on behalf of Stefan, and approved by them before sending."
    '<br>Reply "stop" and Ember won\'t write to you again.</p>'
    '<blockquote type="cite">Earlier: <b>keine E-Mails mehr</b> about prices</blockquote></blockquote>'
    "<p>Bitte keine E-Mails mehr.</p><p>Ann<br></p></body></html>"
)


def test_an_html_quote_is_marked_and_a_stop_below_it_counts() -> None:
    text = mail.html_to_text(THUNDERBIRD_HTML)
    assert text.splitlines() == [
        "Am 28.09.26 um 08:00 schrieb Ember (AI agent of Stefan):",
        "",
        "> Hi Ann,",
        "",
        "> not yet, but I'll tell you when it is.",
        "",
        "> --",
        "> This email was written by Ember, an AI agent, on behalf of Stefan, and approved by them before sending.",
        '> Reply "stop" and Ember won\'t write to you again.',
        "",
        ">> Earlier: keine E-Mails mehr about prices",
        "",
        "Bitte keine E-Mails mehr.",
        "",
        "Ann",
    ]
    assert opt_out("Re: Hello?", text) == "Bitte keine E-Mails mehr."
    # Answered above the quote: what is quoted (the footer, a nested stop) still doesn't count
    above = THUNDERBIRD_HTML.replace("<p>Bitte keine E-Mails mehr.</p>", "").replace(
        '<div class="moz-cite-prefix">', '<p>Danke, ich bestelle.</p><div class="moz-cite-prefix">'
    )
    assert opt_out("Re: Hello?", mail.html_to_text(above)) is None
    # Gmail's and Apple Mail's quotes are blockquotes too; text inside one but hidden stays hidden
    gmail = (
        '<div dir="ltr">Please stop emailing me.</div><div class="gmail_quote"><div class="gmail_attr">On Mon, Sep 28,'
        ' 2026 at 8:00 AM Ember wrote:</div><blockquote class="gmail_quote"><div>Hi Ann,</div>'
        '<div style="display:none">HIDDEN</div></blockquote></div>'
    )
    assert mail.html_to_text(gmail).splitlines() == [
        "Please stop emailing me.",
        "On Mon, Sep 28, 2026 at 8:00 AM Ember wrote:",
        "",
        "> Hi Ann,",
    ]


def test_a_stop_below_the_quote_stops_the_approved_email(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    """The repro: the owner approved an email to Ann; her answer below the quote comes before the round sends."""
    agent, request = live_approved(data_dir)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    body = (
        "Am 28.09.26 um 08:00 schrieb Ember (AI agent of Stefan):\n"
        f"> Hi Ann,\n>\n> not yet, but I'll tell you when it is.\n>\n{FOOTER_QUOTED}\n{STOP_DE}\n\nAnn\n"
    )
    imap.mails[3] = from_ann(3, body).as_bytes()
    assert agent.execute_approved() == [(request, "failed")]  # 0.39.2: sent
    assert sent_to(smtp) == []
    assert rows(agent, "SELECT address, reason FROM email_suppressions") == [
        {"address": ANN, "reason": f'wrote "{STOP_DE}"'}
    ]
    assert approval(agent, request)["result_note"] == f"Not sent: {executor.SUPPRESSED}"
    with agent.db.connection() as conn:
        assert mailstore.inquiries(conn, agent.scope()) == []  # her stop no longer waits to be answered


def test_an_html_answer_below_the_quote_stops_the_approved_email(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    agent, request = live_approved(data_dir)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    imap.mails[3] = from_ann(3, THUNDERBIRD_HTML, html=True).as_bytes()
    assert agent.execute_approved() == [(request, "failed")]
    assert sent_to(smtp) == [] and rows(agent, "SELECT address FROM email_suppressions") == [{"address": ANN}]
    stored = rows(agent, "SELECT body FROM emails WHERE uid = 3")[0]["body"]
    assert '> Reply "stop" and Ember won\'t write to you again.' in stored  # the agent sees the quote marked too


def with_photo(text: str, *, photo_first: bool = False) -> bytes:
    """An email of Ann's sent from a phone with a photo (1.2 MB: over MAX_MESSAGE_BYTES), below its text or above."""
    msg = from_ann(4, "")
    msg.clear_content()
    msg.make_mixed()
    picture, words = EmailMessage(), EmailMessage()
    picture.set_content(b"\xff\xd8\xff\xe0" + bytes(900_000), maintype="image", subtype="jpeg", filename="IMG_2041.jpg")
    words.set_content(text)
    for part in (picture, words) if photo_first else (words, picture):
        msg.attach(part)
    return msg.as_bytes()


def test_a_stop_sent_with_a_photo_is_read_from_the_start_of_the_email(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    agent, request = live_approved(data_dir)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    imap.mails[3] = with_photo("Please stop emailing me. This is what your last mail looked like on my phone.\n\nAnn\n")
    assert len(imap.mails[3]) > mail.MAX_MESSAGE_BYTES
    assert agent.execute_approved() == [(request, "failed")]  # 0.39.2: only its headers stored, and sent
    assert sent_to(smtp) == [] and rows(agent, "SELECT address FROM email_suppressions") == [{"address": ANN}]
    fetched = [c[2:] for c in imap.instances[-1].calls if c[:2] == ("uid", "FETCH")]
    assert ("3", "(BODY.PEEK[])") not in fetched  # never the whole email: its start, and nothing marked read
    assert ("3", f"(BODY.PEEK[TEXT]<0.{mail.START_BYTES}>)") in fetched and mail.START_BYTES == 256 * 1024
    stored = rows(agent, "SELECT body, body_cut, attachments FROM emails WHERE uid = 3")[0]
    assert stored["body"] == (
        "Please stop emailing me. This is what your last mail looked like on my phone.\n\nAnn\n\n"
        "[This email is larger than 1 MB (1.2 MB): Ember read the text at its start, not its attachments.]"
    )
    assert stored["body_cut"] == 0 and stored["attachments"] == "[]"


def test_a_large_email_without_text_at_its_start_keeps_its_headers(
    data_dir: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    box = mail.LiveMailbox(LIVE)
    imap.mails = {1: with_photo("Stop", photo_first=True), 2: with_photo("")}  # its text past the start, or none
    first, second = box.fetch_new(0).mails
    assert first.body == "[This email is larger than 1 MB (1.2 MB), so only its headers were stored.]"
    assert second.body == first.body and (first.subject, first.from_addr) == ("Re: Hello?", ANN)

    class Refusing(FakeIMAP):
        """A server that can't hand over part of a body (BAD), or whose connection breaks while it would."""

        answer: type[imaplib.IMAP4.error] = imaplib.IMAP4.error

        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            if command == "FETCH" and args[1].startswith("(BODY.PEEK[TEXT]"):
                raise self.answer("BAD partial fetch")
            return super().uid(command, *args)

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", Refusing)
    imap.mails = {1: with_photo("Please stop emailing me.")}
    assert "only its headers were stored" in box.fetch_new(0).mails[0].body  # as before 0.39.5
    Refusing.answer = imaplib.IMAP4.abort
    with pytest.raises(mail.MailError, match="BAD partial fetch"):
        box.fetch_new(0)  # a broken connection fails the read: the email is read again next time, not cut


# --- 2. one email no longer stops the mailbox, nor any executor (4.3) ---

HOSTILE = (
    b"From: M\xc3\xbcller <mueller@example.net>\r\nTo: ember@mail.example\r\nSubject: Frage\r\n"
    b"Message-ID: <h1@example.net>\r\nDate: Mon, 28 Sep 2026 09:00:00 +0200\r\n\r\nHallo!\r\n"
)
STOP = raw_mail(4, sender="Bob <bob@example.org>", subject="Re: offer", body="Stop")


def test_raw_8bit_headers_are_read_as_utf8_and_what_isn_t_is_replaced() -> None:
    assert mail.one_line("M\udcc3\udcbcller", 200) == "Müller"  # UTF-8, as RFC 6532 allows
    assert mail.one_line("M\udcfcller \ud800!", 200) == "M\ufffdller \ufffd!"  # Latin-1, and a lone surrogate
    assert mail.clean_text("Gr\udcc3\udcbc\udcc3\udc9fe\r\nM\udcfc") == "Grüße\nM\ufffd"
    assert mail.one_line("Zero\udce2\udc80\udc8bwidth", 200) == "Zerowidth"  # what it decodes to is cleaned too
    parsed = mail.parse_message(HOSTILE, 3)
    assert (parsed.from_name, parsed.from_addr, parsed.subject) == ("Müller", "mueller@example.net", "Frage")
    raw = HOSTILE.replace(b"To: ember@mail.example", b"To: J\xf6rg <j\xc3\xb6rg@example.net>")
    for field in ("from_addr", "from_name", "to_addr", "subject", "body"):
        str(getattr(mail.parse_message(raw, 3), field)).encode("utf-8")  # storable: no surrogate is left


def test_one_8bit_sender_no_longer_stops_the_mailbox_or_the_executors(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    """The repro: an approved email to Ann waits; a sender's name in raw bytes comes, and after it Bob's stop."""
    agent, request = live_approved(data_dir)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    imap.mails[3], imap.mails[4] = HOSTILE, STOP
    called = []
    publish = agent.publisher.run
    agent.publisher.run = lambda *a, **k: called.append("etsy") or publish(*a, **k)  # type: ignore[method-assign]
    assert agent.execute_approved() == [(request, "sent")]  # 0.39.2: UnicodeEncodeError, every round
    assert sent_to(smtp) == [[ANN]] and called == ["etsy"]
    assert agent.db.get_meta(mailstore.meta_key("live", "last_uid")) == "4"
    stored = rows(agent, "SELECT uid, from_addr, from_name FROM emails WHERE direction = 'in' ORDER BY uid")
    assert stored[2:] == [
        {"uid": 3, "from_addr": "mueller@example.net", "from_name": "Müller"},
        {"uid": 4, "from_addr": "bob@example.org", "from_name": "Bob"},
    ]
    assert rows(agent, "SELECT address FROM email_suppressions") == [{"address": "bob@example.org"}]
    assert mailstore.last_fetch(agent.db, "live")[1] is None


def test_a_stop_fetched_with_the_8bit_mail_is_kept(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    agent, _ = live_approved(data_dir)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    imap.mails[3], imap.mails[4] = STOP.replace(b"<m4@", b"<m3@"), HOSTILE  # the stop first, in the same fetch
    agent.check_events()  # the read between cycles
    assert [r["uid"] for r in rows(agent, "SELECT uid FROM emails WHERE direction = 'in' ORDER BY uid")] == [1, 2, 3, 4]
    assert rows(agent, "SELECT address FROM email_suppressions") == [{"address": "bob@example.org"}]


class Handing:
    """A mailbox that hands over these emails as they are: as a parser that left a surrogate in them would."""

    simulated = True
    address = "ember@mail.example"

    def __init__(self, mails: list[mail.IncomingMail]) -> None:
        self.mails = mails

    def fetch_new(
        self, after_uid: int, limit: int = mail.MAX_FETCH, uidvalidity: int | None = None, deadline: Any = None
    ) -> mail.FetchResult:
        new = [m for m in self.mails if m.uid > after_uid]
        return mail.FetchResult(new, max([after_uid, *(m.uid for m in new)]), 7)

    def send(self, message: EmailMessage, to: str) -> mail.SendResult:
        raise AssertionError("never sends")


def incoming(uid: int, **fields: Any) -> mail.IncomingMail:
    return replace(mail.parse_message(raw_mail(uid, sender=f"Person {uid} <p{uid}@example.org>"), uid), **fields)


def test_an_email_the_database_refuses_is_stored_with_its_headers_only(data_dir: Path, imap: type[FakeIMAP]) -> None:
    agent, _ = live_approved(data_dir)
    refused = incoming(4, from_name="M\udcc3ller", subject="Unsubscribe", body="Text \udcff")
    box = Handing([incoming(3, body="Stop"), refused, incoming(5)])
    fetched = mailstore.fetch(agent.db, agent.clock, agent.scope(), box)
    assert fetched.error is None and len(fetched.stored) == 3  # 0.39.2: the three rolled back, and the read raised
    kept = rows(agent, "SELECT uid, from_name, subject, body, body_cut FROM emails WHERE uid > 2 ORDER BY uid")
    assert [r["uid"] for r in kept] == [3, 4, 5]
    assert kept[1] == {
        "uid": 4,
        "from_name": "M?ller",
        "subject": "Unsubscribe",
        "body": mail.UNSTORABLE,
        "body_cut": 1,
    }
    assert rows(agent, "SELECT address FROM email_suppressions ORDER BY address") == [
        {"address": "p3@example.org"},
        {"address": "p4@example.org"},  # the stop in its subject still counts
    ]
    warning = rows(agent, "SELECT level, message FROM events WHERE message LIKE '%number 4%'")
    assert warning == [
        {
            "level": "warning",
            "message": "Ember couldn't store email number 4 as it was, so only its headers were stored: read it in the"
            " mailbox's webmail",
        }
    ]


def test_a_read_that_fails_while_storing_is_shown_and_the_other_executors_run(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = live_approved(data_dir)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    imap.mails[3] = HOSTILE
    store = mailstore.store_incoming

    def refusing(conn: Any, scope: Any, mail_: mail.IncomingMail, *args: Any) -> int | None:
        if mail_.uid == 3:  # even with its headers only
            raise UnicodeEncodeError("utf-8", "M\udcc3\udcbcller", 1, 3, "surrogates not allowed")
        return store(conn, scope, mail_, *args)

    monkeypatch.setattr(mailstore, "store_incoming", refusing)
    ran = []
    publish = agent.publisher.run
    agent.publisher.run = lambda *a, **k: ran.append("etsy") or publish(*a, **k)  # type: ignore[method-assign]
    assert agent.execute_approved() == [(request, executor.WAITING_MAIL)]  # 0.39.2: raised, and nothing else ran
    assert ran == ["etsy"] and smtp.instances == []
    error = "UnicodeEncodeError: 'utf-8' codec can't encode characters in position 1-2: surrogates not allowed"
    assert mailstore.last_fetch(agent.db, "live")[1] == error  # 0.39.2: none, and "Last fetch" only stopped moving
    shown = agent.integrations()["email"]
    assert shown["status"] == "error" and shown["last_error"] == error
    card = dashboard_row(agent, request)["execution"]  # 0.39.2: "waiting", and nothing said why
    assert card["status"] == "waiting" and card["result"].startswith(f"Ember's mailbox can't be read ({error})")
    warned = [r["message"] for r in rows(agent, "SELECT message FROM events WHERE level = 'warning'")]
    assert f"Checking Ember's mailbox failed: {error}" in warned
    assert rows(agent, "SELECT COUNT(*) AS n FROM emails WHERE uid = 3")[0]["n"] == 0
    monkeypatch.setattr(mailstore, "store_incoming", store)  # it can be stored again: the next read takes it
    agent.clock.advance(minutes=mailstore.wait_minutes(agent.db, agent.mode, agenda.MAIL_MINUTES))  # type: ignore[attr-defined]
    assert agent.execute_approved() == [(request, "sent")] and sent_to(smtp) == [[ANN]]
    assert agent.integrations()["email"]["last_error"] is None


def test_each_executor_runs_on_its_own(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    agent, _ = live_approved(data_dir)
    ran: list[str] = []

    def runs(name: str, outcome: list[tuple[int, str]] | None = None) -> Any:
        def run(*_: Any, **__: Any) -> list[tuple[int, str]]:
            ran.append(name)
            if outcome is None:
                raise RuntimeError(f"{name} broke")
            return outcome

        return run

    agent.executor.run = runs("emails")  # type: ignore[method-assign]
    agent.publisher.run = runs("etsy", [(7, "created")])  # type: ignore[method-assign]
    agent.pins.run = runs("pins")  # type: ignore[method-assign]
    agent._bluesky_run = runs("bluesky", [(8, "posted")])  # type: ignore[method-assign]
    agent._pod_run = runs("printify", [])  # type: ignore[method-assign]
    agent._blog_run = runs("blog", [(9, "uploaded")])  # type: ignore[method-assign]
    assert agent.execute_approved() == [(7, "created"), (8, "posted"), (9, "uploaded")]
    assert ran == ["emails", "etsy", "pins", "bluesky", "printify", "blog"]
    # While the agent is paused only the owner's undos run: each on its own too (0.42.3: the website's as well)
    ran.clear()
    agent.executor_blocked = lambda: "paused"  # type: ignore[method-assign]
    agent.undo_blocked = lambda: None  # type: ignore[method-assign]
    agent.publisher.run = runs("etsy")  # type: ignore[method-assign]
    agent.pins.run = runs("pins", [(10, "undone")])  # type: ignore[method-assign]
    agent._blog_run = runs("blog", [(11, "undone")])  # type: ignore[method-assign]
    assert agent.execute_approved() == [(10, "undone"), (8, "posted"), (11, "undone")]
    assert ran == ["etsy", "pins", "printify", "bluesky", "blog"]


# --- 3. CSS comments are stripped in linear time, under the cap (4.12) ---


def test_an_unclosed_css_comment_takes_linear_time() -> None:
    """The repro: twice the text took four times as long (0.03 s, 0.13 s, 0.54 s, 2.3 s); a 0.99 MB email 1,439 s."""
    took = []
    for n in (5_000, 10_000, 20_000, 40_000):
        parser = mail._HtmlText()
        started = time.perf_counter()
        parser._style("/* " * (n // 3))
        took.append(time.perf_counter() - started)
        assert parser.css == "" and parser.css_comment  # an unclosed comment ends the sheet
    assert sum(took) < 0.5
    sheet = "/* " * 330_000  # 990,000 characters: the reviewer's email, through the whole parser
    raw = from_ann(1, f"<html><head><style>{sheet}</style></head><body><p>Shown.</p></body></html>", html=True)
    started = time.perf_counter()
    parsed = mail.parse_message(raw.as_bytes(), 1)
    assert time.perf_counter() - started < 5 and parsed.body == "Shown."
    for css in ("/**/ " * 200_000, "/*" + "*" * 990_000, "/" * 990_000):  # closed comments, stars, slashes
        started = time.perf_counter()
        assert mail.html_to_text(f"<style>{css}</style><p>Shown.</p>") == "Shown."
        assert time.perf_counter() - started < 5


def test_comments_don_t_count_toward_the_cap_and_an_unclosed_one_ends_the_sheet() -> None:
    # A rule after a comment longer than the cap still hides (comments don't count), and after an unclosed one it
    # doesn't apply (the comment runs to the end of the sheet, as in a browser)
    padded = "<style>/*" + "x" * 150_000 + "*/.h{display:none}</style><p class=h>HIDDEN</p><p>Shown.</p>"
    assert mail.html_to_text(padded) == "Shown."
    unclosed = "<style>.g{display:none} /* .h{display:none}</style><p class=h>Shown h.</p><p class=g>HIDDEN</p>"
    assert mail.html_to_text(unclosed) == "Shown h."
    assert mail.html_to_text("<style>/*/ .h{display:none} */ .g{display:none}</style><p class=h>A</p>") == "A"
    # A comment's "/*" or "*/" split between two pieces of the sheet, as the parser may hand them over
    parser = mail._HtmlText()
    for piece in (".a{color:red} /", "* a comment *", "/ .h{display:none} /", "/ .b{color:blue} /", "* open *", "x"):
        parser._style(piece)
    assert parser.css == ".a{color:red}  .h{display:none} // .b{color:blue} "
    parser.handle_endtag("style")
    assert sorted(parser.rules) == [".a", ".b", ".h"] and (parser.css, parser.css_comment) == ("", False)
    # The sheet is kept to _CSS_CHARS without its comments, and nothing past the cap is read
    full = mail._HtmlText()
    full._style("a{color:red}" + "/* c */ b{color:red} " * 10_000)
    assert len(full.css) == 100_000 and "/*" not in full.css
    full._style(".h{display:none}")
    assert len(full.css) == 100_000 and ".h" not in full.css


# --- From 0.39.7 (formerly test_fixes_0397.py) ---


# --- 1. a stop below a long thread (4.2) ---

# Ember's longest email (2,500 characters) and its footer, as the repro writes it
EMBER = ("Hallo Ann,\n\nich habe Ihren Shop gesehen und eine Idee fuer Ihre Produktbeschreibungen. " * 25)[:2_500]
EMBER += '\n\n-- \nEmber, Etsy shop\nReply "stop" and I won\'t write again.\n'


ATTRIBUTION = "Am 14.10.26 um 10:00 schrieb Ember:"


def quoted(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def thread(rounds: int) -> str:
    """Ember's emails and Ann's answers, each round quoting the last one whole: three rounds are the repro's thread."""
    text = quoted(EMBER)
    for _ in range(rounds):
        text = quoted(f"Am 13.10.26 schrieb Ann:\n{text}\n\nDanke, ich ueberlege es mir.\n\n{EMBER}")
    return text


def below(rounds: int, answer: str = STOP_DE) -> str:
    """Ann's answer below the thread, as Thunderbird writes it."""
    return f"{ATTRIBUTION}\n{thread(rounds)}\n\n{answer}\n\nAnn\n"


def below_html(rounds: int, answer: str = STOP_DE) -> str:
    """The same in HTML, as Thunderbird writes it: each round's quote a blockquote in the next."""
    ember = EMBER.replace("\n", "<br>")
    html = f'<blockquote type="cite">{ember}</blockquote>'
    for _ in range(rounds):
        html = (
            '<blockquote type="cite"><div class="moz-cite-prefix">Am 13.10.26 schrieb Ann:<br></div>'
            f"{html}<p>Danke, ich ueberlege es mir.</p><p>{ember}</p></blockquote>"
        )
    return (
        f'<html><body><div class="moz-cite-prefix">{ATTRIBUTION}<br></div>{html}<p>{answer}</p><p>Ann<br></p>'
        "</body></html>"
    )


def test_the_threads_are_the_repro_s_and_one_longer_than_0_39_5_parsed() -> None:
    assert len(thread(3)) == 10_259  # three rounds deep: the repro's
    # twelve rounds: an HTML email's text past what 0.39.5 parsed (2 * BODY_CHARS, and the rest of that piece of HTML)
    assert len(below(12)) > 40_000 and len(mail.html_to_text(below_html(12))) > 2 * mail.BODY_CHARS + mail._HTML_FEED


@pytest.mark.parametrize("rounds", [3, 12], ids=["three-rounds", "twelve-rounds"])
@pytest.mark.parametrize("html", [False, True], ids=["plain", "html"])
def test_a_stop_below_a_long_thread_counts(rounds: int, html: bool) -> None:
    """The repro: 0.39.5 caught a stop below one quote, and missed it below a thread of more than 8,000 characters (and
    an HTML email's below more than 16,000, which wasn't parsed)."""
    text = mail.html_to_text(below_html(rounds)) if html else below(rounds)
    assert opt_out("Re: Idee", text) == STOP_DE
    # what Ann quotes still never counts: Ember's footer ('Reply "stop"'), in every round of the thread
    answer = "Danke, ich bestelle den Planer."
    text = mail.html_to_text(below_html(rounds, answer)) if html else below(rounds, answer)
    assert 'Reply "stop"' in text and opt_out("Re: Idee", text) is None


def test_a_long_email_loses_what_it_quotes_first_the_oldest_first() -> None:
    stored = mail.parse_message(from_ann(3, below(3)).as_bytes(), 3)
    assert stored.body_cut and len(stored.body) <= mail.BODY_CHARS
    lines = stored.body.splitlines()
    assert lines[0] == ATTRIBUTION and lines[-3:] == [STOP_DE, "", "Ann"]  # 0.39.5: its first 8,000 characters
    # the thread's oldest email (quoted four deep) is left out as one line; the three rounds after it are kept whole
    assert [line for line in lines if line.endswith(mail.LEFT_OUT)] == [">>>> [...]"]
    assert sum(line.lstrip("> ").startswith("Am 13.10.26 schrieb Ann:") for line in lines) == 3
    assert sum(line.endswith("Danke, ich ueberlege es mir.") for line in lines) == 3
    # an HTML thread too, read past its quote: the deeper rounds go first
    html = mail.parse_message(from_ann(3, below_html(12), html=True).as_bytes(), 3)
    assert html.body_cut and len(html.body) <= mail.BODY_CHARS and html.body.endswith(f"{STOP_DE}\n\nAnn")
    left_out = [line for line in html.body.splitlines() if line.endswith(mail.LEFT_OUT)]
    assert len(left_out) == 1 and left_out[0].startswith(">>")
    # answered above the thread, the answer comes first as before
    above = mail.parse_message(from_ann(3, f"{STOP_DE}\n\n{ATTRIBUTION}\n{thread(3)}\n").as_bytes(), 3)
    assert above.body.startswith(f"{STOP_DE}\n\n{ATTRIBUTION}\n> ") and len(above.body) <= mail.BODY_CHARS
    # a long text that quotes nothing keeps its first 8,000 characters, as before
    long = mail.parse_message(from_ann(3, "Mein Vorschlag.\n" + "x" * 9_000).as_bytes(), 3)
    assert long.body_cut and long.body == ("Mein Vorschlag.\n" + "x" * 9_000)[: mail.BODY_CHARS]


def test_the_deepest_quote_goes_first_then_the_next_then_the_end() -> None:
    text = (
        "Below, as asked.\n\n> Ember's last email\n>> Ann's answer before it\n>>> Ember's first email, the oldest\n\n"
        ">>> its second paragraph\n> More of Ember's last\n\nStop.\nAnn"
    )
    assert mail.fit_text(text, len(text)) == (text, False)
    oldest = ">>> Ember's first email, the oldest\n\n>>> its second paragraph"  # its paragraphs go as one
    assert mail.fit_text(text, len(text) - 1) == (text.replace(oldest, ">>> [...]"), True)
    deeper = ">> Ann's answer before it\n" + oldest
    assert mail.fit_text(text, len(text) - 60) == (text.replace(deeper, ">> [...]"), True)
    every = "Below, as asked.\n\n> [...]\n\nStop.\nAnn"
    assert mail.fit_text(text, len(every)) == (every, True)
    assert mail.fit_text(text, 20) == (every[:20], True)  # only then is its end cut
    assert mail.fit_text("no quote " * 3, 10) == ("no quote n", True)


def test_an_html_email_is_read_past_its_quotes_up_to_a_cap() -> None:
    # The text outside quotes still ends the parse after 2 * BODY_CHARS, as before
    long = mail.html_to_text("<p>" + "Mein Vorschlag. " * 6_000 + "</p><p>Ende.</p>")
    assert len(long) <= 2 * mail.BODY_CHARS + mail._HTML_FEED and "Ende." not in long
    # A quote nested deep repeats its ">" on each line, so its text is many times its HTML (read whole, this 1 MB
    # email would be 52 MB of text): it is read up to _QUOTED_CHARS
    deep = "<blockquote>" * 255 + "a<br>" * 200_000 + "</blockquote>" * 255 + "<p>Stop.</p>"
    started = time.perf_counter()
    text = mail.html_to_text(deep)
    assert time.perf_counter() - started < 5
    assert mail._QUOTED_CHARS < len(text) < 2 * mail._QUOTED_CHARS and "Stop." not in text


@pytest.mark.parametrize(
    "made",
    [
        lambda: from_ann(3, below(3)).as_bytes(),
        lambda: from_ann(3, below_html(12), html=True).as_bytes(),
        lambda: with_photo(below(3)),
    ],
    ids=["plain", "html", "with-a-photo"],
)
def test_a_stop_below_a_long_thread_stops_the_approved_email(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP], made: Callable[[], bytes]
) -> None:
    """The repro, end to end: the owner approved an email to Ann; her answer below the thread comes before the round
    sends. An email over 1 MB (a photo) keeps its text the same way."""
    agent, request = live_approved(data_dir)
    agent.clock.advance(minutes=10)  # type: ignore[attr-defined]
    imap.mails[3] = made()
    assert agent.execute_approved() == [(request, "failed")]  # 0.39.5: sent
    assert sent_to(smtp) == []
    assert rows(agent, "SELECT address, reason FROM email_suppressions") == [
        {"address": ANN, "reason": f'wrote "{STOP_DE}"'}
    ]
    assert approval(agent, request)["result_note"] == f"Not sent: {executor.SUPPRESSED}"
    with agent.db.connection() as conn:
        assert mailstore.inquiries(conn, agent.scope()) == []  # her stop doesn't wait to be answered
    stored = rows(agent, "SELECT body, body_cut FROM emails WHERE uid = 3")[0]
    assert stored["body_cut"] == 1 and len(stored["body"]) <= mail.BODY_CHARS
    assert f"{STOP_DE}\n\nAnn" in stored["body"] and mail.LEFT_OUT in stored["body"]  # the agent reads it too
