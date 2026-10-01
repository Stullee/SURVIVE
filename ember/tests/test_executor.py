"""Approved emails, sent by Ember's code: exactly what was approved, once, within the daily limit, never retried.

The dry run's fake mailbox records what it is given; the live path runs against in-process fakes of smtplib and
imaplib, so nothing here reaches the network.
"""

from __future__ import annotations

import json
import smtplib
import ssl
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import netguard
from app.agent.owner import Owner
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.integrations import executor, mail
from tests.economy_helpers import ScriptedTransport
from tests.test_agent import ROOMY, make_agent, plan, rows, text
from tests.test_agent import tools as calls
from tests.test_mail import JOURNAL, LIVE, MAIL_OPTIONS, PASSWORD, READER, FakeIMAP, live_agent
from tests.test_mail import imap as imap  # noqa: F401 - the fixture
from tests.test_owner_api import post

REPLY = {
    "reply_to_email_id": 1,
    "subject": "Re: Is your meal planner available in German?",
    "body": "Hi Lena,\n\nnot yet, but I'll tell you when it is.",
    "reason": "Lena asked whether a German version exists.",
}
FOOTER = (
    "-- \nThis email was written by Ember, an AI agent, on behalf of {owner}, and approved by them before sending. "
    'Reply "stop" and Ember won\'t write to you again.'
)


def proposing(*proposals: dict[str, Any]) -> list[Any]:
    """A cycle that proposes these emails."""
    return [plan(steps=["answer my mail"]), calls(*(("propose_email", p) for p in proposals)), text("Done."), JOURNAL]


def owner(agent: Agent) -> Owner:
    return Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)


def approve(agent: Agent, approval_id: int, **body: Any) -> dict[str, Any]:
    reply = owner(agent).decide(approval_id, {"decision": "approve", **body}, "Stefan")
    assert reply.status == 200, reply.body
    return reply.body["approval"]


def ids(agent: Agent) -> list[int]:
    return [r["id"] for r in rows(agent, "SELECT id FROM approvals ORDER BY id")]


