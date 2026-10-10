"""0.19.0: Bluesky. The agent proposes a post (its words with a few hashtags, a link to one of its live Etsy listings or
a page of the owner's website, one of its pictures); the owner approves it as it is; Ember's code posts it once,
journaled, with a line saying an AI wrote it, and the owner's Undo deletes it; the sync reads the account's followers
and the posts' numbers for the plan, the metrics and the reach. The live client is tested against a mocked Bluesky;
nothing reaches Bluesky in a dry run."""

from __future__ import annotations

import base64
import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

httpx2 = pytest.importorskip("httpx2")

from app.agent import metrics, reach, tools  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind  # noqa: E402
from app.config import LoadedSettings, Settings  # noqa: E402
from app.db import Database, migrate  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import bluesky, bluesky_publisher, etsy, qa  # noqa: E402
from app.integrations.bluesky import (  # noqa: E402
    DISCLOSURE,
    AccountInfo,
    FakeBluesky,
    Login,
    NotSent,
    Picture,
    Post,
    PostRef,
    PostStats,
    Refused,
    Unclear,
    Upload,
)
from app.integrations.bluesky_connection import BlueskyConnection  # noqa: E402
from app.integrations.bluesky_live import LiveAccount, _Allowlist, allowed  # noqa: E402
from app.logging_setup import redact  # noqa: E402
from app.products import images  # noqa: E402
from tests.economy_helpers import START, FakeClock  # noqa: E402
from tests.test_agent import ROOMY, rows  # noqa: E402
from tests.test_etsy import Etsy, call, market_next, shop_context, views_approval  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

POSTING = ROOMY.model_copy(update={"bluesky_enabled": True})
PASSWORD = "fake-fake-fake-0000"  # an app password's shape, plainly not one
LIVE = Settings(bluesky_enabled=True, bluesky_handle="@Ember-Shop.bsky.social ", bluesky_app_password=PASSWORD)
LISTING = 900_000_001  # the fake shop's first listing
SITE = "https://www.example.de"
DID = "did:plc:abcdefghijklmnopqrstuvwx"
PDS = "morel.us-east.host.bsky.network"
URI = f"at://{DID}/app.bsky.feed.post/3mpost0000001"
WORDS = "Neu im Shop: ein Wochenplaner zum Ausdrucken, mit Einkaufsliste. #Wochenplaner #Printable"


def picture(width: int = 1200, height: int = 1500) -> bytes:
    return images.png(Image.new("RGB", (width, height), (240, 230, 220)))


def listed(data_dir: Path) -> tuple[Any, FakeTransport]:
    """A dry-run agent with Bluesky on (the fake account) and one live listing in the fake shop."""
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=4, settings=POSTING)
    request = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")[0]["id"]
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    return agent, fake


def post_context(agent: Any) -> tools.ToolContext:
    ctx = shop_context(agent)
    ctx.bluesky = tools.BlueskyAccess(FakeBluesky.HANDLE, 2, SITE)
    return ctx


def a_post(agent: Any, ctx: tools.ToolContext, write: bool = True, **args: Any) -> tools.Outcome:
    """A proposed post; its picture, if it names one, is made first unless ``write`` is False or it exists."""
    image = args.get("image")
    if image and write and agent.roots()[0].size_of(image) is None:
        agent.roots()[0].write_bytes(image, picture())
    fields = {
        "text": WORDS,
        "language": "de",
        "link": etsy.listing_url(LISTING),
        "reason": "People who plan their week follow #Wochenplaner; this brings them to the listing.",
        **args,
    }
    return call(ctx, "propose_bluesky_post", {k: v for k, v in fields.items() if v is not None})


def proposed(data_dir: Path, **args: Any) -> tuple[Any, FakeTransport, int]:
    agent, fake = listed(data_dir)
    made = a_post(agent, post_context(agent), **args)
    assert made.ok, made.text
    return agent, fake, rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'bluesky_post'")[0]["id"]


def posted(data_dir: Path, **args: Any) -> tuple[Any, FakeTransport, int]:
    agent, fake, request = proposed(data_dir, **args)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    return agent, fake, request


def post_rows(agent: Any) -> list[dict[str, Any]]:
    return rows(agent, "SELECT approval_id, status, sent, rkey, error FROM bluesky_posts ORDER BY id")


# --- what a post is ------------------------------------------------------------------------------------------------


