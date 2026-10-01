"""0.13.0 (Phase E1): inbound email as a channel. A person's email waits for an answer (OBLIGATIONS) until Ember
answers it or closes it as needing none; a new one wakes Ember; a newsletter, an automatic reply or a machine's
mail never counts. An answer in their thread is its own action class (never a first contact) with its own checks,
the metrics count people's emails and the answers, and an opt-out after an automatic reply takes the unlock back."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import metrics, obligations, policy, tools  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import connectors, mailstore, qa  # noqa: E402
from tests.test_agent import plan, reply, rows, text  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_executor import REPLY  # noqa: E402
from tests.test_mail import JOURNAL, READER, mail_cycle  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import a_milestone  # noqa: E402
from tests.test_ventures import tool_results  # noqa: E402


def waiting(agent: Any) -> list[int]:
    with agent.db.connection() as conn:
        return [int(r["id"]) for r in mailstore.inquiries(conn, agent.scope())]


def inbound(
    agent: Any,
    sender: str,
    bulk: int | None = 0,
    minutes: int = 1,
    subject: str = "A question",
    authenticated: int | None = 1,
) -> int:
    """An email that arrived ``minutes`` from now (its bulk flag as the mailbox read it; None: stored before 0.13.0;
    0.14.0: whether the provider verified its sender, None: stored before 0.14.0)."""
    scope = agent.scope()
    when = to_iso(agent.clock.now() + timedelta(minutes=minutes))
    with agent.db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, from_addr, to_addr,"
            " subject, received_at, body, bulk, authenticated) VALUES (?, ?, ?, 'in', 9, (SELECT COALESCE(MAX(uid),"
            " 100) + 1 FROM emails), ?, ?, 'ember@example.invalid', ?, ?, 'Hello?', ?, ?)",
            (
                scope.mode,
                scope.session,
                scope.life_id,
                f"<{sender}-{minutes}@example.org>",
                sender,
                subject,
                when,
                bulk,
                authenticated,
            ),
        )
    return int(cursor.lastrowid)


def another_cycle(agent: Any, transport: Any, *turns: Any) -> None:
    transport.outcomes.extend([plan(steps=["read my mail"]), *turns, text("Done."), JOURNAL])
    assert agent.run_cycle("schedule").status == "completed"


def test_a_person_s_email_waits_until_ember_answers_or_closes_it(data_dir: Path) -> None:
    agent, transport = mail_cycle(data_dir, calls(("propose_email", REPLY)))
    assert waiting(agent) == []  # the answer waits for the owner: the email is answered
    [request] = rows(agent, "SELECT id FROM approvals")
    assert owner(agent).decide(request["id"], {"decision": "reject"}, "Stefan").status == 200
    assert waiting(agent) == [1]  # turned down: it waits again
    with agent.db.connection() as conn:
        assert obligations.pressing(conn, agent.scope(), agent.clock.today()) == []  # a new one wakes Ember instead
        assert "- An email from a person waits for your answer: #1 (today): answer with propose_email" in (
            obligations.text(conn, agent.scope(), agent.clock.today())
        )
    # What never counts: a newsletter, mail stored before 0.13.0, a machine, someone who asked to stop
    inbound(agent, "news@list.example", bulk=1)
    inbound(agent, "old@example.org", bulk=None)
    inbound(agent, "MAILER-DAEMON@mail.example", authenticated=None)  # 0.13.0 stored it as bulk 0, with no verdict
    stopped = inbound(agent, "gone@example.org")
    with agent.db.transaction() as conn:
        mailstore.suppress(conn, agent.scope(), "gone@example.org", to_iso(agent.clock.now()), "asked", stopped)
    asked = inbound(agent, "ann@example.org")
    assert waiting(agent) == [1, asked]
    another_cycle(
        agent,
        transport,
        calls(
            ("inquiry_done", {"email_id": asked, "reason": "She only said thanks."}),
            ("inquiry_done", {"email_id": asked, "reason": "Again."}),
            ("inquiry_done", {"email_id": stopped, "reason": "x"}),
        ),
    )
    done, again, never = tool_results(agent, "inquiry_done")
    assert done["result"] == f"Email #{asked} needs no answer: closed."
    assert f"email #{asked} doesn't wait for an answer" in again["result"]
    assert f"email #{stopped} doesn't wait for an answer" in never["result"]
    assert rows(agent, "SELECT email_id, reason, by FROM inquiry_closures") == [
        {"email_id": asked, "reason": "She only said thanks.", "by": "agent"}
    ]
    assert waiting(agent) == [1]


def test_a_new_inquiry_wakes_ember_and_one_it_saw_does_not(data_dir: Path) -> None:
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))
    agent.check_events()  # the reader's email came in before the cycle's MAIL section showed it: no news
    assert rows(agent, "SELECT COUNT(*) AS n FROM agenda WHERE kind = 'inquiry'") == [{"n": 0}]
    agent.clock.advance(minutes=20)
    asked = inbound(agent, "ann@example.org", subject="Do you ship to Austria?")
    agent.check_events()
    agent.check_events()  # once
    assert rows(agent, "SELECT kind, key, urgent, text FROM agenda WHERE kind = 'inquiry'") == [
        {
            "kind": "inquiry",
            "key": str(asked),
            "urgent": 1,
            "text": f'Email #{asked} from ann@example.org writes to you: "Do you ship to Austria?"',
        }
    ]
    decision = agent.decide()
    assert (decision.run, decision.trigger) == (True, "event")
    assert decision.reason.startswith(f"woken by an event: Email #{asked} from ann@example.org writes to you")


def test_an_answer_is_its_own_class_with_its_checks(data_dir: Path) -> None:
    answer = {"to": READER, "subject": "Re: Hi", "body": "Yes.", "in_reply_to": "<a@example.org>"}
    assert connectors.class_of("email", answer).name == "email.reply"
    assert not connectors.class_of("email", answer).first_contact
    assert connectors.class_of("email", {**answer, "in_reply_to": None}).name == "email.send"
    assert qa.defects("email.reply", answer) == []
    assert qa.defects("email.reply", {**answer, "subject": "AW: Hi"}) == []
    assert qa.defects("email.reply", {**answer, "subject": "Hi", "body": "word " * (qa.REPLY_WORDS + 1)}) == [
        "its subject doesn't keep the thread (Re: ...)",
        f"{qa.REPLY_WORDS + 1} words, more than {qa.REPLY_WORDS}: an answer is short",
    ]
    agent, _ = mail_cycle(data_dir, calls(("propose_email", {**REPLY, "subject": "Your question"})))
    [made] = tool_results(agent, "propose_email")
    assert "QA (Ember's code): its subject doesn't keep the thread (Re: ...); your owner sees it too." in made["result"]
    [card] = agent.dashboard()["approvals"]
    assert (card["action_class"]["name"], card["first_contact"]) == ("email.reply", False)
    assert card["qa"] == ["its subject doesn't keep the thread (Re: ...)"]
    guide = tools.guide_text("email")
    assert f"at most {qa.REPLY_WORDS} words" in guide and "{" not in guide


def test_the_inquiry_metrics(data_dir: Path) -> None:
    agent, _ = mail_cycle(data_dir, calls(("propose_email", REPLY)))  # request #1, what an email sent belongs to
    since = to_iso(agent.clock.now() + timedelta(seconds=30))  # a milestone set after the reader's email
    first, second = inbound(agent, "ann@example.org"), inbound(agent, "bob@example.org", minutes=2)
    inbound(agent, "news@list.example", bulk=1, minutes=3)
    scope = agent.scope()
    with agent.db.transaction() as conn:  # Ember's answer to Ann went out
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, message_id, from_addr, to_addr, subject, sent_at,"
            " received_at, body, approval_id) VALUES (?, ?, ?, 'out', '<e1@ember>', 'ember@example.invalid',"
            " 'ann@example.org', 'Re: A question', ?, ?, 'Yes.', 1)",
            (scope.mode, scope.session, scope.life_id, to_iso(agent.clock.now() + timedelta(minutes=5)), since),
        )
    now = to_iso(agent.clock.now() + timedelta(minutes=10))
    with agent.db.connection() as conn:
        assert mailstore.counts(conn, scope, since) == (2, 1)
        for name, value in (("inquiries_received", 2), ("inquiries_answered", 1)):
            row = {"metric": name, "project_id": None, "venture_id": None, "created_at": since}
            assert metrics.read(conn, scope, row, None, now).value == value  # type: ignore[arg-type]
            assert metrics.CATALOGUE[name].source == "ember" and metrics.CATALOGUE[name].since_set
    assert first < second


def test_an_opt_out_after_an_automatic_reply_takes_the_unlock_back(data_dir: Path) -> None:
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))  # the dry run's reader wrote (email #1)
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "email_reply", "level": "auto"}, "Stefan").status == 200
    aimed = json.dumps(
        {
            "assessment": "A reader asked.",
            "goal": "Answer her",
            "steps": ["answer"],
            "sleep_minutes": 60,
            "focus_milestone_id": goal,
        }
    )
    transport.outcomes.extend(
        [
            reply([{"type": "text", "text": aimed}], "end_turn"),
            calls(("propose_email", REPLY)),
            text("Answered."),
            JOURNAL,
        ]
    )
    assert agent.run_cycle("schedule").status == "completed"
    [made] = rows(agent, "SELECT id, status, decided_by FROM approvals")
    assert (made["status"], made["decided_by"]) == ("approved", policy.POLICY_BY)  # an automatic reply
    assert agent.execute_approved() == [(made["id"], "simulated")]
    agent.clock.advance(minutes=30)
    with agent.db.transaction() as conn:
        mailstore.suppress(conn, agent.scope(), READER, to_iso(agent.clock.now()), "asked to stop", 1)
    agent.run_policy()
    assert rows(agent, "SELECT rule, level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {
            "rule": "email_reply",
            "level": "manual",
            "by": policy.REVOKED_BY,
            "why": f"the person you answered in request #{made['id']} asked to stop",  # 0.14.0: never their address
        }
    ]
