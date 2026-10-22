"""0.39.0 (the agent's request): a live Printify product can be changed: a new design, title, description or tags.

propose_etsy_edit leaves the listings Printify made alone (Printify keeps them), and propose_printify_product only
creates products: a flaw the critic found in a live poster (a missing mockup, a claim the product no longer keeps)
could only be fixed by a new product, a second listing fee and a listing without the first one's views and favorites.
Now propose_printify_edit asks the owner to approve a change; Ember's code makes it at Printify (only what changes:
prices and variants stay) and has Printify publish what changed to the same listing, once. The critic, the plan and
etsy_listing read the product as Printify has it, changes included. The live client is tested against a mocked
Printify; nothing reaches Printify in a dry run.

Tests from 0.39.0 (formerly test_fixes_0390.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from app.agent import never, plan, quality, store, tools, ventures  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import connectors, etsy, printify, printify_publisher, qa  # noqa: E402
from app.integrations.printify import DISCLOSURE, Edit, NotSent, PrintifyError, Unclear, Upload  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.channels.test_printify import SMALL, a_product, live, picture, pod_context, proposed  # noqa: E402
from tests.etsy_helpers import call, views_approval  # noqa: E402

NEW_TITLE = "Minimalist mountain poster, matte print, made to order"
NEW_TAGS = "mountain poster, minimalist art, wall art, nordic decor"


def an_edit(**changes: Any) -> Edit:
    fields: dict[str, Any] = {"listing_id": 800_000_002, "product_id": "5d39b411749d0a000f30e0f4"}
    fields.update(changes)
    return Edit(**fields)


# --- what a change is, and the live client --------------------------------------------------------------------------


def test_a_change_names_only_what_changes_and_reads_back() -> None:
    upload = Upload("shop/poster-v2.png", "b" * 64, 4321)
    edit = an_edit(title="New title", tags=("a", "b"), image=upload, width=3600, height=5400)
    assert edit.parts() == ["design", "title", "tags"]
    assert printify.edit_from_action(json.dumps(edit.to_action())) == edit
    product = a_product()
    changed = edit.applied(product)
    assert (changed.title, changed.tags, changed.image, changed.width) == ("New title", ("a", "b"), upload, 3600)
    assert (changed.description, changed.prices) == (product.description, product.prices)  # the rest stays
    assert qa.defects("printify.edit_product", changed) == []
    for action, message in (
        ({"listing_id": 1, "product_id": "p"}, "changes nothing"),
        ({"product_id": "p", "title": "t"}, "isn't readable"),
        ("[]", "isn't readable"),
    ):
        with pytest.raises(PrintifyError, match=message):
            printify.edit_from_action(action if isinstance(action, str) else json.dumps(action))
    text = printify.edit_payload(an_edit(title="New title", description="Better words.", image=upload), product)
    assert text.startswith(
        "Printify product 5d39b411749d0a000f30e0f4, Etsy listing #800000002: Minimalist mountain poster\n"
        "Design: shop/poster-v2.png (0 x 0 pixels, about 0 dpi printed)\n"
        "  (was: shop/poster.png; Printify draws the listing's photos from the new design, in place of the ones it"
        " has)\n"
        "Title: New title\n  (was: Minimalist mountain poster)\n"
        "Prices and variants stay as they are. Printify publishes the change to the listing.\n\n"
        "New description:\nBetter words.\n\n"
    )
    assert text.endswith(DISCLOSURE)


def test_the_live_client_sends_only_what_changes_and_publishes_only_that() -> None:
    product_id = "5d39b411749d0a000f30e0f4"
    path = f"/v1/shops/77/products/{product_id}"
    account, server = live({("PUT", f"{path}.json"): {"id": product_id}, ("POST", f"{path}/publish.json"): {}})
    product = a_product(title="New title", area_width=3300, area_height=4200)
    account.update(77, product_id, product, ["title", "tags"])
    assert json.loads(server.requests[0].content) == {"title": "New title", "tags": list(product.tags)}
    account.publish(77, product_id, ["title", "tags"])
    assert json.loads(server.requests[1].content) == {
        "title": True,
        "description": False,
        "images": False,  # Printify's mockups only with a new design
        "variants": False,  # prices and variants never
        "tags": True,
    }
    account.update(77, product_id, product, ["design", "description"], "img2")
    assert json.loads(server.requests[2].content) == {
        "description": product.full_description(),
        "print_areas": [
            {
                "variant_ids": [SMALL],
                "placeholders": [
                    {
                        "position": "front",
                        "images": [{"id": "img2", "x": 0.5, "y": 0.5, "scale": product.scale(), "angle": 0}],
                    }
                ],
            }
        ],
    }
    account.publish(77, product_id, ["design"])
    assert json.loads(server.requests[3].content)["images"] is True
    with pytest.raises(NotSent, match="wasn't uploaded"):
        account.update(77, product_id, product, ["design"])
    with pytest.raises(NotSent, match="nothing changes"):
        account.update(77, product_id, product, [])
    assert len(server.requests) == 4  # neither was sent


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx2.Response(400, json={"message": "Product is locked"}), NotSent),
        (httpx2.Response(502, json={}), Unclear),
        (httpx2.ReadTimeout("slow"), Unclear),
    ],
)
def test_a_change_s_errors_say_whether_printify_may_have_made_it(answer: Any, error: type) -> None:
    account, _ = live({("PUT", "/v1/shops/77/products/p1.json"): answer})
    with pytest.raises(error):
        account.update(77, "p1", a_product(), ["title"])


# --- the agent's tool and Ember's code, with the dry run's fake account ---------------------------------------------


def live_product(data_dir: Path) -> tuple[Any, int, int, str]:
    """A dry-run agent with one product live in the shop: the agent, the product's request, its Etsy listing and its
    number at Printify."""
    agent, _, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    made = rows(agent, f"SELECT listing_id, product_id FROM printify_products WHERE approval_id = {request}")[0]
    return agent, request, int(made["listing_id"]), str(made["product_id"])


def change(agent: Any, listing_id: int, **args: Any) -> tools.Outcome:
    ctx = pod_context(agent)
    image = args.get("image")
    if image and agent.roots()[0].size_of(image) is None:
        agent.roots()[0].write_bytes(image, picture(2400, 3600))
    return call(ctx, "propose_printify_edit", {"listing_id": listing_id, "reason": "The critic flagged it.", **args})


def last_change(agent: Any) -> dict[str, Any]:
    return rows(agent, "SELECT * FROM approvals WHERE executor = 'printify_edit' ORDER BY id DESC LIMIT 1")[0]


def test_a_change_is_checked_and_waits_for_the_owner(data_dir: Path) -> None:
    agent, _, listing_id, product_id = live_product(data_dir)
    ctx = pod_context(agent)
    shown = call(ctx, "etsy_listing", {"listing_id": listing_id})
    assert shown.ok and "propose_printify_edit changes them" in shown.text
    assert "Title: Minimalist mountain poster, matte print\n" in shown.text  # in full: what a change starts from
    assert "Design: shop/poster.png (2000 x 3000 pixels" in shown.text
    refused = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "title": NEW_TITLE, "reason": "Better."})
    assert not refused.ok and "propose_printify_edit" in refused.text
    for args, message in (
        ({"listing_id": 123}, "#123 isn't one of your live Printify products"),
        ({}, "nothing changes"),
        ({"title": "Minimalist mountain poster, matte print"}, "nothing changes"),  # as it is now
        ({"image": "shop/poster.png"}, "nothing changes"),  # the same design
        ({"tags": ",".join(f"tag{i}" for i in range(14))}, "at most 13"),
        ({"image": "shop/missing.png", "write": False}, "missing.png"),
    ):
        if args.pop("write", True) is False:
            answer = call(ctx, "propose_printify_edit", {"listing_id": listing_id, "reason": "r", **args})
        else:
            answer = change(agent, int(args.pop("listing_id", listing_id)), **args)
        assert not answer.ok and message in answer.text, (args, answer.text)
    made = change(agent, listing_id, title=NEW_TITLE, tags=NEW_TAGS, image="shop/poster-v2.png")
    assert made.ok, made.text
    assert "it changes the design, title, tags of #" in made.text and "Nothing has changed at Printify yet" in made.text
    request = last_change(agent)
    assert (request["type"], request["status"], request["title"]) == (
        "sell",
        "pending",
        "Change Printify product: Minimalist mountain poster, matte print",
    )
    action = json.loads(request["action"])
    assert (action["listing_id"], action["product_id"], action["title"]) == (listing_id, product_id, NEW_TITLE)
    assert (action["width"], action["height"]) == (2400, 3600)
    assert "Prices and variants stay as they are" in request["payload"]
    assert store.EXECUTOR_TYPES["printify_edit"] == "sell"
    with agent.db.connection() as conn:
        assert never.reasons(conn, request) == ["owner_only"]  # never on an unlock: it waits for the owner
        assert connectors.class_of("printify_edit").name == "printify.edit_product"
        assert ventures.request_line(conn, agent.scope(), request) == ventures.listing_project(
            conn, agent.scope(), listing_id
        )
    again = change(agent, listing_id, title="Another title for the mountain poster")
    assert not again.ok and f"request #{request['id']} already changes #{listing_id}" in again.text
    shown = call(pod_context(agent), "etsy_listing", {"listing_id": listing_id})
    assert f"Request #{request['id']} changes it and hasn't been made yet." in shown.text
    who = owner(agent)
    edited = who.decide(request["id"], {"decision": "approve_with_changes", "final_payload": "Title: mine"}, "Owner")
    assert edited.status == 422  # as it was proposed, like a product
    view = views_approval(agent, request["id"])
    assert view["action_class"]["name"] == "printify.edit_product" and view["qa"] == []
    assert view["never"] == [] and view["execution"] is None  # no unlock's rule fits it


def test_an_approved_change_is_made_once_and_the_listing_keeps_its_number(data_dir: Path) -> None:
    agent, _, listing_id, product_id = live_product(data_dir)
    assert change(agent, listing_id, title=NEW_TITLE, description="A calm line drawing, now on heavier paper.").ok
    request = last_change(agent)["id"]
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert views_approval(agent, request)["execution"]["status"] == "waiting"
    assert agent.execute_approved() == [(request, "done")]
    assert agent.execute_approved() == []  # never twice
    account = agent.printify.account()
    item = account.state["products"][product_id]
    assert (item["title"], item["listing_id"], item["changes"]) == (NEW_TITLE, listing_id, 1)  # the same listing
    [made] = rows(
        agent, f"SELECT status, product_id, listing_id, result FROM printify_edits WHERE approval_id = {request}"
    )
    assert (made["status"], made["product_id"], made["listing_id"]) == ("done", product_id, listing_id)
    closed = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {request}")[0]
    assert (closed["status"], closed["closed_by"], closed["result_link"]) == (
        "done",
        "Ember",
        etsy.listing_url(listing_id),
    )
    assert "fake Printify account" in closed["result_note"]
    [entry] = rows(
        agent, f"SELECT class, status, subject, before, undo FROM action_journal WHERE approval_id = {request}"
    )
    assert (entry["class"], entry["status"], entry["subject"], entry["undo"]) == (
        "printify.edit_product",
        "simulated",
        product_id,
        None,  # changed back by another change of the agent's
    )
    assert json.loads(entry["before"])["title"] == "Minimalist mountain poster, matte print"
    assert views_approval(agent, request)["execution"]["status"] == "done"
    # Ember's records, the critic and etsy_listing read the product as it is now.
    assert rows(agent, "SELECT title FROM printify_products")[0]["title"] == NEW_TITLE
    with agent.db.connection() as conn:
        found = printify_publisher.current_product(conn, agent.scope(), listing_id)
        assert found is not None and found[1].title == NEW_TITLE
        assert found[1].description == "A calm line drawing, now on heavier paper."
        project = ventures.listing_project(conn, agent.scope(), listing_id)
        case, _ = quality.case(conn, agent.scope(), agent.roots()[0], int(project or 0), listing_id)
    assert f"Title: {NEW_TITLE}\n" in case and "now on heavier paper" in case
    shown = call(pod_context(agent), "etsy_listing", {"listing_id": listing_id})
    assert NEW_TITLE in shown.text and "hasn't been made yet" not in shown.text
    # The next change starts from this one.
    assert not change(agent, listing_id, title=NEW_TITLE).ok


def test_a_new_design_is_exactly_the_file_approved(data_dir: Path) -> None:
    agent, _, listing_id, product_id = live_product(data_dir)
    assert change(agent, listing_id, image="shop/poster-v2.png").ok
    request = last_change(agent)["id"]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.roots()[0].write_bytes("shop/poster-v2.png", picture(1800, 2700))  # changed after the approval
    assert agent.execute_approved() == [(request, "failed")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert "changed after it was approved: propose the change again" in note
    assert "image_id" not in agent.printify.account().state["products"][product_id]  # nothing was sent
    assert change(agent, listing_id, image="shop/poster-v3.png").ok
    second = last_change(agent)["id"]
    owner(agent).decide(second, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(second, "done")]
    item = agent.printify.account().state["products"][product_id]
    assert item["image_id"] in agent.printify.account().state["images"]
    with agent.db.connection() as conn:
        found = printify_publisher.current_product(conn, agent.scope(), listing_id)
    assert found is not None and (found[1].image.path, found[1].width) == ("shop/poster-v3.png", 2400)


def test_a_change_printify_took_but_didn_t_publish_is_partial(data_dir: Path, monkeypatch: Any) -> None:
    agent, _, listing_id, product_id = live_product(data_dir)
    assert change(agent, listing_id, title=NEW_TITLE).ok
    request = last_change(agent)["id"]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")

    def refused(*_: Any, **__: Any) -> None:
        raise NotSent("HTTP 400: Product is locked")

    monkeypatch.setattr(agent.printify.account(), "publish", refused)
    assert agent.execute_approved() == [(request, "partial")]
    closed = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {request}")[0]
    assert closed["status"] == "failed" and "didn't publish it to the Etsy listing" in closed["result_note"]
    assert "publish it at Printify" in closed["result_note"]
    # Printify has it: the record's title and the next change start from it.
    assert rows(agent, "SELECT title FROM printify_products")[0]["title"] == NEW_TITLE
    [entry] = rows(agent, f"SELECT status FROM action_journal WHERE approval_id = {request}")
    assert entry["status"] == "partial"


def test_a_refused_or_unclear_change_changes_nothing_in_ember_s_records(data_dir: Path, monkeypatch: Any) -> None:
    agent, _, listing_id, _ = live_product(data_dir)
    account = agent.printify.account()
    assert change(agent, listing_id, title=NEW_TITLE).ok
    request = last_change(agent)["id"]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")

    def gone(*_: Any, **__: Any) -> None:
        raise NotSent("HTTP 400: Validation failed.")

    monkeypatch.setattr(account, "update", gone)
    assert agent.execute_approved() == [(request, "failed")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert note == "Not changed: Printify refused it (HTTP 400: Validation failed.)"
    assert rows(agent, "SELECT title FROM printify_products")[0]["title"] == "Minimalist mountain poster, matte print"
    assert change(agent, listing_id, tags=NEW_TAGS).ok  # nothing waits any more
    second = last_change(agent)["id"]
    owner(agent).decide(second, {"decision": "approve"}, "Owner")

    def lost(*_: Any, **__: Any) -> None:
        raise Unclear("the connection to Printify broke (ReadTimeout)")

    monkeypatch.setattr(account, "update", lost)
    assert agent.execute_approved() == [(second, "unclear")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {second}")[0]["result_note"]
    assert "It is unclear what Printify changed" in note and "check the product at Printify" in note


def test_a_change_a_crash_interrupted_is_unclear_and_never_made_again(data_dir: Path, monkeypatch: Any) -> None:
    agent, _, listing_id, _ = live_product(data_dir)
    assert change(agent, listing_id, title=NEW_TITLE).ok
    request = last_change(agent)["id"]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")

    def crash(*_: Any, **__: Any) -> None:
        raise KeyboardInterrupt  # the app stops in the middle

    monkeypatch.setattr(agent.printify.account(), "update", crash)
    with pytest.raises(KeyboardInterrupt):
        agent.execute_approved()
    assert rows(agent, f"SELECT status FROM printify_edits WHERE approval_id = {request}")[0]["status"] == "running"
    assert owner(agent).close(request, {"outcome": "failed"}, "Owner").status == 409  # it is under way
    monkeypatch.undo()
    assert agent.pod.recover() == 1
    assert rows(agent, f"SELECT status FROM printify_edits WHERE approval_id = {request}")[0]["status"] == "unclear"
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {request}")[0]["status"] == "failed"
    assert agent.execute_approved() == []


def test_a_product_gone_from_the_shop_isn_t_changed(data_dir: Path) -> None:
    agent, _, listing_id, product_id = live_product(data_dir)
    assert change(agent, listing_id, title=NEW_TITLE).ok
    request = last_change(agent)["id"]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.printify.account().delete(4242, product_id)  # deleted at Printify by hand
    assert agent.pod.sync(force=True) is None
    assert agent.execute_approved() == [(request, "failed")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert note == f"Not changed: product {product_id} isn't live in the shop as Etsy listing #{listing_id} anymore"
    assert not change(agent, listing_id, title="Another title").ok


def test_the_critic_s_fixes_count_a_change_at_printify(data_dir: Path) -> None:
    assert "printify_edit" in plan.CHECK_EXECUTORS["critic"]
    agent, _, listing_id, _ = live_product(data_dir)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        project = ventures.listing_project(conn, scope, listing_id)
        quality.save(
            conn,
            scope,
            int(project or 0),
            None,
            {"score": 5, "verdict": "improve", "fixes": "Say the size in the title."},
            None,
            to_iso(agent.clock.now()),
            listing_id,
        )
        check = conn.execute("SELECT * FROM quality_checks ORDER BY id DESC LIMIT 1").fetchone()
        assert not quality.changed_since(conn, scope, check)
    agent.clock.advance(hours=1)
    assert change(agent, listing_id, title=NEW_TITLE).ok
    request = last_change(agent)["id"]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    with agent.db.connection() as conn:
        assert quality.changed_since(conn, scope, check)  # checked again next: its fixes may be made
