"""0.42.2 (analysis-0.40.1, 5.6): the plan tree's model, its storage and its checks, split out of plan.py.

The tree's numbers (the stages that run one after another, the channels and their recurring marketing, when a
channel's results count, the clocks of the brake and of a promise); reading and writing its nodes (``nodes``,
``node``, ``_insert``, ``_update``, ``_close``) and the record of every change (``change``); a product's shape (its
stages, their steps, the current stage); the checks a step or a stage closes on, read from Ember's own records
(``Facts``, ``passes``, and ``check_words``, what a check says); and the small helpers every part of the plan uses.
``Facts.factors`` reads ``_channel_factors`` through plan, where tests patch it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..economy.clock import from_iso, to_iso
from . import metrics, reach, templates, ventures, weights
from .store import AgentScope

SEQUENTIAL = (
    "research",
    "create",
    "release",
)  # their steps come one after another; launch's and maintain's side by side
CHANNELS = ("pinterest", "bluesky", "blog")
# A live product's recurring marketing: the check, the days a period lasts, the step's title
RECURRING = {
    "pinterest": ("pin", 7, "This week's pin for {title}"),
    "bluesky": ("post", 7, "This week's Bluesky post for {title}"),
    "blog": ("blog", 30, "This month's blog post for {title}"),
}
CHANNEL_AUDIENCES = {"pinterest": ("en", "de", "both"), "bluesky": ("en", "both"), "blog": ("de", "both")}
REQUESTED = ("pending", "approved", "approved_with_changes", "done")  # a request made (not refused, not lapsed)
FRESH_PINS, FRESH_POSTS = 4, 4  # a channel's results count once this many items are live
# 0.35.3: and each of them live this long (live, four pins hours old, with no click yet, put Pinterest at its floor)
RESULT_DAYS = 7
CLICKS_PER_PIN = 0.5  # clicks a pin that make Pinterest worth 1.0
# 0.39.6: the views of the listings a Bluesky post or a blog post links, gained in its first RESULT_DAYS, that make its
# channel worth 1.0; Bluesky was worth its reactions (3 a post), the blog 1.0 whatever it brought
VIEWS_PER_POST = 1.0
STALE_DAYS = 14  # a step waiting this long is shown for the daily review's look (Release 2c asks it)
PICKS_SHOWN = 12
HISTORY = 12  # cycles read back for the streak
# 0.35.0: an owner's decision was taken first once in this many hours, the rest of the time weighed (live, an
# obligation nobody closed took every cycle's line until 0.28.0 limited it to once a day); 0.35.1: a promise up to
# PROMISE_TRIES times. 0.37.0: both are weighed always, urgent until taken that often in these hours (0.37.1: then
# their worth alone, below a live product's launch marketing)
OBLIGATION_HOURS = 24
PROMISE_TRIES = 3
# 0.35.1: the channel a promise names (pins, a Bluesky post, a blog post): its step is a marketing cycle's
PROMISE_CHANNELS = (
    ("pinterest", re.compile(r"\b(pins?|pinterest)\b", re.IGNORECASE)),
    ("bluesky", re.compile(r"\bbluesky\b", re.IGNORECASE)),
    ("blog", re.compile(r"\bblog", re.IGNORECASE)),
)
REPORT = re.compile(r"\breport", re.IGNORECASE)  # 0.37.6: a promise to report on a channel names no channel's work
ALTERNATIVES = 3  # the other steps YOUR STEP names
# 0.37.6: the brake. Live on 2026-10-09 a step no cycle could advance took every cycle 30 minutes apart until the daily
# cap stopped it ($5.88 of $7 by 12:35): a step stopped being ready only when Ember said it waits (two a day), the owner
# held its product or its channel was off. Now one a cycle took without what its check reads moving (``mark``) waits
# until that moves or the owner's next day ('tried'), and one whose own requests are on their way waits on them
# ('owner': the owner's decision; 'approved': carrying them out), without one of Ember's waits.
FINISHED = ("completed", "idle")  # the cycles whose picks count: they ran their plan (a failed or refused one didn't)
IN_FLIGHT = ("pending", "approved", "approved_with_changes")  # a request not decided yet, or not carried out yet
CHECK_EXECUTORS = {  # the requests a check counts, by their executor (a request check names its own)
    "pin": ("pinterest_pin",),
    "post": ("bluesky_post",),
    "blog": ("site_post",),
    "live": ("etsy_listing", "printify_product"),
    "critic": ("etsy_edit", "printify_edit"),  # 0.39.0: a Printify product's listing is changed at Printify
}
CHANNEL_EXECUTORS = {"pinterest": "pinterest_pin", "bluesky": "bluesky_post", "blog": "site_post"}
# the tools whose results a product's checks read (its files, pictures, KDP package): with a demand note or a request,
# what the cycle that took a promise, a decision or a step Ember says is done made for its product (_made_in)
MADE_BY = ("make_document", "make_spreadsheet", "make_cost_statement", "make_image", "resize_image", "propose_kdp_book")
# 0.35.0: a product's decide-by dates once it has a live listing (in place of the listing test's bars, gates.py until
# 0.34.0): from the day its first listing was seen live. Day 7: 10 views, or its marketing is urgent for a week.
# Day 14: 30 views and 2 favorites; missed with less reach than reach.ENOUGH, one more try by day 28 (its marketing
# urgent meanwhile), else the owner decides. Day 21: an order brings a step to scale it; none, the owner decides
# (keep or drop it).
DAY7_VIEWS, DAY14_VIEWS, DAY14_FAVORITES = 10, 30, 2
PUSH_DAYS = 7  # a views bar missed: the product's marketing is urgent this long
MOVED_KEY = "plan_tree.milestones_upto"  # migration 0088: the newest milestone the plan tree took the place of
SCALE = "Scale it: 5 variants or a bundle"
STEP_CHARS = 1_500  # YOUR STEP, at most (context.PLANNER_BUDGETS' "ready", which a venture cycle's READY shares)
STAGE_TITLES = {
    "research": "Research",
    "create": "Create",
    "release": "Release",
    "launch": "Launch",
    "maintain": "Maintain",
}
_CHECK_LINE = re.compile(r"^Check: ", re.MULTILINE)
_GERMAN = re.compile(
    r"[äöüß]|\b(und|für|der|die|das|mit|ein|eine|vorlage|bewerbung\w*|haushalt\w*|nebenkosten|vermieter\w*|deutsch)\b",
    re.IGNORECASE,
)
_ENGLISH = re.compile(r"\b(and|for|the|with|your|english)\b", re.IGNORECASE)  # words, not product names


# --- reading the tree ---


def nodes(conn: sqlite3.Connection, scope: AgentScope, condition: str = "1", args: tuple[Any, ...] = ()) -> list[Any]:
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM plan_nodes WHERE {where} AND {condition} ORDER BY parent_id, seq, id", (*params, *args)
    ).fetchall()


def node(conn: sqlite3.Connection, scope: AgentScope, node_id: int) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM plan_nodes WHERE id = ? AND {where}", (node_id, *params)).fetchone()


def _insert(conn: sqlite3.Connection, scope: AgentScope, now: str, **columns: Any) -> int:
    columns = {"mode": scope.mode, "session": scope.session, "created_at": now, "updated_at": now, **columns}
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    return int(conn.execute(f"INSERT INTO plan_nodes ({names}) VALUES ({marks})", tuple(columns.values())).lastrowid)


def _update(conn: sqlite3.Connection, node_id: int, now: str, **columns: Any) -> None:
    sets = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(f"UPDATE plan_nodes SET {sets}, updated_at = ? WHERE id = ?", (*columns.values(), now, node_id))


def _close(conn: sqlite3.Connection, node_id: int, now: str, status: str, result: str, by: str = "code") -> None:
    conn.execute(
        "UPDATE plan_nodes SET status = ?, closed_at = ?, closed_by = ?, result = ?, updated_at = ?"
        " WHERE id = ? AND status = 'open'",
        (status, now, by, result[:300], now, node_id),
    )


# --- the checks ---


@dataclass
class Facts:
    """What the checks read, each once a keep or a pick."""

    conn: sqlite3.Connection
    scope: AgentScope
    now: str
    _funnels: dict[int, reach.Funnel] | None = None
    _verdicts: dict[int, str] = field(default_factory=dict)
    _lowest: dict[int, int | None] = field(default_factory=dict)
    _stuck: dict[int, bool] = field(default_factory=dict)  # 0.39.1
    _requests: list[dict[str, Any]] | None = None
    _factors: dict[str, float] | None = None  # 0.40.3

    def factors(self) -> dict[str, float]:
        """0.40.3: how well each channel works (``_channel_factors``), once a keep: a launch's floor (``_launched``)."""
        from .plan import _channel_factors  # 0.42.2: read through plan, where tests patch it

        if self._factors is None:
            self._factors = _channel_factors(self.conn, self.scope, self.now)
        return self._factors

    def requests(self) -> list[dict[str, Any]]:
        """0.37.6: every request to the owner, oldest first, with the product line it carries on
        (ventures.request_line: the line of the listing it is about, else its own or its cycle's)."""
        if self._requests is None:
            where, params = self.scope.where()
            self._requests = [
                {
                    "id": int(r["id"]),
                    "line": ventures.request_line(self.conn, self.scope, r),
                    "executor": r["executor"],
                    "status": str(r["status"]),
                    "created_at": str(r["created_at"]),
                }
                for r in self.conn.execute(f"SELECT * FROM approvals WHERE {where} ORDER BY id", params).fetchall()
            ]
        return self._requests

    def funnel(self, project_id: int) -> reach.Funnel | None:
        if self._funnels is None:
            self._funnels = reach.funnels(self.conn, self.scope)
        return self._funnels.get(project_id)

    def verdict(self, project_id: int) -> str:
        from . import quality  # 0.35.0: here, not at the top: quality imports prompts, which imports tools, then this

        if project_id not in self._verdicts:
            self._verdicts[project_id] = quality.verdict(self.conn, self.scope, project_id)
        return self._verdicts[project_id]

    def defect(self, project_id: int) -> bool:
        """0.35.3: whether the critic's lowest score of the product is a defect's (weights.DEFECT_SCORE or less)."""
        from . import quality

        if project_id not in self._lowest:
            self._lowest[project_id] = quality.lowest(self.conn, self.scope, project_id)
        score = self._lowest[project_id]
        return score is not None and score <= weights.DEFECT_SCORE

    def stuck(self, project_id: int) -> bool:
        """0.39.1: whether a listing of the line had quality.STUCK_CHECKS checks in a row without a pass."""
        from . import quality

        if project_id not in self._stuck:
            self._stuck[project_id] = quality.stuck(self.conn, self.scope, project_id)
        return self._stuck[project_id]

    def live(self, project_id: int) -> int:
        rows = metrics.listings(self.conn, self.scope, project_id, None)
        return sum(1 for r in rows if r["status"] == "active" and (r["state"] or "active") == "active")


