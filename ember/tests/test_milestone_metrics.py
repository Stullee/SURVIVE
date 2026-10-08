"""0.12.0: milestones measured by a metric, checked by Ember's code. Live, "3 listings live" was closed done with
"Done." while none existed. A milestone can now name a metric from Ember's catalogue and a target; Ember's code reads
it from its records after each Etsy sync and before every plan, with no model call, and closes it: done once met (the
numbers are its evidence), missed once its date has passed (with the numbers, and whether there was too little to
judge). The agent can't close such a milestone done, and the database refuses it too; milestones without a metric stay
allowed. 0.35.0: milestone_plan retired (the plan tree takes the agent's own milestones' place): the owner's and
Ember's code's milestones keep their metrics, and a test sets one as the tool did."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import metrics, roadmap, tools, ventures  # noqa: E402
from tests.economy_helpers import owner as owner_entry  # noqa: E402
from tests.roadmap_helpers import led_to_goal, set_milestone  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed, proposed, shop_context  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402


def call(agent: Any, tool: str, history: bool = False, shop: bool = True, **args: Any) -> tools.Outcome:
    ctx = shop_context(agent)
    if not shop:
        ctx.etsy = None
    elif history:
        ctx.etsy = tools.EtsyAccess("EmberTestShop", "EUR", 3, ctx.etsy.categories, stats_history=True)
    llm_call = rows(agent, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, tool, led_to_goal(ctx, tool, args), f"toolu_{tool}", llm_call, "act")


def day(agent: Any, days: int) -> str:
    return (agent.clock.today() + timedelta(days=days)).isoformat()


def made(outcome: tools.Outcome) -> int:
    assert outcome.ok, outcome.text
    return int(outcome.text.split("#", 1)[1].split(" ", 1)[0])


def milestone(agent: Any, milestone_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM milestones WHERE id = {milestone_id}")[0]


def grade(agent: Any, history: bool = False) -> list[str]:
    return metrics.grade_all(agent.db, agent.scope(), agent.economy.life.scope(), agent.clock, history)


def test_the_catalogue() -> None:
    assert set(metrics.NAMES) == {
        "listings_live",
        "views_delta",
        "favorites_delta",
        "orders_observed",
        "revenue_verified_usd",
        "revenue_month_usd",  # 0.29.0: a goal "a month"
        "research_calls_ok",
        "case_complete",
        "stage_reached",
        "api_spend_usd",
        "inquiries_received",  # 0.13.0 (Phase E1)
        "inquiries_answered",
        "pins_live",  # 0.13.0 (Phase E2)
        "pin_clicks",
        "bluesky_posts_live",  # 0.19.0
        "bluesky_reactions",
        "pod_products_live",  # 0.13.0 (Phase E4)
        "pod_orders",
        "qa_clean",
        "views_total",  # 0.15.0: the agent's goals too
        "favorites_total",
    }
    assert set(metrics.CATALOGUE) - set(metrics.NAMES) == {"orders_total"}
    for m in metrics.CATALOGUE.values():
        assert m.code_only or m.name in metrics.help_text() or m.name.endswith("_delta"), m.name
        assert m.source in metrics.SOURCES and m.kind in ("count", "usd", "yes", "stage")
        assert (m.sample == "") == (m.min_sample == 0)
    listings, usd = metrics.CATALOGUE["listings_live"], metrics.CATALOGUE["revenue_verified_usd"]
    stage = metrics.CATALOGUE["stage_reached"]
    assert metrics.parse_target(listings, " 3 ") == 3
    assert metrics.parse_target(usd, "$12,50") == 12_500_000
    assert metrics.parse_target(stage, "Building") == metrics.STAGES.index("building")
    assert metrics.parse_target(metrics.CATALOGUE["qa_clean"], None) == 1
    for m, bad in ((listings, "2.5"), (listings, "0"), (usd, "0.001"), (usd, "lots"), (stage, "idea")):
        with pytest.raises(metrics.TargetError):
            metrics.parse_target(m, bad)
    assert metrics.target_text(metrics.CATALOGUE["api_spend_usd"], 2_000_000) == "at most $2.00"
    assert metrics.measure_text(listings, 3, 4, None) == (
        "Your listings live on Etsy now (project #4): at least 3 listings (Ember's code checks it)"
    )


def test_a_live_listing_milestone_is_closed_by_code_after_the_sync(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    assert agent.sync_shop() is None
    step = set_milestone(agent, "My first listing live", day(agent, 7), metric="listings_live", target="1")
    row = milestone(agent, step)
    assert (row["metric"], row["target"], row["created_by"]) == ("listings_live", 1, "agent")
    assert row["measure"] == "Your listings live on Etsy now: at least 1 listing (Ember's code checks it)"
    refused = call(agent, "milestone_update", milestone_id=step, status="done", result="1 listing live, #900000001")
    assert not refused.ok and "Ember's code closes milestone" in refused.text, refused.text
    with (
        agent.db.transaction() as conn,
        pytest.raises(sqlite3.IntegrityError, match="closes a milestone with a metric"),
    ):
        conn.execute(
            "UPDATE milestones SET status = 'done', result = 'x', closed_at = 'now', closed_by = 'agent' WHERE id = ?",
            (step,),
        )
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="are final"):
        conn.execute("UPDATE milestones SET target = 0 WHERE id = ?", (step,))
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "active")]
    grade(agent)  # the shop hasn't been read since: nothing is known yet
    assert milestone(agent, step)["status"] == "open"
    agent.clock.advance(minutes=61)
    agent.sync_shop()
    row = milestone(agent, step)
    assert (row["status"], row["closed_by"], row["progress"]) == ("done", "code", 1)
    assert row["result"].startswith(
        "Ember's code checked it: listings_live 1 listing (#900000001), target at least 1 listing (Etsy's numbers read "
    )
    events = [e["message"] for e in agent.db.recent_events(limit=30)]
    assert any(e.startswith(f"Ember's code closed milestone #{step} done: listings_live 1 listing") for e in events)
    seen = rows(agent, f"SELECT metric, value FROM observations WHERE subject = 'milestone' AND subject_id = {step}")
    assert seen == [{"metric": "listings_live", "value": 1}]


def test_a_miss_says_the_numbers_and_whether_there_was_enough_to_judge(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    agent.sync_shop()
    step = set_milestone(agent, "First order", day(agent, 1), metric="orders_observed", target="1")
    agent.clock.advance(days=2)
    agent.sync_shop()  # the fake shop's listing #900000001 never sells
    row = milestone(agent, step)
    views = rows(agent, f"SELECT views FROM etsy_listings WHERE listing_id = {listing_id}")[0]["views"]
    assert 0 < views < 200
    assert (row["status"], row["closed_by"]) == ("missed", "code")
    assert row["result"].startswith("Ember's code checked it after its date: orders_observed 0 orders, target at least")
    assert row["result"].endswith(f"Too little to judge: {views:,} views in all, fewer than 200.")


def test_old_numbers_are_not_graded(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    agent.sync_shop()
    step = set_milestone(agent, "Two live", day(agent, 1), metric="listings_live", target="2")
    agent.clock.advance(days=2)  # past its date, but the shop wasn't read since it was set
    assert grade(agent) == []
    assert milestone(agent, step)["status"] == "open"
    with agent.db.connection() as conn:  # 0.35.0: as YOUR PLAN lists it
        today = agent.clock.today()
        text = "\n".join(roadmap.milestone_line(m, today, False) for m in roadmap.open_milestones(conn, agent.scope()))
    assert (
        f'#{step} "Two live"' in text
        and "Ember's code checks it: listings_live at least 2 listings; not checked" in text
    )


def test_revenue_spending_and_a_ventures_stage(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    agent.clock.advance(minutes=1)  # what was spent before is not "since it was set"
    [project] = rows(agent, "SELECT id FROM projects ORDER BY id LIMIT 1")
    earn = set_milestone(
        agent, "Earn 4 USD with it", day(agent, 20), metric="revenue_verified_usd", target="4", project_id=project["id"]
    )
    tight = set_milestone(agent, "Spend a cent at most", day(agent, 20), metric="api_spend_usd", target="0.01")
    loose = set_milestone(agent, "Spend 50 at most", day(agent, 1), metric="api_spend_usd", target="50")
    with agent.db.transaction() as conn:
        venture = ventures.create(
            conn,
            agent.scope(),
            title="Wedding planners",
            pitch="Planners for weddings",
            stage="idea",
            now="2026-09-01T10:00:00Z",
        )
    backed = set_milestone(
        agent, "Backed", day(agent, 20), metric="stage_reached", target="building", venture_id=venture
    )
    case = set_milestone(agent, "A case", day(agent, 20), metric="case_complete", venture_id=venture)
    assert grade(agent) == []
    owner_entry(agent.economy, "revenue", "5", project_id=project["id"], test_money=True)
    agent.run_cycle("schedule")  # Ember's code checks the milestones before the plan; the cycle spends more than a cent
    assert milestone(agent, earn)["status"] == "done"
    assert milestone(agent, earn)["result"].startswith(
        f"Ember's code checked it: revenue_verified_usd $5.00 (project #{project['id']}), target at least $4.00"
    )
    grade(agent)
    assert (milestone(agent, tight)["status"], milestone(agent, loose)["status"]) == ("missed", "open")
    assert "over the limit" in milestone(agent, tight)["result"]
    assert owner(agent).decide_venture(venture, {"action": "back", "confirm": True}, "Owner").status == 200
    grade(agent)
    assert milestone(agent, backed)["status"] == "done" and milestone(agent, case)["status"] == "open"
    assert owner(agent).decide_venture(venture, {"action": "kill", "comment": "No."}, "Owner").status == 200
    assert (milestone(agent, case)["status"], milestone(agent, case)["closed_by"]) == ("dropped", "owner")
    assert milestone(agent, case)["result"] == f"Your owner killed venture #{venture}."  # its milestones go with it
    agent.clock.advance(days=2)
    grade(agent)
    assert milestone(agent, loose)["status"] == "done"  # a ceiling kept to its date


def test_views_gained_need_the_history_and_count_from_when_it_was_set(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    agent.clock.advance(minutes=61)  # the cycles read the shop before the listing was live
    agent.sync_shop()
    before = rows(agent, f"SELECT views FROM etsy_listings WHERE listing_id = {listing_id}")[0]["views"]
    step = set_milestone(agent, "Fifty more views", day(agent, 7), metric="views_delta", target="50")
    assert milestone(agent, step)["baseline"] == before
    agent.clock.advance(hours=12)  # two views an hour in the fake shop
    agent.sync_shop()
    assert grade(agent) == []  # the owner's history is off: views over time aren't kept
    assert milestone(agent, step)["status"] == "open"
    agent.clock.advance(hours=14)
    agent.sync_shop()
    happened = grade(agent, history=True)
    assert happened and happened[0].startswith(f"Ember's code closed milestone #{step} done: views_delta")
    assert milestone(agent, step)["progress"] >= 50


def test_the_plan_sees_what_code_closed_since_the_last_cycle(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    agent.sync_shop()
    step = set_milestone(agent, "Live at last", day(agent, 7), metric="listings_live", target="1")
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    agent.clock.advance(minutes=61)
    agent.sync_shop()
    text = agent.planner_preview()  # 0.35.0: YOUR PLAN says it
    assert f'Since your last cycle, Ember\'s code closed from its records: #{step} "Live at last" done.' in text
