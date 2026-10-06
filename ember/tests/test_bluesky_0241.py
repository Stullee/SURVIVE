"""0.24.1: a Bluesky post carries a second link. The owner asked for the website in every post as well as the shop
("double chance for traffic", message #127); a post had one link, so the agent named the shop in its words, under a
name that wasn't the shop's. propose_bluesky_post's link takes two addresses, separated by a space: each is checked as
the link is (one of Ember's live listings, or a page of the owner's website Ember's code knows), never the same twice;
the second shows in the words as a clickable link, the AI line stays last, and the post keeps within Bluesky's 300
characters. The approval shows it, Ember's code checks it again when it posts, and the reach of the product line it
links counts the post."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import reach, tools  # noqa: E402
from app.integrations import bluesky, bluesky_publisher, etsy  # noqa: E402
from app.integrations.bluesky import DISCLOSURE, Post, Upload  # noqa: E402
from tests.economy_helpers import START  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_bluesky import LISTING, SITE, a_post, listed, post_context, post_rows  # noqa: E402
from tests.test_fixes_0192 import blog_post  # noqa: E402
from tests.test_fixes_0233 import line_of, park  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

LISTING_URL = etsy.listing_url(LISTING)
PAGE = f"{SITE}/blog/wochenplan.html"


def marked(text: str, facets: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Each facet's part of the text (Bluesky marks them by UTF-8 byte) with what it marks."""
    encoded = text.encode("utf-8")
    return [(encoded[f["index"]["byteStart"] : f["index"]["byteEnd"]].decode(), f["features"][0]) for f in facets]


def link(uri: str) -> dict[str, str]:
    return {"$type": "app.bsky.richtext.facet#link", "uri": uri}


def on_the_site(agent: Any) -> None:
    """The owner's website as Ember's code knows it, for the posts' checks when they are made."""
    agent.settings = agent.settings.model_copy(update={"site_url": SITE})
    agent.bluesky_posts.settings = agent.settings


def requests(agent: Any) -> list[int]:
    return [r["id"] for r in rows(agent, "SELECT id FROM approvals WHERE executor = 'bluesky_post' ORDER BY id")]


# --- what a post with two links is ----------------------------------------------------------------------------------


def test_both_links_are_clickable_in_the_words_and_the_ai_line_stays_last() -> None:
    """With a picture both links are in the words, each on a line of its own, each marked as a link by UTF-8 byte (the
    words before them hold umlauts and an emoji: more bytes than characters)."""
    page = f"{SITE}/blog/anschreiben-tipps.html"
    words = "Für Bewerbungen: Tipps fürs Anschreiben 🤖 #Bewerbung"
    upload = Upload("shop/post.png", "0" * 64, 10)
    post = Post(words, "de", LISTING_URL, image=upload, width=800, height=1000, alt_text="x", second_link=page)
    text, facets = bluesky.layout(post)
    assert text == f"{words}\n\nwww.etsy.com/listing/9000...\nwww.example.de/blog/anschre...\n\n{DISCLOSURE['de']}"
    assert marked(text, facets) == [
        ("#Bewerbung", {"$type": "app.bsky.richtext.facet#tag", "tag": "Bewerbung"}),
        ("www.etsy.com/listing/9000...", link(LISTING_URL)),
        ("www.example.de/blog/anschre...", link(page)),
    ]
    made = bluesky.record(post, START, {"$type": "blob", "ref": {"$link": "bafk"}}, None, (800, 1000))
    assert made["text"] == text and made["facets"] == facets and made["embed"]["$type"] == "app.bsky.embed.images"
    # 300 characters with both links and the AI line: the second link's line counts too.
    assert bluesky.room(post) == bluesky.TEXT_MAX - len(text)
    assert bluesky.room(replace(post, second_link=None)) - bluesky.room(post) == len("\nwww.example.de/blog/anschre...")


def test_without_a_picture_the_link_is_the_card_and_the_second_link_is_in_the_words() -> None:
    card = Post("Neu im Blog #Bewerbung", "en", LISTING_URL, link_title="CV template", second_link=PAGE)
    assert card.card()
    text, facets = bluesky.layout(card)
    assert text == f"Neu im Blog #Bewerbung\n\nwww.example.de/blog/wochenp...\n\n{DISCLOSURE['en']}"
    assert marked(text, facets) == [
        ("#Bewerbung", {"$type": "app.bsky.richtext.facet#tag", "tag": "Bewerbung"}),
        ("www.example.de/blog/wochenp...", link(PAGE)),
    ]
    thumb = {"$type": "blob", "ref": {"$link": "bafthumb"}}
    made = bluesky.record(card, START, None, thumb)
    assert made["embed"] == {
        "$type": "app.bsky.embed.external",
        "external": {"uri": LISTING_URL, "title": "CV template", "description": "", "thumb": thumb},
    }
    plain = replace(card, link_title="")  # no title known: both links in the words
    assert bluesky.full_text(plain) == (
        f"Neu im Blog #Bewerbung\n\nwww.etsy.com/listing/9000...\nwww.example.de/blog/wochenp...\n\n{DISCLOSURE['en']}"
    )


