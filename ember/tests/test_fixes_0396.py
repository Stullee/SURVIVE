"""0.39.6: the fixes of the diagnostics report of 2026-10-15 13:06 (analysis-0.39.5: why Ember earns nothing).

1. Every sleep the agent chose (mostly 4 to 6 hours) was cut to 30 minutes while any step was ready, a chore too: 16
   cycles from 00:07 to 07:55 spent $5.59 of the $7 cap. Only work cuts it now: an urgent step, or one that makes or
   ships on a channel above its floor (a venture's step too).
2. The owner rejected the KDP book a sixth time for note lines that left white space under them, and `::: lines N`
   could only draw a count the agent guessed by test renders. `::: lines fill` rules the page down to its bottom
   margin, and a book names a page whose writing lines end with room for two more.
3. The night spent four fifths of the cap, and the owner's decision at 07:48 waited for 20:00. Until 08:00 a scheduled
   cycle leaves half the day's cap for the owner's day.
4. Three daily reviews decided on an Etsy Ads test that no request ever asked for: a line's change is a step now.
5. Bluesky was worth its reactions and the blog 1.0 unmeasured, so a stopped blog's monthly posts ranked second: both
   are worth the listing views that followed their posts.
6. Three approved pins of one listing kept every other listing's pin out of the queue until 10-20: they count once, and
   another pin of that listing waits until they are out.
7. The critic's suggestions for listings few have seen still took cycles as a chore: they wait for views.
8. A rule set for "this week" still held the agent back a week later: each rule shows the day it was set.
9. Ideas on marketplaces with buyers of their own were parked because the Etsy shop got no views: a venture cycle's
   rules say a test there is a test of its own.
"""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from docx import Document

pytest.importorskip("httpx2")

from app.agent import plan, prompts, review, store, tools, weights  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy import metering  # noqa: E402
from app.integrations import publish_queue  # noqa: E402
from app.products import markup, pdf, word  # noqa: E402
from tests.cycle.test_agent import rows  # noqa: E402
from tests.learning.test_weekly_look import judged  # noqa: E402
from tests.plan.test_cycle_line import lined, now  # noqa: E402
from tests.plan.test_marketing_order import CRITIC, PINS, critic, launched  # noqa: E402
from tests.plan.test_plan_tree import ALL, keep  # noqa: E402
from tests.test_fixes_0391 import critic_step, page_texts, seen  # noqa: E402

# --- 1. only work cuts the agent's sleep ---


def works(kind: str, channel: float = 1.0, urgency: float = 0.0, product: int | None = 1) -> bool:
    return plan._works(weights.weigh(weights.Step(1, product, "a step", kind, 2.0, channel=channel, urgency=urgency)))


def test_only_work_cuts_the_agents_sleep() -> None:
    assert not works("chore") and not works("report")  # the critic's quiet suggestions, a views baseline
    assert works("fix") and works("ship") and works("create") and works("launch")
    assert works("create", product=None)  # a venture's step: exploring is work
    assert works("market") and not works("market", channel=weights.CHANNEL_MIN)  # a channel at its floor
    assert works("market", channel=weights.CHANNEL_MIN, urgency=weights.REACH)  # a launch's first marketing
    assert works("report", urgency=weights.RECURRING_DUE)  # a promise to report on its day


def steer(*steps: weights.Step) -> plan.Steer:
    ranked = tuple(weights.rank(steps))
    return plan.Steer(weights.Pick(None, "none", None, ranked), (), "ordinary")