def test_the_words_link_and_hashtags_become_bluesky_s_facets() -> None:
    """Bluesky marks links and hashtags by UTF-8 byte (without them a link isn't clickable), and Ember's code adds the
    AI line in the post's language."""
    upload = Upload("shop/post.png", "0" * 64, 10)
    words = "Für Bewerbungen: Tipps fürs Anschreiben 🤖 #Bewerbung #Anschreiben! #2026"
    post = Post(
        words, "de", "https://www.etsy.com/listing/123456789", image=upload, width=800, height=1000, alt_text="x"
    )
    text, facets = bluesky.layout(post)
    assert text == f"{words}\n\nwww.etsy.com/listing/1234...\n\n{DISCLOSURE['de']}"
    encoded = text.encode("utf-8")
    marked = [(encoded[f["index"]["byteStart"] : f["index"]["byteEnd"]].decode(), f["features"][0]) for f in facets]
    assert marked == [
        ("#Bewerbung", {"$type": "app.bsky.richtext.facet#tag", "tag": "Bewerbung"}),
        ("#Anschreiben", {"$type": "app.bsky.richtext.facet#tag", "tag": "Anschreiben"}),  # not its "!"
        (
            "www.etsy.com/listing/1234...",
            {"$type": "app.bsky.richtext.facet#link", "uri": "https://www.etsy.com/listing/123456789"},
        ),
    ]  # "#2026" is no hashtag (digits alone)
    made = bluesky.record(post, START, {"$type": "blob", "ref": {"$link": "bafk"}}, None, (800, 1000))
    assert made["$type"] == "app.bsky.feed.post" and made["createdAt"] == "2026-09-01T12:00:00.000Z"
    assert made["langs"] == ["de"] and made["text"] == text and made["facets"] == facets
    assert made["embed"] == {
        "$type": "app.bsky.embed.images",
        "images": [
            {
                "alt": "x",
                "image": {"$type": "blob", "ref": {"$link": "bafk"}},
                "aspectRatio": {"width": 800, "height": 1000},
            }
        ],
    }
    # Without a picture, a link Ember's records know the title of is a card, and not in the words.
    card = Post("Planner of the week", "en", "https://www.etsy.com/listing/1", link_title="Weekly planner")
    assert card.card() and bluesky.full_text(card) == f"Planner of the week\n\n{DISCLOSURE['en']}"
    thumb = {"$type": "blob", "ref": {"$link": "bafthumb"}}
    assert bluesky.record(card, START, None, thumb)["embed"] == {
        "$type": "app.bsky.embed.external",
        "external": {
            "uri": "https://www.etsy.com/listing/1",
            "title": "Weekly planner",
            "description": "",
            "thumb": thumb,
        },
    }
    plain = Post("Live numbers", "en", f"{SITE}/live.html")  # no title known: the link stays in the words
    assert not plain.card() and "www.example.de/live.html" in bluesky.full_text(plain)
    assert "embed" not in bluesky.record(plain, START)
    assert bluesky.post_from_action(json.loads(json.dumps(post.to_action()))) == post
    assert bluesky.room(post) == bluesky.TEXT_MAX - len(text)


def test_words_with_a_link_or_a_mention_are_refused() -> None:
    for words, why in (
        ("", "text is empty"),
        ("See https://example.com", "the words hold a link"),
        ("See www.example.com", "the words hold a link"),
        ("Thanks @someone.bsky.social!", "mention @someone.bsky.social"),
        ("Hi @bob", "mention @bob"),
        ("#" + "x" * 65, "longer than 64 characters"),
    ):
        with pytest.raises(bluesky.BlueskyError, match=why):
            bluesky.check_words(words)
    bluesky.check_words("Mail me: hello@example.com, or read #tips")  # an address is no mention
    assert bluesky.words("  One\r\n\r\n\r\n  two   words \n") == "One\n\ntwo words"


def test_the_options_need_a_handle_and_an_app_password() -> None:
    assert bluesky.config_problems(LIVE) == []
    assert bluesky.handle_of(LIVE) == "ember-shop.bsky.social"
    assert bluesky.config_problems(Settings(bluesky_enabled=True)) == [
        "bluesky_handle is missing",
        "bluesky_app_password is missing",
    ]
    own = LIVE.model_copy(
        update={"bluesky_handle": "not a handle", "bluesky_app_password": SecretStr("MyOwnPassw0rd!")}
    )
    problems = bluesky.config_problems(own)
    assert problems[0].startswith("bluesky_handle must be the account's handle")
    assert "never the account's own password" in problems[1]
    assert bluesky.config_problems(LIVE.model_copy(update={"bluesky_handle": "ember.example.de"})) == []
    public = LIVE.public_dict()
    assert "bluesky_app_password" not in public and public["bluesky_app_password_set"] is True


