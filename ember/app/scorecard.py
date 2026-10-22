"""The scorecard (0.44.0, vision/evaluation.md): the evaluation's computed measures, week by week, from the records.

An evaluation scores eight parts of Ember's engine. About half of its measures are arithmetic on her records (what
she spent and on what, the tool calls that went through, the promises kept, the bets placed); the rest a person or a
model grades (her reasoning, her products, her honesty). This module computes the first half, so that every
evaluation starts from the same numbers under the same definitions (``DEFINITIONS``, versioned by ``VERSION``)
instead of writing its own queries each week:

* each measure (``Kpi``) of each of the owner's weeks (Monday to Sunday, their time zone) since her first cycle, with
  its sample; under its minimum sample it is "too early" and scores nothing;
* its score: 0 at its floor, 100 at its target, linear between (where lower is better, the floor is the higher
  number); a part scores the mean of its measures once at least half of them are measured (the graded ones count as
  not measured here, so reasoning and discipline wait for the evaluation); Outcome and Capability weigh the parts;
* the alarms: today's and the last 7 days' productive spend, tool calls not done, promises kept and cycles the owner
  started, the numbers that would have shown a day of cycles on nothing the same afternoon;
* by version: what each app version's cycles cost and did, so a release can be checked against the one before.

It reads a database (the evaluation export's copy, export.py, before it is masked) and writes numbers only: no text
of the records goes into it. It runs no model call. Ember never reads it: a score she saw would become a target.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

VERSION = 1  # the definitions' version: a changed floor, target or formula gets a new one
LIMITS = ("step limit reached", "the conversation got too long")  # how a cycle's work was cut (loop's end reasons)
PRODUCT_TOOLS = ("make_document", "make_spreadsheet", "make_image", "make_cost_statement", "resize_image", "workshop")
BATCHED_TOOLS = ("venture_update", "plan_step", "project_update")  # decisions one cycle can make together
SMALL_REQUESTS = ("pinterest_pin", "pinterest_test_pin", "bluesky_post")  # a pin or a post counts a fifth
NOT_SHIPPED = ("pinterest_delete", "bluesky_delete", "printify_delete", "site_restore", "live_will")  # Undos, the will
RUNWAY_CAP = 365.0  # days: what a runway with no net burn shows
ANSWER_HOURS = 12  # an owner's message answered within this many hours counts as answered
CASE_DAYS = 2  # a retrospective within this many days of what settled counts
LOOKBACK_DAYS = 28  # calibration, evidence and the critic's cases: the last four weeks
TEST_DAYS = 56  # the first tests: the last eight weeks
WEIGHTS = {  # vision/evaluation.md: Outcome and Capability
    "outcome": {"economy": 0.6, "market": 0.4},
    "capability": {
        "execution": 0.2,
        "reasoning": 0.2,
        "learning": 0.2,
        "venturing": 0.15,
        "collaboration": 0.15,
        "discipline": 0.1,
    },
}


@dataclass(frozen=True)
class Kpi:
    key: str
    part: str
    name: str
    unit: str
    floor: float
    target: float
    sample: int  # the least sample it is judged on (0: any)
    lever: str  # who mostly moves it: agent, code or both
    how: str  # what it counts, from which records

    def score(self, value: float) -> float:
        span = self.target - self.floor
        return round(max(0.0, min(1.0, (value - self.floor) / span)) * 100, 1)


DEFINITIONS = (
    Kpi("E1", "economy", "Revenue to spend", "ratio", 0, 1, 0, "both", "revenue / (API spend + expenses), ledger"),
    Kpi("E2", "economy", "Net runway", "days", 7, 60, 0, "both", "balance at the week's end / the week's net burn"),
    Kpi(
        "E3",
        "economy",
        "Productive spend",
        "share",
        0.4,
        0.85,
        10,
        "both",
        "share of the cycles' cost spent in cycles that moved something: a request proposed, a product made, a plan"
        " step closed, an owner's message answered, a venture moved (not parked within a day of its idea), a bet, a"
        " daily review or a weekly look",
    ),
    Kpi("E4", "economy", "Cost per shipped thing", "USD", 10, 2, 1, "both", "API spend / requests approved to go out"),
    Kpi("M1", "market", "Views per live listing", "views", 1, 25, 3, "both", "listing views gained (observations)"),
    Kpi("M2", "market", "Favorites per 100 views", "per 100", 0, 5, 50, "both", "favorites gained / views gained"),
    Kpi("M3", "market", "Sales", "orders", 0, 5, 0, "both", "Etsy and Printify orders (KDP's: outside.csv)"),
    Kpi("M4", "market", "Clicks per pin", "clicks", 0, 1, 5, "both", "clicks of the pins sent in the last 4 weeks"),
    Kpi("X1", "execution", "Tool calls done", "share", 0.8, 0.97, 50, "both", "work-step tool calls that ended ok"),
    Kpi(
        "X2",
        "execution",
        "Work finished",
        "share",
        0.5,
        0.9,
        10,
        "both",
        "cycles whose work wasn't cut by a limit and left nothing undone",
    ),
    Kpi("X3", "execution", "First-pass approvals", "share", 0.5, 0.9, 3, "agent", "the owner's decisions, unchanged"),
    Kpi("X6", "execution", "Critic score", "1-10", 4, 8, 3, "agent", "the newest quality check of each listing"),
    Kpi("R4", "reasoning", "Batching", "decisions", 1, 3, 5, "both", "venture, plan and project updates a cycle"),
    Kpi("L1", "learning", "Bet coverage", "share", 0, 1, 2, "agent", "live products with an open bet"),
    Kpi("L2", "learning", "Retrospectives", "share", 0.5, 1, 3, "both", "what settled with a case within 2 days"),
    Kpi("L3", "learning", "Calibration (Brier)", "Brier", 0.3, 0.12, 5, "agent", "predictions of the last 4 weeks"),
    Kpi("V1", "venturing", "Cost per venture decision", "USD", 0.6, 0.1, 5, "both", "venture cycles' cost / moves"),
    Kpi("V2", "venturing", "Ideas given a look", "share", 0.2, 0.8, 5, "agent", "parked ideas researched first"),
    Kpi("V3", "venturing", "Independent evidence", "share", 0.3, 0.8, 5, "agent", "evidence of the last 4 weeks"),
    Kpi("V4", "venturing", "Case realism", "ratio", 0.2, 0.8, 1, "agent", "the critic's expected net / hers"),
    Kpi("V5", "venturing", "First tests met", "share", 0, 0.5, 2, "both", "first tests closed in the last 8 weeks"),
    Kpi("V6", "venturing", "Days to a verdict", "days", 21, 5, 3, "both", "an idea's days to proposed or researched"),
    Kpi("O1", "collaboration", "Promises kept", "share", 0.5, 0.95, 2, "both", "promises due, closed by their day"),
    Kpi("O2", "collaboration", "Answers", "share", 0.7, 1, 2, "agent", "owner's messages answered within 12 hours"),
    Kpi("O6", "collaboration", "Self-starting", "share", 0.5, 0.9, 10, "both", "cycles not started by Wake now"),
)
BY_KEY = {k.key: k for k in DEFINITIONS}
# Graded by the evaluation, not computed here (vision/evaluation.md): each part counts them toward its half
GRADED = {
    "E5": ("economy", "Spend on stopped work"),
    "X4": ("execution", "Platform first-pass"),
    "X5": ("execution", "Escaped defects"),
    "R1": ("reasoning", "Belief accuracy"),
    "R2": ("reasoning", "Cycle audit"),
    "R3": ("reasoning", "Verdict follow-through"),
    "L4": ("learning", "Clean memory"),
    "L5": ("learning", "Repeat mistakes"),
    "L6": ("learning", "Lessons applied"),
    "O3": ("collaboration", "The owner's time"),
    "O4": ("collaboration", "Instruction fidelity"),
    "O5": ("collaboration", "Message discipline"),
    "D1": ("discipline", "Attempts against the owner's rules"),
    "D2": ("discipline", "Honest record"),
    "D3": ("discipline", "Incidents"),
}
PARTS = ("economy", "market", "execution", "reasoning", "learning", "venturing", "collaboration", "discipline")


@dataclass(frozen=True)
class Window:
    """One of the owner's weeks (or days), as UTC times [start, end) and their local dates."""

    start: str
    end: str
    first: date
    last: date
    complete: bool

    @property
    def days(self) -> list[str]:
        return [(self.first + timedelta(days=n)).isoformat() for n in range((self.last - self.first).days + 1)]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _start(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tz).astimezone(UTC)


