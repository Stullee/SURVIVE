"""0.37.8: the publishing queue, and five things that went wrong around it, as the owner saw them on 10-11.

Live, an approval was carried out at the next round: the owner's batch went out together (two pins of one listing 5
seconds apart on 10-08, a post and two pins within 15 seconds on 10-10, two identical pins 2 seconds apart). The owner:
"there should be some kind of logic in when and how and how often to post". Now approved pins and posts wait in a
queue, each for its slot: in the channel's hours, hours apart, a listing at most every two days, within the daily limit.

And the Plan tab: its steps named no product ("Post it twice on Bluesky" three times read as one step three times), it
took the blog for off while the cycles wrote its posts, a pin proposed "again" as QA asked became a second pin of the
first, a Bluesky post asked through request_approval was never posted, and a German landlord tool got English posts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("httpx2")

from app.agent import news, plan, tools, views  # noqa: E402
from app.config import Settings  # noqa: E402
from app.economy.clock import Clock, from_iso, to_iso  # noqa: E402
from app.integrations import publish_queue  # noqa: E402
from app.integrations.publish_queue import Item, Made, schedule  # noqa: E402
from tests.queue_helpers import next_slot, slot_comes  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_bluesky import a_post, post_context  # noqa: E402
from tests.test_bluesky import listed as bluesky_listed  # noqa: E402
from tests.test_etsy import call, views_approval  # noqa: E402
from tests.test_fixes_0340 import keep, project  # noqa: E402
from tests.test_fixes_0340 import tree as plan_tree  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_pinterest import BOARD, a_pin, listed, picture, pin_context, proposed  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
NIGHT = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)  # 01:40 in Berlin: the owner's batch of approvals, live
COVER_LETTERS, POSTER = ("listing 4584852644",), ("listing 4587046876",)
OTHER = {"title": "Printable meal plan for busy weeks", "description": "Seven dinners and one list for the fridge."}
THIRD = {"title": "Family dinner planner to print", "description": "A week of family dinners at a glance."}


def at(moment: datetime) -> Clock:
    return Clock(now=lambda: moment, tz=BERLIN)


def local(moment: datetime) -> datetime:
    return moment.astimezone(BERLIN)


# --- the queue: when, how often, what next ---------------------------------------------------------------------------


def test_a_night_s_batch_goes_out_by_day_hours_apart() -> None:
    """The batch of 10-11 01:40: pins #89 and #90 of the cover letters, and a poster's, approved within a minute."""
    batch = [Item(89, COVER_LETTERS, "", NIGHT), Item(90, COVER_LETTERS, "", NIGHT), Item(86, POSTER, "", NIGHT)]
    slots = schedule(batch, [], NIGHT, at(NIGHT), "pinterest", 3)
    assert [s.approval_id for s in slots] == [86, 89, 90]  # the oldest approval first, the listing's second last
    first, second, third = (local(s.at) for s in slots)
    assert first.date() == local(NIGHT).date() and 8 <= first.hour < 9 and slots[0].why == "hours"
    assert slots[1].at - slots[0].at >= publish_queue.GAP and slots[1].why == "gap"
    assert slots[2].at - slots[1].at >= timedelta(days=publish_queue.LINK_DAYS) and slots[2].why == "link"
    assert all(8 <= t.hour < 22 for t in (first, second, third))
    # the same slots at every round until they come (each request's few minutes are its own, every time)
    later = NIGHT + timedelta(hours=5)
    assert schedule(batch, [], later, at(later), "pinterest", 3) == slots


def test_a_slot_holds_until_it_comes_and_then_goes_at_the_next_round() -> None:
    made = [Made(NIGHT - timedelta(hours=12), POSTER)]  # 13:40 in Berlin the day before
    noon = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    [slot] = schedule([Item(91, COVER_LETTERS, "", noon)], made, noon, at(noon), "pinterest", 3)
    assert slot.at == made[0].at + publish_queue.GAP + publish_queue.jitter(91) and slot.why == "gap"
    just_after = slot.at + timedelta(minutes=1)
    [again] = schedule([Item(91, COVER_LETTERS, "", noon)], made, just_after, at(just_after), "pinterest", 3)
    assert again.at == just_after and again.why == "now"  # due: it goes at this round