def test_a_picture_goes_out_as_a_small_jpeg_without_its_metadata() -> None:
    noisy = Image.effect_noise((3000, 4000), 90).convert("RGB")  # hard to compress: a big file
    out = io.BytesIO()
    info = images.PngImagePlugin.PngInfo()
    info.add_text(images.MARK, "what it shows")
    noisy.save(out, "PNG", pnginfo=info)
    data = out.getvalue()
    assert len(data) > bluesky.BLOB_MAX_BYTES
    sized = images.within(data, bluesky.BLOB_MAX_BYTES, bluesky.BLOB_LONGEST)
    assert sized.mime == "image/jpeg" and len(sized.data) <= bluesky.BLOB_MAX_BYTES
    assert max(sized.width, sized.height) <= bluesky.BLOB_LONGEST and sized.width * 4 == sized.height * 3
    assert images.within(data, bluesky.BLOB_MAX_BYTES, bluesky.BLOB_LONGEST) == sized  # the same bytes every time
    sent = Image.open(io.BytesIO(sized.data))
    assert sent.format == "JPEG" and images.MARK not in sent.info and "exif" not in sent.info
    clear = io.BytesIO()
    Image.new("RGBA", (40, 30), (0, 0, 0, 0)).save(clear, "PNG")
    small = images.within(clear.getvalue(), bluesky.BLOB_MAX_BYTES, bluesky.BLOB_LONGEST)
    assert (small.width, small.height) == (40, 30)
    assert Image.open(io.BytesIO(small.data)).convert("RGB").getpixel((5, 5)) >= (250, 250, 250)  # white, not black
    with pytest.raises(images.ImageError):
        images.within(b"not a picture", bluesky.BLOB_MAX_BYTES, bluesky.BLOB_LONGEST)


# --- the live client, against a mocked Bluesky ---------------------------------------------------------------------


def token(scope: str = "com.atproto.appPass", minutes: int = 120, salt: str = "") -> str:
    """A token shaped like Bluesky's (made here, so no token is in this file)."""

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    expires = int((START + timedelta(minutes=minutes)).timestamp())
    return f"{part({'typ': 'at+jwt'})}.{part({'scope': scope, 'sub': DID, 'exp': expires, 'jti': salt})}.c2lnbmF0dXJl"


