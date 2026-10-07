"""0.13.0 (Phase E2): Pinterest. The agent proposes a pin (one of its pictures, linking to one of its live Etsy
listings, on one of its boards or a new one); the owner approves it (the pin that makes the first board is a new public
presence: never automatic); Ember's code makes the board and the pin, once, journaled, and the owner's Undo deletes it;
the sync reads the pins' numbers for the plan and the metrics. The one-time connection and the live client are tested
against a mocked Pinterest; nothing reaches Pinterest in a dry run."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from PIL import Image

httpx2 = pytest.importorskip("httpx2")

from app.agent import audit, metrics, never, stages, tools  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind  # noqa: E402
from app.config import Settings  # noqa: E402
from app.economy.clock import Clock, to_iso  # noqa: E402
from app.integrations import etsy, pinterest, qa  # noqa: E402
from app.integrations.pinterest import (  # noqa: E402
    DISCLOSURE,
    NotSent,
    Pin,
    PinterestError,
    TokenFile,
    Tokens,
    Unclear,
    Upload,
)
from app.integrations.pinterest_connection import PinterestConnection  # noqa: E402
from app.integrations.pinterest_live import LiveAccount, _Allowlist, connect  # noqa: E402
from app.logging_setup import redact  # noqa: E402
from app.products import images  # noqa: E402
from tests.test_agent import ROOMY, rows  # noqa: E402
from tests.test_etsy import Etsy, call, shop_context, views_approval  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

PINNING = ROOMY.model_copy(update={"pinterest_enabled": True})
LIVE = Settings(
    pinterest_enabled=True,
    pinterest_app_id="1234567",
    pinterest_app_secret="p1nterest-app-s3cret",
    pinterest_redirect_uri="https://localhost/ember-pinterest",
)
LISTING = 900_000_001  # the fake shop's first listing
BOARD, PIN = "1000000001", "1000000002"  # the fake account's first numbers


def picture(width: int = 1000, height: int = 1500) -> bytes:
    return images.png(Image.new("RGB", (width, height), (240, 230, 220)))


def listed(data_dir: Path) -> tuple[Any, FakeTransport]:
    """A dry-run agent with Pinterest on (the fake account) and one live listing in the fake shop."""
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=4, settings=PINNING)
    request = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")[0]["id"]
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    return agent, fake


def pin_context(agent: Any) -> tools.ToolContext:
    ctx = shop_context(agent)
    ctx.pinterest = tools.PinterestAccess("ember-dry-run", 3)
    return ctx


def a_pin(agent: Any, ctx: tools.ToolContext, write: bool = True, **args: Any) -> tools.Outcome:
    """A proposed pin; its picture (2:3) is made first unless ``write`` is False or it exists."""
    image = str(args.pop("image", "shop/pin-1.png"))
    if write and agent.roots()[0].size_of(image) is None:
        agent.roots()[0].write_bytes(image, picture())
    fields = {
        "listing_id": LISTING,
        "image": image,
        "title": "Weekly meal planner printable",
        "description": "Plan a week of meals on one page: shopping list included. Print it at home.",
        "alt_text": "A printed weekly meal planner on a kitchen table",
        "reason": "Pinterest searchers look for meal planners; this sends them to the listing.",
        **args,
    }
    return call(ctx, "propose_pin", {k: v for k, v in fields.items() if v is not None})


def proposed(data_dir: Path, **args: Any) -> tuple[Any, FakeTransport, int]:
    agent, fake = listed(data_dir)
    made = a_pin(agent, pin_context(agent), board_name="Meal planning printables", **args)
    assert made.ok, made.text
    return agent, fake, rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]


def pinned(data_dir: Path) -> tuple[Any, FakeTransport, int]:
    agent, fake, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    return agent, fake, request


def pin_rows(agent: Any) -> list[dict[str, Any]]:
    return rows(agent, "SELECT approval_id, status, pin_id, board_id, error FROM pinterest_pins ORDER BY id")


# --- what a pin may be ------------------------------------------------------------------------------------------


def test_a_pin_s_image_and_words_are_checked() -> None:
    made = pinterest.image("shop/pin.png", b"\x89PNG data")
    assert made == Upload("shop/pin.png", hashlib.sha256(b"\x89PNG data").hexdigest(), 9)
    for path, data, message in (
        ("shop/pin.pdf", b"x", "a .png or .jpg file"),
        ("shop/pin.jpeg", b"x", "a .png or .jpg file"),  # the workspace's pictures are .png and .jpg
        ("shop/pin.png", b"", "is empty"),
        ("shop/pin.png", b"x" * (pinterest.IMAGE_MAX_BYTES + 1), "larger than 10 MB"),
    ):
        with pytest.raises(PinterestError, match=message):
            pinterest.image(path, data)
    assert pinterest.one_line(" Meal\nplanner\tprintable ") == "Meal planner printable"
    pin = Pin("Planner", "Plan your week.", "https://www.etsy.com/listing/1", "", made, 1000, 1500, board_id="7")
    assert pin.full_description() == f"Plan your week.\n\n{DISCLOSURE}"
    assert len(pin.full_description()) <= pinterest.DESCRIPTION_MAX
    assert pinterest.DESCRIPTION_CHARS + len(DISCLOSURE) + 2 == pinterest.DESCRIPTION_MAX
    assert pinterest.pin_from_action(json.dumps(pin.to_action())) == pin
    with pytest.raises(PinterestError, match="isn't readable"):
        pinterest.pin_from_action('{"title": "no image"}')
    assert qa.defects("pinterest.create_pin", pin) == []
    square = Pin("Planner", "x", "l", "", made, 2400, 2400)
    assert qa.defects("pinterest.create_pin", square) == [
        "2400 x 2400 pixels: a pin shows best portrait, about 2:3 (1000 x 1500)"
    ]
    assert images.SHAPES["pin"] == (2000, 3000)  # make_image's shape for pins: 2:3


def test_the_authorize_link_uses_pkce_and_the_pasted_address_is_checked() -> None:
    url = pinterest.authorize_url(LIVE, "st4te", "ch4llenge")
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.pinterest.com", "/oauth/")
    assert query == {
        "response_type": "code",
        "client_id": "1234567",
        "redirect_uri": "https://localhost/ember-pinterest",
        "scope": "boards:read,boards:write,pins:read,pins:write,user_accounts:read",
        "state": "st4te",
        "code_challenge": "ch4llenge",
        "code_challenge_method": "S256",
    }
    good = "https://localhost/ember-pinterest?code=abc123&state=st4te"
    assert pinterest.code_from(f"  {good} ", LIVE, "st4te") == "abc123"
    for pasted, message in (
        ("https://evil.example/ember-pinterest?code=abc&state=st4te", "paste the whole address"),
        ("https://localhost/ember-pinterest?code=abc&state=other", "another attempt"),
        ("https://localhost/ember-pinterest?error=access_denied&state=st4te", "Pinterest said: access_denied"),
        ("https://localhost/ember-pinterest?state=st4te", "has no code"),
    ):
        with pytest.raises(PinterestError, match=message):
            pinterest.code_from(pasted, LIVE, "st4te")
    assert pinterest.config_problems(LIVE) == []
    assert pinterest.config_problems(Settings(pinterest_redirect_uri="http://localhost/cb")) == [
        "pinterest_app_id is missing",
        "pinterest_app_secret is missing",
        "pinterest_redirect_uri must be an https address",
    ]


def some_tokens(clock: Clock, *, expires: timedelta = timedelta(hours=1)) -> Tokens:
    now = clock.now()
    return Tokens(
        access_token="pina_acc3ss-t0ken-value",
        refresh_token="pinr_r3fresh-t0ken-value",
        expires_at=to_iso(now + expires),
        refresh_expires_at=to_iso(now + timedelta(days=365)),
        username="plannershop",
        connected_at=to_iso(now),
    )


def test_tokens_are_kept_private(tmp_path: Path) -> None:
    store = TokenFile(tmp_path / "pinterest" / "tokens.json")
    assert store.load() is None
    tokens = some_tokens(Clock())
    store.save(tokens)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.load() == tokens
    assert redact(f"token {tokens.access_token} and {tokens.refresh_token}") == "token *** and ***"
    store.clear()
    store.clear()  # twice is fine
    assert store.load() is None and not store.path.exists()


# --- the live client, against a mocked Pinterest -----------------------------------------------------------------


def mock(answers: dict[tuple[str, str], Any]) -> tuple[Etsy, Any]:
    server = Etsy(answers)  # answers requests like an API would, and records them
    return server, httpx2.MockTransport(server)


def test_connecting_exchanges_the_code_and_reads_the_account(tmp_path: Path) -> None:
    server, transport = mock(
        {
            ("POST", "/v5/oauth/token"): {
                "access_token": "pina_fresh-access",
                "refresh_token": "pinr_fresh-refresh",
                "expires_in": 2_592_000,
                "token_type": "bearer",
            },
            ("GET", "/v5/user_account"): {"username": "plannershop", "account_type": "BUSINESS"},
        }
    )
    store = TokenFile(tmp_path / "tokens.json")
    clock = Clock()
    info = connect(LIVE, clock, store, "the-code", "the-verifier", transport)
    assert (info.username, info.url) == ("plannershop", "https://www.pinterest.com/plannershop/")
    assert server.form(0) == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "https://localhost/ember-pinterest",
        "code_verifier": "the-verifier",
        "continuous_refresh": "true",
    }
    basic = base64.b64encode(b"1234567:p1nterest-app-s3cret").decode()
    assert server.requests[0].headers["authorization"] == f"Basic {basic}"  # the app's id and secret
    assert server.requests[1].headers["authorization"] == "Bearer pina_fresh-access"
    assert all(r.url.host == "api.pinterest.com" for r in server.requests)
    saved = store.load()
    assert saved is not None and (saved.username, saved.refresh_token) == ("plannershop", "pinr_fresh-refresh")
    assert redact("secret p1nterest-app-s3cret") == "secret ***"


def live_account(tmp_path: Path, answers: dict[tuple[str, str], Any], **changes: Any) -> tuple[LiveAccount, Etsy]:
    server, transport = mock(answers)
    clock = Clock()
    store = TokenFile(tmp_path / "tokens.json")
    tokens = some_tokens(clock)
    for key, value in changes.items():
        setattr(tokens, key, value)
    store.save(tokens)
    return LiveAccount(LIVE, clock, store, transport), server


def test_a_board_and_a_pin_are_made_read_and_deleted(tmp_path: Path) -> None:
    account, server = live_account(
        tmp_path,
        {
            ("POST", "/v5/boards"): {"id": "5550001", "name": "Meal planning printables"},
            ("POST", "/v5/pins"): {"id": "7770001"},
            ("GET", "/v5/pins/7770001"): {
                "id": "7770001",
                "pin_metrics": {"lifetime_metrics": {"impression": 420, "save": 7, "outbound_click": 12}},
            },
            ("DELETE", "/v5/pins/7770001"): httpx2.Response(204),
        },
    )
    board = account.create_board("Meal planning printables", "")
    assert board == pinterest.Board("5550001", "Meal planning printables")
    assert json.loads(server.requests[0].content) == {
        "name": "Meal planning printables",
        "description": "",
        "privacy": "PUBLIC",
    }
    data = picture(10, 15)
    upload = pinterest.image("shop/pin.png", data)
    pin = Pin("Planner", "Plan your week.", "https://www.etsy.com/listing/1", "A planner", upload, 10, 15)
    assert account.create_pin(board.board_id, pin, data) == "7770001"
    sent = json.loads(server.requests[1].content)
    assert sent == {
        "board_id": "5550001",
        "title": "Planner",
        "description": f"Plan your week.\n\n{DISCLOSURE}",
        "link": "https://www.etsy.com/listing/1",
        "alt_text": "A planner",
        "media_source": {
            "source_type": "image_base64",
            "content_type": "image/png",
            "data": base64.b64encode(data).decode(),
        },
    }
    assert account.pin_stats("7770001") == pinterest.PinStats(impressions=420, saves=7, clicks=12)
    assert server.requests[2].url.params["pin_metrics"] == "true"
    account.delete_pin("7770001")
    assert all(r.headers["authorization"] == "Bearer pina_acc3ss-t0ken-value" for r in server.requests)


def test_an_expiring_token_is_renewed_and_a_refused_renewal_asks_to_connect_again(tmp_path: Path) -> None:
    renew = {"access_token": "pina_renewed", "refresh_token": "pinr_renewed", "expires_in": 2_592_000}
    account, server = live_account(
        tmp_path,
        {("POST", "/v5/oauth/token"): renew, ("GET", "/v5/user_account"): {"username": "plannershop"}},
        expires_at=to_iso(Clock().now() + timedelta(minutes=2)),
    )
    assert account.info().username == "plannershop"
    assert server.form(0) == {"grant_type": "refresh_token", "refresh_token": "pinr_r3fresh-t0ken-value"}
    assert server.requests[1].headers["authorization"] == "Bearer pina_renewed"
    saved = account.tokens.load()
    assert saved is not None and (saved.access_token, saved.refresh_token) == ("pina_renewed", "pinr_renewed")
    account.info()
    assert len(server.requests) == 3  # renewed once
    refused, _ = live_account(
        tmp_path / "again",
        {("POST", "/v5/oauth/token"): httpx2.Response(401, json={"message": "Invalid refresh token"})},
        expires_at=to_iso(Clock().now()),
    )
    with pytest.raises(NotSent, match="didn't renew the connection .* connect it again"):
        refused.info()


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx2.Response(400, json={"message": "Invalid link"}), NotSent),
        (httpx2.Response(404, json={"message": "Board not found"}), pinterest.Gone),
        (httpx2.Response(503, json={}), Unclear),
        (httpx2.ReadTimeout("slow"), Unclear),
        (httpx2.ConnectError("down"), NotSent),
    ],
)
def test_errors_say_whether_pinterest_may_have_made_the_pin(tmp_path: Path, answer: Any, error: type) -> None:
    account, _ = live_account(tmp_path, {("POST", "/v5/pins"): answer})
    upload = pinterest.image("shop/pin.png", b"png")
    with pytest.raises(error):
        account.create_pin("1", Pin("t", "d", "l", "", upload, 10, 15), b"png")


def test_only_pinterest_can_be_reached() -> None:
    allow = _Allowlist(retries=0)
    for url in ("https://example.com/v5/pins", "http://api.pinterest.com/v5/pins", "https://api.pinterest.com:8443/"):
        with pytest.raises(httpx2.ConnectError, match="only talks to"):
            allow.handle_request(httpx2.Request("GET", url))


def test_the_owner_connects_their_account_in_two_steps(data_dir: Path, tmp_path: Path) -> None:
    from app.db import Database, migrate  # noqa: PLC0415

    _, transport = mock(
        {
            ("POST", "/v5/oauth/token"): {"access_token": "pina_a", "refresh_token": "pinr_r", "expires_in": 3600},
            ("GET", "/v5/user_account"): {"username": "plannershop"},
        }
    )
    db = Database(tmp_path / "ember.db")
    migrate(db.path)
    tokens = TokenFile(tmp_path / "pinterest" / "tokens.json")
    clock = Clock()
    connection = PinterestConnection(db, clock, LIVE, "live", 1, tokens, transport)
    assert connection.status() == ("not_connected", "Connect your account: System, Pinterest, Connect.")
    assert connection.account() is None and connection.username() is None
    with pytest.raises(PinterestError, match="start connecting again"):
        connection.finish("https://localhost/ember-pinterest?code=c&state=s")
    query = parse_qs(urlsplit(connection.start()).query)
    state = query["state"][0]
    with pytest.raises(PinterestError, match="another attempt"):
        connection.finish("https://localhost/ember-pinterest?code=c&state=other")
    info = connection.finish(f"https://localhost/ember-pinterest?code=c&state={state}")
    assert info.username == "plannershop" and connection.username() == "plannershop"
    assert connection.status() == ("ok", None) and isinstance(connection.account(), LiveAccount)
    shown = connection.describe()
    assert (shown["mode"], shown["status"], shown["username"]) == ("live", "ok", "plannershop")
    assert "pina_a" not in json.dumps(shown) and "pinr_r" not in json.dumps(shown)
    connection.disconnect()
    assert connection.status()[0] == "not_connected" and not tokens.path.exists()
    off = PinterestConnection(db, clock, LIVE.model_copy(update={"pinterest_enabled": False}), "live", 1, tokens)
    assert off.status() == ("disabled", None) and off.account() is None
    with pytest.raises(PinterestError, match="switch Pinterest on"):
        off.start()
    db.close()


# --- the agent's tools, with the dry run's fake account ------------------------------------------------------------


def test_the_pinterest_tools_come_with_the_account_and_a_shop() -> None:
    assert {"pinterest_boards", "propose_pin"} == tools.PINTEREST_TOOLS
    assert {d["name"] for d in tools.definitions(etsy=True, pinterest=True)} >= tools.PINTEREST_TOOLS
    for kinds in ({"etsy": True}, {"pinterest": True}, {"etsy": True, "pinterest": True, "venture": True}):
        assert not {d["name"] for d in tools.definitions(**kinds)} & tools.PINTEREST_TOOLS
    assert "pinterest" in tools.GUIDES
    guide = tools.guide_text("pinterest")
    assert f"at most {pinterest.TITLE_MAX} characters" in guide and "{" not in guide


def test_a_pin_is_checked_before_it_reaches_the_owner(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = pin_context(agent)
    shown = call(ctx, "pinterest_boards", {})
    assert shown.ok and "Your owner's Pinterest account: ember-dry-run (at most 3 pins a day)." in shown.text
    assert "No board or pin of yours yet: your first board waits for your owner's decision." in shown.text
    agent.roots()[0].write_bytes("shop/notes.pdf", b"%PDF-1.7 x")
    agent.roots()[0].write_bytes("shop/broken.png", b"not a picture")
    for args, message in (
        ({"listing_id": 12, "board_name": "Planners"}, "#12 isn't one of your live listings"),
        ({}, "give board_id (one of your boards) or board_name"),
        ({"board_id": "1", "board_name": "Planners"}, "give board_id (one of your boards) or board_name"),
        ({"board_id": "999"}, "999 isn't one of your boards"),
        ({"board_name": "Planners", "image": "shop/notes.pdf"}, "a pin's image is a .png or .jpg file"),
        ({"board_name": "Planners", "image": "shop/broken.png"}, "isn't a PNG or JPEG picture"),
        ({"board_name": "Planners", "image": "shop/missing.png", "write": False}, "missing.png"),
        ({"board_name": "Planners", "title": " \n "}, "title is empty"),
    ):
        refused = a_pin(agent, ctx, **args)
        assert not refused.ok and message in refused.text, (args, refused.text)
    assert rows(agent, "SELECT id FROM approvals WHERE executor = 'pinterest_pin'") == []
    agent.roots()[0].write_bytes("shop/square.png", picture(2400, 2400))
    made = a_pin(agent, ctx, board_name="Meal  planning\nprintables", image="shop/square.png")
    assert made.ok, made.text
    assert "Nothing is on Pinterest yet" in made.text and "the board 'Meal planning printables' first" in made.text
    assert "QA (Ember's code): 2400 x 2400 pixels: a pin shows best portrait" in made.text
    [request] = rows(agent, "SELECT * FROM approvals WHERE executor = 'pinterest_pin'")
    assert (request["type"], request["title"]) == ("publish", "Pin: Weekly meal planner printable")
    action = json.loads(request["action"])
    assert action["link"] == etsy.listing_url(LISTING) and action["board_name"] == "Meal planning printables"
    assert (action["width"], action["height"], action["board_id"]) == (2400, 2400, None)
    assert request["payload"].startswith(
        "Board: Meal planning printables (a new board: Ember's code makes it first)\n"
        "Title: Weekly meal planner printable\nLink: https://www.etsy.com/listing/900000001\n"
        "Image: shop/square.png (2400 x 2400 pixels)\n"
    )
    assert request["payload"].endswith(DISCLOSURE)
    with agent.db.connection() as conn:
        assert never.reasons(conn, request) == ["first_publication"]  # the first board: the owner's decision
    shown = views_approval(agent, request["id"])
    assert shown["qa"] and shown["execution"] is None and shown["executor"] == "pinterest_pin"


def test_an_approved_pin_makes_its_board_first_and_is_never_made_twice(data_dir: Path) -> None:
    agent, _, request = pinned(data_dir)
    account = agent.pinterest.account()
    assert list(account.state["boards"]) == [BOARD] and list(account.state["pins"]) == [PIN]
    assert account.state["pins"][PIN]["link"] == etsy.listing_url(LISTING)
    assert pin_rows(agent) == [
        {"approval_id": request, "status": "active", "pin_id": PIN, "board_id": BOARD, "error": None}
    ]
    assert rows(agent, "SELECT status, board_id, name FROM pinterest_boards") == [
        {"status": "active", "board_id": BOARD, "name": "Meal planning printables"}
    ]
    closed = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {request}")[0]
    assert (closed["status"], closed["closed_by"]) == ("done", "Ember")
    assert "fake account" in closed["result_note"] and closed["result_link"] == pinterest.pin_url(PIN)
    journal = rows(agent, f"SELECT class, status, subject, undo FROM action_journal WHERE approval_id = {request}")
    assert [(j["class"], j["status"], j["subject"]) for j in journal] == [
        ("pinterest.create_board", "simulated", "Meal planning printables"),
        ("pinterest.create_pin", "simulated", PIN),
    ]
    assert json.loads(journal[1]["undo"]) == {"action": "delete_pin", "pin_id": PIN}
    assert agent.execute_approved() == []  # never twice
    shown = views_approval(agent, request)
    assert shown["execution"]["status"] == "active" and shown["execution"]["url"] == pinterest.pin_url(PIN)
    # The next pin goes on the board: no longer a first publication.
    ctx = pin_context(agent)
    same = a_pin(agent, ctx, board_name="meal planning PRINTABLES", image="shop/pin-2.png")
    assert not same.ok and f"give its board_id, {BOARD}" in same.text
    assert a_pin(agent, ctx, board_id=BOARD, image="shop/pin-2.png").ok
    [second] = rows(agent, f"SELECT * FROM approvals WHERE executor = 'pinterest_pin' AND id > {request}")
    with agent.db.connection() as conn:
        assert never.reasons(conn, second) == []


def test_two_pins_naming_the_same_new_board_share_it(data_dir: Path) -> None:
    agent, _, first = proposed(data_dir)
    assert a_pin(agent, pin_context(agent), board_name="Meal planning printables", image="shop/pin-2.png").ok
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    for request in (first, second):
        assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(first, "active"), (second, "active")]
    assert len(agent.pinterest.account().state["boards"]) == 1
    assert {r["board_id"] for r in pin_rows(agent)} == {BOARD}


def test_the_owner_s_undo_deletes_the_pin(data_dir: Path) -> None:
    agent, _, request = pinned(data_dir)
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "pinterest.create_pin")
    assert entry["undo"] == {"label": "Delete the pin", "why_not": None, "request": None}
    board = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "pinterest.create_board")
    assert board["undo"]["label"] is None and board["undo"]["why_not"] == "Ember's code can't undo it"
    reply = owner(agent).undo(entry["id"], "Stefan")
    assert reply.status == 200, reply.body
    undo = int(reply.body["approval_id"])
    made = rows(agent, f"SELECT executor, status, decided_by, action FROM approvals WHERE id = {undo}")[0]
    assert (made["executor"], made["status"], made["decided_by"]) == ("pinterest_delete", "approved", "Stefan")
    assert json.loads(made["action"]) == {"pin_id": PIN}
    shown = views_approval(agent, undo)
    assert shown["execution"]["status"] == "waiting" and shown["never"] == []
    done = owner(agent).close(undo, {"outcome": "done"}, "Stefan")
    assert done.status == 422 and "carries approved pins out itself" in done.body["error"]
    assert agent.execute_approved() == [(undo, "done")]
    assert agent.pinterest.account().state["pins"] == {}
    assert pin_rows(agent)[0]["status"] == "deleted"
    assert views_approval(agent, undo)["execution"]["status"] == "deleted"
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "pinterest.create_pin")
    assert entry["undo"]["why_not"] == "it is undone"
    with pytest.raises(audit.Refused, match="it is undone"), agent.db.transaction() as conn:
        audit.undo(conn, agent.scope(), to_iso(agent.clock.now()), entry["id"], "Stefan")


def test_a_pin_deleted_at_pinterest_is_noted_and_the_others_are_still_read(data_dir: Path) -> None:
    agent, _, first = pinned(data_dir)
    assert a_pin(agent, pin_context(agent), board_id=BOARD, image="shop/pin-2.png").ok
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    owner(agent).decide(second, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(second, "active")]
    agent.pinterest.account().delete_pin(PIN)  # the owner deleted the first pin at Pinterest
    agent.clock.advance(hours=24 * 3)
    assert agent.pins.sync(force=True) is None
    made = rows(agent, "SELECT approval_id, status, clicks, result FROM pinterest_pins ORDER BY id")
    assert [(r["approval_id"], r["status"]) for r in made] == [(first, "deleted"), (second, "active")]
    assert made[0]["result"] == "Deleted at Pinterest, not by Ember's code" and made[1]["clicks"] == 1
    entry = next(
        e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "pinterest.create_pin" and e["subject"] == PIN
    )
    assert entry["undo"]["why_not"] == "the pin isn't on Pinterest anymore"


def test_the_undo_of_a_pin_gone_already_is_done(data_dir: Path) -> None:
    agent, _, _ = pinned(data_dir)
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "pinterest.create_pin")
    undo = int(owner(agent).undo(entry["id"], "Stefan").body["approval_id"])
    agent.pinterest.account().delete_pin(PIN)  # meanwhile, by hand at Pinterest
    assert agent.execute_approved() == [(undo, "done")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {undo}")[0]["result_note"]
    assert note == f"Pin {PIN} was gone from Pinterest already" and pin_rows(agent)[0]["status"] == "deleted"


def test_a_changed_image_and_the_daily_limit(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.settings = agent.pins.settings = agent.settings.model_copy(update={"pinterest_pins_per_day": 0})
    assert agent.execute_approved() == [(request, "waiting_limit")]
    assert views_approval(agent, request)["execution"]["status"] == "waiting_limit"
    agent.settings = agent.pins.settings = agent.settings.model_copy(update={"pinterest_pins_per_day": 3})
    agent.roots()[0].write_bytes("shop/pin-1.png", picture(1000, 1400))
    assert agent.execute_approved() == [(request, "failed")]
    [row] = pin_rows(agent)
    assert row["pin_id"] is None and "changed after it was approved" in row["error"]
    assert agent.pinterest.account().state["boards"] == {}  # nothing reached Pinterest


def test_a_crash_while_pinning_is_unclear_and_never_repeated(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO pinterest_pins (mode, session, approval_id, title, link, status, started_at)"
            " VALUES (?, ?, ?, 'Planner', 'https://www.etsy.com/listing/1', 'running', '2026-09-01T10:00:00Z')",
            (scope.mode, scope.session, request),
        )
    assert agent.pins.recover() == 1
    assert pin_rows(agent)[0]["status"] == "unclear"
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {request}")[0]["status"] == "failed"
    assert agent.execute_approved() == []


def test_the_owner_approves_a_pin_as_it_is_or_cancels_it(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    who = owner(agent)
    changed = who.decide(request, {"decision": "approve_with_changes", "final_payload": "Title: mine"}, "Owner")
    assert changed.status == 422 and "approve a pin as it is" in changed.body["error"]
    assert who.decide(request, {"decision": "approve"}, "Owner").status == 200
    done = who.close(request, {"outcome": "done"}, "Owner")
    assert done.status == 422 and "to stop one, cancel it" in done.body["error"]
    assert who.close(request, {"outcome": "failed"}, "Owner").status == 200
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert note == "Cancelled by the owner before Ember's code carried it out"
    assert agent.execute_approved() == [] and pin_rows(agent) == []


def test_pins_are_read_for_the_plan_and_the_metrics(data_dir: Path) -> None:
    agent, fake, _ = pinned(data_dir)
    agent.clock.advance(hours=24 * 7)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    [row] = rows(agent, "SELECT impressions, saves, clicks, synced_at FROM pinterest_pins")
    assert (row["impressions"], row["saves"], row["clicks"]) == (280, 3, 2) and row["synced_at"]
    plan = next(r for r in list(fake.sent)[before:] if request_kind(r) == "plan")
    text = plan["messages"][0]["content"][0]["text"]
    assert "\n== PINTEREST ==\nYour owner's account: ember-dry-run (at most 3 pins a day).\n" in text
    assert f"Boards: Meal planning printables ({BOARD})" in text
    assert (
        f"- pin {PIN} (active): Weekly meal planner printable -> {etsy.listing_url(LISTING)}: 280 impressions" in text
    )
    work = next(r for r in list(fake.sent)[before:] if request_kind(r) == "work")
    assert {"propose_pin", "pinterest_boards"} <= {t["name"] for t in work["tools"]}
    now = to_iso(agent.clock.now())
    with agent.db.connection() as conn:
        for name, value in (("pins_live", 1), ("pin_clicks", 2)):
            row = {"metric": name, "project_id": None, "venture_id": None, "created_at": now}
            assert metrics.read(conn, agent.scope(), row, None, now).value == value  # type: ignore[arg-type]
            assert metrics.CATALOGUE[name].source == "pinterest"
    shown = agent.integrations()["pinterest"]
    assert (shown["mode"], shown["status"], shown["username"]) == ("fake", "ok", "ember-dry-run")
    assert shown["boards"] == [{"board_id": BOARD, "name": "Meal planning printables"}]
    assert shown["pins"][0]["clicks"] == 2 and shown["last_sync_at"]


def test_the_pinterest_venture_s_first_test_is_its_pins_clicks(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=PINNING)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        venture = conn.execute("SELECT * FROM ventures WHERE channel = 'pinterest'").fetchone()
        assert venture is not None and venture["title"] == "Pinterest for the Etsy shop"
        milestone = stages.first_test(conn, scope, venture, agent.clock.today(), to_iso(agent.clock.now()))
        row = conn.execute("SELECT metric, target, measure FROM milestones WHERE id = ?", (milestone,)).fetchone()
    assert (row["metric"], row["target"]) == ("pin_clicks", 10)
    assert row["measure"] == stages.CHANNEL_TESTS["pinterest"][2]


def test_the_dashboard_shows_nothing_while_pinterest_is_off(ingress_client: TestClient) -> None:
    csrf = {"X-Ember-Request": "1"}
    refused = ingress_client.post("api/pinterest/connect", headers=csrf)
    assert refused.status_code == 422 and "fake account is always connected" in refused.json()["error"]
    assert ingress_client.post("api/pinterest/finish", json={}, headers=csrf).status_code == 422
    shown = ingress_client.get("api/dashboard").json()["integrations"]["pinterest"]
    assert (shown["status"], shown["boards"], shown["pins"]) == ("disabled", [], [])


def test_nothing_is_offered_or_made_while_pinterest_is_off(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1)
    assert agent.pinterest.account() is None and agent.pinterest.username() is None
    work = next(r for r in fake.sent if request_kind(r) == "work")
    assert not {t["name"] for t in work["tools"]} & tools.PINTEREST_TOOLS


def test_the_approvals_take_a_new_channel_s_executor_but_only_a_plain_name(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1)
    scope = agent.scope()
    insert = (
        "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description, payload,"
        " payload_sha256, expected_cost, expected_benefit, executor, action) VALUES (?, ?, ?, 1, 'now', 'sell',"
        " 't', 'd', ?, 'h', 'c', 'b', ?, '{}')"
    )
    for bad in ("Shop-ify", "shop ify", "x" * 41, ""):
        with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(insert, (scope.mode, scope.session, scope.life_id, f"p {bad}", bad))
    with agent.db.transaction() as conn:
        conn.execute(insert, (scope.mode, scope.session, scope.life_id, "p printify", "printify_order"))
        row = conn.execute("SELECT * FROM approvals WHERE executor = 'printify_order'").fetchone()
        assert never.reasons(conn, row) == ["owner_only"]  # no rule covers it yet: only the owner


# --- 0.30.1: Pinterest's API sandbox, for the video of the owner's Standard access request -------------------------

SANDBOX = LIVE.model_copy(update={"pinterest_sandbox": True})


def test_the_sandbox_is_reached_at_its_own_host(tmp_path: Path) -> None:
    server, transport = mock(
        {
            ("POST", "/v5/oauth/token"): {"access_token": "pina_s", "refresh_token": "pinr_s", "expires_in": 3600},
            ("GET", "/v5/user_account"): {"username": "plannershop"},
        }
    )
    connect(SANDBOX, Clock(), TokenFile(tmp_path / "tokens.json"), "the-code", "the-verifier", transport)
    assert [r.url.host for r in server.requests] == ["api-sandbox.pinterest.com"] * 2
    assert (pinterest.api_host(SANDBOX), pinterest.api_host(LIVE)) == ("api-sandbox.pinterest.com", "api.pinterest.com")
    sandbox = _Allowlist(pinterest.SANDBOX_HOST, retries=0)
    with pytest.raises(httpx2.ConnectError, match="only talks to https://api-sandbox.pinterest.com"):
        sandbox.handle_request(httpx2.Request("GET", "https://api.pinterest.com/v5/pins"))


def test_the_sandbox_connects_apart_and_leaves_the_agent_without_pinterest(tmp_path: Path) -> None:
    from app.agent.loop import _waiting  # noqa: PLC0415
    from app.db import Database, migrate  # noqa: PLC0415

    server, transport = mock(
        {
            ("POST", "/v5/oauth/token"): {"access_token": "pina_s", "refresh_token": "pinr_s", "expires_in": 3600},
            ("GET", "/v5/user_account"): {"username": "plannershop"},
        }
    )
    db = Database(tmp_path / "ember.db")
    migrate(db.path)
    clock = Clock()
    own = TokenFile(tmp_path / "pinterest" / "tokens.json")
    own.save(some_tokens(clock))  # the account's own connection, made before
    asked: list[str] = []
    connection = PinterestConnection(db, clock, SANDBOX, "live", 1, own, transport, lambda: asked.append("test pin"))
    state, reason = connection.status()
    assert state == "sandbox" and "connect your account" in str(reason)
    assert connection.account() is None and connection.sandbox_account() is None
    line = _waiting("Pinterest", connection.status(), "ok")
    assert "waits for your owner's setup (Pinterest's API sandbox" in line and "no Pinterest tools" in line
    state = parse_qs(urlsplit(connection.start()).query)["state"][0]
    connection.finish(f"https://localhost/ember-pinterest?code=c&state={state}")
    assert asked == ["test pin"]  # the owner's test pin, once connected
    assert all(r.url.host == "api-sandbox.pinterest.com" for r in server.requests)
    sandbox = connection.tokens
    assert sandbox.path == tmp_path / "pinterest" / "sandbox_tokens.json"
    saved = sandbox.load()
    assert saved is not None and saved.access_token == "pina_s"
    assert own.load() == some_tokens(clock)  # untouched
    assert connection.account() is None and connection.username() is None  # the agent has no Pinterest
    assert isinstance(connection.sandbox_account(), LiveAccount)
    assert connection.status()[0] == "sandbox" and "waits for your approval" in str(connection.status()[1])
    shown = connection.describe()
    assert (shown["mode"], shown["username"]) == ("sandbox", "plannershop") and shown["connected_at"]
    connection.disconnect()
    assert sandbox.load() is None and own.load() is not None
    for mode, settings in (("live", LIVE), ("dry_run", SANDBOX)):  # off, or a dry run (its fake): no sandbox
        other = PinterestConnection(db, clock, settings, mode, 1, own)
        assert not other.sandbox and other.sandbox_account() is None and other.account() is not None
    db.close()


def sandboxed(data_dir: Path) -> tuple[Any, Any, pinterest.FakePinterest]:
    """A dry-run agent with a live listing, and a pin publisher whose sandbox account is a fake one."""
    from app.integrations import pinterest_publisher  # noqa: PLC0415

    agent, _ = listed(data_dir)
    account = pinterest.FakePinterest(agent.clock, None, lambda state: None)
    publisher = pinterest_publisher.Publisher(
        agent.db, agent.clock, agent.settings, agent.scope, lambda: None, lambda: agent.roots()[0], lambda: account
    )
    return agent, publisher, account


def test_the_sandbox_s_test_pin_waits_for_the_owner_and_is_made_once(data_dir: Path) -> None:
    from app.integrations import pinterest_publisher  # noqa: PLC0415

    agent, publisher, account = sandboxed(data_dir)
    made = publisher.request_test()
    assert made is not None and publisher.request_test() == made  # one at a time
    row = rows(agent, f"SELECT * FROM approvals WHERE id = {made}")[0]
    assert (row["executor"], row["status"]) == ("pinterest_test_pin", "pending")
    assert row["title"].startswith("Test pin in Pinterest's sandbox: ")
    pin = pinterest.pin_from_action(row["action"])
    assert pin.link == etsy.listing_url(LISTING) and pin.board_id is None
    assert str(pin.board_name).startswith(pinterest_publisher.TEST_BOARD)
    assert agent.roots()[0].read_bytes(pin.image.path)  # the listing's own picture
    with agent.db.connection() as conn:
        assert never.reasons(conn, row) == ["owner_only"]  # never on an unlock
    assert publisher.run() == []  # it waits for the owner
    changed = {"decision": "approve_with_changes", "final_payload": "other words"}
    assert owner(agent).decide(made, changed, "Owner").status == 422  # as it is, or not at all
    assert owner(agent).decide(made, {"decision": "approve"}, "Owner").status == 200
    assert views_approval(agent, made)["execution"]["status"] == "waiting"
    assert publisher.run() == [(made, "active")]
    assert publisher.run() == []  # never twice
    [(pin_id, made_pin)] = account.state["pins"].items()
    [board] = account.state["boards"].values()
    assert made_pin["link"] == pin.link and board["name"] == pin.board_name
    closed = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {made}")[0]
    assert (closed["status"], closed["closed_by"]) == ("done", "Ember")
    assert closed["result_link"] == pinterest.pin_url(pin_id)
    assert "Pinterest's sandbox (only you see it)" in closed["result_note"]
    journal = rows(agent, f"SELECT class, status, subject, undo FROM action_journal WHERE approval_id = {made}")
    assert journal == [{"class": "pinterest.test_pin", "status": "done", "subject": pin_id, "undo": None}]
    shown = views_approval(agent, made)["execution"]
    assert (shown["status"], shown["url"]) == ("active", pinterest.pin_url(pin_id))
    # None of Ember's pins: no row, no number, no board for the agent's pins.
    assert rows(agent, "SELECT COUNT(*) AS n FROM pinterest_pins")[0]["n"] == 0
    assert rows(agent, "SELECT COUNT(*) AS n FROM pinterest_boards")[0]["n"] == 0
    # Another take: a new test pin, made on a board of its own.
    again = publisher.request_test()
    assert again is not None and again != made


def test_a_test_pin_isn_t_made_with_the_sandbox_off_or_after_a_crash(data_dir: Path) -> None:
    from app.integrations import connectors, pinterest_publisher  # noqa: PLC0415

    agent, publisher, account = sandboxed(data_dir)
    first = publisher.request_test()
    assert first is not None and owner(agent).decide(first, {"decision": "approve"}, "Owner").status == 200
    production = pinterest.FakePinterest(agent.clock, None, lambda state: None)
    off = pinterest_publisher.Publisher(
        agent.db, agent.clock, agent.settings, agent.scope, lambda: production, lambda: agent.roots()[0]
    )
    assert off.run() == [(first, "failed")]  # the sandbox was turned off before it was made
    note = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {first}")[0]
    assert note["status"] == "failed" and "the sandbox is off" in note["result_note"]
    assert production.state["pins"] == {} and account.state["pins"] == {}
    second = publisher.request_test()
    assert second is not None and owner(agent).decide(second, {"decision": "approve"}, "Owner").status == 200
    with agent.db.transaction() as conn:  # the app stopped while making it
        connectors.begin(conn, second, to_iso(agent.clock.now()))
    assert publisher.recover() == 1
    assert publisher.run() == []
    closed = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {second}")[0]
    assert closed["status"] == "failed" and "unclear" in closed["result_note"]


def test_no_test_pin_without_a_live_listing_with_a_picture(data_dir: Path) -> None:
    from app.integrations import pinterest_publisher  # noqa: PLC0415

    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=2, settings=PINNING)
    publisher = pinterest_publisher.Publisher(
        agent.db, agent.clock, agent.settings, agent.scope, lambda: None, lambda: agent.roots()[0], lambda: None
    )
    assert publisher.request_test() is None
    assert rows(agent, "SELECT COUNT(*) AS n FROM approvals WHERE executor = 'pinterest_test_pin'")[0]["n"] == 0
    [event] = rows(agent, "SELECT message FROM events WHERE message LIKE 'No test pin%'")
    assert "no live Etsy listing with a .png or .jpg photo" in event["message"]
