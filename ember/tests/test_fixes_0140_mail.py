"""0.14.0 (FIX NOW 10, 23 and X25): Ember's mail.

Only a person's email counts as someone having written: its sender verified by the receiving mail provider (the
topmost Authentication-Results header), and no list's, machine's or Ember's own. That one test decides a first contact
(in code and in the database), inquiries and their metrics, answers in a thread and event wakes. Opt-outs are caught
after a greeting and in more words, and cover the address Ember wrote to. A message the server doesn't hand over is
asked for again (and skipped in the end); a failing mailbox is read less and less often. Text hidden by a style
sheet, a colour or an offset is dropped from HTML mail.
"""

from __future__ import annotations

import json
import sqlite3
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import never, obligations, policy  # noqa: E402
from app.db import Database, discover_migrations, migrate  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import mail, mailstore  # noqa: E402
from app.integrations.optout import opt_out  # noqa: E402
from tests.test_agent import make_agent, plan, reply, rows, text  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_executor import REPLY  # noqa: E402
from tests.test_mail import JOURNAL, LIVE, PASSWORD, READER, FakeIMAP, live_agent, mail_cycle, raw_mail  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import a_milestone  # noqa: E402
from tests.test_ventures import tool_results  # noqa: E402

FORGED = "mx.example.invalid; spf=softfail smtp.mailfrom=bank.example; dmarc=fail (p=none) header.from=bank.example"


def verified(domain: str) -> str:
    """What a mail provider adds to an email whose sender it verified."""
    return f"mx.example.invalid; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"


def message(
    uid: int,
    sender: str,
    verdict: str | None = None,
    subject: str = "A question",
    body: str = "Do you make A5 planners?",
    **headers: str,
) -> mail.IncomingMail:
    """An email as the mailbox reads it; ``verdict``: its Authentication-Results (verified for its domain if None)."""
    msg = EmailMessage()
    if verdict != "":
        msg["Authentication-Results"] = verdict or verified(sender.rstrip(">").rsplit("@", 1)[1])
    msg["From"] = sender
    msg["To"] = mail.FAKE_ADDRESS
    msg["Subject"] = subject
    msg["Message-ID"] = f"<u{uid}@example.org>"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return mail.parse_message(msg.as_bytes(), uid)


def arrive(agent: Any, *mails: mail.IncomingMail) -> list[int]:
    """These emails come in (through mailstore.fetch, as a real check stores them); their ids."""

    class Box(mail.FakeMailbox):
        def fetch_new(self, after_uid: int, limit: int = 20, uidvalidity: Any = None, deadline: Any = None) -> Any:
            return mail.FetchResult(list(mails), max(m.uid for m in mails), self.uidvalidity)

    scope = agent.scope()
    return mailstore.fetch(agent.db, agent.clock, scope, Box(scope.session)).stored


# --- FIX NOW 10: the provider's verdict ---


@pytest.mark.parametrize(
    ("sender", "verdicts", "expected"),
    [
        (  # Gmail's, with a comment that holds a ";"
            "ann@example.org",
            [
                "mx.google.com; dkim=pass header.i=@example.org header.s=s1 header.b=abc; spf=pass (google.com: domain"
                " of ann@example.org designates 1.2.3.4 as permitted sender; ok) smtp.mailfrom=ann@example.org;"
                " dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.org"
            ],
            True,
        ),
        (  # rspamd's (mailbox.org)
            "ann@example.org",
            [
                "mx1.mailbox.org; dkim=pass header.d=example.org header.s=x; dmarc=pass (policy=reject) header.from="
                "example.org; spf=pass (mx1.mailbox.org: domain of ann@example.org) smtp.mailfrom=ann@example.org"
            ],
            True,
        ),
        (  # Microsoft's, without an authserv-id
            "ann@example.org",
            [
                "spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=example.org; dkim=pass (signature was verified)"
                " header.d=example.org;dmarc=pass action=none header.from=example.org;compauth=pass reason=100"
            ],
            True,
        ),
        ("ann@example.org", ["mx.example.invalid; dkim=pass header.d=mail.example.org"], True),  # a subdomain
        ("ann@mail.example.org", ["mx.example.invalid; dkim=pass header.d=example.org"], True),  # a parent
        ("ann@example.org", ["mx.example.invalid; spf=pass smtp.mailfrom=bounce@example.org"], True),
        ("ann@shop.co.uk", ["mx.example.invalid; dkim=pass header.d=mail.shop.co.uk"], True),
        ("ceo@bank.example", [FORGED], False),
        ("ann@shop.co.uk", ["mx.example.invalid; dkim=pass header.d=other.co.uk"], False),
        # Review: two domains under one public suffix are two organisations, whatever the suffix
        ("ceo@victim.me.uk", ["mx.example.invalid; dkim=pass header.d=attacker.me.uk; dmarc=none"], False),
        ("ceo@victim.ne.jp", ["mx.example.invalid; spf=pass smtp.mailfrom=x@attacker.ne.jp"], False),
        ("ceo@victim.github.io", ["mx.example.invalid; dkim=pass header.d=attacker.github.io"], False),
        ("ann@a.example.org", ["mx.example.invalid; dkim=pass header.d=b.example.org"], False),  # dmarc says more
        ("ann@example.org", ["mx.example.invalid; dkim=pass header.d=org"], False),
        (
            "ann@example.org",
            [
                "mx.example.invalid; dkim=pass header.d=sendgrid.net; spf=pass smtp.mailfrom="
                "bounces.sendgrid.net; dmarc=none"
            ],
            False,
        ),
        ("ann@example.org", ["mx.example.invalid; dmarc=pass header.from=other.example"], False),
        ("ann@example.org", ["mx.example.invalid; dmarc=bestguesspass header.from=example.org"], False),
        ("ann@example.org", ["mx.example.invalid; dkim=fail header.d=example.org; dmarc=fail"], False),
        ("ann@example.org", [], False),  # no verdict: unverified
        # Only the topmost counts: the one the receiving server added; the sender can add any below it
        ("ceo@bank.example", [FORGED, verified("bank.example")], False),
        ("ann@example.org", [verified("example.org"), FORGED], True),
    ],
)
def test_the_provider_s_verdict_is_read_from_the_topmost_header(
    sender: str, verdicts: list[str], expected: bool
) -> None:
    msg = EmailMessage()
    for verdict in verdicts:
        msg["Authentication-Results"] = verdict
    msg["From"], msg["To"], msg["Subject"] = sender, mail.FAKE_ADDRESS, "Hi"
    msg.set_content("Hello")
    assert mail.parse_message(msg.as_bytes(), 1).authenticated is expected


