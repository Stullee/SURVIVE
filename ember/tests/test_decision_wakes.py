"""Your decisions wake Ember, and waiting for them is no reason to sleep long (0.12.0, the owner's report: it slept
about 12 hours waiting for an approval, instead of working on something else meanwhile)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app import web
from app.agent import context, service
from app.config import Settings
from app.economy.clock import from_iso
from tests.test_agent import make_agent, plan, rows, text, tools
from tests.test_autonomy import request_for
from tests.test_owner_loop import APPROVAL, owner

JOURNAL = tools(("write_journal", {"summary": "Asked to publish", "entry": "Waiting for my owner."}))


def asks_and_sleeps(minutes: int) -> list:
    """A cycle that asks its owner to approve something, then wants to sleep ``minutes``."""
    return [
        plan(steps=["ask to publish"], sleep=minutes),
        tools(("request_approval", APPROVAL)),
        text("Asked."),
        JOURNAL,
    ]


def test_a_request_waiting_for_the_owner_cuts_a_long_sleep(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [*asks_and_sleeps(720), plan(steps=[], sleep=720)])
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
