"""The 0.16.1 analysis, bugs 1 and 5.

Bug 1: Ember's code would have parked the print-on-demand venture around 10-22, and ended the Nebenkosten listing test
with it. Its first test, a first order by 10-21, was closed missed at the first check after its date, without the
week's grace a first test in words has, while no Printify product had ever been made (Ember's own checks refused the
posters). And migration 0068 had linked the Nebenkosten line, a digital download, to print on demand, because its newest
request was made in a cycle aimed at that venture; ventures.adopt chose the same way. Now a product line joins its
channel's venture, migration 0071 takes such lines to the Etsy leg, a first test with a metric has the grace of one in
words, one that never ran starts once more (once), and the owner hears a week before its date what is at stake.

Bug 5: "Unlocked for this milestone" notes stood after the unlocks were taken back. Each unlock was written into the
milestone's one note of the owner's (each click over the last), and no take-back changed it: not the upgrade to
0.15.0's, nor Ember's code's. The planner showed it as the owner's word. Now what is unlocked is said from the grants
that stand, no unlock or take-back writes a note, a change of the unlocks is the agent's news (Ember's code's take-backs
too), and migration 0071 clears the notes that say a rule is unlocked while no grant of it stands.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import metrics, news, obligations, policy, roadmap, stages, store, ventures  # noqa: E402
from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.agent.store import AgentScope  # noqa: E402
from app.config import LoadedSettings  # noqa: E402
from app.db import Database, discover_migrations, migrate  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import printify_publisher  # noqa: E402
from tests.economy_helpers import ScriptedTransport, make_economy  # noqa: E402
from tests.test_agent import ROOMY, rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_policy import a_milestone  # noqa: E402
from tests.test_printify import PRINTING  # noqa: E402

OURS = next(m for m in discover_migrations() if m.name == "channels_unlocks")
LIVE = ROOMY.model_copy(update={"dry_run": False, "anthropic_api_key": "sk-ant-api03-testkey-0123456789"})
THEN = "2026-09-01T08:00:00Z"  # the test clock's day, before its time (tests/economy_helpers.py: 12:00 UTC)


def section(text: str, title: str) -> str:
    return text.split(f"== {title} ==\n", 1)[1].split("\n\n== ", 1)[0]


def line_of(text: str, start: str) -> str:
    return next(line for line in text.split("\n") if line.startswith(start))


def keep(agent: Agent) -> list[str]:
    with agent.db.transaction() as conn:
        return stages.keep(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()))


def warn(agent: Agent) -> list[str]:
    with agent.db.transaction() as conn:
        return stages.warn(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()))


def grade(agent: Agent) -> list[str]:
    return metrics.grade_all(agent.db, agent.scope(), agent.economy.life.scope(), agent.clock, False)


def on(agent: Agent, day: date) -> None:
    agent.clock.advance(days=(day - agent.clock.today()).days)


def venture(agent: Agent, venture_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM ventures WHERE id = {venture_id}")[0]


def milestone(agent: Agent, milestone_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM milestones WHERE id = {milestone_id}")[0]


def first_test_of(agent: Agent, venture_id: int) -> dict[str, Any]:
    return milestone(agent, int(venture(agent, venture_id)["test_milestone_id"]))


# --- bug 1: a first test's grace, once more when it never ran, and the owner's warning ------------------------------


def backed_pod(data_dir: Path) -> tuple[Agent, int]:
    """A dry-run agent with Printify on (its fake account), whose owner backed the print-on-demand venture: its first
    test, a first order, is set."""
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=PRINTING)
    pod = rows(agent, "SELECT id FROM ventures WHERE channel = 'printify'")[0]["id"]
    assert owner(agent).decide_venture(pod, {"action": "back", "confirm": True}, "Stefan").status == 200
    keep(agent)
    test = first_test_of(agent, pod)
    assert (test["metric"], test["kind"], test["created_by"], test["status"]) == (
        "pod_orders",
        "first_test",
        "code",
        "open",
    )
    return agent, pod


def a_product(agent: Agent) -> None:
    """A poster Ember's code made at Printify and published to the Etsy shop: the first test runs."""
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        request = store.insert_approval(
            conn,
            scope,
            cycle,
            now,
            type="sell",
            title="Printify product: Mountain poster",
            description="A first poster.",
            payload="Mountain poster",
            expected_cost="none",
            expected_benefit="a first order",
            executor="printify_product",
            action="{}",
        )
        conn.execute(
            "INSERT INTO printify_products (mode, session, approval_id, product_id, listing_id, title, currency,"
            " status, started_at, finished_at) VALUES (?, ?, ?, 'p1', 7001, 'Mountain poster', 'EUR', 'active', ?, ?)",
            (scope.mode, scope.session, request, now, now),
        )


