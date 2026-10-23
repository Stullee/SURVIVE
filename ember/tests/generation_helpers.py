"""0.47.0: a dry-run agent before and after a generation change (vision/generations.md), for the tests of the estate
and of the new mind."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent import estate
from tests.agent_helpers import owner, rows
from tests.etsy_helpers import listed


def settled(data_dir: Path) -> tuple[Any, int]:
    """A dry-run agent with one live listing and nothing waiting for the owner (the fake's other requests rejected)."""
    agent, listing = listed(data_dir)
    for other in rows(agent, "SELECT id FROM approvals WHERE status = 'pending'"):
        owner(agent).decide(other["id"], {"decision": "reject", "comment": "Not now."}, "Owner")
    return agent, listing


def decide_all(agent: Any, verdict: str, test: dict[str, Any] | None = None) -> list[Any]:
    """The owner's verdict on every product of the hand-over."""
    with agent.db.transaction() as conn:
        estate.refresh(conn, agent.scope(), agent.clock)
        return [
            estate.decide(conn, agent.scope(), agent.clock, r["kind"], r["ref"], verdict, "Owner", test)
            for r in estate.handover(conn, agent.scope())
        ]


def handed_on(data_dir: Path, verdict: str, test: dict[str, Any] | None = None) -> tuple[Any, int]:
    """A second generation after its first cycle, its predecessor's one listing handed on with ``verdict``."""
    agent, listing = settled(data_dir)
    decide_all(agent, verdict, test)
    agent.begin_generation("Owner")
    agent.run_cycle("schedule")
    return agent, listing


def files_in(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