DSN = (
    b"From: Mail Delivery Subsystem <mail@mx.example>\r\nTo: ember@example.invalid\r\nSubject: Undelivered Mail\r\n"
    b"Message-ID: <dsn1@mx.example>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/report;"
    b' report-type=delivery-status; boundary="b1"\r\n\r\n--b1\r\nContent-Type: text/plain\r\n\r\nNot delivered.\r\n'
    b"--b1\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.example\r\n\r\n--b1--\r\n"
)


def test_machine_and_bulk_mail_is_no_person_s() -> None:
    """A machine's mail is told by its sender or its headers; a person's email with ordinary headers is no machine's."""
    machines = (
        "no_reply@shop.example",
        "noreply+abc@shop.example",
        "noreply-orders@shop.example",
        "NoReply@shop.example",
        "notifications@shop.example",
        "notification@facebookmail.com",
        "bounce-123@shop.example",
        "do_not_reply@shop.example",
        "DoNotReply@shop.example",
        "transaction@etsy.com",
        "MAILER-DAEMON@mx.example",
        "postmaster@mx.example",
    )
    for n, sender in enumerate(machines):
        assert message(n, sender).machine, sender
    for name, value in (
        ("X_Auto_Response_Suppress", "All"),
        ("Return_Path", "<>"),
        ("X_Spam_Flag", "YES"),
        ("X_Spam_Status", "Yes, score=9.1"),
    ):
        assert message(40, "ann@example.org", **{name: value}).machine, name
    for name, value in (("Auto_Submitted", "auto-replied"), ("Precedence", "bulk"), ("List_Id", "<news.example>")):
        assert message(41, "ann@example.org", **{name: value}).bulk, name
    assert mail.parse_message(DSN, 50).machine
    for sender in ("ann@example.org", "noreen@example.org", "bouncer@example.org", "info@shop.example"):
        person = message(60, sender, Return_Path=f"<{sender}>", X_Spam_Status="No, score=0.1")
        assert not person.machine and not person.bulk, sender
    assert not message(61, "ann@example.org", X_Auto_Response_Suppress="None").machine


def test_a_machine_s_mail_is_stored_as_no_person_s_but_its_stop_counts(data_dir: Path) -> None:
    """A person's "stop" the provider took for spam still counts (a missed opt-out would break the law); a newsletter's
    "unsubscribe" doesn't, as before."""
    agent, _ = mail_cycle(data_dir, calls(("email_inbox", {})))
    spam, news = arrive(
        agent,
        message(90, "Jan <jan@example.org>", subject="Re: Planner", body="Stopp.", X_Spam_Flag="YES"),
        message(91, "news@list.example", subject="Weekly", body="Unsubscribe", List_Unsubscribe="<https://x.example>"),
    )
    assert rows(agent, f"SELECT id, bulk FROM emails WHERE id IN ({spam}, {news}) ORDER BY id") == [
        {"id": spam, "bulk": 1},
        {"id": news, "bulk": 1},
    ]
    assert rows(agent, "SELECT address FROM email_suppressions") == [{"address": "jan@example.org"}]


def test_a_forged_sender_never_counts_as_someone_who_wrote(data_dir: Path) -> None:
    """The reproduction: a forged email asks for a price, and the owner's email_reply unlock is on auto. Ember's
    answer to it waits for the owner as a first contact (in code and in the database); the answer to the dry run's
    verified reader goes out as before."""
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))  # the reader's email (#1), verified
    [forged] = arrive(agent, message(60, "CEO <ceo@bank.example>", FORGED, body="What does a planner cost?"))
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "email_reply", "level": "auto"}, "Stefan").status == 200
    aimed = json.dumps(
        {
            "assessment": "Two asked.",
            "goal": "Answer",
            "steps": ["answer"],
            "sleep_minutes": 60,
            "focus_milestone_id": goal,
        }
    )
    answer = {"reply_to_email_id": forged, "subject": "Re: A question", "body": "It costs 4 EUR.", "reason": "Asked."}
    transport.outcomes.extend(
        [
            reply([{"type": "text", "text": aimed}], "end_turn"),
            calls(("propose_email", answer), ("propose_email", REPLY)),
            text("Answered."),
            JOURNAL,
        ]
    )
    assert agent.run_cycle("schedule").status == "completed"
    to_forged, to_reader = rows(agent, "SELECT id, status, decided_by FROM approvals ORDER BY id")
    assert (to_forged["status"], to_forged["decided_by"]) == ("pending", None)  # it waits for the owner
    assert (to_reader["status"], to_reader["decided_by"]) == ("approved", policy.POLICY_BY)
    scope = agent.scope()
    with agent.db.connection() as conn:
        request = conn.execute("SELECT * FROM approvals WHERE id = ?", (to_forged["id"],)).fetchone()
        assert never.reasons(conn, request) == ["first_contact"]
        view = conn.execute("SELECT first_contact FROM approvals_never WHERE approval_id = ?", (request["id"],))
        assert view.fetchone()[0] == 1
        assert not mailstore.has_written(conn, scope, "ceo@bank.example")
        assert mailstore.has_written(conn, scope, READER)
        assert forged not in [r["id"] for r in mailstore.inquiries(conn, scope)]
        assert policy.match(conn, scope, request) is None  # not a thread they started
    card = next(a for a in agent.dashboard()["approvals"] if a["id"] == to_forged["id"])
    assert card["first_contact"] is True
    assert "No verified email from this person reached you" in tool_results(agent, "propose_email")[0]["result"]
    assert [done for done, _ in agent.execute_approved()] == [to_reader["id"]]


