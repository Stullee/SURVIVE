"""0.39.7: two gaps the fixes of 0.39.3 and 0.39.5 left (analysis-0.39.1, 9.2; its reproductions are
repro/mail/stop_after_long_thread.py and repro/tools/held_line_venture_cycle.py).

1. A stop below a long thread was missed (4.2). Ember kept the first 8,000 characters of an email's text
   (mail.BODY_CHARS), and the opt-out check read as many (optout.READ_CHARS): a "Bitte keine E-Mails mehr." written
   below a thread quoted three rounds deep, 10,259 characters in, was seen by neither the check nor the agent, and the
   approved email went out to its writer (§ 7 UWG). An HTML email was parsed only until 16,000 characters of its text,
   quotes included, so a reply below a longer quote wasn't even parsed. Now a longer text loses what it quotes first,
   the deepest quote (the oldest emails of the thread) first, the check skips what is quoted however long it is, and
   only the text outside quotes counts toward the parse.
2. A venture cycle could file a request about a line the owner holds (4.4). tools._line returned early in a cycle that
   isn't one line's, before it read the owner's hold: an ordinary cycle was refused, a venture cycle filed it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.integrations import executor, mail, mailstore  # noqa: E402
from app.integrations.optout import opt_out  # noqa: E402
from tests.channels.test_etsy import call  # noqa: E402
from tests.cycle.test_agent import rows  # noqa: E402
from tests.mail.test_executor import FakeSMTP, approval, live_approved  # noqa: E402
from tests.mail.test_executor import smtp as smtp  # noqa: E402, F401 - the fixture
from tests.mail.test_mail import FakeIMAP  # noqa: E402
from tests.mail.test_mail import imap as imap  # noqa: E402, F401 - the fixture
from tests.mail.test_mail_parsing import ANN, STOP_DE, from_ann, sent_to, with_photo  # noqa: E402
from tests.plan.test_cycle_line import lined, working  # noqa: E402
from tests.plan.test_owner_holds import AGAIN, LATER, hold  # noqa: E402
from tests.plan.test_plan_tree import BOOK, TRACKER, keep, project  # noqa: E402

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


# --- 2. a venture cycle takes no line the owner holds (4.4) ---


def test_a_venture_cycle_takes_no_line_the_owner_holds(data_dir: Path) -> None:
    """The repro: an ordinary cycle was refused a request about a line the owner holds, and a venture cycle filed it."""
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    tracker = project(agent, *TRACKER)
    keep(agent)
    hold(agent, book)
    ctx = working(agent, one_line=False)  # a venture cycle: it works on no one line
    ctx.venture = True
    refused = call(ctx, "request_approval", {**AGAIN, "project_id": book})
    assert not refused.ok and f"your owner holds product line #{book} ({LATER}): no request for it" in refused.text
    assert not rows(agent, "SELECT id FROM approvals")  # 0.39.5: request #1, waiting for the owner
    noted = call(ctx, "project_update", {"project_id": book, "note": "Waits for my owner's word"})
    assert noted.ok  # its record is kept, as in any cycle
    reopened = call(ctx, "project_update", {"project_id": book, "status": "active"})
    assert not reopened.ok and f"your owner holds product line #{book}" in reopened.text
    asked = call(ctx, "request_approval", {**AGAIN, "project_id": tracker, "payload": "tracker v2"})
    assert asked.ok and ctx.state.focus_project_id is None  # a line nobody holds: as before, and no line taken
    assert rows(agent, "SELECT project_id FROM approvals") == [{"project_id": tracker}]


def test_a_cycle_on_another_line_hears_that_the_owner_holds_this_one(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK)
    tracker = project(agent, *TRACKER)
    keep(agent)
    hold(agent, book)
    refused = call(working(agent, line=tracker), "request_approval", {**AGAIN, "project_id": book})
    # 0.39.5: "this cycle works on product line #...: no request for project #..., whose work waits for a cycle of
    # its own", which a held line doesn't get
    assert not refused.ok and f"your owner holds product line #{book} ({LATER})" in refused.text
