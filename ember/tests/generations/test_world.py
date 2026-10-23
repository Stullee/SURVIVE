"""0.46.0 (vision/generations.md): Ember's mind and the world she works in have sessions of their own. A generation
change gives her a new mind (her projects, ventures, requests, cases and playbook start empty) in the same world: the
shop and its listings, the boards, pins and posts, the website and blog, the mailbox and its opt-outs, the owner's
rulebook and library. Until a generation change the two sessions are the same, so nothing reads otherwise.

A plain new session would have cut her off from the real world: the Etsy sync read no orders (it asks Etsy for the
listings in its session), the opt-outs were forgotten, a listing fee noted before was dropped unbooked, and KDP's
weekly limit counted from zero. These tests begin a new mind the way a generation change will (``next_mind``) and
check each."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import library, never, store  # noqa: E402
from app.agent.sandbox import Jail  # noqa: E402
from app.agent.store import AgentScope  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.economy.life import LIVE_SESSION_KEY, SESSION_NO_KEY, WORLD_KEY, Life  # noqa: E402
from app.integrations import etsy, etsy_publisher, etsy_revenue, kdp, mailstore  # noqa: E402
from tests.agent_helpers import make_agent, owner, rows  # noqa: E402
from tests.etsy_helpers import listed  # noqa: E402

LISTING = 900_000_001  # the fake shop's first listing (etsy_helpers.listed)


def next_mind(agent: Any) -> AgentScope:
    """A new mind in the world the agent works in, as a generation change begins it: the mind's session counts up,
    the world's stays."""
    before = agent.scope()
    if before.mode == "live":
        agent.db.set_meta(LIVE_SESSION_KEY, str(before.session + 1))
    else:
        agent.db.set_meta(WORLD_KEY, str(before.world_session))
        agent.db.set_meta(SESSION_NO_KEY, str(before.session + 1))
    after = agent.scope()
    assert (after.session, after.world_session) == (before.session + 1, before.world_session)
    return after


def request(agent: Any, scope: AgentScope, executor: str, action: dict[str, Any]) -> sqlite3.Row:
    """A request of ``scope``'s mind, as a tool makes it (filed under the newest cycle)."""
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        approval_id = store.insert_approval(
            conn,
            scope,
            cycle,
            now,
            type="sell" if executor == kdp.EXECUTOR else "publish",
            title="A request",
            description="What it is.",
            payload=json.dumps(action),
            expected_cost="nothing",
            expected_benefit="a test",
            executor=executor,
            action=store.canonical(action),
        )
        row: sqlite3.Row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return row