def test_an_english_post_waits_for_america_s_day_and_a_german_one_doesn_t() -> None:
    morning = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)  # 10:00 in Berlin
    [english] = schedule([Item(1, POSTER, "en", morning)], [], morning, at(morning), "bluesky", 5)
    [german] = schedule([Item(2, POSTER, "de", morning)], [], morning, at(morning), "bluesky", 5)
    assert (local(english.at).hour, english.why) == (14, "hours") and english.language == "en"
    assert (german.at, german.why) == (morning, "now")


def test_the_daily_limit_moves_the_next_to_tomorrow() -> None:
    noon = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    made = [Made(noon - timedelta(hours=4), POSTER)]
    [slot] = schedule([Item(5, COVER_LETTERS, "", noon)], made, noon, at(noon), "pinterest", 1)
    assert local(slot.at).date() == local(noon).date() + timedelta(days=1) and slot.why == "limit"
    assert local(slot.at).hour == 8


def test_a_listing_held_back_doesn_t_hold_the_others() -> None:
    noon = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    made = [Made(noon - timedelta(hours=5), COVER_LETTERS)]
    waiting = [Item(7, COVER_LETTERS, "", noon - timedelta(minutes=2)), Item(8, POSTER, "", noon)]
    slots = schedule(waiting, made, noon, at(noon), "pinterest", 3)
    assert [s.approval_id for s in slots] == [8, 7]  # the poster's now, the cover letters' two days after theirs
    assert slots[0].why == "now" and slots[1].why == "link"


def test_the_rules_and_a_slot_in_words() -> None:
    assert publish_queue.rules("pinterest", 3) == (
        "approved pins go out one at a time, in their slots: from 8:00 to 22:00 your owner's time, at least 3 hours"
        " apart, a listing or page at most once in 2 days, at most 3 a day"
    )
    assert "an English one from 14:00 to 23:00" in publish_queue.rules("bluesky", 5)
    noon = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    clock = at(noon)
    assert publish_queue.when(clock, noon) == "now"
    assert publish_queue.when(clock, noon + timedelta(hours=4)) == "today at 16:00"
    assert publish_queue.when(clock, noon + timedelta(days=1)) == "tomorrow at 12:00"
    assert publish_queue.when(clock, noon + timedelta(days=3)) == "Thu 10-15 at 12:00"
    assert publish_queue.keys(["https://www.etsy.com/de/listing/4584852644/anschreiben?ref=x"]) == COVER_LETTERS
    assert publish_queue.keys(["https://ember-ai.de/blog/a.html", "https://Ember-AI.de/blog/a/"]) == (
        "https://ember-ai.de/blog/a",
    )


# --- the publishers: one at a time, each in its slot -----------------------------------------------------------------


def test_approved_pins_go_out_one_at_a_time_in_their_slots(data_dir: Path) -> None:
    agent, _, first = proposed(data_dir)
    made = a_pin(agent, pin_context(agent), board_name="Meal planning printables", image="shop/pin-2.png", **OTHER)
    assert made.ok, made.text
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    for request in (first, second):
        assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(first, "active")]
    assert agent.execute_approved() == []  # the next never in the same round, nor the next
    shown = views_approval(agent, second)["execution"]
    assert shown["status"] == "scheduled" and shown["why"] == "link"
    assert from_iso(shown["due_at"]) >= agent.clock.now() + timedelta(days=publish_queue.LINK_DAYS - 1)
    assert shown["result"] == "a listing or page gets at most one pin in 2 days"
    queue = views.plan_view(agent)["channels"]
    [pins] = [c for c in queue if c["channel"] == "pinterest"]
    assert [(q["id"], q["at"]) for q in pins["queue"]] == [(second, shown["due_at"])]
    slot_comes(agent, "pinterest")
    assert agent.execute_approved() == [(second, "active")]
    assert views.plan_view(agent)["channels"][0]["queue"] == []