def approval(agent: Agent, approval_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM approvals WHERE id = {approval_id}")[0]


def dashboard_row(agent: Agent, approval_id: int) -> dict[str, Any]:
    return next(a for a in agent.dashboard()["approvals"] if a["id"] == approval_id)


def recording(agent: Agent) -> list[bool]:
    """Wrap the mailbox's send to note whether it ran sealed."""
    sealed: list[bool] = []
    assert agent.mailbox is not None
    original = agent.mailbox.send

    def send(message: EmailMessage, to: str) -> mail.SendResult:
        sealed.append(netguard.is_sealed())
        return original(message, to)

    agent.mailbox.send = send  # type: ignore[method-assign]
    return sealed


# --- the dry run, end to end ---


def test_the_fake_mailbox_flow_end_to_end(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            plan(steps=["answer Lena"]),
            calls(("email_inbox", {})),
            calls(("email_read", {"email_id": 1})),
            calls(("propose_email", REPLY)),
            text("Proposed an answer."),
            JOURNAL,
            plan(steps=[], sleep=600),
        ],
    )
    assert agent.run_cycle("schedule").status == "completed"
    [approval_id] = ids(agent)
    assert agent.execute_approved() == []  # nothing is sent before the owner decides
    assert dashboard_row(agent, approval_id)["execution"] is None
    decided = approve(agent, approval_id)
    assert decided["executor"] == "email"
    assert dashboard_row(agent, approval_id)["execution"]["status"] == "waiting"
    assert agent.sensor_fields()["email_waiting"] == 1

    sealed = recording(agent)
    assert agent.execute_approved() == [(approval_id, "simulated")]
    assert sealed == [True]  # a dry run's send can't reach the network
    [sent] = agent.mailbox.sent  # type: ignore[union-attr]
    assert sent["To"] == READER and sent["Subject"] == REPLY["subject"]
    assert str(sent["From"]) == '"Ember (AI agent)" <ember@example.invalid>'
    assert sent["In-Reply-To"] == sent["References"] == "<planner-question-1@example.org>"
    assert sent["Message-ID"].endswith("@example.invalid>") and sent.get_content_type() == "text/plain"
    assert sent.get_content() == f"{REPLY['body']}\n\n{FOOTER.format(owner='its owner')}\n"

    done = approval(agent, approval_id)
    assert (done["status"], done["closed_by"], done["result_note"]) == ("done", "Ember", "Dry run: not really sent")
    assert done["seen_cycle_id"] is None and done["version"] == decided["version"] + 1
    action = rows(agent, "SELECT status, result, error, finished_at FROM email_actions")[0]
    assert (action["status"], action["result"], action["error"]) == ("simulated", "Dry run: not really sent", None)
    out = rows(agent, "SELECT * FROM emails WHERE direction = 'out'")[0]
    assert (out["to_addr"], out["approval_id"], out["subject"]) == (READER, approval_id, REPLY["subject"])
    assert out["body"].endswith("won't write to you again.\n") and out["message_id"] == sent["Message-ID"]
    execution = dashboard_row(agent, approval_id)["execution"]
    assert execution["status"] == "simulated" and execution["result"] == "Dry run: not really sent"
    assert agent.integrations()["email"]["sent_today"] == 1 and agent.sensor_fields()["email_waiting"] == 0

    assert agent.execute_approved() == [] and len(agent.mailbox.sent) == 1  # type: ignore[union-attr] # never again
    agent.run_cycle("schedule")
    planned = json.dumps(transport.sent[-1]["messages"], ensure_ascii=False)
    assert f'Request #{approval_id} (contact) \\"Email to {READER}: {REPLY["subject"]}\\": done' in planned
    assert 'Result: \\"Dry run: not really sent\\"' in planned
    assert approval(agent, approval_id)["seen_cycle_id"] is not None


def test_the_agent_hears_that_its_email_will_be_sent(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [*proposing(REPLY), plan(steps=[], sleep=600)])
    agent.run_cycle("schedule")
    approve(agent, ids(agent)[0], decision="approve_with_changes", final_payload="Hallo Lena!")
    agent.run_cycle("schedule")  # cycles are off in between: nothing was sent yet
    planned = json.dumps(transport.sent[-1]["messages"], ensure_ascii=False)
    assert "approved with changes. Your owner changed the email's text; this is what is sent:" in planned
    assert "Hallo Lena!" in planned and "Ember's code sends it and you'll hear the result" in planned


def test_approve_with_changes_edits_the_body_only(data_dir: Path) -> None:
    other = {"to": "shop@example.com", "subject": "Your planner question", "body": "Same text.", "reason": "r"}
    agent, _ = make_agent(data_dir, proposing(REPLY, other))
    agent.run_cycle("schedule")
    first, second = ids(agent)
    who = owner(agent)
    too_long = who.decide(first, {"decision": "approve_with_changes", "final_payload": "x" * 5_001}, None)
    assert too_long.status == 422 and too_long.body["field"] == "final_payload"
    changed = approve(agent, first, decision="approve_with_changes", final_payload="Hallo Lena,\n\nnoch nicht.")
    assert changed["status"] == "approved_with_changes"
    same = approve(agent, second, decision="approve_with_changes", final_payload="Same text.")
    assert same["status"] == "approved" and same["final_payload"] is None  # an unchanged text is a plain approval
    assert [outcome for _, outcome in agent.execute_approved()] == ["simulated", "simulated"]
    first_sent, second_sent = agent.mailbox.sent  # type: ignore[union-attr]
    assert first_sent.get_content().startswith("Hallo Lena,\n\nnoch nicht.\n\n-- \n")
    assert (first_sent["To"], first_sent["Subject"]) == (READER, REPLY["subject"])  # only the text changed
    assert second_sent.get_content().startswith("Same text.\n\n-- \n")
    assert "noch nicht" in rows(agent, f"SELECT body FROM emails WHERE approval_id = {first}")[0]["body"]


