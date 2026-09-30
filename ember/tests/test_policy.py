"""0.13.0: a policy engine. Every action waited for the owner's click, also the small, safe ones. Now the owner can
unlock rules for a milestone they back (QA fixes, price changes within 15%, listing variants in a backed leg,
deactivating a listing, replies in threads the other person started): manual, a veto window or auto, with a daily
limit and a budget. Ember's code revokes an unlock on an unclear result, a spent budget, a missed milestone or a veto,
and only proposes promotions."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import policy, roadmap  # noqa: E402
from app.agent.fake_llm import Plan, Reply, ToolCalls  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import etsy, etsy_publisher  # noqa: E402
from app.integrations.etsy import FakeShop  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_ventures import JOURNAL, plan, tool_results  # noqa: E402


def a_milestone(agent: Any, title: str = "Ten sales") -> int:
    due = (agent.clock.today() + timedelta(days=30)).isoformat()
    with agent.db.transaction() as conn:
        return roadmap.create(
            conn, agent.scope(), title=title, measure="10 orders", due=due, now=to_iso(agent.clock.now())
        )


def price_of(agent: Any, listing_id: int) -> Decimal:
    with agent.db.connection() as conn:
        current = etsy_publisher.current_listing(conn, agent.scope(), listing_id)
    assert current is not None
    return Decimal(current.price)


def work_on(agent: Any, milestone_id: int, *calls: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """A cycle aimed at the milestone that makes these tool calls; the requests made, the newest last."""
    aimed = Plan({**dict(plan(steps=["look after the shop"]).plan), "focus_milestone_id": milestone_id})  # type: ignore[arg-type]
    agent.transport.script.extend([aimed, ToolCalls(list(calls)), Reply("Done."), JOURNAL])
    agent.run_cycle("schedule")
    return rows(agent, "SELECT id, status, decided_by, decision_comment, milestone_id FROM approvals ORDER BY id")


def change(listing_id: int, **fields: Any) -> tuple[str, dict[str, Any]]:
    return ("propose_etsy_edit", {"listing_id": listing_id, "reason": "Sells better.", **fields})


def reject(agent: Any, approval_id: int) -> None:
    """The owner says no (one change of a listing waits at a time)."""
    assert owner(agent).decide(approval_id, {"decision": "reject"}, "Stefan").status == 200


def test_an_auto_unlock_carries_a_small_price_change_and_nothing_bigger(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    old = price_of(agent, listing_id)
    small, big = f"{old * Decimal('0.9'):.2f}", f"{old * Decimal('1.5'):.2f}"
    made = work_on(agent, goal, change(listing_id, price=small))  # manual: it waits for the owner as before
    assert made[-1]["status"] == "pending"
    reject(agent, made[-1]["id"])
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200
    assert owner(agent).set_autonomy(goal, {"rule": "nope", "level": "auto"}, "Stefan").status == 422
    assert owner(agent).set_autonomy(goal, {"rule": "deactivate", "level": "auto", "budget": 0}, "Stefan").status == 422
    for other in (change(listing_id, price=small, title="A new title"), change(listing_id, price=big)):
        made = work_on(agent, goal, other)  # not a price change alone, or beyond 15%: it waits for the owner
        assert made[-1]["status"] == "pending"
        reject(agent, made[-1]["id"])
    made = work_on(agent, goal, change(listing_id, price=f"{old * Decimal('0.95'):.2f}"))
    assert (made[-1]["status"], made[-1]["decided_by"]) == ("approved", policy.POLICY_BY)
    said = tool_results(agent, "propose_etsy_edit")[-1]["result"]
    assert (
        "Ember's code approved it at once: your owner unlocked price changes within 15% on a live listing for "
        f"milestone #{goal} (auto)." in said
    )
    assert agent.execute_approved() == [(made[-1]["id"], "done")]  # the executor carries it out as always
    assert price_of(agent, listing_id) == (old * Decimal("0.95")).quantize(Decimal("0.01"))
    assert rows(agent, "SELECT level, approved_at IS NOT NULL AS approved FROM policy_uses") == [
        {"level": "auto", "approved": 1}
    ]
    shown = next(m for m in agent.roadmap()["items"] if m["id"] == goal)["autonomy"]
    assert [(r["rule"], r["level"], r["used"]) for r in shown if r["level"] != "manual"] == [
        ("price_change", "auto", 1)
    ]
    assert rows(agent, f"SELECT owner_comment FROM milestones WHERE id = {goal}")[0]["owner_comment"].startswith(
        "Unlocked for this milestone: price changes within 15%"
    )


def test_a_veto_window_holds_a_request_until_the_owner_decides_or_it_passes(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    goal = a_milestone(agent)
    for rule in ("price_change", "deactivate"):
        assert owner(agent).set_autonomy(goal, {"rule": rule, "level": "veto_window"}, "Stefan").status == 200
    old = price_of(agent, listing_id)
    made = work_on(agent, goal, change(listing_id, price=f"{old * Decimal('0.95'):.2f}"))
    vetoed = made[-1]["id"]
    assert made[-1]["status"] == "pending"
    said = tool_results(agent, "propose_etsy_edit")[-1]["result"]
    assert "It is approved 12 hours from now unless your owner decides first" in said
    card = next(a for a in agent.dashboard()["approvals"] if a["id"] == vetoed)
    assert card["veto_until"] == to_iso(agent.clock.now() + timedelta(hours=policy.VETO_HOURS))
    reject(agent, vetoed)  # the owner vetoes it in its window: that unlock is taken back
    agent.run_policy()
    assert rows(agent, "SELECT rule, level, by, why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {
            "rule": "price_change",
            "level": "manual",
            "by": policy.REVOKED_BY,
            "why": f"your owner vetoed request #{vetoed}",
        }
    ]
    made = work_on(agent, goal, change(listing_id, state="deactivate"))
    held = made[-1]["id"]
    agent.run_policy()
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {held}") == [{"status": "pending"}]
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()
    assert rows(agent, f"SELECT status, decided_by FROM approvals WHERE id = {held}") == [
        {"status": "approved", "decided_by": policy.POLICY_BY}
    ]
    assert agent.execute_approved() == [(held, "done")]
    assert agent.etsy.shop().state["listings"][str(listing_id)]["state"] == etsy.STATES["deactivate"]


def test_code_revokes_an_unlock_and_only_proposes_more(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    goal, other = a_milestone(agent), a_milestone(agent, "Twenty sales")
    old = price_of(agent, listing_id)
    for n in range(policy.PROMOTE_AFTER):  # the owner approves five small price changes unchanged
        made = work_on(agent, goal, change(listing_id, price=f"{old - Decimal(n + 1) / 100:.2f}"))
        assert owner(agent).decide(made[-1]["id"], {"decision": "approve"}, "Stefan").status == 200
        agent.execute_approved()
    [suggested] = agent.roadmap()["autonomy_suggestions"]
    assert (suggested["milestone_id"], suggested["rule"], suggested["approved"]) == (goal, "price_change", 5)
    assert rows(agent, "SELECT COUNT(*) AS n FROM policy_grants") == [{"n": 0}]  # proposed, never granted by code
    # A budget of one: spent at once, then taken back
    assert (
        owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto", "budget": 1}, "Stefan").status == 200
    )
    assert agent.roadmap()["autonomy_suggestions"] == []
    made = work_on(agent, goal, change(listing_id, price=f"{old:.2f}"))
    assert agent.execute_approved() == [(made[-1]["id"], "done")]
    agent.run_policy()
    assert rows(agent, "SELECT why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"why": "its budget of 1 actions is spent"}
    ]
    # An unclear result takes it back too
    assert owner(agent).set_autonomy(other, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200

    def unclear(self: Any, listing: int, price: str) -> None:
        raise etsy.Unclear("the connection broke")

    monkeypatch.setattr(FakeShop, "set_price", unclear)
    made = work_on(agent, other, change(listing_id, price=f"{old - Decimal('0.10'):.2f}"))
    assert (made[-1]["status"], made[-1]["milestone_id"]) == ("approved", other)
    assert agent.execute_approved() == [(made[-1]["id"], "unclear")]
    agent.run_policy()
    assert rows(agent, "SELECT why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"why": f"request #{made[-1]['id']} ended unclear"}
    ]
    # A missed milestone takes back what was unlocked for it
    third = a_milestone(agent, "Thirty sales")
    assert owner(agent).set_autonomy(third, {"rule": "qa_fix", "level": "auto"}, "Stefan").status == 200
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE milestones SET status = 'missed', closed_at = ?, closed_by = 'code', result = 'x' WHERE id = ?",
            (to_iso(agent.clock.now()), third),
        )
    agent.run_policy()
    assert rows(agent, "SELECT why FROM policy_grants ORDER BY id DESC LIMIT 1") == [
        {"why": f"milestone #{third} was missed"}
    ]
