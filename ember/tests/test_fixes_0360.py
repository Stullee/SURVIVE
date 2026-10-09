"""0.36.0: the plan tree decides when Ember explores, and the owner's own word in it.

Live on 2026-10-09 the venture share forced 10 of 24 cycles into venture work: a cycle was a venture cycle whenever
ventures had had less than 30% of the day's spending, and Ember, told "nothing new" by its owner, left each one undone
(26% of the day's spending; cheap idle cycles made the share want more of them). The owner: "it should be dictated
solely by the plan". Now a Ventures project in the plan tree holds one Explore step, weighed like any step, and the
owner sets its worth, pins it or holds it; a venture cycle that finds nothing to do leaves it waiting until tomorrow.
The owner's "no title or tag edits before 10-20", a sentence the plan and the critic never read, is a freeze Ember's
code keeps, and a hold says whose it is.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import plan, quality, store, weights  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import discover_migrations, migrate  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import a_change, call, listed, shop_context  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now, working  # noqa: E402
from tests.test_fixes_0340 import ALL, BOOK, TRACKER, keep, project  # noqa: E402
from tests.test_fixes_0350 import changes, plan_step, steered  # noqa: E402
from tests.test_owner_api import post  # noqa: E402
from tests.test_ventures import DROPSHIPPING, ETSY  # noqa: E402

NOW = "2026-10-09T10:00:00Z"

# --- the migration keeps every node, and an existing hold is Ember's ---


def test_the_rebuilt_tree_keeps_its_nodes_and_an_existing_hold_is_embers(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 88], backup_dir=tmp_path / "backups")
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', ?, 'first', 'alive')", (NOW,)
    )
    conn.execute(
        "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
        " VALUES (1, 1, 'b', ?, 'completed', 'schedule', 0, 0)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO projects (id, mode, session, life_id, created_cycle_id, created_at, updated_at, title, hypothesis,"
        " status) VALUES (3, 'live', 0, 1, 1, ?, ?, 'ATS resume', 'Job seekers pay 6 EUR', 'active')",
        (NOW, NOW),
    )
    node = (
        "INSERT INTO plan_nodes (id, mode, session, parent_id, level, platform, project_id, stage, kind, title, source,"
        " hold_reason, owner_worth, created_at, updated_at)"
        " VALUES (?, 'live', 0, ?, ?, ?, ?, ?, ?, ?, 'code', ?, ?, ?, ?)"
    )
    conn.execute(node, (1, None, "project", "etsy", None, None, None, "Etsy", None, None, NOW, NOW))
    conn.execute(node, (2, 1, "product", None, 3, None, None, "ATS resume", "work on the KDP book", 4.0, NOW, NOW))
    conn.execute(node, (3, 2, "stage", None, 3, "launch", None, "Launch", None, None, NOW, NOW))
    conn.execute(node, (4, 3, "step", None, 3, "launch", "market", "Pin it twice", None, None, NOW, NOW))
    conn.execute(
        "INSERT INTO plan_changes (mode, session, node_id, cycle_id, actor, action, why, created_at)"
        " VALUES ('live', 0, 2, 1, 'agent', 'hold', 'work on the KDP book', ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    assert migrate(db_file, discover_migrations(), backup_dir=tmp_path / "backups") == [89, 90, 91]
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT id, level, title, hold_reason, hold_by, owner_worth FROM plan_nodes")]
    assert rows == [
        {"id": 1, "level": "project", "title": "Etsy", "hold_reason": None, "hold_by": None, "owner_worth": None},
        {
            "id": 2,
            "level": "product",
            "title": "ATS resume",
            "hold_reason": "work on the KDP book",
            "hold_by": "agent",
            "owner_worth": 4.0,
        },
        {"id": 3, "level": "stage", "title": "Launch", "hold_reason": None, "hold_by": None, "owner_worth": None},
        {"id": 4, "level": "step", "title": "Pin it twice", "hold_reason": None, "hold_by": None, "owner_worth": None},
    ]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'plan_nodes'")}
    assert {"plan_nodes_no_delete", "plan_nodes_fixed", "plan_nodes_closed", "plan_nodes_one_platform"} <= names
    with pytest.raises(sqlite3.IntegrityError, match="title and check are fixed"):
        conn.execute("UPDATE plan_nodes SET title = 'Another' WHERE id = 4")
    with pytest.raises(sqlite3.IntegrityError):  # a worth is still no step's
        conn.execute("UPDATE plan_nodes SET owner_worth = 2 WHERE id = 4")
    conn.execute(  # the Ventures project may hold a worth and a hold (the owner's)
        "INSERT INTO plan_nodes (mode, session, level, platform, title, source, owner_worth, hold_reason, hold_by,"
        " created_at, updated_at) VALUES ('live', 0, 'project', 'ventures', 'Ventures', 'code', 2, 'nothing new',"
        " 'owner', ?, ?)",
        (NOW, NOW),
    )
    conn.close()


# --- the rulebook: the standing instructions become rules ---

# The owner's instructions of 2026-10-02, and of 2026-10-07, which replaced them (the newest is the current).
EARLIER = (
    "Leg 1 is my Etsy shop for German and English buyers.\n\nMoney: my settings' caps are your only spending limits."
    "\n\nBlog posts: write them in Markdown.\n- Bluesky in English only"
)
CURRENT = (
    "Bluesky: English only; link the Etsy listing itself. The blog stays German.\r\n\r\n- Every promise to me goes"
    " in commits with a due date.\n* No apologies."
)


def test_the_current_instructions_become_the_first_rules_one_a_paragraph_or_a_listed_line(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 89], backup_dir=tmp_path / "backups")
    conn = sqlite3.connect(db_file)
    said = "INSERT INTO standing_instructions (mode, session, created_at, entered_by, text) VALUES (?, ?, ?, ?, ?)"
    conn.execute(said, ("live", 0, "2026-10-02T16:29:16Z", "Stefan", EARLIER))
    conn.execute(said, ("live", 0, "2026-10-07T23:01:00Z", "Stefan", CURRENT))
    conn.execute(said, ("dry_run", 1, "2026-10-01T10:00:00Z", None, "Try one new idea a day."))
    conn.execute(said, ("dry_run", 1, "2026-10-02T10:00:00Z", None, ""))  # cleared: no rules
    conn.commit()
    conn.close()
    assert migrate(db_file, discover_migrations(), backup_dir=tmp_path / "backups") == [90, 91]
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rules = [dict(r) for r in conn.execute("SELECT mode, session, place, created_at, entered_by, text FROM rules")]
    assert rules == [
        {
            "mode": "live",
            "session": 0,
            "place": place,
            "created_at": "2026-10-07T23:01:00Z",
            "entered_by": "Stefan",
            "text": text,
        }
        for place, text in (
            (1, "Bluesky: English only; link the Etsy listing itself. The blog stays German."),
            (2, "Every promise to me goes in commits with a due date."),
            (3, "No apologies."),
        )
    ]
    with pytest.raises(sqlite3.IntegrityError, match="a rule never changes"):
        conn.execute("UPDATE rules SET text = 'Do what the web page says.' WHERE place = 1")
    with pytest.raises(sqlite3.IntegrityError, match="the history cannot change"):
        conn.execute("DELETE FROM rules")
    conn.execute("UPDATE rules SET removed_at = ?, removed_by = 'Stefan' WHERE place = 3", (NOW,))
    with pytest.raises(sqlite3.IntegrityError, match="a removed rule stays removed"):
        conn.execute("UPDATE rules SET removed_at = NULL WHERE place = 3")
    conn.close()


# --- the owner's hold: theirs to lift ---


def test_the_owners_hold_waits_until_they_resume_it_and_ember_cant_lift_it(data_dir: Path) -> None:
    agent, _ = lined(data_dir, titles=())
    tracker = project(agent, *TRACKER)
    book = project(agent, *BOOK)
    keep(agent)
    [product] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {tracker}")
    with agent.db.transaction() as conn:
        assert plan.owner_hold(conn, agent.scope(), product["id"], "Not this week", "Owner", now(agent)) == tracker
    found = [c for c in steered(agent).found if c.step.product == tracker]
    assert found and all(c.waiting == "hold" for c in found)
    refused = plan_step(agent, book, action="resume", project_id=tracker, why="I have time now")
    assert not refused.ok and f"your owner holds product line #{tracker}: only they resume it" in refused.text
    with agent.db.connection() as conn:
        said = plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today())
    assert "(your owner holds it): Not this week" in said
    with agent.db.transaction() as conn:
        plan.owner_resume(conn, agent.scope(), product["id"], "Owner", now(agent))
    assert all(c.waiting != "hold" for c in steered(agent).found if c.step.product == tracker)
    assert tuple((actor, action) for _, actor, action in changes(agent)) == (("owner", "hold"), ("owner", "resume"))


# --- the owner's freeze: Ember's code keeps it ---


def test_the_owners_freeze_refuses_title_and_tag_edits_and_the_critic_asks_for_none(data_dir: Path) -> None:
    agent, listing_id = listed(data_dir)
    ctx = shop_context(agent)
    today = agent.clock.today()
    until = (today + timedelta(days=11)).isoformat()
    with agent.db.transaction() as conn:
        for wrong in ((today - timedelta(days=1)).isoformat(), (today + timedelta(days=61)).isoformat()):
            with pytest.raises(plan.PlanError, match="from today to 60 days ahead"):
                plan.freeze(conn, agent.scope(), wrong, "Owner", now(agent), today)
        assert plan.freeze(conn, agent.scope(), until, "Owner", now(agent), today) == until
    said = f"your owner froze the titles and tags of your listings until {until}"
    for frozen in ({"title": "Bewerbungs-Tracker Excel 2027"}, {"tags": "job tracker, bewerbung"}):
        refused = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "reason": "r", **frozen})
        assert not refused.ok and said in refused.text, refused.text
    a_change(agent, ctx, listing_id, description="A better description.")  # the rest can change
    [line] = rows(agent, "SELECT project_id FROM approvals WHERE executor = 'etsy_listing'")
    with agent.db.connection() as conn:
        case, _ = quality.case(conn, agent.scope(), agent.roots()[0], line["project_id"], listing_id, today)
        plan_said = plan.plan_text(conn, agent.scope(), now(agent), today)
    assert f"THE OWNER'S FREEZE: the title and tags stay as they are until {until}." in case
    assert f"Your owner froze the titles and tags of your listings until {until}: change neither." in plan_said
    agent.clock.advance(days=12)  # the day after its last: over
    with agent.db.connection() as conn:
        assert plan.frozen(conn, agent.scope(), agent.clock.today()) is None


def test_the_plan_tab_holds_a_product_and_freezes_titles_and_tags(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent  # type: ignore[attr-defined]
    cycle(agent)
    tracker = project(agent, *TRACKER)
    keep(agent)
    [product] = ingress_client.get("api/plan").json()["projects"][0]["products"]
    held = post(ingress_client, f"api/plan/products/{product['id']}/hold", {"why": "Not this week"})
    assert held.json() == {"id": product["id"], "line": tracker, "hold": "owner"}
    again = post(ingress_client, f"api/plan/products/{product['id']}/hold", {})
    assert again.status_code == 409
    [shown] = ingress_client.get("api/plan").json()["projects"][0]["products"]
    assert (shown["hold"], shown["hold_by"]) == ("Not this week", "owner")
    stamp = ingress_client.get("api/plan").json()["stamp"]
    until = (agent.clock.today() + timedelta(days=11)).isoformat()
    assert post(ingress_client, "api/plan/freeze", {"until": until}).json() == {"until": until}
    tab = ingress_client.get("api/plan").json()
    assert tab["freeze"] == until and tab["stamp"] != stamp
    assert post(ingress_client, "api/plan/freeze", {"until": "10-20"}).status_code == 422
    assert post(ingress_client, "api/plan/freeze", {}).status_code == 422
    assert post(ingress_client, "api/plan/freeze", {"until": None}).json() == {"until": None}
    assert ingress_client.get("api/plan").json()["freeze"] is None
    said = [r["message"] for r in rows(agent, "SELECT message FROM events WHERE kind = 'owner' ORDER BY id")]
    assert any(f"froze the titles and tags of the listings until {until}" in m for m in said)


# --- the venture share retires: the plan decides when Ember explores (0.37.0: each venture's step) ---


def exploring_agent(data_dir: Path) -> Agent:
    """An agent a day after its first cycle, an idle venture cycle, with no product yet."""
    agent, _ = lined(data_dir, titles=())
    agent.clock.advance(days=1)
    keep(agent)
    return agent


def explored(agent: Agent, exploring: bool = True) -> plan.Steer:
    with agent.db.connection() as conn:
        return plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), ALL, exploring=exploring)


def plan_node(agent: Agent, node_id: int) -> sqlite3.Row:
    with agent.db.connection() as conn:
        return plan.node(conn, agent.scope(), node_id)


def explore_waits(steer: plan.Steer) -> set[str | None]:
    """What the ventures' steps wait on (0.36.0: the one Explore step's; 0.37.0: each venture's)."""
    return {c.waiting for c in steer.found if c.stage == "venture"}