def window(first: date, days: int, now: datetime, tz: tzinfo) -> Window:
    """``days`` of the owner's from ``first``, cut at ``now`` (complete once it has passed)."""
    end = _start(first + timedelta(days=days), tz)
    cut = min(end, now)
    last = min(first + timedelta(days=days - 1), now.astimezone(tz).date())
    return Window(_iso(_start(first, tz)), _iso(cut), first, last, now >= end)


class Scorer:
    """The measures of one mode's records ('live' or 'dry_run') in a database."""

    def __init__(self, conn: sqlite3.Connection, mode: str, now: datetime, tz: tzinfo) -> None:
        self.conn = conn
        self.mode = mode
        self.sim = 1 if mode == "dry_run" else 0
        self.now = now
        self.tz = tz

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        return self.conn.execute(sql, params).fetchall()

    # --- the weeks ---

    def weeks(self) -> list[Window]:
        """Each of the owner's weeks from the one of her first cycle to this one."""
        first = self.one("SELECT MIN(started_at) FROM cycles WHERE simulated = ?", (self.sim,))
        if first is None:
            return []
        day = datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(self.tz).date()
        monday = day - timedelta(days=day.weekday())
        today = self.now.astimezone(self.tz).date()
        found = []
        while monday <= today:
            found.append(window(monday, 7, self.now, self.tz))
            monday += timedelta(days=7)
        return found

    def measure(self, w: Window) -> dict[str, tuple[float | None, int]]:
        """Every computed measure of a window: its value (None: nothing to measure) and its sample."""
        found: dict[str, tuple[float | None, int]] = {}
        for key, fn in self._measures().items():
            found[key] = fn(w)
        return found

    def _measures(self) -> dict[str, Callable[[Window], tuple[float | None, int]]]:
        return {
            "E1": self.revenue_to_spend,
            "E2": self.net_runway,
            "E3": self.productive_spend,
            "E4": self.cost_per_shipped,
            "M1": lambda w: self.listing_gains(w)[0],
            "M2": lambda w: self.listing_gains(w)[1],
            "M3": self.sales,
            "M4": self.pin_clicks,
            "X1": self.tools_done,
            "X2": self.work_finished,
            "X3": self.first_pass,
            "X6": self.critic_score,
            "R4": self.batching,
            "L1": self.bet_coverage,
            "L2": self.retrospectives,
            "L3": self.calibration,
            "V1": self.cost_per_decision,
            "V2": self.ideas_looked_at,
            "V3": self.evidence_quality,
            "V4": self.case_realism,
            "V5": self.first_tests,
            "V6": self.days_to_verdict,
            "O1": self.promises_kept,
            "O2": self.answers,
            "O6": self.self_starting,
        }

    # --- economy ---

    def _ledger(self, types: str, w: Window) -> int:
        return int(
            self.one(
                f"SELECT COALESCE(SUM(amount_micros), 0) FROM ledger WHERE type IN {types} AND simulated = ?"
                " AND ts >= ? AND ts < ?",
                (self.sim, w.start, w.end),
            )
        )

    def _spend(self, w: Window) -> int:
        """API spend (net of corrections) and expenses, in micro-dollars."""
        return self._ledger("('api_cost', 'api_cost_correction', 'expense')", w)

    def revenue_to_spend(self, w: Window) -> tuple[float | None, int]:
        spend = self._spend(w)
        if spend <= 0:
            return None, 0
        return self._ledger("('revenue')", w) / spend, 1

    def net_runway(self, w: Window) -> tuple[float | None, int]:
        balance = self.one(
            "SELECT COALESCE(SUM(CASE WHEN type IN ('owner_grant', 'revenue', 'adjustment') THEN amount_micros"
            " ELSE -amount_micros END), 0) FROM ledger WHERE simulated IN (0, ?) AND ts < ?",
            (self.sim, w.end),
        )  # the owner's grants are real money in a dry run too (ledger.Scope: its whole sessions, here)
        days = max((w.last - w.first).days + 1, 1)
        burn = (self._spend(w) - self._ledger("('revenue')", w)) / days
        if burn <= 0:
            return RUNWAY_CAP, 1
        return min(max(int(balance), 0) / burn, RUNWAY_CAP), 1

    def _cycles(self, w: Window) -> list[Any]:
        return self.rows(
            "SELECT c.*, COALESCE((SELECT SUM(cost_micros) FROM llm_calls l WHERE l.cycle_id = c.id), 0) AS cost"
            " FROM cycles c WHERE c.simulated = ? AND c.started_at >= ? AND c.started_at < ? ORDER BY c.id",
            (self.sim, w.start, w.end),
        )

    def moved(self, c: Any) -> bool:
        """Whether a cycle moved something (E3's list)."""
        cid, start, end = c["id"], c["started_at"], c["ended_at"] or c["started_at"]
        marks = ", ".join("?" for _ in PRODUCT_TOOLS)
        checks: tuple[tuple[str, tuple[Any, ...]], ...] = (
            ("SELECT 1 FROM approvals WHERE cycle_id = ?", (cid,)),
            (
                f"SELECT 1 FROM tool_calls WHERE cycle_id = ? AND status = 'ok' AND tool IN ({marks})",
                (cid, *PRODUCT_TOOLS),
            ),
            ("SELECT 1 FROM bets WHERE cycle_id = ?", (cid,)),
            ("SELECT 1 FROM reviews WHERE cycle_id = ? AND status = 'ok'", (cid,)),
            ("SELECT 1 FROM weekly_reviews WHERE cycle_id = ? AND status = 'ok'", (cid,)),
            (
                "SELECT 1 FROM messages o JOIN messages a ON a.id = o.answered_by WHERE a.cycle_id = ?",
                (cid,),
            ),
            (
                "SELECT 1 FROM plan_nodes WHERE mode = ? AND status = 'done' AND closed_by IN ('agent', 'code')"
                " AND closed_at >= ? AND closed_at <= ?",
                (self.mode, start, end),
            ),
            (
                "SELECT 1 FROM ventures WHERE mode = ? AND stage_at >= ? AND stage_at <= ? AND NOT (stage = 'parked'"
                " AND julianday(stage_at) - julianday(created_at) < 1)",
                (self.mode, start, end),
            ),
        )
        return any(self.one(sql + " LIMIT 1", params) for sql, params in checks)

    def productive_spend(self, w: Window) -> tuple[float | None, int]:
        cycles = self._cycles(w)
        total = sum(int(c["cost"]) for c in cycles)
        if not cycles or total <= 0:
            return None, len(cycles)
        return sum(int(c["cost"]) for c in cycles if self.moved(c)) / total, len(cycles)

    def cost_per_shipped(self, w: Window) -> tuple[float | None, int]:
        rows = self.rows(
            "SELECT executor, type FROM approvals WHERE mode = ? AND decided_at >= ? AND decided_at < ?"
            " AND status IN ('approved', 'approved_with_changes', 'done', 'failed')",
            (self.mode, w.start, w.end),
        )
        shipped = sum(
            0.2 if r["executor"] in SMALL_REQUESTS else 1.0
            for r in rows
            if r["executor"] not in NOT_SHIPPED and r["type"] in ("publish", "sell", "contact")
        )
        spend = self._ledger("('api_cost', 'api_cost_correction')", w)
        if shipped <= 0:
            return None, 0
        return spend / 1_000_000 / shipped, int(shipped)

    # --- market ---

    def listing_gains(self, w: Window) -> tuple[tuple[float | None, int], tuple[float | None, int]]:
        """(views gained per listing, favorites per 100 views gained) from the daily observations, which Ember's code
        keeps of each live listing while the owner's etsy_stats_history is on."""
        gains: dict[str, dict[int, int]] = {"views": {}, "favorites": {}}
        for metric, found in gains.items():
            for row in self.rows(
                "SELECT subject_id, MIN(CASE WHEN day < ? THEN NULL ELSE value END) AS low, MAX(value) AS high,"
                " (SELECT o2.value FROM observations o2 WHERE o2.mode = o.mode AND o2.subject = 'listing'"
                " AND o2.subject_id = o.subject_id AND o2.metric = o.metric AND o2.day < ? ORDER BY o2.day DESC"
                " LIMIT 1) AS before FROM observations o WHERE mode = ? AND subject = 'listing' AND metric = ?"
                " AND day <= ? GROUP BY subject_id HAVING MAX(day) >= ?",
                (w.first.isoformat(), w.first.isoformat(), self.mode, metric, w.last.isoformat(), w.first.isoformat()),
            ):
                base = row["before"] if row["before"] is not None else row["low"]
                if base is not None:
                    found[int(row["subject_id"])] = max(int(row["high"]) - int(base), 0)
        views = gains["views"]
        per_listing = (sum(views.values()) / len(views), len(views)) if views else (None, 0)
        seen = sum(views.values())
        liked = sum(gains["favorites"].get(listing, 0) for listing in views)
        per_hundred = (liked * 100 / seen, seen) if seen else (None, 0)
        return per_listing, per_hundred

    def sales(self, w: Window) -> tuple[float | None, int]:
        etsy = self.one(
            "SELECT COUNT(*) FROM etsy_orders WHERE mode = ? AND ordered_at >= ? AND ordered_at < ?"
            " AND COALESCE(status, 'paid') NOT IN ('canceled', 'fully refunded')",
            (self.mode, w.start, w.end),
        )
        printify = self.one(
            "SELECT COUNT(*) FROM printify_orders WHERE mode = ? AND created_at >= ? AND created_at < ?",
            (self.mode, w.start, w.end),
        )
        return float(etsy + printify), 1

    def pin_clicks(self, w: Window) -> tuple[float | None, int]:
        since = _iso(datetime.strptime(w.end, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=LOOKBACK_DAYS))
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(clicks), 0) FROM pinterest_pins WHERE mode = ? AND pin_id IS NOT NULL"
            " AND finished_at >= ? AND finished_at < ?",
            (self.mode, since, w.end),
        ).fetchone()
        return (row[1] / row[0], int(row[0])) if row[0] else (None, 0)

    # --- execution ---

    def tools_done(self, w: Window) -> tuple[float | None, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(t.status = 'ok'), 0) FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id"
            " WHERE c.simulated = ? AND t.phase = 'act' AND t.started_at >= ? AND t.started_at < ?",
            (self.sim, w.start, w.end),
        ).fetchone()
        return (row[1] / row[0], int(row[0])) if row[0] else (None, 0)

    def not_done(self, w: Window) -> int:
        """The work-step tool calls that didn't end ok (refused, failed, skipped or cut off)."""
        return int(
            self.one(
                "SELECT COUNT(*) FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id WHERE c.simulated = ?"
                " AND t.phase = 'act' AND t.status <> 'ok' AND t.started_at >= ? AND t.started_at < ?",
                (self.sim, w.start, w.end),
            )
        )

    def work_finished(self, w: Window) -> tuple[float | None, int]:
        marks = ", ".join("?" for _ in LIMITS)
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM((c.act_end_reason IS NULL OR c.act_end_reason NOT IN"
            f" ({marks})) AND COALESCE(d.undone, 0) = 0), 0) FROM cycles c LEFT JOIN cycle_digests d ON d.cycle_id ="
            " c.id WHERE c.simulated = ? AND c.started_at >= ? AND c.started_at < ? AND EXISTS (SELECT 1 FROM"
            " llm_calls l WHERE l.cycle_id = c.id AND l.purpose = 'work')",
            (*LIMITS, self.sim, w.start, w.end),
        ).fetchone()
        return (row[1] / row[0], int(row[0])) if row[0] else (None, 0)

    def first_pass(self, w: Window) -> tuple[float | None, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(status IN ('approved', 'done', 'failed') AND final_payload IS NULL), 0)"
            " FROM approvals WHERE mode = ? AND decided_at >= ? AND decided_at < ? AND status IN ('approved',"
            " 'approved_with_changes', 'rejected', 'done', 'failed') AND COALESCE(decided_by, '') NOT LIKE"
            " 'Ember''s code%'",
            (self.mode, w.start, w.end),
        ).fetchone()
        return (row[1] / row[0], int(row[0])) if row[0] else (None, 0)

    def critic_score(self, w: Window) -> tuple[float | None, int]:
        scores = [
            r[0]
            for r in self.rows(
                "SELECT q.score FROM quality_checks q WHERE q.mode = ? AND q.status = 'ok' AND q.created_at < ?"
                " AND q.id = (SELECT MAX(q2.id) FROM quality_checks q2 WHERE q2.mode = q.mode AND q2.status = 'ok'"
                " AND q2.created_at < ? AND COALESCE(q2.listing_id, -q2.project_id) = COALESCE(q.listing_id,"
                " -q.project_id))",
                (self.mode, w.end, w.end),
            )
        ]
        return (statistics.fmean(scores), len(scores)) if scores else (None, 0)

    # --- reasoning ---

    def batching(self, w: Window) -> tuple[float | None, int]:
        marks = ", ".join("?" for _ in BATCHED_TOOLS)
        counts = [
            r[0]
            for r in self.rows(
                f"SELECT COUNT(*) FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id WHERE c.simulated = ?"
                f" AND t.status = 'ok' AND t.tool IN ({marks}) AND c.started_at >= ? AND c.started_at < ?"
                " GROUP BY t.cycle_id",
                (self.sim, *BATCHED_TOOLS, w.start, w.end),
            )
        ]
        return (statistics.fmean(counts), len(counts)) if counts else (None, 0)

    # --- learning ---

    def bet_coverage(self, w: Window) -> tuple[float | None, int]:
        live = self.rows(
            "SELECT DISTINCT project_id FROM plan_nodes WHERE mode = ? AND level = 'product' AND project_id IS NOT"
            " NULL AND live_since IS NOT NULL AND live_since <= ? AND (status = 'open' OR closed_at >= ?)",
            (self.mode, w.last.isoformat(), w.end),
        )
        if not live:
            return None, 0
        covered = sum(
            1
            for (project,) in live
            if self.one(
                "SELECT 1 FROM bets WHERE mode = ? AND project_id = ? AND placed_at < ? AND (settled_at IS NULL OR"
                " settled_at >= ?) LIMIT 1",
                (self.mode, project, w.end, w.end),
            )
        )
        return covered / len(live), len(live)

    def settled(self, w: Window) -> list[tuple[str, str]]:
        """What settled in a window, as the daily review names it (learning.settled): its subject and when."""
        params = (self.mode, w.start, w.end)
        queries = (
            ("bet #", "SELECT id, settled_at FROM bets WHERE mode = ? AND settled_at >= ? AND settled_at < ?"),
            (
                "milestone #",
                "SELECT id, closed_at FROM milestones WHERE mode = ? AND metric IS NOT NULL AND status IN ('done',"
                " 'missed') AND closed_at >= ? AND closed_at < ?",
            ),
            (
                "project #",
                "SELECT id, updated_at FROM projects WHERE mode = ? AND status IN ('succeeded', 'failed',"
                " 'abandoned') AND updated_at >= ? AND updated_at < ?",
            ),
            (
                "venture #",
                "SELECT id, stage_at FROM ventures WHERE mode = ? AND stage IN ('parked', 'killed') AND stage_at >= ?"
                " AND stage_at < ?",
            ),
            (
                "request #",
                "SELECT id, decided_at FROM approvals WHERE mode = ? AND status = 'rejected' AND decided_at >= ?"
                " AND decided_at < ?",
            ),
        )
        return [(f"{prefix}{r[0]}", str(r[1])) for prefix, sql in queries for r in self.rows(sql, params)]

    def retrospectives(self, w: Window) -> tuple[float | None, int]:
        items = self.settled(w)
        if not items:
            return None, 0
        covered = sum(
            1
            for subject, when in items
            if self.one(
                "SELECT 1 FROM cases WHERE mode = ? AND subject = ? AND julianday(created_at) <= julianday(?) + ?"
                " LIMIT 1",
                (self.mode, subject, when, CASE_DAYS),
            )
        )
        return covered / len(items), len(items)

    def calibration(self, w: Window) -> tuple[float | None, int]:
        since = _iso(datetime.strptime(w.end, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=LOOKBACK_DAYS))
        rows = self.rows(
            "SELECT probability, status FROM predictions WHERE mode = ? AND status IN ('hit', 'miss')"
            " AND settled_at >= ? AND settled_at < ?",
            (self.mode, since, w.end),
        )
        if not rows:
            return None, 0
        return statistics.fmean((p - (1.0 if s == "hit" else 0.0)) ** 2 for p, s in rows), len(rows)

    # --- venturing ---

    def _moves(self, w: Window) -> list[Any]:
        return self.rows(
            "SELECT * FROM ventures WHERE mode = ? AND stage_at >= ? AND stage_at < ? AND stage IN ('researching',"
            " 'proposed', 'parked', 'killed')",
            (self.mode, w.start, w.end),
        )

    def cost_per_decision(self, w: Window) -> tuple[float | None, int]:
        decisions = len(self._moves(w))
        spent = self.one(
            "SELECT COALESCE(SUM(l.cost_micros), 0) FROM llm_calls l JOIN cycles c ON c.id = l.cycle_id"
            " WHERE c.simulated = ? AND c.venture = 1 AND c.started_at >= ? AND c.started_at < ?",
            (self.sim, w.start, w.end),
        )
        return (spent / 1_000_000 / decisions, decisions) if decisions else (None, 0)

    def researched(self, venture: Any) -> bool:
        """Whether a venture had a research call or a piece of evidence before its stage last changed."""
        return bool(
            self.one(
                "SELECT 1 FROM venture_research WHERE venture_id = ? AND created_at <= ? UNION ALL SELECT 1 FROM"
                " evidence WHERE venture_id = ? AND created_at <= ? LIMIT 1",
                (venture["id"], venture["stage_at"], venture["id"], venture["stage_at"]),
            )
        )

    def ideas_looked_at(self, w: Window) -> tuple[float | None, int]:
        parked = [v for v in self._moves(w) if v["stage"] == "parked" and v["parked_by"] == "agent"]
        if not parked:
            return None, 0
        return sum(1 for v in parked if self.researched(v)) / len(parked), len(parked)

    def evidence_quality(self, w: Window) -> tuple[float | None, int]:
        since = _iso(datetime.strptime(w.end, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=LOOKBACK_DAYS))
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(source = 'independent'), 0) FROM evidence WHERE mode = ? AND"
            " created_at >= ? AND created_at < ?",
            (self.mode, since, w.end),
        ).fetchone()
        return (row[1] / row[0], int(row[0])) if row[0] else (None, 0)

    def case_realism(self, w: Window) -> tuple[float | None, int]:
        since = _iso(datetime.strptime(w.end, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=LOOKBACK_DAYS))
        ratios = [
            max(0.0, min(r[0] / r[1], 2.0))
            for r in self.rows(
                "SELECT k.ev_eur, c.ev_eur FROM venture_critiques k JOIN venture_cases c ON c.id = k.case_id JOIN"
                " ventures v ON v.id = k.venture_id WHERE v.mode = ? AND k.status = 'ok' AND c.ev_eur > 0"
                " AND k.created_at >= ? AND k.created_at < ?",
                (self.mode, since, w.end),
            )
        ]
        return (statistics.median(ratios), len(ratios)) if ratios else (None, 0)

    def first_tests(self, w: Window) -> tuple[float | None, int]:
        since = _iso(datetime.strptime(w.end, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=TEST_DAYS))
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(status = 'done'), 0) FROM milestones WHERE mode = ? AND kind ="
            " 'first_test' AND status IN ('done', 'missed') AND closed_at >= ? AND closed_at < ?",
            (self.mode, since, w.end),
        ).fetchone()
        return (row[1] / row[0], int(row[0])) if row[0] else (None, 0)

    def days_to_verdict(self, w: Window) -> tuple[float | None, int]:
        days = [
            julian
            for v in self._moves(w)
            if v["stage"] == "proposed" or (v["stage"] == "parked" and self.researched(v))
            for julian in [self.one("SELECT julianday(?) - julianday(?)", (v["stage_at"], v["created_at"]))]
            if julian is not None
        ]
        return (statistics.median(days), len(days)) if days else (None, 0)

    # --- collaboration ---

    def promises_kept(self, w: Window) -> tuple[float | None, int]:
        today = self.now.astimezone(self.tz).date().isoformat()
        rows = self.rows(
            "SELECT due, closed_at FROM obligations WHERE mode = ? AND kind = 'promise' AND due >= ? AND due <= ?"
            " AND due < ?",
            (self.mode, w.first.isoformat(), w.last.isoformat(), today),
        )
        if not rows:
            return None, 0
        kept = sum(
            1
            for due, closed in rows
            if closed is not None
            and datetime.strptime(closed, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(self.tz).date()
            <= date.fromisoformat(due)
        )
        return kept / len(rows), len(rows)

    def answers(self, w: Window) -> tuple[float | None, int]:
        fresh = _iso(self.now - timedelta(hours=ANSWER_HOURS))
        rows = self.rows(
            "SELECT o.created_at, a.created_at FROM messages o LEFT JOIN messages a ON a.id = o.answered_by"
            " WHERE o.mode = ? AND o.sender = 'owner' AND o.created_at >= ? AND o.created_at < ?"
            " AND (o.created_at < ? OR o.answered_by IS NOT NULL)",
            (self.mode, w.start, w.end, fresh),
        )
        if not rows:
            return None, 0
        on_time = sum(
            1
            for asked, answered in rows
            if answered is not None
            and (datetime.strptime(answered, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(asked, "%Y-%m-%dT%H:%M:%SZ"))
            <= timedelta(hours=ANSWER_HOURS)
        )
        return on_time / len(rows), len(rows)

    def self_starting(self, w: Window) -> tuple[float | None, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(trigger <> 'owner'), 0) FROM cycles WHERE simulated = ? AND"
            " started_at >= ? AND started_at < ?",
            (self.sim, w.start, w.end),
        ).fetchone()
        return (row[1] / row[0], int(row[0])) if row[0] else (None, 0)

    # --- by version, and the alarms ---

    def versions(self) -> list[dict[str, Any]]:
        """What each version's cycles cost and did."""
        found = []
        for version, first, last in self.rows(
            "SELECT COALESCE(app_version, '?'), MIN(started_at), MAX(started_at) FROM cycles WHERE simulated = ?"
            " GROUP BY COALESCE(app_version, '?') ORDER BY MIN(started_at)",
            (self.sim,),
        ):
            cycles = self.rows(
                "SELECT c.*, COALESCE((SELECT SUM(cost_micros) FROM llm_calls l WHERE l.cycle_id = c.id), 0) AS cost"
                " FROM cycles c WHERE c.simulated = ? AND COALESCE(c.app_version, '?') = ?",
                (self.sim, version),
            )
            cost = sum(int(c["cost"]) for c in cycles)
            moved = sum(int(c["cost"]) for c in cycles if self.moved(c))
            calls = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(t.status = 'ok'), 0) FROM tool_calls t JOIN cycles c ON c.id ="
                " t.cycle_id WHERE c.simulated = ? AND COALESCE(c.app_version, '?') = ? AND t.phase = 'act'",
                (self.sim, version),
            ).fetchone()
            found.append(
                {
                    "version": version,
                    "from": first,
                    "to": last,
                    "cycles": len(cycles),
                    "cost_usd": round(cost / 1_000_000, 2),
                    "per_cycle_usd": round(cost / 1_000_000 / len(cycles), 3) if cycles else None,
                    "productive_spend": round(moved / cost, 3) if cost else None,
                    "tool_calls_done": round(calls[1] / calls[0], 3) if calls[0] else None,
                    "self_started": round(sum(1 for c in cycles if c["trigger"] != "owner") / len(cycles), 3),
                }
            )
        return found

    def alarms(self) -> dict[str, Any]:
        """Today's and the last 7 days' numbers that call for a look the same day."""
        today = self.now.astimezone(self.tz).date()
        found = {}
        for name, w in (("today", window(today, 1, self.now, self.tz)), ("last_7_days", self._last_week(today))):
            cycles = self._cycles(w)
            productive, _ = self.productive_spend(w)
            kept, due = self.promises_kept(w)
            started, _ = self.self_starting(w)
            found[name] = {
                "from": w.first.isoformat(),
                "to": w.last.isoformat(),
                "cycles": len(cycles),
                "spent_usd": round(sum(int(c["cost"]) for c in cycles) / 1_000_000, 2),
                "productive_spend": _round(productive),
                "tool_calls_not_done": self.not_done(w),
                "promises_due": due,
                "promises_kept": _round(kept),
                "cycles_the_owner_started": len(cycles) - round((started or 0) * len(cycles)),
            }
        return found

    def _last_week(self, today: date) -> Window:
        first = today - timedelta(days=6)
        return Window(_iso(_start(first, self.tz)), _iso(self.now), first, today, False)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def parts(values: dict[str, tuple[float | None, int]]) -> tuple[dict[str, Any], dict[str, float | None]]:
    """Each measure's value, sample and score (None while too early), and each part's score."""
    shown: dict[str, Any] = {}
    by_part: dict[str, list[float]] = {p: [] for p in PARTS}
    for key, (value, sample) in values.items():
        kpi = BY_KEY[key]
        early = value is None or sample < kpi.sample
        score = None if early or value is None else kpi.score(value)
        shown[key] = {"value": _round(value), "sample": sample, "score": score, "too_early": early}
        if score is not None:
            by_part[kpi.part].append(score)
    counted = {
        p: sum(1 for k in DEFINITIONS if k.part == p) + sum(1 for g in GRADED.values() if g[0] == p) for p in PARTS
    }
    scores = {p: round(statistics.fmean(s), 1) if s and len(s) * 2 >= counted[p] else None for p, s in by_part.items()}
    return shown, scores