def test_the_post_keeps_its_second_link_and_the_owner_sees_it() -> None:
    card = Post("Planner of the week", "en", LISTING_URL, link_title="Weekly planner", second_link=PAGE)
    action = json.loads(json.dumps(card.to_action()))
    assert action["second_link"] == PAGE and bluesky.post_from_action(action) == card
    del action["second_link"]  # a post proposed before 0.24.1
    assert bluesky.post_from_action(action) == replace(card, second_link=None)
    shown = bluesky.payload(card, "ember-shop.bsky.social")
    assert shown.split("\n")[:4] == [
        "Account: @ember-shop.bsky.social",
        "Language: en",
        f"Link: {LISTING_URL} (a card: 'Weekly planner')",
        f"Second link: {PAGE}",
    ]
    assert shown.endswith(f"\n\n{bluesky.full_text(card)}")


# --- proposing it ---------------------------------------------------------------------------------------------------


def test_the_second_link_is_checked_as_the_link_is(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "wochenplan")
    missing = f"{SITE}/blog/wochenplaner.html"  # by the file's name: no page of the blog
    for given, message in (
        (
            f"{LISTING_URL} {missing}",
            f"{missing} isn't a page of your owner's website that Ember's code knows is there: link one of {PAGE},"
            f" {SITE}/blog/, {SITE}/",
        ),
        (f"{LISTING_URL} https://example.org/planer.html", "the second link goes to one of your live Etsy listings"),
        (f"{LISTING_URL} https://www.etsy.com/listing/12", "#12 isn't one of your live listings"),
        (f"{LISTING_URL} http://www.example.de/", "the second link must be an https address"),
        (f"http://www.example.de/ {LISTING_URL}", "link must be an https address"),  # the link, as before
        # The same address twice, however it is written: a listing's or a page's.
        (f"{LISTING_URL} https://www.etsy.com/de/listing/{LISTING}/wochenplaner", f"link names {LISTING_URL} twice"),
        (f"{PAGE} {SITE}/blog/wochenplan", f"link names {PAGE} twice"),
        (f"{LISTING_URL} {PAGE} {SITE}/", "link takes one address, or two separated by a space"),
    ):
        refused = a_post(agent, ctx, link=given)
        assert not refused.ok and message in refused.text, (given, refused.text)
    assert requests(agent) == []
    long = a_post(agent, ctx, text="x" * (bluesky.TEXT_CHARS - 10), link=f"{LISTING_URL} {PAGE}")
    assert not long.ok and "characters with the links and the AI line" in long.text, long.text
    assert "shorten your words by 22" in long.text  # 10 short of the most, and 32 for the second link ("\n\n" and 30)
    made = a_post(agent, ctx, link=f"{LISTING_URL} {SITE}/blog/wochenplan")  # a blog post's address, without .html
    assert made.ok and "characters to spare" in made.text, made.text
    [request] = rows(agent, "SELECT payload, action FROM approvals WHERE executor = 'bluesky_post'")
    action = json.loads(request["action"])
    assert (action["link"], action["second_link"]) == (LISTING_URL, PAGE)  # as Ember's code knows them
    assert action["link_title"] and action["card_photo"]["path"]  # without a picture, the listing is the card
    assert f"\nLink: {LISTING_URL} (a card: " in request["payload"]
    assert f"\nSecond link: {PAGE}\n" in request["payload"]
    assert request["payload"].endswith(f"\n\nwww.example.de/blog/wochenp...\n\n{DISCLOSURE['de']}")


def test_the_tool_says_link_takes_a_second_address() -> None:
    [spec] = [d for d in tools.definitions(bluesky=True) if d["name"] == "propose_bluesky_post"]
    field = spec["input_schema"]["properties"]["link"]
    assert field["description"].startswith("One or two, space-separated: a live Etsy listing's address")
    assert field["maxLength"] == 2 * bluesky.LINK_MAX + 1


# --- posting it -----------------------------------------------------------------------------------------------------


def test_an_approved_post_whose_second_link_is_gone_isnt_posted(data_dir: Path) -> None:
    """A page gone from the blog, a listing no longer live: checked again when the post is made, as the link is."""
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "wochenplan")
    blog_post(agent, "alt", "Alt")
    on_the_site(agent)
    for words, given in (
        ("Neu im Blog: so planst du deine Woche.", f"{PAGE} {SITE}/blog/alt.html"),
        ("Der Wochenplan im Blog, die Vorlage im Shop.", f"{PAGE} {LISTING_URL}"),
    ):
        assert a_post(agent, ctx, text=words, link=given).ok
    gone, unlisted = requests(agent)
    for request in (gone, unlisted):
        assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    with agent.db.transaction() as conn:
        conn.execute("DELETE FROM blog_posts WHERE slug = 'alt'")
        conn.execute("UPDATE etsy_listings SET state = 'inactive'")
    assert dict(agent.execute_approved()) == {gone: "failed", unlisted: "failed"}
    assert agent.bluesky.account().state["posts"] == {}  # nothing reached Bluesky
    notes = {r["id"]: r["result_note"] for r in rows(agent, "SELECT id, result_note FROM approvals")}
    assert notes[gone] == (
        f"Not posted: {SITE}/blog/alt.html isn't a page of your owner's website that Ember's code knows is there (a"
        " blog post's address ends in .html, as BLOG gives it)"
    )
    assert notes[unlisted] == f"Not posted: #{LISTING} isn't live at Etsy any more (deactivated)"
    assert [(r["status"], r["sent"]) for r in post_rows(agent)] == [("failed", 0), ("failed", 0)]