def test_the_code_and_the_database_agree_on_who_wrote(data_dir: Path) -> None:
    agent, _ = mail_cycle(data_dir, calls(("email_inbox", {})))
    scope, now = agent.scope(), to_iso(agent.clock.now())
    kinds = {"person": (0, 1), "unverified": (0, 0), "before": (0, None), "machine": (1, 1), "old": (None, None)}
    with agent.db.transaction() as conn:
        for n, (name, (bulk, verdict)) in enumerate(kinds.items()):
            conn.execute(
                "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, from_addr, to_addr, subject,"
                " received_at, body, bulk, authenticated) VALUES (?, ?, ?, 'in', 5, ?, ?, 'e@example.invalid', 's', ?,"
                " 'b', ?, ?)",
                (scope.mode, scope.session, scope.life_id, 500 + n, f"{name}@example.org", now, bulk, verdict),
            )
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        for to in [*kinds, "out", "nobody"]:
            action = json.dumps({"to": f"{to.upper()}@example.org", "subject": "Hi", "body": "Hello"})
            made = conn.execute(
                "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description,"
                " payload, payload_sha256, expected_cost, expected_benefit, executor, action) VALUES (?, ?, ?, ?, ?,"
                " 'contact', 't', 'd', ?, ?, 'none', 'b', 'email', ?)",
                (scope.mode, scope.session, scope.life_id, cycle, now, f"p-{to}", f"h-{to}", action),
            ).lastrowid
        conn.execute(  # an email Ember sent (with that address as its sender): no one wrote to Ember
            "INSERT INTO emails (mode, session, life_id, direction, from_addr, to_addr, subject, received_at, body,"
            " approval_id, bulk, authenticated) VALUES (?, ?, ?, 'out', 'out@example.org', 'x@example.org', 's', ?,"
            " 'b', ?, 0, 1)",
            (scope.mode, scope.session, scope.life_id, now, made),
        )
    with agent.db.connection() as conn:
        found = {}
        for row in conn.execute("SELECT * FROM approvals WHERE payload LIKE 'p-%'").fetchall():
            first = conn.execute("SELECT first_contact FROM approvals_never WHERE approval_id = ?", (row["id"],))
            in_db = bool(first.fetchone()[0])
            assert ("first_contact" in never.reasons(conn, row)) is in_db
            assert mailstore.has_written(conn, scope, json.loads(row["action"])["to"]) is not in_db
            found[row["payload"][2:]] = in_db
    assert found == {k: k != "person" for k in [*kinds, "out", "nobody"]}


def test_ember_s_own_machines_and_forged_mail_are_no_inquiries(data_dir: Path) -> None:
    """Mail from Ember's own address (the owner's test from webmail), a bounce, a no-reply receipt and a forged email
    wait for no answer, count in no metric and wake no one; a verified person's email does all three."""
    agent, _ = mail_cycle(data_dir, calls(("email_inbox", {})))
    agent.check_events()  # the agenda begins
    agent.clock.advance(minutes=20)
    since = to_iso(agent.clock.now())
    *others, ann = arrive(
        agent,
        message(61, f"Ember <{mail.FAKE_ADDRESS}>", subject="Test"),
        message(62, "MAILER-DAEMON@mx.example", subject="Undelivered Mail"),
        message(63, "Shop <noreply+abc@shop.example>", subject="Your receipt"),
        message(64, "ceo@bank.example", FORGED),
        message(65, "Ann <ann@example.org>", subject="Do you ship to Austria?"),
    )
    scope = agent.scope()
    assert rows(agent, f"SELECT bulk, authenticated FROM emails WHERE id = {others[0]}") == [
        {"bulk": 1, "authenticated": 1}
    ]
    with agent.db.connection() as conn:
        assert [r["id"] for r in mailstore.inquiries(conn, scope)] == [1, ann]
        assert mailstore.counts(conn, scope, since) == (1, 0)
        said = obligations.text(conn, scope, agent.clock.today())
    assert f"#{ann} (today)" in said and not any(f"#{n} (" in said for n in others)
    agent.check_events()
    assert rows(agent, "SELECT kind, key FROM agenda WHERE baseline = 0") == [{"kind": "inquiry", "key": str(ann)}]


