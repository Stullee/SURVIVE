"""0.42.3: the kill switch pressed while a round is under way, for the executors 0.37.3's tests left out (the audit of
what was left after the codebase analysis of 0.40.1): Bluesky posts, Pinterest pins, the blog's posts and link page,
and the owner's Undos.

Every executor reads connectors.halted again before it begins an item (0.37.3): what began runs on, once; the next
item doesn't begin, nothing more reaches the service, its card says it waits and the journal has nothing of it; once
the switch is reset it goes out. test_approval_timing has the emails, the Etsy changes and the live page;
test_approval_boundary the Etsy listing's start; Printify's products are tested apart."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.agent.owner import apply_kill_switch_reset  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.integrations import connectors, etsy, pinterest_publisher  # noqa: E402
from app.integrations.pinterest import FakePinterest  # noqa: E402
from app.products import images  # noqa: E402
from tests.agent_helpers import ROOMY, owner, rows, run  # noqa: E402
from tests.channels.test_approval_timing import press_kill  # noqa: E402
from tests.channels.test_blog import blog_context, post_source, proposed, published  # noqa: E402
from tests.channels.test_bluesky import a_post, post_context, post_rows  # noqa: E402
from tests.channels.test_bluesky import proposed as post_proposed  # noqa: E402
from tests.channels.test_pinterest import LISTING, a_pin, pin_context, pin_rows  # noqa: E402
from tests.channels.test_pinterest import proposed as pin_proposed  # noqa: E402
from tests.etsy_helpers import a_change, call, shop_context, views_approval  # noqa: E402

BOTH = ROOMY.model_copy(update={"pinterest_enabled": True, "bluesky_enabled": True})
POST_PAGE, INDEX, LINKS = "blog/bewerbung-nachfassen.html", "blog/index.html", "links.html"
OTHER_PAGE = "blog/anderer-beitrag.html"
LINK_PAGE = {
    "bio": "Praktische Vorlagen für Bewerbung und Geld.",
    "links": [
        {"label": "Bewerbungs-Tracker", "url": "https://www.etsy.com/listing/1"},
        {"label": "Blog", "url": "/blog/"},
    ],
    "reason": "The new listing first.",
}


def press_during(monkeypatch: pytest.MonkeyPatch, agent: Agent, target: Any, name: str) -> None:
    """The owner presses the kill switch while ``target.name`` runs the first time (a fake service's call, or the
    making of a picture); the call goes on."""
    original: Callable[..., Any] = getattr(target, name)
    pressed: list[bool] = []

    def pressing(*args: Any, **kwargs: Any) -> Any:
        if not pressed:
            pressed.append(True)
            press_kill(agent)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, pressing)


def watch(monkeypatch: pytest.MonkeyPatch, fake: Any, *names: str) -> list[tuple[str, ...]]:
    """What reaches a fake service from now on: each call of ``names``, with the words it was given (a path, an id)."""
    seen: list[tuple[str, ...]] = []
    for name in names:
        original: Callable[..., Any] = getattr(fake, name)

        def recorded(*args: Any, _name: str = name, _original: Callable[..., Any] = original, **kwargs: Any) -> Any:
            seen.append((_name, *(a for a in args if isinstance(a, str))))
            return _original(*args, **kwargs)

        monkeypatch.setattr(fake, name, recorded)
    return seen


def newest(agent: Agent, executor: str) -> int:
    return int(rows(agent, f"SELECT MAX(id) AS id FROM approvals WHERE executor = '{executor}'")[0]["id"])


def approve(agent: Agent, request: int) -> None:
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200


def journal(agent: Agent, request: int) -> list[dict[str, Any]]:
    return rows(agent, f"SELECT class, status FROM action_journal WHERE approval_id = {request}")


def waits(agent: Agent, request: int) -> None:
    """The request isn't begun: still approved, its card says it waits, and the journal has nothing of it."""
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {request}") == [{"status": "approved"}]
    assert views_approval(agent, request)["execution"]["status"] == "waiting"
    assert journal(agent, request) == []


