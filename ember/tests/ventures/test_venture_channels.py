"""A venture that sells through a channel (Pinterest, say): its first test waits for the channel to be set up,
starts again when it wasn't, and the agent hears why the channel's tools are off.

Tests from 0.15.0 (formerly test_fixes_0140_channels.py)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from app.agent import stages  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind  # noqa: E402
from app.agent.owner import Owner  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.integrations.pinterest import FakePinterest  # noqa: E402
from app.integrations.printify import PrintifyError  # noqa: E402
from tests.agent_helpers import owner, rows, run  # noqa: E402
from tests.channels.test_pinterest import PINNING  # noqa: E402
from tests.ventures.test_ventures import PINTEREST, VENTURING, plan, planner_texts, venture  # noqa: E402

# --- From 0.15.0 (formerly test_fixes_0140_channels.py) ---


# --- X21: a channel switched on but not set up ------------------------------------------------------------------


def not_set_up(agent: Agent) -> FakePinterest | None:
    """Pinterest on as in the live report: no app ID, no secret (the fake account put aside)."""
    fake = agent.pinterest._fake
    agent.pinterest.mode, agent.pinterest._fake = "live", None
    return fake


def test_the_agent_hears_that_a_channel_waits_for_its_owner(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=0, settings=PINNING.model_copy(update={"printify_enabled": True}))
    not_set_up(agent)
    agent.printify.mode, agent.printify._fake = "live", None  # Printify on, no token
    agent.run_cycle("schedule")
    plan_request = next(r for r in fake.sent if request_kind(r) == "plan")
    text = plan_request["messages"][0]["content"][0]["text"]
    line = (
        "\n== PINTEREST ==\nSwitched on, but it waits for your owner's setup (pinterest_app_id is missing; "
        "pinterest_app_secret is missing, then System, Pinterest, Connect): no Pinterest tools until then. Your "
        "owner's dashboard shows it: don't ask them about it again. A venture it serves starts its first test only "
        "then.\n"
    )
    assert line in text
    assert "\n== PRINTIFY ==\nSwitched on, but it waits for your owner's setup (printify_api_token is missing):" in text
    work = next(r for r in fake.sent if request_kind(r) == "work")
    assert not {t["name"] for t in work["tools"]} & {"propose_pin", "pinterest_boards"}
    agent.pinterest._fake = FakePinterest(agent.clock, None, lambda s: None)
    agent.pinterest.mode = "dry_run"  # set up: its account is a marketing cycle's section (0.35.0), no waiting line
    agent.run_cycle("schedule")
    text = [r for r in fake.sent if request_kind(r) == "plan"][-1]["messages"][0]["content"][0]["text"]
    assert "setup (pinterest_app_id" not in text


# 0.15.0 (ventures): these ventures have no numbers yet, so the owner backs them with confirm ("Back it anyway").


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_channel_venture_s_first_test_waits_for_its_channel(data_dir: Path) -> None:
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    transport = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, transport, settings=settings)
    fake = not_set_up(agent)
    assert owner(agent).decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    assert venture(agent, PINTEREST)["test_milestone_id"] is None  # its clock doesn't run yet
    agent.clock.advance(days=40)
    agent.run_cycle("schedule")
    backed = venture(agent, PINTEREST)
    assert (backed["stage"], backed["test_milestone_id"]) == ("building", None)  # not parked for a missed test
    # The agent isn't asked to plan a first test that Ember's code sets.
    assert "it is building now. Its first test starts once Pinterest is set up." in planner_texts(transport)[-1]
    agent.pinterest.mode, agent.pinterest._fake = "dry_run", fake  # the owner set it up
    agent.run_cycle("schedule")
    test = venture(agent, PINTEREST)["test_milestone_id"]
    assert test is not None
    [row] = rows(agent, f"SELECT metric, target, due FROM milestones WHERE id = {test}")
    assert (row["metric"], row["target"]) == ("pin_clicks", 10)
    assert row["due"] == (agent.clock.today() + timedelta(days=stages.FIRST_TEST_DAYS)).isoformat()


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_venture_without_a_channel_still_gets_its_test_when_backed(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    back = {"action": "back", "confirm": True}
    assert owner(agent).decide_venture(3, back, "Stefan").status == 200  # dropshipping: no channel
    assert venture(agent, 3)["test_milestone_id"] is not None


def test_the_agent_hears_why_a_set_up_channel_s_tools_are_off(data_dir: Path, monkeypatch: Any) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=0, settings=PINNING.model_copy(update={"printify_enabled": True}))

    def unknown() -> None:
        raise PrintifyError("no shop of yours is connected to Etsy at Printify: connect one, or set printify_shop_id")

    monkeypatch.setattr(agent.printify, "shop", unknown)  # a token, but Printify's shop isn't connected to Etsy
    agent.run_cycle("schedule")
    text = [r for r in fake.sent if request_kind(r) == "plan"][-1]["messages"][0]["content"][0]["text"]
    assert "== PRINTIFY ==\nSwitched on, but no Printify tools this cycle: Printify's shop isn't known: no shop" in text
    assert "starts its first test only then" not in text.split("== PRINTIFY ==")[1].split("==")[0]
    agent.etsy.mode, agent.etsy._fake = "live", None  # the Etsy shop isn't set up: neither channel can work
    agent.run_cycle("schedule")
    text = [r for r in fake.sent if request_kind(r) == "plan"][-1]["messages"][0]["content"][0]["text"]
    for name in ("PINTEREST", "PRINTIFY"):
        assert f"== {name} ==\nSwitched on, but it waits for your owner's setup (the Etsy shop isn't connected)" in text


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_first_test_set_before_its_channel_was_set_up_starts_again(data_dir: Path) -> None:
    """The live case: venture #2 was backed under 0.13.0 (its first test set at once) while Pinterest wasn't set up."""
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    transport = FakeTransport(script=[plan(steps=[])])
    agent, _ = run(data_dir, transport, settings=settings)
    fake = not_set_up(agent)
    assert owner(agent).decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    with agent.db.transaction() as conn:  # as 0.13.0 did when the owner backed it
        old = stages.first_test(conn, agent.scope(), venture(agent, PINTEREST), agent.clock.today(), "2026-09-30")
    agent.clock.advance(days=30)
    agent.run_cycle("schedule")
    backed = venture(agent, PINTEREST)
    assert (backed["stage"], backed["test_milestone_id"]) == ("building", None)  # not parked for a missed test
    [row] = rows(agent, f"SELECT status, closed_by, result FROM milestones WHERE id = {old}")
    assert (row["status"], row["closed_by"]) == ("dropped", "code") and "Pinterest isn't set up" in row["result"]
    agent.pinterest.mode, agent.pinterest._fake = "dry_run", fake  # the owner set it up
    agent.run_cycle("schedule")
    test = venture(agent, PINTEREST)["test_milestone_id"]
    assert test is not None and test != old
    [row] = rows(agent, f"SELECT status, due FROM milestones WHERE id = {test}")
    assert row["status"] == "open"
    assert row["due"] == (agent.clock.today() + timedelta(days=stages.FIRST_TEST_DAYS)).isoformat()


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_channel_venture_backed_while_its_channel_is_ready_gets_its_test_at_once(data_dir: Path) -> None:
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=settings)
    assert agent.channels_ready() == ["pinterest"]
    ready = Owner(agent.db, agent.clock, agent.economy, agent.scope(), "Ember", ready=agent.channels_ready())
    assert ready.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    assert venture(agent, PINTEREST)["test_milestone_id"] is not None
    not_set_up(agent)
    assert agent.channels_ready() == []


