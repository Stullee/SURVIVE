"""0.14.0, the money guard (FIX NOW 1, 2a, 26b; X4, X18): a workshop call's worst case was a guess, not a ceiling
(live, #423 was quoted $0.35 and cost $1.84), any overrun stopped the whole cycle and its reflection, the safety factor
couldn't cover a 5x miss nor come down for a rare purpose, and one work step sent 8 count_tokens requests."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.agent import prompts
from app.agent.fake_llm import FakeTransport, Overrun
from app.config import Settings
from app.db import Database, discover_migrations, migrate
from app.economy import pricing
from app.economy.estimate import MAX_SERVER_ITERATIONS, plan_request, worst_case_micros
from app.economy.metering import OVERRUN_STOP, WORKSHOP, CallRefused, Completed
from app.economy.pricing import safety_factor
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy, message, metered, request
from tests.test_agent import make_agent, plan, rows, text, tools
from tests.test_owner_loop import APPROVAL

MODEL = "claude-sonnet-5"
# The owner's live options at 0.13.0 (the workshop's cap per run raised to $1.50 after #423)
OWNER = Settings(starting_balance_usd=50, daily_spend_cap_usd=7, cycle_spend_cap_usd=1, workshop_run_cap_usd=1.5)
TASK = "Render the three sheets of planner.xlsx to PNG, 1200 px wide."


def workshop(settings: Settings, files: list[str] | None = None) -> dict[str, Any]:
    return prompts.workshop_request(settings, TASK, files or [])


def llm_calls(db: Database) -> list[dict[str, Any]]:
    with db.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id")]


def cycle_row(db: Database, cycle_id: int) -> dict[str, Any]:
    with db.connection() as conn:
        return dict(conn.execute("SELECT status, note FROM cycles WHERE id = ?", (cycle_id,)).fetchone())


# --- FIX 1: a workshop call is reserved honestly, and priced per sampling ---


def test_a_run_like_423_cant_take_the_day_past_its_cap(data_dir: Path) -> None:
    # Default caps with $0.40 of the day left: 0.13.0 admitted the run on its $0.35 quote, and a run like #423 then
    # took the day to $2.94 against a $1.50 cap. Now a workshop call holds at least its cap per run.
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=0.4, cycle_spend_cap_usd=0.4)
    economy = make_economy(data_dir, settings)
    transport = FakeTransport(script=[Overrun()])
    model = economy.metered(transport)
    cycle = model.open_cycle("test")
    assert model.quote(workshop(settings), WORKSHOP) <= settings.workshop_run_cap_usd * 1_000_000
    assert model.reservation(workshop(settings), WORKSHOP) == 750_000  # the default cap per run
    with pytest.raises(CallRefused, match="daily cap"):
        model.call(cycle, WORKSHOP, workshop(settings))
    assert not transport.trace  # refused before anything was sent


def test_a_workshop_call_holds_the_tail_of_recent_runs(data_dir: Path) -> None:
    clock = FakeClock()
    economy = make_economy(data_dir, OWNER, clock=clock)
    model = economy.metered(FakeTransport(script=[Overrun()]))  # 0.14.0: the fake can overrun like #423
    cycle = model.open_cycle("test")
    assert model.reservation(workshop(OWNER), WORKSHOP) == 1_500_000  # nothing seen yet: the cap per run
    result = model.call(cycle, WORKSHOP, workshop(OWNER))
    assert result.overrun and result.cost_micros > 1_700_000  # about #423's $1.84
    [row] = llm_calls(economy.db)
    assert (row["estimate_micros"], row["overrun"], row["iterations"]) == (1_500_000, 1, MAX_SERVER_ITERATIONS)
    # 1.5 times the costliest recent run (or the raised estimate, whichever is more) is held from now on
    held = model.reservation(workshop(OWNER), WORKSHOP)
    tail = -(-result.cost_micros * 3 // 2)
    assert held == max(model.quote(workshop(OWNER), WORKSHOP), tail) == tail
    model.close_cycle(cycle)
    clock.advance(days=15)  # the tail forgets after 14 days (and the factor has faded meanwhile)
    model.open_cycle("test")
    assert model.reservation(workshop(OWNER), WORKSHOP) < held


def test_uncertain_workshop_calls_dont_raise_the_next_hold(data_dir: Path) -> None:
    # Review of 0.14.0: an uncertain answer was booked at its hold, and the hold then grew from what calls were booked
    # at: $0.07 of cheap runs was charged as $12.19, and the workshop locked itself.
    economy = make_economy(data_dir, OWNER)
    odd = [Completed(message(1_000, 200, unknown_field=5)) for _ in range(4)]
    model, _ = metered(economy, ScriptedTransport(outcomes=[*odd, Completed(message(0, 0))]))
    cycle = model.open_cycle("test")
    quote = model.quote(workshop(OWNER), WORKSHOP)
    for _ in range(4):
        assert model.reservation(workshop(OWNER), WORKSHOP) == 1_500_000  # the cap per run, every time
        result = model.call(cycle, WORKSHOP, workshop(OWNER))
        assert result.cost_micros == quote < 1_500_000  # charged at its worst case, as the docs say
    model.call(cycle, WORKSHOP, workshop(OWNER))  # an answer without its usage: its bill is anyone's guess
    assert [(r["billing_uncertain"], r["cost_micros"]) for r in llm_calls(economy.db)][-1] == (1, 1_500_000)
    assert model.reservation(workshop(OWNER), WORKSHOP) == 1_500_000


def test_output_is_priced_per_sampling_of_a_server_tools_loop() -> None:
    # Live #369 wrote 12,439 output tokens with max_tokens 8,000: max_tokens limits each sampling, not the loop.
    body = workshop(Settings())
    price = Settings().price_for(MODEL)
    assert price is not None
    worst = worst_case_micros(plan_request(body, input_tokens=5_000), price, 10)
    assert worst > MAX_SERVER_ITERATIONS * body["max_tokens"] * price.output  # every sampling writes its max_tokens
    plain = {**body, "tools": []}  # a token-only call samples once
    assert worst_case_micros(plan_request(plain, input_tokens=5_000), price, 10) < 2 * body["max_tokens"] * price.output


def test_iterations_are_kept_and_only_samplings_count(data_dir: Path) -> None:
    economy = make_economy(data_dir, OWNER)
    usage = {
        "iterations": [
            {"type": "message", "input_tokens": 600, "output_tokens": 100},
            {"type": "message", "input_tokens": 400, "output_tokens": 100},
        ]
    }
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 200, **usage))]))
    cycle = model.open_cycle("test")
    search = {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
    model.call(cycle, "research", request(tools=[search]))
    assert llm_calls(economy.db)[0]["iterations"] == 2
    model.call(cycle, "work", request())  # a plain call says nothing: NULL
    assert llm_calls(economy.db)[1]["iterations"] is None


# --- FIX 1c and FIX 26b: counting ---


def _anthropic(handler: Any) -> Any:
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("anthropic")
    from app.economy.anthropic_transport import AnthropicTransport

    return AnthropicTransport("sk-ant-test", None, http_transport=httpx2.MockTransport(handler)), httpx2


def test_a_workshop_request_with_files_is_counted(caplog: pytest.LogCaptureFixture) -> None:
    counted: list[dict[str, Any]] = []

    def handler(req: Any) -> Any:
        body = json.loads(req.content)
        counted.append(body)
        blocks = [b for m in body["messages"] if isinstance(m["content"], list) for b in m["content"]]
        if any(b.get("type") == "container_upload" for b in blocks):  # as the API answered live
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error"}})
        return httpx2.Response(200, json={"input_tokens": 1_000})

    transport, httpx2 = _anthropic(handler)
    with caplog.at_level(logging.WARNING):
        tokens = transport.count_tokens(workshop(Settings(), ["file_a", "file_b"]))
    assert "Token counting failed" not in caplog.text
    assert tokens == 1_050 + 200 + 3_000 + 2 * 200  # the code tool's and each file's allowance
    assert len(counted) == 1


def test_a_work_step_counts_each_request_once() -> None:
    counts: list[Any] = []

    def handler(req: Any) -> Any:
        counts.append(json.loads(req.content))
        return httpx2.Response(200, json={"input_tokens": 1_000})

    transport, httpx2 = _anthropic(handler)
    work = request()
    reflect = request(max_tokens=2_000, system="Reflect.")
    for _ in range(3):  # what a step's affordability check and its call count (8 requests in 0.13.0)
        transport.count_tokens(work)
        transport.count_tokens(reflect)
    assert len(counts) == 2
    assert transport.count_tokens({**work, "max_tokens": 5}) == transport.count_tokens(work)  # not counted
    assert len(counts) == 2
    transport.count_tokens({**work, "messages": [{"role": "user", "content": "other"}]})
    assert len(counts) == 3


def test_a_work_step_through_the_guard_sends_two_counts(data_dir: Path) -> None:
    economy = make_economy(data_dir, OWNER)
    counts: list[Any] = []

    def handler(req: Any) -> Any:
        if req.url.path.endswith("/count_tokens"):
            counts.append(req)
            return httpx2.Response(200, json={"input_tokens": 1_000})
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error"}})

    transport, httpx2 = _anthropic(handler)
    transport.simulated = True  # the dry-run economy of the tests
    model = economy.metered(transport)
    cycle = model.open_cycle("test")
    work, reflect = request(), request(max_tokens=2_000, system="Reflect.")
    # As Loop._affordable sizes a step, and the call that follows
    model.quote(work, "work")
    model.expected(work, "work", cycle)
    model.quote(reflect, "reflect", extra_tokens=500)
    cached = model.prompt_tokens(work)
    model.expected(reflect, "reflect", cycle, extra_tokens=500, cached=cached)
    model.affordable(reflect, "reflect", cycle)
    model.reserve(cycle, "work", work)
    assert len(counts) == 2


# --- FIX 2a: an overrun that doesn't stop the cycle, and a reflection that is never refused for one ---


def test_a_workshop_overrun_keeps_the_cycle_running(data_dir: Path) -> None:
    # Live #444: $0.3466 against $0.3441 (0.7%) stopped cycle #46 mid-plan, and its reflection with it. A workshop call
    # doesn't count toward the cycle cap, so however much it overran, the cycle goes on.
    economy = make_economy(data_dir, OWNER)
    model = economy.metered(FakeTransport(script=[Overrun()]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, WORKSHOP, workshop(OWNER))
    assert result.overrun
    assert cycle_row(economy.db, cycle) == {"status": "running", "note": None}
    with pytest.raises(CallRefused, match="no more workshop calls") as refused:
        model.call(cycle, WORKSHOP, workshop(OWNER))
    assert refused.value.category == "cap"
    model.call(cycle, "work", request())  # the rest of the plan goes on
    model.call(cycle, "reflect", request())
    assert model.close_cycle(cycle)
    events = [e["message"] for e in economy.db.recent_events(limit=10)]
    assert any("The wake cycle goes on without further workshop calls" in m for m in events)
    assert safety_factor(economy.db, MODEL, "dry_run", WORKSHOP) > 1


def test_a_small_overrun_of_a_work_call_ends_only_the_work(data_dir: Path) -> None:
    economy = make_economy(data_dir, OWNER)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 1_100))]))
    cycle = model.open_cycle("test")
    result = model.call(cycle, "work", request())
    assert result.overrun and result.cost_micros == 13_000  # 8% ($0.001) over 12,000
    assert cycle_row(economy.db, cycle)["status"] == "running"
    with pytest.raises(CallRefused, match="no more work calls"):
        model.call(cycle, "work", request())
    assert not model.call(cycle, "reflect", request()).overrun


def test_a_large_overrun_stops_the_cycle_but_never_its_reflection(data_dir: Path) -> None:
    economy = make_economy(data_dir, OWNER)
    model, _ = metered(economy, ScriptedTransport(outcomes=[Completed(message(1_000, 5_000))]))
    cycle = model.open_cycle("test")
    assert model.call(cycle, "work", request()).overrun  # 52,000 against 12,000
    assert cycle_row(economy.db, cycle) == {"status": "stopped", "note": OVERRUN_STOP}
    with pytest.raises(CallRefused, match="was stopped") as refused:
        model.call(cycle, "research", request())
    assert refused.value.category == "cap"  # the loop ends the work and still reflects
    assert model.affordable(request(), "reflect", cycle)[0]
    model.call(cycle, "reflect", request())
    assert not model.close_cycle(cycle)  # it had ended already
    assert cycle_row(economy.db, cycle)["status"] == "stopped"


def test_a_cycle_an_overrun_stopped_reflects_and_sleeps_as_chosen(data_dir: Path) -> None:
    big = tools(("workspace_write", {"path": "notes/a.md", "mode": "create", "content": "a"}))
    big.response["usage"]["output_tokens"] = 60_000  # far more than max_tokens: the guard stops the cycle
    reflection = tools(
        ("write_journal", {"summary": "Stopped by an overrun", "entry": "The poster is next."}),
        ("set_sleep", {"minutes": 420, "reason": "the poster waits for the owner"}),
    )
    agent, _ = make_agent(data_dir, [plan(sleep=420), big, reflection])
    end = agent.run_cycle("schedule")
    assert end.status == "stopped"
    assert [r["purpose"] for r in rows(agent, "SELECT purpose FROM llm_calls WHERE status = 'ok' ORDER BY id")] == [
        "plan",
        "work",
        "reflect",
    ]
    assert rows(agent, "SELECT author, summary FROM journal") == [
        {"author": "agent", "summary": "Stopped by an overrun"}
    ]
    # 0.13.0 backed off 30 minutes instead of the 420 the agent chose, which only brought the next paid cycle sooner
    assert agent._meta_time("next_wake_at") == agent.clock.now() + timedelta(minutes=420)


def test_after_an_overrun_stop_a_waiting_request_still_cuts_the_sleep(data_dir: Path) -> None:
    # Review of 0.14.0: the chosen sleep after an overrun skipped what a completed cycle's gets (the cut while a
    # request waits, a milestone's check, maintenance's one cycle a day)
    big = tools(("workspace_write", {"path": "notes/a.md", "mode": "create", "content": "a"}))
    big.response["usage"]["output_tokens"] = 60_000
    reflection = tools(
        ("write_journal", {"summary": "Stopped by an overrun", "entry": "Waiting for my owner."}),
        ("set_sleep", {"minutes": 720, "reason": "the poster waits for the owner"}),
    )
    script = [plan(steps=["ask to publish", "notes"], sleep=720), tools(("request_approval", APPROVAL)), text("Asked.")]
    agent, _ = make_agent(data_dir, [*script, big, reflection])
    assert agent.run_cycle("schedule").status == "stopped"
    assert rows(agent, "SELECT status FROM approvals") == [{"status": "pending"}]
    assert agent._meta_time("next_wake_at") == agent.clock.now() + timedelta(minutes=240)
    assert agent.agent_fields()["next_wake_reason"].endswith(
        "cut to 240 min: 1 request waits for your decision, and it works on something else meanwhile (the cycle was"
        " stopped after a call cost more than its worst case)"
    )


# --- X4: the safety factor covers what was missed and comes down again ---


def test_the_factor_covers_a_5x_miss(data_dir: Path) -> None:
    db = make_economy(data_dir).db
    # #423: $1.8376 against $0.3482 needed 5.81; 0.13.0 capped the factor at 4
    assert pricing.raise_safety_factor(db, MODEL, 1_837_601, 348_191, "live", WORKSHOP) == Decimal("5.81")
    assert pricing.raise_safety_factor(db, MODEL, 100_000_000, 1_000, "live", "research") == pricing.MAX_SAFETY_FACTOR


def test_a_rare_purposes_factor_comes_down_after_a_few_accurate_calls(data_dir: Path) -> None:
    db = make_economy(data_dir).db
    pricing.raise_safety_factor(db, MODEL, 1_837_601, 348_191, "live", WORKSHOP)
    lowered = [pricing.note_accurate_call(db, MODEL, "live", WORKSHOP) for _ in range(3)]
    assert lowered == [None, None, Decimal("3.41")]  # half of 4.81 comes off; 0.13.0 needed 25 calls for 0.05
    for _ in range(9):
        pricing.note_accurate_call(db, MODEL, "live", WORKSHOP)
    assert safety_factor(db, MODEL, "live", WORKSHOP) < Decimal("1.4")
    assert safety_factor(db, MODEL, "live", "work") == 1  # per purpose


def test_a_factor_that_refuses_every_call_fades_with_time(data_dir: Path) -> None:
    clock = FakeClock()
    economy = make_economy(data_dir, clock=clock)
    pricing.raise_safety_factor(economy.db, MODEL, 1_837_601, 348_191, "dry_run", WORKSHOP, clock.today())
    model = economy.metered(FakeTransport())
    clock.advance(days=6)
    model.close_cycle(model.open_cycle("test"))
    assert safety_factor(economy.db, MODEL, "dry_run", WORKSHOP) == Decimal("5.81")
    clock.advance(days=1)
    model.close_cycle(model.open_cycle("test"))
    assert safety_factor(economy.db, MODEL, "dry_run", WORKSHOP) == Decimal("3.41")
    events = [e["message"] for e in economy.db.recent_events(limit=5)]
    assert any("now scaled by 3.41: 7 days without a change" in m for m in events)


def test_a_raised_factor_doesnt_lock_the_workshop_at_its_cap(data_dir: Path) -> None:
    # After #423 the factor went to 4, every run was quoted above the $0.50 cap, and only Reset opened the workshop.
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=7, cycle_spend_cap_usd=1)
    clock = FakeClock()
    economy = make_economy(data_dir, settings, clock=clock)
    model = economy.metered(FakeTransport(script=[Overrun()]))
    first = model.open_cycle("test")
    model.call(first, WORKSHOP, workshop(settings))
    model.close_cycle(first)
    assert safety_factor(economy.db, MODEL, "dry_run", WORKSHOP) > 2
    clock.advance(days=1)  # a new day's cap
    cycle = model.open_cycle("test")
    held = model.reservation(workshop(settings), WORKSHOP)
    assert model.quote(workshop(settings), WORKSHOP, scaled=False) <= 750_000 < held
    result = model.call(cycle, WORKSHOP, workshop(settings))  # admitted: it holds more of the day instead
    assert not result.overrun and llm_calls(economy.db)[-1]["estimate_micros"] == held


def test_the_owners_reset_clears_the_factor_and_the_tail(data_dir: Path) -> None:
    economy = make_economy(data_dir, OWNER)
    model = economy.metered(FakeTransport(script=[Overrun()]))
    model.call(model.open_cycle("test"), WORKSHOP, workshop(OWNER))
    assert model.reservation(workshop(OWNER), WORKSHOP) > 2_000_000
    assert pricing.reset_safety_factors(economy.db, "dry_run") == 1
    assert model.reservation(workshop(OWNER), WORKSHOP) == 1_500_000


def test_the_owner_hears_when_what_a_run_holds_is_more_than_the_day(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=2, cycle_spend_cap_usd=1)
    economy = make_economy(data_dir, settings)
    assert not any("workshop" in w for w in economy.warnings())
    model = economy.metered(FakeTransport(script=[Overrun()]))
    model.call(model.open_cycle("test"), WORKSHOP, workshop(settings))
    [warning] = [w for w in economy.warnings() if "workshop" in w]
    assert "more than the daily spend cap ($2.00), so the workshop can't run" in warning
    day = (economy.clock.now() + timedelta(days=14)).astimezone(economy.clock.tz).date().isoformat()
    assert warning.endswith(f"What recent runs cost stops counting on {day}, or at once with Reset estimates.")


# --- the migration ---


def test_the_migration_keeps_the_calls_and_their_guard(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    ours = [m for m in discover_migrations() if m.name == "money_guard"]
    assert len(ours) == 1
    migrate(db_file, [m for m in discover_migrations() if m.version < ours[0].version], backup_dir=tmp_path / "b")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', 'then', 'stopped', 'schedule', 0, 1000000)"
        )
        insert = (
            "INSERT INTO llm_calls (id, boot_id, cycle_id, purpose, model, simulated, status, ts, local_day,"
            " cost_micros, estimate_micros) VALUES (?, 'b', 1, 'workshop', 'm', 0, ?, 'then', '2026-09-30', ?, ?)"
        )
        conn.execute(insert, (423, "ok", 1_837_601, 348_191))
        conn.execute(insert, (424, "pending", 0, 348_191))
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "b") == [ours[0].version]
    db = Database(db_file)
    assert [(r["id"], r["iterations"], r["overrun"]) for r in llm_calls(db)] == [(423, None, 0), (424, None, 0)]
    with db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="finalized call cannot change"):
        conn.execute("UPDATE llm_calls SET overrun = 1 WHERE id = 423")
    with db.transaction() as conn:
        conn.execute("UPDATE llm_calls SET status = 'ok', iterations = 10, overrun = 1 WHERE id = 424")
    for bad in ("iterations = -1", "overrun = 2"):
        with db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day,"
                " cost_micros) VALUES ('b', 1, 'work', 'm', 0, 'pending', 'then', '2026-09-30', 0)"
            )
            conn.execute(f"UPDATE llm_calls SET {bad} WHERE status = 'pending'")