def test_a_plan_with_chores_alone_leaves_the_agent_its_own_sleep(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    chore = weights.Step(901, 1, "The critic passes it", "chore", 2.0)
    report = weights.Step(902, 1, "Track views after the photo goes live", "report", 2.0)
    floor = weights.Step(903, 1, "This week's pin", "market", 2.0, channel=weights.CHANNEL_MIN)
    fix = weights.Step(904, 1, "Propose the book again", "fix", 2.5)
    with agent.db.connection() as conn:
        assert not plan.busy(conn, agent.scope(), steer(chore, report, floor), now(agent))
        assert plan.busy(conn, agent.scope(), steer(chore, report, floor, fix), now(agent))


# --- 2. writing lines down to the bottom margin ---

BOOK = "---\npage: 6x9\nmargin: 16\nsize: 12.5\nline_height: 1.15\ntable: grid\nfooter: Seite {page}\n---\n\n"


def week(rows_: int, notes: str) -> str:
    """A week page of the Haushaltsbuch as v15 set it: a table to fill in, the week's total, then its notes."""
    table = "| Datum | Beschreibung | Kategorie | Betrag |\n|---|---|---|---|\n" + "| | | | |\n" * rows_
    total = "| | Betrag |\n|---|---|\n| Einnahmen | |\n| Ausgaben | |\n| Saldo | |\n"
    return f"## KW1 · 04.01.–10.01.\n\n{table}\n**Bilanz**\n\n{total}\nNotizen:\n{notes}\n"


def lines_drawn(document: markup.Document) -> list[int]:
    return [b.drawn for b in document.main if isinstance(b, markup.Lines)]


def test_lines_fill_rules_the_page_down_to_its_bottom_margin_and_the_word_copy_draws_as_many() -> None:
    fewer = markup.parse(BOOK + week(14, "::: lines fill"))
    data, report = pdf.render(fewer)
    assert len(page_texts(data)) == 1 and report.warnings == []
    more = markup.parse(BOOK + week(10, "::: lines fill"))
    pdf.render(more)
    [few], [many] = lines_drawn(fewer), lines_drawn(more)
    assert few >= markup.FILL_LEAST and many >= few + 4  # four rows fewer: room for about five more lines
    fixed = markup.parse(BOOK + week(10, "::: lines 4"))
    pdf.render(fixed)
    assert lines_drawn(fixed) == [0]  # a count is drawn as it is
    paragraphs = Document(io.BytesIO(word.render(more))).paragraphs  # the Word copy draws the PDF's count
    border = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pBdr"
    assert len([p for p in paragraphs if p._p.pPr is not None and p._p.pPr.find(border) is not None]) == many


def test_lines_fill_belongs_in_the_main_text() -> None:
    for inside in (
        "::: box\n::: lines fill\n:::\n",
        "::: columns\n::: lines fill\n:::\n",
        "::: center\n::: lines fill\n:::\n",
    ):
        with pytest.raises(markup.DocumentError, match="belongs in the main text"):
            markup.parse(inside)
    with pytest.raises(markup.DocumentError, match="'::: lines fill'"):
        markup.parse("::: lines many\n")


def test_a_book_names_writing_lines_that_leave_white_space_and_lines_fill_leaves_none() -> None:
    """Live, request #120: "extend the lines for the notes so there isn't all that white space"."""
    _, short = pdf.render(
        markup.parse(BOOK + week(10, "::: lines 4") + "\n::: pagebreak\n\n" + week(10, "::: lines 4"))
    )
    assert any("the writing lines on pages 1, 2 end with room for 2 or more lines" in w for w in short.warnings)
    assert any("'::: lines fill'" in w for w in short.warnings)
    _, full = pdf.render(
        markup.parse(BOOK + week(10, "::: lines fill") + "\n::: pagebreak\n\n" + week(10, "::: lines fill"))
    )
    assert not any("writing lines on" in w for w in full.warnings)
    _, letter = pdf.render(markup.parse(BOOK.replace("6x9", "A4") + week(10, "::: lines 4")))
    assert not any("writing lines on" in w for w in letter.warnings)  # only a book is checked


def test_lines_fill_that_cant_draw_two_lines_starts_on_the_next_page_and_a_book_says_so() -> None:
    document = markup.parse(BOOK + week(15, "::: lines fill"))
    data, report = pdf.render(document)
    assert len(page_texts(data)) == 2 and "Notizen" in page_texts(data)[1]  # the label goes with its lines
    assert any("runs on past its page from page 1" in w for w in report.warnings)


def test_the_guides_teach_lines_fill() -> None:
    assert "::: lines 8 " in tools.guide_text("documents") and "'fill': down to the bottom margin" in tools.guide_text(
        "documents"
    )
    assert "'::: lines fill'" in tools.guide_text("kdp")


# --- 3. the owner's day keeps half of the cap until 08:00 ---


def at_hour(agent: Agent, hour: int) -> None:
    local = agent.clock.now().astimezone(agent.clock.tz)
    ahead = (hour - local.hour) % 24 or 24
    agent.clock.advance(hours=ahead, minutes=-local.minute)


def test_until_eight_a_scheduled_cycle_leaves_half_the_cap_for_the_owners_day(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = lined(data_dir, titles=())
    settings, clock = agent.settings, agent.clock
    daily = metering.usd_cap_to_micros(settings.daily_spend_cap_usd)
    at_hour(agent, 3)
    assert metering.night(clock)
    assert metering.event_reserve(settings, clock, "schedule") == int(daily * metering.NIGHT_RESERVE_SHARE)
    assert metering.event_reserve(settings, clock, "owner") == 0  # the owner's own wake spends what it needs
    spent = daily // 2 + 100_000  # more than the night's half: the next scheduled cycle waits for the morning
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, day: spent)
    agent._set_time("next_wake_at", clock.now() - timedelta(minutes=1))
    decision = agent.decide()
    assert not decision.run and decision.reason == "Half of the daily cap is kept for your day until 8:00"
    assert decision.wait_until == clock.at(clock.today(), metering.MORNING_HOUR)
    at_hour(agent, 9)  # the morning: a fifth is kept for events until 20:00, the rest is the day's
    assert not metering.night(clock)
    assert metering.event_reserve(settings, clock, "schedule") == int(daily * metering.EVENT_RESERVE_SHARE)
    assert agent.decide().trigger == "schedule"


def test_the_nights_half_is_kept_with_event_wake_ups_off_too(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    settings = agent.settings.model_copy(update={"wake_on_events": False, "wake_on_decision": False})
    daily = metering.usd_cap_to_micros(settings.daily_spend_cap_usd)
    at_hour(agent, 23)
    assert metering.event_reserve(settings, agent.clock, "schedule") == 0
    at_hour(agent, 2)
    assert metering.event_reserve(settings, agent.clock, "schedule") == int(daily * metering.NIGHT_RESERVE_SHARE)


# --- 4. the review's change is a step of its line ---


def test_a_reviews_change_is_a_step_of_its_line_due_today(data_dir: Path) -> None:
    """Live, the reviews of 10-13, 10-14 and 10-15 each decided on a small Etsy Ads test; no request was made."""
    agent, line, _ = launched(data_dir)
    judged(agent, (line, "change", "reach"))
    day = agent.clock.today().isoformat()
    said = keep(agent)
    assert f"Plan tree: line #{line}'s change from today's review is a step of its own." in said
    made = f"SELECT id, kind, status, title, due, check_kind FROM plan_nodes WHERE template = '{plan.REVIEW_CHANGE}'"
    [step] = rows(agent, made)
    assert (step["kind"], step["status"], step["due"], step["check_kind"]) == ("fix", "open", day, "agent")
    assert step["title"] == f"Review {day[5:]}: the numbers say so"
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
        today = review.of_day(conn, agent.scope(), agent.clock.today())
        assert today is not None and "A change is a step of its line's" in review.planner_text(conn, today)
    [candidate] = [c for c in found if c.step.id == step["id"]]
    assert candidate.waiting is None and candidate.step.urgency == weights.RECURRING_DUE
    assert keep(agent) == [] and len(rows(agent, made)) == 1  # once a day
    agent.clock.advance(days=1)
    judged(agent, (line, "change", "reach"))  # the next review decides anew: it takes the open one's place
    keep(agent)
    old, new = rows(agent, made)
    assert (old["status"], new["status"]) == ("dropped", "open")


def test_a_reviews_change_before_the_line_is_live_adds_no_step(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=("Planner",))
    keep(agent)
    judged(agent, (1, "change", "quality"))
    assert not any("change from today's review" in s for s in keep(agent))


# --- 5. Bluesky and the blog by the views that follow their posts ---


def views_on(agent: Agent, listing: int, day: str, value: int) -> None:
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO observations (mode, session, day, observed_at, subject, subject_id, metric, value)"
            " VALUES (?, ?, ?, ?, 'listing', ?, 'views', ?)",
            (scope.mode, scope.session, day, now(agent), listing, value),
        )


def request(agent: Agent, line: int, executor: str, payload: dict[str, Any], action: dict[str, Any]) -> int:
    """A request of line ``line`` for ``executor``, approved, with its payload and action (neither changes later)."""
    with agent.db.transaction() as conn:
        made = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        approval = store.insert_approval(
            conn,
            agent.scope(),
            1,
            now(agent),
            project_id=line,
            type="publish",
            title="A request",
            description="for the owner",
            payload=json.dumps({**payload, "n": made}),
            expected_cost="nothing",
            expected_benefit="buyers",
            executor=executor,
            action=json.dumps({**action, "n": made}),
        )
        conn.execute("UPDATE approvals SET status = 'approved', decided_at = ? WHERE id = ?", (now(agent), approval))
    return approval


def post(agent: Agent, line: int, link: str, likes: int = 9) -> None:
    approval = request(agent, line, "bluesky_post", {}, {})
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bluesky_posts (mode, session, approval_id, text, status, sent, started_at, finished_at,"
            " likes, link) VALUES (?, ?, ?, 'A post', 'active', 1, ?, ?, ?, ?)",
            (scope.mode, scope.session, approval, now(agent), now(agent), likes, link),
        )


def blog_post(agent: Agent, line: int, slug: str, link: str) -> None:
    approval = request(agent, line, "site_post", {"product_url": link}, {})
    scope = agent.scope()
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO blog_posts (mode, session, slug, title, description, day, position, approval_id, seen_at)"
            " VALUES (?, ?, ?, 'A post', 'About it', ?, 1, ?, ?)",
            (scope.mode, scope.session, slug, agent.clock.today().isoformat(), approval, now(agent)),
        )