def test_only_a_person_s_answer_to_ember_s_email_wakes_it(data_dir: Path) -> None:
    """An out-of-office, a forged answer and a "stop" answer Ember's email without waking it (the MAIL section still
    shows them); a verified person's answer wakes it."""
    agent, _ = mail_cycle(data_dir, calls(("propose_email", REPLY)))  # request #1
    scope = agent.scope()
    with agent.db.transaction() as conn:  # Ember's answer to the reader went out
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, message_id, from_addr, to_addr, subject, sent_at,"
            " received_at, body, approval_id) VALUES (?, ?, ?, 'out', '<m1@ember>', ?, ?, 'Re: Planner', ?, ?, 'Yes.',"
            " 1)",
            (scope.mode, scope.session, scope.life_id, mail.FAKE_ADDRESS, READER, *[to_iso(agent.clock.now())] * 2),
        )
    agent.check_events()
    agent.clock.advance(minutes=40)
    lena = f"Lena <{READER}>"
    thread = {"In_Reply_To": "<m1@ember>", "References": "<m1@ember>"}
    away = message(70, lena, subject="Automatic reply: Re: Planner", Auto_Submitted="auto-replied", **thread)
    forged = message(71, lena, FORGED.replace("bank.example", "example.org"), subject="Re: Planner", **thread)
    arrive(agent, away, forged)
    agent.check_events()
    assert rows(agent, "SELECT kind FROM agenda WHERE baseline = 0") == []
    assert agent.decide().trigger != "event"
    [answer] = arrive(agent, message(72, lena, subject="Re: Planner", body="Great, thanks!", **thread))
    agent.clock.advance(minutes=20)
    agent.check_events()
    assert rows(agent, "SELECT kind, key, urgent FROM agenda WHERE baseline = 0") == [
        {"kind": "reply", "key": str(answer), "urgent": 1}
    ]
    decision = agent.decide()
    assert (decision.run, decision.trigger) == (True, "event")
    [stop] = arrive(agent, message(73, lena, subject="Re: Planner", body="Stop", **thread))
    agent.check_events()
    assert str(stop) not in [r["key"] for r in rows(agent, "SELECT key FROM agenda")]
    assert rows(agent, "SELECT address FROM email_suppressions") == [{"address": READER}]


def test_mail_stored_before_0_14_0_has_no_verdict_and_counts_for_no_one(tmp_path: Path) -> None:
    """The migration on a database just before it: stored mail keeps no verdict (NULL), so an email to someone who
    wrote before is a first contact again until they write again; the rest of NEVER reads as before, and the
    verdict of a stored email never changes."""
    db_file = tmp_path / "ember.db"
    mine = next(m for m in discover_migrations() if m.name == "mail")
    migrate(db_file, [m for m in discover_migrations() if m.version < mine.version], backup_dir=tmp_path / "b")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', 'then', 'completed', 'schedule', 0, 1)"
        )
        for uid, sender, bulk in ((1, "ann@example.org", 0), (2, "news@list.example", 1), (3, "old@example.org", None)):
            conn.execute(
                "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, from_addr,"
                " to_addr, subject, received_at, body, bulk) VALUES ('live', 0, 1, 'in', 1, ?, ?, ?, 'e@x.example',"
                " 's', 'then', 'b', ?)",
                (uid, f"<{uid}@example.org>", sender, bulk),
            )
        for n, (executor, to) in enumerate((("email", "ann@example.org"), ("reddit_link", None), ("email", "x@y.z"))):
            action = json.dumps({"to": to, "subject": "Re: s", "body": "b"} if to else {"title": "t"})
            conn.execute(
                "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description,"
                " payload, payload_sha256, expected_cost, expected_benefit, executor, action) VALUES ('live', 0, 1,"
                " 1, 'then', 'contact', 't', 'd', ?, ?, 'none', 'b', ?, ?)",
                (f"p{n}", f"h{n}", executor, action),
            )
    with old.connection() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM approvals_never ORDER BY 1")]
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "b") == [
        m.version for m in discover_migrations() if m.version >= mine.version
    ]
    upgraded = Database(db_file)
    with upgraded.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT authenticated FROM emails ORDER BY id")] == [None, None, None]
        after = [tuple(r) for r in conn.execute("SELECT * FROM approvals_never ORDER BY 1")]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"approvals_never_on_unlock", "policy_uses_never", "emails_verdict_fixed"} <= names
    first_contact = 3  # the column; the rest but "never" read as before
    assert [r[first_contact] for r in before] == [0, 0, 1]
    assert [r[first_contact] for r in after] == [1, 0, 1]  # Ann's email has no verdict: fail safe
    assert [r[:first_contact] + r[first_contact + 1 : -1] for r in before] == [
        r[:first_contact] + r[first_contact + 1 : -1] for r in after
    ]
    for change in ("authenticated = 1", "authenticated = 0", "bulk = 1", "bulk = NULL"):
        with pytest.raises(sqlite3.IntegrityError, match="cannot change"), upgraded.transaction() as conn:
            conn.execute(f"UPDATE emails SET {change} WHERE uid = 1")  # noqa: S608 - fixed test text
    with upgraded.transaction() as conn:
        conn.execute("UPDATE emails SET read_by_agent_at = 'now' WHERE uid = 1")  # reading it still counts
        conn.execute(  # Ann writes again, verified
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, from_addr, to_addr,"
            " subject, received_at, body, bulk, authenticated) VALUES ('live', 0, 1, 'in', 1, 4, '<4@example.org>',"
            " 'Ann@Example.org', 'e@x.example', 's', 'now', 'b', 0, 1)"
        )
    with upgraded.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT first_contact FROM approvals_never ORDER BY 1")] == [0, 0, 1]
    upgraded.close()