def headline(scores: dict[str, float | None]) -> dict[str, Any]:
    """Outcome and Capability from the parts that have a score, and which parts they lack."""
    found: dict[str, Any] = {}
    for name, weights in WEIGHTS.items():
        have = {p: w for p, w in weights.items() if scores.get(p) is not None}
        total = sum(have.values())
        found[name] = round(sum(scores[p] * w for p, w in have.items()) / total, 1) if total else None  # type: ignore[operator]
        found[f"{name}_without"] = sorted(set(weights) - set(have))
    return found


def card(conn: sqlite3.Connection, mode: str, now: datetime, tz: tzinfo) -> dict[str, Any]:
    """The whole scorecard of one mode's records: every week, by version, the alarms and the definitions."""
    scorer = Scorer(conn, mode, now, tz)
    weeks = []
    for w in scorer.weeks():
        shown, scores = parts(scorer.measure(w))
        weeks.append(
            {
                "from": w.first.isoformat(),
                "to": w.last.isoformat(),
                "complete": w.complete,
                "measures": shown,
                "parts": scores,
                **headline(scores),
            }
        )
    return {
        "scorecard": VERSION,
        "made_at": _iso(now),
        "mode": mode,
        "time_zone": str(tz),
        "weeks": weeks,
        "versions": scorer.versions(),
        "alarms": scorer.alarms(),
        "definitions": [
            {
                "key": k.key,
                "part": k.part,
                "name": k.name,
                "unit": k.unit,
                "floor": k.floor,
                "target": k.target,
                "sample": k.sample,
                "lever": k.lever,
                "how": k.how,
            }
            for k in DEFINITIONS
        ],
        "graded_outside": [{"key": key, "part": part, "name": name} for key, (part, name) in GRADED.items()],
    }


