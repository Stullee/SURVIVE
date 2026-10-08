"""0.13.0: a prediction ledger. The agent's odds and its business cases were never checked against what came of them,
so nothing told it, its owner or the critic whether they could be trusted. Now a metric milestone the agent gives odds
(milestone_plan's likely) and a backed venture's first sale by its case's month are predictions Ember's code settles
from its records. Their record is a calibration line that triage (READY), the critic and the daily review read.
0.35.0: milestone_plan retired; the odds given before still settle."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.agent import critic, econ, metrics, predictions, ventures, views
from app.agent.fake_llm import FakeTransport, request_kind
from app.agent.service import Agent
from app.economy.clock import to_iso
from tests.roadmap_helpers import set_milestone  # noqa: E402
from tests.test_agent import rows
from tests.test_critic import proposed
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner
from tests.test_ventures import DROPSHIPPING, ETSY, PRINT, VENTURING, plan


def settle(agent: Agent) -> None:
    """What runs before every plan: the metric milestones graded, then the predictions settled."""
    ledger = agent.economy.life.scope()
    metrics.grade_all(agent.db, agent.scope(), ledger, agent.clock, False)
    predictions.settle_all(agent.db, agent.scope(), ledger, agent.clock)


def called(agent: Agent) -> list[tuple[str, float, str, str]]:
    return [
        (r["kind"], r["probability"], r["status"], r["result"] or "")
        for r in rows(agent, "SELECT kind, probability, status, result FROM predictions ORDER BY id")
    ]


def test_a_milestones_odds_are_settled_by_code(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake)  # ordinary cycles: the roadmap is laid out there
    today = agent.clock.today()
    soon, later = (today + timedelta(days=7)).isoformat(), (today + timedelta(days=14)).isoformat()
    for title, metric, target, due, likely in (  # given before 0.35.0, each leading to the money goal #1
        ("Research that finds", "research_calls_ok", "1", soon, 70),
        ("First revenue", "revenue_verified_usd", "5", later, 40),
    ):
        made = set_milestone(agent, title, due, metric=metric, target=target)
        [row] = rows(agent, f"SELECT measure FROM milestones WHERE id = {made}")
        with agent.db.transaction() as conn:
            claim = f"milestone #{made}: {row['measure']}"
            predictions.add_milestone(conn, agent.scope(), made, likely, claim, due, to_iso(agent.clock.now()))
    [first, second] = rows(agent, "SELECT milestone_id, claim, due FROM predictions ORDER BY id")
    assert first["due"] == soon and first["claim"].endswith(
        "Research calls that found something since it was set: at least 1 call (Ember's code checks it)"
    )
    with agent.db.transaction() as conn:  # research that found something: the first milestone is met
        ventures.add_research(conn, ETSY, 1, None, "q", None, 2, 10, to_iso(agent.clock.now()))
    settle(agent)
    assert called(agent)[0][:3] == ("milestone", 0.7, "hit")
    assert called(agent)[1][2] == "open"
    agent.clock.advance(days=15)  # the second's date passes without it
    settle(agent)
    assert called(agent)[1][:3] == ("milestone", 0.4, "miss")
    assert f"milestone #{second['milestone_id']} wasn't met by {later} (it is missed)" in called(agent)[1][3]
    with agent.db.connection() as conn:
        record = predictions.calibration(conn, agent.scope())
        review = predictions.review_text(conn, agent.scope(), "2000-01-01")
    assert record == "milestones given odds: 1 of 2 met (50%) at 55% on average (Brier 0.13; a coin toss scores 0.25)"
    assert review.startswith("YOUR FORECASTS (Ember's code settles them against its records)\n#2 miss (40%): milestone")
    assert review.endswith(f"Your record: {record}.")
    shown = {m["title"]: m["prediction"] for m in views.roadmap_view(agent)["items"]}
    assert (shown["Research that finds"]["likely"], shown["Research that finds"]["status"]) == (70, "hit")
    assert shown["First revenue"]["status"] == "miss"
    assert views.roadmap_view(agent)["forecasts"] == record
    with pytest.raises(sqlite3.IntegrityError, match="a settled prediction is final"), agent.db.transaction() as conn:
        conn.execute("UPDATE predictions SET result = 'no'")


def test_a_backed_ventures_first_sale_is_a_prediction_the_critic_and_triage_read(data_dir: Path) -> None:
    fake = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, fake, settings=VENTURING)
    proposed(agent)  # DROPSHIPPING, its case's first sale in a month
    made = econ.Case("other", 30.0, 10.0, 0.0, (1, 3, 8), 10.0, 2.0, 0, 3.0)  # PRINT: a first sale at once
    with agent.db.transaction() as conn:
        ventures.add_case(conn, PRINT, None, made, econ.compute(made), to_iso(agent.clock.now()))
    actions = owner(agent)
    for vid in (DROPSHIPPING, PRINT):
        assert actions.decide_venture(vid, {"action": "back", "confirm": True}, "Stefan").status == 200
    today = agent.clock.today()
    assert rows(agent, "SELECT venture_id, claim, probability, due FROM predictions ORDER BY id") == [
        {
            "venture_id": DROPSHIPPING,
            "claim": f"venture #{DROPSHIPPING}'s first sale within 30 days of being backed (case #1)",
            "probability": 0.5,
            "due": (today + timedelta(days=30)).isoformat(),
        },
        {
            "venture_id": PRINT,
            "claim": f"venture #{PRINT}'s first sale within 14 days of being backed (case #2)",  # 14 at least
            "probability": 0.5,
            "due": (today + timedelta(days=14)).isoformat(),
        },
    ]
    sale = {"amount": "4.90", "source": "The first order", "idempotency_key": "b" * 32, "venture_id": DROPSHIPPING}
    assert agent.economy.record("revenue", sale, "Stefan").status == 201
    settle(agent)
    assert called(agent)[0][2] == "hit" and called(agent)[0][3].startswith(f"revenue for it on {today.isoformat()}")
    with pytest.raises(sqlite3.IntegrityError, match="a prediction never changes"), agent.db.transaction() as conn:
        conn.execute("UPDATE predictions SET probability = 0.9 WHERE status = 'open'")  # PRINT's, still open
    agent.clock.advance(days=15 + predictions.FIRST_SALE_GRACE_DAYS)  # 0.15.0: a late record still counts
    settle(agent)
    assert called(agent)[1][2:] == ("miss", f"no sale recorded for it by {(today + timedelta(days=14)).isoformat()}")
    record = "first sales by the business case's month: 1 of 2 on time"
    shown = {v["id"]: v["first_sale"] for v in views.ventures_view(agent)["items"]}
    assert (shown[DROPSHIPPING]["status"], shown[DROPSHIPPING]["likely"]) == ("hit", 50) and shown[ETSY] is None
    assert views.ventures_view(agent)["desk"]["forecasts"] == record
    agent.run_cycle("schedule")  # the fake model plans by itself: READY carries the record for its triage
    planner = [r for r in fake.sent if request_kind(r) == "plan"][-1]["messages"][0]["content"][0]["text"]
    assert f"\nYour forecasts, settled by Ember's code: {record}.\n" in planner
    with agent.db.connection() as conn:
        venture = ventures.get(conn, agent.scope(), DROPSHIPPING)
        case_row = ventures.latest_case(conn, DROPSHIPPING)
        assert venture is not None and case_row is not None
        text = critic.case_text(conn, venture, case_row, predictions.calibration(conn, agent.scope()))
    assert text.endswith(f"\n\nThe agent's forecasts, settled by Ember's code: {record}.")


def test_the_record_says_which_way_the_odds_lean() -> None:
    assert predictions.record_text([]) == ""
    high = [("milestone", 0.8, hit) for hit in (True, True, False, False, False, False)]
    assert predictions.record_text(high) == (
        "milestones given odds: 2 of 6 met (33%) at 80% on average: your odds run high (Brier 0.44; a coin toss "
        "scores 0.25)"
    )
    low = [("milestone", 0.3, hit) for hit in (True, True, True, True, False)]
    assert ": your odds run low (Brier" in predictions.record_text(low)
    few = [("milestone", 0.8, False)] * 4  # fewer than MIN_SETTLED: no lean yet
    assert "run" not in predictions.record_text(few)
    both = predictions.record_text([("milestone", 0.6, True), ("first_sale", 0.5, False)])
    assert both.endswith("; first sales by the business case's month: 0 of 1 on time")