def test_the_daily_limit_waits_for_tomorrow(data_dir: Path) -> None:
    settings = ROOMY.model_copy(update={"email_daily_limit": 1})
    third = {"to": "b@example.com", "subject": "Two", "body": "Two", "reason": "r"}
    agent, _ = make_agent(
        data_dir, proposing(REPLY, {**third, "to": "a@example.com", "subject": "One"}, third), settings
    )
    agent.run_cycle("schedule")
    for approval_id in ids(agent):
        approve(agent, approval_id)
    first, second, last = ids(agent)
    assert agent.execute_approved() == [(first, "simulated"), (second, "waiting_limit")]
    assert dashboard_row(agent, second)["execution"]["status"] == "waiting_limit"
    assert dashboard_row(agent, last)["execution"]["status"] == "waiting_limit"
    assert agent.sensor_fields()["email_waiting"] == 2 and agent.integrations()["email"]["sent_today"] == 1
    assert agent.execute_approved() == [(second, "waiting_limit")]  # still today: nothing more
    agent.clock.advance(days=1)  # type: ignore[attr-defined]
    assert agent.execute_approved() == [(second, "simulated"), (last, "waiting_limit")]
    agent.clock.advance(days=1)  # type: ignore[attr-defined]
    assert agent.execute_approved() == [(last, "simulated")]
    assert [m["To"] for m in agent.mailbox.sent] == [READER, "a@example.com", "b@example.com"]  # type: ignore[union-attr]


def test_a_limit_of_zero_sends_nothing(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, proposing(REPLY), ROOMY.model_copy(update={"email_daily_limit": 0}))
    agent.run_cycle("schedule")
    approve(agent, ids(agent)[0])
    assert agent.execute_approved() == [(ids(agent)[0], "waiting_limit")] and agent.mailbox.sent == []  # type: ignore[union-attr]


def test_a_stop_reply_cancels_the_waiting_email_and_blocks_new_ones(data_dir: Path) -> None:
    idle = [plan(steps=[], sleep=600) for _ in range(4)]
    again = {**REPLY, "reply_to_email_id": None, "to": READER}
    agent, _ = make_agent(data_dir, [*proposing(REPLY), *idle, *proposing(again)])
    agent.run_cycle("schedule")
    [approval_id] = ids(agent)
    approve(agent, approval_id)
    for _ in range(4):  # the reader's "stop" arrives in the fifth wake cycle, before the email went out
        agent.run_cycle("schedule")
    stop = rows(agent, "SELECT address, reason, email_id FROM email_suppressions")
    assert stop == [{"address": READER, "reason": 'replied "Stop"', "email_id": 3}]
    assert agent.execute_approved() == [(approval_id, "failed")] and agent.mailbox.sent == []  # type: ignore[union-attr]
    failed = approval(agent, approval_id)
    assert (failed["status"], failed["result_note"]) == ("failed", "Not sent: the recipient asked not to get emails")
    assert dashboard_row(agent, approval_id)["execution"]["error"] == "the recipient asked not to get emails"
    agent.run_cycle("schedule")
    refused = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'propose_email' ORDER BY id DESC")[0]
    assert refused["status"] == "error" and "asked not to get emails from you" in refused["result"]


class Crash(BaseException):
    """The process dies (a power cut, a kill -9) in the middle of sending."""


