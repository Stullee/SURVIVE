"""0.49.0 (vision/generations.md, "How we know she is smarter"): the scorecard by generation. Each generation's weeks
end at the change, her own records count for the mind that made them (a first generation's open promise or line is
no second generation's), and each generation's first 14 and 28 days and last 28 sit side by side, with what became
of the principles it inherited."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app import scorecard  # noqa: E402
from app.agent import inheritance  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.agent_helpers import owner  # noqa: E402
from tests.generation_helpers import decide_all, settled  # noqa: E402
from tests.generations.test_inheritance import drafted  # noqa: E402


def a_promise(agent: object, due: str) -> None:
    """A promise of the mind that hands on, open, due on ``due`` (an owner's day)."""
    reply = owner(agent).send_message({"text": "Will you send the summary on Friday?"}, "Owner")
    scope = agent.scope()  # type: ignore[attr-defined]
    with agent.db.transaction() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO obligations (mode, session, kind, what, due, created_at, message_id) VALUES (?, ?,"
            " 'promise', 'Send the summary', ?, ?, ?)",
            (scope.mode, scope.session, due, to_iso(agent.clock.now()), reply.body["id"]),  # type: ignore[attr-defined]
        )


def test_each_generation_is_scored_on_its_own_records(data_dir: Path) -> None:
    agent, _ = settled(data_dir)
    first = agent.scope()
    a_promise(agent, (agent.clock.today() + timedelta(days=2)).isoformat())  # due after the change, never kept
    inheritance.give(agent, drafted(agent), None, "Owner")
    decide_all(agent, "passive")
    agent.clock.advance(days=1)  # the change comes a day later: the promise falls due in the second generation's time
    agent.begin_generation("Owner")
    agent.clock.advance(days=3)
    agent.run_cycle("schedule")
    agent.clock.advance(minutes=5)
    now = agent.clock.now()
    with agent.db.connection() as conn:
        card = scorecard.card(conn, "dry_run", now, agent.clock.tz)
        everyone = scorecard.Scorer(conn, "dry_run", now, agent.clock.tz)
        mixed = everyone.promises_kept(everyone.weeks()[-1])
        changed = conn.execute("SELECT ended_at FROM lives WHERE state = 'ended'").fetchone()[0]
        gen2_cycles = conn.execute(
            "SELECT COUNT(*) FROM cycles WHERE simulated = 1 AND session = ?", (first.session + 1,)
        ).fetchone()[0]
    gen1, gen2 = card["generations"]
    assert (gen1["session"], gen1["generation"], gen2["session"], gen2["generation"]) == (
        first.session,
        1,
        first.session + 1,
        2,
    )
    assert gen1["to"] == gen2["from"] == changed and gen2["to"] is None  # the change, not her first cycle
    assert card["scorecard"] == 3 and [w["session"] for w in card["weeks"]][-1] == first.session + 1
    last_of_1 = [w for w in card["weeks"] if w["session"] == first.session][-1]
    first_of_2 = [w for w in card["weeks"] if w["session"] == first.session + 1][0]
    assert last_of_1["complete"] and last_of_1["to"] <= first_of_2["from"]
    # the first generation's open promise falls due in the second's time: it is the first's, not hers
    assert mixed[1] == 1 and first_of_2["measures"]["O1"]["sample"] == 0
    assert card["alarms"]["today"]["cycles"] <= gen2_cycles
    assert set(gen2["stretches"]) == {"first_14_days", "first_28_days", "last_28_days"}
    assert gen1["stretches"]["last_28_days"]["complete"] and not gen2["stretches"]["first_14_days"]["complete"]
    assert gen2["inherited"] == {"inherited": 1, "established": 0, "hypothesis": 1, "disputed": 0, "retired": 0}
    assert gen1["inherited"]["inherited"] == 0
    text = scorecard.markdown(card)
    assert "## Generations" in text and "gen 2, first 14 days (so far)" in text and "(gen 2)" in text
    assert "gen 2 inherited 1 principles: 0 established" in text
    assert json.loads(json.dumps(card)) == card  # the export writes it as JSON


def test_one_generation_reads_as_before(data_dir: Path) -> None:
    """Until a change the scorecard is the one it was, its generation named: no comparison yet."""
    agent, _ = settled(data_dir)
    agent.clock.advance(minutes=5)
    with agent.db.connection() as conn:
        card = scorecard.card(conn, "dry_run", agent.clock.now(), agent.clock.tz)
        before = scorecard.Scorer(conn, "dry_run", agent.clock.now(), agent.clock.tz)
        weeks = [scorecard._scored(before, w) for w in before.weeks()]
    [gen] = card["generations"]
    assert (
        gen["generation"] == 1
        and [{k: v for k, v in w.items() if k not in ("session", "generation")} for w in card["weeks"]] == weeks
    )
    assert "## Generations" not in scorecard.markdown(card)
