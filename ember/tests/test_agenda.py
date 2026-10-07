"""0.13.0: an agenda with event wake-ups. Only the timer and the owner's messages and decisions woke the agent: a sale,
a reply to Ember's email or a milestone's last day waited for the next scheduled cycle, up to the longest sleep. Now
Ember's code notes such events between cycles, the next plan lists them, and an urgent one wakes the agent for a lean
reactive cycle: at most 4 a day, 30 minutes apart. Until 20:00 a fifth of the daily cap is kept for them."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import agenda, roadmap  # noqa: E402
from app.agent.fake_llm import request_kind  # noqa: E402
from app.economy import metering  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402
from tests.test_etsy_revenue import order, shop_with  # noqa: E402


def sold(agent: Any, *receipts: int) -> None:
    """Orders of the fake shop's listing, read by a sync."""
    shop_with(agent, [order(agent, receipt) for receipt in receipts])
    assert agent.publisher.sync(force=True) is None


def replied(agent: Any, uid: int) -> None:
    """A reply to the email Ember sent (sent first), come in now: urgent (0.15.0: an order no longer wakes it)."""
    scope = agent.scope()
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM emails WHERE direction = 'out'").fetchone():
            approval = conn.execute("SELECT id FROM approvals ORDER BY id LIMIT 1").fetchone()["id"]
            conn.execute(
                "INSERT INTO emails (mode, session, life_id, direction, message_id, from_addr, to_addr, subject,"
                " sent_at, received_at, body, approval_id) VALUES (?, ?, ?, 'out', '<m1@ember>', 'ember@example.org',"
                " 'ann@example.org', 'Your planner', ?, ?, 'Hello', ?)",
                (scope.mode, scope.session, scope.life_id, now, now, approval),
            )
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, in_reply_to,"
            " from_addr, to_addr, subject, sent_at, received_at, body, bulk, authenticated)"
            " VALUES (?, ?, ?, 'in', 1, ?, ?, '<m1@ember>', 'ann@example.org', 'ember@example.org', 'Re: Your planner',"
            " ?, ?, 'Thanks!', 0, 1)",
            (scope.mode, scope.session, scope.life_id, uid, f"<r{uid}@ann>", now, now),
        )


