"""0.37.7: bluesky_posts in an ordinary cycle, and each post by its request's number.

Live, the owner asked how posts #43 and #44 did on Bluesky, and Ember answered that it couldn't say: since 0.35.0 a
ready channel's tools and section are a marketing cycle's, and bluesky_posts went with them, so the ordinary cycle that
answered the owner (and any that judged the channel against its stop rule) had none of the posts' numbers, though the
guide and its own plan named the tool. Reading them brings no buyer: bluesky_posts is in every cycle with the account
but a venture cycle, an ordinary cycle's BLUESKY keeps the account's line with its live posts' reactions, and the daily
review hears them. The tool names each post by its request's number (its record key at Bluesky said nothing to anyone),
with the followers, the posts made today, the reactions a post and what Bluesky doesn't count.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import plan as plan_tree  # noqa: E402
from app.agent import tools  # noqa: E402
from app.agent.fake_llm import request_kind  # noqa: E402
from app.economy.clock import from_iso, to_iso  # noqa: E402
from app.integrations import bluesky_publisher  # noqa: E402
from app.integrations.bluesky import FakeBluesky  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_bluesky import a_post, post_context, posted  # noqa: E402
from tests.test_etsy import call  # noqa: E402

EVERY = {"mail": True, "workshop": True, "etsy": True, "venture": False, "library": True, "pinterest": True}
EVERY |= {"bluesky": True, "blog": True, "printify": True, "site": True, "kdp": True}
NUMBERS = "14 likes, 3 reposts, 2 replies, 1 quotes"  # the fake account's, at a sync a week on


def ordinary_next(agent: Any) -> int:
    """The next cycle an ordinary one: the owner's pin of the plan's first open step that needs no channel."""
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        plan_tree.keep(conn, agent.scope(), now, agent.clock.today(), plan_tree.channels_from(agent.settings))
        [step, *_] = plan_tree.nodes(
            conn,
            agent.scope(),
            "level = 'step' AND status = 'open' AND waiting IS NULL AND channel IS NULL"
            " AND kind NOT IN ('promise', 'owner', 'market')",
        )
        plan_tree.pin(conn, agent.scope(), int(step["id"]), True, "Owner", now)
    return int(step["id"])


def test_reading_the_posts_brings_no_buyer() -> None:
    assert "bluesky_posts" in tools.CHANNEL_READS and not tools.CHANNEL_READS & tools.MARKETING_TOOLS
    assert "propose_bluesky_post" in tools.MARKETING_TOOLS
    for kind in ({}, {"marketing": True}, {"marketing_apart": True}):
        assert tools.offered("bluesky_posts", **kind, **EVERY), kind
    assert not tools.offered("propose_bluesky_post", marketing_apart=True, **EVERY)
    assert not tools.offered("bluesky_posts", **{**EVERY, "venture": True})  # a venture cycle decides a venture
    assert not tools.offered("bluesky_posts", **{**EVERY, "bluesky": False})  # nor without the account
    names = {d["name"] for d in tools.definitions(marketing_apart=True, **EVERY)}
    assert "bluesky_posts" in names and "propose_bluesky_post" not in names


def test_an_ordinary_cycle_reads_each_post_by_its_request_s_number(data_dir: Path) -> None:
    agent, _, request = posted(data_dir)
    ctx = post_context(agent)
    ctx.marketing_apart = True  # an ordinary cycle while marketing steps have cycles of their own
    shown = call(ctx, "bluesky_posts", {})
    assert shown.ok and shown.text.startswith(
        f"Ember's Bluesky account: @{FakeBluesky.HANDLE} (1 posted today, at most 2 a day); 1 post live with 0"
        " reactions (0.0 a post).\n"
    )
    assert "Numbers not read yet." in shown.text
    refused = a_post(agent, ctx, text="Noch ein Planer #Wochenplaner")
    assert refused.text.startswith("Error: propose_bluesky_post belongs to marketing cycles")  # posting stays theirs
    agent.clock.advance(hours=24 * 7)
    assert agent.bluesky_posts.sync(force=True) is None
    ctx.bluesky = replace(ctx.bluesky, followers=agent.bluesky.followers())  # as the cycle's sync gives them
    shown = call(ctx, "bluesky_posts", {})
    [row] = rows(agent, "SELECT finished_at, synced_at FROM bluesky_posts")
    read = from_iso(row["synced_at"]).astimezone(agent.clock.tz).strftime("%Y-%m-%d %H:%M")
    with agent.db.connection() as conn:
        listed = bluesky_publisher.text(conn, agent.scope(), 12)
    assert shown.text == (
        f"Ember's Bluesky account: @{FakeBluesky.HANDLE} (3 followers; 0 posted today, at most 2 a day); 1 post live"
        " with 20 reactions (20.0 a post).\n"
        f"{listed}\n"
        f"Numbers as Ember's code read them at {read}. Bluesky counts no views or clicks: the listings a post links"
        " show their views (etsy_listing)."
    )
    assert f"\n- request #{request}, {row['finished_at'][:10]} (active): " in shown.text and NUMBERS in shown.text


def test_a_repeated_post_names_the_live_one_by_its_request(data_dir: Path) -> None:
    agent, _, request = posted(data_dir)
    again = a_post(agent, post_context(agent))
    assert not again.ok and f"it says what your post of request #{request} on Bluesky" in again.text


def test_an_ordinary_cycle_s_plan_and_the_review_keep_bluesky_s_numbers(data_dir: Path) -> None:
    agent, fake, request = posted(data_dir)
    agent.clock.advance(hours=24 * 7)
    ordinary_next(agent)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    [cycle] = rows(agent, "SELECT marketing, venture FROM cycles ORDER BY id DESC LIMIT 1")
    assert (cycle["marketing"], cycle["venture"]) == (0, 0)
    sent = list(fake.sent)[before:]
    plan = next(r for r in sent if request_kind(r) == "plan")
    text = plan["messages"][0]["content"][0]["text"]
    assert (
        f"\n== BLUESKY ==\nEmber's account: @{FakeBluesky.HANDLE} (3 followers; at most 2 posts a day).\n1 post live"
        " with 20 reactions (20.0 a post). Posting is a marketing cycle's; bluesky_posts reads each post's numbers.\n"
    ) in text
    assert NUMBERS not in text  # each post's numbers are bluesky_posts'
    work = next(r for r in sent if request_kind(r) == "work")
    offered = {t["name"] for t in work["tools"]}
    assert "bluesky_posts" in offered and "propose_bluesky_post" not in offered
    [review] = [r for r in sent if request_kind(r) == "review"]
    card = review["messages"][0]["content"][0]["text"]
    assert "Bluesky: 3 followers, 1 post live with 20 reactions (20.0 a post), at most 2 posts a day" in card


def test_the_summary_counts_live_posts_only(data_dir: Path) -> None:
    agent, _, _ = posted(data_dir)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE bluesky_posts SET likes = 1, reposts = 1, replies = 0, quotes = 0")
        assert bluesky_publisher.summary(conn, agent.scope()) == "1 post live with 2 reactions (2.0 a post)"
        conn.execute("UPDATE bluesky_posts SET status = 'deleted'")
        assert bluesky_publisher.summary(conn, agent.scope()) == "no post live"
