"""0.13.0: a product line's listing test, the bars of the analysis' section 8 as milestones Ember's code sets and
checks: 10 views by day 7, 30 views and 2 favorites by day 14, a first order by day 21, counted from the day its first
listing is live, one bar at a time (0.14.0: day 14's views, then its favorites). Etsy's own numbers grade them (no
views history kept); a miss is owed with its bar's action, a first order sets a milestone to scale the product line,
and a closed project takes its open bars with it."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import gates, metrics, tools  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import listed  # noqa: E402


def keep(agent: Any) -> list[str]:
    with agent.db.transaction() as conn:
        return gates.keep(conn, agent.scope(), agent.clock.today(), to_iso(agent.clock.now()))


def bars(agent: Any) -> dict[str, dict[str, Any]]:
    """The listing test's milestones by bar."""
    found = rows(
        agent, "SELECT g.gate, g.started_on, m.* FROM listing_gates g JOIN milestones m ON m.id = g.milestone_id"
    )
    return {r["gate"]: r for r in found}


def started(data_dir: Path) -> tuple[Any, int]:
    """A dry-run agent whose first listing is live, with its product line's test begun; the project's number."""
    agent, _ = listed(data_dir)
    [project] = rows(agent, "SELECT a.project_id FROM approvals a WHERE a.executor = 'etsy_listing'")
    assert project["project_id"], "the fake's listing belongs to its focus project"
    happened = keep(agent)
    assert happened[-1].startswith(f"Ember's code began the listing test of project #{project['project_id']}")
    return agent, int(project["project_id"])


def close(agent: Any, key: str, status: str) -> None:
    """A bar closed as Ember's code closes it (the fake shop's numbers only ever grow)."""
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        conn.execute(
            "UPDATE milestones SET status = ?, result = 'Ember''s code checked it: views_total 4 views', closed_at = ?,"
            " closed_by = 'code', updated_at = ? WHERE id = ?",
            (status, now, now, bars(agent)[key]["id"]),
        )


def test_a_product_line_s_test_begins_with_its_first_live_listing_one_bar_at_a_time(data_dir: Path) -> None:
    agent, project = started(data_dir)
    today = agent.clock.today()
    first = bars(agent)
    assert set(first) == {"day7_views"}  # one bar open at a time
    row = first["day7_views"]
    assert (row["metric"], row["target"], row["created_by"], row["kind"]) == ("views_total", 10, "code", "first_test")
    assert (row["project_id"], row["status"], row["started_on"]) == (project, "open", today.isoformat())
    assert row["due"] == (today + timedelta(days=7)).isoformat() and row["title"].startswith("Day 7: 10 views of ")
    assert "Missed: fix their titles, tags and category once" in row["measure"]
    assert keep(agent) == []  # the bar is being checked
    agent.clock.advance(days=2)
    close(agent, "day7_views", "done")
    keep(agent)
    for key, metric, target in (("day14_views", "views_total", 30), ("day14_favorites", "favorites_total", 2)):
        found = bars(agent)
        assert [k for k, r in found.items() if r["status"] == "open"] == [key]  # 0.14.0: views first, then favorites
        assert (found[key]["metric"], found[key]["target"]) == (metric, target)
        assert found[key]["due"] == (today + timedelta(days=14)).isoformat()  # from the start, not from now
        assert keep(agent) == []  # this bar is being checked
        close(agent, key, "done")
        keep(agent)
    last = bars(agent)["day21_sale"]
    assert (last["metric"], last["target"], last["due"]) == (
        "orders_total",
        1,
        (today + timedelta(days=21)).isoformat(),
    )
    assert "Met: scale it (5 variants or a bundle)" in last["measure"]