# --- FIX NOW 23: opt-outs, refused fetches, a failing mailbox ---

ASKS = [
    "Hello Ember,\nPlease stop.\n\nThanks",
    "Hi,\n\nstop\n\nLena",
    "Hallo,\n\nStopp.\n\nGruß Jan",
    "Please remove my address from your list.",
    "Take my email off your list please.",
    "I do not want any more emails from you.",
    "Leave me alone.",
    "Not interested. Don't write again.",
    "Bitte entfernen Sie mich aus Ihrem Verteiler.",
    "Bitte streichen Sie mich von Ihrer Liste.",
    "Hören Sie auf, mir zu schreiben.",
    "Ich möchte nicht mehr angeschrieben werden.",
    "Stoppen Sie bitte diese E-Mails.",
    # more of the same
    "Dear Sir or Madam,\n\nSTOP!!!\n\nRegards",
    "Good morning,\nplease, stop it.",
    "Hi Ember,\nThanks.\nStop please, thank you.",
    "Sehr geehrte Damen und Herren,\n\nbitte stoppen.\n\nMit freundlichen Grüßen\nJan Weber",
    "Guten Tag,\nStopp bitte!\nJan",
    "Moin,\n\nbitte nicht mehr anschreiben.",
    "Hallo Ember,\nbitte nehmen Sie mich aus dem Verteiler.\nDanke",
    "Hallo,\nhören Sie bitte damit auf!",
    "Please remove my e-mail address from your mailing list.",
    "Please don't email me anymore.",
    "Don't contact us again.",
    "I don't want further messages.",
    "Hello,\nplease stop now.\nBest, Ann",
    # Review: "no" beside a stop, more German, more words
    "No. Stop.",
    "No, stop.",
    "Hi,\nNo thanks, stop",
    "No more. Stop!",
    "Stop, no more.",
    "Nein danke. Stopp.",
    "Bitte aufhören!",
    "Aufhören!",
    "Hallo,\nhört auf damit.",
    "Hallo,\nIch will das nicht. Hört auf!",
    "Bitte lassen Sie mich in Ruhe.",
    "Hello,\n\nI received your email.\nI'd like you to stop.\n",
    "Dear Ember, please stop sending me these",
    "Please stop sending.",
    "Please remove my email.",
    "Can you remove me from your list?",
    # Review round 2: "us", "spamming", "kindly", "wish to receive", German "mir" and "melden Sie mich ab"
    "Please stop contacting us.",
    "Stop emailing us.",
    "Please remove us from your list.",
    "Kindly remove us from your mailing list.",
    "Do not email us.",
    "Please take us off your list.",
    "Please stop spamming me.",
    "Stop spamming me!",
    "Stop spamming me pls",
    "Not interested. Please stop messaging.",
    "Stop it please, I'm not interested.",
    "Hi Ember,\n\nkindly stop.",
    "Kindly stop emailing us.",
    "Good morning,\n\nWe do not wish to receive further emails.",
    "I do not wish to receive these emails.",
    "Bitte schreiben Sie mir nicht mehr.",
    "Schreiben Sie mir nicht mehr.",
    "Bitte mailen Sie mir nicht mehr.",
    "Bitte schreibt mir nicht mehr.",
    "Schreib mir nicht mehr.",
    "Melden Sie mich bitte ab.",
    "Bitte melden Sie mich ab.",
    "Hör auf mir zu schreiben.",
    "Hört auf, mir zu schreiben.",
]
ORDINARY = [
    "Hi,\n\nI'll stop by tomorrow to pick it up.\n\nAnn",
    "Hello,\nstop by anytime!",
    "Hi Ember,\nThe bus stop is right outside.",
    "Hallo,\nnächster Stopp: Berlin.\nJan",
    "Hi,\nNext stop: the printer.",
    "Hello,\nI can't stop smiling, thank you!",
    "Hi,\nNon-stop fun with the planner!",
    "Full stop.",
    "Hi Ember,\n\nCould you remove the watermark?\n\nThanks",
    "Please remove my address from the order, I moved.",
    "Hallo,\nhören Sie auf meine Worte: das ist super!",
    "Hi,\nplease stop worrying, the price is fine.",
    "Hello,\nDo you have the planner in A5?\nWhat would it cost?\nThanks, Ann",
    "Hallo,\n\nich möchte den Planer bestellen.\n\nGruß Jan",
    "Hi,\nthe stop-motion video was great.",
    # Review: what the wider rules read as asking at first
    "Did it stop working?",
    "Hi,\nstop! I love it",
    "Bitte entfernen Sie mich nicht.",
    "Please remove my email typo in the order",
    "Leave me alone with this price? lol",
    "Hi, I don't want any more emails to get lost, so I resend this.",
    "Hi,\nmy kids won't stop asking for the planner",
    "Hello,\nCan you stop the order? I ordered twice.",
    "Hallo,\nlassen Sie mich in Ruhe überlegen, dann bestelle ich.",
    "Hi,\nI'd like you to stop the second order.",
    "Hello,\nStop the presses, this is great",
    # Review round 2: near the new phrases
    "Don't stop sending us the samples, we love them.",
    "Do not email us the PDF, post it.",
    "I wish to receive the invoice by post.",
    "Kindly send me the price list.",
    "Schreiben Sie mir bitte mehr dazu.",
    "Melden Sie sich bitte ab Montag an.",
    "Hör auf zu zweifeln, wir machen das!",
    "Stop it please, I'm laughing so hard",
    "Nothing can stop us now, thanks to the planner!",
]


