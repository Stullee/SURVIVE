"""0.45.4: stored emails read again from the server after Ember's way of reading an email's text changed.

Live, Amazon KDP's "Attention needed" emails #64 and #65 were stored as their footer and case number: the hidden-text
filter took their text for white on white. 0.45.2 fixed the reading, but a stored email never changes, and Ember keeps
no copy of one to read again: the agent re-read the stored text, found no word of what was wrong, and asked its owner
to log in to KDP. Now a check of the mailbox reads the recent emails stored with an older mail.TEXT_VERSION again from
the server by their UIDs. What it read is kept beside the stored email (email_rereads), a changed text is the one
email_read shows and is unread until the agent opens it, and a "stop" in it counts as in a new email."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import export  # noqa: E402
from app.agent import context, tools  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import mail, mailstore  # noqa: E402
from tests.agent_helpers import plan, rows, text  # noqa: E402
from tests.etsy_helpers import call, shop_context  # noqa: E402
from tests.mail.test_mail import JOURNAL, LIVE, FakeIMAP, live_agent, raw_mail  # noqa: E402
from tests.mail.test_mail import imap as imap  # noqa: E402, F401 - the fixture
from tests.mail.test_mail_style_context import KDP_NOTICE  # noqa: E402

# What 0.45.1 stored of KDP's notice: its footer and case number
FOOTER = "©2026 Amazon.com, Inc. or its affiliates.\n\nSPC-Case-1\n\nSent by Amazon"


def kdp_notice(uid: int) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Amazon KDP <kdp-noreply@amazon.example>"
    msg["To"] = "ember@mail.example"
    msg["Subject"] = "Attention needed: Please review your title Household Budget Book 2027"
    msg["Date"] = "Thu, 22 Oct 2026 08:00:00 +0000"
    msg["Message-ID"] = f"<kdp-{uid}@amazon.example>"
    msg.set_content(KDP_NOTICE, subtype="html")
    return msg.as_bytes()


def started(data_dir: Path, imap: type[FakeIMAP]) -> Agent:
    """A live agent after one cycle, whose read of an empty mailbox left its UIDVALIDITY (7)."""
    imap.mails = {}
    agent = live_agent(data_dir, [plan(steps=["look around"]), text("Done."), JOURNAL])
    assert agent.run_cycle("schedule").status == "completed"
    return agent


def stored_before(
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    uid: int,
    body: str,
    *,
    days: int = 1,
    validity: int = 7,
    version: int = 0,
) -> int:
    """``raw`` as an earlier Ember stored it (``body`` its text then, read with TEXT_VERSION ``version``) ``days``
    ago, and the mailbox read past it."""
    incoming = replace(mail.parse_message(raw, uid), body=body)
    with monkeypatch.context() as patched, agent.db.transaction() as conn:
        patched.setattr(mailstore, "TEXT_VERSION", version)
        when = to_iso(agent.clock.now() - timedelta(days=days))
        found = mailstore.store_incoming(conn, agent.scope(), incoming, validity, when, "ember@mail.example")
    assert found is not None
    last = max(uid, int(agent.db.get_meta(mailstore.meta_key("live", "last_uid")) or 0))
    agent.db.set_meta(mailstore.meta_key("live", "last_uid"), str(last))
    return found


def fetched(agent: Agent) -> mailstore.Fetched:
    assert agent.mailbox is not None
    return mailstore.fetch(agent.db, agent.clock, agent.scope(), agent.mailbox)


def rereads(agent: Agent) -> dict[int, str]:
    return {r["email_id"]: r["status"] for r in rows(agent, "SELECT email_id, status FROM email_rereads")}


def reading(agent: Agent) -> tools.ToolContext:
    ctx = shop_context(agent)
    ctx.mail = tools.MailAccess("ember@mail.example", 3)
    return ctx


def test_kdp_s_notice_stored_as_its_footer_is_read_again_and_shown_whole(
    data_dir: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = started(data_dir, imap)
    notice = stored_before(agent, monkeypatch, kdp_notice(65), 65, FOOTER)
    ctx = reading(agent)
    assert FOOTER in call(ctx, "email_read", {"email_id": notice}).text  # read as it was stored
    imap.mails = {65: kdp_notice(65)}
    result = fetched(agent)
    assert result.error is None and result.reread == [notice] and rereads(agent) == {notice: "changed"}
    assert ("uid", "FETCH", "65", "(BODY.PEEK[])") in imap.instances[-1].calls  # PEEK: nothing marked read
    [kept] = rows(agent, f"SELECT body, text_version FROM emails WHERE id = {notice}")
    assert kept == {"body": FOOTER, "text_version": 0}  # the stored email never changes
    assert any(f"new text in #{notice}" in e["message"] for e in agent.db.recent_events())
    # Unread again, and MAIL says why
    with agent.db.connection() as conn:
        count, newest = mailstore.unread(conn, agent.scope(), 3)
    assert count == 1 and [(r["id"], r["again"]) for r in newest] == [(notice, 1)]
    view = context.MailView("ember@mail.example", 1, ((notice, "kdp@amazon.example", "Attention"),), (), (notice,))
    snap = context.Snapshot(None, "", "", "", 0, 0, 0, mail=view)  # type: ignore[arg-type]
    shown = f'#{notice} from "kdp@amazon.example" "Attention" (read again from the server: new text)'
    assert shown in context.mail_text(snap)
    read = call(ctx, "email_read", {"email_id": notice})
    assert "Ember read it again from the server" in read.text and f"stored with {len(FOOTER):,} characters" in read.text
    assert "Text on your book cover is too close to the edges." in read.text and "0.375 inches" in read.text
    assert rows(agent, "SELECT read_by_agent_at IS NOT NULL AS seen FROM email_rereads") == [{"seen": 1}]
    with agent.db.connection() as conn:
        assert mailstore.unread(conn, agent.scope(), 3)[0] == 0
    assert fetched(agent).reread == [] and rereads(agent) == {notice: "changed"}  # once per version


def test_only_recent_emails_of_this_mailbox_stored_with_an_older_reading_are_read_again(
    data_dir: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = started(data_dir, imap)
    same = stored_before(agent, monkeypatch, raw_mail(1), 1, "Hi!")
    gone = stored_before(agent, monkeypatch, raw_mail(2), 2, "Hi?")
    old = stored_before(agent, monkeypatch, raw_mail(3), 3, "Hi?", days=mailstore.REREAD_DAYS + 1)
    renumbered = stored_before(agent, monkeypatch, raw_mail(4), 4, "Hi?", validity=6)
    current = stored_before(agent, monkeypatch, raw_mail(5), 5, "Hi?", version=mail.TEXT_VERSION)
    other = stored_before(agent, monkeypatch, raw_mail(6), 6, "Hi?")
    imap.mails = {n: raw_mail(n) for n in (1, 3, 4, 5)} | {6: raw_mail(66)}  # 6 is another email now
    result = fetched(agent)
    assert result.error is None and result.reread == []
    assert rereads(agent) == {same: "same", gone: "gone", other: "gone"}
    asked = {int(c[2]) for c in imap.instances[-1].calls if c[:2] == ("uid", "FETCH")}
    assert asked == {1, 2, 6} and not {old, renumbered, current} & set(rereads(agent))
    with agent.db.connection() as conn:
        assert mailstore.text_again(conn, same) is None and mailstore.text_again(conn, other) is None


def test_a_stop_found_when_an_email_is_read_again_counts(
    data_dir: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = started(data_dir, imap)
    ann = stored_before(agent, monkeypatch, raw_mail(7), 7, "Hi!")
    imap.mails = {7: raw_mail(7, body="Stop.\n\nPlease don't email me again.")}
    result = fetched(agent)
    assert result.reread == [ann] and result.suppressed == ["ann@example.org"]
    with agent.db.connection() as conn:
        assert mailstore.is_suppressed(conn, agent.scope(), "ann@example.org")


def test_the_newest_are_read_again_first_and_a_failed_read_again_fails_nothing(
    data_dir: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = started(data_dir, imap)
    first = stored_before(agent, monkeypatch, raw_mail(8), 8, "Hi?")
    second = stored_before(agent, monkeypatch, raw_mail(9), 9, "Hi?")
    imap.mails = {8: raw_mail(8), 9: raw_mail(9)}
    monkeypatch.setattr(mailstore, "REREAD_BATCH", 1)

    def broken(*_: Any) -> Any:
        raise mail.MailError("reading the mailbox failed: BYE")

    assert agent.mailbox is not None
    with monkeypatch.context() as patched:
        patched.setattr(agent.mailbox, "fetch_again", broken)
        result = fetched(agent)
    assert result.error is None and rereads(agent) == {}  # the read of new mail worked; this is tried again
    assert agent.db.get_meta(mailstore.meta_key("live", "failures")) == "0"
    assert fetched(agent).reread == [second] and rereads(agent) == {second: "changed"}  # the newest first
    assert fetched(agent).reread == [first] and set(rereads(agent)) == {first, second}


def test_the_mailboxes_read_again_by_uid_and_only_while_the_numbers_hold(imap: type[FakeIMAP]) -> None:
    box = mail.LiveMailbox(LIVE)
    found = box.fetch_again([2, 3], 7)
    assert set(found) == {2, 3} and found[3] is None and found[2] is not None and found[2].subject == "Second?"
    [conn] = imap.instances
    assert ("select", "INBOX", True) in conn.calls and conn.calls[-1] == ("logout",)
    assert box.fetch_again([1], 8) == {}  # the server renumbered the mailbox: UID 1 may be another email
    assert box.fetch_again([1], 7, deadline=time.monotonic() - 1) == {}  # out of time
    fake = mail.FakeMailbox(1, lambda: 1)
    again = fake.fetch_again([1, 2], fake.uidvalidity)
    assert again[2] is None and again[1] is not None and again[1].subject.startswith("Is your meal planner")
    assert fake.fetch_again([1], fake.uidvalidity + 1) == {}


def test_an_email_read_again_never_changes_and_its_text_stays_out_of_the_export(
    data_dir: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = started(data_dir, imap)
    notice = stored_before(agent, monkeypatch, kdp_notice(64), 64, FOOTER)
    imap.mails = {64: kdp_notice(64)}
    assert fetched(agent).reread == [notice]
    refused = (
        "UPDATE email_rereads SET body = 'x'",
        "UPDATE email_rereads SET status = 'same'",
        "DELETE FROM email_rereads",
        f"UPDATE emails SET text_version = 1 WHERE id = {notice}",
    )
    for sql in refused:
        with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
            conn.execute(sql)
    with agent.db.transaction() as conn:  # the agent reads it once
        conn.execute("UPDATE email_rereads SET read_by_agent_at = 'x'")
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
        conn.execute("UPDATE email_rereads SET read_by_agent_at = 'y'")
    [row] = rows(agent, "SELECT * FROM email_rereads")
    shared = export._shared("email_rereads", row, lambda t: t, {})
    assert shared["body"] == f"[{len(row['body']):,} characters left out]" and "cover" not in str(shared)