def test_the_bars_are_graded_from_etsy_s_numbers_without_a_history(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    agent.clock.advance(minutes=61)
    agent.sync_shop()
    assert bars(agent)["day7_views"]["status"] == "open"  # a few views so far
    agent.clock.advance(hours=14)  # two views an hour in the fake shop, a favorite in nine
    agent.sync_shop()
    assert bars(agent)["day7_views"]["status"] == "done"
    assert bars(agent)["day7_views"]["result"].startswith("Ember's code checked it: views_total ")
    for key in ("day14_views", "day14_favorites"):  # 0.14.0: one bar at a time
        keep(agent)  # the next bar, checked at the next sync
        agent.clock.advance(minutes=61)
        agent.sync_shop()
        assert bars(agent)[key]["status"] == "done"
    keep(agent)
    assert bars(agent)["day21_sale"]["status"] == "open"
    assert rows(agent, "SELECT COUNT(*) AS n FROM obligations")[0]["n"] == 0  # nothing owed for bars met


def test_a_miss_is_owed_with_its_bar_s_action(data_dir: Path) -> None:
    agent, _ = started(data_dir)
    close(agent, "day7_views", "missed")
    happened = keep(agent)
    assert happened[0].startswith("Obligation: fix the titles, tags and category of its listings once")
    [owed] = rows(agent, "SELECT kind, what, milestone_id FROM obligations")
    assert owed["kind"] == "miss" and owed["milestone_id"] == bars(agent)["day7_views"]["id"]
    assert "fix the titles, tags and category of its listings once (propose_etsy_edit)" in owed["what"]
    assert set(bars(agent)) == {"day7_views", "day14_views"}  # a miss doesn't end the test
    close(agent, "day14_views", "missed")
    keep(agent)
    assert "day14_favorites" not in bars(agent)  # 0.14.0: day 14's bar is missed already
    owed_now = rows(agent, "SELECT what FROM obligations ORDER BY id")
    assert len(owed_now) == 2 and "park the product line (its project) with the numbers" in owed_now[1]["what"]
    assert keep(agent) == []  # owed once
    # Day 21 without an order: the fake shop's first listing never sells.
    agent.clock.advance(days=22)
    agent.sync_shop()
    assert bars(agent)["day21_sale"]["status"] == "missed"
    keep(agent)
    last = rows(agent, "SELECT what FROM obligations ORDER BY id DESC LIMIT 1")[0]["what"]
    assert "stop building this product type" in last


def test_a_first_order_by_day_21_sets_a_milestone_to_scale(data_dir: Path) -> None:
    agent, project = started(data_dir)
    for key in ("day7_views", "day14_views", "day14_favorites"):
        close(agent, key, "done")
        keep(agent)
    close(agent, "day21_sale", "done")
    happened = keep(agent)
    assert any(line.startswith("Ember's code set milestone #") and "scale project" in line for line in happened)
    scale = bars(agent)["scale"]
    assert scale["title"].startswith("Scale it: 5 variants or a bundle of ") and scale["metric"] is None
    assert (scale["created_by"], scale["kind"], scale["project_id"], scale["status"]) == (
        "code",
        "decision",
        project,
        "open",
    )
    assert scale["due"] == (agent.clock.today() + timedelta(days=gates.SCALE_DAYS)).isoformat()
    assert keep(agent) == []  # once


def test_a_closed_project_takes_its_open_bars_with_it(data_dir: Path) -> None:
    agent, project = started(data_dir)
    close(agent, "day7_views", "done")
    keep(agent)
    with agent.db.transaction() as conn:
        conn.execute("UPDATE projects SET status = 'abandoned' WHERE id = ?", (project,))
    assert len(keep(agent)) == 1  # 0.14.0: one bar open at a time
    statuses = {key: row["status"] for key, row in bars(agent).items()}
    assert statuses == {"day7_views": "done", "day14_views": "dropped"}
    assert keep(agent) == []


def test_only_ember_s_code_sets_the_order_bar() -> None:
    plan = next(d for d in tools.definitions(etsy=True) if d["name"] == "milestone_plan")
    offered = plan["input_schema"]["properties"]["milestones"]["items"]["properties"]["metric"]["enum"]
    assert "orders_total" not in offered and "views_delta" in offered
    assert {"views_total", "favorites_total"} <= set(offered)  # 0.14.0: the agent's goals use them too
    assert metrics.CATALOGUE["orders_total"].code_only and not metrics.CATALOGUE["views_total"].history