def an_order(agent: Agent) -> None:
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO printify_orders (mode, session, order_id, product_id, quantity, cost_cents, shipping_cents,"
            " currency, status, created_at, synced_at)"
            " VALUES (?, ?, 'o1', 'p1', 1, 790, 450, 'EUR', 'fulfilled', ?, ?)",
            (scope.mode, scope.session, now, now),
        )


def test_a_first_test_that_never_ran_has_its_grace_starts_once_more_and_is_announced(data_dir: Path) -> None:
    agent, pod = backed_pod(data_dir)
    test = first_test_of(agent, pod)
    with agent.db.connection() as conn:
        assert not printify_publisher.made_any(conn, agent.scope())  # Ember's own checks refused every poster
    due = date.fromisoformat(test["due"])
    ends = due + timedelta(days=stages.FIRST_TEST_GRACE_DAYS)
    stake = f"Unmet by {ends}, Ember's code closes it missed and parks venture #{pod}."
    # A week before its date the owner hears once what is at stake (the System log), and the agent owes it.
    on(agent, due - timedelta(days=stages.WARN_DAYS + 1))
    assert warn(agent) == []
    on(agent, due - timedelta(days=stages.WARN_DAYS))
    grade(agent)
    [said] = warn(agent)
    assert said.startswith(
        f'First test #{test["id"]} of venture #{pod} "Print on demand in the Etsy shop" is due {due}, not met yet'
        " (pod_orders at least 1 order; now 0 orders (Printify,"
    )
    assert said.endswith(f"{stake} If no Printify product is made by then, it starts the test once more instead.")
    assert warn(agent) == []  # once
    with agent.db.connection() as conn:
        owed = obligations.text(conn, agent.scope(), agent.clock.today())
    assert f"- {said} Work toward it first, or tell your owner once what it needs." in owed
    # Past its date it is no longer closed missed at the first check: a week's grace, as one in words has.
    on(agent, due + timedelta(days=1))
    assert not any(f"#{test['id']}" in line for line in grade(agent))
    assert not any(f"venture #{pod}" in line for line in keep(agent))
    assert first_test_of(agent, pod)["status"] == "open"
    with agent.db.connection() as conn:
        planned = roadmap.planner_text(roadmap.open_milestones(conn, agent.scope()), [], agent.clock.today())
        owed = obligations.text(conn, agent.scope(), agent.clock.today())
    assert f"unmet by {ends}, Ember's code parks venture #{pod}" in line_of(planned, f"#{test['id']} ")
    assert f"#{test['id']}" not in "".join(line for line in planned.split("\n") if "overdue (" in line)
    assert "Overdue milestones" not in owed  # it can't close, move or drop it: its own line says what happens
    on(agent, ends)
    grade(agent)
    assert not any(f"venture #{pod}" in line for line in keep(agent))
    assert (first_test_of(agent, pod)["id"], venture(agent, pod)["stage"]) == (test["id"], "building")
    # The day after its last day: no product was ever made, so it never ran. It starts once more instead of parking.
    on(agent, ends + timedelta(days=1))
    happened = keep(agent)
    again = first_test_of(agent, pod)
    assert (
        again["id"] != test["id"]
        and again["status"] == "open"
        and (again["metric"], again["target"])
        == (
            "pod_orders",
            1,
        )
    )
    assert again["due"] == (agent.clock.today() + timedelta(days=stages.FIRST_TEST_DAYS)).isoformat()
    why = "no Printify product was ever made by its last day, so it never ran"
    assert (
        f"Ember's code started the first test of venture #{pod} once more as milestone #{again['id']} (due in "
        f"{stages.FIRST_TEST_DAYS} days): {why}" in happened
    )
    old = milestone(agent, test["id"])
    assert (old["status"], old["closed_by"], old["result"]) == (
        "dropped",
        "code",
        f"Venture #{pod}'s first test: {why}. Ember's code started it once more as #{again['id']}.",
    )
    assert venture(agent, pod)["stage"] == "building"
    # Once: its warning no longer promises another start, and unmet when its grace ends, the venture is parked.
    on(agent, date.fromisoformat(again["due"]) - timedelta(days=stages.WARN_DAYS))
    [said] = warn(agent)
    assert said.endswith(
        f"Unmet by {date.fromisoformat(again['due']) + timedelta(days=7)}, Ember's code closes it "
        f"missed and parks venture #{pod}."
    )
    on(agent, date.fromisoformat(again["due"]) + timedelta(days=stages.FIRST_TEST_GRACE_DAYS + 1))
    grade(agent)
    happened = keep(agent)
    missed = milestone(agent, again["id"])
    assert missed["status"] == "missed" and missed["result"].startswith(
        f"Still open {stages.FIRST_TEST_GRACE_DAYS + 1} days after its date (pod_orders at least 1 order; now 0 orders"
    )
    assert venture(agent, pod)["stage"] == "parked"
    assert any(f"parked venture #{pod}" in line for line in happened)


