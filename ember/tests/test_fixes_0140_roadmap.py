"""0.14.0 (FIX NOW 8 and 20, X10, X14, X23, X24): the roadmap's places and integrity.

Milestones Ember's code set took the agent's 16 places and the owner's 4, so the day-14 bars filled the roadmap and
the owner's own "Add milestone" was refused. The agent could move a bar or a metric milestone to another project, void
a losing forecast by dropping its milestone, and take a venture live on a first test it closed on its own word; the
owner's drop of a bar opened the next one; a bar met days late after a sync gap was graded done; a parked venture's
projects kept their bars; the print-on-demand venture backed under 0.12.0 kept its prose first test; a first sale
recorded late could never count; and the agent's own views goal stayed self-reported.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import econ, gates, metrics, predictions, roadmap, stages, tools, ventures  # noqa: E402
from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.agent.store import AgentScope  # noqa: E402
from app.db import Database, discover_migrations, migrate  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed, shop_context  # noqa: E402
from tests.test_listing_gates import bars, close, started  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_venture_stages import backed  # noqa: E402
from tests.test_ventures import DROPSHIPPING, ETSY, PRINT, VENTURING, plan  # noqa: E402


def call(agent: Agent, tool: str, **args: Any) -> tools.Outcome:
    """A tool as the model calls it in the agent's last cycle, with its shop."""
    ctx = shop_context(agent)
    llm_call = rows(agent, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, tool, args, f"toolu_{tool}", llm_call, "act")


def create(agent: Agent, **fields: Any) -> int:
    with agent.db.transaction() as conn:
        return roadmap.create(conn, agent.scope(), now=to_iso(agent.clock.now()), **fields)


def day(agent: Agent, days: int) -> str:
    return (agent.clock.today() + timedelta(days=days)).isoformat()


def milestone(agent: Agent, milestone_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM milestones WHERE id = {milestone_id}")[0]


def keep_gates(agent: Agent) -> list[str]:
    with agent.db.transaction() as conn:
        return gates.keep(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()))


def settle(agent: Agent) -> None:
    ledger = agent.economy.life.scope()
    metrics.grade_all(agent.db, agent.scope(), ledger, agent.clock, False)
    predictions.settle_all(agent.db, agent.scope(), ledger, agent.clock)


# --- FIX NOW 8: Ember's code's milestones take no place of the agent's or the owner's ---


