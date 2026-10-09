"""Shared helpers for the plan-tree reproductions (read-only on the repo: everything lives in a fresh data dir)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, ".")

BASE = Path("/tmp/ember-repro/plan/data")
BASE.mkdir(parents=True, exist_ok=True)


def fresh_dir(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=name + "-", dir=BASE))
    os.environ["EMBER_DATA_DIR"] = str(d)
    os.environ.setdefault("EMBER_SCHEDULER", "off")
    os.environ.setdefault("EMBER_FAKE_DELAY_MS", "0")
    return d


# set before the app imports read it
_first = fresh_dir("boot")

from app.agent import obligations, plan, store, templates, weights  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_fixes_0280 import cycle, lined, now  # noqa: E402
from tests.test_fixes_0340 import ALL, keep, project  # noqa: E402
from tests.test_fixes_0351 import promise  # noqa: E402

NO_VENTURES = True


def no_ventures() -> None:
    """As conftest does for tests not marked exploring: the plan lays out no ventures."""
    plan._ventures = lambda conn, scope, now: []  # type: ignore[assignment]


def steer(agent: Any, exploring: bool = False, channels: dict[str, bool] = ALL, cycle_id: int | None = None):
    with agent.db.connection() as conn:
        return plan.steer(
            conn, agent.scope(), now(agent), agent.clock.today(), channels, exploring=exploring, cycle_id=cycle_id
        )


def run_cycle(agent: Any, exploring: bool = False, channels: dict[str, bool] = ALL) -> plan.Steer:
    """One simulated wake cycle as loop.py runs the plan part: keep, steer, open a cycle row on the step's line (a
    venture cycle flagged), record the pick. Nothing gets done by the agent."""
    keep(agent, channels)
    s = steer(agent, exploring, channels)
    cid = cycle(agent, s.line, status="completed")
    with agent.db.transaction() as conn:
        if s.kind == "venture":
            store.update_cycle(conn, cid, venture=1)
        if s.kind == "marketing":
            store.update_cycle(conn, cid, marketing=1)
        plan.record(conn, agent.scope(), cid, now(agent), s)
    return s


def describe(s: plan.Steer) -> str:
    if s.step is None:
        return f"{s.kind:9} none"
    p = s.pick.parts
    return (
        f"{s.kind:9} #{s.step.id:<3} line={s.line} {s.pick.decided:6} w={p.total:6.2f}"
        f" (worth {p.worth:.2f} kind {p.kind} urg {p.urgency:.1f} age {p.age:.2f} mom {p.momentum:.0f})"
        f" {s.step.title[:60]}"
    )


def ranked(s: plan.Steer, top: int = 6) -> str:
    return "\n".join(
        f"    {st.id:<4} line={st.product} {p.total:6.2f} {st.title[:70]}" for st, p in s.pick.ranked[:top]
    )