def test_a_first_order_in_the_week_of_grace_meets_the_first_test(data_dir: Path) -> None:
    agent, pod = backed_pod(data_dir)
    a_product(agent)
    test = first_test_of(agent, pod)
    due = date.fromisoformat(test["due"])
    on(agent, due + timedelta(days=3))  # unmet by its date: it was closed missed at the first check after it
    assert not any(f"#{test['id']}" in line for line in grade(agent))
    an_order(agent)
    assert any(f"Ember's code closed milestone #{test['id']} done" in line for line in grade(agent))
    assert first_test_of(agent, pod)["status"] == "done"
    assert venture(agent, pod)["stage"] == "building"  # it goes live on the agent's or the owner's word, as before


def test_a_first_test_that_ran_is_missed_after_its_grace_and_parks_the_venture(data_dir: Path) -> None:
    agent, pod = backed_pod(data_dir)
    a_product(agent)  # a poster was made: the test ran, and no buyer ordered
    test = first_test_of(agent, pod)
    ends = date.fromisoformat(test["due"]) + timedelta(days=stages.FIRST_TEST_GRACE_DAYS)
    on(agent, ends - timedelta(days=stages.WARN_DAYS))
    [said] = warn(agent)
    assert "once more" not in said  # a product was made: no other start
    on(agent, ends)
    grade(agent)
    assert first_test_of(agent, pod)["status"] == "open"
    on(agent, ends + timedelta(days=1))
    grade(agent)
    keep(agent)
    assert milestone(agent, test["id"])["status"] == "missed"
    assert venture(agent, pod)["stage"] == "parked"


def test_the_warning_reaches_the_system_log_once_from_a_wake_cycle(data_dir: Path) -> None:
    agent, pod = backed_pod(data_dir)
    test = first_test_of(agent, pod)
    on(agent, date.fromisoformat(test["due"]) - timedelta(days=stages.WARN_DAYS))
    agent.run_cycle("schedule")
    agent.run_cycle("schedule")
    warned = rows(agent, "SELECT level, message FROM events WHERE message LIKE 'First test #%' ORDER BY id")
    assert [(e["level"], e["message"].split(",")[0]) for e in warned] == [
        (
            "warning",
            f'First test #{test["id"]} of venture #{pod} "Print on demand in the Etsy shop" is due {test["due"]}',
        )
    ]


# --- bug 1: a product line joins its channel's venture; 0071 re-links the digital lines under print on demand -------