def session(salt: str = "1", scope: str = "com.atproto.appPass", pds: str = f"https://{PDS}", **extra: Any) -> dict:
    service = {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": pds}
    return {
        "did": DID,
        "handle": "ember-shop.bsky.social",
        "accessJwt": token(scope, salt=f"access-{salt}"),
        "refreshJwt": token("com.atproto.refresh", 90 * 24 * 60, salt=f"refresh-{salt}"),
        "active": True,
        "didDoc": {"id": DID, "service": [service]},
        **extra,
    }


def blob(request: Any) -> dict[str, Any]:
    request.read()
    ref = {"$link": "bafkreiblob"}
    return {
        "blob": {"$type": "blob", "ref": ref, "mimeType": request.headers["content-type"], "size": len(request.content)}
    }


def live(answers: dict[tuple[str, str], Any]) -> tuple[LiveAccount, Etsy, FakeClock]:
    server = Etsy({(method, f"/xrpc/{name}"): answer for (method, name), answer in answers.items()})
    clock = FakeClock()
    return LiveAccount(LIVE, clock, Login(), httpx2.MockTransport(server)), server, clock


def names(server: Etsy) -> list[str]:
    return [r.url.path.removeprefix("/xrpc/") for r in server.requests]


def test_ember_logs_in_once_and_posts_to_the_account_s_own_server() -> None:
    account, server, _ = live(
        {
            ("POST", "com.atproto.server.createSession"): session(),
            ("POST", "com.atproto.repo.uploadBlob"): blob,
            ("POST", "com.atproto.repo.createRecord"): {"uri": URI, "cid": "bafyreicid", "validationStatus": "valid"},
            ("GET", "app.bsky.actor.getProfile"): {
                "did": DID,
                "handle": "ember-shop.bsky.social",
                "followersCount": 12,
                "postsCount": 3,
                "labels": [{"src": DID, "val": "bot"}, {"src": "did:plc:moderation", "val": "needs-review"}],
            },
            ("GET", "app.bsky.feed.getPosts"): {
                "posts": [
                    {
                        "uri": URI,
                        "likeCount": 5,
                        "repostCount": 2,
                        "replyCount": 1,
                        "quoteCount": 0,
                        "labels": [{"src": "did:plc:moderation", "val": "spam"}],
                    }
                ]
            },
            ("POST", "com.atproto.repo.deleteRecord"): {},
        }
    )
    upload = Upload("shop/post.png", "0" * 64, 10)
    post = Post("Wochenplaner #Printable", "de", "https://www.etsy.com/listing/1", image=upload, alt_text="Ein Planer")
    made = account.create_post(post, Picture(b"\xff\xd8jpeg", "image/jpeg", 800, 1000), None)
    assert made == PostRef(URI, "bafyreicid", "3mpost0000001")
    login, upload_request, create = server.requests
    assert login.url.host == "bsky.social"
    assert json.loads(login.content) == {"identifier": "ember-shop.bsky.social", "password": PASSWORD}
    assert upload_request.url.host == create.url.host == PDS  # the account's own server, as the login named it
    assert upload_request.headers["content-type"] == "image/jpeg" and upload_request.content == b"\xff\xd8jpeg"
    assert (
        upload_request.headers["authorization"] == create.headers["authorization"] == f"Bearer {account.login.access}"
    )
    sent = json.loads(create.content)
    assert (sent["repo"], sent["collection"]) == (DID, "app.bsky.feed.post")
    assert sent["record"]["text"] == f"Wochenplaner #Printable\n\nwww.etsy.com/listing/1\n\n{DISCLOSURE['de']}"
    assert sent["record"]["embed"]["images"][0] == {
        "alt": "Ein Planer",
        "image": {"$type": "blob", "ref": {"$link": "bafkreiblob"}, "mimeType": "image/jpeg", "size": 6},
        "aspectRatio": {"width": 800, "height": 1000},
    }
    assert account.info() == AccountInfo(
        "ember-shop.bsky.social", DID, followers=12, posts=3, labels=("needs-review",), automated=True
    )
    gone = f"at://{DID}/app.bsky.feed.post/3mgone"
    assert account.post_stats([URI, gone]) == {URI: PostStats(5, 2, 1, 0, ("spam",))}  # a deleted post is left out
    assert server.requests[-1].url.params.get_list("uris") == [URI, gone]
    account.delete_post("3mpost0000001")
    assert json.loads(server.requests[-1].content) == {
        "repo": DID,
        "collection": "app.bsky.feed.post",
        "rkey": "3mpost0000001",
    }
    assert names(server).count("com.atproto.server.createSession") == 1  # the login is kept (in memory only)
    secrets = f"{PASSWORD} {account.login.access} {account.login.refresh}"
    assert redact(secrets) == "*** *** ***"


def test_an_expired_token_is_renewed_and_the_call_made_again() -> None:
    seen: list[str] = []

    def profile(request: Any) -> Any:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx2.Response(400, json={"error": "ExpiredToken", "message": "Token has expired"})
        return {"did": DID, "handle": "ember-shop.bsky.social", "followersCount": 1}

    account, server, clock = live(
        {
            ("POST", "com.atproto.server.createSession"): session("1"),
            ("POST", "com.atproto.server.refreshSession"): session("2"),
            ("GET", "app.bsky.actor.getProfile"): profile,
        }
    )
    first_refresh = None

    def keep_first() -> None:
        nonlocal first_refresh
        account.keep_alive()
        first_refresh = account.login.refresh

    keep_first()
    assert account.info().followers == 1
    assert names(server) == [
        "com.atproto.server.createSession",
        "app.bsky.actor.getProfile",
        "com.atproto.server.refreshSession",
        "app.bsky.actor.getProfile",
    ]
    renew = server.requests[2]
    assert renew.url.host == "bsky.social" and renew.headers["authorization"] == f"Bearer {first_refresh}"
    assert seen[1] == f"Bearer {account.login.access}" != seen[0]
    # An access token about to end is renewed before the call.
    server.answers[("POST", "/xrpc/com.atproto.server.refreshSession")] = session("3")
    clock.advance(minutes=116)
    account.info()
    assert names(server)[-2:] == ["com.atproto.server.refreshSession", "app.bsky.actor.getProfile"]
    # A refresh token that no longer works: Ember logs in again.
    server.answers[("POST", "/xrpc/com.atproto.server.refreshSession")] = httpx2.Response(
        400, json={"error": "ExpiredToken", "message": "Token has been revoked"}
    )
    clock.advance(minutes=116)
    account.keep_alive()
    assert names(server)[-2:] == ["com.atproto.server.refreshSession", "com.atproto.server.createSession"]


def test_a_refused_login_isn_t_tried_again_for_an_hour() -> None:
    account, server, clock = live(
        {
            ("POST", "com.atproto.server.createSession"): httpx2.Response(
                401, json={"error": "AuthenticationRequired", "message": "Invalid identifier or password"}
            )
        }
    )
    with pytest.raises(Refused, match="the handle or the app password is wrong"):
        account.keep_alive()
    assert account.login.waiting(clock.now())
    with pytest.raises(Refused, match="AuthenticationRequired"):
        account.info()
    assert len(server.requests) == 1  # not tried again: every refused login counts against the account
    clock.advance(minutes=61)
    server.answers[("POST", "/xrpc/com.atproto.server.createSession")] = session()
    account.keep_alive()
    assert len(server.requests) == 2 and account.login.refused == "" and not account.login.waiting(clock.now())
    # A rate limit is no refusal: tried again at the next round.
    limited, server, clock = live(
        {("POST", "com.atproto.server.createSession"): httpx2.Response(429, json={"error": "RateLimitExceeded"})}
    )
    with pytest.raises(NotSent) as caught:
        limited.keep_alive()
    assert not isinstance(caught.value, Refused) and not limited.login.waiting(clock.now())


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (session(scope="com.atproto.access"), "the options hold the account's own password"),
        (session(active=False, status="takendown"), "the account isn't active \\(takendown\\)"),
    ],
)
def test_the_account_s_own_password_and_an_inactive_account_are_refused(answer: dict, why: str) -> None:
    account, _, clock = live({("POST", "com.atproto.server.createSession"): answer})
    with pytest.raises(Refused, match=why):
        account.keep_alive()
    assert account.login.access == "" and account.login.waiting(clock.now())


