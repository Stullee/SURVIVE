"""Etsy (0.8.0): the listing checks, the one-time connection, the live client against a mocked Etsy, the publisher
with the dry run's fake shop, and what the agent and the owner see of it; and Etsy's API terms (0.8.1)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

httpx2 = pytest.importorskip("httpx2")

from app import paths  # noqa: E402
from app.agent import prompts, review, tools  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind, validate_request  # noqa: E402
from app.config import Settings  # noqa: E402
from app.economy.clock import Clock, to_iso  # noqa: E402
from app.economy.life import KILLED_KEY  # noqa: E402
from app.integrations import etsy, etsy_publisher  # noqa: E402
from app.integrations.etsy import (  # noqa: E402
    DISCLOSURE,
    EtsyError,
    FakeShop,
    Listing,
    NotSent,
    TaxonomyFile,
    TokenFile,
    Tokens,
    Unclear,
    Upload,
)
from app.integrations.etsy_connection import EtsyConnection  # noqa: E402
from app.integrations.etsy_live import LiveShop, _Allowlist, connect  # noqa: E402
from app.logging_setup import redact  # noqa: E402
from tests.economy_helpers import owner as owner_entry  # noqa: E402
from tests.test_agent import ROOMY, make_agent, rows  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

LIVE = Settings(
    etsy_enabled=True,
    etsy_keystring="k3ystr1ngk3ystr1ng",
    etsy_shared_secret="sh4red-s3cret-value",
    etsy_redirect_uri="https://localhost/ember-etsy",
)


def a_listing(**changes: Any) -> Listing:
    fields: dict[str, Any] = {
        "title": "Weekly Meal Planner Printable",
        "description": "A weekly meal planner.\n\nWhat you get: a PDF.",
        "price": "4.50",
        "currency": "EUR",
        "tags": ("meal planner", "printable"),
        "taxonomy_id": 1,
        "category": "Paper & Party Supplies > Paper > Calendars & Planners",
        "files": (Upload("shop/planner.pdf", "a" * 64, 12_345),),
        "photos": (Upload("shop/planner-photo-1.png", "b" * 64, 2_000_000),),
    }
    fields.update(changes)
    return Listing(**fields)


# --- what a listing may be ------------------------------------------------------------------------------------


def test_titles_tags_and_prices_follow_etsys_rules() -> None:
    assert etsy.check_title("  Weekly   Planner ") == "Weekly Planner"
    for bad, message in (
        ("x" * 141, "more than 140"),
        ("50% off, 100% fun", "'%' only once"),
        ("Planner‮", "control or direction"),
        ("", "empty"),
    ):
        with pytest.raises(EtsyError, match=message):
            etsy.check_title(bad)
    assert etsy.check_tags("planner, Meal Planner, planner, don’t forget") == (
        "planner",
        "Meal Planner",
        "don't forget",
    )
    for bad, message in (
        (",".join(f"tag{i}" for i in range(14)), "at most 13"),
        ("a" * 21, "more than 20"),
        ("under_score", "may only hold"),
        (" , ", "at least one tag"),
    ):
        with pytest.raises(EtsyError, match=message):
            etsy.check_tags(bad)
    assert etsy.check_price("4,9") == "4.90" and etsy.check_price(" 12 ") == "12.00"
    for bad in ("0.10", "abc", "NaN", "1001"):
        with pytest.raises(EtsyError):
            etsy.check_price(bad)
    assert etsy.check_description(f"Hello.\n\n{DISCLOSURE}") == "Hello."  # Ember adds it, never twice
    with pytest.raises(EtsyError, match="4,000"):
        etsy.check_description("x" * 4_001)


def test_uploads_are_the_kinds_etsy_takes() -> None:
    made = etsy.upload("shop/planner.pdf", b"%PDF-1.7 x", etsy.FILE_KINDS, "files")
    assert made == Upload("shop/planner.pdf", hashlib.sha256(b"%PDF-1.7 x").hexdigest(), 10)
    with pytest.raises(EtsyError, match="must be .jpg, .png files"):
        etsy.upload("shop/planner.pdf", b"x", etsy.PHOTO_KINDS, "photos")
    with pytest.raises(EtsyError, match="at most 70 characters"):
        etsy.upload(f"shop/{'x' * 70}.pdf", b"x", etsy.FILE_KINDS, "files")
    with pytest.raises(EtsyError, match="is empty"):
        etsy.upload("shop/a.pdf", b"", etsy.FILE_KINDS, "files")


def test_the_owner_changes_words_and_price_never_files() -> None:
    listing = a_listing()
    text = etsy.editable(listing)
    assert text.startswith("Title: Weekly Meal Planner Printable\nPrice: 4.50\nTags: meal planner, printable\n\n")
    edited = text.replace("Price: 4.50", "Price: 5.00 EUR").replace("Weekly Meal", "Monthly Meal")
    changed = etsy.with_changes(listing, edited + "\nA new last line.")
    assert changed.title == "Monthly Meal Planner Printable" and changed.price == "5.00"
    assert changed.description.endswith("A new last line.")
    assert (changed.files, changed.photos, changed.taxonomy_id) == (listing.files, listing.photos, listing.taxonomy_id)
    for bad in ("Title: x\nPrice: 4\n\ndescription", "no header at all", "Title: a\nTitle: b\nPrice: 4\nTags: x\n\nd"):
        with pytest.raises(EtsyError):
            etsy.with_changes(listing, bad)
    payload = etsy.payload(listing)
    assert "Price: 4.50 EUR" in payload and "shop/planner.pdf (12 KB)" in payload and payload.endswith(DISCLOSURE)
    assert "Category: Paper & Party Supplies > Paper > Calendars & Planners (#1)" in payload


# --- connecting ------------------------------------------------------------------------------------------------


def test_the_authorize_link_uses_pkce() -> None:
    verifier, challenge = etsy.pkce()
    assert 43 <= len(verifier) <= 128 and set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == digest
    url = etsy.authorize_url(LIVE, "st4te", challenge)
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.etsy.com", "/oauth/connect")
    assert query == {
        "response_type": "code",
        "client_id": LIVE.etsy_keystring,
        "redirect_uri": "https://localhost/ember-etsy",
        "scope": "listings_r listings_w shops_r transactions_r",
        "state": "st4te",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }


def test_the_pasted_address_is_checked() -> None:
    good = "https://localhost/ember-etsy?code=abc123&state=st4te"
    assert etsy.code_from(f"  {good}  ", LIVE, "st4te") == "abc123"
    for pasted, message in (
        ("https://evil.example/ember-etsy?code=abc&state=st4te", "isn't the address Etsy sent you to"),
        ("https://localhost/ember-etsy?code=abc&state=other", "another connection attempt"),
        ("https://localhost/ember-etsy?error=access_denied&error_description=The+user+denied&state=st4te", "Etsy said"),
        ("https://localhost/ember-etsy?state=st4te", "no authorization code"),
    ):
        with pytest.raises(EtsyError, match=message):
            etsy.code_from(pasted, LIVE, "st4te")


def test_the_options_must_describe_a_real_app() -> None:
    assert etsy.config_problems(LIVE) == []
    assert etsy.config_problems(LIVE.model_copy(update={"etsy_redirect_uri": "http://localhost:8123/cb"})) == []
    problems = etsy.config_problems(Settings(etsy_enabled=True, etsy_redirect_uri="http://example.org/cb"))
    assert problems == [
        "etsy_keystring is empty",
        "etsy_shared_secret is empty",
        "etsy_redirect_uri must be an https:// address (or http://localhost), exactly as registered for your app",
    ]


def some_tokens(clock: Clock, *, expires: timedelta = timedelta(hours=1)) -> Tokens:
    now = clock.now()
    return Tokens(
        access_token="12345.acc3ss-t0ken-value",
        refresh_token="r3fresh-t0ken-value",
        expires_at=to_iso(now + expires),
        refresh_expires_at=to_iso(now + timedelta(days=90)),
        user_id=12345,
        shop_id=777,
        shop_name="PlannerShop",
        currency="EUR",
        connected_at=to_iso(now),
    )


def test_tokens_are_kept_private(tmp_path: Path) -> None:
    store = TokenFile(tmp_path / "etsy" / "tokens.json")
    assert store.load() is None
    tokens = some_tokens(Clock())
    store.save(tokens)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.load() == tokens
    assert redact(f"token {tokens.access_token} and {tokens.refresh_token}") == "token *** and ***"
    store.clear()
    assert store.load() is None and not store.path.exists()


# --- the live client, against a mocked Etsy ----------------------------------------------------------------------


class Etsy:
    """Answers requests like Etsy's API would, and records them."""

    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        answer = self.answers.get((request.method, request.url.path))
        if answer is None:
            return httpx2.Response(404, json={"error": "not found"})
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, httpx2.Response) else httpx2.Response(200, json=answer)

    def form(self, index: int) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.requests[index].content.decode()).items()}


