"""A research model checked before it takes over (0.12.0).

Research ran on the worker model. The owner can name a cheaper research model (``research_model``, Claude Haiku 4.5
say), but a cheaper model may find less. So its first ``QUESTIONS`` research questions are paired: each is asked of
the worker model, whose answer the agent reads, and of the research model, whose answer is only compared. Once
``QUESTIONS`` are compared, the research model takes over if it answered at least ``QUESTIONS - 1`` of them and found
web pages for as many questions as the worker model did, less one; otherwise research stays on the worker model.
Every comparison is kept (``research_checks``), and the System log says how it came out.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .store import AgentScope

QUESTIONS = 10  # questions compared before a research model takes over


@dataclass(frozen=True)
class Check:
    """Where the check of ``model`` stands."""

    model: str
    compared: int  # questions compared so far
    answered: int  # of which the research model answered
    found: int  # of which the research model found web pages for
    worker_found: int  # and the worker model

    @property
    def done(self) -> bool:
        return self.compared >= QUESTIONS

    @property
    def passed(self) -> bool:
        return self.done and self.answered >= QUESTIONS - 1 and self.found >= self.worker_found - 1

    def text(self) -> str:
        """How the check stands, for the System log and the dashboard."""
        if not self.done:
            return f"{self.compared} of {QUESTIONS} research questions compared with {self.model}"
        found = (
            f"{self.model} answered {self.answered} of {self.compared} and found web pages for {self.found}, the "
            f"worker model for {self.worker_found}"
        )
        return f"passed: {found}; research runs on it now" if self.passed else f"failed: {found}; research stays"


def check(conn: sqlite3.Connection, scope: AgentScope, model: str) -> Check:
    where, params = scope.where()
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(candidate_ok), 0), COALESCE(SUM(candidate_sources > 0), 0),"
        f" COALESCE(SUM(worker_sources > 0), 0) FROM research_checks WHERE {where} AND model = ?",
        (*params, model),
    ).fetchone()
    return Check(model, int(row[0]), int(row[1]), int(row[2]), int(row[3]))


def record(
    conn: sqlite3.Connection,
    scope: AgentScope,
    model: str,
    cycle_id: int | None,
    question: str,
    worker: tuple[int | None, int, int],
    candidate: tuple[int | None, int, int, bool],
    now: str,
) -> Check:
    """Keep one comparison: (call id, pages found, digest characters) of the worker's answer, and of the research
    model's with whether it answered. Returns the check as it stands after it."""
    conn.execute(
        "INSERT INTO research_checks (mode, session, model, created_at, cycle_id, question, worker_call_id,"
        " candidate_call_id, worker_sources, candidate_sources, worker_chars, candidate_chars, candidate_ok)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            model,
            now,
            cycle_id,
            question[:500] or "?",
            worker[0],
            candidate[0],
            worker[1],
            candidate[1],
            worker[2],
            candidate[2],
            1 if candidate[3] else 0,
        ),
    )
    return check(conn, scope, model)
