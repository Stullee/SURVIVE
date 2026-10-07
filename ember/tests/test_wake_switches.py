"""0.31.0: a switch for each kind of thing that wakes Ember, as the owner asked ("a tick that makes her wake from
approval but not messages"). Under the switch of its group: wake_on_decision for approving, rejecting, marking done
or failed, a venture's and the roadmap's decisions; wake_on_events for a reply, a new email and a milestone's last
day. All on by default, so the owner's options from before mean what they meant."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.datastructures import Headers

from app import web
from app.agent import agenda, context, service
from app.agent.service import Agent
from app.config import WAKING_DECISIONS, WAKING_EVENTS, LoadedSettings, Settings
from app.economy import metering
from app.economy.clock import to_iso
from tests.test_agent import ROOMY, make_agent, plan, rows
from tests.test_autonomy import request_for, send
from tests.test_decision_wakes import asks_and_sleeps
from tests.test_fixes_0140_wakes import urgent_milestone

EVENTFUL = Settings(starting_balance_usd=50, daily_spend_cap_usd=5, cycle_spend_cap_usd=1)


def roomy(**switches: bool) -> Settings:
    return Settings(**{**ROOMY.model_dump(), **switches})


def routed(agent: Agent, settings: Settings) -> tuple[Any, list[str]]:
    """request_for's stand-in, with what the dashboard's routes need too."""
    request, pokes = request_for(agent, settings)
    state = request.app.state.ember
    state.db, state.economy = agent.db, agent.economy
    request.headers = Headers({})
    return request, pokes


def body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def asked(data_dir: Path, settings: Settings) -> Agent:
    """An agent with one request waiting for the owner (#1)."""
    agent, _ = make_agent(data_dir, [*asks_and_sleeps(720), plan(steps=[], sleep=720)], settings)
    assert agent.run_cycle("schedule").status == "completed"
    return agent


def test_each_switch_is_on_by_default_and_needs_its_group() -> None:
    defaults = Settings()
    assert all(defaults.wakes_on(kind) for kind in ("message", *WAKING_DECISIONS, *WAKING_EVENTS))
    assert defaults.waking_events() == ("reply", "inquiry", "milestone_due")
    no_approvals = Settings(wake_on_approval=False)
    assert not no_approvals.wakes_on("approval")
    assert all(no_approvals.wakes_on(k) for k in ("message", "rejection", "outcome", "venture", "roadmap"))
    no_decisions = Settings(wake_on_decision=False)
    assert not any(no_decisions.wakes_on(kind) for kind in WAKING_DECISIONS) and no_decisions.wakes_on("message")
    assert Settings(wake_on_events=False).waking_events() == ()
    assert Settings(wake_on_reply=False, wake_on_milestone_due=False).waking_events() == ("inquiry",)
    assert not Settings(wake_on_message=False).wakes_on("message")


def test_an_approval_wakes_ember_while_a_message_doesnt(data_dir: Path) -> None:
    only = roomy(
        wake_on_message=False,
        wake_on_rejection=False,
        wake_on_outcome=False,
        wake_on_venture=False,
        wake_on_roadmap=False,
    )
    agent = asked(data_dir, only)
    request, pokes = routed(agent, only)
    written = web.send_message(request, {"text": "How is it going?"})
    assert written.status_code == 201 and body(written)["wake"] is None
    assert not agent.message_waiting and pokes == []
    approved = web.decide_approval(request, 1, {"decision": "approve", "expected_version": 0})
    assert approved.status_code == 200 and body(approved)["wake"] == "soon" and pokes == ["x"]
    assert agent.agent_fields()["next_wake_reason"] == "to act on your decision"
    agent.clock.advance(seconds=service.OWNER_QUIET.total_seconds())
    assert agent.decide().trigger == "owner"
    assert agent.run_cycle("owner").status == "idle"
    # Marking it done is switched off: no wake, and the sleep the agent chose stands.
    agent.clock.advance(minutes=2)
    closed = web.close_approval(request, 1, {"outcome": "done", "result_note": "Posted."})
    assert closed.status_code == 200 and "wake" not in body(closed) and not agent.message_waiting


@pytest.mark.parametrize(
    ("decision", "off", "wakes"),
    [
        ("reject", "wake_on_rejection", False),
        ("reject", "wake_on_approval", True),
        ("approve", "wake_on_approval", False),
        ("approve_with_changes", "wake_on_approval", False),
        ("approve", "wake_on_rejection", True),
    ],
)
def test_approving_and_rejecting_have_switches_of_their_own(
    data_dir: Path, decision: str, off: str, wakes: bool
) -> None:
    settings = roomy(**{off: False})
    agent = asked(data_dir, settings)
    request, pokes = routed(agent, settings)
    said = {"decision": decision, "expected_version": 0, "comment": "Not now."}
    if decision == "approve_with_changes":
        said["final_payload"] = "Post the guide on Monday."
    decided = web.decide_approval(request, 1, said)
    assert decided.status_code == 200
    assert (body(decided).get("wake") == "soon") is wakes and agent.message_waiting is wakes
    assert pokes == (["x"] if wakes else [])
    # Either way, no request waits any more: the sleep the agent chose stands again.
    assert agent.agent_fields()["next_wake_reason"] == ("to act on your decision" if wakes else "Ember chose 720 min")


def test_each_route_names_its_kind_of_decision(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _ = make_agent(data_dir, [])
    request, _ = routed(agent, Settings())
    named: list[str] = []
    monkeypatch.setattr(web, "_wake_for_decision", lambda request, reply, kind: named.append(kind))
    web.decide_approval(request, 9, {"decision": "approve"})
    web.close_approval(request, 9, {"outcome": "done"})
    web.decide_venture(request, 9, {"action": "park"})
    web.override_knockout(request, 9, {"rule": "cash", "lift": True})
    web.set_autonomy(request, 9, {"rule": "x", "level": "manual"})
    web.take_back_unlocks(request, {})
    web.set_goal(request, {"per": "month"})
    web.decide_milestone(request, 9, {"action": "drop"})
    assert named == ["approval", "outcome", "venture", "venture", "roadmap", "roadmap", "roadmap", "roadmap"]
    assert set(named) <= set(WAKING_DECISIONS)


def test_only_the_events_switched_on_wake_ember(data_dir: Path) -> None:
    settings = Settings(**{**EVENTFUL.model_dump(), "wake_on_reply": False, "wake_on_milestone_due": False})
    agent, _ = make_agent(data_dir, [], settings)
    agent.decide()  # the first wake-up, in two minutes
    urgent_milestone(agent)  # its last day: switched off
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        agenda._add(conn, agent.scope(), "reply", "1", "Email #1 from ann@example.org answers one you sent", now)
        agenda._add(conn, agent.scope(), "inquiry", "2", "Email #2 from bob@example.org writes to you", now)
    decision = agent.decide()
    assert (decision.run, decision.trigger) == (True, "event")
    assert decision.reason == "woken by an event: Email #2 from bob@example.org writes to you"
    woke = {r["kind"]: r["woke_at"] is not None for r in rows(agent, "SELECT kind, woke_at FROM agenda")}
    assert woke == {"milestone_due": False, "reply": False, "inquiry": True}  # the others wait for the next plan


def test_no_event_wakes_ember_when_each_is_switched_off(data_dir: Path) -> None:
    off = {"wake_on_reply": False, "wake_on_inquiry": False, "wake_on_milestone_due": False}
    agent, _ = make_agent(data_dir, [], Settings(**{**EVENTFUL.model_dump(), **off}))
    agent.decide()
    urgent_milestone(agent)
    assert agent.decide().trigger is None
    agent.clock.advance(minutes=3)
    assert agent.decide().trigger == "schedule"
    with agent.db.connection() as conn:
        assert agenda.waking(conn, agent.scope(), ()) == []
        assert len(agenda.waking(conn, agent.scope(), ("milestone_due", "order"))) == 1  # an order never wakes


def test_a_restart_keeps_the_wake_for_the_kinds_still_switched_on(data_dir: Path) -> None:
    agent = asked(data_dir, ROOMY)
    request, _ = routed(agent, ROOMY)
    send(agent, "Please read this")
    assert web._wake_for_message(request) == "soon"
    assert body(web.decide_approval(request, 1, {"decision": "approve", "expected_version": 0}))["wake"] == "soon"
    assert agent.db.get_meta(agent._key("owner_wake_kinds")) == "approval,message"

    def restarted(settings: Settings) -> Agent:  # changing an option restarts the app
        again = Agent(agent.db, LoadedSettings(settings), agent.economy, transport=agent.transport, cycles_enabled=True)
        again.recover()
        return again

    kept = restarted(roomy(wake_on_message=False))
    assert kept.message_waiting and kept.waiting_kinds == {"approval"} and kept.waiting_for == "decision"
    assert kept.agent_fields()["next_wake_reason"] == "to act on your decision"
    dropped = restarted(roomy(wake_on_message=False, wake_on_approval=False))
    assert not dropped.message_waiting and dropped._meta_time("owner_wake_at") is None


def test_a_wake_kept_before_the_switches_counts_by_its_group(data_dir: Path) -> None:
    agent = asked(data_dir, ROOMY)
    request, _ = routed(agent, ROOMY)
    web.decide_approval(request, 1, {"decision": "approve", "expected_version": 0})
    agent.db.set_meta(agent._key("owner_wake_kinds"), "")  # as 0.30.2 kept it: owner_wake_for alone
    assert agent.db.get_meta(agent._key("owner_wake_for")) == "decision"
    for settings, waits in ((roomy(wake_on_approval=False), True), (roomy(wake_on_decision=False), False)):
        again = Agent(agent.db, LoadedSettings(settings), agent.economy, transport=agent.transport, cycles_enabled=True)
        again.recover()
        assert again.message_waiting is waits


@pytest.mark.parametrize(
    ("switches", "told"),
    [
        ({"wake_on_approval": False}, True),  # rejecting still wakes it
        ({"wake_on_approval": False, "wake_on_rejection": False}, False),
    ],
)
def test_the_plan_says_a_decision_wakes_ember_only_when_one_does(
    data_dir: Path, switches: dict[str, bool], told: bool
) -> None:
    agent, transport = make_agent(data_dir, [*asks_and_sleeps(720), plan(steps=[], sleep=720)], roomy(**switches))
    assert agent.run_cycle("schedule").status == "completed"
    agent.clock.advance(minutes=241)
    assert agent.decide().run and agent.run_cycle("schedule").status == "idle"
    planner = transport.sent[-1]["messages"][0]["content"][0]["text"]
    assert "== WAITING FOR YOUR OWNER ==\n" in planner and (context.WAITING_NOTE in planner) is told


def test_no_evening_share_is_kept_while_no_kind_of_event_wakes_ember(data_dir: Path) -> None:
    """0.30.3 keeps none of the daily cap for events while wake_on_events is off; with each kind's switch off too."""
    agent, _ = make_agent(data_dir, [])
    clock = agent.clock
    assert clock.now().astimezone(clock.tz).hour < metering.EVENT_RESERVE_HOUR
    assert metering.event_reserve(ROOMY, clock, "schedule") > 0
    assert metering.event_reserve(roomy(wake_on_reply=False, wake_on_inquiry=False), clock, "schedule") > 0
    off = roomy(wake_on_reply=False, wake_on_inquiry=False, wake_on_milestone_due=False)
    assert metering.event_reserve(off, clock, "schedule") == 0