def test_a_server_bluesky_doesn_t_host_isn_t_used() -> None:
    account, server, _ = live(
        {
            ("POST", "com.atproto.server.createSession"): session(pds="https://pds.example.com"),
            ("GET", "app.bsky.actor.getProfile"): {"did": DID, "handle": "ember-shop.bsky.social"},
        }
    )
    assert account.info().automated is False
    assert [r.url.host for r in server.requests] == ["bsky.social", "bsky.social"]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (
            httpx2.Response(400, json={"error": "InvalidRequest", "message": "Invalid app.bsky.feed.post record"}),
            NotSent,
        ),
        (httpx2.Response(503, json={}), Unclear),
        (httpx2.ReadTimeout("slow"), Unclear),
        (httpx2.ConnectError("down"), NotSent),
    ],
)
def test_errors_say_whether_bluesky_may_have_made_the_post(answer: Any, error: type) -> None:
    account, _, _ = live(
        {
            ("POST", "com.atproto.server.createSession"): session(),
            ("POST", "com.atproto.repo.createRecord"): answer,
        }
    )
    with pytest.raises(error):
        account.create_post(Post("Hello", "en"), None, None)


def test_only_bluesky_s_servers_can_be_reached() -> None:
    assert allowed("bsky.social") and allowed(PDS)
    for host in ("host.bsky.network", "bsky.social.example.com", "evil.host.bsky.network.example.com", "bsky.app"):
        assert not allowed(host), host
    for url in ("https://example.com/xrpc/x", "http://bsky.social/xrpc/x", "https://bsky.social:8443/xrpc/x"):
        with pytest.raises(httpx2.ConnectError, match="only talks to Bluesky's servers"):
            _Allowlist(retries=0).handle_request(httpx2.Request("GET", url))


def test_the_connection_says_what_it_waits_for(tmp_path: Path) -> None:
    db = Database(tmp_path / "ember.db")
    migrate(db.path)
    clock = FakeClock()
    connection = BlueskyConnection(db, clock, LIVE, "live", 1)
    assert connection.status() == ("ok", None) and isinstance(connection.account(), LiveAccount)
    assert connection.handle() == "ember-shop.bsky.social"
    connection.login.refused, connection.login.refused_at = "HTTP 401: AuthenticationRequired", clock.now()
    status, reason = connection.status()
    assert status == "not_connected" and "check the handle and the app password" in str(reason)
    assert connection.account() is None
    clock.advance(minutes=61)
    assert connection.status() == ("ok", None) and connection.account() is not None
    unset = BlueskyConnection(db, clock, Settings(bluesky_enabled=True), "live", 1)
    assert unset.status()[0] == "not_configured" and unset.account() is None
    off = BlueskyConnection(db, clock, LIVE.model_copy(update={"bluesky_enabled": False}), "live", 1)
    assert off.status() == ("disabled", None) and off.account() is None and off.handle() is None
    assert PASSWORD not in json.dumps(connection.describe())
    db.close()


# --- the agent's tools, with the dry run's fake account ------------------------------------------------------------


def test_the_bluesky_tools_come_with_the_account() -> None:
    assert {"bluesky_posts", "propose_bluesky_post"} == tools.BLUESKY_TOOLS
    assert {d["name"] for d in tools.definitions(bluesky=True)} >= tools.BLUESKY_TOOLS
    for kinds in ({}, {"etsy": True}, {"bluesky": True, "venture": True}):
        assert not {d["name"] for d in tools.definitions(**kinds)} & tools.BLUESKY_TOOLS
    assert "bluesky" in tools.GUIDES
    topics = next(d for d in tools.definitions() if d["name"] == "guide")["input_schema"]["properties"]["topic"]
    assert "bluesky" not in topics["enum"]  # its manual only with the account
    guide = tools.guide_text("bluesky")
    assert f"At most {bluesky.TEXT_MAX} characters" in guide and f"Up to {qa.POST_TAGS} #hashtags" in guide
    assert "{" not in guide


