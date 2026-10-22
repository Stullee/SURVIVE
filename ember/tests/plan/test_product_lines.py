"""The product line a listing, a check or a step belongs to: a listing moves to the line it is, a channel's
line has no pins or posts of its own, and a line with an Etsy listing is no KDP book.

Tests from 0.19.2, 0.39.1, 0.39.8 and 0.40.2 (formerly test_fixes_0192.py, test_fixes_0391.py, test_fixes_0398.py,
test_fixes_0402.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.agent import (  # noqa: E402
    plan,
    reach,
    templates,
    tools,
    ventures,
)
from app.agent.service import Agent  # noqa: E402
from app.agent.tools import Outcome  # noqa: E402
from tests.agent_helpers import rows  # noqa: E402
from tests.etsy_helpers import call, shop_context  # noqa: E402
from tests.plan.test_cycle_line import (  # noqa: E402
    cycle,
    lined,
    now,
    working,
)
from tests.plan.test_marketing_order import critic, launched  # noqa: E402
from tests.plan.test_owner_holds import first_marketing_out, waits, weekly  # noqa: E402
from tests.plan.test_plan_tree import (  # noqa: E402
    ALL,
    keep,
    project,
)
from tests.plan.test_step_brake import made as used  # noqa: E402

# --- From 0.19.2 (formerly test_fixes_0192.py) ---


# --- a listing joins the right product line ------------------------------------------------------------------------


def test_a_focus_project_of_another_venture_doesnt_take_the_cycles_listing(data_dir: Path) -> None:
    from tests.etsy_helpers import listed as etsy_listed  # noqa: PLC0415

    agent, _ = etsy_listed(data_dir)
    ctx = shop_context(agent)
    with agent.db.connection() as conn:
        project = conn.execute("SELECT project_id FROM approvals WHERE executor = 'etsy_listing'").fetchone()[0]
        venture = conn.execute("SELECT venture_id FROM projects WHERE id = ?", (project,)).fetchone()[0]
    assert venture is not None
    ctx.state.focus_project_id, ctx.state.focus_venture_id = project, venture + 100
    with agent.db.connection() as conn, pytest.raises(tools.ToolError) as refused:
        tools._product_line(ctx, conn, {}, "listing")
    assert f"your focus project #{project} belongs to venture #{venture}, and this cycle works on venture" in str(
        refused.value
    )
    ctx.state.focus_venture_id = venture
    with agent.db.connection() as conn:
        assert tools._product_line(ctx, conn, {}, "listing") == project


# --- From 0.39.1 (formerly test_fixes_0391.py) ---


# --- 4. a channel's line ---


def test_a_channels_line_has_no_pins_or_posts_of_its_own(data_dir: Path) -> None:
    """Live, the Pinterest test's line got a Bluesky step the minute the channel was set up, refused: its line has no
    listing for a post to link."""
    agent, _ = lined(data_dir, titles=("Pinterest boards for the shop",))
    keep(agent)
    [line] = rows(agent, "SELECT id FROM projects")
    with agent.db.transaction() as conn:  # the channel is set up, and a pin step as 0.38.1 laid it out
        [maintain] = plan.nodes(
            conn, agent.scope(), "project_id = ? AND level = 'stage' AND stage = 'maintain'", (line["id"],)
        )
        for stage in plan.nodes(
            conn, agent.scope(), "project_id = ? AND level = 'stage' AND stage <> 'maintain'", (line["id"],)
        ):
            for step in plan.nodes(conn, agent.scope(), "parent_id = ? AND status = 'open'", (stage["id"],)):
                plan._close(conn, step["id"], now(agent), "done", "set up", by="owner")
            plan._close(conn, stage["id"], now(agent), "done", "set up", by="owner")
        pin = plan._insert(
            conn,
            agent.scope(),
            now(agent),
            parent_id=maintain["id"],
            level="step",
            project_id=line["id"],
            template="recurring/pinterest",
            stage="maintain",
            kind="market",
            channel="pinterest",
            title="Pin it this week",
            source="code",
        )
    said = keep(agent)
    assert (
        f"Plan tree: line #{line['id']} is a channel: its steps #{pin} for pins and posts of its own dropped (its"
        " products' marketing is theirs)."
    ) in said
    assert rows(agent, f"SELECT status, result FROM plan_nodes WHERE id = {pin}") == [
        {"status": "dropped", "result": plan.CHANNEL_DUTY}
    ]
    keep(agent)
    assert rows(
        agent,
        f"SELECT COUNT(*) AS n FROM plan_nodes WHERE project_id = {line['id']} AND status = 'open' AND level = 'step'",
    ) == [{"n": 0}]


# --- From 0.39.8 (formerly test_fixes_0398.py) ---


# --- 2. a listing moves to the line it is ---


def move(agent: Agent, line: int, listing: int, note: str = "The bundle is this line's only listing") -> Outcome:
    return call(working(agent, line), "project_update", {"project_id": line, "listing_id": listing, "note": note})


def test_a_listing_made_by_another_lines_request_moves_to_its_line(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    other = project(agent, "Commercial licence pack for coaches")
    keep(agent)
    ctx = working(agent, other)
    with agent.db.transaction() as conn, pytest.raises(tools.ToolError) as refused:
        tools._listing_line(ctx, conn, listing, "post")  # noqa: SLF001
    assert f"no post for project #{line}, whose work waits for a cycle of its own" in str(refused.value)
    assert f"Listing #{listing} counts for line #{line}, the line of the request that made it" in str(refused.value)
    assert f"project_update with project_id {other}, listing_id {listing}" in str(refused.value)

    moved = move(agent, other, listing)
    assert moved.ok, moved.text
    assert f"Listing #{listing} counts for line #{other} from now on (it counted for line #{line})" in moved.text
    with agent.db.transaction() as conn:
        assert ventures.listing_project(conn, agent.scope(), listing) == other
        funnels = reach.funnels(conn, agent.scope())
        tools._listing_line(working(agent, other), conn, listing, "post")  # noqa: SLF001 - its line's now
    assert funnels[other].listings == [listing] and line not in funnels
    assert rows(agent, "SELECT listing_id, project_id, moved_from FROM listing_lines") == [
        {"listing_id": listing, "project_id": other, "moved_from": line}
    ]
    assert any(f"moved listing #{listing} from line #{line}" in e["message"] for e in agent.db.recent_events(10))


def test_a_listing_moves_only_with_why_only_ones_own_and_not_out_of_a_line_the_owner_holds(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    other = project(agent, "Commercial licence pack for coaches")
    keep(agent)
    assert "say in note why" in move(agent, other, listing, note="").text
    assert "isn't one of yours" in move(agent, other, 123_456).text
    assert "counts for line" in move(agent, line, listing).text and "already" in move(agent, line, listing).text
    [product] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    with agent.db.transaction() as conn:
        plan.owner_hold(conn, agent.scope(), product["id"], "I look at it next week", "Owner", now(agent))
    held = move(agent, other, listing)
    assert not held.ok and f"your owner holds line #{line}" in held.text
    assert not rows(agent, "SELECT id FROM listing_lines")


def test_a_line_without_a_listing_of_its_own_gets_no_weekly_pin_or_post(data_dir: Path) -> None:
    agent, line, listing = launched(data_dir)
    first_marketing_out(agent, line, ("pin", "post", "blog"))
    critic(agent, line, listing, 8)
    agent.clock.advance(days=1)
    keep(agent)
    week = weekly(agent, line)
    assert week and all(waiting is None for waiting in waits(agent, line, ALL).values())
    other = project(agent, "Commercial licence pack for coaches")
    keep(agent)
    assert move(agent, other, listing).ok  # the line has no listing of its own now
    assert {waits(agent, line, ALL)[s["title"]] for s in week} == {"listing"}
    with agent.db.transaction() as conn:
        for step in week:
            plan._close(conn, step["id"], now(agent), "done", "test")  # noqa: SLF001
    agent.clock.advance(days=31)  # past every period
    keep(agent)
    assert not [s for s in weekly(agent, line) if s["status"] == "open"]


# --- From 0.40.2 (formerly test_fixes_0402.py) ---


COACHES = "Commercial licence pack for coaches"


# --- 7. a line with an Etsy listing is no KDP book ---


def test_a_check_of_a_book_doesnt_make_a_line_with_an_etsy_listing_a_kdp_book(data_dir: Path) -> None:
    """Live, line #12, the coach licence bundle's Etsy listing, became a KDP book on 10-19: a check of the
    Haushaltsbuch had run in one of its cycles."""
    agent, line, listing = launched(data_dir)
    coaches = project(agent, COACHES)
    used(agent, cycle(agent, coaches), "propose_kdp_book")  # a check of another line's book, in its cycle
    keep(agent)
    [product] = rows(agent, f"SELECT id, template FROM plan_nodes WHERE level = 'product' AND project_id = {coaches}")
    assert templates.by_key(product["template"]).name == templates.KDP_BOOK.name  # its tools alone, as until 0.40.2
    fix = {"title": "Fix the cover letter, the licence and two tags", "kind": "fix"}
    with agent.db.transaction() as conn:  # the critic's fix, added under the KDP stages
        plan.add_steps(conn, agent.scope(), coaches, [fix], "the critic's findings", None, now(agent))
    [book_step] = rows(agent, f"SELECT id FROM plan_nodes WHERE project_id = {coaches} AND title = '{fix['title']}'")
    assert move(agent, coaches, listing).ok  # the bundle's listing is this line's
    said = keep(agent)
    assert (
        f"Plan tree: line #{coaches} holds an Etsy listing: it is no KDP book. Its KDP stages are closed, and the steps"
        " added there move to its product's stages."
    ) in said
    [after] = rows(agent, f"SELECT template FROM plan_nodes WHERE id = {product['id']}")
    assert after["template"] == templates.GENERIC.key
    stages = rows(agent, f"SELECT stage FROM plan_nodes WHERE parent_id = {product['id']} AND status = 'open'")
    assert {s["stage"] for s in stages} <= {s.stage for s in templates.GENERIC.stages}
    assert not rows(  # the KDP template's steps are gone
        agent, f"SELECT id FROM plan_nodes WHERE project_id = {coaches} AND status = 'open' AND template LIKE 'kdp%'"
    )
    titled = f"project_id = {coaches} AND title = '{fix['title']}' AND id > {book_step['id']}"
    [moved] = rows(agent, f"SELECT id, status FROM plan_nodes WHERE {titled}")
    assert moved["status"] == "open"  # the fix moved along
    assert rows(agent, f"SELECT actor FROM plan_changes WHERE node_id = {product['id']} AND why LIKE '%Etsy listing%'")
    assert not [s for s in keep(agent) if "KDP" in s]  # once
    with agent.db.connection() as conn:
        found = conn.execute("SELECT * FROM projects WHERE id = ?", (coaches,)).fetchone()
        assert plan.infer_template(conn, agent.scope(), found) == templates.ETSY_DIGITAL  # a new product's type
        assert plan.infer_template(conn, agent.scope(), found, records_only=True) == templates.GENERIC
        kept = conn.execute("SELECT * FROM projects WHERE id = ?", (line,)).fetchone()
        assert plan.infer_template(conn, agent.scope(), kept) == templates.ETSY_DIGITAL  # its request, as before


def test_a_line_whose_words_or_requests_name_a_kdp_book_stays_one(data_dir: Path) -> None:
    agent, _, listing = launched(data_dir)
    books = project(agent, "Haushaltsbuch 2027 (KDP paperback)")
    keep(agent)
    assert move(agent, books, listing).ok
    assert not [s for s in keep(agent) if "no KDP book" in s]
    [product] = rows(agent, f"SELECT template FROM plan_nodes WHERE level = 'product' AND project_id = {books}")
    assert templates.by_key(product["template"]).name == templates.KDP_BOOK.name