def factors(agent: Agent) -> dict[str, float]:
    with agent.db.connection() as conn:
        return plan._channel_factors(conn, agent.scope(), now(agent))


def test_bluesky_and_the_blog_are_worth_the_listing_views_that_followed_their_posts(data_dir: Path) -> None:
    """Live, Bluesky was worth 0.76 for its reactions, and the blog 1.0 measured by nothing, while the listings its
    posts linked gained 0 to 2 views."""
    agent, line, listing = launched(data_dir)
    link = f"https://www.etsy.com/listing/{listing}"
    start = agent.clock.today()
    views_on(agent, listing, start.isoformat(), 2)
    for n in range(plan.FRESH_POSTS):
        post(agent, line, link)  # liked, but followed by no view
        blog_post(agent, line, f"post-{n}", link)
    assert (factors(agent)["bluesky"], factors(agent)["blog"]) == (1.0, 1.0)  # too new to judge
    agent.clock.advance(days=plan.RESULT_DAYS)
    views_on(agent, listing, agent.clock.today().isoformat(), 2)
    assert (factors(agent)["bluesky"], factors(agent)["blog"]) == (weights.CHANNEL_MIN, weights.CHANNEL_MIN)
    agent.clock.advance(days=1)
    views_on(agent, listing, agent.clock.today().isoformat(), 2 + 2 * plan.FRESH_POSTS)  # two views a post
    with agent.db.connection() as conn:
        assert plan._views_after(conn, agent.scope(), start.isoformat(), link) == 0  # its week ended before them
        assert plan._views_after(conn, agent.scope(), (start + timedelta(days=1)).isoformat(), link) == 8
        assert plan._views_after(conn, agent.scope(), start.isoformat(), "https://ember-ai.de/blog/") == 0