def test_a_second_link_without_its_html_goes_out_at_its_address(data_dir: Path) -> None:
    """As the link (0.19.2): a blog post's address without its ".html" is posted with it, and the result says so."""
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "wochenplan")
    on_the_site(agent)
    assert a_post(agent, ctx, link=f"{LISTING_URL} {PAGE}").ok
    [request] = requests(agent)
    with agent.db.transaction() as conn:  # as an approval may hold it: the page by its address without ".html"
        conn.execute("DROP TRIGGER approvals_action_fixed")
        action = json.loads(conn.execute(f"SELECT action FROM approvals WHERE id = {request}").fetchone()[0])
        action["second_link"] = PAGE.removesuffix(".html")
        conn.execute("UPDATE approvals SET action = ? WHERE id = ?", (json.dumps(action), request))
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    [made] = agent.bluesky.account().state["posts"].values()
    assert (made["link"], made["second_link"], made["card"]) == (LISTING_URL, PAGE, True)
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert f"Its second link went out as {PAGE}: the approved address named no page there." in note
    assert "Its link went out" not in note


# --- what it brings -------------------------------------------------------------------------------------------------


def test_a_live_post_counts_for_the_product_line_its_second_link_links(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "wochenplan")
    on_the_site(agent)
    # The blog's post as the card, the listing in the words: only the second link names the product line.
    assert a_post(agent, ctx, text="Neu im Blog: so planst du deine Woche.", link=f"{PAGE} {LISTING_URL}").ok
    [first] = requests(agent)
    assert owner(agent).decide(first, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(first, "active")]
    [made] = agent.bluesky.account().state["posts"].values()
    assert made["card"] is True and made["text"].endswith(f"\n\nwww.etsy.com/listing/9000...\n\n{DISCLOSURE['de']}")
    assert rows(agent, "SELECT link, second_link FROM bluesky_posts") == [{"link": PAGE, "second_link": LISTING_URL}]
    scope = agent.scope()
    with agent.db.connection() as conn:
        [funnel] = reach.funnels(conn, scope).values()
        assert funnel.listings == [LISTING] and funnel.bluesky == 1
        assert f" -> {PAGE} and {LISTING_URL}: " in bluesky_publisher.text(conn, scope)
        [shown] = [bluesky_publisher.post_json(r) for r in bluesky_publisher.posts(conn, scope)]
        assert (shown["link"], shown["second_link"]) == (PAGE, LISTING_URL)
    # The listing as the link and the page as the second: one post more, counted once for the line it links.
    ctx.state = tools.CycleTools()
    assert a_post(agent, ctx, text="Der Wochenplaner zum Ausdrucken.", link=f"{LISTING_URL} {PAGE}").ok
    second = requests(agent)[-1]
    assert owner(agent).decide(second, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(second, "active")]
    with agent.db.connection() as conn:
        [funnel] = reach.funnels(conn, scope).values()
    assert funnel.bluesky == 2 and "2 Bluesky post(s)" in funnel.text()


# --- a line your owner stopped --------------------------------------------------------------------------------------


def test_a_second_link_to_a_listing_the_owner_parked_stops_the_post(data_dir: Path) -> None:
    """0.23.3's stop holds for the second link too: a post approved before the owner's park isn't carried out, and no
    new post links the stopped line's listing (here only the second link does: the link is the blog's page)."""
    agent, _ = listed(data_dir)
    ctx = post_context(agent)
    blog_post(agent, "wochenplan")
    assert a_post(agent, ctx, text="Neu im Blog: so planst du deine Woche.", link=f"{PAGE} {LISTING_URL}").ok
    [request] = requests(agent)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    _, leg = line_of(agent)
    park(agent, leg)
    [row] = rows(agent, f"SELECT status, result_note FROM approvals WHERE id = {request}")
    assert row == {
        "status": "failed",
        "result_note": f"Not carried out: your owner parked venture #{leg} before Ember's code carried it out.",
    }
    assert agent.execute_approved() == [] and post_rows(agent) == []
    ctx.state = tools.CycleTools()
    refused = a_post(agent, ctx, text="Der Wochenplan, neu im Blog.", link=f"{PAGE} {LISTING_URL}")
    assert not refused.ok and (
        f"#{LISTING} is a listing of venture #{leg}, which your owner parked: no post for it until they take it up"
        " again" in refused.text
    )
    assert a_post(agent, ctx, text="Der Wochenplan, neu im Blog.", link=PAGE).ok  # the blog's page alone goes on