def mock(answers: dict[tuple[str, str], Any]) -> tuple[Etsy, Any]:
    server = Etsy(answers)
    return server, httpx2.MockTransport(server)


def test_connecting_exchanges_the_code_and_finds_the_shop(tmp_path: Path) -> None:
    server, transport = mock(
        {
            ("POST", "/v3/public/oauth/token"): {
                "access_token": "12345.fresh-access",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "fresh-refresh-token",
            },
            ("GET", "/v3/application/users/me"): {"user_id": 12345, "shop_id": 777},
            ("GET", "/v3/application/shops/777"): {
                "shop_id": 777,
                "shop_name": "PlannerShop",
                "currency_code": "EUR",
                "url": "https://www.etsy.com/shop/PlannerShop",
            },
        }
    )
    store = TokenFile(tmp_path / "tokens.json")
    clock = Clock()
    info = connect(LIVE, clock, store, "the-code", "the-verifier", transport)
    assert (info.shop_id, info.name, info.currency) == (777, "PlannerShop", "EUR")
    assert server.form(0) == {
        "grant_type": "authorization_code",
        "client_id": LIVE.etsy_keystring,
        "redirect_uri": "https://localhost/ember-etsy",
        "code": "the-code",
        "code_verifier": "the-verifier",
    }
    assert "x-api-key" not in server.requests[0].headers  # the token endpoint takes the keystring as client_id
    for request in server.requests[1:]:
        assert request.headers["x-api-key"] == f"{LIVE.etsy_keystring}:sh4red-s3cret-value"
        assert request.headers["authorization"] == "Bearer 12345.fresh-access"
        assert request.url.host == "api.etsy.com"
    saved = store.load()
    assert saved is not None and (saved.user_id, saved.shop_id, saved.shop_name) == (12345, 777, "PlannerShop")
    assert saved.refresh_token == "fresh-refresh-token"


def test_an_account_without_a_shop_is_explained(tmp_path: Path) -> None:
    _, transport = mock(
        {
            ("POST", "/v3/public/oauth/token"): {"access_token": "1.a", "refresh_token": "r" * 10, "expires_in": 3600},
            ("GET", "/v3/application/users/me"): {"user_id": 1, "shop_id": None},
        }
    )
    with pytest.raises(EtsyError, match="has no shop yet"):
        connect(LIVE, Clock(), TokenFile(tmp_path / "t.json"), "c", "v", transport)


def live_shop(tmp_path: Path, answers: dict[tuple[str, str], Any], **token_changes: Any) -> tuple[LiveShop, Etsy]:
    server, transport = mock(answers)
    clock = Clock()
    store = TokenFile(tmp_path / "tokens.json")
    tokens = some_tokens(clock)
    for key, value in token_changes.items():
        setattr(tokens, key, value)
    store.save(tokens)
    return LiveShop(LIVE, clock, store, transport), server


def test_a_listing_is_created_uploaded_and_published(tmp_path: Path) -> None:
    shop, server = live_shop(
        tmp_path,
        {
            ("POST", "/v3/application/shops/777/listings"): httpx2.Response(
                201, json={"listing_id": 555, "state": "draft"}
            ),
            ("POST", "/v3/application/shops/777/listings/555/images"): httpx2.Response(
                201, json={"listing_image_id": 1}
            ),
            ("POST", "/v3/application/shops/777/listings/555/files"): httpx2.Response(201, json={"listing_file_id": 2}),
            ("PATCH", "/v3/application/shops/777/listings/555"): {"listing_id": 555, "state": "active"},
        },
    )
    listing = a_listing(tags=("meal planner", "printable", "digital download"))
    assert shop.create_draft(listing) == 555
    form = server.form(0)
    assert form == {
        "quantity": "999",
        "title": "Weekly Meal Planner Printable",
        "description": f"{listing.description}\n\n{DISCLOSURE}",
        "price": "4.50",
        "who_made": "i_did",
        "when_made": "made_to_order",
        "taxonomy_id": "1",
        "type": "download",
        "is_supply": "false",
        "tags": "meal planner,printable,digital download",
        "should_auto_renew": "false",
    }
    shop.upload_photo(555, "planner-photo-1.png", b"\x89PNG photo", 1)
    shop.upload_file(555, "planner.pdf", b"%PDF-1.7 file", 1)
    assert shop.activate(555) == "active"
    image, file, publish = server.requests[1:]
    assert b'name="image"; filename="planner-photo-1.png"' in image.content and b'name="rank"\r\n\r\n1' in image.content
    assert b'name="file"; filename="planner.pdf"' in file.content and b'name="name"\r\n\r\nplanner.pdf' in file.content
    assert publish.method == "PATCH" and parse_qs(publish.content.decode()) == {"state": ["active"]}
    assert all(r.headers["authorization"] == "Bearer 12345.acc3ss-t0ken-value" for r in server.requests)


def test_an_expiring_token_is_renewed_once_and_saved(tmp_path: Path) -> None:
    shop, server = live_shop(
        tmp_path,
        {
            ("POST", "/v3/public/oauth/token"): {
                "access_token": "12345.new",
                "refresh_token": "new-refresh-1",
                "expires_in": 3600,
            },
            ("GET", "/v3/application/shops/777"): {"shop_name": "PlannerShop", "currency_code": "EUR"},
        },
        expires_at=to_iso(Clock().now() + timedelta(minutes=2)),  # inside the 5 minutes renewed early
    )
    shop.info()
    shop.info()
    assert [r.url.path for r in server.requests] == [
        "/v3/public/oauth/token",
        "/v3/application/shops/777",
        "/v3/application/shops/777",
    ]
    assert server.form(0) == {
        "grant_type": "refresh_token",
        "client_id": LIVE.etsy_keystring,
        "refresh_token": "r3fresh-t0ken-value",
    }
    saved = shop.tokens.load()
    assert saved is not None and (saved.access_token, saved.refresh_token) == ("12345.new", "new-refresh-1")
    assert server.requests[2].headers["authorization"] == "Bearer 12345.new"


def test_a_refused_renewal_asks_for_a_new_connection(tmp_path: Path) -> None:
    shop, _ = live_shop(
        tmp_path,
        {("POST", "/v3/public/oauth/token"): httpx2.Response(400, json={"error": "invalid_grant"})},
        expires_at=to_iso(Clock().now() - timedelta(minutes=1)),
    )
    with pytest.raises(NotSent, match="connect the shop again"):
        shop.info()


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx2.Response(400, json={"error": "Invalid taxonomy_id"}), NotSent),
        (httpx2.Response(500, json={"error": "oops"}), Unclear),
        (httpx2.ReadTimeout("slow"), Unclear),
        (httpx2.ConnectError("down"), NotSent),
    ],
)
def test_errors_say_whether_etsy_may_have_changed_something(tmp_path: Path, answer: Any, error: type) -> None:
    shop, _ = live_shop(tmp_path, {("POST", "/v3/application/shops/777/listings"): answer})
    with pytest.raises(error) as caught:
        shop.create_draft(a_listing())
    if isinstance(answer, httpx2.Response) and answer.status_code == 400:
        assert "Invalid taxonomy_id" in str(caught.value)