def undo_of(agent: Agent, journal_class: str) -> int:
    """The owner's Undo of Ember's newest action of this class (approved at once, carried out in the next round)."""
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == journal_class)
    reply = owner(agent).undo(entry["id"], "Stefan")
    assert reply.status == 200, reply.body
    return int(reply.body["approval_id"])


def pinning_and_posting(data_dir: Path) -> Agent:
    """A dry-run agent with Pinterest and Bluesky on (their fake accounts) and one live listing in the fake shop."""
    agent, _ = run(data_dir, FakeTransport(), cycles=4, settings=BOTH, brake=False)  # the fourth cycle's listing
    listing = newest(agent, "etsy_listing")
    approve(agent, listing)
    assert agent.execute_approved() == [(listing, "active")]
    return agent


def a_pin_and_a_post(agent: Agent) -> tuple[int, int]:
    """An approved pin and an approved post of the listing, each the first in its queue: both due now."""
    made = a_pin(agent, pin_context(agent), board_name="Meal planning printables")
    assert made.ok, made.text
    pin = newest(agent, "pinterest_pin")
    made = a_post(agent, post_context(agent))
    assert made.ok, made.text
    post = newest(agent, "bluesky_post")
    approve(agent, pin)
    approve(agent, post)
    return pin, post


# --- Bluesky posts -------------------------------------------------------------------------------------------------


def test_the_kill_switch_pressed_while_a_pin_goes_out_keeps_the_post_of_that_round_off_bluesky(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = pinning_and_posting(data_dir)
    pin, post = a_pin_and_a_post(agent)
    apply_kill_switch_reset(agent.db, agent.economy, 0)  # the option's value at the start
    press_during(monkeypatch, agent, agent.pinterest.account(), "create_pin")
    account = agent.bluesky.account()
    sent = watch(monkeypatch, account, "create_post", "delete_post")
    assert agent.execute_approved() == [(pin, "active"), (post, connectors.HALTED)]
    assert pin_rows(agent)[0]["status"] == "active"  # what began ran on, once
    assert sent == [] and account.state["posts"] == {} and post_rows(agent) == []  # the post wasn't begun
    waits(agent, post)
    assert agent.execute_approved() == []  # the next rounds: nothing while the switch is on
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(post, "active")]  # the owner approved it
    assert sent == [("create_post",)] and len(account.state["posts"]) == 1
    assert journal(agent, post) == [{"class": "bluesky.create_post", "status": "simulated"}]


