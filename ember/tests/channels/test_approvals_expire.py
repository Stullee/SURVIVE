"""0.42.2 (analysis-0.40.1 audit, M2): every approved request Ember's code carries out ages out, as an email does.

Since 0.40.3 an approved email not begun within APPROVED_DAYS of its approval is closed unsent. A listing, a change, a
pin, a post, a Printify product or a blog page never aged: one that a pause, the kill switch, money, a daily limit or
its service's waits held went out whenever it was free, weeks later, when it may no longer fit. Now each is closed
unsent after the same days, with why, also while it waits for its service (retry.py); the agent hears it and its plan
proposes it again. What began, and the owner's Undo, never age out.
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
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import connectors, etsy, executor, pinterest, printify, retry  # noqa: E402
from app.integrations.etsy import FakeShop  # noqa: E402
from app.integrations.pinterest import FakePinterest  # noqa: E402
from app.integrations.printify import FakePrintify  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.channels.test_approval_boundary import WAIT, journal, refuse, request_of  # noqa: E402
from tests.channels.test_blog import proposed as page_proposed  # noqa: E402
from tests.channels.test_bluesky import proposed as post_proposed  # noqa: E402
from tests.channels.test_etsy import listing_rows  # noqa: E402
from tests.channels.test_pinterest import BOARD, OTHER_WORDS, a_pin, pin_context, pin_rows, pinned  # noqa: E402
from tests.channels.test_pinterest import proposed as pin_proposed  # noqa: E402
from tests.channels.test_printify import proposed as product_proposed  # noqa: E402
from tests.etsy_helpers import a_change, listed, proposed, shop_context, views_approval  # noqa: E402
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


# Each kind: how it is proposed, what its note begins with, the table Ember's code begins it in, and its channel's
# queue (None: it goes out at the next round)
KINDS: dict[str, tuple[Callable[[Path], tuple[Any, int]], str, str, str | None]] = {
    "listing": (listing, "Not listed", "SELECT * FROM etsy_listings WHERE approval_id = ?", None),
    "change": (change, "Not changed", "SELECT * FROM etsy_edits WHERE approval_id = ?", None),
    "pin": (pin, "Not pinned", "SELECT * FROM pinterest_pins WHERE approval_id = ?", "pinterest"),
    "post": (post, "Not posted", "SELECT * FROM bluesky_posts WHERE approval_id = ?", "bluesky"),
    "product": (product, "Not created", "SELECT * FROM printify_products WHERE approval_id = ?", None),
    "page": (page, "Not uploaded", "SELECT * FROM site_uploads WHERE approval_id = ? AND status <> 'proposed'", None),
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
