"""0.42.2 (analysis-0.40.1 audit, M2): every approved request Ember's code carries out ages out, as an email does.

Since 0.40.3 an approved email not begun within APPROVED_DAYS of its approval is closed unsent. A listing, a change, a
pin, a post, a Printify product or a blog page never aged: one that a pause, the kill switch, money, a daily limit or
its service's waits held went out whenever it was free, weeks later, when it may no longer fit. Now each is closed
unsent after the same days, with why, also while it waits for its service (retry.py); the agent hears it and its plan
proposes it again. What began, and the owner's Undo, never age out.

0.42.3 (analysis-0.40.1 audit): an executor read the age only as it would start a request: while an account stayed
disconnected, Bluesky refused the login, Printify had no shop or Ember's mailbox couldn't be read, the card said
"approved" until it ran. Now the round's last step closes such a request too (Agent.close_stale). And a Printify change
and the blog's link page have their own tests, as the product and the post had.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import version  # noqa: E402
from app.agent import news, obligations, plan  # noqa: E402
from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.economy.life import KILLED_KEY  # noqa: E402
from app.integrations import bluesky, connectors, etsy, executor, pinterest, printify, retry  # noqa: E402
from app.integrations.bluesky import FakeBluesky  # noqa: E402
from app.integrations.etsy import FakeShop  # noqa: E402
from app.integrations.pinterest import FakePinterest  # noqa: E402
from app.integrations.printify import FakePrintify  # noqa: E402
from tests.agent_helpers import owner, rows, run  # noqa: E402
from tests.agent_helpers import tools as calls  # noqa: E402
from tests.channels.test_approval_boundary import WAIT, journal, refuse, request_of  # noqa: E402
from tests.channels.test_approval_timing import an_email  # noqa: E402
from tests.channels.test_blog import BLOG, blog_context  # noqa: E402
from tests.channels.test_blog import proposed as page_proposed  # noqa: E402
from tests.channels.test_bluesky import proposed as post_proposed  # noqa: E402
from tests.channels.test_etsy import listing_rows  # noqa: E402
from tests.channels.test_pinterest import BOARD, OTHER_WORDS, a_pin, pin_context, pin_rows, pinned  # noqa: E402
from tests.channels.test_pinterest import proposed as pin_proposed  # noqa: E402
from tests.channels.test_printify import proposed as product_proposed  # noqa: E402
from tests.channels.test_printify_changes import NEW_TITLE, last_change, live_product  # noqa: E402
from tests.channels.test_printify_changes import change as product_edit  # noqa: E402
from tests.etsy_helpers import a_change, call, listed, proposed, shop_context, views_approval  # noqa: E402
from tests.mail.test_executor import live_approved, smtp  # noqa: E402, F401 - the fixture
from tests.mail.test_mail import imap as imap  # noqa: E402, F401 - the fixture
from tests.mail.test_mail import mail_cycle  # noqa: E402
from tests.owner.test_audit import feed, undo  # noqa: E402
from tests.plan.test_step_brake import waits  # noqa: E402
from tests.queue_helpers import slot_comes  # noqa: E402

DAYS = connectors.APPROVED_DAYS
STALE = connectors.STALE.format(days=DAYS)


def listing(data_dir: Path) -> tuple[Any, int]:
    agent, _, request = proposed(data_dir)
    return agent, request


def change(data_dir: Path) -> tuple[Any, int]:
    agent, listing_id = listed(data_dir)
    return agent, a_change(agent, shop_context(agent), listing_id, title="Weekly Planner, A4")


def pin(data_dir: Path) -> tuple[Any, int]:
    agent, _, request = pin_proposed(data_dir)
    return agent, request


def post(data_dir: Path) -> tuple[Any, int]:
    agent, _, request = post_proposed(data_dir)
    return agent, request


def product(data_dir: Path) -> tuple[Any, int]:
    agent, _, request = product_proposed(data_dir)
    return agent, request


def page(data_dir: Path) -> tuple[Any, int]:
    return page_proposed(data_dir)


def product_change(data_dir: Path) -> tuple[Any, int]:
    """0.42.3: a change of a live Printify product."""
    agent, _, listing_id, _ = live_product(data_dir)
    assert product_edit(agent, listing_id, title=NEW_TITLE).ok
    return agent, last_change(agent)["id"]


def link_page(data_dir: Path) -> tuple[Any, int]:
    """0.42.3: the blog's link page."""
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=BLOG)
    links = [{"label": "Planner", "url": "https://www.etsy.com/listing/1"}, {"label": "Blog", "url": "/blog/"}]
    args = {"bio": "Praktische Vorlagen für Bewerbung und Geld.", "links": links, "reason": "The new listing first."}
    made = call(blog_context(agent), "propose_link_page", args)
    assert made.ok, made.text
    return agent, rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'site_links'")[0]["id"]