def test_a_reply_wakes_the_agent_for_a_lean_reactive_cycle(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    agent.check_events()  # the agenda begins: what came before is history
    assert rows(agent, "SELECT COUNT(*) AS n FROM agenda WHERE baseline = 0") == [{"n": 0}]
    agent.clock.advance(minutes=10)
    sold(agent, 71)  # 0.15.0: an order is noted for the next plan, and wakes no one
    agent.check_events()
    replied(agent, 7)
    agent.check_events()
    agent.check_events()  # noted once
    noted = rows(agent, "SELECT kind, key, urgent, woke_at, seen_cycle_id, text FROM agenda WHERE baseline = 0")
    assert [(n["kind"], n["urgent"], n["woke_at"]) for n in noted] == [("order", 0, None), ("reply", 1, None)]
    assert (
        noted[0]["text"].startswith("An Etsy order (receipt #71, ")
        and f"of your listing #{listing} " in noted[0]["text"]
    )
    assert agent.sensor_fields()["agenda_open"] == 2
    decision = agent.decide()
    assert (decision.run, decision.trigger) == (True, "event")
    assert decision.reason.startswith(f"woken by an event: Email #{noted[1]['key']} from ann@example.org answers")
    fake = agent.transport
    before = len(fake.sent)
    end = agent.run_cycle("event")
    assert end.status in ("completed", "idle")
    cycle = rows(agent, "SELECT id, trigger FROM cycles ORDER BY id DESC LIMIT 1")[0]
    assert cycle["trigger"] == "event"
    sent = list(fake.sent)[before:]
    kinds = [request_kind(r) for r in sent]
    assert kinds[0] == "plan" and "review" not in kinds and "critic" not in kinds  # lean: the plan first
    planner = sent[0]["messages"][0]["content"][0]["text"]
    assert "\n== TASK ==\nPlan this reactive cycle: an event woke you (Agenda in SINCE YOUR LAST WAKE)." in planner
    assert "Agenda (reply, noted " in planner and "Agenda (order, noted " in planner
    assert sum(1 for k in kinds if k == "work") <= agenda.REACTIVE_STEPS
    after = rows(agent, "SELECT woke_at, seen_cycle_id FROM agenda WHERE baseline = 0 ORDER BY id")
    assert after[1]["woke_at"] is not None and all(a["seen_cycle_id"] == cycle["id"] for a in after)
    fields = agent.sensor_fields()
    assert (fields["agenda_open"], fields["event_wakes_today"]) == (0, 1)
    # Another reply within 30 minutes of that wake-up waits (for the gap, or the next cycle's plan)
    agent.clock.advance(minutes=5)
    replied(agent, 8)
    agent.check_events()
    assert agent.decide().trigger != "event"
    agent.clock.advance(minutes=30)
    assert agent.decide().trigger == "event"


def test_event_wakes_stop_at_four_a_day(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    agent.check_events()
    for n in range(agenda.EVENT_WAKES + 1):
        agent.clock.advance(minutes=31)
        replied(agent, 100 + n)
        agent.check_events()
        decision = agent.decide()
        if n < agenda.EVENT_WAKES:
            assert decision.trigger == "event", n
            agent.run_cycle("event")
        else:
            assert decision.trigger != "event"  # the fifth waits for the next cycle's plan
    assert rows(agent, "SELECT COUNT(*) AS n FROM cycles WHERE trigger = 'event'") == [{"n": agenda.EVENT_WAKES}]


def test_replies_favorites_and_a_milestones_last_day_are_noted(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    with agent.db.transaction() as conn:  # a listing with 12 favorites before the agenda begins: the baseline
        conn.execute("UPDATE etsy_listings SET favorites = 12 WHERE listing_id = ?", (listing,))
    agent.check_events()
    assert rows(agent, "SELECT kind, key, baseline FROM agenda") == [
        {"kind": "favorites", "key": f"{listing}:10", "baseline": 1}
    ]
    scope = agent.scope()
    now = to_iso(agent.clock.now() + timedelta(minutes=1))
    with agent.db.transaction() as conn:
        conn.execute("UPDATE etsy_listings SET favorites = 26 WHERE listing_id = ?", (listing,))
        approval = conn.execute("SELECT id FROM approvals ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, message_id, from_addr, to_addr, subject, sent_at,"
            " received_at, body, approval_id) VALUES (?, ?, ?, 'out', '<m1@ember>', 'ember@example.org',"
            " 'ann@example.org', 'Your planner', ?, ?, 'Hello', ?)",
            (scope.mode, scope.session, scope.life_id, now, now, approval),
        )
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, in_reply_to,"
            " from_addr, to_addr, subject, sent_at, received_at, body, bulk, authenticated) VALUES (?, ?, ?, 'in', 1,"
            " 7, '<r1@ann>', '<m1@ember>', 'ann@example.org', 'ember@example.org', 'Re: Your planner', ?, ?, 'Thanks!',"
            " 0, 1)",  # 0.15.0: a verified person's reply (a machine's or a forged one wakes no one)
            (scope.mode, scope.session, scope.life_id, now, now),
        )
        today = agent.clock.today().isoformat()
        roadmap.create(conn, scope, title="Five listings live", measure="x", due=today, now=now)
    local_hour = agent.clock.now().astimezone(agent.clock.tz).hour
    if local_hour < agenda.CHECK_HOUR:
        agent.clock.advance(hours=agenda.CHECK_HOUR - local_hour)
    agent.clock.advance(minutes=2)
    agent.check_events()
    noted = rows(agent, "SELECT kind, urgent, text FROM agenda WHERE baseline = 0 ORDER BY id")
    assert [(n["kind"], n["urgent"]) for n in noted] == [("reply", 1), ("favorites", 0), ("milestone_due", 1)]
    assert noted[0]["text"].endswith('answers one you sent: "Re: Your planner"')
    assert noted[1]["text"].startswith(f"Your listing #{listing} ") and noted[1]["text"].endswith(
        "has 26 favorites (25 or more)"
    )
    assert "is due today: its last day" in noted[2]["text"]


def test_the_evening_share_of_the_daily_cap_is_kept_for_events(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = listed(data_dir)
    settings, clock = agent.settings, agent.clock
    daily = metering.usd_cap_to_micros(settings.daily_spend_cap_usd)
    local = clock.now().astimezone(clock.tz)
    assert local.hour < metering.EVENT_RESERVE_HOUR
    held = int(daily * metering.EVENT_RESERVE_SHARE)
    assert metering.event_reserve(settings, clock, "schedule") == held
    assert metering.event_reserve(settings, clock, "event") == metering.event_reserve(settings, clock, "owner") == 0
    # Spent so far: all but a bit more than a cycle needs, less than it needs with the events' share kept
    spent = daily - held // 2 - 150_000
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, day: spent)
    agent._set_time("next_wake_at", clock.now() - timedelta(minutes=1))
    decision = agent.decide()
    assert not decision.run and decision.reason.startswith("The rest of the daily cap is kept for event wake-ups")
    evening = clock.day_start(clock.today()) + timedelta(hours=metering.EVENT_RESERVE_HOUR)
    assert decision.wait_until == evening
    clock.advance(seconds=(evening - clock.now()).total_seconds() + 60)  # 20:01: the evening releases it
    assert metering.event_reserve(settings, clock, "schedule") == 0
    assert agent.decide().trigger == "schedule"


def test_with_event_wake_ups_off_nothing_is_kept_for_events(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.30.3: with the owner's wake_on_events off, no event can spend the evening share: a scheduled cycle keeps none
    of it, and a scheduled wake that would need it runs before 20:00 instead of waiting for it."""
    agent, _ = listed(data_dir)
    clock = agent.clock
    daily = metering.usd_cap_to_micros(agent.settings.daily_spend_cap_usd)
    assert clock.now().astimezone(clock.tz).hour < metering.EVENT_RESERVE_HOUR
    held = int(daily * metering.EVENT_RESERVE_SHARE)
    spent = daily - held // 2 - 150_000  # as above: the events' share kept, the scheduled wake would wait
    monkeypatch.setattr(agent.economy.books, "cap_spend_on", lambda scope, day: spent)
    agent._set_time("next_wake_at", clock.now() - timedelta(minutes=1))
    assert agent.decide().reason.startswith("The rest of the daily cap is kept for event wake-ups")
    agent.settings = agent.meter.settings = agent.settings.model_copy(update={"wake_on_events": False})
    assert metering.event_reserve(agent.settings, clock, "schedule") == 0
    assert agent.meter.held_for_events("schedule") == 0  # every call of a scheduled cycle, its cap too
    agent._set_time("next_wake_at", clock.now() - timedelta(minutes=1))
    assert agent.decide().trigger == "schedule"