@pytest.mark.parametrize("body", ASKS)
def test_asking_to_stop_after_a_greeting_and_in_more_words(body: str) -> None:
    assert opt_out("Re: Planner", body) is not None


@pytest.mark.parametrize("body", ORDINARY)
def test_ordinary_replies_don_t_ask(body: str) -> None:
    assert opt_out("Re: Planner", body) is None


def test_the_reason_is_the_line_that_asked() -> None:
    assert opt_out("Re: hi", "Hello Ember,\nPlease stop.\n\nThanks") == "Please stop."
    assert opt_out("Re: hi", "Hallo,\n\nStopp.\n\nGruß Jan") == "Stopp."


def test_a_stop_in_a_thread_covers_the_address_ember_wrote_to(data_dir: Path) -> None:
    """Ember wrote to info@; a colleague answers "stop" from her own address: neither is emailed again."""
    agent, _ = mail_cycle(data_dir, calls(("propose_email", REPLY)))  # request #1
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, message_id, from_addr, to_addr, subject, sent_at,"
            " received_at, body, approval_id) VALUES (?, ?, ?, 'out', '<m9@ember>', ?, 'Info@Shop.example',"
            " 'Your shop', ?, ?, 'Hello', 1)",
            (scope.mode, scope.session, scope.life_id, mail.FAKE_ADDRESS, *[to_iso(agent.clock.now())] * 2),
        )
    stop = message(
        80,
        "Anna <anna@shop.example>",
        subject="Re: Your shop",
        body="Hallo,\n\nStopp.\n\nAnna",
        In_Reply_To="<m9@ember>",
    )
    [email_id] = arrive(agent, stop)
    assert rows(agent, "SELECT address, reason, email_id FROM email_suppressions ORDER BY address") == [
        {"address": "anna@shop.example", "reason": 'replied "Stopp."', "email_id": email_id},
        {
            "address": "info@shop.example",
            "reason": f"an answer to Ember's email to this address asked to stop (email #{email_id})",
            "email_id": email_id,
        },
    ]
    with agent.db.connection() as conn:
        assert mailstore.is_suppressed(conn, scope, "info@shop.example")
    # An automatic reply in the thread asks nothing
    away = message(
        81,
        "bob@other.example",
        subject="Out of office",
        body="Stop",
        In_Reply_To="<m1@ember>",
        Auto_Submitted="auto-replied",
    )
    arrive(agent, away)
    assert len(rows(agent, "SELECT address FROM email_suppressions")) == 2


class RefusingIMAP(FakeIMAP):
    """A server that doesn't hand over some messages: ``refuse`` maps a UID to how (NO, or OK with nothing)."""

    refuse: dict[int, str] = {}

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "FETCH" and int(args[0]) in self.refuse:
            self.calls.append(("uid", command, *args))
            if self.refuse[int(args[0])] == "NO":
                return "NO", [b"[UNAVAILABLE] Try again later"]
            return "OK", [None]  # expunged in between: nothing comes back
        return super().uid(command, *args)


@pytest.fixture
def refusing(monkeypatch: pytest.MonkeyPatch) -> type[RefusingIMAP]:
    RefusingIMAP.instances = []
    RefusingIMAP.mails = {
        1: raw_mail(1),
        2: raw_mail(2, sender="Lena <lena@example.org>", subject="Please stop", body="stop"),
        3: raw_mail(3, subject="Third"),
    }
    RefusingIMAP.validity = 7
    RefusingIMAP.password = PASSWORD
    RefusingIMAP.refuse = {2: "NO"}
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", RefusingIMAP)
    return RefusingIMAP


def test_a_message_the_server_refuses_is_fetched_again(data_dir: Path, refusing: type[RefusingIMAP]) -> None:
    """The reproduction: UID 2 (a "stop") was refused, stored empty, and passed for good."""
    box = mail.LiveMailbox(LIVE)
    result = box.fetch_new(0)
    assert [m.uid for m in result.mails] == [1] and result.last_uid == 1 and result.refused == 2
    assert result.waiting == 2
    agent = live_agent(data_dir, [])
    scope = agent.scope()
    first = mailstore.fetch(agent.db, agent.clock, scope, box)
    assert len(first.stored) == 1 and first.error is None
    assert agent.db.get_meta(mailstore.meta_key(scope.mode, "last_uid")) == "1"
    refusing.refuse = {}  # the server hands it over now
    second = mailstore.fetch(agent.db, agent.clock, scope, box)
    assert len(second.stored) == 2 and second.suppressed == ["lena@example.org"]
    assert rows(agent, "SELECT uid, from_addr, subject FROM emails WHERE uid > 1 ORDER BY uid") == [
        {"uid": 2, "from_addr": "lena@example.org", "subject": "Please stop"},
        {"uid": 3, "from_addr": "ann@example.org", "subject": "Third"},
    ]


def test_a_message_the_server_never_hands_over_is_skipped_in_the_end(
    data_dir: Path, refusing: type[RefusingIMAP]
) -> None:
    refusing.refuse = {2: "gone"}
    box = mail.LiveMailbox(LIVE)
    agent = live_agent(data_dir, [])
    scope = agent.scope()
    for _ in range(mailstore.REFUSED_TRIES - 1):
        assert mailstore.fetch(agent.db, agent.clock, scope, box).stored in ([], [1])
        assert rows(agent, "SELECT uid FROM emails ORDER BY uid") == [{"uid": 1}]
    assert mailstore.fetch(agent.db, agent.clock, scope, box).stored == []  # given up: the next check goes on
    assert any("Ember skipped it" in e["message"] for e in agent.db.recent_events(limit=5))
    assert len(mailstore.fetch(agent.db, agent.clock, scope, box).stored) == 1
    assert rows(agent, "SELECT uid FROM emails ORDER BY uid") == [{"uid": 1}, {"uid": 3}]