def test_a_digital_line_under_print_on_demand_moves_to_the_etsy_leg_and_survives_its_park(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    everything = discover_migrations()
    migrate(db_file, [m for m in everything if m.version < OURS.version], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        for life, mode in ((1, "live"), (2, "dry_run")):
            conn.execute(
                "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (?, ?, ?, 'born', 'alive')",
                (life, mode, THEN),
            )
        venture_row = (
            "INSERT INTO ventures (id, mode, session, life_id, created_by, created_at, updated_at, title, pitch, stage,"
            " channel, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'x', ?, ?, NULL)"
        )
        for vid, mode, session, life, by, title, stage, channel in (
            (1, "live", 0, 1, "agent", ventures.ETSY_LEG, "live", None),
            (4, "live", 0, 1, "owner", "Print on demand in the Etsy shop", "building", "printify"),
            (21, "dry_run", 2, 2, "agent", ventures.ETSY_LEG, "parked", None),  # a dry run's leg, parked
            (24, "dry_run", 2, 2, "owner", "Print on demand in the Etsy shop", "building", "printify"),
        ):
            conn.execute(venture_row, (vid, mode, session, life, by, THEN, THEN, title, stage, channel))
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros, venture_id)"
            " VALUES (1, 1, 'b', ?, 'completed', 'schedule', 0, 1, 4)",  # a cycle aimed at print on demand
            (THEN,),
        )
        project_row = (
            "INSERT INTO projects (id, mode, session, life_id, created_cycle_id, created_at, updated_at, title,"
            " hypothesis, status, venture_id) VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'x', ?, ?)"
        )
        request_row = (
            "INSERT INTO approvals (mode, session, life_id, cycle_id, project_id, created_at, type, title, description,"
            " payload, payload_sha256, expected_cost, expected_benefit, executor, action, status, venture_id)"
            " VALUES (?, ?, ?, 1, ?, ?, 'sell', 't', 'd', ?, ?, 'c', 'b', ?, '{}', 'pending', ?)"
        )
        for pid, mode, session, life, title, status, vid, executors in (
            (7, "live", 0, 1, "Nebenkostenabrechnung", "active", 4, ("etsy_listing",)),  # 0068's link: the bug
            (8, "live", 0, 1, "Posters", "active", 4, ("printify_product",)),
            (9, "live", 0, 1, "Posters and their PDFs", "active", 4, ("etsy_listing", "printify_product")),
            (10, "live", 0, 1, "Old planner", "abandoned", 4, ("etsy_listing",)),  # closed: final
            (11, "live", 0, 1, "Bewerbungs-Tracker", "active", 1, ("etsy_listing", "etsy_edit")),
            (12, "dry_run", 2, 2, "Budget planner", "active", 24, ("etsy_listing",)),
        ):
            conn.execute(project_row, (pid, mode, session, life, THEN, THEN, title, status, vid))
            for executor in executors:
                payload = f"{pid}-{executor}"
                conn.execute(
                    request_row,
                    (mode, session, life, pid, THEN, payload, store.sha256(payload), executor, vid),
                )
        # The bars of #7's and #8's listing tests, and the venture's first test (0065's: a first order by 10-21).
        milestone_row = (
            "INSERT INTO milestones (id, mode, session, life_id, title, measure, first_due, due, created_at,"
            " updated_at, created_by, kind, metric, target, project_id, venture_id)"
            " VALUES (?, 'live', 0, 1, ?, 'x', ?, ?, ?, ?, 'code', 'first_test', ?, ?, ?, ?)"
        )
        for mid, title, due, metric, target, project, vid in (
            (16, "First test: Print on demand in the Etsy shop", "2026-10-21", "pod_orders", 1, None, 4),
            (30, "Day 7: 10 views: Nebenkostenabrechnung", "2026-10-08", "views_total", 10, 7, None),
            (31, "Day 7: 10 views: Posters", "2026-10-08", "views_total", 10, 8, None),
        ):
            conn.execute(milestone_row, (mid, title, due, due, THEN, THEN, metric, target, project, vid))
        conn.execute("UPDATE ventures SET test_milestone_id = 16 WHERE id = 4")
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == [
        m.version for m in everything if m.version >= OURS.version
    ]
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        linked = dict(conn.execute("SELECT id, venture_id FROM projects ORDER BY id").fetchall())
        assert linked == {7: 1, 8: 4, 9: 4, 10: 4, 11: 1, 12: None}  # #12: its leg is parked, so no venture
        # Ember's code parks print on demand: the Nebenkosten test goes on, the posters' ends with it.
        scope = AgentScope("live", 0, 1)
        happened = stages.park(
            conn, scope, ventures.get(conn, scope, 4), "2026-10-29T06:00:00Z", "its first test was missed"
        )
        assert "dropped with it: #16, #31" in happened
        status = dict(conn.execute("SELECT id, status FROM milestones ORDER BY id").fetchall())
        assert status == {16: "dropped", 30: "open", 31: "dropped"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    upgraded.close()


def test_a_listing_of_a_line_without_a_venture_counts_for_its_channel_not_its_cycle_s(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    leg = rows(agent, f"SELECT id FROM ventures WHERE title = '{ventures.ETSY_LEG}' AND parent_id IS NULL")[0]["id"]
    pod = rows(agent, "SELECT id FROM ventures WHERE channel = 'printify'")[0]["id"]
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        cycle = conn.execute(  # a cycle aimed at print on demand made a digital listing of a line with no venture
            "INSERT INTO cycles (life_id, boot_id, started_at, status, trigger, simulated, cap_micros, session,"
            " venture_id) SELECT life_id, boot_id, ?, 'completed', 'schedule', simulated, cap_micros, session, ?"
            " FROM cycles ORDER BY id DESC LIMIT 1",
            (now, pod),
        ).lastrowid
        project = store.create_project(
            conn,
            scope,
            cycle_id=cycle,
            title="Budget planner",
            hypothesis="x",
            next_step="",
            status="abandoned",
            now=now,
        )
        request = store.insert_approval(
            conn,
            scope,
            cycle,
            now,
            project_id=project,
            type="sell",
            title="Etsy listing: Budget planner",
            description="A planner.",
            payload="Budget planner",
            expected_cost="USD 0.20",
            expected_benefit="sales",
            executor="etsy_listing",
            action="{}",
        )
        conn.execute(
            "INSERT INTO etsy_listings (mode, session, approval_id, started_at, finished_at, status, listing_id, title)"
            " VALUES (?, ?, ?, ?, ?, 'active', 4242, 'Budget planner')",
            (scope.mode, scope.session, request, now, now),
        )
        assert rows_of(conn, "SELECT venture_id FROM approvals WHERE id = ?", request) == [pod]  # the cycle's
        [row] = [r for r in metrics.listings(conn, scope, None, None) if r["listing_id"] == 4242]
    assert (row["for_project"], row["for_venture"]) == (project, leg)


def rows_of(conn: sqlite3.Connection, sql: str, *params: Any) -> list[Any]:
    return [r[0] for r in conn.execute(sql, params)]


# --- bug 5: what is unlocked comes from the grants; a change is news; the upgrade clears the stale notes -----------

# 0.13.0's labels, as its notes named the rules
LABELS_0130 = {
    "price_change": "price changes within 15% on a live listing",
    "deactivate": "taking a listing of Ember's off Etsy",
    "email_reply": "email replies in threads the other person started",
}


def unlocked_as_0130(conn: sqlite3.Connection, milestone_id: int, rule: str, level: str) -> None:
    """The owner's unlock as 0.13.0's set_autonomy made it: a grant, and its note over the milestone's last one (as
    roadmap.owner_word wrote it); the agent's plan of cycle #1 saw it."""
    conn.execute(
        "INSERT INTO policy_grants (mode, session, milestone_id, rule, level, per_day, budget, by, created_at)"
        " VALUES ('live', 0, ?, ?, ?, 3, 10, 'Stefan', ?)",
        (milestone_id, rule, level, THEN),
    )
    said = f"Unlocked for this milestone: {LABELS_0130[rule]} ({level.replace('_', ' ')}, at most 3 a day, 10 in all)"
    conn.execute(
        "UPDATE milestones SET owner_action = 'note', owner_comment = ?, owner_at = ?, owner_by = 'Stefan',"
        " owner_version = owner_version + 1, seen_cycle_id = 1, updated_at = ? WHERE id = ?",
        (said, THEN, THEN, milestone_id),
    )


def test_after_the_upgrade_from_0130_the_plan_says_nothing_is_unlocked_and_hears_the_take_back(data_dir: Path) -> None:
    db_file = data_dir / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 58], backup_dir=data_dir / "backups")  # 0.13.0
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', ?, 'born', 'alive')",
            (THEN,),
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros)"
            " VALUES (1, 1, 'b', ?, ?, 'completed', 'schedule', 0, 1)",
            (THEN, THEN),
        )
        conn.execute(
            "INSERT INTO projects (id, mode, session, life_id, created_cycle_id, created_at, updated_at, title,"
            " hypothesis, status) VALUES (7, 'live', 0, 1, 1, ?, ?, 'Nebenkostenabrechnung', 'x', 'active')",
            (THEN, THEN),
        )
        milestone_row = (
            "INSERT INTO milestones (id, mode, session, life_id, created_cycle_id, title, measure, first_due, due,"
            " created_at, updated_at, created_by, entered_by, owner_action, owner_at, owner_by, owner_version,"
            " seen_cycle_id, project_id, parent_id) VALUES (?, 'live', 0, 1, ?, ?, 'x', '2026-09-30', '2026-09-30', ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        owners = ("owner", "Stefan", "added", THEN, "Stefan", 1, 1, None, None)
        conn.execute(milestone_row, (1, None, "Ten sales", THEN, THEN, *owners))
        for mid, title, project in ((2, "First Nebenkosten sale", 7), (3, "A blog post", None)):  # leading to #1
            agents = ("agent", None, None, None, None, 0, None, project, 1)
            conn.execute(milestone_row, (mid, 1, title, THEN, THEN, *agents))
        unlocked_as_0130(conn, 1, "email_reply", "veto_window")
        unlocked_as_0130(conn, 2, "price_change", "auto")
        unlocked_as_0130(conn, 2, "deactivate", "veto_window")  # the note names the last click only
        conn.execute(  # the owner's own note: theirs, it stays
            "UPDATE milestones SET owner_action = 'note', owner_comment = 'Write it in German first', owner_at = ?,"
            " owner_by = 'Stefan', owner_version = 1, seen_cycle_id = 1 WHERE id = 3",
            (THEN,),
        )
    old.close()
    economy = make_economy(data_dir, LIVE)  # the upgrade: 0.15.0 took every unlock back (0062), 0071 clears the notes
    agent = Agent(economy.db, LoadedSettings(LIVE), economy, transport=ScriptedTransport(False), cycles_enabled=True)
    agent.recover()
    planner = agent.planner_preview()
    assert "Unlocked for this milestone" not in planner
    shown = section(planner, "ROADMAP")
    assert "note" not in line_of(shown, '#1 "Ten sales"') and "unlocked" not in line_of(shown, '#1 "Ten sales"')
    assert "your owner" not in line_of(shown, '#2 "First Nebenkosten sale"')
    assert line_of(shown, '#3 "A blog post"').endswith(' · your owner\'s note: "Write it in German first"')
    heard = section(planner, "SINCE YOUR LAST WAKE")
    nothing = "Nothing is unlocked on it now: its requests wait for your owner's click."
    assert (
        f'Unlocks of milestone #1 "Ten sales": Ember\'s code took back email replies in threads the other person '
        f"started (the upgrade to 0.15.0). {nothing}" in heard
    )
    assert (
        f'Unlocks of milestone #2 "First Nebenkosten sale": Ember\'s code took back price changes within 15% of the '
        f"approved price on a live listing and taking a listing of Ember's off Etsy (the upgrade to 0.15.0). {nothing}"
        in heard
    )
    # The Roadmap tab: no note posing as the owner's word, and nothing unlocked, as its Autonomy box says
    items = {m["id"]: m for m in agent.roadmap()["items"]}
    assert [(items[i]["owner_action"], items[i]["owner_comment"], items[i]["unlocked"]) for i in (1, 2, 3)] == [
        (None, None, ""),
        (None, None, ""),
        ("note", "Write it in German first", ""),
    ]
    deactivate = next(r for r in items[2]["autonomy"] if r["rule"] == "deactivate")
    assert (deactivate["level"], deactivate["why"]) == ("manual", "the upgrade to 0.15.0")
    # Ember's code's take-backs are news, not the owner's decision (they don't wake the agent); once a plan showed
    # them, they aren't news again.
    with agent.db.transaction() as conn:
        assert not news.decided_unseen(conn, agent.scope())
        items_heard = news.collect(conn, agent.db, agent.scope(), "0.0.0").items()
        assert [i for i in items_heard if i[0] == "unlock"] == [("unlock", 1, 4), ("unlock", 2, 6)]
        news.mark_seen(conn, 1, items_heard)
        assert news.collect(conn, agent.db, agent.scope(), "0.0.0").venture_lines() == []
    assert "Unlocks of milestone" not in agent.planner_preview()


def test_the_upgrade_clears_a_note_only_where_its_rule_stands_unlocked_nowhere(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    everything = discover_migrations()
    migrate(db_file, [m for m in everything if m.version < OURS.version], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', ?, 'born', 'alive')",
            (THEN,),
        )
        milestone_row = (
            "INSERT INTO milestones (id, mode, session, life_id, title, measure, first_due, due, created_at,"
            " updated_at, created_by, status, closed_at, closed_by, result, owner_action, owner_comment, owner_at,"
            " owner_by, owner_version) VALUES (?, 'live', 0, 1, ?, 'x', '2026-10-30', '2026-10-30', ?, ?, 'agent', ?,"
            " ?, ?, ?, 'note', ?, ?, 'Stefan', 1)"
        )
        notes = {
            1: "Unlocked for this milestone: taking a listing of Ember's off Etsy (veto window, at most 3 a day, 10 in"
            " all)",
            2: "Unlocked for this milestone: price changes within 15% of the approved price on a live listing (auto, at"
            " most 3 a day, 10 in all)",
            3: "Unlocked for this milestone: QA fixes: photos up to 5 on a live listing (auto, at most 3 a day, 10 in"
            " all)",
            4: "Keep the price at 4.90",
        }
        for mid, status in ((1, "open"), (2, "open"), (3, "done"), (4, "open")):
            closed = (THEN, "agent", "Met.") if status == "done" else (None, None, "")
            conn.execute(milestone_row, (mid, f"Milestone {mid}", THEN, THEN, status, *closed, notes[mid], THEN))
        grant_row = (
            "INSERT INTO policy_grants (id, mode, session, milestone_id, rule, level, per_day, budget, by, why,"
            " created_at) VALUES (?, 'live', 0, ?, ?, ?, 3, 10, ?, ?, ?)"
        )
        for gid, mid, rule, level, by, why in (
            (1, 1, "deactivate", "veto_window", "Stefan", None),  # stands: its note stays
            (2, 2, "deactivate", "veto_window", "Stefan", None),
            (3, 2, "price_change", "auto", "Stefan", None),
            (4, 2, "price_change", "manual", policy.REVOKED_BY, "your owner vetoed request #9"),  # taken back
            (5, 3, "qa_fix", "auto", "Stefan", None),  # of a closed milestone: it doesn't stand
            (6, 4, "qa_fix", "auto", "Stefan", None),
            (7, 4, "qa_fix", "manual", policy.REVOKED_BY, "its budget of 10 actions is spent"),
        ):
            conn.execute(grant_row, (gid, mid, rule, level, by, why, THEN))
    old.close()
    migrate(db_file, backup_dir=tmp_path / "backups")
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        kept = dict(conn.execute("SELECT id, owner_comment FROM milestones ORDER BY id").fetchall())
        assert kept == {1: notes[1], 2: None, 3: None, 4: notes[4]}
        cleared = conn.execute("SELECT owner_action, owner_at, owner_by FROM milestones WHERE id = 2").fetchone()
        assert tuple(cleared) == (None, None, None)
        # Ember's code's take-backs that stand on an open milestone are news once; the rest was heard before
        unseen = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM policy_grants WHERE id NOT IN (SELECT grant_id FROM policy_grants_seen) ORDER BY id"
            )
        ]
        assert unseen == [4, 7]
        scope = AgentScope("live", 0, 1)
        heard = [line for line in news.collect(conn, upgraded, scope, "0.0.0").venture_lines() if "Unlocks" in line]
        assert heard == [
            'Unlocks of milestone #2 "Milestone 2": Ember\'s code took back price changes within 15% of the approved'
            " price on a live listing (your owner vetoed request #9). Still unlocked on it: taking a listing of Ember's"
            " off Etsy (veto window, at most 3 a day, 10 in all).",
            'Unlocks of milestone #4 "Milestone 4": Ember\'s code took back QA fixes: photos up to 5 on a live listing'
            " (its budget of 10 actions is spent). Nothing is unlocked on it now: its requests wait for your owner's"
            " click.",
        ]
        with pytest.raises(sqlite3.IntegrityError, match="stays seen"):
            conn.execute("UPDATE policy_grants_seen SET cycle_id = NULL WHERE grant_id = 1")
    upgraded.close()