def test_a_post_is_checked_before_it_reaches_the_owner(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    shown = call(ctx, "bluesky_posts", {})
    head = f"Ember's Bluesky account: @{FakeBluesky.HANDLE} (0 posted today, at most 2 a day)."  # 0.37.3: today's
    assert shown.ok and f"{head}\nNo post of yours yet." == shown.text
    agent.roots()[0].write_bytes("shop/notes.pdf", b"%PDF-1.7 x")
    for args, message in (
        ({"text": "Look: https://example.com"}, "the words hold a link"),
        ({"text": "Danke @someone.bsky.social!"}, "mention @someone.bsky.social"),
        ({"link": "https://example.com/planner"}, "other sites aren't yours to promote"),
        ({"link": "http://www.etsy.com/listing/1"}, "link must be an https address"),
        ({"link": "https://www.etsy.com/listing/12"}, "#12 isn't one of your live listings"),
        ({"image": "shop/post.png"}, "alt_text is missing"),
        ({"alt_text": "A planner"}, "alt_text describes a picture"),
        ({"image": "shop/notes.pdf", "alt_text": "x"}, "a post's picture is a .png or .jpg file"),
        ({"image": "shop/missing.png", "alt_text": "x", "write": False}, "missing.png"),
        ({"language": "fr"}, "language"),
        ({"text": "x" * bluesky.TEXT_CHARS, "image": "shop/post.png", "alt_text": "x"}, "shorten your words by"),
    ):
        write = args.pop("write", True)
        refused = a_post(agent, ctx, write, **args)
        assert not refused.ok and message in refused.text, (args, refused.text)
    assert rows(agent, "SELECT id FROM approvals WHERE executor = 'bluesky_post'") == []
    made = a_post(agent, ctx)
    assert made.ok, made.text
    assert "Nothing is on Bluesky yet" in made.text and "characters to spare" in made.text and "QA" not in made.text
    [request] = rows(agent, "SELECT * FROM approvals WHERE executor = 'bluesky_post'")
    assert (request["type"], request["title"]) == ("publish", f"Bluesky: {WORDS}"[:120])
    action = json.loads(request["action"])
    listing = rows(agent, "SELECT listing_id FROM etsy_listings WHERE status = 'active'")
    assert listing == [{"listing_id": LISTING}] and action["link"] == etsy.listing_url(LISTING)
    assert action["link_title"] and action["card_photo"]["path"] and action["tags"] == ["Wochenplaner", "Printable"]
    photo = action["card_photo"]["path"]
    assert request["payload"] == (
        f"Account: @{FakeBluesky.HANDLE}\nLanguage: de\n"
        f"Link: {etsy.listing_url(LISTING)} (a card: {action['link_title']!r}, with the photo {photo})\n\n"
        f"{WORDS}\n\n{DISCLOSURE['de']}"
    )
    shown = views_approval(agent, request["id"])
    assert shown["qa"] == [] and shown["execution"] is None and shown["executor"] == "bluesky_post"
    assert shown["action_class"]["name"] == "bluesky.create_post" and shown["action_class"]["reversible"]
    many = a_post(agent, ctx, text="Planer #eins #zwei #drei #vier", link=None)
    assert many.ok and "QA (Ember's code): 4 hashtags: more than 3 read as spam on Bluesky" in many.text


def test_a_link_to_the_owner_s_website(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO blog_posts (mode, session, slug, title, description, day, position, seen_at)"
            " VALUES (?, ?, 'wochenplan', 'Der Wochenplan', 'So planst du deine Woche.', '2026-09-01', 0, 'x')",
            (scope.mode, scope.session),
        )
    made = a_post(agent, ctx, text="Neu im Blog: so planst du deine Woche.", link=f"{SITE}/blog/wochenplan.html")
    assert made.ok, made.text
    action = json.loads(rows(agent, "SELECT action FROM approvals WHERE executor = 'bluesky_post'")[-1]["action"])
    assert (action["link_title"], action["link_description"]) == ("Der Wochenplan", "So planst du deine Woche.")
    assert action["card_photo"] is None
    agent.db.set_meta(f"integrations.live.{scope.mode}.on_server", '["live.html", "live/banner.svg"]')  # 0.19.2
    plain = a_post(agent, ctx, text="Meine Zahlen, live.", link=f"{SITE}/live.html")
    assert plain.ok, plain.text
    payload = rows(agent, "SELECT payload FROM approvals WHERE executor = 'bluesky_post'")[-1]["payload"]
    assert f"Link: {SITE}/live.html\n" in payload and "\n\nwww.example.de/live.html\n\n" in payload
    ctx.bluesky = tools.BlueskyAccess(FakeBluesky.HANDLE, 2, "")
    ctx.state = tools.CycleTools()  # another cycle (two posts a cycle at most)
    unset = a_post(agent, ctx, text="Mehr dazu.", link=f"{SITE}/live.html")
    assert not unset.ok and "your owner hasn't set its address, site_url" in unset.text


# --- carrying it out -------------------------------------------------------------------------------------------------


def test_an_approved_post_is_posted_once_and_the_undo_deletes_it(data_dir: Path) -> None:
    agent, _, request = posted(data_dir)
    account = agent.bluesky.account()
    [rkey] = list(account.state["posts"])
    made = account.state["posts"][rkey]
    assert made["card"] is True and made["thumb_bytes"] > 0 and made["text"].endswith(DISCLOSURE["de"])
    assert post_rows(agent) == [{"approval_id": request, "status": "active", "sent": 1, "rkey": rkey, "error": None}]
    closed = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {request}")[0]
    assert (closed["status"], closed["closed_by"]) == ("done", "Ember")
    assert "fake account" in closed["result_note"] and closed["result_link"] == bluesky.post_url(FakeBluesky.DID, rkey)
    [journal] = rows(agent, f"SELECT class, status, subject, undo FROM action_journal WHERE approval_id = {request}")
    assert (journal["class"], journal["status"], journal["subject"]) == ("bluesky.create_post", "simulated", rkey)
    assert json.loads(journal["undo"]) == {"action": "delete_post", "rkey": rkey}
    assert agent.execute_approved() == []  # never twice
    shown = views_approval(agent, request)
    assert shown["execution"]["status"] == "active"
    assert shown["execution"]["url"] == bluesky.post_url(FakeBluesky.DID, rkey)
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "bluesky.create_post")
    assert entry["undo"] == {"label": "Delete the post", "why_not": None, "request": None}
    reply = owner(agent).undo(entry["id"], "Stefan")
    assert reply.status == 200, reply.body
    undo = int(reply.body["approval_id"])
    undone = rows(agent, f"SELECT executor, status, decided_by, action FROM approvals WHERE id = {undo}")[0]
    assert (undone["executor"], undone["status"], undone["decided_by"]) == ("bluesky_delete", "approved", "Stefan")
    assert json.loads(undone["action"]) == {"rkey": rkey}
    done = owner(agent).close(undo, {"outcome": "done"}, "Stefan")
    assert done.status == 422 and "carries approved posts out itself" in done.body["error"]
    assert agent.execute_approved() == [(undo, "done")]
    assert account.state["posts"] == {} and post_rows(agent)[0]["status"] == "deleted"
    assert views_approval(agent, undo)["execution"]["status"] == "deleted"
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "bluesky.create_post")
    assert entry["undo"]["why_not"] == "it is undone"