UPLOADS = "SELECT * FROM site_uploads WHERE approval_id = ? AND status <> 'proposed'"
# Each kind: how it is proposed, what its note begins with, the table Ember's code begins it in, and its channel's
# queue (None: it goes out at the next round)
KINDS: dict[str, tuple[Callable[[Path], tuple[Any, int]], str, str, str | None]] = {
    "listing": (listing, "Not listed", "SELECT * FROM etsy_listings WHERE approval_id = ?", None),
    "change": (change, "Not changed", "SELECT * FROM etsy_edits WHERE approval_id = ?", None),
    "pin": (pin, "Not pinned", "SELECT * FROM pinterest_pins WHERE approval_id = ?", "pinterest"),
    "post": (post, "Not posted", "SELECT * FROM bluesky_posts WHERE approval_id = ?", "bluesky"),
    "product": (product, "Not created", "SELECT * FROM printify_products WHERE approval_id = ?", None),
    "page": (page, "Not uploaded", UPLOADS, None),
    "product_change": (product_change, "Not changed", "SELECT * FROM printify_edits WHERE approval_id = ?", None),
    "link_page": (link_page, "Not uploaded", UPLOADS, None),
}
# 0.42.3: each kind's executor, as the agent holds it
EXECUTORS = {
    "listing": "publisher",
    "change": "publisher",
    "pin": "pins",
    "post": "bluesky_posts",
    "product": "pod",
    "page": "blog",
    "product_change": "pod",
    "link_page": "blog",
}


def begun(agent: Any, sql: str, approval_id: int) -> list[dict[str, Any]]:
    with agent.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql, (approval_id,))]


def paused_for(agent: Any, days: float, channel: str | None = None) -> None:
    """The owner pauses Ember (nothing approved goes out meanwhile) and resumes it ``days`` later; a pin or a post's
    slot then comes."""
    agent.economy.set_paused(True)
    assert all(outcome != "active" for _, outcome in agent.execute_approved())
    agent.clock.advance(days=days)
    agent.economy.set_paused(False)
    if channel is not None:
        slot_comes(agent, channel)
    agent.run_policy()  # the first round after Resume, in the scheduler's order


def events(agent: Any) -> list[str]:
    return [e["message"] for e in agent.db.recent_events(limit=50)]


def keep(agent: Any) -> None:
    """The plan tree kept, as at a cycle's start."""
    with agent.db.transaction() as conn:
        plan.keep(conn, agent.scope(), to_iso(agent.clock.now()), agent.clock.today(), {})


# --- each kind ages out as an email does ----------------------------------------------------------------------------


def test_every_approved_email_and_action_ages_out_after_the_same_days() -> None:
    assert DAYS == executor.APPROVED_DAYS == 7
    assert set(connectors.AGING) == {
        "etsy_listing",
        "etsy_edit",
        "pinterest_pin",
        "bluesky_post",
        "printify_product",
        "printify_edit",
        "site_post",
        "site_links",
    }  # not an Undo, nor what the owner carries out, nor the sandbox's test pin or the last will
    assert STALE.endswith("so it may no longer fit: it takes a new request if it still does")


