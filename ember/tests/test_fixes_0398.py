"""0.39.8: the fixes of the diagnostics report of 2026-10-16 (analysis-0.39.6).

1. usage.speed: an answer served at the standard speed is charged what it cost, and the calls charged at their worst
   case for that field alone are settled at startup (3.1).
2. A listing made by another line's request moves to the line it is (project_update's listing_id), and a line with no
   listing of its own gets no weekly pin or post (3.3).
3. make_document says what '::: lines fill' drew, and a request the owner rejected, proposed again, is urgent for a
   few days and gets a second cycle a day (3.2).
4. A channel worth less than twice its floor cuts no sleep and its recurring steps wait unless the owner pins one; a
   recurring step is skipped for its period outside the day's waits (3.4).
5. The owner's yes to a spend on Ember's listings is a paid test, shown in STATUS while it runs and after it ends (3.5).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import paid, plan, reach, store, tools, ventures, weights  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.agent.tools import Outcome  # noqa: E402
from app.config import Settings  # noqa: E402
from app.economy import metering  # noqa: E402
from app.economy.metering import Completed  # noqa: E402
from app.economy.service import Economy  # noqa: E402
from app.products import markup, pdf  # noqa: E402
from tests.economy_helpers import (  # noqa: E402
    FakeClock,
    ScriptedTransport,
    make_economy,
    message,
    owner,
    request,
    restart,
)
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call  # noqa: E402
from tests.test_fixes_0280 import lined, now, working  # noqa: E402
from tests.test_fixes_0340 import ALL, TRACKER, keep, project  # noqa: E402
from tests.test_fixes_0340 import BOOK as BOOK_TITLE  # noqa: E402
from tests.test_fixes_0350 import plan_step, steered  # noqa: E402
from tests.test_fixes_0351 import promise  # noqa: E402
from tests.test_fixes_0353 import critic, launched  # noqa: E402
from tests.test_fixes_0376 import asked, candidate, closed_without_proposing, cycles, decided  # noqa: E402
from tests.test_fixes_0376 import waits as waits_on  # noqa: E402
from tests.test_fixes_0393 import first_marketing_out, waits, weekly  # noqa: E402
from tests.test_fixes_0396 import BOOK, steer, week  # noqa: E402

KNOWN = 1_000 * 2 + 200 * 10  # what each call below costs at the standard price: 1,000 in and 200 out on Sonnet 5
LIVE = Settings(dry_run=False, starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5)


# --- 1. usage.speed ---


def live_economy(data_dir: Path) -> Economy:
    return make_economy(data_dir, LIVE, clock=FakeClock())


def speed_calls(economy: Economy, count: int, speed: str = "standard") -> list[metering.CallResult]:
    outcomes = [Completed(message(1_000, 200, speed=speed)) for _ in range(count)]
    model = economy.metered(ScriptedTransport(simulated=False, outcomes=outcomes))
    cycle = model.open_cycle("test")
    results = [model.call(cycle, "work", request()) for _ in range(count)]
    model.close_cycle(cycle)
    return results


def test_an_answer_at_the_standard_speed_is_charged_what_it_cost(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    [result] = speed_calls(economy, 1)
    assert not result.billing_uncertain
    assert result.cost_micros == KNOWN  # the price table's standard price
    with economy.db.connection() as conn:
        assert conn.execute("SELECT error FROM llm_calls").fetchone()[0] is None


def test_an_answer_at_another_speed_stays_uncertain(data_dir: Path) -> None:
    economy = live_economy(data_dir)
    [result] = speed_calls(economy, 1, speed="fast")
    assert result.billing_uncertain and result.cost_micros == result.estimate_micros
    with economy.db.connection() as conn:
        assert conn.execute("SELECT error FROM llm_calls").fetchone()[0] == "speed fast"


def booked_as_before(economy: Economy, monkeypatch: pytest.MonkeyPatch, count: int) -> list[metering.CallResult]:
    """Calls answered with usage.speed "standard" as 0.39.7 charged them: at their worst case."""
    with monkeypatch.context() as patch:
        patch.setattr(metering, "_KNOWN_USAGE_KEYS", metering._KNOWN_USAGE_KEYS - {"speed"})  # noqa: SLF001
        results = speed_calls(economy, count)
    assert all(r.billing_uncertain and r.cost_micros == r.estimate_micros for r in results)
    return results


def refunds(economy: Economy) -> list[tuple[str, int, str]]:
    with economy.db.connection() as conn:
        return [
            (r["occurred_on"], r["amount_micros"], r["created_by"])
            for r in conn.execute(
                "SELECT occurred_on, amount_micros, created_by FROM ledger WHERE type = 'api_cost_correction'"
                " ORDER BY id"
            )
        ]


def test_calls_charged_at_their_worst_case_for_the_speed_alone_are_settled_at_startup(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    economy = live_economy(data_dir)
    first = booked_as_before(economy, monkeypatch, 2)
    economy.clock.advance(days=1)
    second = booked_as_before(economy, monkeypatch, 1)
    scope = economy.life.scope()
    excess = sum(r.cost_micros - KNOWN for r in first + second)
    assert economy.books.provisional_excess(scope) == excess
    cap_before = economy.books.cap_spend_on(scope, economy.clock.today())
    balance_before = economy.books.balance(scope)

    fresh = restart(economy)
    scope = fresh.life.scope()
    days = sorted({row[0] for row in refunds(fresh)})
    assert len(days) == 2  # one refund for each day the calls were made on
    assert sum(amount for _, amount, _ in refunds(fresh)) == -excess
    assert all(who == "system" for _, _, who in refunds(fresh))
    assert fresh.books.provisional_excess(scope) == 0
    assert fresh.books.balance(scope) == balance_before + excess
    assert fresh.books.cap_spend_on(scope, fresh.clock.today()) == cap_before  # the cap counted what they cost
    assert fresh.status().settled_balance == fresh.status().balance  # the System warning goes away
    assert any("settled at what they cost" in e["message"] for e in fresh.db.recent_events(limit=5))
    assert metering.settle_speed(fresh.db, fresh.clock) == 0  # once
    restart(fresh)
    assert len(refunds(fresh)) == 2


def test_the_owners_own_refund_since_then_counts_first(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    economy = live_economy(data_dir)
    results = booked_as_before(economy, monkeypatch, 3)
    excess = sum(r.cost_micros - KNOWN for r in results)
    owner(economy, "api-correction", "0.01", direction="decrease")  # the System warning asked for it
    balance = economy.books.balance(economy.life.scope())
    fresh = restart(economy)
    system = [amount for _, amount, who in refunds(fresh) if who == "system"]
    assert sum(system) == -(excess - 10_000)
    assert fresh.books.balance(fresh.life.scope()) == balance + excess - 10_000
    assert fresh.books.provisional_excess(fresh.life.scope()) == 0


def test_a_refund_larger_than_the_excess_leaves_nothing_to_settle(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    economy = live_economy(data_dir)
    [result] = booked_as_before(economy, monkeypatch, 1)
    assert result.cost_micros - KNOWN <= 10_000 <= result.cost_micros
    owner(economy, "api-correction", "0.01", direction="decrease")
    fresh = restart(economy)
    assert [who for _, _, who in refunds(fresh)] == ["owner"]
    assert metering.settle_speed(fresh.db, fresh.clock) == 0


# --- 2. a listing moves to the line it is ---


def move(agent: Agent, line: int, listing: int, note: str = "The bundle is this line's only listing") -> Outcome:
    return call(working(agent, line), "project_update", {"project_id": line, "listing_id": listing, "note": note})


def test_a_listing_made_by_another_lines_request_moves_to_its_line(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    other = project(agent, "Commercial licence pack for coaches")
    keep(agent)
    ctx = working(agent, other)
    with agent.db.transaction() as conn, pytest.raises(tools.ToolError) as refused:
        tools._listing_line(ctx, conn, listing, "post")  # noqa: SLF001
    assert f"no post for project #{line}, whose work waits for a cycle of its own" in str(refused.value)
    assert f"Listing #{listing} counts for line #{line}, the line of the request that made it" in str(refused.value)
    assert f"project_update with project_id {other}, listing_id {listing}" in str(refused.value)

    moved = move(agent, other, listing)
    assert moved.ok, moved.text
    assert f"Listing #{listing} counts for line #{other} from now on (it counted for line #{line})" in moved.text
    with agent.db.transaction() as conn:
        assert ventures.listing_project(conn, agent.scope(), listing) == other
        funnels = reach.funnels(conn, agent.scope())
        tools._listing_line(working(agent, other), conn, listing, "post")  # noqa: SLF001 - its line's now
    assert funnels[other].listings == [listing] and line not in funnels
    assert rows(agent, "SELECT listing_id, project_id, moved_from FROM listing_lines") == [
        {"listing_id": listing, "project_id": other, "moved_from": line}
    ]
    assert any(f"moved listing #{listing} from line #{line}" in e["message"] for e in agent.db.recent_events(10))


def test_a_listing_moves_only_with_why_only_ones_own_and_not_out_of_a_line_the_owner_holds(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    other = project(agent, "Commercial licence pack for coaches")
    keep(agent)
    assert "say in note why" in move(agent, other, listing, note="").text
    assert "isn't one of yours" in move(agent, other, 123_456).text
    assert "counts for line" in move(agent, line, listing).text and "already" in move(agent, line, listing).text
    [product] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    with agent.db.transaction() as conn:
        plan.owner_hold(conn, agent.scope(), product["id"], "I look at it next week", "Owner", now(agent))
    held = move(agent, other, listing)
    assert not held.ok and f"your owner holds line #{line}" in held.text
    assert not rows(agent, "SELECT id FROM listing_lines")


def test_a_line_without_a_listing_of_its_own_gets_no_weekly_pin_or_post(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    first_marketing_out(agent, line, ("pin", "post", "blog"))
    critic(agent, line, listing, 8)
    agent.clock.advance(days=1)
    keep(agent)
    week = weekly(agent, line)
    assert week and all(waiting is None for waiting in waits(agent, line, ALL).values())
    other = project(agent, "Commercial licence pack for coaches")
    keep(agent)
    assert move(agent, other, listing).ok  # the line has no listing of its own now
    assert {waits(agent, line, ALL)[s["title"]] for s in week} == {"listing"}
    with agent.db.transaction() as conn:
        for step in week:
            plan._close(conn, step["id"], now(agent), "done", "test")  # noqa: SLF001
    agent.clock.advance(days=31)  # past every period
    keep(agent)
    assert not [s for s in weekly(agent, line) if s["status"] == "open"]


# --- 3. the KDP book: what fill drew, and what the owner rejected comes first ---


def test_make_document_says_what_lines_fill_drew_and_that_it_reaches_the_bottom_margin(data_dir: Path) -> None:
    """Live, the agent read 2 lines under a week's table, ruled down to the bottom margin, as 'fill stops short'."""
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    source = BOOK + week(14, "::: lines fill") + "\n::: pagebreak\n\n" + week(10, "::: lines fill")
    ctx.workspace.write("books/weeks.md", source)
    made = call(ctx, "make_document", {"source": "books/weeks.md", "output": "books/weeks.pdf"})
    assert made.ok, made.text
    _, report = pdf.render(markup.parse(source))
    [(page_one, few), (page_two, many)] = report.fills
    assert (page_one, page_two) == (1, 2) and few == markup.FILL_LEAST and many > few
    assert (
        f"'::: lines fill' drew {few} lines on page 1; {many} lines on page 2: each is ruled down to the bottom margin"
        in made.text
    )
    ctx.workspace.write("books/counted.md", BOOK + week(14, "::: lines 2"))
    counted = call(ctx, "make_document", {"source": "books/counted.md", "output": "books/counted.pdf"})
    assert counted.ok and "'::: lines fill' drew" not in counted.text


