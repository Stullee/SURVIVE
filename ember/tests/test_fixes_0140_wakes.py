"""0.14.0: wake-ups behind the guards (the analysis of 0.13.0, FIX NOW 11, 25 and 26, X7, X12 and X13).

Event wake-ups ran before the no-room, crash-loop and back-off guards and ignored the owner's wake settings; an order, a
listing's favorites or a milestone Ember's code checks itself woke a paid cycle. Wake now while the last will was due
ran it back to back (three failures in a minute, or a refused cycle every round until midnight). The owner's clicks
each started a full cycle (two approvals 83 seconds apart: $2.57 in 8 minutes). Maintenance's one cycle a day held
only after a completed cycle. A waiting request cut the sleep to 60 minutes at the owner's options, not the 240 the
notes said. And on the days the clocks change, the evening reserve ended at 19:00 or 21:00, and in autumn the scheduler
decided every second for an hour."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app import web
from app.agent import agenda, loop, roadmap, service
from app.agent.loop import CycleEnd
from app.agent.scheduler import ROUND_SECONDS, Scheduler
from app.agent.service import Agent, Decision
from app.config import LoadedSettings, Settings
from app.economy import burn, metering
from app.economy.clock import from_iso, to_iso
from tests.economy_helpers import FakeClock, ScriptedTransport, make_economy
from tests.test_agent import ROOMY, make_agent, plan, reply, rows, text
from tests.test_autonomy import request_for, send
from tests.test_decision_wakes import asks_and_sleeps
from tests.test_owner_loop import owner

BERLIN = ZoneInfo("Europe/Berlin")


def due_today(agent: Agent, title: str = "Five listings live", **columns: Any) -> int:
    """A milestone whose last day is today (noted from 08:00, the owner's time)."""
    scope = agent.scope()
    with agent.db.transaction() as conn:
        return roadmap.create(
            conn,
            scope,
            title=title,
            measure="x",
            due=agent.clock.today().isoformat(),
            now=to_iso(agent.clock.now()),
            **columns,
        )


def urgent_milestone(agent: Agent) -> None:
    """An urgent event in the agenda: the last day of one of the agent's own milestones."""
    agent.check_events()  # the agenda begins
    due_today(agent)
    agent.check_events()
    assert "milestone_due" in [r["kind"] for r in rows(agent, "SELECT kind FROM agenda WHERE urgent = 1")]


# --- FIX NOW 11: event wake-ups behind the guards, and the owner's switch ---


def test_an_event_waits_while_the_agent_backs_off_after_a_failed_cycle(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [text("not json"), plan(steps=[], sleep=600)])
    assert agent.run_cycle("schedule").status == "failed"
    backoff = agent._meta_time("next_wake_at")
    assert backoff == agent.clock.now() + timedelta(minutes=30)
    urgent_milestone(agent)
    decision = agent.decide()
    assert not decision.run and decision.reason == "after a failed cycle, backing off"
    agent.clock.advance(minutes=31)  # the back-off has passed: the event wakes it before the schedule
    assert agent.decide().trigger == "event"


def test_an_event_wakes_once_the_back_off_has_passed_while_the_reserve_holds_the_schedule(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = make_agent(data_dir, [text("not json"), plan(steps=[], sleep=600)])
    assert agent.run_cycle("schedule").status == "failed"
    agent.clock.advance(minutes=31)
    daily = metering.usd_cap_to_micros(agent.settings.daily_spend_cap_usd)
    held = int(daily * metering.EVENT_RESERVE_SHARE)
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, today: daily - held // 2 - 150_000)
    decision = agent.decide()  # the schedule waits for the evening: the rest of the cap is the events'
    assert not decision.run and decision.reason.startswith("The rest of the daily cap is kept for event wake-ups")
    urgent_milestone(agent)
    assert agent.decide().trigger == "event"  # what the reserve is kept for; the back-off is over


def test_in_maintenance_an_event_wakes_once_the_back_off_has_passed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.MAINTENANCE)
    agent, _ = make_agent(data_dir, [text("not json"), plan(steps=[], sleep=600)])
    assert agent.run_cycle("schedule").status == "failed"
    agent.clock.advance(minutes=10)
    urgent_milestone(agent)
    decision = agent.decide()  # backing off, and the next scheduled cycle waits a day
    assert not decision.run and decision.reason == "the burn mode is maintenance: one cycle a day"
    agent.clock.advance(minutes=21)
    assert agent.decide().trigger == "event"


def test_an_event_waits_during_a_crash_loop(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    with agent.db.connection() as conn:
        life = conn.execute("SELECT id FROM lives").fetchone()[0]
        for _ in range(service.CRASH_LOOP):
            conn.execute(
                "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros,"
                " session) VALUES (?, 'b', '2026-09-01T11:00:00Z', '2026-09-01T11:01:00Z', 'interrupted', 't', 1, 0,"
                " ?)",
                (life, agent.economy.life.session()),
            )
    urgent_milestone(agent)
    assert agent.decide().trigger is None  # no event wake after three interrupted cycles
    agent.clock.advance(minutes=3)
    decision = agent.decide()
    assert not decision.run and "interrupted" in decision.reason


def test_an_event_waits_when_the_cycle_cap_leaves_no_room_for_work(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=0.04, venture_share=0)
    agent, _ = make_agent(data_dir, [plan()], settings)
    assert agent.run_cycle("schedule").note == loop.NO_STEP
    urgent_milestone(agent)
    decision = agent.decide()
    assert not decision.run and decision.reason.startswith("The last cycle had no money left for a work step")


def test_the_owner_can_switch_event_wake_ups_off(data_dir: Path) -> None:
    assert Settings().wake_on_events is True
    off = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1, wake_on_events=False)
    agent, _ = make_agent(data_dir, [], off)
    agent.decide()  # the first wake-up, in two minutes
    urgent_milestone(agent)  # still noted for the next plan
    assert agent.decide().trigger is None
    agent.clock.advance(minutes=3)
    assert agent.decide().trigger == "schedule"
    assert rows(agent, "SELECT woke_at FROM agenda WHERE kind = 'milestone_due'") == [{"woke_at": None}]


def test_orders_favorites_and_the_milestones_code_checks_are_noted_without_a_wake(data_dir: Path) -> None:
    from tests.test_agenda import sold
    from tests.test_etsy import listed

    agent, listing = listed(data_dir)
    agent.check_events()
    agent.clock.advance(minutes=10)
    sold(agent, 71)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET favorites = 6 WHERE listing_id = ?", (listing,))
    metric = due_today(agent, "Three listings live", metric="listings_live", target=3)
    own = due_today(agent, "Ask two shops")
    goal = due_today(agent, "Earn what you spend", created_by="code", kind="money_goal")
    decision = due_today(agent, "Decision point: go on, change or stop", created_by="code", kind="decision")
    agent.check_events()
    noted = {(r["kind"], r["key"].split(":")[0]): r["urgent"] for r in rows(agent, "SELECT * FROM agenda")}
    assert noted[("order", "71")] == 0
    assert noted[("favorites", str(listing))] == 0
    assert noted[("milestone_due", str(metric))] == 0  # Ember's code grades it: bookkeeping, no wake
    assert noted[("milestone_due", str(goal))] == 0  # the money goal too
    assert noted[("milestone_due", str(decision))] == 1  # a decision point is the agent's to make
    assert noted[("milestone_due", str(own))] == 1  # the agent's own milestone: its last day wakes it
    with agent.db.connection() as conn:
        waking = [int(r["key"].split(":")[0]) for r in agenda.waking(conn, agent.scope())]
    assert waking == [own, decision]


# --- FIX NOW 25: Wake now runs the last will once, and the scheduler waits a round after a refusal ---


def test_wake_now_runs_a_failing_last_will_once_not_three_times_in_a_minute(data_dir: Path) -> None:
    settings = Settings(starting_balance_usd=0.03, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    agent, _ = make_agent(data_dir, [reply([], "end_turn")] * 3, settings)
    assert agent.run_cycle("schedule").status == "refused"  # starves: the will is due
    assert agent.decide().trigger == "last_will"
    assert agent.run_cycle("last_will").status == "failed"
    assert agent.request_wake()[0] == 202  # the owner presses Wake now
    assert agent.decide().trigger == "last_will"
    assert agent.run_cycle("last_will").status == "failed" and not agent.wake_requested
    decision = agent.decide()  # the will's retry time stands
    assert not decision.run and decision.reason == "The last will is due; retrying later"
    assert agent.db.get_meta(agent._key("will_given_up")) is None


def test_a_refused_last_will_after_wake_now_waits_for_its_retry(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(starting_balance_usd=0.03, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)
    agent, _ = make_agent(data_dir, [], settings)
    assert agent.run_cycle("schedule").status == "refused"
    monkeypatch.setattr(loop.CycleRunner, "_last_will", lambda self, cycle_id: CycleEnd("refused", "last will refused"))
    assert agent.request_wake()[0] == 202
    assert agent.decide().trigger == "last_will"
    assert agent.run_cycle("last_will").status == "refused"
    for _ in range(3):  # no refused cycle every round until midnight
        decision = agent.decide()
        assert not decision.run and decision.wait_until is not None and decision.wait_until > agent.clock.now()
    assert rows(agent, "SELECT COUNT(*) AS n FROM cycles WHERE trigger = 'last_will'") == [{"n": 1}]


def scheduled_cycles(end: CycleEnd, seconds: float = 0.5) -> int:
    """How many cycles a scheduler starts in ``seconds`` when every decision says run and every cycle ends ``end``."""
    runs: list[str] = []

    async def main() -> None:
        agent = SimpleNamespace(
            running_cycle=False,
            stop=threading.Event(),
            run_policy=lambda: None,
            execute_approved=lambda: None,
            sync_shop=lambda: None,
            check_events=lambda: None,
            decide=lambda: Decision(True, "last_will", "the last will is due"),
            run_cycle=lambda trigger: runs.append(trigger) or end,
        )
        economy = SimpleNamespace(tick=lambda: None, clock=SimpleNamespace(now=lambda: datetime.now(UTC)))
        db = SimpleNamespace(prune_events=lambda keep: None)
        scheduler = Scheduler(db, economy, agent)  # type: ignore[arg-type]
        scheduler.start()
        await asyncio.sleep(seconds)
        await scheduler.stop()

    asyncio.run(main())
    return len(runs)


def test_the_scheduler_waits_a_round_after_a_refused_or_failed_cycle() -> None:
    assert ROUND_SECONDS >= 30
    assert scheduled_cycles(CycleEnd("refused", "last will refused")) == 1
    assert scheduled_cycles(CycleEnd("failed", "planning failed")) == 1
    assert scheduled_cycles(CycleEnd("completed")) > 1  # a finished cycle: decide again at once, as before
    assert scheduled_cycles(CycleEnd("refused", "starved", rerun=True)) > 1  # its last will is due now


# --- X7: the owner's messages and decisions wake one cycle for all of them ---


def test_the_owners_clicks_wake_one_cycle_after_a_quiet_period(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [*asks_and_sleeps(720), plan(steps=[], sleep=720)])
    assert agent.run_cycle("schedule").status == "completed"
    request, pokes = request_for(agent, Settings())
    first = agent.clock.now()
    send(agent, "I looked at the guide.")
    assert web._wake_for_message(request) == "soon" and not agent.wake_requested and pokes == ["x"]
    decision = agent.decide()
    assert not decision.run and decision.wait_until == first + service.OWNER_QUIET
    agent.clock.advance(seconds=83)  # an approval 83 seconds later: the same cycle
    decided = owner(agent).decide(1, {"decision": "approve", "expected_version": 0}, "Stefan")
    web._wake_for_decision(request, decided)
    assert decided.body["wake"] == "soon" and not agent.wake_requested
    decision = agent.decide()
    assert not decision.run and decision.wait_until == first + timedelta(seconds=83) + service.OWNER_QUIET
    assert agent.agent_fields()["next_wake_at"] == to_iso(decision.wait_until)
    agent.clock.advance(minutes=service.OWNER_QUIET.total_seconds() / 60)
    assert agent.decide().trigger == "owner"
    assert agent.run_cycle("owner").status == "idle"
    planner = transport.sent[-1]["messages"][0]["content"][0]["text"]
    assert "I looked at the guide." in planner
    assert not agent.decide().run  # both seen: no second cycle
    woke = [e["message"] for e in agent.db.recent_events(30) if e["message"].startswith("The owner")]
    assert woke == ["The owner's message woke the agent"]
    assert rows(agent, "SELECT COUNT(*) AS n FROM cycles WHERE trigger = 'owner'") == [{"n": 1}]


def test_wake_now_stays_immediate(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)])
    request, _ = request_for(agent, Settings())
    send(agent, "Hello!")
    assert web._wake_for_message(request) == "soon"
    assert agent.request_wake()[0] == 202  # the owner presses Wake now: no quiet period
    assert agent.decide().trigger == "owner"
    assert agent.run_cycle("owner").status == "idle"
    assert not agent.decide().run and not agent.message_waiting  # it read the message: nothing left to wake for


def test_a_click_during_a_cycle_that_saw_it_starts_no_cycle_after_it(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [plan(steps=[], sleep=600)])
    request, _ = request_for(agent, Settings())
    send(agent, "Quick question.")
    agent.running_cycle = True  # the message came in after the cycle started, but before it planned
    assert web._wake_for_message(request) == "after_cycle"
    agent.running_cycle = False
    assert agent.run_cycle("schedule").status == "idle"  # it planned with the message
    agent.clock.advance(minutes=10)
    assert not agent.decide().run and not agent.message_waiting
    assert len(transport.sent) == 1


def test_a_message_while_dormant_promises_no_wake(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.DORMANT)
    agent, _ = make_agent(data_dir, [])
    request, pokes = request_for(agent, Settings())
    send(agent, "Money is coming.")
    assert web._wake_for_message(request) is None and not agent.message_waiting and pokes == []
    agent.clock.advance(minutes=10)
    assert agent.decide().reason.startswith("Dormant: ")  # only Wake now runs a cycle


# --- X12: maintenance's one cycle a day, after a failed cycle too ---


def test_maintenance_keeps_one_cycle_a_day_after_a_failed_cycle(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(burn, "_raw", lambda status: burn.MAINTENANCE)
    agent, _ = make_agent(data_dir, [text("not json"), plan(steps=[], sleep=60)])
    started = agent.clock.now()
    assert agent.run_cycle("schedule").status == "failed"
    agent.clock.advance(minutes=31)  # the back-off alone would wake it now
    decision = agent.decide()
    assert not decision.run and decision.wait_until == started + timedelta(days=1)
    assert decision.reason == "the burn mode is maintenance: one cycle a day"
    assert agent.agent_fields()["next_wake_at"] == to_iso(started + timedelta(days=1))
    agent.clock.advance(days=1)
    assert agent.decide().trigger == "schedule"


# --- X13: a waiting request cuts the sleep to four hours, as the notes say ---


@pytest.mark.parametrize(("interval", "chosen", "slept"), [(60, 720, 240), (300, 720, 300), (60, 180, 180)])
def test_a_waiting_request_cuts_the_sleep_to_four_hours_at_a_short_default_sleep(
    data_dir: Path, interval: int, chosen: int, slept: int
) -> None:
    settings = ROOMY.model_copy(update={"min_sleep_minutes": 30, "wake_interval_minutes": interval})
    agent, _ = make_agent(data_dir, asks_and_sleeps(chosen), settings)
    started = agent.clock.now()
    assert agent.run_cycle("schedule").status == "completed"
    assert agent._meta_time("next_wake_at") == started + timedelta(minutes=slept)
    reason = agent.agent_fields()["next_wake_reason"]
    assert reason.startswith(f"Ember chose {chosen} min")
    assert (f"; cut to {slept} min: 1 request waits for your decision" in reason) is (slept < chosen)


# --- FIX NOW 26a: the owner's wall clock on the days the clocks change ---


def berlin_agent(data_dir: Path, local: datetime, settings: Settings = ROOMY) -> Agent:
    clock = FakeClock(local.replace(tzinfo=BERLIN).astimezone(UTC), tz=BERLIN)
    economy = make_economy(data_dir, settings, clock=clock)
    agent = Agent(economy.db, LoadedSettings(settings), economy, transport=ScriptedTransport(), cycles_enabled=True)
    agent.recover()
    return agent


@pytest.mark.parametrize(
    ("day", "evening_utc"), [("2026-03-29", "2026-03-29T18:00:00Z"), ("2026-10-25", "2026-10-25T19:00:00Z")]
)
def test_the_event_reserve_ends_at_20_00_on_the_days_the_clocks_change(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, day: str, evening_utc: str
) -> None:
    agent = berlin_agent(data_dir, datetime.fromisoformat(f"{day}T19:30:00"))
    settings, clock = agent.settings, agent.clock
    assert metering.event_reserve(settings, clock, "schedule") > 0  # 19:30 local: the reserve still holds
    daily = metering.usd_cap_to_micros(settings.daily_spend_cap_usd)
    held = int(daily * metering.EVENT_RESERVE_SHARE)
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, today: daily - held // 2 - 150_000)
    agent._set_time("next_wake_at", clock.now() - timedelta(minutes=1))
    decision = agent.decide()
    assert not decision.run and decision.reason.startswith("The rest of the daily cap is kept for event wake-ups")
    assert decision.wait_until == from_iso(evening_utc)  # 20:00 in Berlin
    assert decision.wait_until > clock.now()  # never a moment already past: no deciding every second


@pytest.mark.parametrize(
    ("day", "morning_utc"), [("2026-03-29", "2026-03-29T06:00:00Z"), ("2026-10-25", "2026-10-25T07:00:00Z")]
)
def test_a_milestones_check_wakes_at_08_00_on_the_days_the_clocks_change(
    data_dir: Path, day: str, morning_utc: str
) -> None:
    agent = berlin_agent(data_dir, datetime.fromisoformat(f"{day}T00:30:00"))
    milestone = due_today(agent, "Hear back from the shop")
    with agent.db.transaction() as conn:
        conn.execute("UPDATE milestones SET wait_for = 'an answer', check_at = ? WHERE id = ?", (day, milestone))
    assert agent._next_check(agent.clock.now()) == (from_iso(morning_utc), milestone)


def test_the_reserve_and_the_check_are_unchanged_on_an_ordinary_day(data_dir: Path) -> None:
    agent = berlin_agent(data_dir, datetime.fromisoformat("2026-10-24T10:00:00"))
    assert agent.clock.at(agent.clock.today(), 20) == from_iso("2026-10-24T18:00:00Z")
    assert agent.clock.at(agent.clock.today(), 8) == from_iso("2026-10-24T06:00:00Z")