@pytest.mark.parametrize("kind", list(KINDS))
def test_an_approved_action_held_through_a_long_pause_goes_back_instead_of_out(data_dir: Path, kind: str) -> None:
    make, done, started, channel = KINDS[kind]
    agent, request = make(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    paused_for(agent, 21, channel)
    assert (request, "failed") in agent.execute_approved()  # before: it went out, 21 days late
    closed = request_of(agent, request)
    assert (closed["status"], closed["closed_by"]) == ("failed", "Ember")
    assert closed["result_note"] == f"{done}: {STALE}"
    assert begun(agent, started, request) == [] and journal(agent, request) == []  # nothing began
    assert rows(agent, f"SELECT seen_cycle_id FROM approvals WHERE id = {request}") == [{"seen_cycle_id": None}]
    assert f"Request #{request}: {done}: it didn't go out within {DAYS} days" in "\n".join(events(agent))
    agent.clock.advance(days=1)
    assert all(approval_id != request for approval_id, _ in agent.execute_approved())  # never later either


@pytest.mark.parametrize("kind", ["listing", "pin"])
def test_an_approved_action_within_its_days_still_goes_out(data_dir: Path, kind: str) -> None:
    make, _, _, channel = KINDS[kind]
    agent, request = make(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    paused_for(agent, DAYS - 1, channel)
    assert (request, "active") in agent.execute_approved()


def test_a_listing_the_daily_limit_holds_past_its_days_goes_back(data_dir: Path) -> None:
    agent, request = listing(data_dir)
    agent.settings = agent.publisher.settings = agent.settings.model_copy(update={"etsy_listings_per_day": 0})
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "waiting_limit")]
    agent.clock.advance(days=DAYS, minutes=1)
    assert agent.execute_approved() == [(request, "failed")]  # before: it waited for the limit for good
    assert request_of(agent, request)["result_note"] == f"Not listed: {STALE}" and listing_rows(agent) == []


# --- a request that waits for its service (retry.py) ----------------------------------------------------------------


def test_a_listing_that_waits_for_etsy_ages_out_before_it_is_tried_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = listing(data_dir)
    calls = refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.clock.advance(days=DAYS - 1, hours=23, minutes=59)  # held until then (a pause, the daily limit)
    assert agent.execute_approved() == [(request, retry.LATER)]  # within its days: tried, and Etsy asks to wait
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]  # before: tried again, at most 3 times
    assert len(calls) == 1 and agent.etsy.shop().state["listings"] == {}
    closed = request_of(agent, request)
    assert (closed["status"], closed["result_note"]) == ("failed", f"Not listed: {STALE}")
    [row] = listing_rows(agent)  # the row of its wait says why it isn't tried again, not when it would be
    assert (row["status"], row["result"], row["error"]) == ("failed", f"Not listed: {STALE}", STALE)
    shown = views_approval(agent, request)["execution"]
    assert (shown["status"], shown["error"]) == ("failed", STALE)
    [entry] = journal(agent, request)  # only its wait: nothing more began
    assert json.loads(entry["after"])["retry_at"]
    with agent.db.connection() as conn:
        assert retry.waiting(conn, request) is None
    agent.clock.advance(hours=7)
    assert agent.execute_approved() == [] and len(calls) == 1


def test_a_pin_that_waits_for_pinterest_ages_out_in_its_next_slot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, _ = pinned(data_dir)  # its board exists: a pin on it may wait for Pinterest
    assert a_pin(agent, pin_context(agent), board_id=BOARD, image="shop/pin-2.png", **OTHER_WORDS).ok
    request = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    calls = refuse(monkeypatch, FakePinterest, "create_pin", pinterest.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.clock.advance(days=DAYS - 1, hours=23, minutes=59)
    slot_comes(agent, "pinterest")
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.clock.advance(hours=1)
    slot_comes(agent, "pinterest")
    assert agent.execute_approved() == [(request, "failed")]
    assert len(calls) == 1 and len(agent.pinterest.account().state["pins"]) == 1  # the first pin alone
    assert request_of(agent, request)["result_note"] == f"Not pinned: {STALE}"
    [row] = [r for r in pin_rows(agent) if r["approval_id"] == request]
    assert (row["status"], row["board_id"], row["error"]) == ("failed", None, STALE)  # no day's pin is counted


def test_a_product_that_waits_for_printify_ages_out_before_it_is_tried_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0.42.3 (analysis-0.40.1 audit, M3): a Printify product waits for its service too, and ages out as a listing does
    agent, request = product(data_dir)
    calls = refuse(monkeypatch, FakePrintify, "upload", printify.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.clock.advance(days=DAYS - 1, hours=23, minutes=59)
    assert agent.execute_approved() == [(request, retry.LATER)]  # within its days: tried, and Printify asks to wait
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]
    assert len(calls) == 1 and agent.printify.account().state["products"] == {}
    closed = request_of(agent, request)
    assert (closed["status"], closed["result_note"]) == ("failed", f"Not created: {STALE}")
    [row] = begun(agent, KINDS["product"][2], request)  # the row of its wait says why it isn't tried again
    assert (row["status"], row["result"], row["error"]) == ("failed", f"Not created: {STALE}", STALE)
    shown = views_approval(agent, request)["execution"]
    assert (shown["status"], shown["error"]) == ("failed", STALE)
    [entry] = journal(agent, request)  # only its wait: nothing more began
    assert json.loads(entry["after"])["retry_at"]


# --- what began, and the owner's Undo, never age out ----------------------------------------------------------------


def test_a_listing_ember_s_code_began_is_never_closed_as_too_old(data_dir: Path) -> None:
    agent, request = listing(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    scope = agent.scope()
    with agent.db.transaction() as conn:  # begun, and the app stopped in the middle of it
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, status, title)"
            " VALUES (?, ?, ?, ?, 'running', 'Planner')",
            (scope.mode, scope.session, request, to_iso(agent.clock.now())),
        )
    agent.clock.advance(days=DAYS + 1)
    assert agent.execute_approved() == [] and request_of(agent, request)["status"] == "approved"
    assert agent.publisher.recover() == 1  # it may have reached Etsy: unclear, as before, never "too old"
    assert listing_rows(agent)[0]["status"] == "unclear"
    assert "it didn't go out within" not in str(request_of(agent, request)["result_note"])