def test_the_book_the_owner_rejected_comes_before_a_promise_and_gets_a_second_cycle_a_day(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK_TITLE)
    tracker = project(agent, *TRACKER)
    keep(agent)
    package = asked(agent, book, "kdp_package")
    keep(agent)  # its stages up to the release
    decided(agent, package, "rejected")  # "finally looking good. last small change is to extend the lines…"
    keep(agent)
    closed_without_proposing(agent)  # a cycle took the decision in
    promise(agent, "Resubmit the Bluesky post once the mapping is fixed", days=4, line=tracker)
    keep(agent)
    again = f"Propose the book again: request #{package} rejected"
    s = steered(agent)
    found = candidate(s, again, book)
    assert found.waiting is None and found.step.urgency == weights.REWORK
    assert s.step is not None and s.step.title == again  # before the promise (live: 2.6 below 6.0)
    taken = cycles(agent, 3)  # each takes it, and nothing moves for it: two tries today, then the brake
    assert [t.title for t in taken[:2]] == [again, again]
    assert taken[2].title != again and waits_on(agent, again, book) == "tried"
    agent.clock.advance(days=4)  # 0.40.1 (analysis-0.40.0, 3.3): days after the decision, until it is proposed again
    keep(agent)
    assert candidate(steered(agent), again, book).step.urgency == weights.REWORK


