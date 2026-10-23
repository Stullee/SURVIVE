"""0.45.1: a marketing cycle reads an email (Ember's upgrade request: an email_read for KDP's notices).

Live, a cycle the owner asked to read KDP's "Attention needed" emails #64 and #65 about live books #140 and #141 saw
their subjects in MAIL and had no email_read: the mail tools were an ordinary cycle's, and the cycle was a marketing
one. It could only tell its owner the emails existed, and asked for the tool. Now email_read, and mark_opt_out with it
(a read email lets an approved one to its sender go out), are a marketing cycle's too. Answering, closing an inquiry
and the inbox's list stay an ordinary cycle's, and a venture cycle has none of them. demand_note, which serves a line's
first listing (a demand step takes an ordinary cycle), leaves the marketing cycle to make room in its prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import prompts, tools  # noqa: E402
from app.agent.fake_llm import request_kind  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import mail, mailstore  # noqa: E402
from tests.agent_helpers import make_agent, plan, rows, text  # noqa: E402
from tests.channels.test_bluesky import posted  # noqa: E402
from tests.etsy_helpers import call, market_next, shop_context  # noqa: E402
from tests.mail.test_mail import JOURNAL  # noqa: E402

EVERY = {"mail": True, "workshop": True, "etsy": True, "venture": False, "library": True, "pinterest": True}
EVERY |= {"bluesky": True, "blog": True, "printify": True, "site": True, "kdp": True}
NOTICE = mail.IncomingMail(
    uid=64,
    message_id="<kdp-64@kdp.example>",
    in_reply_to=None,
    references=None,
    from_addr="no-reply@kdp.example",
    from_name="KDP",
    to_addr="ember@example.invalid",
    subject="Attention needed: your paperback Wochenplaner 2027",
    sent_at=None,
    body="Hello,\n\nthe title on your cover doesn't match the title you entered. Update your book within 5 days.",
    body_cut=False,
    machine=True,
)
ANN = mail.IncomingMail(**{**NOTICE.__dict__, "uid": 65, "message_id": "<ann-65@example.org>", "machine": False})
ANN.__dict__.update(from_addr="ann@example.org", from_name="Ann", subject="Planner?", authenticated=True)


def stored(agent: Any, incoming: mail.IncomingMail) -> int:
    with agent.db.transaction() as conn:
        found = mailstore.store_incoming(conn, agent.scope(), incoming, 1, to_iso(agent.clock.now()))
    assert found is not None
    return found


def marketing_context(agent: Any) -> tools.ToolContext:
    ctx = shop_context(agent)
    ctx.mail = tools.MailAccess("ember@example.invalid", 3)
    ctx.marketing = True
    return ctx


def test_a_marketing_cycle_reads_an_email_and_a_venture_cycle_doesn_t() -> None:
    assert {"email_read", "mark_opt_out"} == tools.MAIL_READS and tools.MAIL_READS < tools.MAIL_TOOLS
    assert not tools.MAIL_READS & tools.BUILDING_TOOLS and tools.MAIL_READS <= tools.ORDINARY_TOOLS
    for name in tools.MAIL_TOOLS:
        assert tools.offered(name, **EVERY) and tools.offered(name, marketing_apart=True, **EVERY), name  # ordinary
        assert tools.offered(name, marketing=True, **EVERY) == (name in tools.MAIL_READS), name
        assert not tools.offered(name, **{**EVERY, "venture": True}), name  # a venture cycle decides a venture
        assert not tools.offered(name, marketing=True, **{**EVERY, "mail": False}), name  # nor without a mailbox
    assert tools.offered("demand_note", **EVERY) and not tools.offered("demand_note", marketing=True, **EVERY)
    names = {d["name"] for d in tools.definitions(marketing=True, **EVERY)}
    assert names & tools.MAIL_TOOLS == tools.MAIL_READS and "demand_note" not in names
    assert "files, the shop or your mail\n  (email_read) is your next ordinary cycle's" in prompts.VENTURE_RULES
    guide = tools.guide_text("email")
    assert "Notices from Amazon KDP, Etsy or Printify" in guide and len(guide) <= tools.MAX_RESULT_CHARS


def test_a_marketing_cycle_opens_kdp_s_notice_and_leaves_answering_to_an_ordinary_one(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=["look around"]), text("Done."), JOURNAL])
    assert agent.run_cycle("schedule").status == "completed"
    notice, ann = stored(agent, NOTICE), stored(agent, ANN)
    ctx = marketing_context(agent)
    read = call(ctx, "email_read", {"email_id": notice})
    assert read.ok and read.text.startswith(f"Email #{notice}, received ")
    assert "Not a verified person's (an unverified sender, a list or a machine): no obligation" in read.text
    assert f"Subject: {NOTICE.subject}" in read.text and "Update your book within 5 days." in read.text
    assert read.text.endswith(f"To answer it, use propose_email with reply_to_email_id {notice} in an ordinary cycle.")
    unread = rows(agent, f"SELECT read_by_agent_at IS NULL AS unread FROM emails WHERE id = {notice}")
    assert unread == [{"unread": 0}]
    with agent.db.connection() as conn:
        assert mailstore.is_inquiry(conn, agent.scope(), ann)  # Ann's question waits for an ordinary cycle's answer
    assert call(ctx, "email_read", {"email_id": ann}).ok
    with agent.db.connection() as conn:
        assert mailstore.is_inquiry(conn, agent.scope(), ann)  # read, not answered
    stop = call(ctx, "mark_opt_out", {"email_id": ann, "reason": "She asks to hear no more."})
    assert stop.ok and stop.text == f"The sender of email #{ann} is never emailed again."
    reply = {"reply_to_email_id": notice, "subject": "Re: Attention", "body": "Thanks.", "reason": "It asked."}
    refusals = {
        "propose_email": reply,
        "inquiry_done": {"email_id": ann, "reason": "Spam."},
        "email_inbox": {},
        "demand_note": {"project_id": 1, "keywords": "wochenplaner 2027"},
    }
    for name, args in refusals.items():
        refused = call(ctx, name, args)
        assert not refused.ok and refused.text.startswith(f"Error: {name} is not one of your tools in a marketing")
        assert "KDP and answering email belong to ordinary cycles" in refused.text, name
        hint = "; email_read opens an email by its number (MAIL lists the unread ones)"
        assert (hint in refused.text) == (name in tools.MAIL_TOOLS), name
    ctx.marketing = False  # an ordinary cycle: the answer is its own
    assert call(ctx, "email_read", {"email_id": notice}).text.endswith(f"reply_to_email_id {notice}.")


def test_a_marketing_cycle_s_plan_lists_the_notice_and_its_work_can_open_it(data_dir: Path) -> None:
    agent, fake, _ = posted(data_dir)
    notice = stored(agent, NOTICE)
    before = len(fake.sent)
    market_next(agent, "bluesky")
    agent.run_cycle("schedule")
    [cycle] = rows(agent, "SELECT marketing, venture FROM cycles ORDER BY id DESC LIMIT 1")
    assert (cycle["marketing"], cycle["venture"]) == (1, 0)
    sent = list(fake.sent)[before:]
    plan_text = next(r for r in sent if request_kind(r) == "plan")["messages"][0]["content"][0]["text"]
    assert f'\n#{notice} from "{NOTICE.from_addr}" "{NOTICE.subject}"' in plan_text
    work = next(r for r in sent if request_kind(r) == "work")
    offered = {t["name"] for t in work["tools"]}
    assert offered & tools.MAIL_TOOLS == tools.MAIL_READS and "demand_note" not in offered