def test_the_owner_can_cancel_a_pin_in_the_queue(data_dir: Path) -> None:
    agent, _, first = proposed(data_dir)
    assert a_pin(agent, pin_context(agent), board_name="Meal planning printables", image="shop/pin-2.png", **OTHER).ok
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    for request in (first, second):
        owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(first, "active")]
    assert views_approval(agent, second)["execution"]["status"] == "scheduled"
    assert owner(agent).close(second, {"outcome": "failed"}, "Owner").status == 200
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {second}")[0]["status"] == "failed"
    agent.clock.advance(days=3)
    assert agent.execute_approved() == []


def test_the_agent_hears_when_an_approved_pin_goes_out(data_dir: Path) -> None:
    agent, _, first = proposed(data_dir)
    assert a_pin(agent, pin_context(agent), board_name="Meal planning printables", image="shop/pin-2.png", **OTHER).ok
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    for request in (first, second):
        owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(first, "active")]
    per_day = {"pinterest": 3, "bluesky": 2}
    with agent.db.connection() as conn:
        fresh = news.collect(conn, agent.db, agent.scope(), "0.37.8")
        fresh.slots = publish_queue.slot_words(conn, agent.scope(), agent.clock, per_day)
        queue = publish_queue.text(conn, agent.scope(), agent.clock, "pinterest", 3)
    slot = next_slot(agent, "pinterest")
    said = publish_queue.when(agent.clock, slot.at)
    [line] = [x for x in fresh.approval_lines() if x.startswith(f"Request #{second} ")]
    assert f"Ember's code carries it out in its slot, {said} (a listing or page gets at most one pin in" in line
    assert queue.startswith("Queue (approved pins go out one at a time, in their slots:")
    assert f"\n- request #{second} Pin: Printable meal plan for busy weeks: {said} (a listing or page" in queue


def test_a_post_that_fails_its_checks_takes_no_slot(data_dir: Path) -> None:
    """Nothing reached Bluesky, so the next post needn't wait hours for it (two posts fail in one round)."""
    agent, _ = bluesky_listed(data_dir)
    ctx = post_context(agent)
    assert a_post(agent, ctx, text="Ein Wochenplaner für die ganze Familie. #Wochenplaner").ok
    assert a_post(agent, ctx, text="Plane die Woche an einem Sonntagabend. #Printable", link=None).ok
    first, second = (r["id"] for r in rows(agent, "SELECT id FROM approvals WHERE executor = 'bluesky_post'"))
    for request in (first, second):
        owner(agent).decide(request, {"decision": "approve"}, "Owner")
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET state = 'inactive'")
    assert agent.execute_approved() == [(first, "failed"), (second, "active")]


# --- how: no pin twice, no post through the wrong tool, no queue too long --------------------------------------------


def test_a_pin_in_the_same_words_replaces_the_waiting_one_and_can_t_repeat_a_live_one(data_dir: Path) -> None:
    agent, _, first = proposed(data_dir)
    ctx = pin_context(agent)
    again = a_pin(agent, ctx, board_name="Meal planning printables", image="shop/pin-2.png")  # a better picture
    assert again.ok and f"It replaces request #{first} (withdrawn)" in again.text
    second = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    status = rows(agent, f"SELECT status, decision_comment FROM approvals WHERE id = {first}")[0]
    assert status == {"status": "withdrawn", "decision_comment": tools.REPLACED_PIN}
    owner(agent).decide(second, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(second, "active")]
    for words, what in (({}, "title"), ({"title": "Another title"}, "description")):
        repeated = a_pin(agent, ctx, board_id=BOARD, image="shop/pin-3.png", **words)
        assert not repeated.ok, repeated.text
        assert (
            f"it repeats the {what} of request #{second}'s pin of this listing, which is on Pinterest" in repeated.text
        )
    same_picture = a_pin(agent, ctx, board_id=BOARD, image="shop/pin-2.png", **OTHER)
    assert not same_picture.ok and "it repeats the picture of" in same_picture.text
    assert a_pin(agent, ctx, board_id=BOARD, image="shop/pin-3.png", **OTHER).ok  # new words, a new picture


