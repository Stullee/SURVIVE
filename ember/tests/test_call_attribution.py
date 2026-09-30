"""0.12.0: what each model call and each request to the owner worked for. A cycle's whole cost was charged to its focus
(its plan too), and a research call for another venture counted for the focus one. Now each call names the venture and
milestone its work served, plans, reviews and brainstorms are overhead, and a request to the owner names the venture
and milestone of the cycle that made it."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.agent import roadmap, ventures
from app.agent.fake_llm import FakeTransport, Plan, Reply, ToolCalls
from app.db import Database, discover_migrations, migrate
from tests.test_agent import rows
from tests.test_loop_shapes import run
from tests.test_ventures import DROPSHIPPING, JOURNAL, PRINT, RESEARCH, VENTURING, found


def research(venture_id: int | None = None) -> tuple[str, dict[str, Any]]:
    question = f"{RESEARCH[1]['question']} (venture {venture_id})"  # 0.12.0: the same question is answered from before
    return ("research", {"question": question, **({"venture_id": venture_id} if venture_id else {})})


def test_each_call_names_what_its_work_served(data_dir: Path) -> None:
    focus = Plan(
        {
            "assessment": "ok",
            "goal": "Research dropshipping for the money goal",
            "money_path": "A business case my owner can back",
            "focus_project_id": None,
            "focus_venture_id": DROPSHIPPING,
            "focus_milestone_id": 1,
            "steps": ["research the venture"],
            "sleep_minutes": 120,
        }
    )
    fake = FakeTransport(
        script=[
            focus,
            ToolCalls([research(), research(PRINT)]),
            found("https://example.invalid/a"),
            found("https://example.invalid/b"),
            ToolCalls(
                [
                    (
                        "request_approval",
                        {
                            "type": "other",
                            "title": "May I test a supplier?",
                            "description": "One sample order.",
                            "payload": "Order one sample.",
                            "expected_cost": "None to you",
                            "expected_benefit": "A delivery time",
                        },
                    )
                ]
            ),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent, ends = run(data_dir, fake, settings=VENTURING)
    assert ends[0].status == "completed"
    calls = rows(agent, "SELECT purpose, venture_id, milestone_id, overhead, cost_micros FROM llm_calls ORDER BY id")
    by = [(c["purpose"], c["venture_id"], c["milestone_id"], c["overhead"]) for c in calls]
    assert by[0] == ("plan", None, None, 1)
    assert ("research", DROPSHIPPING, 1, 0) in by and ("research", PRINT, 1, 0) in by  # the venture it names
    assert all(v == DROPSHIPPING and m == 1 and o == 0 for p, v, m, o in by if p in ("work", "reflect"))
    with agent.db.connection() as conn:
        spent = ventures.money(conn, agent.scope())
        effort = roadmap.effort(conn, agent.scope())
        overhead = roadmap.overhead(conn, agent.scope())
    served = sum(c["cost_micros"] for c in calls if c["overhead"] == 0)
    assert spent[DROPSHIPPING].spent + spent[PRINT].spent == served  # the plan isn't charged to a venture
    assert spent[PRINT].spent == sum(c["cost_micros"] for c in calls if c["venture_id"] == PRINT)
    assert effort[1] == (1, served) and overhead == calls[0]["cost_micros"]
    [request] = rows(agent, "SELECT venture_id, milestone_id FROM approvals")
    assert request == {"venture_id": DROPSHIPPING, "milestone_id": 1}
    with agent.db.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="worked for is fixed"):
        conn.execute("UPDATE approvals SET venture_id = NULL")


def test_the_history_is_attributed_from_its_cycles(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 33], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        conn.execute(
            "INSERT INTO ventures (id, mode, session, life_id, created_by, created_at, updated_at, title, pitch, stage)"
            " VALUES (7, 'live', 0, 1, 'agent', 'then', 'then', 'Planners', 'Planners', 'researching')"
        )
        conn.execute(
            "INSERT INTO cycles (id, life_id, boot_id, started_at, status, trigger, simulated, cap_micros, venture_id)"
            " VALUES (1, 1, 'b', 'then', 'completed', 'schedule', 0, 1, 7)"
        )
        insert = (
            "INSERT INTO llm_calls (id, boot_id, cycle_id, purpose, model, simulated, status, ts, local_day,"
            " cost_micros) VALUES (?, 'b', 1, ?, 'm', 0, 'ok', 'then', '2026-09-01', 100)"
        )
        conn.execute(insert, (1, "plan"))
        conn.execute(insert, (2, "work"))
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == list(range(34, 55))
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        got = [tuple(r) for r in conn.execute("SELECT id, venture_id, overhead FROM llm_calls ORDER BY id")]
        assert got == [(1, None, 1), (2, 7, 0)]
        with pytest.raises(sqlite3.IntegrityError, match="finalized call cannot change"):
            conn.execute("UPDATE llm_calls SET venture_id = NULL WHERE id = 2")
