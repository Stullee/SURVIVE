"""0.37.1: three timing defects at the approval boundary (analysis-0.37.0, 4.1.1 to 4.1.3; its reproductions are
repro/outside/r6_stop_before_send.py, r1_kill_mid_round.py and r2_expired_veto.py).

- A "stop" in Ember's mailbox was read after the first round's sends: nothing read the mailbox while the kill switch
  was on, the app was down or reading failed, and the executor checked only the opt-outs it had stored.
- The kill switch was checked once, before a round: 2 of 3 approved emails went out after the owner pressed it, and
  the live page went on uploading every 15 minutes.
- Requests expired only at a cycle's start, after the round's unlocks: on Resume after 8 days paused, an unlock
  approved an 8-day-old reply, and it was sent.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.agent import agenda, policy, store
from app.agent.owner import apply_kill_switch_reset, kill
from app.agent.service import Agent
from app.economy.clock import to_iso
from app.integrations import connectors, executor, mail, mailstore
from app.products import live
from tests.test_agent import rows
from tests.test_agent import tools as calls
from tests.test_etsy import a_change, listed, shop_context
from tests.test_executor import REPLY, FakeSMTP, approval, approve, dashboard_row, ids, live_approved, proposing
from tests.test_executor import smtp as smtp  # noqa: F401 - the fixture
from tests.test_fixes_0140_unlock_safety import answer, status_of, unlock
from tests.test_live_view import agent_with
from tests.test_mail import PASSWORD, FakeIMAP, live_agent, mail_cycle
from tests.test_mail import imap as imap  # noqa: F401 - the fixture
from tests.test_owner_loop import owner
from tests.test_policy import a_milestone

ANN = "ann@example.org"
VERIFIED = "mx1.mail.example; dkim=pass header.d=example.org; dmarc=pass header.from=example.org"


def from_ann(uid: int, subject: str, body: str, reply_to: str | None = None) -> bytes:
    """An email of Ann's, as Ember's mail provider hands it over."""
    msg = EmailMessage()
    msg["Authentication-Results"] = VERIFIED
    msg["From"] = f"Ann <{ANN}>"
    msg["To"] = "ember@mail.example"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 28 Sep 2026 08:00:00 +0200"
    msg["Message-ID"] = f"<m{uid}@example.org>"
    if reply_to:
        msg["In-Reply-To"] = reply_to
    msg.set_content(body)
    return msg.as_bytes()


def press_kill(agent: Agent) -> None:
    name = agent.settings.agent_name
    assert kill(agent.db, agent.economy, name, {"confirm_name": name}, "Stefan").status == 200


def scheduler_round(agent: Agent) -> None:
    """What the scheduler does every round before it decides on a cycle (agent/scheduler.py)."""
    agent.run_policy()
    agent.execute_approved()
    agent.sync_shop()
    agent.publish_live()
    agent.check_events()


def an_email(agent: Agent, to: str, n: int) -> int:
    """An email to ``to`` that the owner approved; its request's number."""
    cycle_id = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    action = mail.email_action(to, f"Hello {n}", f"Text {n}")
    with agent.db.transaction() as conn:
        made = store.insert_approval(
            conn,
            agent.scope(),
            cycle_id,
            to_iso(agent.clock.now()),
            type="contact",
            title=f"Email {n}",
            description="d",
            payload=f"To: {to}\n\nText {n}",
            expected_cost="none",
            expected_benefit="b",
            executor="email",
            action=store.canonical(action),
        )
    approve(agent, made)
    return made


def events(agent: Agent) -> list[str]:
    return [r["message"] for r in rows(agent, "SELECT message FROM events ORDER BY id")]


# --- 4.1.1: a "stop" waiting in the mailbox is read before anything is sent ---


def test_a_stop_that_came_while_the_kill_switch_was_on_is_read_before_anything_is_sent(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]
) -> None:
    imap.mails = {1: from_ann(1, "Hello?", "Do you have the planner in German?")}
    agent = live_agent(data_dir, proposing({**REPLY, "subject": "Re: Hello?"}))
    assert agent.run_cycle("schedule").status == "completed"
    [request] = ids(agent)
    approve(agent, request)
    apply_kill_switch_reset(agent.db, agent.economy, 0)  # the option's value at the start
    press_kill(agent)
    imap.mails[2] = from_ann(2, "Re: Hello?", "Stop. Please don't email me.", "<m1@example.org>")
    for _ in range(3):  # a day of rounds while the switch is on: nothing reads the mailbox
        scheduler_round(agent)
        agent.clock.advance(hours=8)  # type: ignore[attr-defined]
    assert len(imap.instances) == 1 and rows(agent, "SELECT address FROM email_suppressions") == []
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    agent.run_policy()  # the first round after the reset, in the scheduler's order
    assert agent.execute_approved() == [(request, "failed")]  # before: sent, and her stop read later in the round
    assert smtp.instances == [] and len(imap.instances) == 2  # read first
    assert rows(agent, "SELECT address FROM email_suppressions") == [{"address": ANN}]
    assert approval(agent, request)["result_note"] == f"Not sent: {executor.SUPPRESSED}"