def test_listings_orders_and_categories_are_read(tmp_path: Path) -> None:
    receipts = {
        "count": 3,
        "results": [
            {
                "receipt_id": 91,
                "status": "paid",
                "is_paid": True,
                "created_timestamp": 1_790_000_000,
                "grandtotal": {"amount": 450, "divisor": 100, "currency_code": "EUR"},
                "buyer_email": "someone@example.org",
                "transactions": [{"listing_id": 555, "title": "Planner", "quantity": 1}],
            },
            {"receipt_id": 92, "status": "canceled", "is_paid": True, "grandtotal": {"amount": 1, "divisor": 1}},
            {"receipt_id": 93, "status": "open", "is_paid": False, "grandtotal": {"amount": 1, "divisor": 1}},
        ],
    }
    taxonomy = {
        "results": [
            {"id": 1, "name": "Paper", "children": [{"id": 2, "name": "Calendars & Planners", "children": []}]},
            {"id": 3, "name": "Art", "children": []},
        ]
    }
    shop, server = live_shop(
        tmp_path,
        {
            ("GET", "/v3/application/listings/batch"): {
                "results": [{"listing_id": 555, "state": "active", "title": "Planner", "views": 40, "num_favorers": 3}]
            },
            ("GET", "/v3/application/shops/777/receipts"): receipts,
            ("GET", "/v3/application/seller-taxonomy/nodes"): taxonomy,
        },
    )
    [listing] = shop.listings([555])
    assert (listing.state, listing.views, listing.favorites, listing.url) == (
        "active",
        40,
        3,
        "https://www.etsy.com/listing/555",
    )
    order, cancelled, unpaid = shop.orders(Clock().now() - timedelta(days=30))
    assert (order.receipt_id, order.total_cents, order.currency, order.status) == (91, 450, "EUR", "paid")
    assert order.items == [{"listing_id": 555, "title": "Planner", "quantity": 1, "price_cents": 0}]  # never who bought
    assert "someone@example.org" not in json.dumps(order.__dict__)
    # 0.12.0: cancelled and unpaid receipts come too (a stored order learns it), and by change, not by creation.
    assert (cancelled.status, cancelled.paid, unpaid.status, unpaid.paid) == ("canceled", False, "unpaid", False)
    receipts_request = next(r for r in server.requests if r.url.path.endswith("/receipts"))
    assert "min_last_modified" in receipts_request.url.params and "min_created" not in receipts_request.url.params
    assert shop.taxonomy() == [(1, "Paper"), (2, "Paper > Calendars & Planners"), (3, "Art")]
    assert "authorization" not in server.requests[-1].headers  # the categories need only the app key


def test_only_etsy_can_be_reached() -> None:
    allow = _Allowlist(retries=0)
    for url in ("https://example.com/v3/application/users/me", "http://api.etsy.com/", "https://api.etsy.com:8443/"):
        with pytest.raises(httpx2.ConnectError, match="only talks to"):
            allow.handle_request(httpx2.Request("GET", url))


# --- the publisher, with the dry run's fake shop -----------------------------------------------------------------


def proposed(data_dir: Path) -> tuple[Any, FakeTransport, int]:
    """A dry-run agent whose fourth cycle proposed an Etsy listing."""
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=4)
    found = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")
    assert found, "the fake proposes a listing in its fourth cycle"
    return agent, fake, found[0]["id"]


def listing_rows(agent: Any) -> list[dict[str, Any]]:
    return rows(agent, "SELECT approval_id, status, listing_id, result, error FROM etsy_listings")


def test_an_approved_listing_goes_live_in_the_fake_shop(data_dir: Path) -> None:
    agent, fake, request = proposed(data_dir)
    action = json.loads(rows(agent, f"SELECT action FROM approvals WHERE id = {request}")[0]["action"])
    pdf = agent.roots()[0].read_bytes(action["files"][0]["path"])
    assert action["files"][0]["sha256"] == hashlib.sha256(pdf).hexdigest()  # the exact file the owner saw
    assert agent.execute_approved() == []  # not approved yet
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    [made] = listing_rows(agent)
    assert made["status"] == "active" and made["listing_id"] == 900_000_001
    closed = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {request}")[0]
    assert closed["status"] == "done" and closed["closed_by"] == "Ember"
    assert "fake shop" in closed["result_note"] and closed["result_link"] == "https://www.etsy.com/listing/900000001"
    assert agent.execute_approved() == []  # never twice
    shown = agent.integrations()["etsy"]
    assert shown["mode"] == "fake" and shown["listings"][0]["status"] == "active" and shown["created_today"] == 1
    # The next cycle syncs: the plan shows the listing's views.
    agent.clock.advance(hours=5)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    plan = next(r for r in list(fake.sent)[before:] if request_kind(r) == "plan")
    text = plan["messages"][0]["content"][0]["text"]
    assert "\n== ETSY SHOP ==\nShop: EmberTestShop.\nAll 1 live listings, top sellers first (sold s, views v," in text
    assert "\nNewest:\n- #900000001 [active]" in text
    assert rows(agent, "SELECT views FROM etsy_listings")[0]["views"] > 0


def test_a_file_changed_after_approval_is_never_listed(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    path = json.loads(rows(agent, f"SELECT action FROM approvals WHERE id = {request}")[0]["action"])["files"][0][
        "path"
    ]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.roots()[0].write_bytes(path, b"%PDF-1.7 something else")
    assert agent.execute_approved() == [(request, "failed")]
    [made] = listing_rows(agent)
    assert made["listing_id"] is None and f"{path} changed after you approved it" in made["error"]
    assert agent.etsy.shop().state["listings"] == {}  # nothing reached the shop


def test_the_owners_changes_are_what_gets_listed(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    who = owner(agent)
    refused = who.decide(request, {"decision": "approve_with_changes", "final_payload": "Title: only this"}, "Owner")
    assert refused.status == 422 and refused.body["field"] == "final_payload"
    editable = etsy.editable(
        etsy.listing_from_action(rows(agent, f"SELECT action FROM approvals WHERE id = {request}")[0]["action"])
    )
    changed = editable.replace("Price: 4.50", "Price: 6").split("\n", 1)[1]
    decided = who.decide(
        request, {"decision": "approve_with_changes", "final_payload": f"Title: My Better Title\n{changed}"}, "Owner"
    )
    assert decided.status == 200 and decided.body["approval"]["status"] == "approved_with_changes"
    agent.execute_approved()
    item = next(iter(agent.etsy.shop().state["listings"].values()))
    assert item["title"] == "My Better Title" and item["price_cents"] == 600


def test_an_unchanged_edit_is_a_plain_approval(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    action = rows(agent, f"SELECT action FROM approvals WHERE id = {request}")[0]["action"]
    text = etsy.editable(etsy.listing_from_action(action))
    decided = owner(agent).decide(request, {"decision": "approve_with_changes", "final_payload": text}, "Owner")
    assert decided.body["approval"]["status"] == "approved" and decided.body["approval"]["final_payload"] is None


def test_the_daily_limit_holds(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    agent.settings = agent.publisher.settings = agent.settings.model_copy(update={"etsy_listings_per_day": 0})
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "waiting_limit")]
    assert listing_rows(agent) == []


def test_a_listing_failing_after_its_draft_stays_a_draft(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, request = proposed(data_dir)

    def refuse(self: FakeShop, listing_id: int, name: str, data: bytes, rank: int) -> None:
        raise NotSent("HTTP 400: file too large")

    monkeypatch.setattr(FakeShop, "upload_file", refuse)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "draft")]
    [made] = listing_rows(agent)
    assert made["listing_id"] == 900_000_001 and "HTTP 400: file too large" in made["result"]
    closed = rows(agent, f"SELECT status, result_link FROM approvals WHERE id = {request}")[0]
    assert closed == {
        "status": "failed",
        "result_link": "https://www.etsy.com/your/shops/me/listing-editor/edit/900000001",
    }


def test_etsy_refusing_the_draft_changes_nothing(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, request = proposed(data_dir)
    monkeypatch.setattr(FakeShop, "create_draft", lambda self, listing: (_ for _ in ()).throw(NotSent("HTTP 403")))
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "failed")]
    assert "Etsy refused it (HTTP 403)" in listing_rows(agent)[0]["result"]


def test_a_crash_while_creating_is_unclear_and_never_repeated(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, status, title, listing_id)"
            " VALUES (?, ?, ?, '2026-09-01T10:00:00Z', 'running', 'Planner', 900000009)",
            (scope.mode, scope.session, request),
        )
    assert agent.publisher.recover() == 1
    [made] = listing_rows(agent)
    assert made["status"] == "unclear" and made["listing_id"] == 900_000_009
    assert agent.execute_approved() == []


def test_the_owner_can_cancel_before_it_is_created(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    who = owner(agent)
    who.decide(request, {"decision": "approve"}, "Owner")
    done = who.close(request, {"outcome": "done"}, "Owner")
    assert done.status == 422 and "creates approved listings itself" in done.body["error"]
    assert who.close(request, {"outcome": "failed"}, "Owner").status == 200
    assert rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"] == (
        "Cancelled by the owner before it was listed"
    )
    assert agent.execute_approved() == [] and listing_rows(agent) == []


# --- changing a live listing (0.9.0) ----------------------------------------------------------------------------


def listed(data_dir: Path) -> tuple[Any, int]:
    """A dry-run agent with one live listing in the fake shop, and the listing's number."""
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "active")]
    return agent, 900_000_001


def shop_context(agent: Any) -> tools.ToolContext:
    """The tools' context in the agent's last cycle, with its shop (and one department among the categories)."""
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )
    categories = (*etsy.FAKE_CATEGORIES, (99, "Accessories"))
    ctx.etsy = tools.EtsyAccess(shop_name="EmberTestShop", currency="EUR", daily_limit=3, categories=categories)
    return ctx


