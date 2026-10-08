"""0.23.3: the last ways a venture's work went on after the owner parked or killed it. A stopped product line's
listings could still be changed, pinned, posted and bet on, and requests made for it; a new product line of no venture
listed in the channel of the parked Etsy leg; a listing the owner had approved before the park went live after it;
and research spent the parked venture's budget."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import demand, tools, ventures  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import call, listed, proposed, shop_context  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402


def line_of(agent: Any) -> tuple[int, int]:
    """The fake shop's product line and its venture (the Etsy leg)."""
    [row] = rows(
        agent,
        "SELECT a.project_id, p.venture_id FROM approvals a JOIN projects p ON p.id = a.project_id"
        " WHERE a.executor = 'etsy_listing' ORDER BY a.id LIMIT 1",
    )
    return int(row["project_id"]), int(row["venture_id"])


def park(agent: Any, venture_id: int, action: str = "park") -> None:
    assert owner(agent).decide_venture(venture_id, {"action": action, "comment": "Not now."}, "Stefan").status == 200


def test_no_change_bet_or_request_for_a_line_the_owner_parked(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    project, leg = line_of(agent)
    park(agent, leg)
    ctx = shop_context(agent)
    edit = call(ctx, "propose_etsy_edit", {"listing_id": listing, "price": "3.90", "reason": "More views."})
    assert not edit.ok and (
        f"#{listing} is a listing of venture #{leg}, which your owner parked: no change or renewal (deactivate it, if "
        "it shouldn't sell) for it until they take it up again" in edit.text
    )
    off = call(ctx, "propose_etsy_edit", {"listing_id": listing, "state": "deactivate", "reason": "It waits."})
    assert off.ok, off.text  # taking it out of the shop stays possible
    bet = call(ctx, "project_update", {"project_id": project, "bet": "+15 views in 7 days: the new title is clearer"})
    assert not bet.ok and f"bet: project #{project} belongs to venture #{leg}, which your owner parked" in bet.text
    asked = call(
        ctx,
        "request_approval",
        {
            "type": "spend_money", "title": "Refund for a buyer", "description": "x", "payload": "Refund 4.50 EUR",
            "expected_cost": "4.50 EUR", "expected_benefit": "a fair end", "project_id": project,
        },
    )  # fmt: skip
    assert asked.ok, asked.text  # your owner carries it out, and a stopped line's wind-down is work too


def test_no_pin_links_a_listing_the_owner_parked(data_dir: Path) -> None:
    from tests.test_pinterest import a_pin, pin_context  # noqa: PLC0415
    from tests.test_pinterest import listed as pinning  # noqa: PLC0415

    agent, _ = pinning(data_dir)
    _, leg = line_of(agent)
    park(agent, leg)
    pin = a_pin(agent, pin_context(agent), board_name="Meal planning printables")
    assert not pin.ok and "which your owner parked: no pin for it until they take it up again" in pin.text


def test_no_post_links_a_listing_the_owner_killed(data_dir: Path) -> None:
    from tests.test_bluesky import a_post, post_context  # noqa: PLC0415
    from tests.test_bluesky import listed as posting  # noqa: PLC0415

    agent, _ = posting(data_dir)
    _, leg = line_of(agent)
    park(agent, leg, "kill")
    post = a_post(agent, post_context(agent))
    assert not post.ok and f"is a listing of venture #{leg}, which your owner killed: no post for it" in post.text


def test_a_line_of_no_venture_doesn_t_sell_in_a_parked_channel(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    _, leg = line_of(agent)
    park(agent, leg)
    ctx = shop_context(agent)
    made = call(ctx, "project_create", {"title": "Planners again", "hypothesis": "h", "status": "idea"})
    assert made.ok, made.text
    with agent.db.transaction() as conn:
        demand.add(
            conn, ctx.scope, ctx.cycle_id, made.project_id, "weekly planner", "many buy", "https://example.com", None,
            to_iso(agent.clock.now()),
        )  # fmt: skip
    agent.roots()[0].write_bytes("shop/p2.pdf", b"%PDF-1.7 planner2")
    agent.roots()[0].write_bytes("shop/p2.png", b"\x89PNG photo2")
    listing = {
        "title": "Weekly Planner Two", "description": "A weekly planner.", "price": "4.50", "tags": "planner",
        "category_id": 1, "files": "shop/p2.pdf", "photos": "shop/p2.png", "reason": "next",
        "project_id": made.project_id,
    }  # fmt: skip
    refused = call(ctx, "propose_etsy_listing", listing)
    assert not refused.ok and (
        f"project #{made.project_id} sells in the channel of venture #{leg}, which your owner parked" in refused.text
    )
    with agent.db.transaction() as conn:  # a product line of another venture lists as before
        other = ventures.create(conn, ctx.scope, title="B", pitch="p.", stage="building", now=to_iso(agent.clock.now()))
        conn.execute("UPDATE projects SET venture_id = ? WHERE id = ?", (other, made.project_id))
        assert tools._product_line(ctx, conn, {"project_id": made.project_id}, "listing") == made.project_id


def test_what_the_owner_approved_for_a_line_isn_t_carried_out_after_the_park(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    _, leg = line_of(agent)
    park(agent, leg)  # before Ember's code made it (Etsy's daily limit, or the next round)
    [row] = rows(agent, f"SELECT status, closed_by, result_note FROM approvals WHERE id = {request}")
    assert row == {
        "status": "failed",
        "closed_by": "Ember's code",
        "result_note": f"Not carried out: your owner parked venture #{leg} before Ember's code carried it out.",
    }
    assert agent.execute_approved() == []
    assert rows(agent, "SELECT COUNT(*) AS n FROM etsy_listings WHERE status = 'active'") == [{"n": 0}]


def test_no_research_evidence_or_case_for_a_venture_the_owner_parked(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        idea = ventures.create(conn, agent.scope(), title="Idea X", pitch="p.", stage="researching", now=now)
    park(agent, idea)
    ctx = shop_context(agent)
    asked = call(ctx, "research", {"question": "Who sells X?", "venture_id": idea})
    assert not asked.ok and f"your owner parked venture #{idea}: no research for it until they take" in asked.text
    claim = {
        "claim": "Shops sell X for 15 to 25 EUR.", "metric": "price", "low": "15", "high": "25", "unit": "EUR",
        "region": "DE", "url": "https://www.example.invalid/x", "venture_id": idea,
    }  # fmt: skip
    with agent.db.transaction() as conn:  # a venture cycle's tools
        with pytest.raises(tools.ToolError, match=f"your owner parked venture #{idea}: no evidence for it"):
            tools._evidence(ctx, claim, conn)
        with pytest.raises(tools.ToolError, match=f"your owner parked venture #{idea}: no business case for it"):
            tools._venture_case(ctx, {"venture_id": idea}, conn)


def test_parking_print_on_demand_stops_printify_products_of_the_etsy_leg_s_lines(data_dir: Path) -> None:
    """The agent's product lines are the Etsy leg's, and they made Printify products: parking print on demand stopped
    none. A product venture the owner backed sells through the channel on its own word."""
    agent, _ = listed(data_dir)
    project, leg = line_of(agent)
    ctx = shop_context(agent)
    [pod] = rows(agent, "SELECT id FROM ventures WHERE channel = 'printify'")
    park(agent, pod["id"])
    with agent.db.transaction() as conn:
        with pytest.raises(tools.ToolError, match=f"sells in the channel of venture #{pod['id']}, which your owner"):
            tools._product_line(ctx, conn, {"project_id": project}, "product")
        assert tools._product_line(ctx, conn, {"project_id": project}, "listing") == project  # the leg sells on
        assert ventures.project_stopped(conn, ctx.scope, project) is None  # it sells in Etsy's channel too
        backed = ventures.create(
            conn, ctx.scope, title="B", pitch="p.", stage="building", now=to_iso(agent.clock.now())
        )
        conn.execute("UPDATE projects SET venture_id = ? WHERE id = ?", (backed, project))
        assert tools._product_line(ctx, conn, {"project_id": project}, "product") == project
    assert leg


def test_an_unlock_doesn_t_approve_stopped_work_when_its_veto_window_passes(data_dir: Path) -> None:
    """A line of no venture sells in the Etsy leg's channel: its milestone isn't the leg's, so the park didn't drop it,
    and its veto-window unlock approved a price change after the park."""
    from decimal import Decimal  # noqa: PLC0415

    from app.agent import policy  # noqa: PLC0415
    from tests.test_policy import a_milestone, change, price_of, work_on  # noqa: PLC0415

    agent, listing = listed(data_dir)
    project, leg = line_of(agent)
    with agent.db.transaction() as conn:  # listed while its channel had no venture it could join
        conn.execute("UPDATE projects SET venture_id = NULL WHERE id = ?", (project,))
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "veto_window"}, "Stefan").status == 200
    price = price_of(agent, listing)
    made = work_on(agent, goal, change(listing, price=f"{price * Decimal('0.95'):.2f}"))[-1]
    assert made["status"] == "pending"
    park(agent, leg)
    agent.clock.advance(hours=policy.VETO_HOURS, minutes=1)
    agent.run_policy()
    [row] = rows(agent, f"SELECT status, decision_comment FROM approvals WHERE id = {made['id']}")
    assert row["status"] == "pending" and f"your owner parked venture #{leg}, whose work it" in row["decision_comment"]
    assert agent.execute_approved() == [] and price_of(agent, listing) == price


def test_the_owner_s_undo_isn_t_held_by_their_park(data_dir: Path) -> None:
    from decimal import Decimal  # noqa: PLC0415

    from tests.test_audit import feed, undo  # noqa: PLC0415
    from tests.test_policy import a_milestone, change, price_of, work_on  # noqa: PLC0415

    agent, listing = listed(data_dir)
    _, leg = line_of(agent)
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "auto"}, "Stefan").status == 200
    price = price_of(agent, listing)
    work_on(agent, goal, change(listing, price=f"{(price * Decimal('1.10')).quantize(Decimal('0.01'))}"))
    agent.execute_approved()
    back = undo(agent, feed(agent)[0]["id"])  # the owner: change it back
    park(agent, leg)  # and park the venture, in the same minute
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {back}") == [{"status": "approved"}]
    agent.execute_approved()
    assert price_of(agent, listing) == price


