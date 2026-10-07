"""0.29.0: the owner's goal leads the roadmap. The owner sets one goal at its root (earn an amount a month, or in total,
by a date), Ember's code checks it from the books, and everything else leads to it: the agent splits it into sub-goals,
and Ember's code links whatever leads to no goal. While it stands, the money goal Ember's code kept gives way to it;
once it closes (met, missed, removed), the money goal stands in until the owner sets the next one. Every milestone says
how far it got: by its metric, by the books, or by the steps that lead to it, with its pace."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import fake_llm, roadmap, tools
from app.agent.fake_llm import FakeTransport
from app.agent.service import Agent
from app.economy.clock import to_iso
from tests.economy_helpers import owner as owner_entry
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_owner_api import post
from tests.test_owner_loop import owner
from tests.test_roadmap import TODAY, plan, row

APP_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "app.js"


def call(agent: Agent, tool: str, **args: Any) -> tools.Outcome:
    """A tool as the model calls it, its milestones as given (no parent filled in)."""
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )
    llm_call = rows(agent, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, tool, args, f"toolu_{tool}", llm_call, "act")


def day(agent: Agent, days: int) -> str:
    return (agent.clock.today() + timedelta(days=days)).isoformat()


def made(outcome: tools.Outcome) -> int:
    assert outcome.ok, outcome.text
    return int(outcome.text.split("#", 1)[1].split(" ", 1)[0])


def milestone(agent: Agent, milestone_id: int) -> dict[str, Any]:
    return rows(agent, f"SELECT * FROM milestones WHERE id = {milestone_id}")[0]


def set_goal(agent: Agent, **body: Any) -> Any:
    return owner(agent).set_goal({"amount_usd": "1000", "per": "month", "due": day(agent, 120), **body}, "Felix")


def keep(agent: Agent, settle: bool = True) -> list[str]:
    """Ember's code's keeper of the goal at the root, as before a plan."""
    now = agent.clock.now()
    books = agent.economy.life.scope()
    start = now - timedelta(days=roadmap.MONEY_WINDOW_DAYS)
    earned = agent.economy.books.net_revenue_between(books, start, now)
    spent = agent.economy.books.api_spend_between(books, start, now)
    with agent.db.transaction() as conn:
        return roadmap.keep_money_goal(
            conn, agent.scope(), agent.clock.today(), to_iso(now), earned, spent, None, settle
        )


def started(data_dir: Path, plans: int = 1) -> tuple[Agent, int]:
    """An agent after its first plan (the money goal #1 with its decision points #2 and #3) with a sub-goal of its
    own under it (#4); ``plans``: the plans its fake model has for this cycle and the next."""
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[]) for _ in range(plans)]))
    step = made(
        call(
            agent,
            "milestone_plan",
            milestones=[dict(title="Printables bring $50 a month", measure="x", due=day(agent, 60), parent="#1")],
        )
    )
    return agent, step


# --- the owner sets the goal ---


def test_the_owners_goal_takes_the_money_goals_place(data_dir: Path) -> None:
    agent, step = started(data_dir)
    reply = set_goal(agent, comment="Pay for itself and then some")
    assert reply.status == 200, reply.body
    goal = milestone(agent, reply.body["id"])
    assert (goal["title"], goal["created_by"], goal["entered_by"], goal["owner_goal"], goal["parent_id"]) == (
        "Earn $1,000 a month",
        "owner",
        "Felix",
        1,
        None,
    )
    assert (goal["metric"], goal["target"], goal["due"], goal["owner_comment"]) == (
        "revenue_month_usd",
        1_000_000_000,
        day(agent, 120),
        "Pay for itself and then some",
    )
    assert goal["measure"].startswith("Over the last 30 days, the revenue recorded, less expenses, is at least $1,000")
    money = milestone(agent, 1)
    assert (money["status"], money["closed_by"]) == ("dropped", "code")
    assert money["result"] == f"Your owner set their goal #{goal['id']}: it takes the place of this one."
    assert [(milestone(agent, i)["status"], milestone(agent, i)["closed_by"]) for i in (2, 3)] == [
        ("dropped", "code")
    ] * 2
    assert milestone(agent, step)["parent_id"] == goal["id"]  # what led to the money goal leads to the owner's
    events = [e["message"] for e in agent.db.recent_events(limit=10)]
    assert f"Felix set the goal #{goal['id']}" in events
    keep(agent)  # the next plan: the owner's goal gets its decision points, and no money goal comes back
    with agent.db.connection() as conn:
        assert roadmap.money_goal(conn, agent.scope()) is None
        assert roadmap.root(conn, agent.scope())["id"] == goal["id"]
        decisions = [r for r in roadmap.children(conn, goal["id"]) if r["kind"] == "decision"]
    assert [(r["title"], r["due"]) for r in decisions] == [
        (roadmap.DECISION_TITLE, day(agent, 30)),  # a quarter and half of its 120 days (no spending: no runway)
        (roadmap.DECISION_TITLE, day(agent, 60)),
    ]
    keep(agent)
    with agent.db.connection() as conn:
        assert len([r for r in roadmap.children(conn, goal["id"]) if r["kind"] == "decision"]) == 2  # once