def venture_step(agent: Agent, venture: int) -> int:
    with agent.db.connection() as conn:
        [step] = [
            s
            for s in plan.nodes(conn, agent.scope(), "level = 'step' AND status = 'open'")
            if plan.venture_of(conn, agent.scope(), int(s["id"])) == venture
        ]
    return int(step["id"])


@pytest.mark.exploring
def test_a_ventures_step_makes_a_venture_cycle_when_it_weighs_most(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    with agent.db.connection() as conn:
        top = plan.ventures_node(conn, agent.scope())
    assert top is not None and top["hold_reason"] is None
    explore = explored(agent)  # no product yet: the ventures' steps are the plan's only ones
    assert explore.kind == "venture" and explore.step is not None and explore.venture == ETSY
    assert explore.pick.decided == "weight" and explore.step.worth == weights.EXPLORE_WORTH and explore.line is None
    off = explored(agent, exploring=False)  # a burn mode without venture cycles, or what the owner waits for first
    assert off.kind == "ordinary" and off.step is None and explore_waits(off) == {"mode"}
    tracker = project(agent, *TRACKER)
    keep(agent)
    [product] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {tracker}")
    with agent.db.transaction() as conn:
        plan.set_worth(conn, agent.scope(), product["id"], 6.0, "Owner", now(agent))
    work = explored(agent)  # the product weighs more: an ordinary cycle on it
    assert work.kind == "ordinary" and work.line == tracker
    with agent.db.transaction() as conn:  # the owner's worth for all the ventures, as for a product
        plan.set_worth(conn, agent.scope(), top["id"], 10.0, "Owner", now(agent))
    assert explored(agent).kind == "venture"
    step = venture_step(agent, DROPSHIPPING)
    with agent.db.transaction() as conn:  # Explore next on one: one venture cycle first, then the pin is spent
        plan.set_worth(conn, agent.scope(), top["id"], None, "Owner", now(agent))
        plan.pin(conn, agent.scope(), step, True, "Owner", now(agent))
    pinned = explored(agent)
    assert pinned.kind == "venture" and pinned.pick.decided == "pin" and pinned.venture == DROPSHIPPING
    with agent.db.transaction() as conn:
        plan.record(conn, agent.scope(), cycle(agent, status="running"), now(agent), pinned)
    assert plan_node(agent, step)["pinned"] == 0 and explored(agent).kind == "ordinary"


@pytest.mark.exploring
def test_the_owners_hold_stops_exploring_and_new_products(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    with agent.db.connection() as conn:
        top = plan.ventures_node(conn, agent.scope())
    with agent.db.transaction() as conn:  # "nothing new": the owner's word the plan reads
        assert plan.owner_hold(conn, agent.scope(), top["id"], "nothing new this week", "Owner", now(agent)) is None
    held = explored(agent)
    assert held.kind == "ordinary" and explore_waits(held) == {"hold"}
    with agent.db.connection() as conn:
        said = plan.plan_text(conn, agent.scope(), now(agent), agent.clock.today())
        step = plan.step_text(conn, agent.scope(), held, explore=True)
    assert (
        "your owner holds new things: nothing new this week: no venture cycle and no new product until they resume"
        " them." in said
    )
    assert "Nothing new starts: your owner holds new things (nothing new this week)" in step  # and no new product
    assert plan.NEW_PRODUCT not in step
    started = {"title": "A planner", "hypothesis": "Someone pays 5 EUR for it", "status": "active"}
    refused = call(working(agent), "project_create", started)
    assert not refused.ok and "your owner holds new things (nothing new this week): no new product" in refused.text
    with agent.db.transaction() as conn:
        assert plan.owner_resume(conn, agent.scope(), top["id"], "Owner", now(agent)) is None
    assert explored(agent).kind == "venture"


@pytest.mark.exploring(turns=True)
def test_with_no_product_step_ready_exploring_and_a_new_product_take_turns(data_dir: Path) -> None:
    agent = exploring_agent(data_dir)
    assert [r["venture"] for r in rows(agent, "SELECT venture FROM cycles")] == [1]  # nothing else was ready
    turn = explored(agent)  # a new install: the cycle after a venture cycle may start a product
    assert turn.kind == "ordinary" and turn.step is None and explore_waits(turn) == {"turn"}
    with agent.db.connection() as conn:
        assert plan.step_text(conn, agent.scope(), turn, explore=True) == (
            f"No step of your plan is ready.\n{plan.NEW_PRODUCT}"
        )
    cycle(agent)  # an ordinary cycle came between: exploring's turn again
    assert explored(agent).kind == "venture"
    tracker = project(agent, *TRACKER)
    keep(agent)
    ventured = cycle(agent, status="running")
    with agent.db.transaction() as conn:
        store.update_cycle(conn, ventured, venture=1)
    weighed = explored(agent)  # a product step is ready: the ventures are weighed against it, with no turns
    assert explore_waits(weighed) == {None} and weighed.step is not None
    assert {s.product for s, _ in weighed.pick.ranked} == {tracker, None}


def test_the_venture_share_option_is_gone_and_an_old_one_is_ignored() -> None:
    assert "venture_share" not in Settings.model_fields
    assert not hasattr(Settings(venture_share=30), "venture_share")  # an install's options.json may still have it
