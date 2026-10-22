"""0.42.0 (analysis-0.40.1 codebase, M3 and M4): the approval boundary.

M3: a 429 or a 503 with Retry-After, before anything changed, closed the approval as failed: the agent proposed again
and the owner approved again, though the service had said when to come back. Now the request stays approved and is
tried again then, through every check of its executor's start transaction, at most retry.TRIES times; a refusal after
something changed (a draft, a new board, a part of a change) stays as it was, an unlock's request isn't tried again,
and an email never is.

M4: the cards of the requests whose files Ember's code uploads only as proposed (a listing, a change, a pin, a post, a
product) showed the workspace file as it is now. Now each says which file changed since it was proposed, as the KDP
book's card does, and the owner's Approve of it is refused.
"""

from __future__ import annotations

import json
import smtplib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

httpx2 = pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import policy, store  # noqa: E402
from app.economy.clock import Clock, from_iso, to_iso  # noqa: E402
from app.economy.life import KILLED_KEY  # noqa: E402
from app.integrations import bluesky, connectors, etsy, etsy_publisher, pinterest, publish_queue, retry  # noqa: E402
from app.integrations.bluesky import FakeBluesky  # noqa: E402
from app.integrations.etsy import FakeShop, Unclear  # noqa: E402
from app.integrations.pinterest import FakePinterest, Pin  # noqa: E402
from tests.agent_helpers import (  # noqa: E402
    owner,
    plan,
    rows,
)
from tests.api_helpers import post  # noqa: E402
from tests.channels.test_bluesky import live as bluesky_live  # noqa: E402
from tests.channels.test_bluesky import post_rows, session  # noqa: E402
from tests.channels.test_bluesky import proposed as post_proposed  # noqa: E402
from tests.channels.test_etsy import listing_rows  # noqa: E402
from tests.channels.test_pinterest import (  # noqa: E402
    BOARD,
    OTHER_WORDS,
    a_pin,
    live_account,
    picture,
    pin_context,
    pin_rows,
    pinned,
)
from tests.channels.test_pinterest import proposed as pin_proposed  # noqa: E402
from tests.economy_helpers import ScriptedTransport  # noqa: E402
from tests.etsy_helpers import (  # noqa: E402
    a_change,
    a_listing,
    listed,
    live_shop,
    proposed,
    shop_context,
    views_approval,
)
from tests.mail.test_executor import approval, live_approved, smtp  # noqa: E402, F401 - the fixture
from tests.mail.test_mail import imap as imap  # noqa: E402, F401 - the fixture
from tests.owner.test_parked_lines import line_of, park  # noqa: E402
from tests.owner.test_policy import a_milestone  # noqa: E402
from tests.owner.test_unlock_safety import a_small_cut  # noqa: E402
from tests.queue_helpers import next_slot, slot_comes  # noqa: E402

WAIT = 120.0  # the seconds the fake services ask Ember's code to wait


def refuse(monkeypatch: pytest.MonkeyPatch, cls: type, name: str, busy: type, times: int = 1) -> list[Any]:
    """``cls.name`` answers "come back later" (``busy``, retry after WAIT s) its first ``times`` calls, then as before.
    Returns the calls."""
    original = getattr(cls, name)
    calls: list[Any] = []

    def answer(self: Any, *args: Any) -> Any:
        calls.append(args)
        if len(calls) <= times:
            raise busy(f"HTTP 429{retry.said(429, WAIT)}", WAIT)
        return original(self, *args)

    monkeypatch.setattr(cls, name, answer)
    return calls


def journal(agent: Any, approval_id: int) -> list[dict[str, Any]]:
    return rows(
        agent, f"SELECT class, status, after, note FROM action_journal WHERE approval_id = {approval_id} ORDER BY id"
    )