def call(ctx: tools.ToolContext, name: str, args: dict[str, Any]) -> tools.Outcome:
    """A tool as the model calls it while working: checked, recorded and limited per cycle."""
    llm_call = rows_of(ctx, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, name, args, f"toolu_{name}", llm_call, "act")


def rows_of(ctx: tools.ToolContext, sql: str) -> list[dict[str, Any]]:
    with ctx.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def a_change(agent: Any, ctx: tools.ToolContext, listing_id: int, **args: Any) -> int:
    """A proposed change (its request number); the photos and files it names are made first."""
    for name in ("photos", "files"):
        for path in [p.strip() for p in str(args.get(name) or "").split(",") if p.strip()]:
            agent.roots()[0].write_bytes(path, f"{path} data".encode())
    made = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "reason": "Fix the listing.", **args})
    assert made.ok, made.text
    return rows_of(ctx, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'etsy_edit'")[0]["id"]


def test_a_live_listing_is_read_and_a_change_proposed(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    before = etsy.listing_from_action(
        rows(agent, "SELECT action FROM approvals WHERE executor = 'etsy_listing'")[0]["action"]
    )
    short = call(ctx, "etsy_listing", {})
    assert short.text.startswith("Your live listings (newest first):\n- #900000001 ")
    assert f"· 4.50 EUR · category #{before.taxonomy_id} · 1 photo, 2 files" in short.text
    full = call(ctx, "etsy_listing", {"listing_id": listing_id}).text
    assert f"Title: {before.title}\nPrice: 4.50 EUR\n" in full and full.endswith(f"after it):\n{before.description}")
    assert "isn't one of your live listings" in call(ctx, "etsy_listing", {"listing_id": 5}).text
    agent.roots()[0].write_bytes("shop/new-1.png", b"\x89PNG one")
    agent.roots()[0].write_bytes("shop/x.pdf", b"%PDF-1.7 x")
    refusals = {
        "isn't one of your live listings": {"listing_id": 5, "price": "3.90"},
        "nothing changes": {"listing_id": listing_id, "price": "4.50", "category_id": before.taxonomy_id},
        "a whole department of Etsy's": {"listing_id": listing_id, "category_id": 99},
        "must be .jpg, .png files": {"listing_id": listing_id, "photos": "shop/new-1.png, shop/x.pdf"},
    }
    for error, args in refusals.items():
        refused = call(ctx, "propose_etsy_edit", {**args, "reason": "r"})
        assert not refused.ok and error in refused.text, (error, refused.text)
    request = a_change(
        agent,
        ctx,
        listing_id,
        price="3.90",
        category_id=2,
        photos="shop/new-1.png, shop/new-2.png",
        description="A better description.",
    )
    made = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'propose_etsy_edit' AND status = 'ok'")[0]
    assert (
        f"#{request} is waiting for your owner: it changes the description, category, price, photos of"
        in (made["result"])
    )
    approval = rows(agent, f"SELECT type, executor, expected_cost, title, payload FROM approvals WHERE id = {request}")[
        0
    ]
    assert (approval["type"], approval["executor"]) == ("sell", "etsy_edit")
    assert approval["title"] == f"Change Etsy listing: {before.title}"[:120]
    assert "charges nothing" in approval["expected_cost"]
    payload = approval["payload"]
    assert payload.startswith(f"Listing #900000001: {before.title}\n")
    assert "Price: 3.90 EUR (was: 4.50 EUR)" in payload
    assert f"Category: {etsy.FAKE_CATEGORIES[1][1]} (#2)\n  (was: {before.category} (#{before.taxonomy_id}))" in payload
    assert (
        "Photos, the main one first: shop/new-1.png (1 KB); shop/new-2.png (1 KB)\n  (they replace every photo it has"
        " at Etsy: Ember's "
    ) in payload and ", and any you added there)" in payload
    assert payload.endswith(f"New description:\nA better description.\n\n{DISCLOSURE}")
    again = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "title": "Another title", "reason": "r"})
    assert not again.ok and f"request #{request} already changes #900000001" in again.text
    assert "Request #" in call(ctx, "etsy_listing", {"listing_id": listing_id}).text  # it says a change waits
    with agent.db.connection() as conn:
        shop = etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "EmberTestShop", 3)
    assert shop.endswith(
        "\nChange a live listing (free at Etsy): etsy_listing shows it, propose_etsy_edit asks your owner.\n"
        f"Changes not made yet: request #{request} for #900000001 (your owner decides)."
    )


def test_an_approved_change_is_made_and_never_twice(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    request = a_change(
        agent,
        ctx,
        listing_id,
        title="Weekly Planner, A4",
        description="Only A4: one PDF and its Word copy.",
        tags="weekly planner, a4 planner",
        category_id=2,
        price="3.90",
        photos="shop/p1.png, shop/p2.png, shop/p3.png",
        files="shop/planner-a4.pdf",
    )
    assert agent.execute_approved() == []  # not approved yet
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    item = agent.etsy.shop().state["listings"]["900000001"]
    old_photos = list(item.get("photo_ids") or [])
    assert agent.execute_approved() == [(request, "done")]
    item = agent.etsy.shop().state["listings"]["900000001"]
    assert item["title"] == "Weekly Planner, A4" and item["price_cents"] == 390
    assert item["photos"] == 3 and item["files"] == 1 and not set(old_photos) & set(item["photo_ids"])
    closed = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {request}")[0]
    assert closed["status"] == "done" and closed["closed_by"] == "Ember"
    assert closed["result_note"] == (
        "Changed in the dry run's fake shop (title, description, tags, category, price, photos, files); nothing"
        " reached Etsy."
    )
    assert closed["result_link"] == "https://www.etsy.com/listing/900000001"
    [change] = rows(agent, "SELECT status, listing_id, listing FROM etsy_edits")
    assert (change["status"], change["listing_id"]) == ("done", listing_id)
    now = etsy.listing_from_action(change["listing"])
    assert (now.title, now.price, now.taxonomy_id, now.tags) == (
        "Weekly Planner, A4",
        "3.90",
        2,
        ("weekly planner", "a4 planner"),
    )
    assert [p.path for p in now.photos] == ["shop/p1.png", "shop/p2.png", "shop/p3.png"]
    assert rows(agent, "SELECT title FROM etsy_listings")[0]["title"] == "Weekly Planner, A4"
    assert (
        "Price: 3.90 EUR" in call(ctx, "etsy_listing", {"listing_id": listing_id}).text
    )  # the next change starts here
    assert agent.execute_approved() == []  # never twice
    shown = views_approval(agent, request)
    assert shown["execution"]["status"] == "done"
    assert shown["editable"] == (
        "Title: Weekly Planner, A4\nPrice: 3.90\nTags: weekly planner, a4 planner\n\n"
        "Only A4: one PDF and its Word copy."
    )


def views_approval(agent: Any, approval_id: int) -> dict[str, Any]:
    """An approval as the dashboard gets it."""
    from app.agent import views  # noqa: PLC0415

    with agent.db.connection() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return views._carried_out(agent, conn, agent.scope(), row)


def test_the_owners_version_of_a_change_is_made(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    request = a_change(agent, ctx, listing_id, price="3.90", description="A better description.")
    who = owner(agent)
    assert views_approval(agent, request)["editable"] == "Price: 3.90\n\nA better description."
    for wrong in ("Title: x\n\nText", "Price: 3.50", "Price: 3.50\nPrice: 3\n\nText"):
        refused = who.decide(request, {"decision": "approve_with_changes", "final_payload": wrong}, "Owner")
        assert refused.status == 422 and refused.body["field"] == "final_payload", wrong
    decided = who.decide(
        request, {"decision": "approve_with_changes", "final_payload": "Price: 3.50 EUR\n\nMy own words."}, "Owner"
    )
    assert decided.status == 200 and decided.body["approval"]["final_payload"] == "Price: 3.50\n\nMy own words."
    assert agent.execute_approved() == [(request, "done")]
    now = etsy.listing_from_action(rows(agent, "SELECT listing FROM etsy_edits")[0]["listing"])
    assert (now.price, now.description) == ("3.50", "My own words.")
    # A change of photos only has no words to change: approved as it is, or rejected.
    photos = a_change(agent, ctx, listing_id, photos="shop/only-photo.png")
    assert views_approval(agent, photos)["editable"] is None
    refused = who.decide(photos, {"decision": "approve_with_changes", "final_payload": "Price: 1"}, "Owner")
    assert refused.status == 422 and "no words or price" in refused.body["error"]


def test_the_plan_names_the_listings_with_too_few_photos(data_dir: Path) -> None:
    # 0.11.1: the plans never saw a photo count, so the owner asked three times for more photos.
    agent, listing_id = listed(data_dir)  # the fake listing has one photo
    with agent.db.connection() as conn:
        shop = etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "EmberTestShop", 3)
    assert f"\nFewer than 5 photos: #{listing_id} (1). Etsy shows up to 10: make more and give the whole set" in shop
    assert shop.index("Fewer than 5 photos") < shop.index("Orders in the last 7 days")  # the tail is cut first
    request = a_change(agent, shop_context(agent), listing_id, photos=", ".join(f"shop/p{i}.png" for i in range(5)))
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "done")]
    with agent.db.connection() as conn:
        assert "Fewer than" not in etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "EmberTestShop", 3)