# --- 6. approved pins of one listing count once in the queue ---


def pin_request(agent: Agent, line: int, listing: int, status: str) -> int:
    link = f"https://www.etsy.com/listing/{listing}"
    image = {"path": "shop/pin.png", "sha256": "0" * 64, "bytes": 1}
    pin = {"board_id": "1", "title": "A pin", "description": "", "link": link, "image": image}
    approval = request(agent, line, "pinterest_pin", {}, pin)
    if status != "approved":
        with agent.db.transaction() as conn:
            conn.execute("UPDATE approvals SET status = ?, decided_at = NULL WHERE id = ?", (status, approval))
    return approval


def test_approved_pins_of_one_listing_count_once_in_the_queue(data_dir: Path) -> None:
    """Live, three approved pins of the Haushaltsbuch listing (slots 10-16, 10-18, 10-20) kept every other listing's
    pin out of the queue until 10-20."""
    agent, line, listing = launched(data_dir)
    same = [pin_request(agent, line, listing, "approved") for _ in range(3)]
    with agent.db.connection() as conn:
        waiting = publish_queue.waiting(conn, agent.scope(), "pinterest")
    assert waiting == same[:1] and len(waiting) < publish_queue.WAITING
    other = pin_request(agent, line, listing + 1, "approved")
    asked = pin_request(agent, line, listing, "pending")  # one waiting for the owner counts whatever it links
    with agent.db.connection() as conn:
        assert publish_queue.waiting(conn, agent.scope(), "pinterest") == [same[0], other, asked]


