"""Your decisions wake Ember, and waiting for them is no reason to sleep long (0.12.0, the owner's report: it slept
about 12 hours waiting for an approval, instead of working on something else meanwhile).

Also tests from 0.15.0 and 0.40.1 (formerly test_fixes_0140_live_fixes.py, test_fixes_0401.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app import web
from app.agent import context, service
from app.config import Settings
from app.economy.clock import from_iso
from tests.cycle.test_agent import ROOMY, make_agent, plan, rows, text, tools
from tests.owner.test_autonomy import request_for
from tests.owner.test_owner_loop import APPROVAL, owner

JOURNAL = tools(("write_journal", {"summary": "Asked to publish", "entry": "Waiting for my owner."}))
# 0.40.1: a waiting request cuts the sleep only while one of the owner's decisions on it wouldn't wake the agent
QUIET_REJECTION = ROOMY.model_copy(update={"wake_on_rejection": False})


def asks_and_sleeps(minutes: int) -> list:
    """A cycle that asks its owner to approve something, then wants to sleep ``minutes``."""
    return [
        plan(steps=["ask to publish"], sleep=minutes),
        tools(("request_approval", APPROVAL)),
        text("Asked."),
        JOURNAL,
    ]


def test_a_request_waiting_for_the_owner_cuts_a_long_sleep(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [*asks_and_sleeps(720), plan(steps=[], sleep=720)], QUIET_REJECTION)
    started = agent.clock.now()
    assert agent.run_cycle("schedule").status == "completed"
    fields = agent.agent_fields()
    assert from_iso(fields["next_wake_at"]) - started <= timedelta(minutes=241)  # the default interval, not 12 hours
    assert fields["next_wake_reason"] == (
        "Ember chose 720 min; cut to 240 min: 1 request waits for your decision, and it works on something else"
        " meanwhile"
    )
    # The next plan is told plainly: the decision wakes it, and waiting isn't its job.
    agent.clock.advance(minutes=241)
    assert agent.decide().run and agent.run_cycle("schedule").status == "idle"
    planner = transport.sent[-1]["messages"][0]["content"][0]["text"]
    assert f"== WAITING FOR YOUR OWNER ==\n{context.WAITING_NOTE}\n#1 publish: Post the guide" in planner


def test_the_owners_decision_wakes_ember_to_act_on_it(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [*asks_and_sleeps(720), plan(steps=[], sleep=720)])
    assert agent.run_cycle("schedule").status == "completed"
    request, pokes = request_for(agent, Settings())
    decided = owner(agent).decide(1, {"decision": "approve", "expected_version": 0}, "Stefan")
    assert decided.status == 200
    web._wake_for_decision(request, decided, "approval")
    assert decided.body["wake"] == "soon" and not agent.wake_requested and pokes == ["x"]
    agent.clock.advance(seconds=service.OWNER_QUIET.total_seconds())  # 0.15.0: once the owner has been quiet
    assert agent.decide().trigger == "owner"
    assert "The owner's decision woke the agent" in [e["message"] for e in agent.db.recent_events(10)]
    assert agent.run_cycle("owner").status == "idle"
    # Nothing waits any more: the sleep it chose stands.
    assert agent.agent_fields()["next_wake_reason"] == "Ember chose 720 min"


def test_a_decision_during_a_cycle_wakes_ember_after_it_unless_it_saw_it(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [*asks_and_sleeps(720), plan(steps=[], sleep=720), plan(steps=[], sleep=720)])
    assert agent.run_cycle("schedule").status == "completed"
    request, pokes = request_for(agent, Settings())
    agent.running_cycle = True  # a cycle is working (it planned before the decision came)
    decided = owner(agent).decide(1, {"decision": "reject", "comment": "Not now.", "expected_version": 0}, None)
    web._wake_for_decision(request, decided, "rejection")
    assert decided.body["wake"] == "after_cycle" and agent.message_waiting and pokes == []
    assert agent.agent_fields()["next_wake_reason"] == "to act on your decision"
    agent.running_cycle = False  # it ended without seeing the decision
    agent.clock.advance(seconds=service.OWNER_QUIET.total_seconds())  # 0.15.0: once the owner has been quiet
    assert agent.decide().trigger == "owner" and not agent.message_waiting
    assert agent.run_cycle("owner").status == "idle"
    assert rows(agent, "SELECT seen_cycle_id FROM approvals")[0]["seen_cycle_id"] == 2  # seen now
    agent.clock.advance(seconds=61)
    agent.message_waiting, agent.waiting_for = True, "decision"  # a decision it has seen: no second cycle for it
    assert not agent.decide().run and not agent.message_waiting
    messages = [e["message"] for e in agent.db.recent_events(20)]
    assert messages.count("The owner's decision woke the agent") == 1


def test_no_wake_when_the_owner_turned_it_off(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, asks_and_sleeps(720))
    assert agent.run_cycle("schedule").status == "completed"
    off, pokes = request_for(agent, Settings(wake_on_decision=False))
    decided = owner(agent).decide(1, {"decision": "approve", "expected_version": 0}, None)
    web._wake_for_decision(off, decided, "approval")
    assert "wake" not in decided.body and not agent.wake_requested and pokes == []
    refused = owner(agent).decide(99, {"decision": "approve"}, None)
    web._wake_for_decision(request_for(agent, Settings())[0], refused, "approval")  # a failed decision wakes nothing
    assert not agent.wake_requested


# --- From 0.15.0 (formerly test_fixes_0140_live_fixes.py) ---


# --- LIVE 5: the sleep the agent chose stands once nothing waits ----------------------------------------------------


def test_the_owners_last_decision_lifts_the_sleep_cut(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, asks_and_sleeps(720), QUIET_REJECTION)  # 0.40.1: the cut needs it
    assert agent.run_cycle("schedule").status == "completed"
    ended = agent.clock.now()
    fields = agent.agent_fields()
    assert "cut to 240 min" in fields["next_wake_reason"]
    assert any("chose 720 min of sleep" in e["message"] for e in agent.db.recent_events(10))
    agent.clock.advance(minutes=30)
    off, _ = request_for(agent, Settings(wake_on_decision=False))
    decided = owner(agent).decide(1, {"decision": "reject", "comment": "No.", "expected_version": 0}, None)
    web._wake_for_decision(off, decided, "rejection")
    fields = agent.agent_fields()
    assert from_iso(fields["next_wake_at"]) - ended == timedelta(minutes=720)
    assert fields["next_wake_reason"] == "Ember chose 720 min"


def test_the_cut_stays_while_a_request_waits_or_the_wake_changed(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, asks_and_sleeps(720), QUIET_REJECTION)  # 0.40.1: the cut needs it
    assert agent.run_cycle("schedule").status == "completed"
    cut = agent.agent_fields()["next_wake_at"]
    agent.lift_sleep_cut()  # the request still waits
    assert agent.agent_fields()["next_wake_at"] == cut
    agent._set_time("next_wake_at", agent.clock.now() + timedelta(minutes=5))  # something else set the wake
    off, _ = request_for(agent, Settings(wake_on_decision=False))
    web._wake_for_decision(
        off, owner(agent).decide(1, {"decision": "approve", "expected_version": 0}, None), "approval"
    )
    assert from_iso(agent.agent_fields()["next_wake_at"]) - agent.clock.now() == timedelta(minutes=5)


# --- From 0.40.1 (formerly test_fixes_0401.py) ---


# --- 6. a waiting request and the sleep ---


def test_a_request_waiting_for_the_owner_leaves_the_sleep_when_their_decision_wakes_ember(data_dir: Path) -> None:
    """Live on 10-17 (analysis-0.40.0, 3.4), a pin waiting for the owner woke the agent after 4 of the 6 hours it chose,
    twice to nothing it could do; the owner's approval woke it anyway, at 04:43."""
    agent, _ = make_agent(data_dir, asks_and_sleeps(360), ROOMY.model_copy(update={"max_sleep_minutes": 360}))
    started = agent.clock.now()
    assert agent.run_cycle("schedule").status == "completed"
    assert agent._meta_time("next_wake_at") == started + timedelta(minutes=360)
    assert agent.agent_fields()["next_wake_reason"] == "Ember chose 360 min"