def test_an_unchanged_owners_version_is_a_plain_approval_of_the_change(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    request = a_change(agent, shop_context(agent), listing_id, title="Weekly Planner, A4")
    decided = owner(agent).decide(
        request, {"decision": "approve_with_changes", "final_payload": "Title: Weekly Planner, A4"}, "Owner"
    )
    assert decided.body["approval"]["status"] == "approved" and decided.body["approval"]["final_payload"] is None


def test_a_change_etsy_refuses_halfway_is_partial(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    request = a_change(agent, ctx, listing_id, title="Weekly Planner, A4", price="3.90")
    monkeypatch.setattr(FakeShop, "set_price", lambda self, i, price: (_ for _ in ()).throw(NotSent("HTTP 400: no")))
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "partial")]
    [change] = rows(agent, "SELECT status, listing, result FROM etsy_edits")
    assert change["result"].startswith("Partly changed: title changed; price not, Etsy refused it (HTTP 400: no).")
    now = etsy.listing_from_action(change["listing"])
    assert (now.title, now.price) == ("Weekly Planner, A4", "4.50")  # what changed, and only that
    closed = rows(agent, f"SELECT status, result_link FROM approvals WHERE id = {request}")[0]
    assert closed == {"status": "failed", "result_link": etsy.edit_url(listing_id)}


def test_photos_etsy_refuses_halfway_are_reported(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    request = a_change(agent, ctx, listing_id, photos="shop/p1.png, shop/p2.png")
    upload = FakeShop.upload_photo

    def second_fails(self: FakeShop, listing: int, name: str, data: bytes, rank: int) -> None:
        if rank == 2:
            raise NotSent("HTTP 400: too small")
        upload(self, listing, name, data, rank)

    monkeypatch.setattr(FakeShop, "upload_photo", second_fails)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "partial")]
    [change] = rows(agent, "SELECT listing, result FROM etsy_edits")
    assert "Its photos were left half replaced." in change["result"] and change["listing"] is None
    assert agent.etsy.shop().state["listings"]["900000001"]["photos"] == 2  # the new first one and the old one


def test_a_change_etsy_refuses_changes_nothing(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    request = a_change(agent, ctx, listing_id, title="Weekly Planner, A4")
    monkeypatch.setattr(FakeShop, "update_listing", lambda self, i, fields: (_ for _ in ()).throw(NotSent("HTTP 403")))
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "failed")]
    [change] = rows(agent, "SELECT status, listing, result FROM etsy_edits")
    assert change == {"status": "failed", "listing": None, "result": "Not changed: Etsy refused it (HTTP 403)."}
    assert "Weekly Planner, A4" not in call(ctx, "etsy_listing", {"listing_id": listing_id}).text


