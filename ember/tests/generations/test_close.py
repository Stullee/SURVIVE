"""0.50.0 (vision/generations.md, "The change, step by step"): at the change the old mind's open work closes as ended
with its generation: her lines, ventures, plan, milestones, bets, predictions and promises, and her unlocks are taken
back. Nothing of hers stands open forever where a later reading would find it. The owner's goal is theirs, as the
rulebook is: the next generation works toward it too."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import policy, roadmap  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.generation_helpers import decide_all, settled  # noqa: E402

OPEN = {
    "projects": "status IN ('idea', 'active', 'waiting')",
    "ventures": "stage NOT IN ('parked', 'killed')",
    "plan_nodes": "status = 'open'",
    "milestones": "status = 'open'",
    "bets": "status = 'open'",
    "predictions": "status = 'open'",
    "obligations": "status = 'open'",
}


def open_work(agent: Any, session: int) -> dict[str, int]:
    return {
        table: rows(agent, f"SELECT COUNT(*) AS n FROM {table} WHERE mode = 'dry_run' AND session = {session}"
                    f" AND {where}")[0]["n"]
        for table, where in OPEN.items()
    }  # fmt: skip


def busy(agent: Any) -> dict[str, int]:
    """A first generation with one of each open: a line with its bet and a promise about it, a venture, the owner's
    goal, a milestone of hers with a prediction and an unlock."""
    scope, now = agent.scope(), to_iso(agent.clock.now())
    due = (agent.clock.today() + timedelta(days=60)).isoformat()
    reply = owner(agent).set_goal({"amount_usd": "500", "per": "month", "due": due}, "Owner")
    assert reply.status == 200, reply.body
    message = owner(agent).send_message({"text": "When is the planner done?"}, "Owner").body["id"]
    with agent.db.transaction() as conn:
        line = conn.execute(
            "SELECT id FROM projects WHERE mode = ? AND session = ? AND status = 'active' ORDER BY id LIMIT 1",
            (scope.mode, scope.session),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO bets (mode, session, project_id, metric, gain, baseline, reach, expect, placed_at, due)"
            " VALUES (?, ?, ?, 'views', 20, 0, 1, '20 views from the pins', ?, ?)",
            (scope.mode, scope.session, line, now, due),
        )
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, message_id) VALUES (?, ?,"
            " 'promise', 'Finish the planner', ?, ?, ?)",
            (scope.mode, scope.session, due, now, message),
        )
        mine = roadmap.create(conn, scope, title="Ten orders", measure="10 orders", due=due, now=now)
        conn.execute(
            "INSERT INTO predictions (mode, session, kind, milestone_id, claim, probability, due, created_at)"
            " VALUES (?, ?, 'milestone', ?, 'Ten orders by then', 0.3, ?, ?)",
            (scope.mode, scope.session, mine, due, now),
        )
        policy.set_grant(conn, scope, mine, "deactivate", "auto", now, by="Owner")
    return open_work(agent, scope.session)


def test_the_change_closes_the_old_mind_s_open_work(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    old = agent.scope()
    before = busy(agent)
    assert all(before[t] for t in OPEN), before  # one of each, at least
    decide_all(agent, "passive")
    done = agent.begin_generation("Owner")
    assert open_work(agent, old.session) == dict.fromkeys(OPEN, 0)
    closed = done["closed"]
    assert closed["lines"] == before["projects"] and closed["ventures"] == before["ventures"]
    assert closed["plan_nodes"] == before["plan_nodes"] and closed["milestones"] == before["milestones"]
    assert (closed["bets"], closed["predictions"], closed["promises"], closed["unlocks"]) == (1, 1, 1, 1)
    why = "Ended with generation 1: Owner began generation 2."
    where = f"mode = 'dry_run' AND session = {old.session}"
    lines = rows(agent, f"SELECT status, notes FROM projects WHERE {where} AND updated_at = '{done_at(agent)}'")
    assert lines and all(r["status"] == "abandoned" and r["notes"].endswith(why) for r in lines)
    parked = rows(agent, f"SELECT parked_by, notes FROM ventures WHERE {where} AND notes LIKE '%{why}'")
    assert len(parked) == before["ventures"] and {r["parked_by"] for r in parked} == {"code"}
    goals = rows(agent, f"SELECT closed_by, result FROM milestones WHERE {where} AND owner_goal = 1")
    assert [(g["closed_by"], g["result"]) for g in goals] == [("owner", why)]  # the owner's change closed theirs
    [grant] = rows(agent, f"SELECT level FROM policy_grants WHERE {where} ORDER BY id DESC LIMIT 1")
    assert grant["level"] == "manual"
    with agent.db.connection() as conn:
        goal = roadmap.owner_goal(conn, agent.scope())
    assert goal is not None and done["goal"] == goal["id"]
    assert (goal["target"], goal["due"], goal["metric"]) == (500_000_000, before_due(agent), "revenue_month_usd")
    [event] = rows(agent, "SELECT message FROM events WHERE message LIKE '%began generation 2%'")
    assert "of the old mind's work" in event["message"] and f"your goal carried as #{goal['id']}" in event["message"]


def done_at(agent: Any) -> str:
    return rows(agent, "SELECT ended_at FROM lives WHERE state = 'ended'")[0]["ended_at"]


def before_due(agent: Any) -> str:
    return rows(agent, "SELECT due FROM milestones WHERE owner_goal = 1 ORDER BY id LIMIT 1")[0]["due"]


def test_a_change_without_a_goal_sets_none(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    decide_all(agent, "passive")
    done = agent.begin_generation("Owner")
    with agent.db.connection() as conn:
        assert roadmap.owner_goal(conn, agent.scope()) is None
    assert done["goal"] is None
