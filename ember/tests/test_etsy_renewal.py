"""0.12.0: Etsy listings end after four months. They were created without automatic renewal and Ember had no renew or
deactivate action, so every listing, the ones that sell too, expired unseen and still counted as live. Now the sync
reads when each ends, Etsy's state counts, a change can renew or deactivate a listing, and a listing that sold gets
Etsy's automatic renewal (once: the owner's own choice at Etsy stands after it)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.economy.clock import from_iso, to_iso  # noqa: E402
from app.integrations import etsy, etsy_publisher  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import a_change, call, listed, live_shop, shop_context, views_approval  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

LISTING = "900000001"


def listing(agent: Any) -> dict[str, Any]:
    return rows(agent, "SELECT state, ends_at, auto_renew, renew_set_at, title FROM etsy_listings")[0]


def fake(agent: Any) -> dict[str, Any]:
    return agent.etsy.shop().state["listings"][LISTING]


def shop_text(agent: Any) -> str:
    with agent.db.connection() as conn:
        return etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "EmberTestShop", 3)


def approve(agent: Any, request: int) -> None:
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200


def test_an_expired_listing_isnt_live_and_is_renewed_with_a_change(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    ends = to_iso(from_iso(fake(agent)["live_since"]) + timedelta(days=etsy.LISTING_DAYS))
    assert agent.publisher.sync(force=True) is None
    assert (listing(agent)["state"], listing(agent)["ends_at"], listing(agent)["auto_renew"]) == ("active", ends, 0)
    assert f"\nNewest:\n- #{listing_id} [active] " in shop_text(agent) and f" · ends {ends[:10]}" in shop_text(agent)
    agent.clock.advance(days=etsy.LISTING_DAYS + 1)
    assert agent.publisher.sync(force=True) is None
    assert listing(agent)["state"] == "expired"
    ctx = shop_context(agent)
    assert (
        call(ctx, "etsy_listing", {}).text
        == f"You have no live listings. Not live at Etsy: #{listing_id} expired on {ends[:10]}."
    )
    assert f"\nAt Etsy: expired on {ends[:10]}\nTitle: " in call(ctx, "etsy_listing", {"listing_id": listing_id}).text
    assert (
        f"\nNot live at Etsy: #{listing_id} (expired on {ends[:10]}). Renew one worth selling again with "
        "propose_etsy_edit (state renew, USD 0.20).\n"
    ) in shop_text(agent)
    assert "All 1 live listings" not in shop_text(agent)  # it counted as live
    for args, error in (
        ({"title": "A sharper title"}, f"#{listing_id} isn't live at Etsy (expired on {ends[:10]}): renew it"),
        ({"state": "deactivate"}, "isn't live at Etsy"),
    ):
        refused = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "reason": "r", **args})
        assert not refused.ok and error in refused.text, refused.text
    request = a_change(agent, ctx, listing_id, state="renew", title="A sharper title")
    said = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'propose_etsy_edit' AND status = 'ok'")[0]["result"]
    assert said.startswith(
        f"Approval request #{request} is waiting for your owner: it renews #{listing_id} and changes"
    )
    approval = rows(agent, f"SELECT title, expected_cost, payload FROM approvals WHERE id = {request}")[0]
    assert approval["title"].startswith("Renew Etsy listing: ")
    assert approval["expected_cost"].startswith("Etsy's listing fee for the renewal (USD 0.20 at most)")
    assert (
        "\nRenew it: it goes live at Etsy again for four months (USD 0.20 at most).\n"
        f"  (now: expired on {ends[:10]})\nTitle: A sharper title\n"
    ) in approval["payload"]
    assert views_approval(agent, request)["editable"] == "Title: A sharper title"  # the words stay the owner's to edit
    approve(agent, request)
    assert agent.execute_approved() == [(request, "done")]
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    assert note == "Changed in the dry run's fake shop (renewal, title); nothing reached Etsy."
    assert (listing(agent)["state"], listing(agent)["title"]) == ("active", "A sharper title")
    assert fake(agent)["state"] == "active" and fake(agent)["renewals"] == 1
    assert agent.publisher.sync(force=True) is None
    renewed = to_iso(agent.clock.now() + timedelta(days=etsy.LISTING_DAYS))
    assert (listing(agent)["state"], listing(agent)["ends_at"]) == ("active", renewed)
    assert call(ctx, "etsy_listing", {}).text.startswith("Your live listings (newest first):\n- #900000001 ")


def test_a_live_listing_is_deactivated_on_its_own_and_renewed_later(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    ends = listing(agent)["ends_at"]
    ctx = shop_context(agent)
    for args, error in (
        ({"state": "renew"}, f"#{listing_id} is live until {ends[:10]} at Etsy: only an expired, sold-out or"),
        ({"state": "deactivate", "price": "3.90"}, "deactivate on its own"),
    ):
        refused = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "reason": "r", **args})
        assert not refused.ok and error in refused.text, refused.text
    request = a_change(agent, ctx, listing_id, state="deactivate")
    approval = rows(agent, f"SELECT title, expected_cost, payload FROM approvals WHERE id = {request}")[0]
    assert approval["title"].startswith("Deactivate Etsy listing: ")
    assert approval["expected_cost"] == "none: Etsy charges nothing for changing a listing"
    assert approval["payload"].endswith(
        f"\nDeactivate it: buyers no longer find it at Etsy; it can be renewed later.\n  (now: live until {ends[:10]})"
    )
    assert views_approval(agent, request)["editable"] is None  # approve or reject: no words to change
    approve(agent, request)
    assert agent.execute_approved() == [(request, "done")]
    assert listing(agent)["state"] == fake(agent)["state"] == "inactive"
    assert agent.publisher.sync(force=True) is None
    assert listing(agent)["state"] == "inactive"
    assert (
        call(ctx, "etsy_listing", {}).text == f"You have no live listings. Not live at Etsy: #{listing_id} deactivated."
    )
    again = a_change(agent, ctx, listing_id, state="renew")
    approve(agent, again)
    assert agent.execute_approved() == [(again, "done")]
    assert fake(agent)["state"] == "active" and "renewals" not in fake(agent)  # before its end: reactivated only
    assert agent.publisher.sync(force=True) is None
    assert (listing(agent)["state"], listing(agent)["ends_at"]) == ("active", ends)


def sale(agent: Any, listing_id: int, quantity: int) -> None:
    scope, now = agent.scope(), to_iso(agent.clock.now())
    items = json.dumps([{"listing_id": listing_id, "title": "t", "quantity": quantity, "price_cents": 450}])
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, total, total_cents, currency, items,"
            " synced_at, status) VALUES (?, ?, 1, ?, '4.50 EUR', 450, 'EUR', ?, ?, 'paid')",
            (scope.mode, scope.session, now, items, now),
        )


def test_a_listing_that_sold_renews_itself_once_ember_turned_it_on(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    assert agent.publisher.sync(force=True) is None
    assert listing(agent)["auto_renew"] == 0 and fake(agent)["auto_renew"] is False  # created without it
    sale(agent, listing_id, 2)
    assert agent.publisher.sync(force=True) is None
    assert listing(agent)["auto_renew"] == 1 and listing(agent)["renew_set_at"] == to_iso(agent.clock.now())
    assert fake(agent)["auto_renew"] is True
    events = [e["message"] for e in agent.db.recent_events(limit=10)]
    assert (
        "Listing #900000001 sold 2 time(s): Ember turned on automatic renewal at the dry run's fake shop (USD 0.20"
        " every four months)"
    ) in events
    assert f"- #{listing_id} [active] " in shop_text(agent) and " · renews itself" in shop_text(agent)
    agent.clock.advance(days=etsy.LISTING_DAYS + 1)
    assert agent.publisher.sync(force=True) is None
    assert listing(agent)["state"] == "active" and fake(agent)["renewals"] == 1  # it didn't expire
    # The owner turns it off at Etsy: that stands.
    agent.etsy.shop().set_auto_renew(listing_id, False)
    assert agent.publisher.sync(force=True) is None
    assert listing(agent)["auto_renew"] == 0 and fake(agent)["auto_renew"] is False
    assert sum("turned on automatic renewal" in e["message"] for e in agent.db.recent_events(limit=20)) == 1


def test_the_owner_can_keep_listings_from_renewing_themselves(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    agent.publisher.settings = agent.settings.model_copy(update={"etsy_auto_renew_sold": False})
    sale(agent, listing_id, 1)
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    assert (listing(agent)["auto_renew"], listing(agent)["renew_set_at"]) == (0, None)
    assert fake(agent)["auto_renew"] is False


def test_a_refused_renewal_is_reported_once(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, listing_id = listed(data_dir)
    sale(agent, listing_id, 1)
    tries: list[int] = []

    def refuse(self: etsy.FakeShop, listing_id: int, on: bool) -> None:
        tries.append(listing_id)
        raise etsy.NotSent("HTTP 403: not allowed")

    monkeypatch.setattr(etsy.FakeShop, "set_auto_renew", refuse)
    assert agent.publisher.sync(force=True) is None and agent.publisher.sync(force=True) is None
    assert tries == [listing_id]  # tried once, never every hour
    assert listing(agent)["auto_renew"] == 0 and listing(agent)["renew_set_at"] is not None
    events = [e["message"] for e in agent.db.recent_events(limit=10)]
    assert (
        "Listing #900000001 sold, but Ember couldn't turn on its automatic renewal (HTTP 403: not allowed): turn it"
        " on at Etsy, or it expires after four months"
    ) in events


def test_the_live_shop_reads_ends_and_sets_state_and_renewal(tmp_path: Path) -> None:
    ending = 1_790_000_000
    path = "/v3/application/shops/777/listings/5"
    shop, server = live_shop(
        tmp_path,
        {
            ("GET", "/v3/application/listings/batch"): {
                "results": [
                    {
                        "listing_id": 5,
                        "state": "expired",
                        "title": "Planner",
                        "url": "https://www.etsy.com/listing/5",
                        "views": 30,
                        "num_favorers": 2,
                        "ending_timestamp": ending,
                        "should_auto_renew": False,
                    },
                    {"listing_id": 6, "state": "active", "title": "Other"},
                ]
            },
            ("PATCH", path): lambda request: {"listing_id": 5, "state": "active"},
        },
    )
    expired, other = shop.listings([5, 6])
    assert (expired.state, expired.ends_at, expired.auto_renew) == (
        "expired",
        to_iso(datetime.fromtimestamp(ending, tz=UTC)),
        False,
    )
    assert (other.ends_at, other.auto_renew) == (None, None)  # not given: not known
    assert shop.set_state(5, "active") == "active"
    shop.set_auto_renew(5, True)
    assert [(r.method, r.url.path) for r in server.requests[1:]] == [("PATCH", path), ("PATCH", path)]
    assert server.form(1) == {"state": "active"} and server.form(2) == {"should_auto_renew": "true"}