def test_a_file_changed_after_the_change_was_approved_is_never_uploaded(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    request = a_change(agent, shop_context(agent), listing_id, files="shop/new.pdf")
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.roots()[0].write_bytes("shop/new.pdf", b"%PDF-1.7 another file")
    assert agent.execute_approved() == [(request, "failed")]
    [change] = rows(agent, "SELECT status, error FROM etsy_edits")
    assert change["status"] == "failed" and "shop/new.pdf changed after you approved it" in change["error"]
    assert agent.etsy.shop().state["listings"]["900000001"]["files"] == 2  # untouched


def test_a_crash_while_changing_is_unclear_and_never_repeated(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    request = a_change(agent, shop_context(agent), listing_id, title="Weekly Planner, A4")
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO etsy_edits (mode, session, approval_id, listing_id, started_at, status)"
            " VALUES (?, ?, ?, ?, '2026-09-01T10:00:00Z', 'running')",
            (scope.mode, scope.session, request, listing_id),
        )
    assert agent.publisher.recover() == 1
    [change] = rows(agent, "SELECT status, result FROM etsy_edits")
    assert change["status"] == "unclear" and "Ember won't try again" in change["result"]
    assert agent.execute_approved() == []


def test_the_owner_can_cancel_a_change_before_it_is_made(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    request = a_change(agent, shop_context(agent), listing_id, title="Weekly Planner, A4")
    who = owner(agent)
    who.decide(request, {"decision": "approve"}, "Owner")
    done = who.close(request, {"outcome": "done"}, "Owner")
    assert done.status == 422 and "makes approved changes itself" in done.body["error"]
    assert who.close(request, {"outcome": "failed"}, "Owner").status == 200
    assert rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"] == (
        "Cancelled by the owner before the listing was changed"
    )
    assert agent.execute_approved() == [] and rows(agent, "SELECT id FROM etsy_edits") == []


def test_new_photos_come_first_and_the_old_ones_go_without_ever_leaving_none() -> None:
    log: list[str] = []
    count = [10]

    def upload(name: str, data: bytes, rank: int) -> None:
        assert count[0] < etsy.MAX_PHOTOS
        count[0] += 1
        log.append(f"+{name}@{rank}")

    def delete(item: int) -> None:
        assert count[0] > 1
        count[0] -= 1
        log.append(f"-{item}")

    items = [(f"n{i}", b"x") for i in range(1, 11)]
    etsy_publisher._replace(list(range(1, 11)), upload, delete, items, etsy.MAX_PHOTOS, [])
    assert log[:4] == ["-10", "+n1@1", "-9", "+n2@2"] and count[0] == 10 and len(log) == 20
    log.clear()
    count[0] = 2
    etsy_publisher._replace([7, 8], upload, delete, items[:1], etsy.MAX_PHOTOS, [])
    assert log == ["+n1@1", "-7", "-8"] and count[0] == 1


def test_the_dashboard_shows_changes_and_can_cancel_them() -> None:
    # Checked by hand in a browser too (0.9.0): the change's photos and text, "Approve with changes" filled with the
    # new words, then "Waiting to be made" with "Cancel change".
    script = (paths.WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert '|| a.executor === "etsy_edit" ? a.executor : null' in script  # carried out by Ember's code
    assert "var CHANGE_EXECUTION = {" in script and "function etsyChangeDraft(action, payload, final)" in script
    assert 'panelButton(it, "failed", "Cancel change", true)' in script
    assert 'executor === "etsy_edit" && !a.editable' in script  # photos, files or category only: no "with changes"


def test_a_live_listing_is_changed_through_etsys_api(tmp_path: Path) -> None:
    inventory = {
        "products": [
            {
                "product_id": 1,
                "sku": "",
                "property_values": [],
                "offerings": [{"offering_id": 2, "quantity": 999, "is_enabled": True, "price": {"amount": 450}}],
            }
        ]
    }
    shop, server = live_shop(
        tmp_path,
        {
            ("PATCH", "/v3/application/shops/777/listings/555"): {"listing_id": 555},
            ("GET", "/v3/application/listings/555/inventory"): inventory,
            ("PUT", "/v3/application/listings/555/inventory"): inventory,
            ("GET", "/v3/application/listings/555/images"): {
                "results": [{"listing_image_id": 32, "rank": 2}, {"listing_image_id": 31, "rank": 1}]
            },
            ("DELETE", "/v3/application/shops/777/listings/555/images/31"): httpx2.Response(204),
            ("GET", "/v3/application/shops/777/listings/555/files"): {"results": [{"listing_file_id": 41, "rank": 1}]},
            ("DELETE", "/v3/application/shops/777/listings/555/files/41"): httpx2.Response(204),
        },
    )
    edit = etsy.Edit(listing_id=555, currency="EUR", title="New title", description="New text.", tags=("a", "b"))
    shop.update_listing(555, edit.listing_fields())
    assert server.form(0) == {
        "title": "New title",
        "description": f"New text.\n\n{DISCLOSURE}",
        "tags": "a,b",
    }
    shop.set_price(555, "3.90")
    put = server.requests[-1]
    assert put.method == "PUT" and json.loads(put.content) == {
        "products": [
            {"sku": "", "property_values": [], "offerings": [{"price": 3.9, "quantity": 999, "is_enabled": True}]}
        ],
        "price_on_property": [],
        "quantity_on_property": [],
        "sku_on_property": [],
    }
    assert shop.photo_ids(555) == [31, 32] and shop.file_ids(555) == [41]  # in their order
    shop.delete_photo(555, 31)
    shop.delete_file(555, 41)
    assert [r.method for r in server.requests[-2:]] == ["DELETE", "DELETE"]
    assert all(r.headers["authorization"] == "Bearer 12345.acc3ss-t0ken-value" for r in server.requests)


def test_a_price_with_variations_is_left_to_the_owner(tmp_path: Path) -> None:
    offering = {"quantity": 1, "is_enabled": True}
    variations = {
        "products": [
            {"property_values": [{"property_id": 1}], "offerings": [offering]},
            {"property_values": [{"property_id": 2}], "offerings": [offering]},
        ]
    }
    shop, server = live_shop(tmp_path, {("GET", "/v3/application/listings/555/inventory"): variations})
    with pytest.raises(NotSent, match="the listing has variations: change its price at Etsy"):
        shop.set_price(555, "3.90")
    assert [r.method for r in server.requests] == ["GET"]  # nothing was changed


def test_only_orders_with_embers_listings_are_kept_and_the_owner_records_them(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    shop = agent.etsy.shop()
    real_orders = shop.orders

    def orders(since: Any) -> list[etsy.Order]:
        return [
            *real_orders(since),
            etsy.Order(
                71,
                "2026-09-02T10:00:00Z",
                450,
                "EUR",
                [{"listing_id": 900_000_001, "title": "P", "quantity": 1, "price_cents": 450}],
                items_cents=450,
            ),
            etsy.Order(
                72, "2026-09-02T11:00:00Z", 999, "EUR", [{"listing_id": 1, "title": "The owner's own", "quantity": 1}]
            ),
        ]

    shop.orders = orders  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    kept = agent.integrations()["etsy"]["orders"]
    assert [(o["receipt_id"], o["total"], o["recorded"]) for o in kept] == [(71, "4.50 EUR", False)]
    owner_entry(agent.economy, "revenue", "4.50", idempotency_key=kept[0]["revenue_key"], test_money=True)
    assert agent.integrations()["etsy"]["orders"][0]["recorded"] is True


def test_an_order_counts_only_embers_lines_and_follows_its_refunds(data_dir: Path) -> None:
    """0.12.0: the whole receipt was stored (tax, shipping and the owner's own products), a refund after the sync
    was never seen, and a pound order was offered as dollars."""
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    shop = agent.etsy.shop()
    ours = 900_000_001
    receipt = {"status": "paid", "refunded": 0}

    def orders(since: Any) -> list[etsy.Order]:
        lines = [
            {"listing_id": ours, "title": "Planner", "quantity": 1, "price_cents": 490},
            {"listing_id": 1, "title": "The owner's own", "quantity": 1, "price_cents": 1_000},
        ]
        return [
            etsy.Order(
                81,
                "2026-09-02T10:00:00Z",
                1_830,  # with tax and shipping
                "EUR",
                lines,
                status=receipt["status"],
                items_cents=1_490,
                discount_cents=149,  # a 10% coupon: Ember's share is 49 cents
                refunded_cents=receipt["refunded"],
            ),
            etsy.Order(82, "2026-09-03T10:00:00Z", 600, "GBP", [{**lines[0], "price_cents": 600}], items_cents=600),
        ]

    def shown() -> dict[int, dict[str, Any]]:
        assert agent.publisher.sync(force=True) is None
        return {o["receipt_id"]: o for o in agent.integrations()["etsy"]["orders"]}

    def sold() -> str:
        with agent.db.connection() as conn:
            return etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "Shop", 3)

    shop.orders = orders  # type: ignore[method-assign]
    first = shown()
    assert (first[81]["total"], first[81]["status"], first[81]["recordable"]) == ("4.41 EUR", "paid", True)
    assert [i["listing_id"] for i in first[81]["items"]] == [ours]  # only Ember's line
    assert (first[82]["total"], first[82]["recordable"]) == ("6.00 GBP", False)  # never prefilled as dollars
    assert re.search(rf"#{ours} [^·]* 2s ", sold())

    entry = owner_entry(agent.economy, "revenue", "4.41", idempotency_key=first[81]["revenue_key"], test_money=True)
    receipt.update(status="partially refunded", refunded=745)  # half the receipt: Ember's share is 2.45
    partly = shown()[81]
    assert (partly["total"], partly["status"], partly["recorded"]) == ("1.96 EUR", "partially refunded", True)
    receipt.update(status="fully refunded", refunded=1_830)
    refunded = shown()[81]
    assert (refunded["total"], refunded["status"], refunded["recordable"]) == ("0.00 EUR", "fully refunded", False)
    assert refunded["entry_id"] == entry["entry"]["id"]  # the dashboard asks for a correction of that entry
    assert re.search(rf"#{ours} [^·]* 1s ", sold())  # a refunded order no longer counts


# --- what the agent and the owner see --------------------------------------------------------------------------


def test_the_etsy_tools_come_with_a_shop() -> None:
    names = {d["name"] for d in tools.definitions(mail=True)}
    assert not names & tools.ETSY_TOOLS
    assert {d["name"] for d in tools.definitions(mail=True, etsy=True)} >= tools.ETSY_TOOLS
    assert "etsy" in tools.GUIDES and len(tools.guide_text("etsy")) <= tools.MAX_RESULT_CHARS


def test_a_proposal_is_checked_before_it_reaches_the_owner(data_dir: Path) -> None:
    from app.agent.fake_llm import Plan, Reply, ToolCalls  # noqa: PLC0415
    from tests.test_loop_shapes import JOURNAL, PLAN  # noqa: PLC0415

    def made(agent: Any) -> None:
        agent.roots()[0].write_bytes("shop/p.pdf", b"%PDF-1.7 planner")
        agent.roots()[0].write_bytes("shop/p.png", b"\x89PNG photo")

    base = {
        "title": "Planner",
        "description": "A planner.",
        "price": "4.50",
        "tags": "planner",
        "category_id": 1,
        "files": "shop/p.pdf",
        "photos": "shop/p.png",
        "reason": "A first test.",
    }
    calls = [
        ("propose_etsy_listing", {**base, "category_id": 999}),
        ("propose_etsy_listing", {**base, "files": "shop/missing.pdf"}),
        ("propose_etsy_listing", {**base, "photos": "shop/p.pdf"}),
    ]
    line = {"title": "Planners", "hypothesis": "Printables sell.", "next_step": "List one", "status": "active"}
    fake = FakeTransport(
        script=[
            Plan(PLAN),
            ToolCalls(calls),
            # 0.12.0: a listing belongs to a product line, whose first listing needs a demand note
            ToolCalls([("project_create", line), ("demand_note", {"project_id": 1, "keywords": "planner"})]),
            ToolCalls([("propose_etsy_listing", base)]),
            Reply("Ok."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake, before=made, settings=ROOMY.model_copy(update={"etsy_market_probe": True}))
    results = rows(agent, "SELECT status, result FROM tool_calls WHERE tool = 'propose_etsy_listing' ORDER BY id")
    assert [r["status"] for r in results] == ["error", "error", "error", "ok"]
    assert "category 999 isn't an Etsy category" in results[0]["result"]
    assert "shop/missing.pdf" in results[1]["result"] and "must be .jpg, .png files" in results[2]["result"]
    assert "Nothing is on Etsy yet" in results[3]["result"]
    assert f"in the category {etsy.FAKE_CATEGORIES[0][1]} (#1)" in results[3]["result"]  # the agent sees its pick
    approval = rows(agent, "SELECT type, executor, expected_cost, payload FROM approvals")[0]
    assert (approval["type"], approval["executor"]) == ("sell", "etsy_listing")
    assert "USD 0.20" in approval["expected_cost"] and approval["payload"].startswith("Title: Planner\nPrice: 4.50 EUR")


def test_categories_are_found_without_accents_and_departments_are_refused(data_dir: Path) -> None:
    # 0.9.0: in live use the agent found no category for 'resume', guessed 1 (Etsy's department 'Accessories') and
    # the listing went live there; the reply never said which category it had picked.
    agent, _ = make_agent(data_dir, [])
    ctx = research_context(agent, [])
    templates = "Paper & Party Supplies > Paper > Stationery > Design & Templates > Templates"
    nodes = [
        (1, "Accessories"),
        (1874, templates),
        (9001, f"{templates} > Résumé Templates"),
        *[(100 + i, f"Paper & Party Supplies > Paper > Party Papers > Kind {i:02}") for i in range(12)],
    ]
    ctx.etsy = tools.EtsyAccess(shop_name="Shop", currency="EUR", daily_limit=3, categories=tuple(nodes))

    def search(words: str) -> str:
        return tools.HANDLERS["etsy_categories"](ctx, {"search": words}, None).text

    assert search("resume") == f"Categories (number: path):\n9001: {templates} > Résumé Templates"
    assert search("RÉSUMÉ templates").endswith("Résumé Templates")
    assert search("accessories") == "Categories (number: path):\n1: Accessories" + tools.DEPARTMENT
    many = search("paper")
    assert many.count("\n") == tools.CATEGORIES_SHOWN + 1 and "1874: " not in many  # shortest paths first
    assert many.endswith("...and 4 more with longer paths (more specific): add a word to see them.")
    assert "\n1874: " in search("paper templates")
    with pytest.raises(tools.ToolError, match="category 1 is Accessories, a whole department of Etsy's"):
        tools._category(ctx.etsy, 1)
    with pytest.raises(tools.ToolError, match="category 5 isn't an Etsy category"):
        tools._category(ctx.etsy, 5)
    assert tools._category(ctx.etsy, 1874) == templates
    assert etsy.department("Accessories") and not etsy.department(templates)
    assert not any(etsy.department(path) for _, path in etsy.FAKE_CATEGORIES)  # the dry run's are all specific


def test_the_owner_connects_only_a_live_shop(ingress_client: TestClient) -> None:
    csrf = {"X-Ember-Request": "1"}
    refused = ingress_client.post("api/etsy/connect", headers=csrf)
    assert refused.status_code == 422 and "fake shop is always connected" in refused.json()["error"]
    assert ingress_client.post("api/etsy/finish", json={}, headers=csrf).status_code == 422
    shown = ingress_client.get("api/dashboard").json()["integrations"]["etsy"]
    assert (shown["mode"], shown["status"], shown["shop_name"]) == ("fake", "ok", "EmberTestShop")
    assert shown["listings"] == [] and shown["orders"] == []


def test_the_rebuilt_approvals_keep_every_rule(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1)
    with agent.db.connection() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'approvals'")}
    assert names == {
        "approvals",
        "approvals_by_scope",
        "approvals_one_pending",
        "approvals_action_fixed",
        "approvals_action_pair",
        "approvals_attribution_fixed",  # 0.12.0
        "approvals_closed_by_final",
        "approvals_decision_final",
        "approvals_no_delete",
        "approvals_request_fixed",
        "approvals_status_flow",
        "approvals_never_on_unlock",  # 0.13.0: NEVER, in the database
        "approvals_unlock_carries",  # 0.13.0
    }
    scope = agent.scope()
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description, payload,"
            " payload_sha256, expected_cost, expected_benefit, executor, action) VALUES (?, ?, ?, 1, 'now', 'sell',"
            " 't', 'd', 'p', 'h', 'c', 'b', 'shopify', '{}')",
            (scope.mode, scope.session, scope.life_id),
        )


def test_a_0_8_shop_keeps_its_listings_through_the_0_9_migration(tmp_path: Path) -> None:
    # 0.9.0 rebuilds the approvals table again (for 'etsy_edit'), under the listings that point at it.
    from app.db import Database, discover_migrations, migrate  # noqa: PLC0415

    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 10], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', 'then', 'completed', 'schedule', 0, 1)"
        )
        conn.execute(
            "INSERT INTO approvals (id, mode, session, life_id, cycle_id, created_at, type, title, description,"
            " payload, payload_sha256, expected_cost, expected_benefit, status, closed_at, closed_by, version,"
            " executor, action) VALUES (3, 'live', 0, 1, 1, 'then', 'sell', 'Etsy listing: CV', 'd', 'p', 'h',"
            " 'USD 0.20', 'b', 'done', 'now', 'Ember', 2, 'etsy_listing', '{}')"
        )
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, listing_id,"
            " title) VALUES ('live', 0, 3, 'then', 'now', 'active', 4584845289, 'CV')"
        )
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == [
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        51,
        52,
    ]
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        assert tuple(conn.execute("SELECT id, executor, status, version FROM approvals").fetchone()) == (
            3,
            "etsy_listing",
            "done",
            2,
        )
        assert conn.execute("SELECT approval_id, listing_id FROM etsy_listings").fetchone()[1] == 4584845289
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description, payload,"
            " payload_sha256, expected_cost, expected_benefit, executor, action) VALUES ('live', 0, 1, 1, 'now',"
            " 'sell', 'Change', 'd', 'p2', 'h2', 'none', 'b', 'etsy_edit', '{\"listing_id\": 4584845289}')"
        )
    with pytest.raises(sqlite3.IntegrityError), upgraded.transaction() as conn:  # a closed request stays closed
        conn.execute("UPDATE approvals SET closed_by = 'Owner' WHERE id = 3")
    upgraded.close()


