"""Harness for the plan-tree review: a fresh data dir per script, the tests' helpers, a few seeders."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(tempfile.mkdtemp(prefix="ember-repro-plan-"))  # outside the repository
os.environ["EMBER_DATA_DIR"] = str(DATA)
os.environ["EMBER_SCHEDULER"] = "off"
os.environ["EMBER_FAKE_DELAY_MS"] = "0"
os.environ.pop("EMBER_OPTIONS_PATH", None)
os.environ.pop("EMBER_DEV_MODE", None)

from app.agent import plan, store, templates, weights  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402

ALL = {"pinterest": True, "bluesky": True, "blog": True}


def new_agent(titles=()):
    from tests.test_fixes_0280 import lined

    agent, fake = lined(DATA, titles=titles)
    return agent, fake


def now(agent) -> str:
    return to_iso(agent.clock.now())


def rows(agent, sql, args=()):
    with agent.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def keep(agent, channels=ALL):
    with agent.db.transaction() as conn:
        return plan.keep(conn, agent.scope(), now(agent), agent.clock.today(), channels)


def candidates(agent, channels=ALL, exploring=False):
    with agent.db.connection() as conn:
        return plan.candidates(conn, agent.scope(), now(agent), agent.clock.today(), channels, exploring)


def steer(agent, channels=ALL, exploring=False, cycle_id=None):
    with agent.db.connection() as conn:
        return plan.steer(conn, agent.scope(), now(agent), agent.clock.today(), channels, exploring=exploring,
                          cycle_id=cycle_id)


def project(agent, title, hypothesis="Someone pays 5 EUR for it", status="active", venture_id=None):
    with agent.db.transaction() as conn:
        return store.create_project(conn, agent.scope(), cycle_id=1, title=title, hypothesis=hypothesis,
                                    next_step="make it", status=status, now=now(agent), venture_id=venture_id)


def show(cands, only_line=None):
    for c in cands:
        if only_line is not None and c.step.product != only_line:
            continue
        print(f"  step #{c.step.id:<4} line={c.step.product} stage={c.stage:<9} waiting={c.waiting!s:<8} "
              f"pinned={c.step.pinned!s:<5} kind={c.step.kind:<7} worth={c.step.worth} urg={c.step.urgency} "
              f"title={c.step.title[:60]!r}")


def tree(agent, line=None):
    sql = ("SELECT id, parent_id, level, project_id, stage, kind, title, status, waiting, wait_ref, template, "
           "check_kind, obligation_id, pinned, hold_reason, hold_by FROM plan_nodes ORDER BY id")
    out = rows(agent, sql)
    return [r for r in out if line is None or r["project_id"] == line]
