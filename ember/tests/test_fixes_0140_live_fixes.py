"""0.14.0: faults the owner's live diagnostics of 2026-10-01 showed: a site address that named its home page's file, a
workshop run "ok" with only its script back, a PRINTIFY line about a request that didn't exist, a 503 booked at the
worst case, a sleep cut that outlived the request it waited for, length refusals without numbers, a one-time-code mask
across table cells, upgrade requests the agent couldn't see, a kept promise the message cap blocked, a folder read as
a file and an opt-out "reply" to mail Ember never sent."""

from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from app import privacy, web
from app.agent import context, tools, website, workshop
from app.agent.fake_llm import FakeTransport
from app.config import Settings
from app.economy.clock import from_iso
from app.economy.metering import Interrupted, Rejected
from app.integrations import mail, mailstore, printify_publisher
from app.products import site
from tests.test_agent import make_agent, rows
from tests.test_anthropic_transport import BLOCK, REQUEST, START, Server, error, sse, stream, transport
from tests.test_autonomy import request_for
from tests.test_decision_wakes import asks_and_sleeps
from tests.test_loop_shapes import run
from tests.test_money_goal import call
from tests.test_owner_loop import owner
from tests.test_printify import proposed
from tests.test_roadmap import day
from tests.test_roadmap import plan as idle
from tests.test_site import OWNER, WHO, home

# --- LIVE 1: the site's address is a folder -------------------------------------------------------------------------


def test_a_home_page_file_in_site_url_is_taken_off_the_address() -> None:
    settings = Settings(**OWNER, site_url="https://ember-ai.de/index.html")
    who = website.owner(settings)
    assert who.url == "https://ember-ai.de" == website.address(settings)
    assert website.address(Settings(site_url="https://example.org/shop/index.htm")) == "https://example.org/shop"
    assert website.address(Settings(site_url="https://example.org/INDEX.HTML")) == "https://example.org"
    files = site.build([home(), site.check("bewerbungs-tracker", "Tracker", "A tracker.", "Text.\n")], who)
    assert (
        b'<link rel="canonical" href="https://ember-ai.de/bewerbungs-tracker.html">' in files["bewerbungs-tracker.html"]
    )
    assert b"<loc>https://ember-ai.de/</loc>" in files["sitemap.xml"]
    assert b"index.html/" not in files["sitemap.xml"] + files["robots.txt"] + files["index.html"]


@pytest.mark.parametrize("url", ["https://example.org/page.html", "https://example.org/shop/start.php"])
def test_a_site_url_naming_another_file_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="site_url must be an https address without a query or a file name"):
        Settings(site_url=url)
    for fine in ("https://example.org", "https://example.org/", "https://example.org/v1.2/shop/"):
        assert Settings(site_url=fine).site_url == fine
    assert WHO.url == ""


# --- LIVE 2: a run that brought back only its script ----------------------------------------------------------------


def test_a_workshop_run_with_only_its_script_back_is_not_ok() -> None:
    script = "workshop/scripts/render-a-print-ready-7.py"
    run_ = workshop.Run(
        task="Render a poster",
        script_used=None,
        inputs=[],
        kept=[(script, 4_096)],
        script_path=script,
        answer="Saved shop/posters/bauhaus_geometric_no1.png (4961 x 7016) and a preview.png.",
        cost=893_929,
    )
    ok, text, summary = workshop.report(run_, lambda source, body: body)
    assert not ok and workshop.made(run_) == []
    assert "Only the script came back (bauhaus_geometric_no1.png, preview.png didn't)" in text
    assert "every file must be saved into $OUTPUT_DIR" in text and summary.endswith("only its script")
    run_.kept.append(("workshop/out/poster.png", 150_000))
    ok, text, _ = workshop.report(run_, lambda source, body: body)
    assert ok and "Only the script" not in text


