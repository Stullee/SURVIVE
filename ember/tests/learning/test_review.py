"""The daily business review (0.7.1): the scorecard Ember's code builds from its records, the review before the
first plan of a day, and what the plans and the dashboard see of it.

Also tests from 0.19.2, 0.23.1, 0.24.0 and 0.33.0 (formerly test_fixes_0192.py, test_fixes_0231.py,
test_fixes_0240.py, test_fixes_0330.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import context, loop, prompts, review, roadmap, store
from app.agent.fake_llm import Fail, FakeTransport, Reply, request_kind, validate_request
from app.config import Settings
from app.economy import pricing
from app.economy.clock import to_iso
from app.economy.metering import REVIEW, MeteredModel, NotSent, rough_token_count
from tests.agent_helpers import owner, rows, run, unbraked
from tests.channels.test_pinterest import PINNING
from tests.channels.test_printify import EVERYWHERE
from tests.economy_helpers import owner as owner_entry
from tests.plan.test_roadmap import plan
from tests.ventures.test_venture_channels import not_set_up


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
    # once a day; 0.18.0: the weekly look follows the first review (its call is a review's in the books)
    assert today[:3] == ["review", "weekly", "plan"] and today.count("review") == 1
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
    # 0.18.0: the review's purpose is the weekly look's too
    cost = rows(agent, "SELECT SUM(cost_micros) AS c FROM llm_calls WHERE purpose = 'review'")[0]["c"]
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


def test_a_bug_after_a_paid_review_ends_no_cycle_and_isnt_paid_for_again_and_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.22.0 (analysis 0.20.1, FIX NOW 17): the review ran outside the guard of the steps before the plan. A bug after
    its paid call ended the cycle before it planned, nothing recorded the review, and the next cycle paid for it
    again."""
    agent, fake = next_day(data_dir)

    def broken(*_: Any) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(review, "parse", broken)
    for _ in range(3):
        before = len(fake.sent)
        assert agent.run_cycle("schedule").status == "completed"  # the plan goes on
        assert "plan" in kinds(fake, before)
    assert kinds(fake).count("review") == review.MAX_ATTEMPTS  # at most as often as failed reviews
    events = rows(agent, "SELECT message FROM events WHERE message = 'The daily review failed: RuntimeError'")
    assert len(events) == review.MAX_ATTEMPTS


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
    agent, _ = run(data_dir, FakeTransport(), cycles=3, brake=False)  # the fake's founder routine works on line #1
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
        # 0.39.1: only the owner closes a line: the review's stop is their decision (plan._review_stops)
        assert "- #1 stop: No demand after a day. → your owner decides on it (their Plan tab)" in text


def test_the_dry_run_agent_holds_what_its_review_stopped(data_dir: Path) -> None:
    from tests.learning.test_weekly_look import judged  # noqa: PLC0415

    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=2, brake=False)  # the fake started line #1 and works on it
    judged(agent, (1, "stop", "demand"))  # today's review says stop
    with unbraked():  # 0.37.6: its founder routine didn't write the demand note it was given: it would wait
        agent.run_cycle("schedule")  # the plan tree's step is line #1's (its only product)
    # 0.35.0: only the owner closes or drops a product: the agent holds it, to work elsewhere
    assert rows(agent, "SELECT status FROM projects WHERE id = 1")[0]["status"] == "active"
    [product] = rows(agent, "SELECT hold_reason FROM plan_nodes WHERE level = 'product' AND project_id = 1")
    assert product["hold_reason"] and "stop" in product["hold_reason"]
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
        "Act on it: a stop is your owner's decision (only they close a line): Ember's code asks them on their Plan tab;"
        " until they decide, Ember's code holds the line: its steps wait. A change is a step of its line's,"
        ' due today ("Review MM-DD: ...") unless the line waits for your owner\'s keep-or-drop: carry it out, a'
        " request to your owner where it needs one, then plan_step done. Work on a bottleneck (reach: its marketing"
        " cycles bring buyers) waits for its line's cycle. Ember's code kept the lesson."
        "\n- continue: #1 (too_early)"
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


# --- From 0.19.2 (formerly test_fixes_0192.py) ---