def test_the_mind_and_the_world_are_one_session_until_a_new_mind(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    first = agent.scope()
    assert first.world_session == first.session and first.world is not None
    assert first.world_where() == first.where()
    mind = next_mind(agent)
    assert mind.where() == ("mode = ? AND session = ?", ("dry_run", first.session + 1))
    assert mind.world_where() == ("mode = ? AND session = ?", ("dry_run", first.session))
    assert mind.minds_where("a") == ("a.mode = ? AND a.session >= ?", ("dry_run", first.session))
    # a scope built before there was a world (a row's own, a test's) has its mind's
    assert AgentScope("live", 0, 1).world_session == 0


def test_live_the_world_stays_0_while_the_mind_counts_generations(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    life = Life(agent.db, agent.settings, agent.clock, agent.economy.books, "live")
    assert (life.session(), life.world()) == (0, 0)  # every install's first generation
    agent.db.set_meta(LIVE_SESSION_KEY, "1")
    assert (life.session(), life.world()) == (1, 0)
    assert agent.economy.life.session() == agent.scope().session  # dry run doesn't read the live counter


def test_a_new_dry_run_session_begins_a_new_world_too(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    scope = agent.scope()
    assert agent.db.get_meta(WORLD_KEY) == str(scope.session)  # written when the dry-run session began


def test_the_shop_carries_over_and_her_mind_starts_empty(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    old = agent.scope()
    with agent.db.connection() as conn:
        assert store.open_projects(conn, old)
    scope = next_mind(agent)
    with agent.db.connection() as conn:
        assert store.open_projects(conn, scope) == []  # her predecessor's lines are not hers
        assert store.pending_requests(conn, scope) == []
        assert etsy_publisher.listing_row(conn, scope, listing) is not None
        assert etsy_publisher.current_listing(conn, scope, listing) is not None
        assert f"#{listing}" in etsy_publisher.shop_text(conn, scope, agent.clock, "EmberTestShop", 5)
        assert etsy_publisher.created_today(conn, agent.clock, scope) == 1  # Etsy's day counts the shop's
    # the sync still asks Etsy for the shop's listings and reads their numbers
    shop = agent.etsy.shop()
    asked: list[list[int]] = []
    listings = shop.listings

    def read(ids: list[int]) -> Any:
        asked.append(list(ids))
        return listings(ids)

    shop.listings = read  # type: ignore[method-assign]
    assert agent.publisher.sync(force=True) is None
    assert asked and listing in asked[0]
    [row] = rows(agent, f"SELECT session, synced_at FROM etsy_listings WHERE listing_id = {listing}")
    assert row["session"] == old.session and row["synced_at"] is not None


def test_an_order_after_a_new_mind_is_stored_and_its_revenue_recorded(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    next_mind(agent)
    shop = agent.etsy.shop()
    line = {"listing_id": listing, "title": "Ember's planner", "quantity": 1, "price_cents": 450}
    ordered = to_iso(agent.clock.now() - timedelta(minutes=5))
    shop.orders = lambda since: [etsy.Order(71, ordered, 450, "EUR", [line], items_cents=450, status="paid")]  # type: ignore[method-assign]
    shop.payment_fees = lambda receipt_id: 48  # type: ignore[method-assign]
    agent.publisher.settings = agent.settings.model_copy(
        update={"etsy_auto_record_revenue": True, "etsy_usd_per_eur": 1.1}
    )
    assert agent.publisher.sync(force=True) is None  # stores the order (0.12.0: the revenue comes the sync after)
    assert agent.publisher.sync(force=True) is None
    [order] = rows(agent, "SELECT session FROM etsy_orders WHERE receipt_id = 71")
    assert order["session"] == agent.scope().world_session
    revenue = rows(
        agent,
        "SELECT amount_micros FROM ledger WHERE created_by = 'etsy' AND type = 'revenue' AND source = 'Etsy order 71'",
    )
    assert revenue == [{"amount_micros": 4_950_000}]


def due_fees(agent: Any) -> int:
    key = etsy_revenue.FEE_DUE_KEY
    return int(rows(agent, f"SELECT COUNT(*) AS n FROM meta WHERE substr(key, 1, {len(key)}) = '{key}'")[0]["n"])


def test_the_listing_fees_noted_by_the_old_mind_are_booked_by_the_new_one(data_dir: Path) -> None:
    agent, listing = listed(data_dir)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        etsy_publisher.listing_fee_due(
            conn, "dry_run", agent.scope().world_session, listing, "renewal-test", now, "it renewed itself"
        )
    booked = rows(agent, f"SELECT COUNT(*) AS n FROM ledger WHERE source = 'Etsy listing {listing}'")[0]["n"]
    due = due_fees(agent)
    assert due >= 2  # publishing it and the renewal, not booked yet
    next_mind(agent)
    assert agent.publisher.sync(force=True) is None
    agent._etsy_numbers(agent.scope(), agent.settings)
    # 0.46.0: before, a fee noted in another session was dropped unbooked
    assert due_fees(agent) == 0
    assert rows(agent, f"SELECT COUNT(*) AS n FROM ledger WHERE source = 'Etsy listing {listing}'")[0]["n"] == (
        booked + due
    )


def test_an_opt_out_holds_for_every_generation(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        assert mailstore.suppress(conn, agent.scope(), "Someone@Example.org", now, "asked to stop", None)
    scope = next_mind(agent)
    with agent.db.connection() as conn:
        assert mailstore.is_suppressed(conn, scope, "someone@example.org")
        assert mailstore.suppressions(conn, scope)[0] == 1


def test_kdps_weekly_limit_counts_her_predecessors_books(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    old = agent.scope()
    for title in ("Haushaltsbuch 2027", "Kassenbuch 2027"):
        request(agent, old, kdp.EXECUTOR, {"format": "paperback", "title": title})
    scope = next_mind(agent)
    with agent.db.connection() as conn:
        assert kdp.weekly(conn, scope, agent.clock.now())["paperback"] == 2  # Amazon's account, not her mind, counts


def test_never_reads_the_world_a_new_mind_works_in(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    scope = next_mind(agent)
    row = request(agent, scope, "etsy_listing", {"title": "A second listing"})
    with agent.db.connection() as conn:
        assert "first_publication" not in never.reasons(conn, row)  # the shop has a live listing
        [view] = conn.execute(
            "SELECT first_publication FROM approvals_never WHERE approval_id = ?", (row["id"],)
        ).fetchall()
    assert view["first_publication"] == 0
    # someone who wrote to her predecessor is no first contact
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO emails (mode, session, direction, uidvalidity, uid, message_id, from_addr, to_addr, subject,"
            " received_at, body, bulk, authenticated) VALUES ('dry_run', ?, 'in', 1, 1, '<m1@example.org>',"
            " 'buyer@example.org', 'ember@example.org', 'A question', ?, 'Hello', 0, 1)",
            (scope.world_session, now),
        )
    email = request(agent, scope, "email", {"to": "buyer@example.org", "subject": "Re: A question", "body": "Hi"})
    with agent.db.connection() as conn:
        assert "first_contact" not in never.reasons(conn, email)
        assert mailstore.has_written(conn, scope, "buyer@example.org")
        [view] = conn.execute(
            "SELECT first_contact FROM approvals_never WHERE approval_id = ?", (email["id"],)
        ).fetchall()
    assert view["first_contact"] == 0


def test_never_still_holds_a_first_listing_in_a_world_without_one(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    with agent.db.transaction() as conn:
        conn.execute(  # a cycle to file the request under
            "INSERT INTO cycles (session, simulated, life_id, boot_id, cap_micros, started_at, status, trigger)"
            " VALUES (?, 1, ?, 'test', 0, ?, 'completed', 'schedule')",
            (agent.scope().session, agent.scope().life_id, to_iso(agent.clock.now())),
        )
    scope = next_mind(agent)
    row = request(agent, scope, "etsy_listing", {"title": "A first listing"})
    with agent.db.connection() as conn:
        assert "first_publication" in never.reasons(conn, row)


def test_the_owners_rulebook_and_library_carry_over(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    owner(agent).add_rule({"text": "Write to me at most once a day."}, "Owner")
    owner(agent).add_document({"title": "Pricing notes", "text": "Buyers compare prices. " * 40}, "Owner")
    scope = next_mind(agent)
    with agent.db.connection() as conn:
        assert [r["text"] for r in store.rules(conn, scope)] == ["Write to me at most once a day."]
        assert [d["title"] for d in library.documents(conn, scope)] == ["Pricing notes"]
    owner(agent).add_rule({"text": "Ship one title before its variants."}, "Owner")  # the new mind's owner adds one
    with agent.db.connection() as conn:
        assert len(store.rules(conn, scope)) == 2


def test_a_later_generations_kept_texts_stay_while_its_files_are_there(data_dir: Path) -> None:
    """The daily pruning looked the live workspace up as session 0's: a later generation's kept texts would have been
    pruned 14 days after their newest, as a file gone."""
    agent, _ = make_agent(data_dir, [])
    agent.db.set_meta(LIVE_SESSION_KEY, "1")  # the live mind's second generation
    Jail(data_dir / "workspace").write("books/live.md", "# live\n")
    old = to_iso(agent.clock.now() - timedelta(days=40))
    with agent.db.transaction() as conn:
        for path in ("books/live.md", "books/gone.md"):
            conn.execute(
                "INSERT INTO workspace_versions (mode, session, path, content, reason, created_at)"
                " VALUES ('live', 1, ?, 'Kept.', 'overwrite', ?)",
                (path, old),
            )
    assert agent.prune_versions() == 1  # the file that is gone
    assert [r["path"] for r in rows(agent, "SELECT path FROM workspace_versions WHERE mode = 'live'")] == [
        "books/live.md"
    ]