def test_the_owner_s_undo_never_ages_out(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    [created] = feed(agent)
    request = undo(agent, created["id"])  # Deactivate it
    agent.clock.advance(days=DAYS + 1)  # held (the app was down, say)
    assert agent.execute_approved() == [(request, "done")]
    assert agent.etsy.shop().state["listings"][str(listing_id)]["state"] == "inactive"


# --- 0.42.3: also when its executor doesn't run ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(KINDS))
def test_an_approved_action_whose_executor_doesn_t_run_goes_back_after_its_days_too(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    make, done, started, _ = KINDS[kind]
    agent, request = make(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    monkeypatch.setattr(getattr(agent, EXECUTORS[kind]), "run", lambda *_, **__: [])  # it never comes to it
    agent.clock.advance(days=DAYS, minutes=-1)
    assert agent.execute_approved() == [] and request_of(agent, request)["status"] == "approved"  # within its days
    agent.clock.advance(minutes=2)
    assert agent.execute_approved() == [(request, "failed")]  # before: "approved" while its executor didn't run
    closed = request_of(agent, request)
    assert (closed["status"], closed["closed_by"], closed["result_note"]) == ("failed", "Ember", f"{done}: {STALE}")
    assert begun(agent, started, request) == [] and journal(agent, request) == []  # nothing began
    assert rows(agent, f"SELECT seen_cycle_id FROM approvals WHERE id = {request}") == [{"seen_cycle_id": None}]
    assert f"Request #{request}: {done}: it didn't go out within {DAYS} days" in "\n".join(events(agent))
    agent.clock.advance(days=1)
    assert agent.execute_approved() == []  # once


def test_a_product_waits_no_longer_than_its_days_while_printify_has_no_shop(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = product(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    monkeypatch.setattr(agent.pod, "shop_id", lambda: None)  # it can't be told which shop sells through Etsy
    assert agent.execute_approved() == [] and views_approval(agent, request)["execution"]["status"] == "waiting"
    agent.clock.advance(days=DAYS, minutes=1)
    assert agent.execute_approved() == [(request, "failed")]  # before: "waiting" until Printify had a shop again
    assert request_of(agent, request)["result_note"] == f"Not created: {STALE}"
    assert agent.printify.account().state["products"] == {} and journal(agent, request) == []
    with agent.db.transaction() as conn:  # the agent hears it, as from the executor
        obligations.keep(conn, agent.scope(), to_iso(agent.clock.now()), "2000-01-01T00:00:00Z")
    [owed] = rows(agent, f"SELECT what FROM obligations WHERE approval_id = {request}")
    assert "failed (Not created: it didn't go out" in owed["what"]


def test_a_post_waits_no_longer_than_its_days_while_bluesky_refuses_the_login(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = post(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")

    def refused(self: FakeBluesky) -> None:
        raise bluesky.Refused("HTTP 401: AuthenticationRequired")

    monkeypatch.setattr(FakeBluesky, "keep_alive", refused)
    agent.clock.advance(days=DAYS, minutes=1)
    slot_comes(agent, "bluesky")
    assert agent.execute_approved() == [(request, "failed")]  # before: approved until Bluesky took the login again
    assert request_of(agent, request)["result_note"] == f"Not posted: {STALE}"
    assert begun(agent, KINDS["post"][2], request) == [] and agent.bluesky.account().state["posts"] == {}


def test_a_listing_that_waits_for_etsy_ages_out_while_the_shop_is_disconnected(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = listing(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, retry.LATER)]  # begun, and Etsy asked to wait: nothing made
    monkeypatch.setattr(agent.publisher, "shop", lambda: None)  # then the owner's Etsy connection is gone
    agent.clock.advance(days=DAYS, minutes=1)
    assert agent.execute_approved() == [(request, "failed")]
    assert request_of(agent, request)["result_note"] == f"Not listed: {STALE}"
    [row] = listing_rows(agent)  # the row of its wait says why it isn't tried again
    assert (row["status"], row["result"], row["error"]) == ("failed", f"Not listed: {STALE}", STALE)
    [entry] = journal(agent, request)  # only its wait
    assert json.loads(entry["after"])["retry_at"]


def test_an_approved_email_goes_back_after_its_days_while_the_mailbox_can_t_be_read(
    data_dir: Path,
    imap: Any,
    smtp: Any,  # noqa: F811 - the fixtures
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, request = live_approved(data_dir)
    monkeypatch.setattr(imap, "password", "changed at the provider")  # the login is refused, for good
    agent.clock.advance(days=DAYS, minutes=1)
    # before: it waited for the mailbox, "approved", until it could be read: then it went back
    assert agent.execute_approved() == [(request, executor.WAITING_MAIL), (request, "failed")]
    closed = request_of(agent, request)
    assert (closed["status"], closed["closed_by"]) == ("failed", "Ember")
    assert closed["result_note"] == f"Not sent: {executor.STALE.format(days=DAYS)}"
    assert smtp.instances == [] and rows(agent, "SELECT * FROM email_actions") == []
    assert f"The approved email of request #{request} was not sent: it didn't go out within" in "\n".join(events(agent))


def test_an_approved_email_goes_back_after_its_days_when_email_is_switched_off(data_dir: Path) -> None:
    agent, _ = mail_cycle(data_dir, calls(("email_inbox", {})))
    request = an_email(agent, "reader@example.org", 1)
    agent.mailbox = agent.executor.mailbox = None  # switched off since: the email executor doesn't run
    agent.clock.advance(days=DAYS, minutes=1)
    assert agent.execute_approved() == [(request, "failed")]  # before: "approved" for good
    assert request_of(agent, request)["result_note"] == f"Not sent: {executor.STALE.format(days=DAYS)}"


def test_nothing_goes_back_while_the_kill_switch_is_on(data_dir: Path) -> None:
    agent, request = listing(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    email = an_email(agent, "reader@example.org", 1)
    agent.clock.advance(days=DAYS, minutes=1)
    agent.db.set_meta(KILLED_KEY, "1")  # pressed in the middle of a round: every executor stops at its next item
    assert agent.close_stale() == []
    assert [request_of(agent, n)["status"] for n in (request, email)] == ["approved", "approved"]
    agent.db.set_meta(KILLED_KEY, "0")
    assert agent.close_stale() == [(request, "failed"), (email, "failed")]


# --- what the agent hears, and its plan ----------------------------------------------------------------------------


def test_the_agent_hears_it_and_its_plan_proposes_the_listing_again(data_dir: Path) -> None:
    agent, request = listing(data_dir)
    [line] = rows(agent, f"SELECT project_id FROM approvals WHERE id = {request}")
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    keep(agent)
    assert waits(agent, "You approve it", line["project_id"]) == "owner"  # its listing is on its way
    paused_for(agent, DAYS + 1)
    assert agent.execute_approved() == [(request, "failed")]
    with agent.db.transaction() as conn:
        said = news.collect(conn, agent.db, agent.scope(), version.app_version()).approval_lines()
        obligations.keep(conn, agent.scope(), to_iso(agent.clock.now()), "2000-01-01T00:00:00Z")
    [title] = rows(agent, f"SELECT title FROM approvals WHERE id = {request}")
    assert f'Request #{request} (sell) "{title["title"]}": failed. Result: "Not listed: {STALE}".' in said
    [owed] = rows(agent, f"SELECT what FROM obligations WHERE approval_id = {request}")
    assert owed["what"].startswith(f"request #{request} ") and "failed (Not listed: it didn't go out" in owed["what"]
    keep(agent)
    again = rows(agent, "SELECT title, status FROM plan_nodes WHERE title LIKE 'Propose the listing again%'")
    assert again == [{"title": f"Propose the listing again: request #{request} failed", "status": "open"}]
    assert waits(agent, "You approve it", line["project_id"]) == "step"  # not on the owner: on the new proposal