def test_a_request_that_expired_gets_no_rework_urgency(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    book = project(agent, *BOOK_TITLE)
    keep(agent)
    package = asked(agent, book, "kdp_package")
    keep(agent)
    decided(agent, package, "expired")
    keep(agent)
    closed_without_proposing(agent)
    keep(agent)
    assert candidate(steered(agent), "Propose the book again", book).step.urgency == 0.0


# --- 4. a channel at its floor takes no more cycles ---


FLOORED = {"pinterest": 0.5, "bluesky": 0.21, "blog": 0.2}  # live on 10-16


def weekly_out(data_dir: Path) -> tuple[Agent, int]:
    """A live product line with this period's weekly marketing laid out, one step for each channel its buyers read."""
    agent, line, listing = launched(data_dir)
    first_marketing_out(agent, line, ("pin", "post", "blog"))
    critic(agent, line, listing, 8)
    agent.clock.advance(days=1)
    keep(agent)
    assert weekly(agent, line)
    return agent, line


def by_channel(agent: Agent, line: int) -> dict[str, dict[str, Any]]:
    return {s["template"].removeprefix("recurring/"): s for s in weekly(agent, line) if s["status"] == "open"}


def test_a_step_of_a_channel_just_above_its_floor_leaves_the_agent_its_own_sleep(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    bluesky = weights.Step(901, 1, "This week's Bluesky post", "market", 2.0, channel=0.21)
    pins = weights.Step(902, 1, "This week's pin", "market", 2.0, channel=0.5)
    with agent.db.connection() as conn:
        assert not plan.busy(conn, agent.scope(), steer(bluesky), now(agent))  # 0.39.6 counted it: 0.21 > 0.20
        assert plan.busy(conn, agent.scope(), steer(bluesky, pins), now(agent))


def test_a_weekly_step_of_a_channel_at_its_floor_waits_unless_the_owner_pins_it(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, line = weekly_out(data_dir)
    monkeypatch.setattr(plan, "_channel_factors", lambda conn, scope, now: dict(FLOORED))
    steps = by_channel(agent, line)
    waiting = waits(agent, line, ALL)
    assert waiting[steps["pinterest"]["title"]] is None
    assert {waiting[steps[c]["title"]] for c in ("bluesky", "blog") if c in steps} == {"floor"}
    post = steps.get("bluesky") or steps["blog"]
    with agent.db.transaction() as conn:
        plan.pin(conn, agent.scope(), post["id"], True, "Owner", now(agent))
    assert waits(agent, line, ALL)[post["title"]] is None  # the owner wants it: it is taken first
    assert steered(agent).step.id == post["id"]


def test_a_recurring_step_is_skipped_for_its_period_outside_the_days_waits(data_dir: Path) -> None:
    agent, line = weekly_out(data_dir)
    steps = list(by_channel(agent, line).values())
    assert len(steps) >= 2
    first, other = steps[0], steps[1]
    skipped = plan_step(agent, line, action="skip", step_id=first["id"], why="the blog has no readers")
    assert skipped.ok, skipped.text
    assert f"Step #{first['id']} is skipped for this period (it ends {first['due']})" in skipped.text
    assert rows(agent, "SELECT actor, action FROM plan_changes WHERE action = 'drop'") == [
        {"actor": "agent", "action": "drop"}
    ]
    until = (agent.clock.today() + timedelta(days=3)).isoformat()
    waited = plan_step(agent, line, action="wait", step_id=other["id"], on="date", until=until)
    assert waited.ok and "(1 of 2 waits today)" in waited.text  # the skip was no wait
    keep(agent)
    assert first["template"] not in {s["template"] for s in weekly(agent, line) if s["status"] == "open"}
    agent.clock.advance(days=8)  # a week's period ended: the channel's next step for the line comes
    keep(agent)
    assert first["template"] in {s["template"] for s in weekly(agent, line) if s["status"] == "open"}


def test_only_a_recurring_step_of_ember_s_code_is_skipped_and_never_a_pinned_one(data_dir: Path) -> None:
    agent, line = weekly_out(data_dir)
    post = next(iter(by_channel(agent, line).values()))
    with agent.db.transaction() as conn:
        plan.pin(conn, agent.scope(), post["id"], True, "Owner", now(agent))
    pinned = plan_step(agent, line, action="skip", step_id=post["id"])
    assert not pinned.ok and "your owner pinned" in pinned.text
    added = plan_step(
        agent, line, action="add", stage="maintain", steps=[{"title": "Ask for a review", "kind": "ship"}]
    )
    assert added.ok, added.text
    [own] = rows(agent, f"SELECT id FROM plan_nodes WHERE project_id = {line} AND title = 'Ask for a review'")
    refused = plan_step(agent, line, action="skip", step_id=own["id"])
    assert not refused.ok and "skip is for a recurring pin, post or blog post" in refused.text


# --- 5. the owner's paid test is tracked ---


def ads(agent: Agent, listing: int, status: str = "approved_with_changes", named: bool = True) -> int:
    """The owner's yes to a spend request naming ``listing`` (or none), as #128 was approved: "done starting from
    now"."""
    stamp = now(agent)
    words = f"#{listing} - Coach bundle (4 views)" if named else "the shop's best listings"
    with agent.db.transaction() as conn:
        found = store.insert_approval(
            conn,
            agent.scope(),
            1,
            stamp,
            type="spend_money",
            title="Enable Etsy Ads: €1/day for 7 days on top 3 listings",
            description=f"Please turn on Etsy Ads for these listings only, at a €1/day budget: {words}",
            payload="Etsy Ads, €1/day budget, 7 days, ~€7 total.",
            expected_cost="€7",
            expected_benefit="views",
        )
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decision_comment = 'done starting from now' WHERE id = ?",
            (status, stamp, found),
        )
    return found


def keep_paid(agent: Agent) -> list[str]:
    with agent.db.transaction() as conn:
        return paid.keep(conn, agent.scope(), agent.clock)


def test_a_paid_tests_days_come_from_its_words() -> None:
    assert paid.days_named("Enable Etsy Ads: €1/day for 7 days on top 3 listings") == 7
    assert paid.days_named("a 14-day test at the minimum budget") == 14
    assert paid.days_named("Etsy Ads 10 Tage lang") == 10
    assert paid.days_named("Etsy Ads on the coach bundle") == paid.DEFAULT_DAYS


def test_the_owners_yes_to_a_spend_on_embers_listings_is_a_test_shown_in_status_until_it_is_judged(
    data_dir: Path,
) -> None:
    agent, _, listing = launched(data_dir)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = 4 WHERE listing_id = ?", (listing,))
    request_id = ads(agent, listing)
    said = keep_paid(agent)
    start, ends = agent.clock.today(), agent.clock.today() + timedelta(days=7)
    assert said == [f"Request #{request_id} is a paid test of #{listing} from {start:%m-%d} to {ends:%m-%d}."]
    assert keep_paid(agent) == []  # once
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = 9 WHERE listing_id = ?", (listing,))
    running = "+5 views (4 to 9) and +0 favorites (0 to 0) on its 1 listing so far"
    with agent.db.connection() as conn:
        assert running in paid.status_text(conn, agent.scope(), agent.clock.today())
    assert f"Paid test (your owner's spend, request #{request_id})" in agent.planner_preview()  # in STATUS
    agent.clock.advance(days=8)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = 20 WHERE listing_id = ?", (listing,))
    [ended] = keep_paid(agent)
    gained = "+16 views (4 to 20) and +0 favorites (0 to 0) on its 1 listing"
    assert ended == f"The paid test of request #{request_id} ended: {gained}."
    with agent.db.connection() as conn:
        shown = paid.status_text(conn, agent.scope(), agent.clock.today())
    assert f"ended {ends:%m-%d}" in shown and "Judge it now" in shown and "in 7 days" in shown
    agent.clock.advance(days=paid.SHOWN_DAYS + 1)
    with agent.db.connection() as conn:
        assert paid.status_text(conn, agent.scope(), agent.clock.today()) == ""


def test_a_spend_that_names_no_listing_of_embers_is_no_paid_test(data_dir: Path) -> None:
    agent, _, listing = launched(data_dir)
    ads(agent, listing, named=False)
    with agent.db.transaction() as conn:
        store.insert_approval(
            conn, agent.scope(), 1, now(agent), type="spend_money", title="Ads", description="#123456789 is not ours",
            payload="x", expected_cost="€1", expected_benefit="views",
        )  # fmt: skip
    assert keep_paid(agent) == []
    assert not rows(agent, "SELECT id FROM paid_tests")
