"""0.13.0: the connector protocol and the QA registry. Each channel had its own executor and records, and nothing said
across them which actions reach people, cost money, publish under the owner's name or can be undone; the photo
thresholds disagreed (5 to 10, 3 or more, fewer than 5). Now every request has an action class with the same flags,
the email executor and the Etsy publisher write one shared journal (the state before and after, and what would undo
it) without changing what they do, and MIN_PHOTOS is the one photo number."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import tools  # noqa: E402
from app.integrations import connectors, etsy, qa  # noqa: E402
from tests.test_agent import make_agent, plan, rows, text  # noqa: E402  # noqa: E402
from tests.test_agent import tools as calls  # noqa: E402
from tests.test_etsy import a_change, listed, shop_context, views_approval  # noqa: E402
from tests.test_etsy_revenue import order, shop_with  # noqa: E402
from tests.test_executor import REPLY, approve, ids  # noqa: E402
from tests.test_executor import owner as owner_of  # noqa: E402
from tests.test_mail import JOURNAL, READER  # noqa: E402


def journal(agent: Any) -> list[dict[str, Any]]:
    return rows(agent, "SELECT approval_id, class, subject, status, before, after, undo, note FROM action_journal")


def test_every_request_has_an_action_class_and_its_flags() -> None:
    assert connectors.class_of("email").name == "email.send"
    assert connectors.class_of("etsy_listing").name == "etsy.create_listing"
    assert connectors.class_of("etsy_edit", {"listing_id": 1, "price": "3.90"}).name == "etsy.edit_listing"
    assert connectors.class_of("etsy_edit", json.dumps({"listing_id": 1, "state": "renew"})).name == "etsy.renew"
    assert connectors.class_of("etsy_edit", {"listing_id": 1, "state": "deactivate"}).name == "etsy.deactivate"
    assert connectors.class_of("reddit_link").name == "reddit.post"
    assert connectors.class_of("kdp_package").name == "kdp.publish"
    assert connectors.class_of(None, None, "create_account").name == "owner.create_account"
    assert connectors.class_of(None, None, "spend_money").name == "owner.spend_money"
    assert connectors.class_of(None, None, "publish").name == "owner.other"
    email, listing = connectors.CLASSES["email.send"], connectors.CLASSES["etsy.create_listing"]
    assert (email.reaches_people, email.first_contact, email.reversible) == (True, True, False)
    assert (listing.costs_money, listing.publishes_under_owner_identity, listing.undo) == (True, True, "deactivate it")
    for c in connectors.CLASSES.values():
        assert c.reversible == bool(c.undo) and c.connector == c.name.split(".")[0]
        # Ember's code can't carry those out (0.25.0: nor a KDP book: KDP has no API)
        assert c.by_owner == (c.connector in ("owner", "reddit", "kdp")), c.name


def test_the_email_executor_writes_the_shared_journal(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [plan(steps=["answer Lena"]), calls(("propose_email", REPLY)), text("Proposed an answer."), JOURNAL],
    )
    agent.run_cycle("schedule")
    [approval_id] = ids(agent)
    approve(agent, approval_id)
    assert agent.execute_approved() == [(approval_id, "simulated")]  # what it did is unchanged
    [entry] = journal(agent)
    # 0.13.0 (Phase E1): an answer in a thread the person started is its own class, never a first contact
    assert (entry["approval_id"], entry["class"], entry["status"]) == (approval_id, "email.reply", "simulated")
    assert entry["subject"] == READER  # what it acted on: the one recipient
    assert json.loads(entry["after"]) == {"status": "simulated"} and entry["undo"] is None  # an email can't be unsent
    shown = next(a for a in agent.dashboard()["approvals"] if a["id"] == approval_id)
    assert shown["action_class"]["name"] == "email.reply" and not shown["action_class"]["first_contact"]
    assert shown["qa"] == []  # it keeps the thread's subject and is short
    with pytest.raises(sqlite3.IntegrityError, match="a finished action is final"), agent.db.transaction() as conn:
        conn.execute("UPDATE action_journal SET note = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="rows cannot be deleted"), agent.db.transaction() as conn:
        conn.execute("DELETE FROM action_journal")


def test_etsy_actions_are_journaled_with_what_undoes_them(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    [created] = journal(agent)
    assert (created["class"], created["status"], created["subject"]) == ("etsy.create_listing", "done", str(listing_id))
    assert json.loads(created["after"]) == {"listing_id": listing_id, "status": "active"}
    assert json.loads(created["undo"]) == {"action": "deactivate", "listing_id": listing_id}
    ctx = shop_context(agent)
    request = a_change(agent, ctx, listing_id, price="3.90")
    assert owner_of(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    agent.execute_approved()
    changed = journal(agent)[1]
    assert (changed["class"], changed["status"], changed["subject"]) == ("etsy.edit_listing", "done", str(listing_id))
    assert json.loads(changed["before"])["price"] != json.loads(changed["after"])["price"] == "3.90"
    assert json.loads(changed["undo"]) == {"action": "restore", "listing_id": listing_id}  # to its before
    off = a_change(agent, ctx, listing_id, state="deactivate")
    assert owner_of(agent).decide(off, {"decision": "approve"}, "Owner").status == 200
    agent.execute_approved()
    assert (journal(agent)[2]["class"], json.loads(journal(agent)[2]["undo"])) == (
        "etsy.deactivate",
        {"action": "renew", "listing_id": listing_id},
    )
    # Ember's code's own action: a sold listing's automatic renewal, journaled without a request
    renew = a_change(agent, ctx, listing_id, state="renew")
    assert owner_of(agent).decide(renew, {"decision": "approve"}, "Owner").status == 200
    agent.execute_approved()
    assert journal(agent)[3]["class"] == "etsy.renew" and journal(agent)[3]["undo"] is None  # the fee is spent
    shop_with(agent, [order(agent, 5)])
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET auto_renew = 0, renew_set_at = NULL, state = 'active'")
    assert agent.publisher.sync(force=True) is None
    renewal = journal(agent)[-1]
    assert (renewal["approval_id"], renewal["class"], renewal["status"]) == (None, "etsy.auto_renew", "simulated")
    assert json.loads(renewal["undo"]) == {"action": "auto_renew_off", "listing_id": listing_id}


def test_one_photo_number_for_the_guide_the_checks_and_the_owner(data_dir: Path) -> None:
    assert qa.MIN_PHOTOS == 5 and qa.photo_defect(5) == ""
    assert qa.photo_defect(1) == f"1 photo, fewer than 5 (Etsy shows up to {etsy.MAX_PHOTOS})"
    guide = tools.guide_text("etsy")
    assert f"photos: {qa.MIN_PHOTOS} to {etsy.MAX_PHOTOS} listing photos" in guide and "{MIN_PHOTOS}" not in guide
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    request = a_change(agent, ctx, listing_id, photos="shop/p1.png, shop/p2.png")
    shown = views_approval(agent, request)
    assert shown["action_class"]["name"] == "etsy.edit_listing"
    assert shown["qa"] == [f"2 photos, fewer than 5 (Etsy shows up to {etsy.MAX_PHOTOS})"]
    listing = etsy.listing_from_action(
        rows(agent, "SELECT action FROM approvals WHERE executor = 'etsy_listing'")[0]["action"]
    )
    few = qa.defects("etsy.create_listing", listing)
    assert few == ([] if len(listing.photos) >= qa.MIN_PHOTOS else [qa.photo_defect(len(listing.photos))])