def passes(facts: Facts, project_id: int | None, kind: str | None, spec: Mapping[str, Any]) -> bool:
    """Whether a check holds for a product (templates.CHECKS); 'agent' and an unknown check never do."""
    conn, scope = facts.conn, facts.scope
    if kind == "obligation":
        row = conn.execute("SELECT status FROM obligations WHERE id = ?", (spec.get("id"),)).fetchone()
        return row is not None and row["status"] == "closed"
    if project_id is None or kind in (None, "agent"):
        return False
    if kind == "any":
        return any(passes(facts, project_id, s.get("check"), s) for s in spec.get("of", ()))
    if kind == "demand":
        since = to_iso(from_iso(facts.now) - timedelta(days=int(spec.get("days", 14))))
        where, params = scope.where()
        return (
            conn.execute(
                f"SELECT 1 FROM demand_notes WHERE {where} AND project_id = ? AND created_at >= ? LIMIT 1",
                (*params, project_id, since),
            ).fetchone()
            is not None
        )
    if kind == "built":
        tools = [str(t) for t in spec.get("tools", ())]
        if not tools:
            return False
        marks = ", ".join("?" for _ in tools)
        made = conn.execute(
            "SELECT t.result FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id WHERE c.project_id = ?"
            f" AND c.session = ? AND c.simulated = ? AND t.tool IN ({marks}) AND t.status = 'ok'",
            (project_id, scope.session, 1 if scope.simulated else 0, *tools),
        ).fetchall()
        clean = sum(1 for r in made if not _CHECK_LINE.search(r["result"] or ""))
        return clean >= int(spec.get("count", 1))
    if kind == "request":  # 0.37.6: ``since``, one made since then (a step proposing it again, _again)
        where, params = scope.where("a")
        statuses = [
            r[0]
            for r in conn.execute(
                "SELECT a.status FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
                f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor = ? AND a.created_at >= ?",
                (*params, project_id, spec.get("executor"), str(spec.get("since") or "")),
            )
        ]
        wanted = ("done",) if spec.get("status") == "done" else REQUESTED
        return any(s in wanted for s in statuses)
    if kind == "live":
        return facts.live(project_id) >= int(spec.get("count", 1))
    if kind in ("pin", "post", "blog"):
        funnel = facts.funnel(project_id)
        have = 0 if funnel is None else {"pin": funnel.pins, "post": funnel.bluesky, "blog": funnel.posts}[kind]
        return have >= int(spec.get("count", 1))
    if kind == "critic":
        return facts.verdict(project_id) == "pass"
    if kind == "kdp_check":
        return (
            conn.execute(
                "SELECT 1 FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id WHERE c.project_id = ?"
                " AND c.session = ? AND c.simulated = ? AND t.tool = 'propose_kdp_book' AND t.status = 'ok'"
                " AND json_extract(t.input, '$.check') = 1 LIMIT 1",
                (project_id, scope.session, 1 if scope.simulated else 0),
            ).fetchone()
            is not None
        )
    return False