def test_a_lines_pin_waits_while_its_listing_has_one_waiting_for_its_slot(data_dir: Path) -> None:
    """The tool refuses another pin of a listing whose approved pin waits for its slot: the plan's pin step waits too,
    so no cycle is paid for the refusal."""
    agent, line, listing = launched(data_dir)
    assert pin_step(agent, line).waiting is None
    pin_request(agent, line, listing, "approved")
    assert pin_step(agent, line).waiting == "queue"


def pin_step(agent: Agent, line: int) -> plan.Candidate:
    with agent.db.connection() as conn:
        found = plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), ALL)
    return next(c for c in found if c.step.product == line and c.step.title == PINS)


# --- 7. the critic's suggestions wait for views ---


def test_the_critics_suggestions_wait_for_views_and_a_defect_still_comes_first(data_dir: Path) -> None:
    """Live on 10-15, five cycles ($1.95) made new covers for listings 1 to 3 people had seen."""
    agent, line, listing = launched(data_dir)
    critic(agent, line, listing, 5)
    assert critic_step(agent, line).waiting == "views"
    assert "views" in plan.WAITS
    seen(agent, listing, weights.CRITIC_VIEWS)
    assert critic_step(agent, line).waiting is None  # seen: its suggestions weigh again
    seen(agent, listing, 0)
    critic(agent, line, listing, 3)  # a defect, seen or not
    defect = critic_step(agent, line)
    assert (defect.waiting, defect.step.urgency) == (None, weights.DEFECT)
    assert defect.step.title == CRITIC


# --- 8. each rule says when it was set ---


def test_each_rule_says_the_day_it_was_set() -> None:
    """Live, rule #1 ("This week, in order: ... 3) nothing new. No title or tag edits before 10-20") was set on 10-08
    and still quoted as "the owner's rule this week" on 10-15."""
    rule = {"id": 1, "created_at": "2026-10-07T23:01:00Z", "text": "This week:  nothing new."}
    assert store.rulebook_text([rule]) == "#1 (set 2026-10-07) This week: nothing new."  # type: ignore[list-item]
    today = date(2026, 10, 15)
    dated = store.rulebook_text([rule], today)  # type: ignore[list-item]
    assert dated == "#1 (set 2026-10-07, 8 days ago) This week: nothing new."
    assert store._set_on("2026-10-14T08:00:00Z", today) == "set 2026-10-14, yesterday"
    assert store._set_on("2026-10-15T08:00:00Z", today) == "set 2026-10-15, today"


# --- 9. a marketplace with buyers of its own ---


def test_a_venture_cycle_hears_that_another_marketplace_is_a_test_of_its_own() -> None:
    """Live, triage parked Creative Fabrica, Zazzle and Teachers Pay Teachers untested, because the Etsy shop got no
    views."""
    rules = " ".join(prompts.VENTURE_RULES.split())
    assert "the Etsy shop's views are no reason to park an idea there" in rules