def test_a_send_interrupted_by_a_crash_is_unclear_and_never_repeated(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, proposing(REPLY))
    agent.run_cycle("schedule")
    [approval_id] = ids(agent)
    approve(agent, approval_id)

    def crash(message: EmailMessage, to: str) -> mail.SendResult:
        raise Crash

    agent.mailbox.send = crash  # type: ignore[union-attr, method-assign]
    with pytest.raises(Crash):
        agent.execute_approved()
    running = rows(agent, "SELECT status, message_id FROM email_actions")[0]
    assert running["status"] == "running" and running["message_id"]  # committed before the send
    assert owner(agent).close(approval_id, {"outcome": "failed"}, "Stefan").status == 409  # Ember reports it

    agent.recover()  # the next start
    action = rows(agent, "SELECT status, error, finished_at FROM email_actions")[0]
    assert (action["status"], action["error"]) == ("unclear", "the app stopped while sending")
    closed = approval(agent, approval_id)
    assert closed["status"] == "failed" and closed["closed_by"] == "Ember"
    assert closed["result_note"] == (
        "It is unclear whether it was sent (the app stopped while sending). Ember won't resend it; check the Sent"
        " folder at your mail provider."
    )
    sent: list[Any] = []
    agent.mailbox.send = lambda message, to: sent.append(message)  # type: ignore[union-attr, method-assign, assignment]
    assert agent.execute_approved() == [] and sent == []
    assert dashboard_row(agent, approval_id)["execution"]["status"] == "unclear"


def test_the_owner_can_cancel_a_waiting_email(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, proposing(REPLY), ROOMY.model_copy(update={"email_daily_limit": 0}))
    agent.run_cycle("schedule")
    [approval_id] = ids(agent)
    approve(agent, approval_id)
    who = owner(agent)
    refused = who.close(approval_id, {"outcome": "done"}, "Stefan")
    assert refused.status == 422 and refused.body["field"] == "outcome"
    cancelled = who.close(approval_id, {"outcome": "failed"}, "Stefan")
    assert cancelled.status == 200
    closed = cancelled.body["approval"]
    assert (closed["status"], closed["closed_by"]) == ("failed", "Stefan")
    assert closed["result_note"] == "Cancelled by the owner before it was sent"
    # A run that listed it before the cancel checks again in the transaction that would start the send.
    assert agent.executor._one(agent.scope(), approval_id) == "skipped"
    assert agent.execute_approved() == [] and rows(agent, "SELECT * FROM email_actions") == []
    assert dashboard_row(agent, approval_id)["execution"] is None


def test_nothing_is_sent_while_the_agent_can_not_run(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, proposing(REPLY))
    agent.run_cycle("schedule")
    approve(agent, ids(agent)[0])
    agent.economy.set_paused(True)
    assert agent.executor_blocked() == "The agent is paused" and agent.execute_approved() == []
    agent.economy.set_paused(False)
    agent.cycles_enabled = False
    assert agent.execute_approved() == []
    agent.cycles_enabled = True
    assert agent.execute_approved() == [(ids(agent)[0], "simulated")]


