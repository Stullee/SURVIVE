"""0.42.2: the owner's daily digest counts a wait for the service as a wait, not as a failed action.

Since 0.42.0, when Etsy, Pinterest or Bluesky asks Ember's code to come back later before anything changed (429, or
503 with Retry-After), the attempt is journaled 'failed' with when it is tried again (retry.py), at most 3 times. The
digest counted every 'failed' entry as a failed action: a listing that waited twice and then went out read as 3
actions, 2 of them failed. Now a wait is no action: the requests that waited are counted apart, one that fails at its
last try fails once, and so does one whose approval aged out while it waited (connectors.stale). The list of what
Ember's code did shows such a try as "waited", and the sensor's digest_failed agrees.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import audit  # noqa: E402
from app.integrations import connectors, etsy, retry  # noqa: E402
from app.integrations.etsy import FakeShop  # noqa: E402
from tests.test_approval_boundary import WAIT, refuse  # noqa: E402
from tests.test_audit import feed  # noqa: E402
from tests.test_etsy import proposed  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

WAITED = "1 request(s) waited when their service asked to come back later (not failures)"


def approved(data_dir: Path, monkeypatch: pytest.MonkeyPatch, refusals: int) -> tuple[Any, int]:
    """A listing the owner approved, whose first ``refusals`` drafts Etsy answers with "come back later"."""
    agent, _, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy, times=refusals)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    return agent, request


def today(agent: Any) -> tuple[str, dict[str, Any]]:
    with agent.db.connection() as conn:
        return audit.digest(conn, agent.scope(), agent.clock, agent.clock.today())


def test_a_listing_that_waited_twice_and_went_out_is_one_action_and_no_failure(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = approved(data_dir, monkeypatch, 2)
    for _ in range(2):
        assert agent.execute_approved() == [(request, retry.LATER)]
        agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "active")]
    text, data = today(agent)
    # before: 3 actions, 2 of them failed
    assert (data["actions"], data["trouble"]) == ({"owner": 1}, {}) and data.get("waited_for_service") == 1
    day = agent.clock.today().isoformat()
    assert text.startswith(f"{day}: Ember's code carried out 1 action (1 you approved). {WAITED}.")
    agent.clock.current = agent.clock.day_start(agent.clock.today() + timedelta(days=1)) + timedelta(minutes=5)
    agent.run_policy()  # the next day's first round writes it
    sensors = agent.sensor_fields()
    assert (sensors["digest_day"], sensors["digest_actions"], sensors["digest_failed"]) == (day, 1, 0)


def test_a_listing_that_failed_after_its_waits_counts_once_as_failed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = approved(data_dir, monkeypatch, 99)
    for _ in range(retry.TRIES):
        assert agent.execute_approved() == [(request, retry.LATER)]
        agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]
    text, data = today(agent)
    assert (data["actions"], data["trouble"]) == ({"owner": 1}, {"failed": 1}) and data.get("waited_for_service") == 1
    assert f"Ember's code carried out 1 action (1 you approved): 1 failed. {WAITED}." in text  # before: 4, 4 failed


def test_a_listing_still_waiting_at_the_day_s_end_is_no_action(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, request = approved(data_dir, monkeypatch, 99)
    assert agent.execute_approved() == [(request, retry.LATER)]
    text, data = today(agent)
    assert (data["actions"], data["trouble"]) == ({}, {}) and data.get("waited_for_service") == 1
    assert f"Ember's code carried out nothing. {WAITED}." in text  # before: 1 action, 1 failed


def test_a_listing_that_aged_out_while_it_waited_counts_once_as_failed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = approved(data_dir, monkeypatch, 99)
    agent.clock.advance(days=connectors.APPROVED_DAYS - 1, hours=23, minutes=59)
    day = agent.clock.today()
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]  # its approval aged out before it was tried again
    assert agent.clock.today() == day
    _, data = today(agent)
    assert (data["actions"], data["trouble"]) == ({"owner": 1}, {"failed": 1}) and data.get("waited_for_service") == 1


def test_the_list_of_what_ember_s_code_did_shows_a_wait_as_a_wait(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, request = approved(data_dir, monkeypatch, 1)
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "active")]
    made, waited = feed(agent)[:2]
    assert (made["status"], waited["status"]) == ("done", "waited")  # before: "failed"
    assert waited["note"].startswith("Etsy asked to wait: retrying at ")
    assert waited["undo"]["why_not"] == "nothing was done" and waited["undo"]["label"] is None
