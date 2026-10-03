"""0.18.0: phases 2 and 3 of vision/learning.md, the learning loop. Bets say what the agent expects of a change and
Ember's code settles them; the daily review writes a retrospective of each thing that settled, kept as a case; the
weekly look reads the whole business, rewrites the strategy and draws principles from the cases, whose confidence
Ember's code sets; the plan's LESSONS shows that playbook first and the work steps recall the matching cases; an
independent critic scores a product line's listing; and an ordinary plan gets READY, the useful work while projects
wait, with no long sleep while it lists something."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import bets, context, learning, prompts, quality, reach, slack, tools, ventures, weekly  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call, shop_context  # noqa: E402
from tests.test_listing_gates import started  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_printify import proposed  # noqa: E402


def now(agent: Any) -> str:
    return to_iso(agent.clock.now())


def views(agent: Any, listing_views: int, favorites: int = 0) -> None:
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET views = ?, favorites = ?", (listing_views, favorites))


def place(agent: Any, project: int, text: str) -> tools.Outcome:
    """project_update with a bet, as the model calls it."""
    return call(shop_context(agent), "project_update", {"project_id": project, "bet": text})


def settle(agent: Any) -> list[str]:
    with agent.db.transaction() as conn:
        return bets.settle(conn, agent.scope(), agent.clock.today(), now(agent))


# --- bets ---


def test_a_bet_is_parsed_or_refused_with_how_to_write_it() -> None:
    assert bets.parse("+15 views in 7 days: the pins bring buyers") == (15, "views", 7, "the pins bring buyers")
    assert bets.parse("2 sales within 21 days - the bundle") == (2, "orders", 21, "the bundle")
    for wrong in (
        "more views soon",
        "+15 clicks in 7 days: x",
        "+5 views in 2 days: too short",
        "+5 views in 60 days: x",
    ):
        with pytest.raises(bets.BetError, match="write it as"):
            bets.parse(wrong)


def test_a_bet_on_views_without_reach_settles_as_no_reach(data_dir: Path) -> None:
    agent, project = started(data_dir)
    views(agent, 2)
    outcome = place(agent, project, "+15 views in 7 days: the new title is clearer")
    assert outcome.ok and "Bet #1: +15 views by " in outcome.text
    assert settle(agent) == []  # not due, not won
    agent.clock.advance(days=8)
    views(agent, 6)
    [said] = settle(agent)
    assert said.startswith("Bet #1 on project") and " no_reach: " in said
    [bet] = rows(agent, "SELECT status, final FROM bets")
    assert (bet["status"], bet["final"]) == ("no_reach", 6)


def test_a_bet_is_won_as_soon_as_its_gain_is_there(data_dir: Path) -> None:
    agent, project = started(data_dir)
    views(agent, 2)
    place(agent, project, "+10 views in 14 days: the blog post links it")
    views(agent, 14)
    [said] = settle(agent)
    assert " won: +10 views by " in said and "got +12" in said


def test_favorites_and_orders_wait_until_the_listings_are_seen(data_dir: Path) -> None:
    agent, project = started(data_dir)
    views(agent, 3)
    refused = place(agent, project, "+2 favorites in 7 days: better photos")
    assert not refused.ok and "aren't seen yet (3 views): bet on views" in refused.text
    views(agent, 40)
    with agent.db.transaction() as conn:
        quality.save(
            conn, agent.scope(), project, None, {"score": 5, "verdict": "improve", "fixes": "x"}, None, now(agent)
        )
    refused = place(agent, project, "+1 orders in 14 days: the price is right")
    assert not refused.ok and "its last quality check said improve" in refused.text
    assert place(agent, project, "+2 favorites in 7 days: better photos").ok
    again = place(agent, project, "+3 favorites in 7 days: more")
    assert not again.ok and "has an open bet on favorites" in again.text


# --- retrospectives and cases ---


def test_what_settled_is_listed_and_its_retrospectives_become_cases(data_dir: Path) -> None:
    agent, project = started(data_dir)
    views(agent, 1)
    place(agent, project, "+15 views in 7 days: the new title")
    agent.clock.advance(days=8)
    settle(agent)
    with agent.db.connection() as conn:
        items = learning.settled(conn, agent.scope(), "2000-01-01T00:00:00Z")
    assert items[0].subject == "bet #1" and items[0].project_id == project
    text = learning.settled_text(items)
    assert text.startswith("SETTLED SINCE YOUR LAST REVIEW") and "- bet #1: project #" in text
    retros = learning.parse_retros(
        [
            {
                "subject": "bet #1",
                "expected": "+15",
                "happened": "+0",
                "why": "Nobody saw it.",
                "cause": "no_reach",
                "sure": "high",
                "lesson": "Bring buyers before betting on views.",
            },
            {
                "subject": "bet #99",
                "expected": "",
                "happened": "",
                "why": "x",
                "cause": "worked",
                "sure": "low",
                "lesson": "",
            },
            {
                "subject": "bet #1",
                "expected": "",
                "happened": "",
                "why": "twice",
                "cause": "worked",
                "sure": "low",
                "lesson": "",
            },
        ],
        {i.subject for i in items},
    )
    assert [r["subject"] for r in retros] == ["bet #1"]
    with agent.db.transaction() as conn:
        [case_id] = learning.save_cases(conn, agent.scope(), None, retros, items, now(agent))
    [case] = rows(agent, "SELECT * FROM cases")
    assert (case["id"], case["project_id"], case["cause"]) == (case_id, project, "no_reach")


def test_the_review_asks_for_retros_and_keeps_them(data_dir: Path) -> None:
    item = prompts.REVIEW_SCHEMA["properties"]["retros"]["items"]
    assert (
        item["properties"]["cause"]["enum"] == list(learning.CAUSES) and "retros" in prompts.REVIEW_SCHEMA["required"]
    )
    assert "Write a retrospective of each item SETTLED lists" in " ".join(prompts.REVIEW_RULES.split())


# --- the weekly look and the playbook ---


def test_the_playbook_s_confidence_is_set_by_ember_s_code_and_old_guesses_fade(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    scope, stamp = agent.scope(), now(agent)
    with agent.db.transaction() as conn:
        ids = []
        for i in range(4):
            ids += learning.save_cases(
                conn,
                scope,
                None,
                [
                    {
                        "subject": f"bet #{i}",
                        "expected": "",
                        "happened": "",
                        "why": "Nobody saw it.",
                        "cause": "no_reach",
                        "sure": "high",
                        "lesson": "",
                    }
                ],
                [],
                stamp,
            )
        said = learning.apply_principles(
            conn,
            scope,
            [
                {
                    "id": None,
                    "text": "Listings without outside traffic stay unseen.",
                    "supports": ids[:3],
                    "against": [],
                    "retire": "",
                },
                {"id": None, "text": "Price drives views.", "supports": [ids[3]], "against": [], "retire": ""},
                {"id": None, "text": "No case behind it.", "supports": [999], "against": [], "retire": ""},
            ],
            set(ids),
            stamp,
        )
        assert said[0].startswith("new principle #1 (established)") and said[1].startswith("new principle #2 (hyp")
        assert len(said) == 2  # the one citing no real case is dropped
        learning.apply_principles(
            conn, scope, [{"id": 2, "text": "", "supports": [], "against": [ids[0]], "retire": ""}], set(ids), stamp
        )
        found = {p["id"]: p["confidence"] for p in learning.principles(conn, scope)}
        assert found == {1: "established", 2: "disputed"}
        later = to_iso(agent.clock.now() + timedelta(days=learning.FADE_DAYS))
        assert learning.fade(conn, scope, later) == [
            f"Ember's code retired principle #2: no case confirmed it for {learning.FADE_DAYS} days"
        ]
    playbook = learning.playbook_text(rows(agent, "SELECT * FROM principles WHERE status = 'active'"), 1_000)
    assert playbook.startswith("Your playbook") and "[established, 3 case(s) for] Listings without" in playbook


def test_the_weekly_look_rewrites_the_strategy_unless_it_names_a_parked_venture(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    scope, mem = agent.scope(), agent.memory()
    with agent.db.transaction() as conn:
        parked = ventures.create(
            conn, scope, title="Candle subscription box", pitch="x", stage="parked", now=now(agent)
        )
        bad = weekly.parse(json.dumps({"assessment": "x", "strategy": f"Priority: candle boxes (#{parked})."}))
        assert bad is not None
        assert weekly.apply(conn, scope, mem, bad, now(agent)) == [
            "the new strategy wasn't kept: it names a parked or killed venture"
        ]
        good = weekly.parse(json.dumps({"assessment": "x", "strategy": "Bring buyers to what is live."}))
        assert good is not None and weekly.apply(conn, scope, mem, good, now(agent)) == ["the strategy was rewritten"]
    assert mem.read("strategy") == "Bring buyers to what is live.\n"


def test_the_weekly_look_runs_once_a_week_and_every_plan_sees_its_questions(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    with agent.db.connection() as conn:
        assert not weekly.due(conn, agent.scope(), agent.clock.today())  # no daily review yet
    agent.clock.advance(days=1)
    agent.run_cycle("schedule")  # the daily review, then the weekly look
    [row] = rows(agent, "SELECT * FROM weekly_reviews")
    assert row["status"] == "ok" and "PROJECTS" in row["view"] and "WHERE THE WEEK WENT" in row["view"]
    assert "the strategy was rewritten" in row["outcome"]
    with agent.db.connection() as conn:
        assert not weekly.due(conn, agent.scope(), agent.clock.today())
        text = weekly.planner_text(weekly.latest(conn, agent.scope(), agent.clock.today()))
    assert "Question to answer this week: Which channel brings the first 30 views" in text
    agent.clock.advance(days=weekly.DAYS)
    with agent.db.connection() as conn:
        assert weekly.due(conn, agent.scope(), agent.clock.today())


# --- the quality critic ---


def test_the_quality_critic_checks_a_live_product_line_once_and_again_later(data_dir: Path) -> None:
    agent, project = started(data_dir)
    with agent.db.connection() as conn:
        assert quality.due(conn, agent.scope(), agent.clock.today()) == project
        text, _ = quality.case(conn, agent.scope(), agent.roots()[0], project)
    assert "Title: " in text and "Price: " in text and "funnel: " in text
    assert quality.parse('{"score": 8, "fixes": ""}') == {"score": 8, "verdict": "pass", "fixes": ""}
    assert quality.parse('{"score": 11, "fixes": ""}') is None
    agent.clock.advance(days=1)
    agent.run_cycle("schedule")
    [check] = rows(agent, "SELECT * FROM quality_checks")
    assert (check["status"], check["score"], check["verdict"]) == ("ok", 6, "improve")
    with agent.db.connection() as conn:
        assert quality.due(conn, agent.scope(), agent.clock.today()) is None
        assert "quality 6/10, improve" in reach.research_text(conn, agent.scope(), project, None)
    agent.clock.advance(days=quality.RECHECK_DAYS)
    with agent.db.connection() as conn:
        assert quality.due(conn, agent.scope(), agent.clock.today()) == project


# --- recall ---


def test_the_work_steps_recall_the_matching_cases() -> None:
    assert context.KNOWLEDGE_HEADING.startswith("WHAT YOU LEARNED (your playbook and cases")


def test_cases_and_principles_are_found_by_their_words(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    scope, stamp = agent.scope(), now(agent)
    with agent.db.transaction() as conn:
        learning.save_cases(
            conn,
            scope,
            None,
            [
                {
                    "subject": "bet #1",
                    "expected": "",
                    "happened": "2 views",
                    "why": "No pins for the poster.",
                    "cause": "no_reach",
                    "sure": "high",
                    "lesson": "Posters need pins.",
                }
            ],
            [],
            stamp,
        )
        assert learning.relevant(conn, scope, "make pins for the new poster")[0].startswith("case #1 (bet #1")
        assert learning.similar(conn, scope, "Bauhaus poster number 3")[0].startswith("case #1")
        assert learning.relevant(conn, scope, "spreadsheet template") == []


# --- waiting time ---


def test_an_ordinary_plan_gets_ready_and_no_long_sleep_while_it_lists_something(data_dir: Path) -> None:
    agent, project = started(data_dir)
    with agent.db.connection() as conn:
        found = slack.items(conn, agent.scope(), agent.clock.today())
    keys = [i.key for i in found]
    assert f"reach #{project}" in keys
    assert slack.text(found).startswith(slack.HEADING)
    assert slack.sleep(720, found, 30, "explore") == slack.SLEEP_MINUTES
    assert slack.sleep(720, found, 240, "explore") == 240  # not below the owner's shortest sleep
    assert slack.sleep(720, found, 30, "maintenance") == 720
    assert slack.sleep(720, [], 30, "explore") == 720


# --- 0.18.1: the quality critic reads a print-on-demand product line's Printify product ---


def test_the_quality_critic_reads_a_printify_product(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    agent.execute_approved()
    [row] = rows(agent, f"SELECT project_id FROM approvals WHERE id = {request}")
    with agent.db.connection() as conn:
        found = quality._printify(conn, agent.scope(), int(row["project_id"]))
    assert found is not None
    product, prices = found
    assert product.title and "EUR" in prices
