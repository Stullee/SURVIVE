"""0.42.3 (analysis-0.40.1 audit): the owner's daily digest counts an approved request that went back unsent apart.

Since 0.40.3 an approved email, and since 0.42.2 a listing, change, pin, post, Printify product or blog page, that
Ember's code hasn't begun within APPROVED_DAYS of its approval is closed unsent: failed, by Ember, with why, and
nothing journaled (connectors.lapse, executor.Executor._one). The digest counted every request Ember's code closed
without a journal entry as an action, and a failed one as a failure: "carried out 1 action (1 you approved): 1 failed",
though nothing was carried out. Now such a request is counted apart, by whose approval it had; a request that really
failed still fails. The sensors, the System log and the list of what Ember's code did agree.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import audit, policy, store  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import connectors  # noqa: E402
from tests.agent_helpers import make_agent, owner, rows  # noqa: E402
from tests.channels.test_approvals_expire import listing, paused_for  # noqa: E402
from tests.etsy_helpers import listed  # noqa: E402
from tests.mail.test_executor import REPLY, approve, ids, proposing  # noqa: E402
from tests.owner.test_audit import feed  # noqa: E402
from tests.owner.test_digest_waits import today  # noqa: E402
from tests.owner.test_policy import a_milestone, change, price_of, work_on  # noqa: E402

DAYS = connectors.APPROVED_DAYS
WENT_BACK = (
    f"1 request went back unsent (1 you approved): it didn't go out within {DAYS} days of its approval (Ember proposes"
    " it again if it still fits)"
)


def next_day(agent: Any) -> str:
    """The day of today, as its digest is written at the next day's first round."""
    day = agent.clock.today().isoformat()
    agent.clock.current = agent.clock.day_start(agent.clock.today() + timedelta(days=1)) + timedelta(minutes=5)
    agent.run_policy()
    return day


def a_request(agent: Any, title: str, listing_id: int = 1) -> int:
    """A change of a listing's title, as the tools ask for one."""
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        return store.insert_approval(
            conn,
            agent.scope(),
            cycle,
            to_iso(agent.clock.now()),
            type="sell",
            title=title,
            description="A better title.",
            payload=f"{title}: a better title",
            expected_cost="none",
            expected_benefit="more views",
            executor="etsy_edit",
            action=store.canonical({"listing_id": listing_id, "currency": "EUR", "title": "A better title"}),
        )


def approved_long_ago(agent: Any, request: int, by: str) -> None:
    """Approved by ``by`` (the owner, or an unlock) more than APPROVED_DAYS ago, and never begun."""
    stamp = to_iso(agent.clock.now() - timedelta(days=DAYS, minutes=1))
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ? WHERE id = ?",
            (stamp, by, request),
        )


def test_a_listing_that_aged_out_went_back_unsent_and_no_action_failed(data_dir: Path) -> None:
    agent, request = listing(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    paused_for(agent, 21)
    assert (request, "failed") in agent.execute_approved()
    text, data = today(agent)
    assert (data["actions"], data["trouble"], data["aged_out"]) == ({}, {}, {"owner": 1})
    day = agent.clock.today().isoformat()
    assert text == f"{day}: Ember's code carried out nothing. {WENT_BACK}."  # before: 1 action (...): 1 failed
    # What Ember's code did has no entry for it (nothing began), and the next day's digest says the same everywhere
    assert all(entry.get("approval_id") != request for entry in feed(agent))
    assert next_day(agent) == day
    sensors = agent.sensor_fields()
    assert (sensors["digest_day"], sensors["digest_actions"], sensors["digest_failed"]) == (day, 0, 0)
    logged = [r["message"] for r in rows(agent, "SELECT message FROM events WHERE message LIKE 'Daily digest: %'")]
    assert f"Daily digest: {text}"[:300] in logged
    assert agent.dashboard()["audit"]["digest"]["text"] == text


def test_an_approved_email_that_aged_out_went_back_unsent(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, proposing(REPLY))
    agent.run_cycle("schedule")
    [request] = ids(agent)
    approve(agent, request)
    agent.economy.set_paused(True)
    agent.clock.advance(days=21)  # type: ignore[attr-defined]
    agent.economy.set_paused(False)
    agent.run_policy()
    assert agent.execute_approved() == [(request, "failed")]  # executor.STALE, in the email executor's own words
    text, data = today(agent)
    assert (data["actions"], data["trouble"], data["aged_out"]) == ({}, {}, {"owner": 1})
    assert text.endswith(f": Ember's code carried out nothing. {WENT_BACK}.")  # before: 1 action (1 you approved)...


def test_requests_that_went_back_are_counted_by_whose_approval(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200
    price = f"{price_of(agent, listing_id) * Decimal('0.95'):.2f}"
    unlocked = work_on(agent, goal, change(listing_id, price=price))[-1]
    assert unlocked["decided_by"] == policy.POLICY_BY
    mine = a_request(agent, "Change 1", listing_id)
    assert owner(agent).decide(mine, {"decision": "approve"}, "Owner").status == 200
    agent.clock.advance(days=DAYS, minutes=1)  # neither began (Ember was paused, say)
    assert sorted(agent.execute_approved()) == [(unlocked["id"], "failed"), (mine, "failed")]  # connectors.lapse
    text, data = today(agent)
    assert (data["actions"], data["trouble"], data["aged_out"]) == ({}, {}, {"owner": 1, "unlock": 1})
    assert (
        "Ember's code carried out nothing. 2 requests went back unsent (1 on your unlocks, 1 you approved): they"
        f" didn't go out within {DAYS} days of their approval (Ember proposes each again if it still fits)."
    ) in text


def test_a_request_closed_unsent_for_another_reason_still_failed(data_dir: Path) -> None:
    """A request Ember's code closed failed without a journal entry (as before the journal began, 0.13.0) went back
    unsent only if its note says it aged out: closed_by and status are those of every failure."""
    agent, _ = listing(data_dir)
    request = a_request(agent, "Change 1")
    approved_long_ago(agent, request, "Owner")
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET status = 'failed', closed_at = ?, closed_by = ?, result_note = ? WHERE id = ?",
            (to_iso(agent.clock.now()), connectors.CLOSED_BY, "Not changed: Etsy refused it (title too long)", request),
        )
    text, data = today(agent)
    assert (data["actions"], data["trouble"], data["aged_out"]) == ({"owner": 1}, {"failed": 1}, {})
    assert "Ember's code carried out 1 action (1 you approved): 1 failed." in text and "went back" not in text
    assert audit.AGED_OUT[0] == connectors.STALE.format(days=DAYS)  # what lapse writes, whole