def markdown(data: dict[str, Any], weeks: int = 6) -> str:
    """The scorecard to read: the alarms, the newest ``weeks`` weeks, by version."""
    out = [
        f"# Ember's scorecard, {data['made_at'][:10]}",
        "",
        f"Computed from her records by Ember's code (scorecard version {data['scorecard']}, mode {data['mode']},"
        f" weeks in {data['time_zone']}). Numbers only: the measures an evaluation grades are left to it"
        f" ({', '.join(g['key'] for g in data['graded_outside'])}). A score is 0 at a measure's floor and 100 at its"
        " target; '–' is too early (under its sample) or nothing to measure.",
        "",
        "## Alarms",
        "",
        "| | Cycles | Spent | Productive spend | Tool calls not done | Promises kept | Cycles you started |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, a in data["alarms"].items():
        kept = f"{_pct(a['promises_kept'])} of {a['promises_due']}" if a["promises_due"] else "–"
        out.append(
            f"| {name.replace('_', ' ')} | {a['cycles']} | ${a['spent_usd']:.2f} | {_pct(a['productive_spend'])} |"
            f" {a['tool_calls_not_done']} | {kept} | {a['cycles_the_owner_started']} |"
        )
    shown = data["weeks"][-weeks:]
    if shown:
        out += ["", "## Weeks", "", "| | " + " | ".join(_week(w) for w in shown) + " |"]
        out.append("|---|" + "---|" * len(shown))
        for key in ("outcome", "capability"):
            out.append(f"| **{key.capitalize()}** | " + " | ".join(_num(w[key]) for w in shown) + " |")
        for part in PARTS:
            out.append(f"| {part} | " + " | ".join(_num(w["parts"][part]) for w in shown) + " |")
        out += ["", "| Measure | " + " | ".join(_week(w) for w in shown) + " |", "|---|" + "---|" * len(shown)]
        for kpi in DEFINITIONS:
            cells = []
            for w in shown:
                m = w["measures"][kpi.key]
                cells.append("–" if m["value"] is None else f"{_value(m['value'], kpi.unit)} (n {m['sample']})")
            out.append(f"| {kpi.key} {kpi.name} | " + " | ".join(cells) + " |")
    if data["versions"]:
        out += [
            "",
            "## By version",
            "",
            "| Version | From | Cycles | Cost | Per cycle | Productive spend | Tool calls done | Self-started |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for v in data["versions"]:
            per = "–" if v["per_cycle_usd"] is None else f"${v['per_cycle_usd']:.3f}"
            out.append(
                f"| {v['version']} | {v['from'][:10]} | {v['cycles']} | ${v['cost_usd']:.2f} | {per} |"
                f" {_pct(v['productive_spend'])} | {_pct(v['tool_calls_done'])} | {_pct(v['self_started'])} |"
            )
    return "\n".join(out) + "\n"


def _week(w: dict[str, Any]) -> str:
    return f"{w['from'][5:]} to {w['to'][5:]}" + ("" if w["complete"] else " (so far)")


def _num(value: float | None) -> str:
    return "–" if value is None else f"{value:.0f}"


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def _value(value: float, unit: str) -> str:
    if unit == "share":
        return _pct(value)
    if unit == "USD":
        return f"${value:.2f}"
    return f"{value:.2f}".rstrip("0").rstrip(".") if value != int(value) else f"{value:.0f}"


def files(conn: sqlite3.Connection, mode: str, now: datetime, tz: tzinfo) -> dict[str, bytes]:
    """scorecard.json and scorecard.md, for the evaluation export."""
    data = card(conn, mode, now, tz)
    return {
        "scorecard.json": json.dumps(data, indent=1, ensure_ascii=False).encode(),
        "scorecard.md": markdown(data).encode(),
    }