# --- the live path, against in-process fakes of smtplib ---


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL (port 465) and smtplib.SMTP (587); ``fail`` maps a stage to its error."""

    instances: list[FakeSMTP] = []
    fail: dict[str, BaseException] = {}

    def __init__(self, host: str, port: int, timeout: float | None = None, context: Any = None) -> None:
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[tuple[Any, ...]] = []
        self.sealed = netguard.is_sealed()
        FakeSMTP.instances.append(self)
        if "connect" in self.fail:
            raise self.fail["connect"]

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))

    def starttls(self, context: Any = None) -> None:
        self.calls.append(("starttls", context))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password == PASSWORD))
        if "login" in self.fail:
            raise self.fail["login"]

    def send_message(self, msg: EmailMessage, from_addr: str | None = None, to_addrs: Any = None) -> dict[str, Any]:
        self.calls.append(("send_message", msg, from_addr, to_addrs))
        if "send" in self.fail:
            raise self.fail["send"]
        return {}

    def quit(self) -> None:
        self.calls.append(("quit",))


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeSMTP]]:
    FakeSMTP.instances = []
    FakeSMTP.fail = {}
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    yield FakeSMTP
    FakeSMTP.fail = {}


def live_approved(data_dir: Path, settings: Settings = LIVE, **body: Any) -> tuple[Agent, int]:
    """A live agent whose reply to the first email in its (fake IMAP) mailbox is approved."""
    reply = {**REPLY, "subject": "Re: Hello?"}
    agent = live_agent(data_dir, proposing(reply), settings)
    assert agent.run_cycle("schedule").status == "completed"
    [approval_id] = ids(agent)
    approve(agent, approval_id, **body)
    return agent, approval_id


def verified(context: Any) -> bool:
    return (
        isinstance(context, ssl.SSLContext)
        and context.verify_mode == ssl.CERT_REQUIRED
        and context.check_hostname
        and context.minimum_version >= ssl.TLSVersion.TLSv1_2
    )


def test_a_live_send_goes_to_the_one_approved_recipient_over_verified_tls(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    agent, approval_id = live_approved(data_dir)
    assert agent.execute_approved() == [(approval_id, "sent")]
    [conn] = smtp.instances
    assert type(conn) is FakeSMTPSSL and (conn.host, conn.port, conn.timeout) == ("smtp.mail.example", 465, 20)
    assert verified(conn.context) and conn.sealed is False
    assert conn.calls[0] == ("login", "ember@mail.example", True)
    _, message, from_addr, to_addrs = conn.calls[1]
    assert (from_addr, to_addrs) == ("ember@mail.example", ["ann@example.org"])  # the envelope: never from headers
    assert conn.calls[2] == ("quit",)
    assert message.get_all("To") == ["ann@example.org"] and message["Cc"] is None and message["Bcc"] is None
    assert str(message["From"]) == '"Ember (AI agent of Stefan)" <ember@mail.example>'
    assert message["In-Reply-To"] == "<m1@example.org>" and message["Message-ID"].endswith("@mail.example>")
    assert message.get_content() == f"{REPLY['body']}\n\n{FOOTER.format(owner='Stefan')}\n"
    assert message["Content-Transfer-Encoding"] == "quoted-printable" and not message.is_multipart()
    done = approval(agent, approval_id)
    assert done["status"] == "done" and done["result_note"] == f"Sent 2026-09-01 12:00 UTC as {message['Message-ID']}"
    assert rows(agent, "SELECT status FROM email_actions") == [{"status": "sent"}]
    assert {c.host for c in imap.instances} == {"imap.mail.example"}  # only the configured servers
    assert agent.execute_approved() == [] and len(smtp.instances) == 1  # never sent twice
    shown = json.dumps(agent.dashboard()) + json.dumps(agent.integrations()) + json.dumps(agent.db.recent_events())
    assert PASSWORD not in shown


def test_starttls_on_port_587(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    agent, approval_id = live_approved(data_dir, LIVE.model_copy(update={"email_smtp_port": 587}))
    assert agent.execute_approved() == [(approval_id, "sent")]
    [conn] = smtp.instances
    assert type(conn) is FakeSMTP and conn.port == 587 and conn.context is None
    assert [c[0] for c in conn.calls] == ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert verified(conn.calls[1][1])


@pytest.mark.parametrize(
    ("stage", "error", "status"),
    [
        ("connect", ConnectionRefusedError("connection refused"), "failed"),
        ("connect", ssl.SSLCertVerificationError("certificate verify failed"), "failed"),
        ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed"), "failed"),
        ("send", smtplib.SMTPRecipientsRefused({"ann@example.org": (550, b"5.1.1 unknown user")}), "failed"),
        ("send", smtplib.SMTPSenderRefused(553, b"5.7.1 not yours", "ember@mail.example"), "failed"),
        ("send", smtplib.SMTPDataError(554, b"5.7.1 rejected after DATA"), "unclear"),
        ("send", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), "unclear"),
        ("send", TimeoutError("timed out"), "unclear"),
    ],
)
def test_a_failed_send_is_never_retried(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP], stage: str, error: BaseException, status: str
) -> None:
    agent, approval_id = live_approved(data_dir)
    smtp.fail = {stage: error}
    assert agent.execute_approved() == [(approval_id, status)]
    closed = approval(agent, approval_id)
    assert closed["status"] == "failed" and closed["closed_by"] == "Ember"
    if status == "failed":
        assert closed["result_note"].startswith("Not sent: ")
    else:
        assert "It is unclear whether it was sent" in closed["result_note"]
        assert "Ember won't resend it; check the Sent folder at your mail provider." in closed["result_note"]
    action = rows(agent, "SELECT status, error FROM email_actions")[0]
    assert action["status"] == status and action["error"] and PASSWORD not in action["error"]
    assert dashboard_row(agent, approval_id)["execution"]["status"] == status
    smtp.fail = {}
    assert agent.execute_approved() == [] and len(smtp.instances) == 1  # never tried again
    assert rows(agent, "SELECT COUNT(*) AS n FROM emails WHERE direction = 'out'") == [{"n": 0}]


def test_an_action_that_no_longer_checks_out_is_not_sent(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    agent, approval_id = live_approved(data_dir)
    with agent.db.transaction() as conn:  # e.g. written by an older version: the executor checks it again
        conn.execute("DROP TRIGGER approvals_action_fixed")
        conn.execute("UPDATE approvals SET action = ? WHERE id = ?", ('{"to": "a@b.org, c@d.org"}', approval_id))
    assert agent.execute_approved() == [(approval_id, "failed")] and smtp.instances == []
    assert "can't be sent" in approval(agent, approval_id)["result_note"]


# --- what the owner sees ---

EMAIL_KEYS = {
    "available",
    "mode",
    "status",
    "reason",
    "address",
    "last_fetch_at",
    "last_error",
    "unread",
    "sent_today",
    "daily_limit",
    "suppressed_count",  # 0.12.0: the addresses Ember never emails
    "suppressed",
}
CARRIED_OUT = {"executor", "action", "first_contact", "execution", "reddit_url"}


def test_the_dashboard_and_sensor_contract(ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    state = ingress_client.app.state.ember
    agent = state.agent
    reddit_post = {"subreddit": "SideProject", "kind": "post", "title": "Planner?", "body": "Would you use it?",
                   "reason": "A test."}  # fmt: skip
    cold = {"to": "shop@example.com", "subject": "Hi", "body": "Hello", "reason": "r"}
    agent.transport = ScriptedTransport(
        simulated=True,
        outcomes=[
            plan(steps=["mail"]),
            calls(("propose_email", REPLY), ("propose_email", cold), ("propose_reddit_post", reddit_post)),
            text("Done."),
            JOURNAL,
        ],
    )
    agent.meter = agent.economy.metered(agent.transport)
    assert agent.run_cycle("owner").status == "completed"
    pokes: list[bool] = []
    monkeypatch.setattr(state.scheduler, "poke", lambda: pokes.append(True))

    data = ingress_client.get("api/dashboard").json()
    email = data["integrations"]["email"]
    assert set(data["integrations"]) == {"email", "etsy", "pinterest", "printify", "site", "blog"}
    assert set(email) == EMAIL_KEYS
    assert data["integrations"]["site"] == {"status": "disabled"}  # 0.13.0: the website is off by default
    assert (email["available"], email["mode"], email["status"]) == (True, "fake", "ok")
    assert (email["address"], email["unread"], email["sent_today"], email["daily_limit"]) == (
        "ember@example.invalid",
        1,
        0,
        3,
    )
    assert email["last_fetch_at"] and email["last_error"] is None
    answer, first, posted = sorted(data["approvals"], key=lambda a: a["id"])
    for row in (answer, first, posted):
        assert set(row) >= CARRIED_OUT
    assert answer["executor"] == "email" and answer["action"]["to"] == READER and answer["first_contact"] is False
    assert first["first_contact"] is True and first["action"]["subject"] == "Hi"
    assert answer["execution"] is None and answer["reddit_url"] is None
    assert posted["executor"] == "reddit_link" and posted["action"]["kind"] == "post" and posted["execution"] is None
    assert posted["reddit_url"].startswith("https://www.reddit.com/r/SideProject/submit?title=Planner%3F&text=")

    assert post(ingress_client, f"api/approvals/{answer['id']}/decide", {"decision": "approve"}).status_code == 200
    assert pokes == [True]  # the scheduler sends it right away (here cycles are off, so it waits)
    changed = {"decision": "approve_with_changes", "final_payload": "Would you use it? Be honest."}
    assert post(ingress_client, f"api/approvals/{posted['id']}/decide", changed).status_code == 200
    data = ingress_client.get("api/dashboard").json()
    answer, _, posted = sorted(data["approvals"], key=lambda a: a["id"])
    assert answer["execution"] == {
        "status": "waiting",
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    assert posted["reddit_url"].endswith("&text=Would%20you%20use%20it%3F%20Be%20honest.")  # the owner's text
    sensors = ingress_client.get("api/sensors").json()
    assert (sensors["email_unread"], sensors["email_waiting"]) == (1, 1)


def test_the_integration_status_is_shown_without_secrets(
    client_factory: Callable, write_options: Callable[[dict], Path]
) -> None:
    write_options({**MAIL_OPTIONS, "email_password": PASSWORD})  # dry run: the fake mailbox, the password unused
    with client_factory() as client:
        data = client.get("api/dashboard").json()
        report = client.get("api/diagnostics").text
        events = client.get("api/events").text
    assert data["system"]["options"]["email_password_set"] is True
    assert data["integrations"]["email"]["mode"] == "fake"
    integrations = report.split("\n## INTEGRATIONS", 1)[1].split("\n## ", 1)[0]
    assert '"status": "ok"' in integrations and '"mode": "fake"' in integrations and "-- sends" in integrations
    assert '"email_password_set": true' in report and '"email_sending_blocked": "Wake cycles are switched off' in report
    for text_ in (json.dumps(data), report, events):
        assert PASSWORD not in text_


def test_an_incomplete_live_mailbox_is_shown_as_not_configured(
    client_factory: Callable, write_options: Callable[[dict], Path]
) -> None:
    key = "sk-ant-api03-testkey-0123456789"
    write_options({"dry_run": False, "anthropic_api_key": key, "email_enabled": True, "email_address": "ember@x.org"})
    with client_factory() as client:
        data = client.get("api/dashboard").json()
        sensors = client.get("api/sensors").json()
    assert data["system"]["safe_mode"] is False
    email = data["integrations"]["email"]
    assert (email["available"], email["status"]) == (False, "not_configured")
    assert "email_password is empty" in email["reason"] and "email_owner_name is empty" in email["reason"]
    assert (sensors["email_unread"], sensors["email_waiting"]) == (0, 0)


def test_the_executor_module_sends_only_through_a_mailbox() -> None:
    source = Path(executor.__file__).read_text(encoding="utf-8")
    assert "smtplib" not in source and "imaplib" not in source and "socket" not in source
    assert LoadedSettings(Settings()).settings.email_daily_limit == 3


def test_approved_emails_are_not_the_owners_to_do(data_dir: Path) -> None:
    # Ember sends them itself, so the "approved, to carry out" badge doesn't count them.
    agent, _ = make_agent(data_dir, proposing(REPLY), ROOMY.model_copy(update={"email_daily_limit": 0}))
    agent.run_cycle("schedule")
    [approval_id] = ids(agent)
    assert agent.dashboard()["badges"]["approvals_pending"] == 1
    approve(agent, approval_id)
    badges = agent.dashboard()["badges"]
    assert (badges["approvals_pending"], badges["approvals_todo"]) == (0, 0)
