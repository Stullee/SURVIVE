"""0.45.0 (scorecard 2, evaluation 2026-10-22, C2): the scorecard weighs cycles by what their calls cost once settled,
and its X2 counts the work a cycle left undone, not every call that wasn't done."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app import scorecard
from app.agent.fake_llm import FakeTransport
from tests.agent_helpers import rows, run


def scorer(agent: Any) -> scorecard.Scorer:
    with agent.db.connection() as conn:
        return scorecard.Scorer(conn, "dry_run", agent.clock.now(), agent.clock.tz)


def a_call(agent: Any, cycle_id: int, status: str, cost: int, floor: int, uncertain: int, usage: Any) -> int:
    """A call of the cycle as Ember's code books it (its rows never change once finished)."""
    now, day = agent.clock.now().isoformat(), agent.clock.today().isoformat()
    with agent.db.transaction() as conn:
        made = conn.execute(
            "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day, cost_micros,"
            " floor_micros, billing_uncertain, usage_raw) VALUES ('test', ?, 'work', 'scripted', 1, ?, ?, ?, ?, ?, ?,"
            " ?)",
            (cycle_id, status, now, day, cost, floor, uncertain, json.dumps(usage) if usage is not None else None),
        ).lastrowid
        conn.execute(
            "INSERT INTO ledger (ts, occurred_on, type, amount_micros, simulated, llm_call_id, created_by)"
            " VALUES (?, ?, 'api_cost', ?, 1, ?, 'system')",
            (now, day, cost, made),
        )
    return int(made)


def test_a_cycle_weighs_what_its_calls_cost_once_settled(data_dir: Path) -> None:
    """Live, 180 calls of 10-15 and 10-16 stood at about four times their price after 0.39.8 had settled them: E3
    read 74.6% for a real 78.7%, and 0.39.6 $0.922 a cycle for a real $0.258."""
    agent, _ = run(data_dir, FakeTransport(), cycles=2)
    cycle = rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"]
    before = scorer(agent).cost[cycle]
    read = {"input_tokens": 1_000, "output_tokens": 200, "speed": "standard"}
    a_call(agent, cycle, "ok", 4_000_000, 1_000_000, 1, read)  # an unknown usage field: its worst case, settled
    a_call(agent, cycle, "interrupted", 2_000_000, 0, 1, None)  # interrupted: what it was charged
    a_call(agent, cycle, "ok", 600_000, 600_000, 0, read)  # a known bill: as booked
    card = scorer(agent)
    assert card.cost[cycle] == before + 1_000_000 + 2_000_000 + 600_000
    [version] = card.versions()
    assert version["cost_usd"] == round(sum(card.cost.values()) / 1_000_000, 2)
    assert scorecard.VERSION == 3


def test_x2_counts_the_work_left_undone(data_dir: Path) -> None:
    """Version 1 counted every call not done: X2 read 10% in the week 10-12 to 10-18 while 86% of the cycles weren't
    cut by a limit."""
    agent, _ = run(data_dir, FakeTransport(), cycles=1)
    agent.clock.advance(minutes=5)  # the cycle's minute has passed: its week counts it
    [cycle] = rows(agent, "SELECT id, act_end_reason FROM cycles")
    assert cycle["act_end_reason"] not in scorecard.LIMITS
    call = rows(agent, f"SELECT MAX(id) AS id FROM llm_calls WHERE cycle_id = {cycle['id']}")[0]["id"]
    seq = iter(range(900, 999))

    def tool(name: str, status: str, **args: Any) -> None:
        now = agent.clock.now().isoformat()
        with agent.db.transaction() as conn:
            conn.execute(
                "INSERT INTO tool_calls (cycle_id, llm_call_id, seq, phase, origin, tool, tool_use_id, input, status,"
                " started_at) VALUES (?, ?, ?, 'act', 'local', ?, ?, ?, ?, ?)",
                (cycle["id"], call, next(seq), name, f"toolu_{name}", json.dumps(args), status, now),
            )

    base = scorer(agent).left_undone(cycle["id"])
    tool("make_document", "error", source="a.md", output="shop/a.pdf", path="shop/a.md")
    assert scorer(agent).left_undone(cycle["id"]) == base + 1
    tool("make_document", "ok", source="a.md", output="shop/a.pdf", path="shop/a.md")  # done after all
    assert scorer(agent).left_undone(cycle["id"]) == base
    tool("propose_pin", "error", listing_id=7)
    tool("propose_pin", "ok", listing_id=8)  # another listing's pin: #7's is still undone
    tool("write_journal", "error", entry="early")  # a journal tried during the work is no work
    assert scorer(agent).left_undone(cycle["id"]) == base + 1
    card = scorer(agent)
    week = card.weeks()[-1]
    finished, sample = card.work_finished(week)
    assert sample == 1 and finished == 0.0
    tool("propose_pin", "ok", listing_id=7)
    assert scorer(agent).work_finished(week) == (1.0 if base == 0 else 0.0, 1)