def test_a_refused_file_is_not_reported_as_lost() -> None:
    script = "workshop/scripts/x-1.py"
    run_ = workshop.Run(
        task="Render a poster",
        script_used=None,
        inputs=["workshop/in/logo.png"],
        kept=[(script, 900)],
        refused=[("poster.png", "larger than 40 MP")],
        script_path=script,
        answer="Used logo.png. Saved poster.png to $OUTPUT_DIR/poster.png",
        cost=890_000,
    )
    ok, text, _ = workshop.report(run_, lambda source, body: body)
    assert not ok and "Not kept: poster.png: larger than 40 MP." in text and "Only the script" not in text
    run_.refused.clear()
    run_.answer += " and $OUTPUT_DIR/thumb.png, thumb.png"
    _, text, _ = workshop.report(run_, lambda source, body: body)
    assert "Only the script came back (poster.png, thumb.png didn't)" in text  # names, once each; no input


def test_the_task_says_files_go_into_the_output_folder() -> None:
    prompt = workshop.Workshop._prompt("Save as shop/posters/a.png", [], None)
    assert "save it by its file name at the top of $OUTPUT_DIR (e.g. $OUTPUT_DIR/x.png), or it is lost." in prompt


# --- LIVE 3: PRINTIFY counts the proposals that wait ----------------------------------------------------------------


def test_printify_says_whether_a_proposal_waits(data_dir: Path) -> None:
    agent, _, _ = proposed(data_dir)
    with agent.db.connection() as conn:
        assert printify_publisher.text(conn, agent.scope()) == (
            "No product of yours yet: 1 proposal waits for your owner's decision."
        )
    with agent.db.transaction() as conn:
        conn.execute("UPDATE approvals SET status = 'withdrawn' WHERE executor = 'printify_product'")
    with agent.db.connection() as conn:
        assert printify_publisher.text(conn, agent.scope()) == (
            "No product of yours yet, and none waiting (1 rejected, withdrawn or expired)."
        )
    fresh, _ = make_agent(data_dir / "fresh", [])
    with fresh.db.connection() as conn:
        assert printify_publisher.text(conn, fresh.scope()) == "No product of yours yet, and none proposed yet."


def test_printify_says_an_approved_product_is_not_created_yet(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200  # not executed yet
    with agent.db.connection() as conn:
        assert printify_publisher.text(conn, agent.scope()) == "No product of yours yet: 1 approved, not created yet."


# --- LIVE 4: a server error before the stream opened costs nothing --------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_before_the_stream_is_rejected(status: int) -> None:
    outcome = transport(Server(error(status, "api_error"))).send(REQUEST)
    assert isinstance(outcome, Rejected) and outcome.status == status  # the guard books nothing
    overloaded = {"type": "error", "error": {"type": "api_error", "message": "Internal error"}}
    late = transport(Server(stream(sse(START, *BLOCK[:2], overloaded)))).send(REQUEST)
    assert isinstance(late, Interrupted)  # after the stream opened: the cost is unknown


# --- LIVE 5: the sleep the agent chose stands once nothing waits ----------------------------------------------------


def test_the_owners_last_decision_lifts_the_sleep_cut(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, asks_and_sleeps(720))
    assert agent.run_cycle("schedule").status == "completed"
    ended = agent.clock.now()
    fields = agent.agent_fields()
    assert "cut to 240 min" in fields["next_wake_reason"]
    assert any("chose 720 min of sleep" in e["message"] for e in agent.db.recent_events(10))
    agent.clock.advance(minutes=30)
    off, _ = request_for(agent, Settings(wake_on_decision=False))
    decided = owner(agent).decide(1, {"decision": "reject", "comment": "No.", "expected_version": 0}, None)
    web._wake_for_decision(off, decided)
    fields = agent.agent_fields()
    assert from_iso(fields["next_wake_at"]) - ended == timedelta(minutes=720)
    assert fields["next_wake_reason"] == "Ember chose 720 min"


def test_the_cut_stays_while_a_request_waits_or_the_wake_changed(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, asks_and_sleeps(720))
    assert agent.run_cycle("schedule").status == "completed"
    cut = agent.agent_fields()["next_wake_at"]
    agent.lift_sleep_cut()  # the request still waits
    assert agent.agent_fields()["next_wake_at"] == cut
    agent._set_time("next_wake_at", agent.clock.now() + timedelta(minutes=5))  # something else set the wake
    off, _ = request_for(agent, Settings(wake_on_decision=False))
    web._wake_for_decision(off, owner(agent).decide(1, {"decision": "approve", "expected_version": 0}, None))
    assert from_iso(agent.agent_fields()["next_wake_at"]) - agent.clock.now() == timedelta(minutes=5)


# --- LIVE 6: length refusals with the numbers, notes cut --------------------------------------------------------------


def test_a_too_long_field_is_refused_with_its_length_and_what_to_do(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[idle(steps=[])]))
    long = call(agent, "workspace_write", path="drafts/article.md", mode="create", content="ä" * 2_742)
    assert not long.ok
    assert "content is too long: 2,742 of 2,500 characters: create with the first part, then append the rest" in (
        long.text
    )
    path = call(agent, "workspace_read", path="p" * 230 + ".md")
    assert not path.ok and "path is too long: 233 of 200 characters." in path.text  # no remedy: none needed