# --- Etsy's API terms (0.8.1) ------------------------------------------------------------------------------------

NOTICE = (
    "The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified"
    " by Etsy, Inc."
)


def test_the_trademark_notice_comes_with_the_shop_in_both_modes(data_dir: Path) -> None:
    assert etsy.NOTICE == NOTICE  # word for word, as Etsy's terms give it
    agent, _ = make_agent(data_dir, [])
    assert agent.integrations()["etsy"]["notice"] == NOTICE  # the dry run's fake shop
    live = EtsyConnection(
        agent.db, agent.clock, LIVE, "live", 0, TokenFile(data_dir / "t.json"), TaxonomyFile(data_dir / "c.json")
    )
    assert live.describe()["notice"] == NOTICE  # a live shop, connected or not


def test_the_dashboard_shows_the_notice_and_how_fresh_the_numbers_are() -> None:
    script = (paths.WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    start = script.index("function renderEtsy(d)")
    render = script[start : script.index("\n  }\n", start)]
    assert "e.notice" in render and '$("etsy-notice")' in render
    assert "e.last_sync_at ? [" in render and '"Numbers from"' in render and "timeEl(e.last_sync_at" in render
    assert 'class="card-note" id="etsy-notice"' in (paths.WEB_DIR / "index.html").read_text(encoding="utf-8")


def research_context(agent: Any, calls: list[tuple[str | None, str | None]]) -> tools.ToolContext:
    """The research tool's context; its model call is a stand-in that records the page or the site it was given."""
    return tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=0,
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
        research=lambda question, url, cycle_id, site, venture_id: (
            calls.append((url, site)) or tools.Outcome(True, "ok", "ok")
        ),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.etsy.com/listing/1/x",
        "https://etsy.com/",
        "https://shop.etsy.com/a",
        "https://etsy.me/abc",
        "https://WWW.Etsy.com/listing/1",
        "https://www.etsy.com./listing/1",
    ],
)
def test_research_never_reads_etsys_pages(data_dir: Path, url: str) -> None:
    agent, _ = make_agent(data_dir, [])
    calls: list[tuple[str | None, str | None]] = []
    ctx = research_context(agent, calls)
    ctx.state.seen_urls.add(url)  # even a page its own search found
    with pytest.raises(tools.ToolError, match=r"Etsy's pages can't be read by a program \(Etsy's API terms forbid"):
        tools.HANDLERS["research"](ctx, {"question": "What sells?", "url": url})
    assert calls == []


def test_other_pages_are_read_and_etsy_is_searched(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    calls: list[tuple[str | None, str | None]] = []
    ctx = research_context(agent, calls)
    for url in ("https://example.com/pricing", "https://notetsy.com/planners"):
        ctx.state.seen_urls.add(url)
        tools.HANDLERS["research"](ctx, {"question": "q", "url": url})
    for url in ("https://notetsy.com/other", "https://[etsy.com"):  # refused because no search found them
        with pytest.raises(tools.ToolError, match="appeared in your research results"):
            tools.HANDLERS["research"](ctx, {"question": "q", "url": url})
    tools.HANDLERS["research"](ctx, {"question": "What sells?", "site": "etsy.com"})  # a search engine's results
    assert calls == [("https://example.com/pricing", None), ("https://notetsy.com/planners", None), (None, "etsy.com")]


def test_the_page_reader_is_barred_from_etsy_on_the_server_too() -> None:
    request = prompts.research_request(Settings(), "q", "https://example.com/page")
    [fetch] = request["tools"]
    assert fetch["name"] == "web_fetch" and fetch["blocked_domains"] == ["etsy.com", "etsy.me"]
    assert "allowed_domains" not in fetch and validate_request(request) is None  # one list or the other, not both
    [search] = prompts.research_request(Settings(), "q", None, "etsy.com")["tools"]
    assert search["allowed_domains"] == ["etsy.com"] and "blocked_domains" not in search


def test_the_shop_is_read_every_hour_while_the_agent_sleeps(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "active")]
    key = etsy_publisher.meta_key(agent.mode, "last_sync_at")
    synced = agent.db.get_meta(key)  # by the first cycle, before the listing existed
    cycles = len(rows(agent, "SELECT id FROM cycles"))
    agent.clock.advance(minutes=30)
    agent.sync_shop()
    assert agent.db.get_meta(key) == synced  # not again within the hour
    assert rows(agent, "SELECT views FROM etsy_listings")[0]["views"] is None
    agent.clock.advance(minutes=31)
    agent.sync_shop()
    assert agent.db.get_meta(key) == to_iso(agent.clock.now())
    assert rows(agent, "SELECT views FROM etsy_listings")[0]["views"] > 0
    assert len(rows(agent, "SELECT id FROM cycles")) == cycles  # without a wake cycle
    assert agent.integrations()["etsy"]["last_sync_at"] == to_iso(agent.clock.now())  # the dashboard's "Numbers from"


