"""The daily business review (0.7.1): the scorecard Ember's code builds from its records, the review before the
first plan of a day, and what the plans and the dashboard see of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import context, prompts, review
from app.agent.fake_llm import FakeTransport, Reply, request_kind, validate_request
from app.config import Settings
from app.economy import pricing
from app.economy.metering import REVIEW, MeteredModel, rough_token_count
from tests.economy_helpers import owner as owner_entry
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner


def kinds(fake: FakeTransport, since: int = 0) -> list[str]:
    return [request_kind(r) for r in list(fake.sent)[since:]]


def no_invalid(fake: FakeTransport) -> None:
    invalid = [t for t in fake.trace if t[1] == "invalid"]
    assert invalid == [], invalid


def next_day(data_dir: Path, fake: FakeTransport | None = None, cycles: int = 1) -> tuple[Any, FakeTransport]:
    """An agent that worked yesterday, on the morning of the next day (before any cycle today)."""
    fake = fake or FakeTransport()
    agent, _ = run(data_dir, fake, cycles=cycles)
    agent.clock.advance(days=1)
    return agent, fake


def scorecard(agent: Any) -> review.Scorecard:
    with agent.db.connection() as conn:
        return review.scorecard(
            conn,
            agent.scope(),
            agent.clock,
            agent.economy.books,
            agent.economy.life.scope(),
            agent.economy.life.evaluate(),
            dry_run=True,
        )


# --- when it happens ---------------------------------------------------------------------


def test_the_first_cycle_of_a_day_reviews_before_it_plans(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=2)
    assert "review" not in kinds(fake)  # the first day has nothing to review yet
    agent.clock.advance(days=1)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    agent.run_cycle("schedule")
    today = kinds(fake, before)
    assert today[:2] == ["review", "plan"] and today.count("review") == 1  # once a day
    saved = rows(agent, "SELECT day, status, verdicts, focus, note FROM reviews")
    assert len(saved) == 1 and saved[0]["status"] == "ok" and saved[0]["note"] is None
    assert saved[0]["day"] == agent.clock.today().isoformat() and saved[0]["focus"]
    assert [v["project_id"] for v in json.loads(saved[0]["verdicts"])] == [1]
    plans = [r for r in list(fake.sent)[before:] if request_kind(r) == "plan"]
    assert all("\n== TODAY'S REVIEW ==\nYour review of today" in r["messages"][0]["content"][0]["text"] for r in plans)
    call = rows(agent, "SELECT model, cost_micros FROM llm_calls WHERE purpose = 'review'")
    assert call[0]["model"] == agent.settings.planner_model and call[0]["cost_micros"] > 0
    no_invalid(fake)


def test_the_review_counts_toward_the_day_not_the_cycle(data_dir: Path) -> None:
    agent, _ = next_day(data_dir)
    agent.run_cycle("schedule")
    cycle_id = rows(agent, "SELECT cycle_id FROM reviews")[0]["cycle_id"]
    cost = rows(agent, "SELECT cost_micros FROM llm_calls WHERE purpose = 'review'")[0]["cost_micros"]
    everything, _ = agent.economy.books.cycle_spend(cycle_id)
    capped, _ = agent.economy.books.cycle_spend(cycle_id, outside_cap=False)
    assert everything - capped == cost > 0
    spent_today = agent.economy.books.cap_spend_on(agent.economy.life.scope(), agent.clock.today())
    assert spent_today >= everything


def test_a_review_the_budget_cant_cover_now_waits_for_the_next_cycle(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, fake = next_day(data_dir)
    real = MeteredModel.headroom

    def no_room_for_reviews(self: MeteredModel, cycle_id: int, purpose: str = "work", keep: int = 0) -> int:
        return 0 if purpose == REVIEW else real(self, cycle_id, purpose, keep)

    monkeypatch.setattr(MeteredModel, "headroom", no_room_for_reviews)
    before = len(fake.sent)
    assert agent.run_cycle("schedule").status == "completed"  # the cycle goes on without it
    assert "review" not in kinds(fake, before) and not rows(agent, "SELECT id FROM reviews")
    monkeypatch.undo()
    agent.run_cycle("schedule")
    assert [r["status"] for r in rows(agent, "SELECT status FROM reviews")] == ["ok"]


def test_a_review_that_isnt_json_is_recorded_and_tried_once_more(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    fake.script.append(Reply("Things are going fine."))
    assert agent.run_cycle("schedule").status == "completed"  # the plan goes on
    assert rows(agent, "SELECT status, note FROM reviews") == [
        {"status": "failed", "note": "the review wasn't valid JSON"}
    ]
    fake.script.append(Reply("Still fine.", "max_tokens"))
    agent.run_cycle("schedule")
    before = len(fake.sent)
    agent.run_cycle("schedule")
    assert "review" not in kinds(fake, before)  # two failures: no more reviews today
    assert [r["note"] for r in rows(agent, "SELECT note FROM reviews")] == [
        "the review wasn't valid JSON",
        "the review was cut off (max_tokens)",
    ]
    events = rows(agent, "SELECT message FROM events WHERE message LIKE 'The daily review failed%'")
    assert len(events) == 2


def test_only_verdicts_on_listed_projects_count() -> None:
    answer = {
        "verdicts": [
            {"project_id": 1, "verdict": "stop", "why": "x" * 500},
            {"project_id": 1, "verdict": "continue", "why": "a second verdict on #1"},
            {"project_id": 7, "verdict": "stop", "why": "not listed"},
            {"project_id": 2, "verdict": "pause", "why": "not a verdict"},
            {"project_id": True, "verdict": "stop", "why": "not a number"},
        ],
        "working": "w" * 900,
        "not_working": "",
        "owner_feedback": "",
        "lesson": "Finish before asking.",
        "focus": "One listing.",
    }
    parsed = review.parse(f"Here it is:\n{json.dumps(answer)}\nDone.", {1, 2})
    assert parsed is not None and [(v.project_id, v.verdict) for v in parsed.verdicts] == [(1, "stop")]
    assert len(parsed.verdicts[0].why) == review.WHY_CHARS and len(parsed.working) == review.LIMITS["working"]
    assert review.parse("No JSON here.", {1}) is None
    assert review.parse(json.dumps({"verdicts": [], "focus": ""}), {1}) is None


# --- the scorecard ---------------------------------------------------------------------


def test_the_scorecard_holds_the_facts(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=3)
    pending = rows(agent, "SELECT id FROM approvals WHERE status = 'pending' ORDER BY id")
    assert pending, "the fake asks for an approval in its third cycle"
    rejection = {"decision": "reject", "comment": "Too pricey for a first try."}
    decided = owner(agent).decide(pending[0]["id"], rejection, "Stefan")
    assert decided.status == 200
    owner_entry(agent.economy, "revenue", "12.50", source="Etsy: the meal planner")
    agent.clock.advance(days=1)
    card = scorecard(agent)
    text = card.text
    assert card.project_ids == {1}
    assert text.startswith("YOUR NUMBERS (from Ember's records: exact)\nPeriod: the last 7 days,")
    assert "DRY RUN: the money is simulated." in text
    assert "Revenue recorded: $12.50 in these days, $12.50 in all." in text
    assert "$12.50 from Etsy: the meal planner" in text
    assert "YOUR LAST REVIEW\nThis is your first review." in text
    assert "#1 [active] " in text and "3 cycles in the period (3 in all)" in text and "hypothesis: " in text
    assert "1 rejected" in text and '"Too pricey for a first try."' in text
    assert "3 cycles (3 completed)" in text
    spent = rows(agent, "SELECT SUM(cost_micros) AS s FROM llm_calls")[0]["s"]
    assert f"API calls: ${spent / 1_000_000:.2f} in the period (planning and work $" in text
    assert len(text) <= review.SCORECARD_MAX


def test_the_scorecard_holds_the_agent_to_its_last_verdicts(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    fake.script.append(
        Reply(
            json.dumps(
                {
                    "verdicts": [{"project_id": 1, "verdict": "stop", "why": "No demand after a day."}],
                    "working": "",
                    "not_working": "No listing yet.",
                    "owner_feedback": "",
                    "lesson": "Test demand first.",
                    "focus": "Something new.",
                }
            )
        )
    )
    agent.run_cycle("schedule")
    assert rows(agent, "SELECT status FROM reviews")[0]["status"] == "ok"
    agent.clock.advance(days=1)
    text = scorecard(agent).text
    assert "YOUR LAST REVIEW (" in text and "Your focus then: Something new." in text
    if rows(agent, "SELECT status FROM projects WHERE id = 1")[0]["status"] in ("idea", "active", "waiting"):
        assert "- #1 stop: No demand after a day. → still " in text and "you haven't carried it out" in text


def test_the_dry_run_agent_carries_out_its_stop(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=14)  # 14 cycles on one project: the fake's review says stop
    agent.clock.advance(days=1)
    for _ in range(3):
        agent.run_cycle("schedule")
    verdicts = json.loads(rows(agent, "SELECT verdicts FROM reviews")[0]["verdicts"])
    assert verdicts[0]["project_id"] == 1 and verdicts[0]["verdict"] == "stop"
    assert rows(agent, "SELECT status FROM projects WHERE id = 1")[0]["status"] == "abandoned"
    assert rows(agent, "SELECT COUNT(*) AS n FROM projects WHERE status = 'active'")[0]["n"] == 1  # a new start
    no_invalid(fake)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_chaos_reviews_never_break_a_cycle(data_dir: Path, seed: int) -> None:
    fake = FakeTransport(scenario="chaos", seed=seed)
    agent, _ = run(data_dir, fake, cycles=2)
    due_days = set()
    for _ in range(4):
        agent.clock.advance(days=1)
        # Due once a cycle of an earlier day completed. Under chaos every cycle of a day may fail, and each cycle's
        # nonce makes the fake's answers differ from run to run, so the days are counted, not assumed.
        if rows(agent, "SELECT id FROM cycles WHERE status = 'completed'"):
            due_days.add(agent.clock.today().isoformat())
        for _ in range(2):
            assert agent.run_cycle("schedule").status in ("completed", "idle", "failed", "refused", "stopped")
    assert {r["day"] for r in rows(agent, "SELECT day FROM reviews")} == due_days  # tried on every day it was due
    no_invalid(fake)


# --- what the plans, the prices and the dashboard see -------------------------------------


def test_the_planner_sees_todays_review_only_today(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    agent.run_cycle("schedule")
    with agent.db.connection() as conn:
        today = review.of_day(conn, agent.scope(), agent.clock.today())
        assert today is not None
        text = review.planner_text(conn, today)
    assert text.startswith(f"Your review of today ({agent.clock.today().isoformat()})")
    # 0.15.0: the advice first, the verdicts after it (a project to continue on one line), so a cut takes them first.
    assert "\nFocus today: " in text and text.endswith(
        "Act on it: carry out every stop and change (project_update), and keep the lesson with memory_update if it"
        " is new.\n- continue: #1"
    )
    agent.clock.advance(days=1)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    plan = next(r for r in list(fake.sent)[before:] if request_kind(r) == "plan")
    # A new day's plan shows the new day's review, never yesterday's.
    assert f"Your review of today ({agent.clock.today().isoformat()})" in plan["messages"][0]["content"][0]["text"]


def test_the_review_profile_covers_the_biggest_review() -> None:
    card = ("ä" * 40 + "\n") * (review.SCORECARD_MAX // 41)
    request = prompts.review_request(Settings(agent_name="X" * 40), card)
    tokens = rough_token_count(request)
    assert tokens <= pricing.REVIEW_CALL.input_tokens <= tokens * 1.15
    assert pricing.REVIEW_CALL.max_tokens == prompts.REVIEW_MAX_TOKENS == request["max_tokens"]
    assert validate_request(request) is None
    assert context.fits(request, pricing.REVIEW_CALL.input_tokens)


def test_a_thinking_planner_reviews_with_room_to_think() -> None:
    request = prompts.review_request(Settings(planner_model="claude-opus-5-5"), "YOUR NUMBERS")
    assert request["thinking"] == {"type": "adaptive"}
    assert request["max_tokens"] == prompts.REVIEW_MAX_TOKENS + pricing.THINKING_ROOM
    assert validate_request(request) is None


def test_the_owner_sees_the_reviews(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent  # type: ignore[attr-defined]
    scope = agent.scope()
    now = "2026-09-28T07:00:00Z"
    with agent.db.transaction() as conn:
        cycle = conn.execute(
            "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros,"
            " session) VALUES (?, 'b', ?, ?, 'completed', 't', 1, 0, ?)",
            (scope.life_id, now, now, scope.session),
        ).lastrowid
        project = conn.execute(
            "INSERT INTO projects (mode, session, life_id, created_cycle_id, created_at, updated_at, title, hypothesis,"
            " status) VALUES (?, ?, ?, ?, ?, ?, 'Meal planner', 'Parents buy it.', 'active')",
            (scope.mode, scope.session, scope.life_id, cycle, now, now),
        ).lastrowid
        card = review.Scorecard("YOUR NUMBERS\nMONEY\nAPI calls: $1.00", {project})
        verdict = review.Review(
            [review.Verdict(project, "change", "Cheaper price.")], "Drafts", "Sales", "Wants proof", "Test", "List it"
        )
        review.save(conn, scope, cycle, now, agent.clock.today(), card, verdict)
        review.save(conn, scope, cycle, now, agent.clock.today(), card, None, "the review wasn't valid JSON")
    shown = ingress_client.get("api/dashboard").json()["mind"]["reviews"]
    assert [r["status"] for r in shown] == ["failed", "ok"]  # newest first
    ok = shown[1]
    expected = {"project_id": project, "title": "Meal planner", "status": "active", "verdict": "change"}
    assert ok["verdicts"] == [{**expected, "why": "Cheaper price."}]
    assert (ok["working"], ok["not_working"], ok["owner_feedback"], ok["lesson"], ok["focus"]) == (
        "Drafts",
        "Sales",
        "Wants proof",
        "Test",
        "List it",
    )
    assert ok["scorecard"].startswith("YOUR NUMBERS") and shown[0]["note"] == "the review wasn't valid JSON"


def test_reviews_are_history(data_dir: Path) -> None:
    agent, _ = next_day(data_dir)
    agent.run_cycle("schedule")
    with agent.db.transaction() as conn, pytest.raises(Exception, match="history cannot change"):
        conn.execute("UPDATE reviews SET focus = 'rewritten'")