def test_owner_facing_notes_are_cut_not_refused(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[idle(steps=[])]))
    filed = call(
        agent,
        "request_upgrade",
        title="Read big PNGs",
        problem="p" * 742,
        proposed_change="c" * 650,
        expected_benefit="Unblocks the poster venture.",
        priority="high",
    )
    assert filed.ok and "problem was cut to 600 of its 742 characters" in filed.text, filed.text
    [row] = rows(agent, "SELECT problem, proposed_change FROM upgrades")
    assert len(row["problem"]) == 600 and row["problem"].endswith("…") and len(row["proposed_change"]) == 600
    assert tools.SPECS["venture_update"].fields["learned"].cut


# --- LIVE 7: the one-time-code mask stays within a table cell -------------------------------------------------------


def test_a_number_in_the_next_cell_is_not_a_code() -> None:
    row = "9 | 55 | ok | 893929 | - | workshop/scripts/verify-and-fix-the-9.py | Verify and fix the existing file"
    assert privacy.Masker()(row) == row
    assert privacy.Masker()("Your verification code | is 483920") == "Your verification code | is 483920"
    assert privacy.Masker()("| Verification code | 483920 |") == "| Verification code | 483920 |"  # DOCS says so
    assert privacy.Masker()("Your verification code is 483920") == f"Your verification code is {privacy.CODE}"


# --- LIVE 8: the plan lists the agent's open upgrade requests -------------------------------------------------------


def test_the_plan_lists_open_upgrade_requests(data_dir: Path) -> None:
    fake = FakeTransport(script=[idle(steps=[]), idle(steps=[])])
    agent, _ = run(data_dir, fake)
    for title in ("Read big PNGs", "Bigger notes"):
        filed = call(
            agent,
            "request_upgrade",
            title=title,
            problem="p",
            proposed_change="c",
            expected_benefit="b",
            priority="low",
        )
        assert filed.ok, filed.text
    with agent.db.transaction() as conn:
        conn.execute("UPDATE upgrades SET status = 'accepted', decided_at = '2026-10-02T08:00:00Z' WHERE id = 1")
    with agent.db.connection() as conn:
        line = context.upgrades_line(context.open_upgrades(conn, agent.scope()))
    today = agent.clock.now().date().isoformat()
    assert line == f"Your upgrade requests not built in yet: #2 new ({today}), #1 accepted (2026-10-02)."
    agent.run_cycle("schedule")
    planner = fake.sent[-1]["messages"][0]["content"][0]["text"]  # type: ignore[attr-defined]
    assert f"== WAITING FOR YOUR OWNER ==\n{line}" in planner
    with agent.db.transaction() as conn:
        conn.execute("UPDATE upgrades SET status = 'released', released_version = '0.14.0'")
    with agent.db.connection() as conn:
        assert context.open_upgrades(conn, agent.scope()) == []