def _spec(row: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(row["check_spec"]) if row["check_spec"] else {}


def _holds(facts: Facts, row: Mapping[str, Any]) -> bool:
    return passes(facts, row["project_id"], row["check_kind"], _spec(row))


def _open_steps(conn: sqlite3.Connection, scope: AgentScope, parent_id: int) -> list[Any]:
    return nodes(conn, scope, "level = 'step' AND status = 'open' AND parent_id = ?", (parent_id,))


def _descendants(conn: sqlite3.Connection, scope: AgentScope, node_id: int) -> list[Any]:
    found: list[Any] = []
    frontier = [node_id]
    while frontier:
        marks = ", ".join("?" for _ in frontier)
        children = nodes(conn, scope, f"parent_id IN ({marks}) AND status = 'open'", tuple(frontier))
        found += children
        frontier = [c["id"] for c in children]
    return found


def _stages(conn: sqlite3.Connection, scope: AgentScope, product_id: int) -> list[Any]:
    return nodes(conn, scope, "parent_id = ? AND level = 'stage'", (product_id,))


def _replaced(stage: Mapping[str, Any], product: Mapping[str, Any]) -> bool:
    """0.35.0: a stage a re-type replaced (see _retemplate), dropped while its product went on."""
    if stage["status"] != "dropped":
        return False
    return product["closed_at"] is None or str(stage["closed_at"]) < str(product["closed_at"])


def _steps(conn: sqlite3.Connection, scope: AgentScope, stage_id: int) -> list[Any]:
    return nodes(conn, scope, "parent_id = ? AND level = 'step'", (stage_id,))


def current_stage(conn: sqlite3.Connection, scope: AgentScope, product_id: int) -> Any | None:
    return next((s for s in _stages(conn, scope, product_id) if s["status"] == "open"), None)


def current_stage_name(conn: sqlite3.Connection, scope: AgentScope, line: int) -> str:
    """0.35.0: a product line's stage in the plan, with its steps done ("create, 1 of 3 steps done"), "" for none."""
    found = nodes(conn, scope, "level = 'product' AND project_id = ?", (line,))
    if not found:
        return ""
    if found[0]["status"] != "open":
        return str(found[0]["status"])
    stage = current_stage(conn, scope, found[0]["id"])
    if stage is None:
        return ""
    steps = [s for s in _steps(conn, scope, stage["id"]) if s["status"] != "dropped" and s["kind"] != "promise"]
    done = sum(1 for s in steps if s["status"] == "done")
    held = ""
    if found[0]["hold_reason"]:  # 0.36.0: whose hold it is (the owner's, Ember can't lift)
        held = " (your owner holds it)" if found[0]["hold_by"] == "owner" else " (on hold)"
    return f"{stage['stage']}, {done} of {len(steps)} steps done{held}" if steps else f"{stage['stage']}{held}"


def change(
    conn: sqlite3.Connection,
    scope: AgentScope,
    node_id: int,
    cycle_id: int | None,
    actor: str,
    action: str,
    why: str,
    now: str,
) -> None:
    """0.35.0: a change to the tree that isn't a check passing, kept with its reason (the owner's "changed today")."""
    conn.execute(
        "INSERT INTO plan_changes (mode, session, node_id, cycle_id, actor, action, why, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, node_id, cycle_id, actor, action, " ".join(why.split())[:300] or action, now),
    )


def _days(since: str | None, now: str) -> float:
    if not since:
        return 0.0
    return max(0.0, (from_iso(now) - from_iso(since)).total_seconds() / 86_400)


def _midnight(today: date) -> str:
    """When ``today`` began in UTC: the owner's day where the caller gave no ``day_start`` of their clock's (a clock in
    UTC; Ember's cycles, the Plan tab and the report give theirs)."""
    return f"{today.isoformat()}T00:00:00Z"


def check_words(kind: str | None, spec: Mapping[str, Any]) -> str:
    """What a check reads, in words (templates.CHECKS with its parameters filled in)."""
    text = templates.CHECKS.get(kind or "", "")
    if kind == "any":
        return " or ".join(check_words(s.get("check"), s) for s in spec.get("of", ())) or text
    for name, value in spec.items():
        shown = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        text = text.replace(f"`{name}`", shown)
    said = re.sub(r"`(\w+)`", r"\1", text)
    if kind == "request" and spec.get("since"):  # 0.37.6: a step proposing it again (_again)
        said += f", a new one made since {str(spec['since'])[:10]}"
    return said


def _quoted(text: Any) -> str:
    return json.dumps(_cut(str(text), 60), ensure_ascii=False)


def _cut(text: str, chars: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= chars else flat[: chars - 1].rstrip() + "…"


def _make_room(conn: sqlite3.Connection, parent_id: int, seq: int, count: int, now: str) -> None:
    """Moves the steps from ``seq`` on along by ``count``, so new ones fit before them."""
    conn.execute(
        "UPDATE plan_nodes SET seq = seq + ?, updated_at = ? WHERE parent_id = ? AND seq >= ?",
        (count, now, parent_id, seq),
    )


def _ids(found: list[int]) -> str:
    return ", ".join(f"#{n}" for n in found)