def test_code_milestones_take_no_place_of_the_agents_or_the_owners(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    for n in range(16):  # the day-14 bars of many product lines, the money goal and its decision points besides
        create(agent, title=f"Bar {n}", measure="x", due=day(agent, 10), created_by="code", kind="first_test")
    mine = call(agent, "milestone_plan", milestones=[dict(title="My step", measure="3 drafts", due=day(agent, 5))])
    assert mine.ok, mine.text
    for n in range(15):
        create(agent, title=f"Step {n}", measure="x", due=day(agent, 10))
    full = call(agent, "milestone_plan", milestones=[dict(title="One more", measure="x", due=day(agent, 5))])
    assert not full.ok and full.text.startswith("Error: 16 of your and your owner's milestones are open"), full.text
    for n in range(4):  # the owner's 4 places stay free, whatever Ember's code set
        reply = owner(agent).add_milestone({"title": f"Owner's {n}", "measure": "x", "due": day(agent, 10)}, None)
        assert reply.status == 201, reply.body
    fifth = owner(agent).add_milestone({"title": "Fifth", "measure": "x", "due": day(agent, 10)}, None)
    assert fifth.status == 409 and fifth.body["error"] == "20 of your and the agent's milestones are open already"
    with agent.db.connection() as conn:
        assert roadmap.placed(conn, agent.scope()) == 20 and roadmap.count(conn, agent.scope(), "open") > 36


def test_the_money_goal_comes_back_on_a_full_roadmap(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    for n in range(20):
        create(agent, title=f"Step {n}", measure="x", due=day(agent, 10))
    with agent.db.transaction() as conn:
        goal = roadmap.money_goal(conn, agent.scope())
        happened = roadmap.keep_money_goal(
            conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()), 5_000_000, 1, 60
        )
        after = roadmap.money_goal(conn, agent.scope())
    assert goal is not None and after is not None and after["id"] != goal["id"], happened
    assert happened[-1].startswith(f"Ember's code set the money goal #{after['id']} (Earn twice what you spend")


def test_a_product_line_holds_one_open_bar_at_a_time(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    start = agent.clock.today()
    close(agent, "day7_views", "done")
    keep_gates(agent)
    assert {k: r["status"] for k, r in bars(agent).items()} == {"day7_views": "done", "day14_views": "open"}
    assert "then 2 favorites" in bars(agent)["day14_views"]["measure"]
    close(agent, "day14_views", "done")
    keep_gates(agent)
    favorites = bars(agent)["day14_favorites"]
    assert (favorites["status"], favorites["metric"], favorites["target"]) == ("open", "favorites_total", 2)
    assert favorites["due"] == (start + timedelta(days=14)).isoformat()  # the bar's day, from the start
    assert keep_gates(agent) == []
    close(agent, "day14_favorites", "missed")
    happened = keep_gates(agent)
    assert happened[0].startswith("Obligation: park the product line")
    assert bars(agent)["day21_sale"]["status"] == "open"
    with agent.db.connection() as conn:
        opened = [r for r in roadmap.open_milestones(conn, agent.scope()) if r["created_by"] == "code"]
    assert [r["kind"] for r in opened].count("first_test") == 1  # the money goal and its decision points besides


def test_a_missed_day_14_views_bar_misses_the_day_14_bar(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    close(agent, "day7_views", "done")
    keep_gates(agent)
    close(agent, "day14_views", "missed")
    happened = keep_gates(agent)
    assert happened[0].startswith("Obligation: park the product line")
    found = bars(agent)
    assert "day14_favorites" not in found and found["day21_sale"]["status"] == "open"  # no favorites bar after it
    assert keep_gates(agent) == []


def test_a_favorites_bar_opened_after_its_day_is_due_the_day_it_opens(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    close(agent, "day7_views", "done")
    keep_gates(agent)
    close(agent, "day14_views", "done")  # met by day 14, but graded after midnight
    agent.clock.advance(days=15)
    keep_gates(agent)
    assert bars(agent)["day14_favorites"]["due"] == agent.clock.today().isoformat()


# --- FIX NOW 20a: what a milestone Ember's code checks counts is fixed ---


def test_the_links_of_code_and_metric_milestones_are_fixed(data_dir: Path) -> None:
    agent, project = started(data_dir)
    with agent.db.transaction() as conn:  # another product line
        other_id = conn.execute(
            "INSERT INTO projects (mode, session, life_id, created_cycle_id, created_at, updated_at, title, hypothesis,"
            " status) SELECT mode, session, life_id, created_cycle_id, created_at, updated_at, 'Other line', 'x',"
            " 'active' FROM projects WHERE id = ?",
            (project,),
        ).lastrowid
    bar = bars(agent)["day7_views"]["id"]
    for link in ({"project_id": other_id}, {"venture_id": ETSY}):
        refused = call(agent, "milestone_update", milestone_id=bar, **link)
        assert not refused.ok and "what Ember's code counts for it is fixed" in refused.text, refused.text
    goal = call(
        agent,
        "milestone_plan",
        milestones=[dict(title="Orders of my line", metric="orders_observed", target="3", due=day(agent, 20))],
    )
    assert goal.ok, goal.text
    mine = int(goal.text.split("#", 1)[1].split(" ", 1)[0])
    refused = call(agent, "milestone_update", milestone_id=mine, project_id=project)
    assert not refused.ok and "what Ember's code counts for it is fixed" in refused.text, refused.text
    plain = create(agent, title="Plain", measure="x", due=day(agent, 20))
    assert call(agent, "milestone_update", milestone_id=plain, project_id=project).ok  # without a metric: linkable
    for sql in (
        f"UPDATE milestones SET project_id = {other_id} WHERE id = {bar}",
        f"UPDATE milestones SET venture_id = {ETSY} WHERE id = {mine}",
        f"UPDATE milestones SET parent_id = {plain} WHERE id = {bar}",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="milestones: "), agent.db.transaction() as conn:
            conn.execute(sql)
    with agent.db.transaction() as conn:  # Ember's code leads it to the next money goal, or to none
        conn.execute(f"UPDATE milestones SET parent_id = NULL WHERE id = {bar}")


# --- FIX NOW 20b: the owner's drop ends a product line's test ---


def test_the_owners_drop_of_a_bar_ends_that_product_lines_test(data_dir: Path) -> None:
    agent, project = started(data_dir)
    bar = bars(agent)["day7_views"]["id"]
    reply = owner(agent).decide_milestone(bar, {"action": "drop", "comment": "Stop testing this line."}, "Stefan")
    assert reply.status == 200, reply.body
    assert keep_gates(agent) == []
    agent.clock.advance(days=8)
    assert keep_gates(agent) == []
    assert set(bars(agent)) == {"day7_views"}


def test_the_owners_drop_of_the_money_goal_keeps_the_listing_tests(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    bar = bars(agent)["day7_views"]
    assert bar["parent_id"] is not None  # it leads to the money goal
    reply = owner(agent).decide_milestone(bar["parent_id"], {"action": "drop"}, "Stefan")
    assert reply.status == 200 and bar["id"] not in reply.body["dropped_with"], reply.body
    after = milestone(agent, bar["id"])
    assert (after["status"], after["parent_id"]) == ("open", None)
    assert keep_gates(agent) == []  # no second bar
    with agent.db.transaction() as conn:
        assert roadmap.keep_money_goal(conn, agent.scope(), agent.clock.today(), "2026-09-02T10:00:00Z", 0, 1, 60) == []
        assert roadmap.money_goal(conn, agent.scope()) is None  # dropped until the owner wants one


# --- FIX NOW 20c: the agent's drop settles its odds as a miss ---


def test_the_agents_drop_settles_its_odds_as_a_miss(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    made = []
    for title in ("Research that finds", "More research that finds"):
        outcome = call(
            agent,
            "milestone_plan",
            milestones=[
                dict(title=title, metric="research_calls_ok", target="5", due=day(agent, 7), likely=90),
            ],
        )
        assert outcome.ok, outcome.text
        made.append(int(outcome.text.split("#", 1)[1].split(" ", 1)[0]))
    dropped = call(agent, "milestone_update", milestone_id=made[0], status="dropped", result="Out of reach.")
    assert dropped.ok and "Your odds on it (90%) count as a miss" in dropped.text, dropped.text
    assert owner(agent).decide_milestone(made[1], {"action": "drop"}, "Stefan").status == 200
    settle(agent)
    found = rows(agent, "SELECT milestone_id, status, result FROM predictions ORDER BY id")
    assert (found[0]["status"], found[1]["status"]) == ("miss", "void")
    assert found[0]["result"].startswith(f"milestone #{made[0]} was dropped by you on ")
    with agent.db.connection() as conn:
        assert predictions.calibration(conn, agent.scope()).startswith("milestones given odds: 0 of 1 met")


# --- FIX NOW 20d: a venture goes live on a first test Ember's code or the owner closed ---


def test_a_venture_goes_live_only_on_a_first_test_code_or_the_owner_closed(data_dir: Path) -> None:
    agent, test = backed(data_dir)
    refused = call(agent, "milestone_update", milestone_id=test, status="done", result="3 samples sold, #71-#73")
    assert not refused.ok and "your owner confirms it" in refused.text, refused.text
    with agent.db.transaction() as conn:  # a done on the agent's word, as 0.13.0 allowed it
        conn.execute(
            "UPDATE milestones SET status = 'done', result = '3 sold', closed_at = 'now', closed_by = 'agent'"
            f" WHERE id = {test}"
        )
    live = call(agent, "venture_update", venture_id=DROPSHIPPING, stage="live")
    assert not live.ok and "Ember's code checks it or your owner confirms it" in live.text, live.text
    with pytest.raises(sqlite3.IntegrityError, match="once its first test is met"), agent.db.transaction() as conn:
        conn.execute(f"UPDATE ventures SET stage = 'live' WHERE id = {DROPSHIPPING}")


def test_the_owners_word_on_a_first_test_takes_the_venture_live(data_dir: Path) -> None:
    agent, test = backed(data_dir)
    assert owner(agent).decide_milestone(test, {"action": "drop", "comment": "Met: 3 sold."}, "Stefan").status == 200
    launched = call(agent, "venture_update", venture_id=DROPSHIPPING, stage="live")
    assert launched.ok, launched.text


# --- FIX NOW 20e: a bar met only after its date is missed ---


def test_a_bar_met_only_after_its_date_is_missed(data_dir: Path) -> None:
    agent, _ = listed(data_dir)  # the fake shop's listing gets 2 views an hour
    agent.clock.advance(minutes=61)
    agent.sync_shop()
    bar = create(
        agent,
        title="Day 1: 60 views",
        measure="60 views by tomorrow",
        due=day(agent, 1),
        created_by="code",
        kind="first_test",
        metric="views_total",
        target=60,
    )
    agent.clock.advance(minutes=61)
    agent.sync_shop()
    before = milestone(agent, bar)
    assert before["status"] == "open" and before["progress"] < 60
    agent.clock.advance(days=3)  # Etsy couldn't be read in between: its token had expired
    agent.sync_shop()
    after = milestone(agent, bar)
    assert (after["status"], after["closed_by"]) == ("missed", "code"), after["result"]
    assert f"views_total {before['progress']} views" in after["result"]
    assert "its last reading by its date" in after["result"]


def test_a_milestone_not_read_by_its_date_is_missed(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    agent.clock.advance(minutes=61)
    agent.sync_shop()
    bar = create(
        agent,
        title="Day 1: 60 views",
        measure="60 views by tomorrow",
        due=day(agent, 1),
        created_by="code",
        kind="first_test",
        metric="views_total",
        target=60,
    )
    agent.clock.advance(days=3)
    agent.sync_shop()
    after = milestone(agent, bar)
    assert after["status"] == "missed" and "not read by its date" in after["result"], after["result"]


# --- X10: a parked venture's projects drop their bars ---


def test_a_parked_ventures_projects_drop_their_bars(data_dir: Path) -> None:
    agent, project = started(data_dir)
    with agent.db.transaction() as conn:
        conn.execute(f"UPDATE projects SET venture_id = {ETSY} WHERE id = {project}")
    reply = owner(agent).decide_venture(ETSY, {"action": "park", "comment": "Not now."}, "Stefan")
    assert reply.status == 200, reply.body
    bar = bars(agent)["day7_views"]
    assert (bar["status"], bar["closed_by"]) == ("dropped", "owner")
    assert bar["result"] == f"Your owner parked venture #{ETSY}."
    assert keep_gates(agent) == []  # the product line's test ended with it
    assert set(bars(agent)) == {"day7_views"}


def test_code_parks_a_venture_with_its_projects_bars(data_dir: Path) -> None:
    agent, project = started(data_dir)
    with agent.db.transaction() as conn:
        conn.execute(f"UPDATE projects SET venture_id = {DROPSHIPPING} WHERE id = {project}")
        row = ventures.get(conn, agent.scope(), DROPSHIPPING)
        assert row is not None
        stages.park(conn, agent.scope(), row, to_iso(agent.clock.now()), "a test")
    bar = bars(agent)["day7_views"]
    assert (bar["status"], bar["closed_by"]) == ("dropped", "code")


# --- X14: the print-on-demand venture's prose first test becomes a first order ---


POD_TITLE = "Print on demand in the Etsy shop"


def _as_0_12_left_it(tmp_path: Path) -> Path:
    """A database as 0.12.0 left it (its last migration: demand_notes): the owner backed the seeded print-on-demand
    venture (#4), whose first test (#8) is in words; another backed venture's (#5) too; a twin whose first test was
    missed (#6); and a backed venture whose first test the agent closed done on its own word (#7)."""
    db_file = tmp_path / "ember.db"
    last = next(m for m in discover_migrations() if m.name == "demand_notes")
    migrate(db_file, [m for m in discover_migrations() if m.version <= last.version], backup_dir=tmp_path / "b")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        for vid, title in ((4, POD_TITLE), (5, "Dropshipping store"), (6, POD_TITLE), (7, "Posters")):
            conn.execute(
                "INSERT INTO ventures (id, mode, session, life_id, created_by, created_at, updated_at, title, pitch,"
                " stage, first_test, owner_action, owner_version) VALUES (?, 'live', 0, 1, 'owner',"
                " '2026-09-01T10:00:00Z', '2026-09-29T11:57:00Z', ?, 'x', 'building',"
                " 'Publish 3 POD listings; any 1 sale = continue', 'back', 1)",
                (vid, title),
            )
        prose = "Its first test is met: Cost: $0 to Ember. Test: publish 3 POD listings; any 1 sale = continue"
        for mid, parent, vid, title, kind, status, closed_by in (
            (5, None, None, "Earn as much as you spend", "money_goal", "open", None),
            (8, 5, 4, f"First test: {POD_TITLE}", "first_test", "open", None),
            (9, 5, 5, "First test: Dropshipping store", "first_test", "open", None),
            (10, 5, 6, f"First test: {POD_TITLE}", "first_test", "missed", "code"),
            (11, 5, 7, "First test: Posters", "first_test", "done", "agent"),
        ):
            conn.execute(
                "INSERT INTO milestones (id, mode, session, life_id, parent_id, venture_id, created_by, created_at,"
                " updated_at, title, measure, first_due, due, kind, status, result, closed_at, closed_by)"
                " VALUES (?, 'live', 0, 1, ?, ?, 'code', '2026-09-29T11:57:00Z', '2026-09-29T11:57:00Z', ?, ?,"
                " '2026-10-20', '2026-10-20', ?, ?, ?, ?, ?)",
                (
                    mid,
                    parent,
                    vid,
                    title,
                    prose,
                    kind,
                    status,
                    *(("", None) if status == "open" else ("3 sold", "then")),
                    closed_by,
                ),
            )
        conn.execute(  # the agent's step toward #8
            "INSERT INTO milestones (id, mode, session, life_id, parent_id, venture_id, created_by, created_at,"
            " updated_at, title, measure, first_due, due) VALUES (12, 'live', 0, 1, 8, 4, 'agent',"
            " '2026-09-29T12:00:00Z', '2026-09-29T12:00:00Z', 'Publish 3 POD listings', 'x', '2026-10-10',"
            " '2026-10-10')"
        )
        for vid, mid in ((4, 8), (5, 9), (6, 10), (7, 11)):
            conn.execute("UPDATE ventures SET test_milestone_id = ? WHERE id = ?", (mid, vid))
    old.close()
    return db_file


def test_the_pod_ventures_prose_first_test_becomes_a_first_order(tmp_path: Path) -> None:
    db_file = _as_0_12_left_it(tmp_path)
    ours = [m for m in discover_migrations() if m.name == "roadmap"][-1]  # this version's (0.11.0's has the name too)
    migrate(db_file, [m for m in discover_migrations() if m.version < ours.version], backup_dir=tmp_path / "b")
    before = Database(db_file)
    with before.connection() as conn:  # E4 on the owner's device: the channel came, the first test stayed in words
        pod = conn.execute("SELECT channel, test_milestone_id FROM ventures WHERE id = 4").fetchone()
        assert (pod["channel"], pod["test_milestone_id"]) == ("printify", 8)
        assert conn.execute("SELECT metric, status FROM milestones WHERE id = 8").fetchone()[:] == (None, "open")
    before.close()
    migrate(db_file, backup_dir=tmp_path / "b")
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        pod = conn.execute("SELECT * FROM ventures WHERE id = 4").fetchone()
        new = conn.execute("SELECT * FROM milestones WHERE id = ?", (pod["test_milestone_id"],)).fetchone()
        assert new["id"] not in (8, 9, 10, 11, 12)
        assert (new["metric"], new["target"], new["kind"], new["created_by"]) == ("pod_orders", 1, "first_test", "code")
        assert (new["venture_id"], new["parent_id"], new["due"], new["first_due"]) == (4, 5, "2026-10-20", "2026-10-20")
        assert (new["title"], new["measure"], new["replaces_id"], new["status"]) == (
            f"First test: {POD_TITLE}",
            stages.CHANNEL_TESTS["printify"][2],
            8,
            "open",
        )
        old_test = conn.execute("SELECT * FROM milestones WHERE id = 8").fetchone()
        assert (old_test["status"], old_test["closed_by"]) == ("dropped", "code")
        assert f"#{new['id']}" in old_test["result"]
        step = conn.execute("SELECT status, parent_id FROM milestones WHERE id = 12").fetchone()
        assert (step["status"], step["parent_id"]) == ("open", new["id"])  # the agent's step leads to the new one
        others = conn.execute("SELECT id, status FROM milestones WHERE id IN (9, 10, 11)")
        assert {r["id"]: r["status"] for r in others} == {9: "open", 10: "missed", 11: "done"}  # untouched
        tests = dict(conn.execute("SELECT id, test_milestone_id FROM ventures WHERE id IN (5, 6, 7)").fetchall())
        assert tests == {5: 9, 6: 10, 7: None}  # the agent's done on #11 no longer counts: a new first test comes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        scope = AgentScope("live", 0, 1)
        happened = stages.keep(conn, scope, date(2026, 10, 1), "2026-10-01T08:00:00Z")
        assert any(line.startswith("Ember's code set the first test of venture #7 as milestone #") for line in happened)
        with pytest.raises(sqlite3.IntegrityError, match="once its first test is met"):
            conn.execute("UPDATE ventures SET stage = 'live' WHERE id = 7")


# --- X23: a first sale recorded late still counts ---


def test_a_first_sale_recorded_late_still_counts(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]), settings=VENTURING)
    this_month = econ.Case("other", 30.0, 10.0, 0.0, (1, 3, 8), 10.0, 2.0, 0, 3.0)
    with agent.db.transaction() as conn:
        for vid in (DROPSHIPPING, PRINT):
            ventures.add_case(conn, vid, None, this_month, econ.compute(this_month), to_iso(agent.clock.now()))
    for vid in (DROPSHIPPING, PRINT):
        assert owner(agent).decide_venture(vid, {"action": "back"}, "Stefan").status == 200
    due = (agent.clock.today() + timedelta(days=predictions.FIRST_SALE_MIN_DAYS)).isoformat()
    agent.clock.advance(days=predictions.FIRST_SALE_MIN_DAYS + 2)
    settle(agent)
    assert [r["status"] for r in rows(agent, "SELECT status FROM predictions ORDER BY id")] == ["open", "open"]
    sale = {"amount": "4.90", "source": "Sold on its last day", "idempotency_key": "c" * 32, "day": due}
    assert agent.economy.record("revenue", {**sale, "venture_id": DROPSHIPPING}, "Stefan").status == 201
    settle(agent)
    found = rows(agent, "SELECT status, result FROM predictions ORDER BY id")
    assert found[0]["status"] == "hit" and found[0]["result"].startswith(f"revenue for it on {due}")
    assert found[1]["status"] == "open"
    agent.clock.advance(days=predictions.FIRST_SALE_GRACE_DAYS)
    settle(agent)
    assert rows(agent, "SELECT status, result FROM predictions ORDER BY id")[1] == {
        "status": "miss",
        "result": f"no sale recorded for it by {due}",
    }


# --- X24: the agent's goals are graded by code where code has the number ---


def test_the_agent_sets_views_and_favorites_in_all_that_code_grades(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    agent.clock.advance(minutes=61)
    agent.sync_shop()
    project = rows(agent, "SELECT a.project_id FROM approvals a WHERE a.executor = 'etsy_listing'")[0]["project_id"]
    goal = call(
        agent,
        "milestone_plan",
        milestones=[
            dict(
                title="Listings reach 200 views",
                metric="views_total",
                target="200",
                due=day(agent, 7),
                project_id=project,
            ),
        ],
    )
    assert goal.ok and "Ember's code checks views_total (at least 200 views)" in goal.text, goal.text
    met = call(
        agent,
        "milestone_plan",
        milestones=[dict(title="One view", metric="views_total", target="1", due=day(agent, 7))],
    )
    assert not met.ok and "views_total is 2 views already" in met.text, met.text
    for title, measure, hint in (
        ("Listings reach 20 views", "20 views in all", "views_total"),
        ("Two favorites", "The poster listings have 2 favorites", "favorites_total"),
    ):
        prose = call(agent, "milestone_plan", milestones=[dict(title=title, measure=measure, due=day(agent, 7))])
        assert not prose.ok and f"set metric {hint}" in prose.text, prose.text
    for title, measure in (
        ("Pins seen", "500 views on Pinterest"),
        ("Site visited", "The website has 50 views"),
        ("Shop video", "The shop's video has 100 views"),
        ("Newsletter read", "40 views of the newsletter"),
    ):
        elsewhere = call(agent, "milestone_plan", milestones=[dict(title=title, measure=measure, due=day(agent, 7))])
        assert elsewhere.ok, elsewhere.text  # not the shop's views
    assert "orders_total" not in metrics.NAMES and metrics.CATALOGUE["orders_total"].code_only
    mine = int(goal.text.split("#", 1)[1].split(" ", 1)[0])
    agent.clock.advance(days=5)  # two views an hour in the fake shop
    agent.sync_shop()
    graded = milestone(agent, mine)
    assert (graded["status"], graded["closed_by"]) == ("done", "code"), graded["result"]