def test_no_blog_post_recommends_a_listing_the_owner_parked(data_dir: Path) -> None:
    from app.integrations import etsy  # noqa: PLC0415
    from tests.test_blog import POST, blog_context  # noqa: PLC0415

    agent, listing = listed(data_dir)
    _, leg = line_of(agent)
    park(agent, leg)
    agent.roots()[0].write(
        "blog/post.md", POST.replace("https://www.etsy.com/listing/4584899301", etsy.listing_url(listing))
    )
    made = call(blog_context(agent), "propose_blog_post", {"source": "blog/post.md", "reason": "Search traffic."})
    assert not made.ok and "which your owner parked: no blog post recommending it for it" in made.text


def test_a_failed_listing_attempt_isn_t_selling_in_a_channel(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    project, leg = line_of(agent)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE projects SET venture_id = NULL WHERE id = ?", (project,))
        conn.execute("UPDATE etsy_listings SET listing_id = NULL, status = 'failed'")
    park(agent, leg, "kill")
    with agent.db.connection() as conn:
        assert ventures.project_stopped(conn, agent.scope(), project) is None
        assert ventures.project_stopped(conn, agent.scope(), project, "etsy")["id"] == leg


def test_requests_approved_before_an_earlier_park_are_held_at_the_first_start(data_dir: Path) -> None:
    """0.23.2 didn't hold them at the park: the first start of 0.23.3 does, once."""
    from app.agent import service  # noqa: PLC0415

    agent, _, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    _, leg = line_of(agent)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:  # parked as 0.23.2 did it, and before 0.23.3 started
        ventures.update(conn, leg, now, stage="parked", parked_by="owner")
        conn.execute("DELETE FROM meta WHERE key LIKE '%parks_held_0233'")
    agent.recover()
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {request}") == [{"status": "failed"}]
    with agent.db.transaction() as conn:
        assert service.held_once(conn, agent.scope(), now) == []  # once
