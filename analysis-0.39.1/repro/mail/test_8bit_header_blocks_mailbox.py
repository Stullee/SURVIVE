"""Repro: one email whose From: display name holds raw 8-bit bytes makes mailstore.fetch raise forever.

Effects: the mailbox is never read past it (stops after it are never seen), and while an approved email waits,
Agent.execute_approved() raises before the Etsy publisher / pins / Bluesky / Printify / blog executors run."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import pytest
from tests.conftest import data_dir, ventures_in_the_plan, helper_databases  # noqa: F401 fixtures
from tests.test_mail import imap, raw_mail, FakeIMAP  # noqa: F401
from tests.test_executor import smtp, FakeSMTP, live_approved, ids  # noqa: F401
from tests.test_agent import rows
from app.integrations import mailstore

HOSTILE = (b"From: M\xc3\xbcller <mueller@example.net>\r\nTo: ember@mail.example\r\nSubject: Frage\r\n"
           b"Message-ID: <h1@example.net>\r\nDate: Mon, 28 Sep 2026 09:00:00 +0200\r\n\r\nHallo!\r\n")
STOP = raw_mail(4, sender="Bob <bob@example.org>", subject="Re: offer", body="Stop")


def test_one_8bit_display_name_blocks_the_mailbox_and_all_executors(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    agent, approval_id = live_approved(data_dir)          # owner approved a reply; mailbox read once (uid 1,2)
    agent.clock.advance(minutes=10)                       # the round runs later than the approval
    imap.mails[3] = HOSTILE                               # then a hostile mail arrives ...
    imap.mails[4] = STOP                                  # ... and after it someone's "stop"
    called = []
    orig = agent.publisher.run
    agent.publisher.run = lambda *a, **k: called.append("etsy publisher") or orig(*a, **k)  # type: ignore
    for attempt in range(3):
        try:
            out = agent.execute_approved()
            print("execute_approved ->", out)
        except Exception as exc:  # noqa: BLE001
            print(f"attempt {attempt}: execute_approved raised {type(exc).__name__}: {exc}")
    agent.check_events()  # the between-cycles read swallows the exception and logs it
    print("last_uid:", agent.db.get_meta(mailstore.meta_key("live", "last_uid")))
    print("stored froms:", [r["from_addr"] for r in rows(agent, "SELECT from_addr FROM emails WHERE direction='in' ORDER BY id")])
    print("suppressions:", rows(agent, "SELECT address FROM email_suppressions"))
    print("etsy publisher ran:", called)
    row = next(a for a in agent.dashboard()["approvals"] if a["id"] == approval_id)
    print("card execution:", row["execution"], "| mailbox last_error:", mailstore.last_fetch(agent.db, "live"))
    print("smtp sends:", len(smtp.instances), "approval status:", rows(agent, f"SELECT status FROM approvals WHERE id={approval_id}"))


def test_a_stop_before_the_hostile_mail_in_the_same_batch_is_rolled_back(data_dir: Path, imap: type[FakeIMAP], smtp: type[FakeSMTP]) -> None:
    agent, approval_id = live_approved(data_dir)
    agent.clock.advance(minutes=10)
    imap.mails[3] = STOP      # the stop first ...
    imap.mails[4] = HOSTILE   # ... then the hostile mail, fetched in the same batch
    agent.check_events()      # the between-cycles read (exception logged and swallowed)
    print("[same batch] stored uids:", [r["uid"] for r in rows(agent, "SELECT uid FROM emails WHERE direction='in' ORDER BY id")],
          "| suppressions:", rows(agent, "SELECT address FROM email_suppressions"))