def test_an_unlock_is_said_from_its_grant_and_each_change_is_news(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    goal = a_milestone(agent)
    scope = agent.scope()

    def heard() -> list[str]:
        with agent.db.connection() as conn:
            return news.collect(conn, agent.db, scope, "0.0.0").venture_lines()

    def unseen() -> bool:
        with agent.db.connection() as conn:
            return news.decided_unseen(conn, scope)

    def seen() -> None:
        with agent.db.transaction() as conn:
            cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
            news.mark_seen(conn, cycle, news.collect(conn, agent.db, scope, "0.0.0").items())

    seen()
    assert owner(agent).set_autonomy(goal, {"rule": "deactivate", "level": "veto_window"}, "Stefan").status == 200
    granted = "taking a listing of Ember's off Etsy (veto window, at most 3 a day, 10 in all)"
    assert heard() == [f'Unlocks of milestone #{goal} "Ten sales": your owner unlocked {granted}.']
    assert unseen()  # the owner's word: what their decisions wake the agent for
    item = next(m for m in agent.roadmap()["items"] if m["id"] == goal)
    assert (item["owner_action"], item["owner_comment"], item["unlocked"]) == (None, None, granted)
    planner = agent.planner_preview()
    shown = line_of(section(planner, "ROADMAP"), f'#{goal} "Ten sales"')
    assert shown.endswith(" · your owner unlocked: taking listings off Etsy (veto window)")  # its room kept
    assert f"Unlocked by your owner (Ember's code carries these out without their click): {granted}" in (
        roadmap.focus_text(milestone(agent, goal), agent.clock.today(), None, unlocked=item["unlocked"])
    )
    seen()
    assert (heard(), unseen()) == ([], False)
    # Taking back what isn't unlocked changes nothing: no news
    assert owner(agent).set_autonomy(goal, {"rule": "price_change", "level": "manual"}, "Stefan").status == 200
    assert (heard(), unseen()) == ([], False)
    # Ember's code takes it back (its milestone was met): news, not the owner's decision
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE milestones SET status = 'done', result = 'Met.', closed_at = ?, closed_by = 'agent' WHERE id = ?",
            (to_iso(agent.clock.now()), goal),
        )
    agent.run_policy()
    assert heard() == [
        f"Unlocks of milestone #{goal} \"Ten sales\": Ember's code took back taking a listing of Ember's off Etsy"
        f" (milestone #{goal} was done). Nothing is unlocked on it now: its requests wait for your owner's click."
    ]
    assert not unseen()
    assert rows(agent, f"SELECT owner_comment FROM milestones WHERE id = {goal}") == [{"owner_comment": None}]