def test_a_changed_picture_and_the_daily_limit(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir, image="shop/post-1.png", alt_text="Ein Wochenplaner auf dem Tisch")
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.settings = agent.bluesky_posts.settings = agent.settings.model_copy(update={"bluesky_posts_per_day": 0})
    assert agent.execute_approved() == [(request, "waiting_limit")]
    assert views_approval(agent, request)["execution"]["status"] == "waiting_limit"
    agent.settings = agent.bluesky_posts.settings = agent.settings.model_copy(update={"bluesky_posts_per_day": 2})
    agent.roots()[0].write_bytes("shop/post-1.png", picture(1000, 1400))
    assert agent.execute_approved() == [(request, "failed")]
    [row] = post_rows(agent)
    assert row["rkey"] is None and row["sent"] == 0 and "changed after it was approved" in row["error"]
    assert agent.bluesky.account().state["posts"] == {}  # nothing reached Bluesky
    with agent.db.connection() as conn:
        assert bluesky_publisher.created_today(conn, agent.clock, agent.scope()) == 0  # nor counts toward the limit


def test_a_post_with_a_picture_sends_it_smaller(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir, image="shop/post-1.png", alt_text="Ein Wochenplaner auf dem Tisch")
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "active")]
    [made] = agent.bluesky.account().state["posts"].values()
    assert 0 < made["image_bytes"] <= bluesky.BLOB_MAX_BYTES and made["card"] is False
    assert "www.etsy.com/listing/9000..." in made["text"]  # with a picture, the link is in the words


def test_a_crash_while_posting_is_unclear_and_never_repeated(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bluesky_posts (mode, session, approval_id, text, status, sent, started_at)"
            " VALUES (?, ?, ?, 'Hello', 'running', 1, '2026-09-01T10:00:00Z')",
            (scope.mode, scope.session, request),
        )
    assert agent.bluesky_posts.recover() == 1
    assert post_rows(agent)[0]["status"] == "unclear"
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {request}")[0]["status"] == "failed"
    assert agent.execute_approved() == []