def test_the_qa_note_says_the_new_pin_replaces_the_first(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    agent.roots()[0].write_bytes("shop/square.png", picture(1000, 1000))
    made = a_pin(agent, pin_context(agent), board_name="Meal planning printables", image="shop/square.png")
    request = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    assert made.ok and "propose it again." not in made.text
    assert (
        f"make one with make_image (shape pin) and propose the pin again, in the same words: it replaces #{request}."
        in (made.text)
    )


def test_a_channel_takes_no_more_while_three_wait_for_their_slots(data_dir: Path) -> None:
    agent, _, first = proposed(data_dir)
    ctx = pin_context(agent)
    assert a_pin(agent, ctx, board_name="Meal planning printables", image="shop/pin-2.png", **OTHER).ok
    ctx.state = tools.CycleTools()  # two pins a cycle
    assert a_pin(agent, ctx, board_name="Meal planning printables", image="shop/pin-3.png", **THIRD).ok
    waiting = [r["id"] for r in rows(agent, "SELECT id FROM approvals WHERE executor = 'pinterest_pin'")]
    for request in waiting:
        owner(agent).decide(request, {"decision": "approve"}, "Owner")
    more = {"title": "Meal prep checklist", "description": "Shop once, cook three times."}
    ctx.state = tools.CycleTools()
    refused = a_pin(agent, ctx, board_name="Meal planning printables", image="shop/pin-4.png", **more)
    # 0.39.6: approved pins of one listing count once toward the 3; another of that listing waits for them all
    assert (
        not refused.ok
        and f"your approved pin (request #{waiting[0]}) for that listing or page waits for its slot" in (refused.text)
    )
    assert agent.execute_approved() == [(first, "active")]  # one out: the next of that listing still waits
    again = a_pin(agent, ctx, board_id=BOARD, image="shop/pin-4.png", **more)
    assert not again.ok and f"(request #{waiting[1]})" in again.text


def test_a_proposal_says_when_it_would_go_out(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    made = a_pin(agent, pin_context(agent), board_name="Meal planning printables")
    assert made.ok and "in its slot: approved pins go out one at a time" in made.text
    assert "approved now, it would go out at once" in made.text  # nothing went out yet: noon is in its hours


def test_request_approval_sends_a_post_or_a_pin_to_its_own_tool(data_dir: Path) -> None:
    agent, _ = bluesky_listed(data_dir)
    ctx = post_context(agent)
    asked = {
        "type": "publish",
        "title": "Bluesky post 1/2: cover letter opening-line tip -> Anschreiben/Cover Letter listing",
        "description": "Step #27 (project #4): post 1 of 2 Bluesky posts linking the listing directly.",
        "payload": 'Text: "Cover letter tip: open with the role." Language: en Link: https://www.etsy.com/listing/1',
        "expected_cost": "$0",
        "expected_benefit": "views",
    }
    refused = call(ctx, "request_approval", asked)
    assert not refused.ok and "a Bluesky post goes through propose_bluesky_post (a marketing cycle's tool)" in (
        refused.text
    )
    assert "Ember's code posts it in its slot once your owner approves it, and your plan counts it" in refused.text
    pin = {**asked, "title": "Pin: Anschreiben Vorlage", "payload": "Board: Vorlagen. Link: https://www.etsy.com/x"}
    assert call(ctx, "request_approval", pin).ok  # no Pinterest account here: only the owner's hand can pin it
    bio = {**asked, "title": "Bluesky bio: add the shop's link", "payload": "Bio: Shop: etsy.com/shop/x"}
    assert call(ctx, "request_approval", bio).ok  # not a post: the owner changes the bio by hand
    ctx.bluesky = None
    assert call(ctx, "request_approval", {**asked, "payload": asked["payload"] + " (2)"}).ok


# --- the plan: the blog's switch, the products' names and audiences --------------------------------------------------


def test_the_blog_is_a_channel_by_its_own_switch() -> None:
    blog_only = Settings(blog_enabled=True, site_enabled=False)
    assert plan.channels_from(blog_only)["blog"] is True  # as the cycles take it (loop.blog_on)
    assert plan.channels_from(Settings(blog_enabled=False, site_enabled=True))["blog"] is False


def test_a_german_market_names_german_buyers() -> None:
    def audience(title: str, hypothesis: str) -> str:
        return plan.infer_audience({"title": title, "hypothesis": hypothesis})

    landlords = (
        "Small private landlords in Germany pay $5-8 for a ready-made Excel tool to create a legally-structured"
        " Nebenkostenabrechnung (utility cost statement) for tenants, with auto-calculated Umlageschlüssel."
    )
    assert audience("Nebenkostenabrechnung Excel Template (DE landlord utility bill tool)", landlords) == "de"
    assert audience("Cover letter templates EN+DE", "Bundle EN cover letter + DE Anschreiben") == "both"
    assert audience("Budget Planner", "a bilingual (EN/DE) monthly budget") == "both"
    assert audience("Etsy digital templates", "EN resume template sells to English-speaking job seekers") == "en"


def test_a_product_whose_buyers_read_german_drops_its_english_posts(data_dir: Path) -> None:
    from tests.test_fixes_0280 import lined  # noqa: PLC0415

    agent, _ = lined(data_dir, titles=())
    words = "Landlords pay 6 EUR on Etsy for an Excel download with the costs, für Vermieter"
    line = project(agent, "Nebenkosten Excel template for Etsy", words)
    keep(agent)
    product = next(n for n in plan_tree(agent) if n["level"] == "product" and n["project_id"] == line)
    assert product["audience"] == "both"
    posts = [n for n in plan_tree(agent) if n["project_id"] == line and n["title"] == plan_title("posts")]
    assert [n["status"] for n in posts] == ["open"]
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE projects SET hypothesis = ? WHERE id = ?",
            ("Landlords in Germany pay 6 EUR on Etsy for an Excel download", line),
        )
    said = keep(agent)
    assert any(
        f"line #{line}'s buyers read German; its steps #{posts[0]['id']} for other readers dropped" in s for s in said
    )
    nodes = {n["id"]: n for n in plan_tree(agent)}
    assert nodes[posts[0]["id"]]["status"] == "dropped" and nodes[product["id"]]["audience"] == "de"
    blog = [n for n in nodes.values() if n["project_id"] == line and n["title"] == plan_title("blog")]
    assert [n["status"] for n in blog] == ["open"]  # the German blog post stays
    changes = rows(agent, f"SELECT actor, action, why FROM plan_changes WHERE node_id = {product['id']}")
    why = "Ember's code: its buyers read German (it was English and German)"
    assert changes[-1] == {"actor": "code", "action": "replace", "why": why}
    assert not any("buyers read" in line for line in keep(agent))  # once


def plan_title(key: str) -> str:
    from app.agent import templates  # noqa: PLC0415

    return {"posts": templates._POSTS.title, "blog": templates._BLOG.title}[key]


def test_the_plan_tab_names_each_step_s_product() -> None:
    script = (Path(__file__).parent.parent / "app" / "web" / "static" / "js" / "app.js").read_text()
    assert "function planLine(line) {" in script and "if (s.line) return planLine(s.line);" in script
    assert '" · line #" + st.line' not in script  # the channels' and upgrades' steps by their product too


def test_the_report_shows_the_queue(ingress_client: Any) -> None:
    from tests.test_diagnostics import report, section  # noqa: PLC0415

    text = section(report(ingress_client), "AGENT RECORDS")
    assert (
        "-- publishing queue (0.37.8: each approved pin and post with its slot; why: now, gap, link, hours or limit)"
        in (text)
    )


def test_slots_are_iso_in_the_plan_view(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.clock.current = agent.clock.current.replace(hour=23)  # after the pins' hours
    [pins] = [c for c in views.plan_view(agent)["channels"] if c["channel"] == "pinterest"]
    [queued] = pins["queue"]
    assert queued["id"] == request and to_iso(from_iso(queued["at"])) == queued["at"]
    assert queued["why"] == "pins go out from 8:00 to 22:00"