def test_the_goal_is_checked(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    for body, field in (
        ({"per": "week"}, "per"),
        ({"amount_usd": "lots"}, "amount_usd"),
        ({"amount_usd": True}, "amount_usd"),
        ({"amount_usd": "0.50"}, "amount_usd"),
        ({"amount_usd": "100001"}, "amount_usd"),
        ({"due": "next year"}, "due"),
        ({"due": day(agent, 6)}, "due"),  # a week ahead at the earliest
        ({"due": day(agent, 367)}, "due"),
        ({"replaces": 99}, "replaces"),  # the owner saw a goal that isn't there
        ({"color": "blue"}, "color"),
    ):
        reply = set_goal(agent, **body)
        assert reply.status in (409, 422) and reply.body["field"] == field, (body, reply.body)
    thousand = set_goal(agent, amount_usd="1,000")  # a thousand, not one
    assert thousand.status == 200 and milestone(agent, thousand.body["id"])["target"] == 1_000_000_000
    stale = set_goal(agent, replaces=None)  # the owner saw no goal, but one stands now
    assert (
        stale.status == 409
        and stale.body["error"] == f"your goal changed meanwhile (#{thousand.body['id']} stands now)"
    )


def test_one_goal_at_a_time_that_only_the_owner_and_ember_s_code_close(data_dir: Path) -> None:
    agent, step = started(data_dir)
    goal = set_goal(agent).body["id"]
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        for sql, error in (
            ("UPDATE milestones SET status = 'missed', closed_at = ?, closed_by = 'agent' WHERE id = ?", "closes"),
            ("UPDATE milestones SET parent_id = 4, updated_at = ? WHERE id = ?", "leads to nothing"),
            ("UPDATE milestones SET proposed_due = '2027-01-01', proposed_at = ? WHERE id = ?", "sets the date"),
            ("UPDATE milestones SET owner_goal = 0, updated_at = ? WHERE id = ?", "is fixed"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=error):
                conn.execute(sql, (now, goal))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            roadmap.create(
                conn,
                agent.scope(),
                title="Another",
                measure="x",
                due=day(agent, 30),
                now=now,
                created_by="owner",
                metric="revenue_month_usd",
                target=1,
                owner_goal=True,
            )
        with pytest.raises(sqlite3.IntegrityError, match="measured from the books"):
            roadmap.create(conn, agent.scope(), title="Mine", measure="x", due=day(agent, 30), now=now, owner_goal=True)
    for args in (
        {"status": "dropped", "result": "Too far."},
        {"due": day(agent, 200), "note": "Later."},
        {"wait_for": "buyers", "check_at": day(agent, 3)},
        {"parent_id": step},
    ):
        refused = call(agent, "milestone_update", milestone_id=goal, **args)
        assert not refused.ok and "is your owner's goal" in refused.text, (args, refused.text)
    assert call(agent, "milestone_update", milestone_id=goal, note="Two legs toward it this month.").ok


def test_a_goal_raised_in_total_keeps_counting_from_its_day(data_dir: Path) -> None:
    agent, step = started(data_dir)
    first = set_goal(agent, per="total", amount_usd="500").body["id"]
    keep(agent)
    assert milestone(agent, first)["counts_from"] == agent.clock.today().isoformat()
    began = agent.clock.today().isoformat()
    agent.clock.advance(days=10)
    owner_entry(agent.economy, "revenue", "120", test_money=True)
    second = set_goal(agent, per="total", amount_usd="800", due=day(agent, 90), replaces=first)
    assert second.status == 200 and second.body["replaced"] == first
    new = milestone(agent, second.body["id"])
    assert (new["title"], new["counts_from"], new["replaces_id"]) == ("Earn $800 in total", began, first)
    assert new["measure"].startswith(f"From {began} on, the revenue recorded, less expenses, adds up to at least $800")
    old = milestone(agent, first)
    assert (old["status"], old["closed_by"], old["result"]) == (
        "dropped",
        "owner",
        "Your owner set a new goal in its place.",
    )
    assert milestone(agent, step)["parent_id"] == new["id"]
    with agent.db.connection() as conn:
        decisions = [r for r in roadmap.children(conn, first) if r["kind"] == "decision"]
    assert decisions and all(r["status"] == "dropped" for r in decisions)  # the new goal brings its own
    view = agent.roadmap()
    assert view["goal"]["id"] == new["id"] and view["goal"]["progress"]["text"] == "$120 of $800"  # counted from day 1
    monthly = set_goal(agent, per="month", amount_usd="100", replaces=new["id"]).body["id"]
    assert milestone(agent, monthly)["counts_from"] is None  # a month counts the last 30 days


def test_the_goal_met_closes_and_the_money_goal_stands_in(data_dir: Path) -> None:
    agent, step = started(data_dir, plans=2)
    goal = set_goal(agent, amount_usd="200").body["id"]
    keep(agent)
    owner_entry(agent.economy, "revenue", "120", test_money=True)
    assert agent.roadmap()["goal"]["progress"]["percent"] == 60  # read from the books at once
    owner_entry(agent.economy, "revenue", "90", test_money=True)
    agent.run_cycle("schedule")
    met = milestone(agent, goal)
    assert (met["status"], met["closed_by"]) == ("done", "code") and met["result"].startswith(
        "Ember's code checked it: revenue_month_usd $210.00"
    )
    with agent.db.connection() as conn:
        money = roadmap.money_goal(conn, agent.scope())
    assert money is not None  # in the same plan: the goal never stands empty
    assert milestone(agent, step)["parent_id"] == money["id"]
    view = agent.roadmap()
    assert view["goal"]["id"] == money["id"] and not view["goal"]["owner"]
    assert view["last_goal"]["id"] == goal and view["last_goal"]["status"] == "done"


def test_the_owner_removes_their_goal_and_the_work_goes_on(data_dir: Path) -> None:
    agent, step = started(data_dir)
    goal = set_goal(agent).body["id"]
    keep(agent)
    reply = owner(agent).decide_milestone(goal, {"action": "drop", "comment": "Too much for now."}, "Felix")
    assert reply.status == 200 and len(reply.body["dropped_with"]) == 2, reply.body  # its two decision points
    assert (milestone(agent, step)["status"], milestone(agent, step)["parent_id"]) == ("open", None)
    keep(agent)
    with agent.db.connection() as conn:
        money = roadmap.money_goal(conn, agent.scope())
    assert money is not None and milestone(agent, step)["parent_id"] == money["id"]
    with agent.db.connection() as conn:
        news = [roadmap.news_line(r) for r in conn.execute("SELECT * FROM milestones WHERE id = ?", (goal,))]
    assert news[0].startswith("Your owner removed their goal milestone #")


# --- everything leads to the goal ---


def test_the_agents_milestones_lead_to_the_goal(data_dir: Path) -> None:
    agent, step = started(data_dir)
    loose = call(agent, "milestone_plan", milestones=[dict(title="Somewhere", measure="x", due=day(agent, 10))])
    assert not loose.ok and loose.text.startswith(
        'Error: every milestone leads to the goal #1 "Earn as much as you spend": give parent #1'
    ), loose.text
    later = call(  # the money goal stands in: what leads to it may be due after it (it leads to the next one)
        agent, "milestone_plan", milestones=[dict(title="Far", measure="x", due=day(agent, 200), parent="#1")]
    )
    assert later.ok, later.text
    goal = set_goal(agent, due=day(agent, 100)).body["id"]
    beyond = call(
        agent,
        "milestone_plan",
        milestones=[dict(title="After it", measure="x", due=day(agent, 101), parent=f"#{goal}")],
    )
    assert not beyond.ok and f"milestone #{goal} is due {day(agent, 100)}" in beyond.text
    two = call(
        agent,
        "milestone_plan",
        milestones=[
            dict(
                key="leg",
                title="Posters bring $200 a month",
                metric="revenue_month_usd",
                target="200",
                due=day(agent, 90),
                parent=f"#{goal}",
            ),
            dict(parent="leg", title="10 posters live", measure="10 listings", due=day(agent, 20)),
        ],
    )
    assert two.ok, two.text
    reply = owner(agent).add_milestone({"title": "Ask me first", "measure": "We talked", "due": day(agent, 9)}, "Felix")
    assert reply.status == 201 and milestone(agent, reply.body["id"])["parent_id"] == goal  # the owner's too


def test_what_leads_to_no_goal_is_linked_to_it(data_dir: Path) -> None:
    agent, step = started(data_dir)
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        orphan = roadmap.create(conn, agent.scope(), title="From before", measure="x", due=day(agent, 9), now=now)
        mid = roadmap.create(
            conn, agent.scope(), title="Middle", measure="x", due=day(agent, 9), now=now, parent_id=step
        )
        below = roadmap.create(
            conn, agent.scope(), title="Below", measure="x", due=day(agent, 5), now=now, parent_id=mid
        )
        conn.execute(
            "UPDATE milestones SET status = 'done', result = '1', closed_at = ?, closed_by = 'agent' WHERE id = ?",
            (now, mid),
        )
        linked = roadmap.adopt(conn, agent.scope(), 1, now)
    assert sorted(linked) == sorted([orphan, below])
    assert milestone(agent, orphan)["parent_id"] == 1
    assert milestone(agent, below)["parent_id"] == step  # the nearest open one above it


# --- how far each milestone got ---


def test_progress_by_metric_books_and_steps() -> None:
    today = TODAY
    goal = row(
        id=1,
        owner_goal=1,
        metric="revenue_month_usd",
        target=1_000_000_000,
        progress=340_000_000,
        created_at="2026-08-01T10:00:00Z",
        due="2026-10-01",
    )
    leg = row(id=2, parent_id=1, title="Leg", due="2026-09-30", created_at="2026-09-01T10:00:00Z")
    done = row(id=3, parent_id=2, status="done", closed_by="agent", metric=None)
    views = row(
        id=4,
        parent_id=2,
        metric="views_total",
        target=30,
        progress=12,
        created_at="2026-09-01T10:00:00Z",
        due="2026-09-15",
    )
    dropped = row(id=5, parent_id=2, status="dropped")
    ceiling = row(id=6, parent_id=2, metric="api_spend_usd", target=2_000_000, progress=500_000)
    leaf = row(id=7, parent_id=1, title="Decide", due="2026-09-20")
    money = row(id=8, kind="money_goal", created_at="2026-09-01T10:00:00Z", due="2026-11-30")
    found = roadmap.progress([goal, leg, done, views, dropped, ceiling, leaf, money], today, (5_000_000, 20_000_000))
    assert found[1] == roadmap.Progress(34, "metric", "$340 of $1,000", "behind", 50)  # 31 of 61 days gone
    assert found[4] == roadmap.Progress(40, "metric", "12 of 30 views", "ahead", 0)
    assert found[3] == roadmap.Progress(100, "done")
    assert found[5].percent is None and found[6].percent is None and found[6].text == "$0.50 of $2 at most"
    assert found[2] == roadmap.Progress(70, "steps", "1 of 2 steps done", "ahead", 0)  # (100 + 40) / 2
    assert found[7] == roadmap.Progress(0, "open")  # nothing measures it until it is done
    assert found[8] == roadmap.Progress(25, "books", "$5 of $20", "ahead", 0)
    assert roadmap.progress([money], today)[8] == roadmap.Progress(None, "books")


def test_the_plan_shows_the_goal_first_and_how_far_everything_got(data_dir: Path) -> None:
    agent, step = started(data_dir)
    goal = set_goal(agent, amount_usd="100", comment="Cover the hosting").body["id"]
    agent.clock.advance(days=100)  # far behind its pace
    text = roadmap_text(agent)
    lines = text.split("\n")
    assert lines[1].startswith(f'Your owner\'s goal: #{goal} "Earn $100 a month" · due ')
    assert " · 0%: not checked yet (" in lines[1] and "% of its time gone: behind)" in lines[1]
    assert "Ember's code checks it from the books; only your owner changes it" in lines[1]
    assert 'Their word on it: "Cover the hosting"' in lines[1]
    assert f"Roadmap check: the goal #{goal} is at 0% with " in text
    assert "% of its time gone. Say in your plan what changes" in text
    assert f"Sub-goals (they lead to the goal #{goal}; the rest leads to them):" in text
    assert f'#{step} "Printables bring $50 a month"' in text


def roadmap_text(agent: Agent) -> str:
    with agent.db.connection() as conn:
        scope = agent.scope()
        today = agent.clock.today()
        found = roadmap.progress_for(conn, scope, today)
        return roadmap.planner_text(roadmap.open_milestones(conn, scope), [], today, progress=found)


def test_nothing_of_the_agents_leads_to_the_goal_yet(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[plan(steps=[])]))
    text = roadmap_text(agent)
    assert "The goal (Ember's code's, until your owner sets theirs): #1 \"Earn as much as you spend\"" in text
    assert "Roadmap check: nothing of yours leads to the goal #1 yet. Split it with milestone_plan" in text
    steps, focus = fake_llm.roadmap_plan(f"== ROADMAP ==\n{text}\n\n== OTHER ==\n")
    assert steps == [fake_llm.GOAL_STEP.format(id=1, due=milestone(agent, 1)["due"])] and focus is None


def test_the_dry_run_splits_the_goal(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(seed=7), cycles=2)
    laid = rows(agent, "SELECT id, parent_id, created_by, due FROM milestones WHERE created_by = 'agent' ORDER BY id")
    assert laid and laid[0]["parent_id"] == 1, laid  # the fake's first under the money goal
    assert all(r["parent_id"] is not None for r in laid)


# --- the owner's view ---


def test_the_goal_on_the_roadmap_tab_and_the_overview(ingress_client: TestClient) -> None:
    due = (date.fromisoformat(ingress_client.get("api/roadmap").json()["today"]) + timedelta(days=60)).isoformat()
    reply = post(
        ingress_client, "api/roadmap/goal", {"amount_usd": "250.50", "per": "total", "due": due, "replaces": None}
    )
    assert reply.status_code == 200, reply.json()
    goal = reply.json()["id"]
    view = ingress_client.get("api/roadmap").json()
    assert view["goal"]["id"] == goal and view["goal"]["owner"] and view["goal"]["title"] == "Earn $250.50 in total"
    assert (view["goal"]["per"], view["goal"]["target_usd"], view["goal"]["due"]) == ("total", 250.5, due)
    assert view["goal"]["progress"] == {
        "percent": 0,
        "basis": "metric",
        "text": "$0 of $250.50",
        "pace": "on pace",
        "elapsed": 0,
    }
    [item] = view["items"]
    assert item["owner_goal"] is True and item["kind"] is None and item["progress"]["basis"] == "metric"
    summary = ingress_client.get("api/dashboard").json()["roadmap"]["goal"]
    assert summary["id"] == goal and summary["progress"]["text"] == "$0 of $250.50"
    bad = post(ingress_client, "api/roadmap/goal", {"amount_usd": "x", "per": "month", "due": due})
    assert bad.status_code == 422 and bad.json()["field"] == "amount_usd"
    html = ingress_client.get("/").text
    for element in ("goal-strip", "rm-goal-card", "rm-goal-form", "rm-goal-amount", "rm-goal-due", "rm-tree"):
        assert f'id="{element}"' in html, element


def test_the_dashboard_reads_amounts_as_the_owner_writes_them() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node isn't installed")
    source = APP_JS.read_text(encoding="utf-8")
    body = re.search(r"\n  function goalAmount\(text\) \{.*?\n  \}\n", source, re.DOTALL)
    assert body is not None
    written = ["$1,000", "1000", "1000.5", "1000,50", "1.000,00", "1,00,0", "-5", "abc", "250 usd"]
    driver = body.group(0) + f"console.log(JSON.stringify({json.dumps(written)}.map(goalAmount)));"
    done = subprocess.run([node, "-e", driver], capture_output=True, text=True, timeout=30, check=True)  # noqa: S603
    out = done.stdout
    assert out.strip() == '["1000","1000","1000.5","1000.50","","","","","250"]'