def test_the_shop_is_read_while_paused_but_not_once_killed(data_dir: Path) -> None:
    # 0.12.0: reading spends nothing, so a sale is still seen while the agent is paused or dormant (it stopped)
    agent, _ = make_agent(data_dir, [])
    key = etsy_publisher.meta_key(agent.mode, "last_sync_at")
    agent.economy.set_paused(True)
    assert agent.executor_blocked() == "The agent is paused" and agent.sync_blocked() is None  # sending waits
    agent.sync_shop()
    assert agent.db.get_meta(key) == to_iso(agent.clock.now()) and rows(agent, "SELECT id FROM cycles") == []
    agent.economy.set_paused(False)
    agent.economy.life.set_switch(KILLED_KEY, True)
    assert agent.sync_blocked() == "The agent is killed"
    agent.clock.advance(hours=2)
    agent.sync_shop()
    assert agent.db.get_meta(key) != to_iso(agent.clock.now())  # not read again


def test_a_failed_check_of_the_shop_waits_an_hour(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = make_agent(data_dir, [])
    tries: list[str] = []
    monkeypatch.setattr(agent.publisher, "sync", lambda: tries.append("sync") or "HTTP 503")
    agent.sync_shop()
    agent.clock.advance(minutes=59)
    agent.sync_shop()  # a failing Etsy isn't asked again every minute
    assert tries == ["sync"]
    agent.clock.advance(minutes=2)

    def unreachable(shop: Any) -> None:
        raise NotSent("Etsy couldn't be reached (ConnectError)")

    monkeypatch.setattr(agent.etsy, "refresh_categories", unreachable)
    with pytest.raises(NotSent):
        agent.sync_shop()  # the scheduler logs it
    agent.clock.advance(minutes=59)
    agent.sync_shop()
    assert tries == ["sync", "sync"]


def test_failing_categories_dont_keep_the_listings_numbers_old(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = make_agent(data_dir, [])

    def unreachable(shop: Any) -> None:
        raise NotSent("Etsy couldn't be reached (ConnectError)")

    monkeypatch.setattr(agent.etsy, "refresh_categories", unreachable)
    with pytest.raises(NotSent):
        agent.sync_shop()
    assert agent.db.get_meta(etsy_publisher.meta_key(agent.mode, "last_sync_at")) == to_iso(agent.clock.now())


def test_a_round_between_checks_leaves_the_shop_alone(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = make_agent(data_dir, [])
    agent.sync_shop()
    monkeypatch.setattr(agent.etsy, "shop", lambda: pytest.fail("the shop was looked at between checks"))
    for _ in range(59):  # the scheduler's rounds, a minute apart
        agent.clock.advance(minutes=1)
        agent.sync_shop()


def test_etsys_categories_are_fetched_again_after_a_day(tmp_path: Path) -> None:
    cache = TaxonomyFile(tmp_path / "categories.json")
    now = Clock().now()
    assert cache.stale(now)  # none yet
    cache.save([(1, "Paper")], to_iso(now - timedelta(hours=23)))
    assert not cache.stale(now)
    cache.save([(1, "Paper")], to_iso(now - timedelta(hours=25)))
    assert cache.stale(now)


def test_the_plan_and_the_review_see_every_live_listing_top_sellers_first(data_dir: Path) -> None:
    """0.12.0: the plan saw only the newest 10 listings and the review the newest 8, so the oldest ones, live the
    longest and with the most views, dropped out first."""
    agent, _ = make_agent(data_dir, [])
    scope = agent.scope()
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        cycle_id = conn.execute(
            "INSERT INTO cycles (life_id, boot_id, started_at, status, trigger, simulated, cap_micros, session)"
            " VALUES (?, 'b', ?, 'running', 'schedule', 1, 0, ?)",
            (scope.life_id, now, scope.session),
        ).lastrowid
        for n in range(1, 16):  # listing 1 is the oldest, and the only one that sold
            approval_id = conn.execute(
                "INSERT INTO approvals (mode, session, life_id, cycle_id, created_at, type, title, description,"
                " payload, payload_sha256, expected_cost, expected_benefit, executor, action) VALUES (?, ?, ?, ?, ?,"
                " 'sell', 't', 'd', 'p', ?, 'c', 'b', 'etsy_listing', '{}')",
                (scope.mode, scope.session, scope.life_id, cycle_id, now, f"s{n}"),
            ).lastrowid
            conn.execute(
                "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, title,"
                " listing_id, state, views, favorites) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 'active', ?, ?)",
                (scope.mode, scope.session, approval_id, now, now, f"Planner {n}", 5_000 + n, 100 - n, n % 3),
            )
        conn.execute(
            "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, total, total_cents, currency, items,"
            " synced_at, status) VALUES (?, ?, 1, ?, '4.90 EUR', 490, 'EUR', ?, ?, 'paid')",
            (scope.mode, scope.session, now, json.dumps([{"listing_id": 5_001, "quantity": 2}]), now),
        )
    with agent.db.connection() as conn:
        shop = etsy_publisher.shop_text(conn, scope, agent.clock, "Shop", 3)
        review_text = review._etsy(conn, scope, to_iso(agent.clock.now() - timedelta(days=1)))
    summary = next(line for line in shop.splitlines() if line.startswith("All 15 live listings"))
    order = [int(part.split(" ", 1)[0][1:]) for part in summary.split(": ", 1)[1].split(" · ")]
    assert sorted(order) == list(range(5_001, 5_016)) and order[0] == 5_001  # all, the one that sold first
    assert "#5001 Planner 1 2s 99v 1f" in summary
    assert order[1:] == sorted(order[1:], key=lambda n: (-((n - 5_000) % 3), n))  # then the most favorited
    listed = [line for line in review_text.splitlines() if line.startswith("- #")]
    assert len(listed) == 15 and listed[0].startswith("- #5001 Planner 1 ·") and "2 sold in the period" in listed[0]


def test_the_shop_is_observed_once_a_day_and_its_listings_only_when_allowed(data_dir: Path) -> None:
    """0.12.0 (FIX NOW 4): every sync overwrote a listing's views and favorites, so nothing showed a trend. The shop's
    own counts are kept every day; the listings' numbers only while the owner allows the history (Etsy's terms)."""
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    agent.clock.advance(days=1)  # the day's first sync counts: today's came before the listing went live
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None  # twice in a day
    today = agent.clock.today().isoformat()
    observed = rows(agent, f"SELECT subject, metric, value FROM observations WHERE day = '{today}' ORDER BY id")
    assert [(o["subject"], o["metric"], o["value"]) for o in observed] == [
        ("shop", "listings_live", 1),
        ("shop", "orders", 0),
        ("shop", "units_sold", 0),
    ]
    agent.publisher.settings = agent.settings.model_copy(update={"etsy_stats_history": True})
    for _ in range(4):  # a week of syncs, every other day
        agent.clock.advance(days=2)
        assert agent.publisher.sync(force=True) is None
    listing = rows(
        agent, "SELECT day, value FROM observations WHERE subject = 'listing' AND metric = 'views' ORDER BY day"
    )
    assert len(listing) == 4 and len({o["day"] for o in listing}) == 4  # one a day
    assert listing[-1]["value"] >= listing[0]["value"]
    with agent.db.connection() as conn:
        shop = etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "Shop", 3)
    gained = listing[-1]["value"] - listing[0]["value"]  # since the first day of the last 7 observed
    assert f"v(+{gained})" in shop and "views v (gained this week)" in shop
    with pytest.raises(sqlite3.IntegrityError, match="never changes"), agent.db.transaction() as conn:
        conn.execute("UPDATE observations SET value = 0")
    with pytest.raises(sqlite3.IntegrityError, match="kept"), agent.db.transaction() as conn:
        conn.execute("DELETE FROM observations")