@pytest.mark.exploring  # 0.36.0: the plan's Explore step makes its venture cycles
def test_a_first_test_running_when_the_channel_is_switched_off_starts_again(data_dir: Path, monkeypatch: Any) -> None:
    """Switched off mid-test, the channel's test isn't left to pass its date and be missed the moment it is back."""
    settings = VENTURING.model_copy(update={"pinterest_enabled": True})
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=settings)
    ready = Owner(agent.db, agent.clock, agent.economy, agent.scope(), "Ember", ready=agent.channels_ready())
    assert ready.decide_venture(PINTEREST, {"action": "back", "confirm": True}, "Stefan").status == 200
    old = venture(agent, PINTEREST)["test_milestone_id"]
    assert old is not None
    on = agent.pinterest.status, agent.pinterest.account
    monkeypatch.setattr(agent.pinterest, "status", lambda: ("disabled", None))  # the owner switched it off
    monkeypatch.setattr(agent.pinterest, "account", lambda: None)
    agent.clock.advance(days=40)
    agent.run_cycle("schedule")
    [row] = rows(agent, f"SELECT status, closed_by FROM milestones WHERE id = {old}")
    assert (row["status"], row["closed_by"]) == ("dropped", "code")
    assert (venture(agent, PINTEREST)["stage"], venture(agent, PINTEREST)["test_milestone_id"]) == ("building", None)
    monkeypatch.setattr(agent.pinterest, "status", on[0])  # and on again
    monkeypatch.setattr(agent.pinterest, "account", on[1])
    agent.run_cycle("schedule")
    backed = venture(agent, PINTEREST)
    assert backed["stage"] == "building" and backed["test_milestone_id"] not in (None, old)