def request_of(agent: Any, approval_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT status, closed_at, closed_by, result_note FROM approvals WHERE id = {approval_id}")[0]


# --- M3: what "come back later" is ----------------------------------------------------------------------------------


def test_retry_after_is_read_in_seconds_or_as_a_date_and_kept_within_bounds() -> None:
    assert retry.seconds("120") == 120.0 and retry.seconds(" 0 ") == 0.0
    sent = "Wed, 21 Oct 2026 07:28:00 GMT"
    assert retry.seconds("Wed, 21 Oct 2026 07:30:00 GMT", sent) == 120.0  # counted from the answer's own Date
    for unreadable in (None, "", "soon", "-5", "1e3", "Wed, 21 Oct 2026 07:30:00 GMT", "9" * 30):
        assert retry.seconds(unreadable) is None
    assert retry.asks_to_wait(429, None) and retry.asks_to_wait(503, "30")
    assert not retry.asks_to_wait(503, None) and not retry.asks_to_wait(500, "30") and not retry.asks_to_wait(400, "30")
    now = Clock().now()
    assert retry.until(now, 5) == now + retry.FLOOR  # at least a minute
    assert retry.until(now, 10**12) == now + retry.CEILING  # at most six hours
    assert retry.until(now, None) == now + retry.UNSAID
    assert retry.until(now, 600) == now + timedelta(minutes=10)


@pytest.mark.parametrize(
    ("answer", "error", "wait"),
    [
        (httpx2.Response(429, headers={"retry-after": "120"}, json={"error": "Too many requests"}), etsy.Busy, 120.0),
        (httpx2.Response(429, json={}), etsy.Busy, None),
        (httpx2.Response(503, headers={"retry-after": "60"}, json={}), etsy.Busy, 60.0),
        (httpx2.Response(503, json={}), Unclear, None),  # without Retry-After a proxy may have reached Etsy
        (httpx2.Response(500, headers={"retry-after": "60"}, json={}), Unclear, None),
    ],
)
def test_etsy_asking_to_come_back_later_is_told_from_what_may_have_changed(
    tmp_path: Path, answer: Any, error: type, wait: float | None
) -> None:
    shop, _ = live_shop(tmp_path, {("POST", "/v3/application/shops/777/listings"): answer})
    with pytest.raises(error) as caught:
        shop.create_draft(a_listing())
    if error is etsy.Busy:
        assert isinstance(caught.value, etsy.NotSent) and caught.value.wait == wait
        assert str(caught.value).startswith(f"HTTP {answer.status_code}")


def test_pinterest_and_bluesky_asking_to_come_back_later_too(tmp_path: Path) -> None:
    limited = httpx2.Response(429, headers={"retry-after": "30"}, json={"message": "Rate limited"})
    account, _ = live_account(tmp_path, {("POST", "/v5/pins"): limited})
    upload = pinterest.image("shop/pin.png", b"png")
    with pytest.raises(pinterest.Busy) as caught:
        account.create_pin("1", Pin("t", "d", "l", "", upload, 10, 15), b"png")
    assert caught.value.wait == 30.0 and isinstance(caught.value, pinterest.NotSent)
    later = httpx2.Response(503, headers={"retry-after": "90"}, json={"error": "Unavailable"})
    poster, _, _ = bluesky_live(
        {
            ("POST", "com.atproto.server.createSession"): session(),
            ("POST", "com.atproto.repo.createRecord"): later,
        }
    )
    with pytest.raises(bluesky.Busy) as said:
        poster.create_post(bluesky.Post("Hallo.", "de", None), None, None)
    assert said.value.wait == 90.0 and not isinstance(said.value, bluesky.Refused)


# --- M3: an Etsy listing --------------------------------------------------------------------------------------------


def test_a_listing_etsy_asks_to_wait_for_stays_approved_and_is_listed_once_then(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = proposed(data_dir)
    calls = refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    asked = agent.clock.now()
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert request_of(agent, request)["status"] == "approved" and request_of(agent, request)["closed_at"] is None
    [row] = listing_rows(agent)
    assert row["status"] == "failed" and row["listing_id"] is None
    assert row["result"].startswith(f"Etsy asked to wait: retrying at {asked + timedelta(seconds=WAIT):%H:%M} (wait 1")
    [entry] = journal(agent, request)
    assert entry["status"] == "failed" and json.loads(entry["after"]) == {
        "retry_at": to_iso(asked + timedelta(seconds=WAIT))
    }
    shown = views_approval(agent, request)["execution"]
    assert shown["status"] == "retrying" and shown["due_at"] == to_iso(asked + timedelta(seconds=WAIT))
    assert "HTTP 429" in shown["error"]
    with agent.db.connection() as conn:
        assert etsy_publisher.created_today(conn, agent.clock, agent.scope()) == 0  # nothing made: the limit is free
        assert etsy_publisher.waiting(conn, agent.scope()) == 1
    assert agent.publisher.recover() == 0  # a restart leaves it waiting: it isn't unclear
    agent.clock.advance(seconds=WAIT - 1)
    assert agent.execute_approved() == []  # not before Etsy's wait is over
    agent.clock.advance(seconds=1)
    assert agent.execute_approved() == [(request, "active")]
    assert len(calls) == 2 and len(agent.etsy.shop().state["listings"]) == 1
    assert [e["status"] for e in journal(agent, request)] == ["failed", "done"]
    assert request_of(agent, request)["status"] == "done" and listing_rows(agent)[0]["status"] == "active"
    assert agent.execute_approved() == []  # never twice


def test_a_listing_fails_as_before_once_it_waited_three_times(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, request = proposed(data_dir)
    calls = refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    for _ in range(retry.TRIES):
        assert agent.execute_approved() == [(request, retry.LATER)]
        agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]
    assert len(calls) == retry.TRIES + 1
    closed = request_of(agent, request)
    assert (closed["status"], closed["closed_by"]) == ("failed", "Ember")
    assert closed["result_note"].startswith("Not listed: Etsy refused it (HTTP 429")
    entries = journal(agent, request)
    assert [e["status"] for e in entries] == ["failed"] * (retry.TRIES + 1)
    assert [json.loads(e["after"] or "{}").get("retry_at") is not None for e in entries] == [True] * 3 + [False]
    agent.clock.advance(hours=7)
    assert agent.execute_approved() == [] and agent.etsy.shop().state["listings"] == {}


def test_the_kill_switch_pause_and_the_files_check_apply_to_every_try(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.clock.advance(seconds=WAIT)
    agent.economy.set_paused(True)
    assert agent.execute_approved() == []  # paused: nothing is tried
    agent.economy.set_paused(False)
    agent.db.set_meta(KILLED_KEY, "1")
    assert agent.publisher.run() == [(request, connectors.HALTED)]  # the start transaction reads the switch again
    agent.db.set_meta(KILLED_KEY, "0")
    path = json.loads(rows(agent, f"SELECT action FROM approvals WHERE id = {request}")[0]["action"])["files"][0][
        "path"
    ]
    agent.roots()[0].write_bytes(path, b"%PDF-1.7 something else")
    assert agent.execute_approved() == [(request, "failed")]
    assert f"{path} changed after you approved it" in listing_rows(agent)[0]["error"]
    assert agent.etsy.shop().state["listings"] == {}


def test_the_owner_cancels_a_listing_that_waits_for_etsy(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy)
    who = owner(agent)
    who.decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert who.close(request, {"outcome": "failed"}, "Owner").status == 200  # begun, but nothing made
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [] and agent.etsy.shop().state["listings"] == {}
    assert request_of(agent, request)["closed_by"] == "Owner"


def test_the_owner_s_park_of_its_line_stops_a_listing_that_waits_for_etsy(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, retry.LATER)]
    _, leg = line_of(agent)
    park(agent, leg)  # nothing was made, so it is held as an approval nothing began
    closed = request_of(agent, request)
    assert (closed["status"], closed["closed_by"]) == ("failed", "Ember's code")
    assert (
        closed["result_note"]
        == f"Not carried out: your owner parked venture #{leg} before Ember's code carried it out."
    )
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [] and agent.etsy.shop().state["listings"] == {}


def test_etsy_asking_to_wait_after_the_draft_exists_leaves_the_draft(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "upload_photo", etsy.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "draft")]  # something changed at Etsy: never tried again
    assert request_of(agent, request)["status"] == "failed"
    agent.clock.advance(hours=7)
    assert agent.execute_approved() == []


# --- M3: a change of a live listing ---------------------------------------------------------------------------------


def test_a_change_etsy_asks_to_wait_for_is_made_once_then(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    request = a_change(agent, shop_context(agent), listing_id, title="Weekly Planner, A4")
    calls = refuse(monkeypatch, FakeShop, "update_listing", etsy.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert views_approval(agent, request)["execution"]["status"] == "retrying"
    [change] = rows(agent, "SELECT status, listing FROM etsy_edits")
    assert change == {"status": "failed", "listing": None}
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "done")]
    assert len(calls) == 2 and agent.etsy.shop().state["listings"][str(listing_id)]["title"] == "Weekly Planner, A4"
    assert [e["status"] for e in journal(agent, request)] == ["failed", "done"]


def test_a_change_etsy_asks_to_wait_for_halfway_is_partial(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    request = a_change(agent, shop_context(agent), listing_id, title="Weekly Planner, A4", price="3.90")
    refuse(monkeypatch, FakeShop, "set_price", etsy.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "partial")]  # the title changed first: never tried again
    agent.clock.advance(hours=7)
    assert agent.execute_approved() == []


def test_an_unlock_s_change_isn_t_tried_again(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The unlock approves the agent's next proposal by itself; taking it back stops only what nothing began.
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200
    made = a_small_cut(agent, goal, listing_id)
    assert (made["status"], made["decided_by"]) == ("approved", policy.POLICY_BY)
    refuse(monkeypatch, FakeShop, "set_price", etsy.Busy, times=99)
    assert agent.execute_approved() == [(made["id"], "failed")]
    assert request_of(agent, made["id"])["status"] == "failed"


# --- M3: a pin and a post -------------------------------------------------------------------------------------------


def test_a_pin_pinterest_asks_to_wait_for_waits_for_its_slot_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, first = pinned(data_dir)
    assert a_pin(agent, pin_context(agent), board_id=BOARD, image="shop/pin-2.png", **OTHER_WORDS).ok
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    owner(agent).decide(second, {"decision": "approve"}, "Owner")
    slot_comes(agent, "pinterest")
    calls = refuse(monkeypatch, FakePinterest, "create_pin", pinterest.Busy)
    asked = agent.clock.now()
    assert agent.execute_approved() == [(second, retry.LATER)]
    assert pin_rows(agent)[-1] == {"approval_id": second, "status": "failed", "pin_id": None, "board_id": None,
                                   "error": f"HTTP 429{retry.said(429, WAIT)}"}  # fmt: skip
    slot = next_slot(agent, "pinterest")
    assert slot.approval_id == second and slot.at >= asked + timedelta(seconds=WAIT)
    shown = views_approval(agent, second)["execution"]
    assert shown["status"] == "retrying" and from_iso(shown["due_at"]) == slot.at
    with agent.db.connection() as conn:
        assert publish_queue.waiting(conn, agent.scope(), "pinterest") == [second]  # the agent proposes no repeat
        assert "Pinterest asked to wait" in publish_queue.text(conn, agent.scope(), agent.clock, "pinterest", 3)
    assert agent.execute_approved() == []
    slot_comes(agent, "pinterest")
    assert agent.execute_approved() == [(second, "active")]
    assert len(calls) == 2 and len(agent.pinterest.account().state["pins"]) == 2
    assert [e["status"] for e in journal(agent, second)] == ["failed", "simulated"]
    assert request_of(agent, first)["status"] == "done"


def test_a_pin_that_made_its_new_board_isn_t_tried_again(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, request = pin_proposed(data_dir)  # on a new board, made first
    refuse(monkeypatch, FakePinterest, "create_pin", pinterest.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "failed")]
    assert len(agent.pinterest.account().state["boards"]) == 1 and request_of(agent, request)["status"] == "failed"


def test_a_post_bluesky_asks_to_wait_for_is_posted_once_then(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, request = post_proposed(data_dir)
    calls = refuse(monkeypatch, FakeBluesky, "create_post", bluesky.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, retry.LATER)]
    [row] = post_rows(agent)
    assert (row["status"], row["sent"], row["rkey"]) == ("failed", 0, None)  # not counted toward the day's limit
    assert views_approval(agent, request)["execution"]["status"] == "retrying"
    assert agent.bluesky_posts.recover() == 0
    slot_comes(agent, "bluesky")
    assert agent.execute_approved() == [(request, "active")]
    assert len(calls) == 2 and len(agent.bluesky.account().state["posts"]) == 1
    assert post_rows(agent)[0]["sent"] == 1


def test_an_email_is_never_tried_again_after_a_refusal_for_now(
    data_dir: Path,
    imap: Any,
    smtp: Any,  # noqa: F811 - the fixtures
) -> None:
    agent, approval_id = live_approved(data_dir)
    smtp.fail = {"send": smtplib.SMTPRecipientsRefused({"ann@example.org": (450, b"4.2.1 try again later")})}
    assert agent.execute_approved() == [(approval_id, "failed")]
    smtp.fail = {}
    agent.clock.advance(hours=7)
    assert agent.execute_approved() == [] and len(smtp.instances) == 1
    assert approval(agent, approval_id)["status"] == "failed"


# --- M4: the files a card shows -------------------------------------------------------------------------------------


def test_a_card_says_which_file_changed_since_it_was_proposed(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    action = json.loads(rows(agent, f"SELECT action FROM approvals WHERE id = {request}")[0]["action"])
    frozen = [u["path"] for u in action["photos"] + action["files"]]
    shown = views_approval(agent, request)
    assert [f["path"] for f in shown["files"]] == frozen and all(f["unchanged"] is True for f in shown["files"])
    photo = action["photos"][0]["path"]
    agent.roots()[0].write_bytes(photo, picture(900, 900))
    changed = {f["path"]: f["unchanged"] for f in views_approval(agent, request)["files"]}
    assert changed[photo] is False and sum(1 for v in changed.values() if v is False) == 1
    agent.roots()[0].delete(action["files"][0]["path"])
    assert sum(1 for f in views_approval(agent, request)["files"] if f["unchanged"] is False) == 2
    owner(agent).decide(request, {"decision": "reject"}, "Owner")
    assert all(f["unchanged"] is None for f in views_approval(agent, request)["files"])  # closed: not read again


def test_a_pin_s_and_a_change_s_cards_say_it_too(data_dir: Path) -> None:
    agent, _, request = pin_proposed(data_dir)
    [image] = views_approval(agent, request)["files"]
    assert image == {"path": "shop/pin-1.png", "bytes": image["bytes"], "unchanged": True}
    agent.roots()[0].write_bytes("shop/pin-1.png", picture(1000, 1400))
    assert views_approval(agent, request)["files"][0]["unchanged"] is False
    change = a_change(agent, shop_context(agent), 900_000_001, photos="shop/new-1.png")
    assert [f["path"] for f in views_approval(agent, change)["files"]] == ["shop/new-1.png"]
    listing_request = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")[0]["id"]
    assert all(f["unchanged"] is None for f in views_approval(agent, listing_request)["files"])  # done: not read


def test_the_owner_can_t_approve_a_request_whose_file_changed(
    ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = ingress_client.app.state.ember
    agent = state.agent
    monkeypatch.setattr(state.scheduler, "poke", lambda: None)
    agent.transport = ScriptedTransport(simulated=True, outcomes=[plan(steps=[], sleep=600)])
    agent.meter = agent.economy.metered(agent.transport)
    agent.run_cycle("schedule")
    data = picture()
    agent.roots()[0].write_bytes("shop/pin-1.png", data)
    pin = Pin(
        "Planner", "A weekly planner.", etsy.listing_url(1), "", pinterest.image("shop/pin-1.png", data), 1000, 1500
    )
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        fields = {"type": "publish", "description": "d", "expected_cost": "none", "expected_benefit": "b"}
        made = [
            store.insert_approval(
                conn, agent.scope(), cycle, to_iso(agent.clock.now()), title=f"Pin {n}", payload=f"pin {n}",
                executor="pinterest_pin", action=store.canonical(pin.to_action()), **fields,
            )
            for n in (1, 2)
        ]  # fmt: skip
    agent.roots()[0].write_bytes("shop/pin-1.png", picture(1000, 1400))
    refused = post(ingress_client, f"api/approvals/{made[0]}/decide", {"decision": "approve"})
    assert refused.status_code == 409
    name = agent.settings.agent_name
    assert refused.json() == {
        "error": f"shop/pin-1.png changed since {name} proposed this: reject it and ask {name} to propose it again",
        "field": "decision",
    }
    card = next(a for a in ingress_client.get("api/dashboard").json()["approvals"] if a["id"] == made[0])
    assert card["status"] == "pending" and card["files"][0]["unchanged"] is False
    assert post(ingress_client, f"api/approvals/{made[0]}/decide", {"decision": "reject"}).status_code == 200
    agent.roots()[0].write_bytes("shop/pin-1.png", data)  # the bytes proposed again: it can be approved
    assert post(ingress_client, f"api/approvals/{made[1]}/decide", {"decision": "approve"}).status_code == 200


def test_the_dashboard_marks_a_changed_file_as_the_kdp_card_does() -> None:
    script = (paths.WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'changedMark(f, "check it, or reject and ask for it again")' in script  # the KDP card's, shared
    assert (
        "changedFilesBox(a, name)," in script
        and 'changed.map(function (f, i) { return [i ? h("br") : null, String(f.path), changedMark(f, advice)]; })'
        in script
    )
    for service in ("Etsy", "Pinterest", "Bluesky"):
        assert f'retrying: {{ icon: "◷", label: "{service} asked to wait", tone: "warning" }}' in script
        assert f'retryDetail(ex, "{service}")' in script
    assert 'ps === "scheduled" || ps === "retrying" ? [panelButton(it, "failed", "Cancel", true)]' in script