# --- LIVE 9: the message that reports a kept promise gets through ---------------------------------------------------


def test_a_kept_promise_can_be_reported_past_the_daily_message_limit(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[idle(steps=[])]))
    assert call(agent, "message_owner", text="Update 1.").ok
    assert call(agent, "message_owner", text="Site soon.", commits="Draft the website's home page", due=day(4)).ok
    early = call(agent, "obligation_done", numbers="1", result="Wrote it, see message #2")
    assert not early.ok and "naming #1 (the daily limit lets it through if it promises nothing new)" in early.text
    assert not call(agent, "message_owner", text="The home page is done.").ok  # the limit holds for others
    chained = call(agent, "message_owner", text="#1 is kept. Shop next.", commits="Open the shop", due=day(5))
    assert not chained.ok and "2 messages today" in chained.text  # a report that promises anew is no exemption
    told = call(agent, "message_owner", text="Promise #1 is kept: the home page is done (site_page 'index').")
    assert told.ok, told.text
    assert call(agent, "obligation_done", numbers="1", result="Told in message #3; site_page 'index'").ok
    again = call(agent, "message_owner", text="About #1 once more.")
    assert not again.ok and "2 messages today" in again.text  # once per promise, while it is open and untold


# --- LIVE 10: a folder read as a file --------------------------------------------------------------------------------


def test_reading_a_folder_points_at_workspace_list(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[idle(steps=[])]))
    assert call(agent, "workspace_write", path="shop/notes.md", mode="create", content="Hi").ok
    read = call(agent, "workspace_read", path="shop")
    assert not read.ok and "shop is a folder: workspace_list lists it" in read.text
    missing = call(agent, "workspace_read", path="nothing")
    assert not missing.ok and "only text files" in missing.text


# --- LIVE 11: an opt-out "replied" only to Ember's own email --------------------------------------------------------


def incoming(uid: int, sender: str, body: str, in_reply_to: str | None = None) -> Any:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "ember@mail.example", "Re: Hello"
    msg["Date"], msg["Message-ID"] = "Mon, 28 Sep 2026 08:00:00 +0200", f"<m{uid}@example.org>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body)
    return mail.parse_message(msg.as_bytes(), uid)


def test_an_opt_out_says_wrote_unless_it_answers_embers_email(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, asks_and_sleeps(720))
    agent.run_cycle("schedule")
    scope = agent.scope()
    with agent.db.transaction() as conn:
        mailstore.store_outgoing(
            conn,
            scope,
            approval_id=1,
            message_id="<ours-1@ember>",
            in_reply_to=None,
            references=None,
            from_addr="ember@mail.example",
            from_name="Ember",
            to_addr="ann@example.org",
            subject="Hello",
            body="Hello Ann.",
            now="2026-09-28T07:00:00Z",
        )
    ann = incoming(61, "ann@example.org", "Stop", in_reply_to="<ours-1@ember>")
    bob = incoming(62, "bob@example.org", "Bitte keine Werbung mehr.")

    class Box(mail.FakeMailbox):
        def fetch_new(self, after_uid: int, limit: int = 20, uidvalidity: Any = None, deadline: Any = None) -> Any:
            return mail.FetchResult([ann, bob], 62, self.uidvalidity)

    mailstore.fetch(agent.db, agent.clock, scope, Box(scope.session))
    reasons = {r["address"]: r["reason"] for r in rows(agent, "SELECT address, reason FROM email_suppressions")}
    assert reasons["ann@example.org"] == 'replied "Stop"'
    assert reasons["bob@example.org"].startswith('wrote "')