def test_a_send_rests_on_a_read_since_its_approval(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    imap.mails = {1: from_ann(1, "Hello?", "Do you have the planner in German?")}
    later = {"to": "bob@example.org", "subject": "Planner", "body": "Hi Bob.", "reason": "r"}
    agent = live_agent(data_dir, proposing({**REPLY, "subject": "Re: Hello?"}, later))
    assert agent.run_cycle("schedule").status == "completed"
    first, second = ids(agent)
    approve(agent, first)  # in the minute the cycle read the mailbox
    reads = len(imap.instances)
    assert agent.execute_approved() == [(first, "sent")] and len(imap.instances) == reads  # fresh: not read again
    agent.clock.advance(minutes=2)  # type: ignore[attr-defined]
    approve(agent, second)  # after the last read: a stop may have come since
    assert agent.execute_approved() == [(second, "sent")] and len(imap.instances) == reads + 1
    agent.clock.advance(minutes=executor.FRESH_MINUTES + 1)  # type: ignore[attr-defined]
    assert agent.execute_approved() == [] and len(imap.instances) == reads + 1  # nothing to send: nothing read


def test_approved_emails_wait_while_the_mailbox_can_not_be_read(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = live_approved(data_dir)
    agent.clock.advance(minutes=executor.FRESH_MINUTES + 1)  # type: ignore[attr-defined]
    monkeypatch.setattr(imap, "password", "changed at the provider")  # the login is refused
    waits = [(request, executor.WAITING_MAIL)]
    assert agent.execute_approved() == waits
    assert smtp.instances == [] and rows(agent, "SELECT * FROM email_actions") == []
    shown = dashboard_row(agent, request)["execution"]
    assert shown["status"] == "waiting" and shown["result"].startswith("Ember's mailbox can't be read (")
    assert shown["result"].endswith(
        "a reply asking not to be emailed may wait there: it goes out once the mailbox is read"
    )
    agent.clock.advance(minutes=1)  # type: ignore[attr-defined]
    assert agent.execute_approved() == waits  # read again, failed again
    reads = len(imap.instances)
    agent.clock.advance(minutes=1)  # type: ignore[attr-defined]
    assert agent.execute_approved() == waits and len(imap.instances) == reads  # a failing mailbox's wait (0.15.0)
    monkeypatch.setattr(imap, "password", PASSWORD)
    agent.clock.advance(minutes=mailstore.wait_minutes(agent.db, agent.mode, agenda.MAIL_MINUTES))  # type: ignore[attr-defined]
    assert agent.execute_approved() == [(request, "sent")] and len(smtp.instances) == 1
    assert dashboard_row(agent, request)["execution"]["status"] == "sent"


def test_a_stop_at_the_end_of_a_backlog_is_read_before_the_send(
    data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mailstore, "MAX_FETCH", 1)  # one email a read, so the mailbox takes three
    monkeypatch.setattr(mailstore, "FETCH_ROUNDS", 1)
    imap.mails = {
        1: from_ann(1, "Hello?", "Do you have the planner in German?"),
        2: from_ann(2, "Also", "And in A5?"),
        3: from_ann(3, "Re: Hello?", "Stop. Please don't email me.", "<m1@example.org>"),
    }
    agent = live_agent(data_dir, proposing({**REPLY, "subject": "Re: Hello?"}))
    assert agent.run_cycle("schedule").status == "completed"  # it read email 1, two wait
    [request] = ids(agent)
    approve(agent, request)
    assert agent.execute_approved() == [(request, executor.WAITING_MAIL)]  # read email 2: one still waits
    assert mailstore.read_at(agent.db, agent.mode) is None and smtp.instances == []
    assert agent.execute_approved() == [(request, "failed")]  # read through: her stop
    assert smtp.instances == [] and rows(agent, "SELECT address FROM email_suppressions") == [{"address": ANN}]


# --- 4.1.2: the kill switch stops a round that is already sending ---


def test_the_kill_switch_stops_a_round_that_is_already_sending(data_dir: Path) -> None:
    agent, _ = mail_cycle(data_dir, calls(("email_inbox", {})))
    first, second, third = (an_email(agent, f"reader{n}@example.org", n) for n in (1, 2, 3))
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    assert agent.mailbox is not None
    original = agent.mailbox.send
    pressed: list[bool] = []

    def send(message: EmailMessage, to: str) -> mail.SendResult:
        if not pressed:  # the owner presses the switch while the first email is handed over
            press_kill(agent)
            pressed.append(True)
        return original(message, to)

    agent.mailbox.send = send  # type: ignore[method-assign]
    assert agent.execute_approved() == [(first, "simulated"), (second, connectors.HALTED)]
    assert len(agent.mailbox.sent) == 1  # type: ignore[attr-defined] # before: all three, two after the kill
    assert rows(agent, "SELECT approval_id FROM email_actions") == [{"approval_id": first}]
    assert [approval(agent, n)["status"] for n in (second, third)] == ["approved", "approved"]  # they wait
    assert dashboard_row(agent, second)["execution"]["status"] == "waiting"
    assert agent.execute_approved() == []  # the next rounds: nothing while the switch is on
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(second, "simulated"), (third, "simulated")]  # the owner approved them


def test_the_kill_switch_pressed_while_an_email_goes_out_stops_the_publishers_of_that_round(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    change = a_change(agent, shop_context(agent), listing_id, price="3.90")
    assert owner(agent).decide(change, {"decision": "approve"}, "Stefan").status == 200
    email = an_email(agent, "reader@example.org", 1)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    assert agent.mailbox is not None
    original = agent.mailbox.send

    def send(message: EmailMessage, to: str) -> mail.SendResult:
        press_kill(agent)
        return original(message, to)

    agent.mailbox.send = send  # type: ignore[method-assign]
    assert agent.execute_approved() == [(email, "simulated"), (change, connectors.HALTED)]
    assert agent.etsy.shop().state["listings"][str(listing_id)]["price_cents"] == 450  # type: ignore[union-attr]
    assert rows(agent, "SELECT * FROM etsy_edits") == []  # not begun: nothing to call unclear
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(change, "done")]
    assert agent.etsy.shop().state["listings"][str(listing_id)]["price_cents"] == 390  # type: ignore[union-attr]


def test_the_live_page_is_not_uploaded_while_the_kill_switch_is_on(data_dir: Path) -> None:
    agent = agent_with(data_dir)
    assert agent.publish_live() == "done"
    fake = agent.blog.fake
    assert fake is not None
    before = dict(fake.files)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    press_kill(agent)
    agent.clock.advance(minutes=live.UPLOAD_MINUTES)  # type: ignore[attr-defined]
    assert agent.publish_live() is None and fake.files == before  # before: "Angehalten", and every 15 minutes again
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.publish_live() == "done"


# --- 4.1.3: a request past its days expires before an unlock can approve it ---


def held_reply(data_dir: Path) -> tuple[Agent, int]:
    """A dry-run agent whose reply to the reader is held for its veto window by the owner's unlock."""
    agent, transport = mail_cycle(data_dir, calls(("email_inbox", {})))
    goal = a_milestone(agent)  # of no project: it covers email replies
    unlock(agent, goal, "email_reply", "veto_window")
    made = answer(agent, transport, goal)
    assert made["status"] == "pending" and dashboard_row(agent, made["id"])["veto_until"]
    return agent, int(made["id"])


def test_a_reply_held_through_a_long_pause_expires_instead_of_going_out(data_dir: Path) -> None:
    agent, request = held_reply(data_dir)
    agent.economy.set_paused(True, "Stefan")
    agent.clock.advance(days=8)  # type: ignore[attr-defined]
    agent.run_policy()  # a round while paused: the unlocks don't act
    agent.economy.set_paused(False, "Stefan")
    agent.run_policy()  # the first round after Resume
    assert status_of(agent, request)["status"] == "expired"  # before: approved by the unlock, and sent
    assert agent.execute_approved() == [] and agent.mailbox.sent == []  # type: ignore[union-attr]
    assert f"Request #{request} expired: no decision in 7 days" in events(agent)


def test_a_request_past_its_days_is_never_approved_by_an_unlock(data_dir: Path) -> None:
    agent, request = held_reply(data_dir)
    agent.clock.advance(days=8)  # type: ignore[attr-defined] # the app was down: no round ran
    with agent.db.transaction() as conn:
        assert policy.run_due(conn, agent.scope(), agent.clock) == []
    assert status_of(agent, request)["status"] == "pending"
    agent.run_policy()  # expired first
    assert status_of(agent, request)["status"] == "expired" and agent.execute_approved() == []


def test_a_veto_window_that_ran_out_during_a_pause_starts_again_when_ember_runs(data_dir: Path) -> None:
    agent, request = held_reply(data_dir)
    agent.economy.set_paused(True, "Stefan")
    agent.run_policy()  # a round while paused: the unlocks don't act
    agent.clock.advance(hours=policy.VETO_HOURS + 1)  # type: ignore[attr-defined] # its window runs out meanwhile
    agent.economy.set_paused(False, "Stefan")
    agent.run_policy()
    assert status_of(agent, request)["status"] == "pending"  # before: approved in the first round after Resume
    until = to_iso(agent.clock.now() + timedelta(hours=policy.VETO_HOURS))
    assert dashboard_row(agent, request)["veto_until"] == until
    said = f"Request #{request}: its veto window starts again now that Ember runs again (approved 12 hours from now"
    assert any(line.startswith(said) for line in events(agent))
    with pytest.raises(sqlite3.IntegrityError, match="only ever ends later"), agent.db.transaction() as conn:
        conn.execute(
            "UPDATE policy_uses SET veto_until = ? WHERE approval_id = ?", (to_iso(agent.clock.now()), request)
        )
    agent.run_policy()  # an ordinary round: nothing starts again
    assert dashboard_row(agent, request)["veto_until"] == until
    agent.clock.advance(hours=policy.VETO_HOURS)  # type: ignore[attr-defined]
    agent.run_policy()
    assert status_of(agent, request)["decided_by"] == policy.POLICY_BY
    assert agent.execute_approved() == [(request, "simulated")]