def test_a_failing_mailbox_is_read_less_often_and_warns_once(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduction: a wrong password meant a failed login every 15 minutes (96 a day) and a warning each time."""
    FakeIMAP.instances, FakeIMAP.mails, FakeIMAP.validity = [], {1: raw_mail(1)}, 7
    monkeypatch.setattr(FakeIMAP, "password", "changed by the owner")
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    agent = live_agent(data_dir, [])
    for _ in range(24 * 4):  # a day, looked at every 15 minutes
        agent.check_events()
        agent.clock.advance(minutes=15)
    logins = len(FakeIMAP.instances)
    assert 8 <= logins <= 12  # after 15, 30, 60 and 120 minutes, then every 4 hours
    warnings = [e for e in agent.db.recent_events(limit=200) if "Checking Ember's mailbox failed" in e["message"]]
    assert len(warnings) == 1
    assert mailstore.wait_minutes(agent.db, "live", 15) == mailstore.BACKOFF_MINUTES
    monkeypatch.setattr(FakeIMAP, "password", PASSWORD)  # fixed: the next read works, and the wait is 15 minutes again
    agent.clock.advance(minutes=mailstore.BACKOFF_MINUTES)
    agent.check_events()
    assert len(FakeIMAP.instances) == logins + 1
    assert mailstore.wait_minutes(agent.db, "live", 15) == 15
    assert any("can be read again" in e["message"] for e in agent.db.recent_events(limit=5))


def test_a_cycle_doesn_t_read_a_failing_mailbox_before_its_wait_is_over(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2: the read at the start of each cycle logged in whatever the failures."""
    FakeIMAP.instances, FakeIMAP.mails, FakeIMAP.validity = [], {1: raw_mail(1)}, 7
    monkeypatch.setattr(FakeIMAP, "password", "changed by the owner")
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    turns = [plan(steps=["look around"]), text("Done."), JOURNAL]
    agent = live_agent(data_dir, turns * 3)
    for _ in range(3):  # three failed reads: the next waits 60 minutes
        agent.check_events()
        agent.clock.advance(minutes=60)
    assert len(FakeIMAP.instances) == 3
    agent.clock.advance(minutes=-30)
    assert not mailstore.due(agent.db, agent.clock, "live", 15)
    assert agent.run_cycle("schedule").status == "completed"
    assert len(FakeIMAP.instances) == 3  # the cycle left the mailbox alone
    agent.clock.advance(minutes=30)
    assert agent.run_cycle("schedule").status == "completed"
    assert len(FakeIMAP.instances) == 4  # the wait is over: it tries again
    monkeypatch.setattr(FakeIMAP, "password", PASSWORD)
    agent.clock.advance(minutes=mailstore.BACKOFF_MINUTES)
    assert agent.run_cycle("schedule").status == "completed"
    assert mailstore.due(agent.db, agent.clock, "live", 15)  # it works again: every cycle reads it


# --- X25: hidden text in HTML mail ---


def test_text_hidden_by_style_rules_colours_and_offsets_is_dropped() -> None:
    html = (
        "<html><head><style>/* .shown{display:none} */ .h1{display:none} #h2, p.h3 {visibility:hidden}"
        " .wrap .h4{font-size:0} @media (max-width:600px){.desk{display:none}} a:hover .hover{display:none}"
        "</style></head><body>"
        '<p class="h1">HIDDEN-1</p><div id="h2">HIDDEN-2</div><p class="x h3">HIDDEN-3</p>'
        '<div class="wrap"><span class="h4">HIDDEN-4</span></div>'
        '<p style="color:#ffffff">HIDDEN-5</p><font color="#FFFFFE">HIDDEN-6</font>'
        '<span style="color:rgba(0,0,0,0)">HIDDEN-7</span><span style="font-size:1px">HIDDEN-8</span>'
        '<div style="position:absolute;left:-9999px">HIDDEN-9</div><div style="text-indent:-5000px">HIDDEN-10</div>'
        '<div style="mso-hide:all">HIDDEN-11</div>'
        '<span style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)">HIDDEN-12</span>'
        '<table bgcolor="#000000"><tr><td style="color:#000">HIDDEN-13</td>'
        '<td style="color:#fff">Shown 1.</td></tr></table>'
        '<p class="shown">Shown 2.</p><p class="desk">Shown 3.</p><p class="hover">Shown 4.</p>'
        '<td style="color:#ffffff;background-color:#e8541c">Shown 5.</td>'
        '<td background="hero.jpg"><h1 style="color:#fff">Shown 6.</h1></td>'
        '<p style="color:#999999">Shown 7.</p><p style="margin-left:-20px;font-size:0.5em">Shown 8.</p>'
        "</body></html>"
    )
    shown = mail.html_to_text(html)
    assert "HIDDEN" not in shown
    assert [f"Shown {n}." in shown for n in range(1, 9)] == [True] * 8
    dark = '<style>body{background:#111}</style><body><p style="color:#ffffff">Shown on a dark page.</p></body>'
    assert mail.html_to_text(dark) == "Shown on a dark page."
    # What only looks like hiding: a border of 1px, a clip that shows a part, a rule for the cells of one table
    ordinary = (
        "<style>.promo td{display:none} table.x > td{visibility:hidden}</style>"
        '<div style="border-width:1px;border-style:solid;overflow:hidden">Shown 1.</div>'
        '<div style="position:absolute;clip:rect(0,300px,200px,0)">Shown 2.</div>'
        "<table><tr><td>Shown 3.</td></tr></table>"
    )
    assert mail.html_to_text(ordinary).split() == ["Shown", "1.", "Shown", "2.", "Shown", "3."]


@pytest.mark.parametrize(
    "html",
    [  # Review: what got past the style sheet's rules
        "<style>" + "".join(f".a{n}{{display:none}}" for n in range(300)) + ".h{display:none}</style>",
        "<style>" + "".join(f".h.p{n}{{display:none}}" for n in range(300)) + ".h.q{display:none}</style>",
        "<style>/*" + "x" * 100_000 + "*/.h{display:none}</style>",
        "<style>.h{color:#ffffff}</style>",
        "<style>.bg{background:#000} .h{color:#fff}</style>",  # a background elsewhere doesn't make the page unknown
        '<style>.h{color:hsl(0, 0%, 100%)}</style><p style="color:snow">HIDDEN</p>',
        '<style>.h{font-size:0.1em}</style><p style="transform:scale(0)">HIDDEN</p>',
    ],
    ids=["padded", "padded-same-class", "long-comment", "white-class", "background-elsewhere", "hsl-named", "em-scale"],
)
def test_a_style_sheet_can_t_be_padded_past_and_colours_count_from_it(html: str) -> None:
    shown = mail.html_to_text(f'{html}<p class="h q">HIDDEN</p><p>Shown.</p>')
    assert shown == "Shown."


@pytest.mark.parametrize(
    "style",
    [  # Review round 2: inline tricks that got past
        "opacity:0.01",
        "opacity:.001",
        "opacity:4%",
        "color:rgb(100%,100%,100%)",
        "color:rgb(255 255 255)",
        "color:rgb(255 255 255 / 1)",
        "color:hsl(0 0% 100%)",
        "color:rgba(0,0,0,0.01)",
        "color:rgb(0 0 0 / 0%)",
        "color:#ffff",
        "color:#ffffffff",
        "color:#00000001",
        "font-size:1%",
        "font-size:2.5px",
        "text-indent:-100em",
        "left:-400px",
        "margin-left:-60em",
    ],
)
def test_faint_tiny_and_far_off_text_is_dropped(style: str) -> None:
    assert mail.html_to_text(f'<p style="{style}">HIDDEN</p><p>Shown.</p>') == "Shown."


@pytest.mark.parametrize(
    "style",
    [
        "opacity:0.5",
        "opacity:.05",
        "opacity:40%",
        "color:rgb(50%,50%,50%)",
        "color:rgb(255 0 0)",
        "color:#f00f",
        "color:#ff000080",
        "font-size:80%",
        "font-size:3.5px",
        "text-indent:-2em",
        "left:-200px",
        "margin-left:-10em",
    ],
)
def test_readable_text_in_these_styles_stays(style: str) -> None:
    assert mail.html_to_text(f'<p style="{style}">Shown.</p>') == "Shown."
    dark = f'<body style="background:#000"><p style="{style};color:rgb(255 255 255 / 90%)">Shown.</p></body>'
    assert mail.html_to_text(dark) == "Shown."


def test_text_on_a_dark_background_from_a_style_sheet_stays() -> None:
    dark = "<style>.d{background-color:#000} .w{color:#fff}</style>"
    assert mail.html_to_text(f'{dark}<div class="d"><span class="w">Shown 1.</span></div>') == "Shown 1."
    no_body = '<style>body{background:#111}</style><p style="color:#fff">Shown 2.</p>'
    assert mail.html_to_text(no_body) == "Shown 2."
    dark_mode = '<style>@media (prefers-color-scheme:dark){body{background:#000}}</style><p style="color:#fff">x</p>'
    assert mail.html_to_text(dark_mode) == "x"  # a background Ember can't place: the page isn't known


def test_a_style_sheet_with_many_rules_is_read_in_time() -> None:
    """A style sheet can't make reading an email slow: at most _KEY_RULES rules per class, id or tag count, and a
    class with more hiding rules hides."""
    rules = "".join(f".a.b{n}{{display:none}}" for n in range(5_000))
    html = f"<style>{rules}</style>" + '<p class="a b150">x</p>' * 40_000
    started = time.monotonic()
    assert mail.html_to_text(html[: mail.MAX_MESSAGE_BYTES]) == ""
    assert time.monotonic() - started < 20


def test_email_read_says_whether_a_person_wrote_it(data_dir: Path) -> None:
    """Review: the agent sees why an email is no obligation."""
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["read my mail"]),
            calls(("email_read", {"email_id": 1}), ("email_read", {"email_id": 2})),
            text("Done."),
            JOURNAL,
        ],
    )
    assert arrive(agent, message(64, "ceo@bank.example", FORGED), message(65, "Ann <ann@example.org>")) == [1, 2]
    assert agent.run_cycle("schedule").status == "completed"
    forged, ann = (r["result"] for r in tool_results(agent, "email_read"))
    assert "Not a verified person's" in forged and "verified its sender" not in forged
    assert "From: Ann <ann@example.org>" in ann and "Your mail provider verified its sender." in ann