# --- the daily review's roadmap ------------------------------------------------------------------------------------


def test_the_review_scorecard_shows_the_roadmap_before_the_projects(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    scope = agent.scope()
    now = "2026-09-01T12:00:00Z"
    with agent.db.transaction() as conn:
        for number in range(12):
            store.create_project(
                conn,
                scope,
                cycle_id=1,
                title=f"Product line {number} " + "x" * 50,
                hypothesis="h" * 400,
                next_step="n" * 150,
                status="active",
                now=now,
            )
        made = roadmap.create(conn, scope, title="Day 7: 10 views", measure="views", due="2026-09-20", now=now)
    card = _scorecard(agent)
    assert f'\n- due later this month: #{made} "Day 7: 10 views" (2026-09-20)' in card.text
    assert card.text.index("ROADMAP") < card.text.index("PROJECTS")


def _scorecard(agent: Any) -> review.Scorecard:
    books, ledger_scope = agent.economy.books, agent.economy.life.scope()
    with agent.db.connection() as conn:
        return review.scorecard(
            conn,
            agent.scope(),
            agent.clock,
            books,
            ledger_scope,
            agent.economy.life.evaluate(),
            dry_run=True,
        )


# --- From 0.23.1 (formerly test_fixes_0231.py) ---


# --- 1. the daily review's attempts ---


def test_a_free_retry_of_the_review_call_doesn_t_use_up_the_day_s_second_review(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loop, "RETRY_DELAY_SECONDS", 0)
    agent, fake = next_day(data_dir)
    # the first call never reached the API (free, tried again at once); the retry's answer isn't JSON
    fake.script.extend([Fail(NotSent("no connection")), Reply("Things are going fine.")])
    agent.run_cycle("schedule")
    assert [r["status"] for r in rows(agent, "SELECT status FROM llm_calls WHERE purpose = 'review'")] == [
        "failed",
        "ok",
    ]
    assert [r["status"] for r in rows(agent, "SELECT status FROM reviews")] == ["failed"]
    with agent.db.connection() as conn:
        assert review.sent_today(conn, agent.scope(), agent.clock.today()) == 1
        assert review.due(conn, agent.scope(), agent.clock)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    assert "review" in kinds(fake, before)  # the day's second attempt


def test_the_weekly_look_doesn_t_use_up_the_day_s_second_review(data_dir: Path) -> None:
    agent, fake = next_day(data_dir)
    agent.run_cycle("schedule")  # the review and the weekly look
    agent.clock.advance(days=7)
    fake.script.append(Reply("Things are going fine."))  # today's review isn't JSON; the weekly look follows
    before = len(fake.sent)
    agent.run_cycle("schedule")
    assert kinds(fake, before)[:2] == ["review", "weekly"]
    before = len(fake.sent)
    agent.run_cycle("schedule")
    assert "review" in kinds(fake, before)


def test_answered_review_calls_count_whatever_happens_with_their_answer(data_dir: Path) -> None:
    """0.22.0's guard stays: a review call answered and paid for counts at once, though nothing saved its review."""
    agent, _ = next_day(data_dir)
    scope, today, now = agent.scope(), agent.clock.today(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        assert review.due(conn, scope, agent.clock)
        for _ in range(review.MAX_ATTEMPTS):
            review.note_sent(conn, scope, today, now)
        assert review.sent_today(conn, scope, today) == review.MAX_ATTEMPTS
        assert not review.due(conn, scope, agent.clock)
    agent.clock.advance(days=1)
    with agent.db.connection() as conn:
        assert review.sent_today(conn, scope, agent.clock.today()) == 0  # a new day


# --- 3. every project stays named in the review ---


def test_the_review_names_every_project_it_leaves_out() -> None:
    lines = []
    for pid in range(1, 41):
        lines.append(f"#{pid} [active] Product line {pid} with a long enough title to fill its line · spent $0.10")
        if pid % 3 == 0:
            lines.append(f"   hypothesis: People buy product {pid}.")
    text = "\n".join(lines)
    for room in (2_000, 1_000, 700):
        cut = review._cut_lines(text, room)
        assert len(cut) <= room, room
        for pid in range(1, 41):
            assert f"#{pid} " in cut or f"#{pid}," in cut or cut.endswith(f"#{pid}]"), (room, pid)
        assert "left out, for the scorecard's room: #" in cut
    assert review._cut_lines(text, len(text)) == text


# --- From 0.24.0 (formerly test_fixes_0240.py) ---


def test_the_daily_review_hears_which_channels_wait_for_the_owner(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1, settings=PINNING)
    not_set_up(agent)
    agent.clock.advance(days=1)
    since = len(fake.sent)
    agent.run_cycle("schedule")
    [sent] = [r for r in list(fake.sent)[since:] if request_kind(r) == "review"]
    card = sent["messages"][0]["content"][0]["text"]
    assert "CHANNELS NOT READY (no request or tool can use them until then)\n- Pinterest: Switched on, but it" in card
    assert "Your owner's dashboard shows it: don't ask them about it again." in card
    assert loop._waiting("Pinterest", ("ok", None), "ok") == ""  # set up: nothing waits


# --- From 0.33.0 (formerly test_fixes_0330.py) ---


# --- the review hears which channels are ready ---


def test_the_daily_review_hears_which_channels_are_ready(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1, settings=EVERYWHERE)
    agent.clock.advance(days=1)
    since = len(fake.sent)
    agent.run_cycle("schedule")
    [sent] = [r for r in list(fake.sent)[since:] if request_kind(r) == "review"]
    card = sent["messages"][0]["content"][0]["text"]
    settings = EVERYWHERE
    assert (
        f"CHANNELS READY (their tools work now): Pinterest: ember-dry-run, at most {settings.pinterest_pins_per_day}"
        f" pins a day; Bluesky: 0 followers, no post live, at most {settings.bluesky_posts_per_day} posts a day;"
        f" Printify: at most {settings.printify_products_per_day} products a day." in card
    )
    assert "CHANNELS NOT READY" not in card


# --- From 0.42.6 (analysis-0.42.3, 3.3) ---


def test_the_scorecard_quotes_the_owners_messages_and_lists_the_kdp_books(data_dir: Path) -> None:
    """Live, the review of 10-22 read the owner's decisions on requests only: its focus planned the Fahrtenbuch the
    owner had put off in #205, and pins "linking to Amazon" for two books without a link."""
    agent, _ = next_day(data_dir)
    said = "Fix the three table errors in the Kassenbuch first. The Fahrtenbuch waits."
    assert owner(agent).send_message({"text": said}, "Stefan").status == 201
    [message] = rows(agent, "SELECT MAX(id) AS id FROM messages WHERE sender = 'owner'")
    stamp = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:  # two KDP books: one published with its link, one without
        for n, link in ((1, "https://www.amazon.de/dp/B0EXAMPLE"), (2, None)):
            book = store.insert_approval(
                conn, agent.scope(), 1, stamp, project_id=None, type="sell", title=f"KDP paperback: Book {n}",
                description="a book", payload=f"book {n}", expected_cost="none", expected_benefit="royalties",
                executor="kdp_package", action=json.dumps({"format": "paperback"}),
            )  # fmt: skip
            conn.execute("UPDATE approvals SET status = 'approved', decided_at = ? WHERE id = ?", (stamp, book))
            conn.execute(
                "UPDATE approvals SET status = 'done', closed_at = ?, result_link = ? WHERE id = ?", (stamp, link, book)
            )
    text = scorecard(agent).text
    assert "YOUR OWNER'S MESSAGES IN THE PERIOD (the newest first; what they ask stands" in text
    assert f"- #{message['id']} (" in text and f'UTC): "{said}"' in text
    assert '"KDP paperback: Book 1": published at https://www.amazon.de/dp/B0EXAMPLE' in text
    assert '"KDP paperback: Book 2": published, no link at Amazon yet: no pin or post can link it' in text
    assert text.index("YOUR OWNER'S MESSAGES") < text.index("AMAZON KDP") < text.index("#1 [")  # before the projects
    assert "- Read your owner's messages, decisions and comments" in prompts.REVIEW_RULES