def test_while_bluesky_refuses_the_login_approved_posts_wait(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")

    class Refusing:
        simulated = False

        def keep_alive(self) -> None:
            raise Refused("Bluesky refused Ember's login: HTTP 401: AuthenticationRequired")

    agent.bluesky_posts.account = lambda: Refusing()
    assert agent.bluesky_posts.run() == []
    assert post_rows(agent) == []  # nothing begun
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {request}")[0]["status"] == "approved"
    error = agent.db.get_meta(bluesky_publisher.meta_key(agent.scope().mode, "last_error"))
    assert "AuthenticationRequired" in error


def test_the_owner_approves_a_post_as_it_is_or_cancels_it(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    who = owner(agent)
    changed = who.decide(request, {"decision": "approve_with_changes", "final_payload": "Mine"}, "Owner")
    assert changed.status == 422 and "approve a post as it is" in changed.body["error"]
    assert who.decide(request, {"decision": "approve"}, "Owner").status == 200
    done = who.close(request, {"outcome": "done"}, "Owner")
    assert done.status == 422 and "to stop one, cancel it" in done.body["error"]
    assert who.close(request, {"outcome": "failed"}, "Owner").status == 200
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert note == "Cancelled by the owner before Ember's code carried it out"
    assert agent.execute_approved() == [] and post_rows(agent) == []


def test_a_post_deleted_at_bluesky_is_noted_at_the_sync(data_dir: Path) -> None:
    agent, _, _ = posted(data_dir)
    account = agent.bluesky.account()
    [rkey] = list(account.state["posts"])
    account.delete_post(rkey)  # the owner deleted it in the app
    assert agent.bluesky_posts.sync(force=True) is None
    [row] = rows(agent, "SELECT status, result FROM bluesky_posts")
    assert (row["status"], row["result"]) == ("deleted", "Deleted at Bluesky, not by Ember's code")
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "bluesky.create_post")
    assert entry["undo"]["why_not"] == "the post isn't on Bluesky anymore"


def test_posts_are_read_for_the_plan_the_metrics_and_the_reach(data_dir: Path) -> None:
    agent, fake, request = posted(data_dir)
    agent.clock.advance(hours=24 * 7)
    before = len(fake.sent)
    market_next(agent, "bluesky")  # 0.35.0: a marketing cycle's plan has the BLUESKY section
    agent.run_cycle("schedule")
    [row] = rows(agent, "SELECT finished_at, likes, reposts, replies, quotes, synced_at FROM bluesky_posts")
    assert (row["likes"], row["reposts"], row["replies"], row["quotes"]) == (14, 3, 2, 1) and row["synced_at"]
    plan = next(r for r in list(fake.sent)[before:] if request_kind(r) == "plan")
    text = plan["messages"][0]["content"][0]["text"]
    assert f"\n== BLUESKY ==\nEmber's account: @{FakeBluesky.HANDLE} (3 followers; at most 2 posts a day).\n" in text
    day = row["finished_at"][:10]  # 0.37.3: each by its request's number and day
    assert f"- request #{request}, {day} (active): {WORDS[:60]} -> {etsy.listing_url(LISTING)}: 14 likes" in text
    work = next(r for r in list(fake.sent)[before:] if request_kind(r) == "work")
    assert {t["name"] for t in work["tools"]} >= tools.BLUESKY_TOOLS
    now = to_iso(agent.clock.now())
    with agent.db.connection() as conn:
        for name, value in (("bluesky_posts_live", 1), ("bluesky_reactions", 20)):
            milestone = {"metric": name, "project_id": None, "venture_id": None, "created_at": now}
            assert metrics.read(conn, agent.scope(), milestone, None, now).value == value  # type: ignore[arg-type]
            assert metrics.CATALOGUE[name].source == "bluesky"
        funnel = next(iter(reach.funnels(conn, agent.scope()).values()))
    assert funnel.bluesky == 1 and "1 Bluesky post(s)" in funnel.text()
    shown = agent.integrations()["bluesky"]
    assert (shown["mode"], shown["status"], shown["handle"], shown["followers"]) == (
        "fake",
        "ok",
        FakeBluesky.HANDLE,
        3,
    )
    assert shown["automated"] is True and shown["posts"][0]["likes"] == 14 and shown["last_sync_at"]


def test_the_dashboard_shows_nothing_while_bluesky_is_off(ingress_client: TestClient) -> None:
    data = ingress_client.get("api/dashboard").json()
    assert (data["integrations"]["bluesky"]["status"], data["integrations"]["bluesky"]["posts"]) == ("disabled", [])


def test_the_app_password_is_never_shown(client_factory: Any) -> None:
    with client_factory(LoadedSettings(LIVE.model_copy(update={"dry_run": False}))) as client:
        dashboard = client.get("api/dashboard")
        report = client.get("api/diagnostics")
    assert dashboard.status_code == 200 and PASSWORD not in dashboard.text
    assert dashboard.json()["integrations"]["bluesky"]["handle"] == "ember-shop.bsky.social"
    assert PASSWORD not in report.text


def test_nothing_is_offered_or_made_while_bluesky_is_off(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1)
    assert agent.bluesky.account() is None and agent.bluesky.handle() is None
    work = next(r for r in fake.sent if request_kind(r) == "work")
    assert not {t["name"] for t in work["tools"]} & tools.BLUESKY_TOOLS
    assert agent.execute_approved() == []