def test_the_kill_switch_pressed_while_a_post_s_pictures_are_made_keeps_it_off_bluesky(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its pictures are made outside any transaction (a moment's work): the start transaction reads the switch again."""
    agent, _, post = post_proposed(data_dir, image="shop/post-1.png", alt_text="Ein Wochenplaner auf dem Tisch")
    approve(agent, post)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    press_during(monkeypatch, agent, images, "within")
    account = agent.bluesky.account()
    sent = watch(monkeypatch, account, "create_post")
    assert agent.execute_approved() == [(post, connectors.HALTED)]
    assert sent == [] and account.state["posts"] == {} and post_rows(agent) == []
    waits(agent, post)
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(post, "active")]
    assert sent == [("create_post",)] and post_rows(agent)[0]["status"] == "active"


# --- Pinterest pins ------------------------------------------------------------------------------------------------


def test_the_kill_switch_pressed_while_an_etsy_change_is_made_keeps_the_pin_of_that_round_off_pinterest(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, pin = pin_proposed(data_dir)
    change = a_change(agent, shop_context(agent), LISTING, price="3.90")
    approve(agent, change)
    approve(agent, pin)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    press_during(monkeypatch, agent, agent.etsy.shop(), "set_price")
    account = agent.pinterest.account()
    sent = watch(monkeypatch, account, "create_board", "create_pin", "delete_pin")
    assert agent.execute_approved() == [(change, "done"), (pin, connectors.HALTED)]
    assert agent.etsy.shop().state["listings"][str(LISTING)]["price_cents"] == 390  # type: ignore[union-attr]
    assert sent == [] and account.state["boards"] == {} and account.state["pins"] == {}  # no board, no pin
    assert pin_rows(agent) == []
    waits(agent, pin)
    assert agent.execute_approved() == []
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(pin, "active")]
    assert [call[0] for call in sent] == ["create_board", "create_pin"] and len(account.state["pins"]) == 1


def test_the_kill_switch_pressed_while_a_pin_goes_out_keeps_the_sandbox_s_test_pin_of_that_round_unmade(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.30.2: the test pin of the owner's Standard access request comes last in the pins' round, in the sandbox."""
    agent, _, pin = pin_proposed(data_dir)
    approve(agent, pin)
    account, sandbox = agent.pinterest.account(), FakePinterest(agent.clock, None, lambda state: None)
    workspace = agent.roots()[0]
    publisher = pinterest_publisher.Publisher(
        agent.db, agent.clock, agent.settings, agent.scope, lambda: account, lambda: workspace, lambda: sandbox
    )  # the owner's account, and the sandbox they connected with the sandbox option (test_pinterest.sandboxed)
    test = publisher.request_test()
    assert test is not None
    approve(agent, test)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    press_during(monkeypatch, agent, account, "create_pin")
    sent = watch(monkeypatch, sandbox, "create_board", "create_pin")
    assert publisher.run() == [(pin, "active"), (test, connectors.HALTED)]
    assert sent == [] and sandbox.state["boards"] == {} and sandbox.state["pins"] == {}
    waits(agent, test)
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert publisher.run() == [(test, "active")]
    assert [call[0] for call in sent] == ["create_board", "create_pin"]
    assert journal(agent, test) == [{"class": "pinterest.test_pin", "status": "done"}]


# --- the blog: posts and the link page ------------------------------------------------------------------------------


def test_the_kill_switch_pressed_while_a_post_goes_up_keeps_the_link_page_of_that_round_off_the_server(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, post = proposed(data_dir)
    made = call(blog_context(agent), "propose_link_page", LINK_PAGE)
    assert made.ok, made.text
    links = newest(agent, "site_links")
    approve(agent, post)
    approve(agent, links)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    server = agent.blog.fake
    assert server is not None
    press_during(monkeypatch, agent, server, "write")  # the owner presses it as the post's page goes up
    sent = watch(monkeypatch, server, "read", "write", "remove")
    assert agent.execute_approved() == [(post, "done"), (links, connectors.HALTED)]
    # The post began: it went up with the blog's list, once. The link page's turn: the server isn't even read.
    assert sent == [("read", POST_PAGE), ("read", INDEX), ("write", POST_PAGE), ("write", INDEX)]
    assert LINKS not in server.files and POST_PAGE in server.files
    assert rows(agent, f"SELECT status FROM site_uploads WHERE approval_id = {links}") == [{"status": "proposed"}]
    waits(agent, links)
    assert agent.execute_approved() == []
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(links, "done")]
    assert sent[4:] == [("read", LINKS), ("write", LINKS)] and b"Bewerbungs-Tracker" in server.files[LINKS]


def test_the_kill_switch_pressed_while_the_server_is_read_for_a_post_keeps_it_off_the_server(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post reads its page and the blog's list on the owner's server first, outside any transaction: the start
    transaction reads the switch again."""
    agent, post = proposed(data_dir)
    approve(agent, post)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    server = agent.blog.fake
    assert server is not None
    press_during(monkeypatch, agent, server, "read")
    sent = watch(monkeypatch, server, "write", "remove")
    assert agent.execute_approved() == [(post, connectors.HALTED)]
    assert sent == [] and server.files == {}
    assert rows(agent, f"SELECT status FROM site_uploads WHERE approval_id = {post}") == [{"status": "proposed"}]
    waits(agent, post)
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(post, "done")]
    assert sent == [("write", POST_PAGE), ("write", INDEX)]


# --- the owner's Undos -----------------------------------------------------------------------------------------------


def test_the_kill_switch_pressed_during_the_first_undo_of_a_paused_round_stops_the_undos_after_it(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the agent is paused a round carries out only the owner's Undos (0.15.0): the Etsy listing's first, then
    the pin's and the post's."""
    agent = pinning_and_posting(data_dir)
    pin, post = a_pin_and_a_post(agent)
    assert agent.execute_approved() == [(pin, "active"), (post, "active")]
    pinterest, bluesky = agent.pinterest.account(), agent.bluesky.account()
    [pin_id], [rkey] = list(pinterest.state["pins"]), list(bluesky.state["posts"])
    unlist = undo_of(agent, "etsy.create_listing")
    unpin = undo_of(agent, "pinterest.create_pin")
    unpost = undo_of(agent, "bluesky.create_post")
    agent.economy.set_paused(True)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    press_during(monkeypatch, agent, agent.etsy.shop(), "set_state")  # as Etsy takes the listing off
    unpinned = watch(monkeypatch, pinterest, "delete_pin")
    unposted = watch(monkeypatch, bluesky, "delete_post")
    assert agent.execute_approved() == [(unlist, "done"), (unpin, connectors.HALTED), (unpost, connectors.HALTED)]
    listing = agent.etsy.shop().state["listings"][str(LISTING)]  # type: ignore[union-attr]
    assert listing["state"] == etsy.STATES["deactivate"]  # what began ran on, once
    assert unpinned == unposted == []  # neither Undo began
    assert list(pinterest.state["pins"]) == [pin_id] and list(bluesky.state["posts"]) == [rkey]
    for request in (unpin, unpost):
        waits(agent, request)
    assert agent.execute_approved() == []  # the next rounds: nothing while the switch is on
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.executor_blocked() == "The agent is paused"  # still paused: its Undos only
    assert agent.execute_approved() == [(unpin, "done"), (unpost, "done")]
    assert unpinned == [("delete_pin", pin_id)] and unposted == [("delete_post", rkey)]
    assert pinterest.state["pins"] == {} and bluesky.state["posts"] == {}


def test_the_kill_switch_pressed_while_a_post_goes_up_stops_the_blog_s_undo_after_it(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, first = proposed(data_dir)
    agent.roots()[0].write("blog/other.md", post_source(slug="anderer-beitrag"))
    assert call(blog_context(agent), "propose_blog_post", {"source": "blog/other.md", "reason": "More."}).ok
    second = newest(agent, "site_post")
    approve(agent, first)
    assert agent.execute_approved() == [(first, "done")]
    undo = undo_of(agent, "site.publish_post")  # after the second post in the round: its request is newer
    approve(agent, second)
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    server = agent.blog.fake
    assert server is not None
    press_during(monkeypatch, agent, server, "write")  # as the second post's page goes up
    sent = watch(monkeypatch, server, "read", "write", "remove")
    assert agent.execute_approved() == [(second, "done"), (undo, connectors.HALTED)]
    assert sent == [("read", OTHER_PAGE), ("read", INDEX), ("write", OTHER_PAGE), ("write", INDEX)]  # the Undo: none
    assert POST_PAGE in server.files  # the first post stays up
    waits(agent, undo)
    assert rows(agent, f"SELECT * FROM site_uploads WHERE approval_id = {undo}") == []
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(undo, "done")]
    assert sent[4:] == [("read", POST_PAGE), ("read", INDEX), ("remove", POST_PAGE), ("write", INDEX)]
    assert POST_PAGE not in server.files and OTHER_PAGE in server.files


def test_the_kill_switch_pressed_while_the_server_is_read_for_the_blog_s_undo_changes_nothing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Undo reads the page on the owner's server first (is it still the one Ember's code uploaded?), outside any
    transaction: the start transaction reads the switch again."""
    agent, _ = published(data_dir)
    undo = undo_of(agent, "site.publish_post")
    apply_kill_switch_reset(agent.db, agent.economy, 0)
    server = agent.blog.fake
    assert server is not None
    before = dict(server.files)
    press_during(monkeypatch, agent, server, "read")
    sent = watch(monkeypatch, server, "write", "remove")
    assert agent.execute_approved() == [(undo, connectors.HALTED)]
    assert sent == [] and server.files == before
    waits(agent, undo)
    assert apply_kill_switch_reset(agent.db, agent.economy, 1)
    assert agent.execute_approved() == [(undo, "done")]
    assert sent == [("remove", POST_PAGE), ("write", INDEX)] and POST_PAGE not in server.files
