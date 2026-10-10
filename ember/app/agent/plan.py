"""The plan tree (0.34.0): the tree under the owner's goal, laid out and kept by Ember's code.

Live, on 2026-10-07 twelve cycles went to eight things, and a book promised three times got none: READY's ranking,
the obligations, the spending shares and the daily review each decided part of what Ember worked on. Release 2 puts
one tree under the goal. Its projects are platforms (Etsy, KDP, Printify, the website, the channels); each open project
row (a product line) is a product under its platform, laid out from its type's template (templates.py) the first
time this keeper sees it: stages (research, create, release, launch, maintain) and the small steps under them, each
with a check read from Ember's own records, so neither a step nor a stage closes on words. Each cycle, before the
plan, ``keep``:

* adopts new product lines and lays out their stages and steps;
* closes steps and stages whose checks pass (a later stage done closes the ones before it: they are moot);
* puts each promise to the owner that names a product under that product, as a step closed when it is kept;
* adds a live product's recurring steps: a week's pin, a week's Bluesky post (English), a month's blog post (German)
  and the critic's fixes; a missed one stays open and ages;
* closes the products of closed lines.

In 0.34.0 the tree ran in the shadow: each cycle recorded the step it would have taken (weights.py) next to what
READY took. 0.35.0 (Release 2b): the tree steers. ``steer`` takes each cycle's step before the plan, and the step
decides what the cycle is (a marketing step a marketing cycle, any other an ordinary one, unless it is the ventures'
turn); the plan sees it as YOUR STEP (``step_text``). The owner's decisions on a product's requests are steps too
(``_decisions``), taken first like a promise due. 0.35.1: a promise is a step of its own, taken before the heaviest
step and the ventures' turn until it is kept; one made without naming its product gets the one its words name.
0.37.0: a promise and a decision are weighed like any step (``candidates``); 0.37.1: urgent only near their day.
0.37.6: one brake for every step: a step a cycle took without what its check reads moving waits until that moves or
the next day, one whose own requests are on their way waits on them (``candidates``, ``mark``), and a stage that still
needs a request of the owner's gets a step to propose it again (``_again``).
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
from . import critic, desk, metrics, obligations, policy, reach, roadmap, templates, ventures, weights
from .store import CLOSED_STATUSES, OPEN_STATUSES, AgentScope, canonical, sha256

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
CLICKS_PER_PIN, REACTIONS_PER_POST = 0.5, 3.0  # results that make a channel worth 1.0
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
    "critic": ("etsy_edit",),
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


# --- laying a product out ---


def infer_template(
    conn: sqlite3.Connection, scope: AgentScope, project: Mapping[str, Any], records_only: bool = False
) -> templates.Template:
    """A product line's type, from its records first (its requests to the owner, its tools), then its words.
    ``records_only`` (0.35.0, a generic product re-typed): its requests and the tools of one type only."""
    pid = project["id"]
    where, params = scope.where("a")
    executors = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT a.executor FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
            f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor IS NOT NULL",
            (*params, pid),
        )
    }
    for executor, template in (
        ("kdp_package", templates.KDP_BOOK),
        ("printify_product", templates.PRINTIFY_POD),
        ("etsy_listing", templates.ETSY_DIGITAL),
        ("site_post", templates.SITE_CONTENT),
        ("site_links", templates.SITE_CONTENT),
    ):
        if executor in executors:
            return template
    text = f"{project['title']} {project['hypothesis']}".lower()
    if re.search(r"\b(kdp|amazon|paperback|ebook|e-book)\b", text):
        return templates.KDP_BOOK
    used = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT t.tool FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id"
            " WHERE c.project_id = ? AND c.session = ? AND c.simulated = ? AND t.status = 'ok'",
            (pid, scope.session, 1 if scope.simulated else 0),
        )
    }
    if "propose_kdp_book" in used:
        return templates.KDP_BOOK
    if used & {"propose_printify_product", "printify_catalog"}:
        return templates.PRINTIFY_POD
    if records_only:
        return templates.ETSY_DIGITAL if "propose_etsy_listing" in used else templates.GENERIC
    for pattern, template in (
        (r"\b(poster|posters|printify|print on demand)\b", templates.PRINTIFY_POD),
        (r"\b(blog|website|site)\b", templates.SITE_CONTENT),
        (r"\b(pinterest|bluesky)\b", templates.CHANNEL),
    ):
        if re.search(pattern, text):
            return template
    if used & {"make_spreadsheet", "make_document", "make_cost_statement", "propose_etsy_listing"} or re.search(
        r"\b(etsy|downloads?|printables?|vorlagen?|templates?|planners?|trackers?|bundles?)\b", text
    ):
        return templates.ETSY_DIGITAL
    return templates.GENERIC


def infer_audience(project: Mapping[str, Any]) -> str:
    """Who a product's marketing speaks to: English, German or both, from its words (Release 2c: the owner sets it).
    No record holds a listing's language yet."""
    text = f"{project['title']} {project['hypothesis']}"
    low = text.lower()
    if re.search(r"\b(en\s*\+\s*de|de\s*\+\s*en|bilingual|zweisprachig)\b", low):
        return "both"
    german, english = len(_GERMAN.findall(text)), len(_ENGLISH.findall(text))
    if re.search(r"\b(german|deutsch)\b", low) and not re.search(r"\benglish\b", low):
        return "de"
    if german >= 2 and english == 0:
        return "de"
    if english >= 2 and german == 0:
        return "en"
    return "both"


def could_earn(conn: sqlite3.Connection, scope: AgentScope, project: Mapping[str, Any], fallback: float) -> float:
    """What a product could earn a month ($): the agent's own revenue sub-goal for its line, else its venture's split
    over the venture's open lines, else its template's default."""
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT project_id, venture_id, target FROM milestones WHERE {where} AND status = 'open'"
        " AND metric = 'revenue_month_usd' AND owner_goal = 0 AND kind IS NULL AND target > 0",
        params,
    ).fetchall()
    own = [r["target"] for r in rows if r["project_id"] == project["id"]]
    if own:
        return round(max(own) / metrics.MICROS, 2)
    venture = project["venture_id"]
    shared = [
        r["target"] for r in rows if r["project_id"] is None and venture is not None and r["venture_id"] == venture
    ]
    if shared:
        lines = conn.execute(
            f"SELECT COUNT(*) FROM projects WHERE {where} AND venture_id = ? AND status IN {OPEN_STATUSES}",
            (*params, venture),
        ).fetchone()[0]
        return round(max(shared) / metrics.MICROS / max(lines, 1), 2)
    return fallback


def _platform(conn: sqlite3.Connection, scope: AgentScope, platform: str, now: str) -> int:
    where, params = scope.where()
    found = conn.execute(
        f"SELECT id FROM plan_nodes WHERE {where} AND level = 'project' AND platform = ?", (*params, platform)
    ).fetchone()
    if found is not None:
        return int(found["id"])
    title = templates.PLATFORMS[platform]
    return _insert(conn, scope, now, level="project", platform=platform, title=title, source="code")


def lay_out(conn: sqlite3.Connection, scope: AgentScope, project: Mapping[str, Any], now: str) -> int:
    """A product line as a product of the tree, under its platform, with its template's stages and steps."""
    template = infer_template(conn, scope, project)
    audience = infer_audience(project)
    parent = _platform(conn, scope, template.platform, now)
    product = _insert(
        conn,
        scope,
        now,
        parent_id=parent,
        level="product",
        project_id=project["id"],
        template=template.key,
        title=str(project["title"])[:160],
        source="code",
        could_earn=could_earn(conn, scope, project, template.could_earn),
        audience=audience,
    )
    for seq, stage in enumerate(template.stages):
        stage_id = _insert(
            conn,
            scope,
            now,
            parent_id=product,
            level="stage",
            project_id=project["id"],
            stage=stage.stage,
            title=f"{STAGE_TITLES[stage.stage]}: {stage.done}"[:160],
            check_kind=stage.check,
            check_spec=json.dumps(stage.spec) if stage.check else None,
            seq=seq,
            source="template",
        )
        for number, step in enumerate(templates.steps_for(stage, audience)):
            _template_step(conn, scope, now, stage_id, int(project["id"]), template, stage, step, number)
    return product


def _template_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    stage_id: int,
    line: int,
    template: templates.Template,
    stage: templates.StageT,
    step: templates.StepT,
    seq: int,
) -> int:
    return _insert(
        conn,
        scope,
        now,
        parent_id=stage_id,
        level="step",
        project_id=line,
        template=f"{template.key}/{stage.stage}/{step.key}",
        stage=stage.stage,
        kind=step.kind,
        channel=step.channel,
        title=step.title,
        check_kind=step.check,
        check_spec=json.dumps(step.spec),
        seq=seq,
        source="template",
        effort=step.effort,
        waiting=step.waiting,
    )


def _retemplate(
    conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], project: Mapping[str, Any], now: str
) -> list[str]:
    """0.35.0: a product laid out as generic (its words named no type, and its line had no records yet) takes the
    template its line's records name once they do (its first request to the owner, the tools its cycles used). Its
    open generic stages close, and that type's stages and steps take their place under the same product (a stage
    done before stays done): what Ember, the owner or Ember's code added there moves along (to the type's next stage
    when it has none of that name: a site post has no create stage), a promise's or a decision's step stays where it
    is (it is taken in any stage). A node's place is fixed, so the product stays under its first project: the owner's
    Plan tab shows it under its type's."""
    if templates.by_key(product["template"]).name != templates.GENERIC.name:
        return []
    template = infer_template(conn, scope, project, records_only=True)
    if template.name == templates.GENERIC.name:
        return []
    line = int(project["id"])
    audience = str(product["audience"] or "both")
    old = {str(s["stage"]): s for s in _stages(conn, scope, product["id"]) if s["status"] != "dropped"}
    moved = f"Ember's code: its type is {template.title} now"
    made: dict[str, int] = {}
    for seq, stage in enumerate(template.stages):
        row = old.get(stage.stage)
        if row is not None and row["status"] != "open":
            continue  # done before: it stays done
        made[stage.stage] = _insert(
            conn, scope, now, parent_id=product["id"], level="stage", project_id=line, stage=stage.stage,
            title=f"{STAGE_TITLES[stage.stage]}: {stage.done}"[:160], check_kind=stage.check,
            check_spec=json.dumps(stage.spec) if stage.check else None, seq=seq, source="template",
        )  # fmt: skip
        for number, step in enumerate(templates.steps_for(stage, audience)):
            _template_step(conn, scope, now, made[stage.stage], line, template, stage, step, number)
    order = list(STAGE_TITLES)
    for name, row in old.items():
        if row["status"] != "open":
            continue
        # its stage in the new type, else the type's next one; none (a later one is done): its steps end here
        target = next((n for n in order[order.index(name) :] if n in made), None)
        for kept in _steps(conn, scope, row["id"]):
            if kept["status"] != "open" or kept["obligation_id"] is not None:
                continue
            if kept["source"] == "template" or target is None:
                _close(conn, kept["id"], now, "dropped", moved)
                continue
            copy = _insert(
                conn, scope, now, parent_id=made[target], level="step", project_id=line, stage=target,
                template=kept["template"], kind=kept["kind"], channel=kept["channel"], title=kept["title"],
                check_kind=kept["check_kind"], check_spec=kept["check_spec"], seq=kept["seq"], source=kept["source"],
                due=kept["due"], effort=kept["effort"], ready_since=kept["ready_since"], pinned=kept["pinned"],
                waiting=kept["waiting"], wait_ref=kept["wait_ref"], wait_until=kept["wait_until"],
                wait_why=kept["wait_why"],
            )  # fmt: skip
            _close(conn, kept["id"], now, "dropped", f"{moved}: as step #{copy}")
        _close(conn, row["id"], now, "dropped", moved)
    _update(conn, product["id"], now, template=template.key)
    _platform(conn, scope, template.platform, now)  # its type's project, where the owner's tab shows it
    why = f"its records show its type, {template.title}: that type's stages and steps"
    change(conn, scope, int(product["id"]), None, "code", "replace", why, now)
    return [
        f"Plan tree: line #{line}'s records show its type, {template.title}: it takes that type's stages and steps."
    ]


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
    _requests: list[dict[str, Any]] | None = None

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


# --- keeping the tree ---


def keep(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date, channels: Mapping[str, bool]) -> list[str]:
    """Lays out new product lines, closes what its checks show done, adds promise and recurring steps, closes the
    products of closed lines; returns what happened, for the System log."""
    said: list[str] = []
    facts = Facts(conn, scope, now)
    where, params = scope.where()
    products = {r["project_id"]: r for r in nodes(conn, scope, "level = 'product'")}
    laid: set[int] = set()
    for project in conn.execute(f"SELECT * FROM projects WHERE {where} ORDER BY id", params).fetchall():
        product = products.get(project["id"])
        if product is None and project["status"] in OPEN_STATUSES:
            product_id = lay_out(conn, scope, project, now)
            laid.add(product_id)
            said.append(f"Plan tree: line #{project['id']} laid out as a product (#{product_id}).")
        elif product is not None and product["status"] == "open" and project["status"] in CLOSED_STATUSES:
            _close_product(conn, scope, product, project, now)
            said.append(f"Plan tree: product #{product['id']} closed with line #{project['id']}.")
    said += move(conn, scope, now)  # once: the milestones the tree takes the place of (migration 0088)
    said += _link_promises(conn, scope)
    decided = _decided(conn, scope)
    for product in nodes(conn, scope, "level = 'product' AND status = 'open'"):
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (product["project_id"],)).fetchone()
        if project is not None:
            retyped = _retemplate(conn, scope, product, project, now)
            if retyped:
                said += retyped
                product = node(conn, scope, int(product["id"])) or product
            template = templates.by_key(product["template"])
            earn = could_earn(conn, scope, project, template.could_earn)
            if earn != product["could_earn"]:
                _update(conn, product["id"], now, could_earn=earn)
        closed = _close_done(facts, product, now)
        said += closed if product["id"] not in laid else []  # a new product's stages already done: no news
        said += _promises(facts, product, now)
        said += _decisions(facts, product, decided.get(int(product["project_id"]), []), now)
        said += _again(facts, product, now)  # 0.37.6: a request its stage still needs, proposed again
        said += _recurring(facts, product, now, today, channels)
        said += _decide_by(facts, product, now, today)
    said += _ventures(conn, scope, now)  # 0.37.0: each venture a node, its next decision a step
    said += _owner_owed(conn, scope, now)  # 0.37.0: and what is owed to the owner of no product
    said += _lift_waits(conn, scope, now, today)
    _clocks(conn, scope, now, today, channels)
    return said


# --- the ventures: the plan's sub-goal, each venture a node with its next decision as a step (0.37.0) ---

# 0.37.0: each venture being explored is a node of the Ventures project, its next decision (desk.py's, READY's until
# then) a step under it, weighed like any step: a cycle that takes one is a venture cycle on that venture. 0.36.0 had
# one Explore step for all of them, and READY said which (migration 0091 closed it). Each decision's step: its
# template, its kind (the owner's decision on a case waits on them) and its title.
VENTURE_STEPS: dict[str, tuple[str, str, str]] = {
    "triage": ("triage@1", "create", "Triage it: research it, or park it with why"),
    "appraise": ("appraise@1", "create", "Research its next question; then its business case, or park it"),
    "answer": ("answer@1", "create", "Answer the critic with evidence or new numbers, or park it"),
    "decide": ("decide@1", templates.OWNER_KIND, "Your owner decides on its business case: back it or park it"),
}
BRAINSTORM_TEMPLATE = "brainstorm@1"
BRAINSTORM_TITLE = "Brainstorm six new ideas for your ventures"
# the steps that make a venture cycle (the owner's decision is theirs)
VENTURE_TEMPLATES = frozenset({"triage@1", "appraise@1", "answer@1", BRAINSTORM_TEMPLATE})
DECISION_OF = {template: decision for decision, (template, _, _) in VENTURE_STEPS.items()}


def ventures_node(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    found = nodes(conn, scope, "level = 'project' AND platform = 'ventures'")
    return found[0] if found else None


def new_things_held(conn: sqlite3.Connection, scope: AgentScope) -> str | None:
    """0.36.0: the owner's hold on the Ventures ("nothing new"): why, while it lasts. Ember's code keeps it: no venture
    cycle, and no new product (project_create refuses one; YOUR STEP offers none)."""
    top = ventures_node(conn, scope)
    if top is None or not top["hold_reason"] or top["hold_by"] != "owner":
        return None
    return str(top["hold_reason"])


def venture_decision(conn: sqlite3.Connection, v: Mapping[str, Any]) -> str | None:
    """A venture's next decision by its stage: triage an idea, appraise one being researched, answer the critic on a
    proposed one it judged test or park, else the owner's decision on its case; None once it is backed, live, parked
    or killed (a backed one's work is its product line's)."""
    stage = v["stage"]
    if stage == "idea":
        return "triage"
    if stage == "researching":
        return "appraise"
    if stage != "proposed":
        return None
    case_row = ventures.latest_case(conn, int(v["id"])) if v["cases"] else None
    judged = critic.latest(conn, int(v["id"])) if case_row is not None else None
    return "answer" if judged is not None and judged["verdict"] in ("test", "park") else "decide"


def _open_steps(conn: sqlite3.Connection, scope: AgentScope, parent_id: int) -> list[Any]:
    return nodes(conn, scope, "level = 'step' AND status = 'open' AND parent_id = ?", (parent_id,))


def _ventures(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """The Ventures project with a node for each venture being explored and its next decision as a step (the step
    before it closed once its stage moved on), the node closed once the venture is backed or live (done: its work is
    its product line's) or parked or killed (dropped); and a brainstorm step while the funnel is thin
    (desk.brainstorm_due). Kept before every plan."""
    top_id = _platform(conn, scope, "ventures", now)
    first = not nodes(conn, scope, "level = 'venture'")
    open_nodes = {int(n["venture_id"]): n for n in nodes(conn, scope, "level = 'venture' AND status = 'open'")}
    rows = ventures.all_ventures(conn, scope)
    made = 0
    for v in rows:
        vid = int(v["id"])
        decision = venture_decision(conn, v)
        here = open_nodes.get(vid)
        if decision is None:
            if here is not None:
                ended = "done" if v["stage"] in ("building", "live") else "dropped"
                why = _cut(f"venture #{vid} is {v['stage']}", 300)
                for step in _open_steps(conn, scope, int(here["id"])):
                    _close(conn, step["id"], now, ended, why)
                _close(conn, here["id"], now, ended, why)
            continue
        if here is None:
            here_id = _insert(
                conn,
                scope,
                now,
                parent_id=top_id,
                level="venture",
                venture_id=vid,
                title=_cut(" ".join(str(v["title"]).split()), 160),
                source="code",
            )
            made += 1
        else:
            here_id = int(here["id"])
        template, kind, title = VENTURE_STEPS[decision]
        steps = _open_steps(conn, scope, here_id)
        if any(s["template"] == template for s in steps):
            continue
        for step in steps:
            _close(conn, step["id"], now, "done", _cut(f"venture #{vid} is {v['stage']} now", 300))
        _insert(
            conn, scope, now, parent_id=here_id, level="step", kind=kind, template=template, title=title, source="code"
        )
    due = desk.brainstorm_due(rows)
    brainstorms = nodes(conn, scope, "level = 'step' AND status = 'open' AND template = ?", (BRAINSTORM_TEMPLATE,))
    if due and not brainstorms:
        _insert(
            conn,
            scope,
            now,
            parent_id=top_id,
            level="step",
            kind="create",
            template=BRAINSTORM_TEMPLATE,
            title=BRAINSTORM_TITLE,
            source="code",
        )
    elif not due:
        for step in brainstorms:
            _close(conn, step["id"], now, "done", "enough ideas wait, or have their numbers")
    if first and made:
        return ["Plan tree: each venture you explore is a node of its Ventures now, its next decision a step."]
    return []


def _last_taken(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, str]:
    """When a cycle last took each step (0.37.0: a venture's step ages from then, so the others come in turn)."""
    where, params = scope.where()
    return {
        int(r[0]): str(r[1])
        for r in conn.execute(
            f"SELECT node_id, MAX(created_at) FROM plan_picks WHERE {where} AND node_id IS NOT NULL GROUP BY node_id",
            params,
        )
    }


def _ventured_last(conn: sqlite3.Connection, scope: AgentScope, before: int | None) -> bool:
    """Whether the cycle before ``before`` (None: the latest) was a venture cycle."""
    row = conn.execute(
        "SELECT venture FROM cycles WHERE session = ? AND simulated = ? AND (? IS NULL OR id < ?)"
        " ORDER BY id DESC LIMIT 1",
        (scope.session, 1 if scope.simulated else 0, before, before),
    ).fetchone()
    return bool(row and row["venture"])


def venture_worth(conn: sqlite3.Connection, v: Mapping[str, Any] | None) -> float:
    """What a venture's step is worth unless the owner set one: what its business case expects a month (the critic's
    where it is lower), as a product's worth from what it could earn (weights.prior), at least weights.EXPLORE_WORTH;
    an idea's by its scores (ventures.weight, an unscored one's as middling), half to one and a half of that, so the
    heaviest ideas are researched first (READY's order until 0.36.0)."""
    if v is None:
        return weights.EXPLORE_WORTH
    case_row = ventures.latest_case(conn, int(v["id"])) if v["cases"] else None
    ev = critic.ranking_ev(case_row, critic.latest(conn, int(v["id"])) if case_row is not None else None)
    if ev is not None and ev > 0:
        return max(weights.EXPLORE_WORTH, weights.prior(ev))
    if v["stage"] == "idea":
        scored = ventures.weight(v)
        return round(weights.EXPLORE_WORTH * (0.5 + (50 if scored is None else scored) / 100), 3)
    return weights.EXPLORE_WORTH


def _venture_candidates(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    exploring: bool,
    turn: bool = False,
    picks: Mapping[int, Any] | None = None,
) -> list[Candidate]:
    """The ventures' steps as the scorer sees them: each worth the owner's (the venture's, else all the ventures'),
    else what its case expects (venture_worth); urgent when the owner wished for it (weights.ASKED) or Ember's code
    parks it soon (weights.PARK_SOON); aging since it was ready or a cycle last took it. They wait on the owner (their
    decision on a case), while the owner holds new things ('hold'), while the cycle runs no venture work ('mode': a
    burn mode without venture cycles, or what the owner waits for first: ``exploring`` false), when a new product has
    its turn ('turn': no product step is ready and the cycle before explored; not the step the owner pinned), an
    idea while ventures.MAX_ACTIVE are researched or proposed ('room'), and once its venture was backed, parked or
    killed until the next cycle's keeper closes it ('moved'). 0.37.6: and once a cycle took it without its venture's
    records moving (``mark``: its stage, evidence, cases, the critic's answers; a brainstorm's new ideas), until
    they move or the next day ('tried')."""
    top = ventures_node(conn, scope)
    if top is None:
        return []
    facts = Facts(conn, scope, now)
    picks = _last_picks(conn, scope) if picks is None else picks
    rows = {int(v["id"]): v for v in ventures.all_ventures(conn, scope)}
    room = sum(1 for v in rows.values() if v["stage"] in ventures.EXPLORED) < ventures.MAX_ACTIVE
    taken = _last_taken(conn, scope)
    owned = (*VENTURE_TEMPLATES, VENTURE_STEPS["decide"][0])
    marks = ", ".join("?" for _ in owned)
    found: list[Candidate] = []
    for step in nodes(conn, scope, f"level = 'step' AND status = 'open' AND template IN ({marks})", owned):
        parent = node(conn, scope, int(step["parent_id"]))
        v = rows.get(int(parent["venture_id"])) if parent is not None and parent["venture_id"] is not None else None
        item = desk.item(conn, v, today=today, room=room) if v is not None else None
        mine = parent is not None and parent["venture_id"] is not None  # a venture's step (not the brainstorm)
        reason: str | None
        if mine and (v is None or v["stage"] not in ventures.EXPLORING):
            reason = "moved"  # backed, parked or killed since the tree was kept: the next cycle's keeper closes it
        elif step["kind"] == templates.OWNER_KIND:
            reason = "owner"
        elif top["hold_reason"]:
            reason = "hold"
        elif not exploring:
            reason = "mode"
        elif turn and not step["pinned"]:  # the owner's Explore next is their word: it doesn't wait for the turn
            reason = "turn"
        elif step["waiting"]:
            reason = str(step["waiting"])
        elif mine and item is None:
            reason = "room"
        else:
            reason = None
        stale = _stale(facts, step, picks)
        if reason is None and stale == today.isoformat():
            reason = "tried"
        if parent is not None and parent["owner_worth"] is not None:
            worth = float(parent["owner_worth"])
        elif top["owner_worth"] is not None:
            worth = float(top["owner_worth"])
        else:
            worth = venture_worth(conn, v)
        tier = item.tier if item is not None else ""
        urgency = weights.ASKED if tier == "wish" else weights.PARK_SOON if tier == "urgent" else 0.0
        since = max(filter(None, (step["ready_since"], taken.get(int(step["id"])))), default=None)
        found.append(
            Candidate(
                weights.Step(
                    step["id"],
                    None,
                    str(step["title"]),
                    _kind(step),
                    worth,
                    urgency=urgency,
                    age_days=round(_days(since, now), 3) if reason is None else 0.0,
                    pinned=bool(step["pinned"]),
                    blocked=reason is not None,
                ),
                "venture",
                reason,
                stale is not None,
            )
        )
    return found


def venture_of(conn: sqlite3.Connection, scope: AgentScope, step_id: int | None) -> int | None:
    """The venture a step of the Ventures is about (None: a brainstorm, or no venture's step)."""
    row = node(conn, scope, step_id) if step_id is not None else None
    parent = node(conn, scope, int(row["parent_id"])) if row is not None and row["parent_id"] is not None else None
    return int(parent["venture_id"]) if parent is not None and parent["venture_id"] is not None else None


def ventures_view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    found: list[Candidate],
    parts: Mapping[int, weights.Parts],
    today: date,
) -> dict[str, Any] | None:
    """The Ventures as the owner's Plan tab shows them: their worth (the owner's, else each venture's own) and their
    hold, each venture being explored with its step (its weight now, or what it waits on) and what it needs decided,
    and the brainstorm when one is due. None before they are laid out."""
    top = ventures_node(conn, scope)
    if top is None:
        return None
    by_id = {c.step.id: c for c in found}
    rows = {int(v["id"]): v for v in ventures.all_ventures(conn, scope)}
    room = sum(1 for v in rows.values() if v["stage"] in ventures.EXPLORED) < ventures.MAX_ACTIVE

    def shown(step: Mapping[str, Any]) -> dict[str, Any]:
        candidate, weight = by_id.get(int(step["id"])), parts.get(int(step["id"]))
        return {
            "id": step["id"],
            "title": step["title"],
            "decision": DECISION_OF.get(str(step["template"]), "brainstorm"),
            "pinned": bool(step["pinned"]),
            "waiting": candidate.waiting if candidate is not None else None,
            "weight": round(weight.total, 2) if weight else None,
            "why": weight.text() if weight else None,
        }

    items = []
    for here in nodes(conn, scope, "level = 'venture' AND status = 'open'"):
        v = rows.get(int(here["venture_id"]))
        steps = _open_steps(conn, scope, int(here["id"]))
        item = desk.item(conn, v, today=today, room=room) if v is not None else None
        items.append(
            {
                "node": here["id"],
                "venture": here["venture_id"],
                "title": v["title"] if v is not None else here["title"],
                "stage": v["stage"] if v is not None else None,
                "owner_worth": here["owner_worth"],
                "worth": here["owner_worth"]
                if here["owner_worth"] is not None
                else top["owner_worth"]
                if top["owner_worth"] is not None
                else venture_worth(conn, v),
                "step": shown(steps[0]) if steps else None,
                "needs": _needs(item, v),
            }
        )
    brainstorm = nodes(conn, scope, "level = 'step' AND status = 'open' AND template = ?", (BRAINSTORM_TEMPLATE,))
    return {
        "id": top["id"],
        "worth": top["owner_worth"],  # the owner's for every venture, or None: each venture's own
        "owner_worth": top["owner_worth"],
        "hold": top["hold_reason"],
        "hold_by": top["hold_by"],
        "ventures": items,
        "brainstorm": (
            {**shown(brainstorm[0]), "needs": desk.brainstorm_due(list(rows.values()))} if brainstorm else None
        ),
    }


def _needs(item: desk.Item | None, v: Mapping[str, Any] | None) -> str | None:
    """What a venture needs now (desk.item), without its title: the Plan tab's row names it."""
    if item is None or v is None:
        return None
    title = " ".join(str(v["title"] or "").split())[:50]  # as desk.item begins its text
    return item.text.replace(f"{title} · ", "", 1)


def _ventures_line(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """YOUR PLAN's line on the ventures: those being explored with their next decision (VENTURES shows the tree), the
    owner's worth and hold, and a brainstorm due."""
    top = ventures_node(conn, scope)
    if top is None:
        return ""
    open_nodes = nodes(conn, scope, "level = 'venture' AND status = 'open'")
    words = []
    for here in open_nodes[:VENTURES_SHOWN]:
        steps = _open_steps(conn, scope, int(here["id"]))
        decision = DECISION_OF.get(str(steps[0]["template"]), "") if steps else ""
        words.append(f"#{here['venture_id']} {_cut(str(here['title']), 40)} ({VENTURE_WORDS.get(decision, '-')})")
    more = len(open_nodes) - VENTURES_SHOWN
    said = "Ventures, each next decision a step of yours: " + (" · ".join(words) if words else "none being explored")
    if more > 0:
        said += f" · {more} more"
    if nodes(conn, scope, "level = 'step' AND status = 'open' AND template = ?", (BRAINSTORM_TEMPLATE,)):
        said += " · a brainstorm is due"
    if top["owner_worth"] is not None:
        said += f" · your owner's worth for them: {float(top['owner_worth']):g}"
    if top["hold_reason"] and top["hold_by"] == "owner":
        said += (
            f" · your owner holds new things: {_cut(top['hold_reason'], 80)}: no venture cycle and no new product"
            " until they resume them."
        )
    elif top["hold_reason"]:
        said += f" · on hold: {_cut(top['hold_reason'], 80)}: no venture cycle until they're resumed."
    return said


VENTURES_SHOWN = 6  # the ventures YOUR PLAN's line names (VENTURES lists the tree)
VENTURE_WORDS = {"triage": "triage", "appraise": "research", "answer": "answer the critic", "decide": "your owner's"}


def _close_product(
    conn: sqlite3.Connection,
    scope: AgentScope,
    product: Mapping[str, Any],
    project: Mapping[str, Any],
    now: str,
    by: str = "code",
) -> None:
    """A product whose line closed, with everything under it and its own milestone (its unlocks end with it)."""
    ended = "done" if project["status"] == "succeeded" else "dropped"
    for row in _descendants(conn, scope, product["id"]):
        _close(conn, row["id"], now, ended, f"its line closed: {project['status']}")
    _close(conn, product["id"], now, ended, f"its line closed: {project['status']}", by=by)
    _close_milestone(conn, product["id"], now, f"Its product (line #{project['id']}) closed.")


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


def _close_done(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    conn, scope = facts.conn, facts.scope
    said: list[str] = []
    stages = _stages(conn, scope, product["id"])
    for stage in stages:
        if stage["status"] != "open":
            continue
        for step in _steps(conn, scope, stage["id"]):  # a promise's or a decision's: _promises closes it
            if step["status"] == "open" and step["obligation_id"] is None and _holds(facts, step):
                _close(conn, step["id"], now, "done", "Ember's code: its check passed")
    done_upto = -1
    for index, stage in enumerate(stages):
        passed = stage["status"] == "open" and bool(stage["check_kind"]) and _holds(facts, stage)
        if stage["status"] == "done" or passed:
            done_upto = index
    for index, stage in enumerate(stages):
        if stage["status"] != "open":
            continue
        open_steps = [s for s in _steps(conn, scope, stage["id"]) if s["status"] == "open"]
        steps_done = (
            not open_steps
            and bool(_steps(conn, scope, stage["id"]))
            and not stage["check_kind"]
            and stage["stage"] != "maintain"  # it recurs: never done
        )
        if index <= done_upto or steps_done:
            moot = "a later stage is done" if index < done_upto else "the stage is done"
            for step in open_steps:
                if step["obligation_id"] is None:  # a promise or a decision closes with its obligation only
                    _close(conn, step["id"], now, "done", f"Ember's code: {moot}")
            _close(conn, stage["id"], now, "done", "Ember's code: its check passed" if index == done_upto else moot)
            said.append(f"Plan tree: product #{product['id']}'s {stage['stage']} stage is done.")
    return said


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


def _promises(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    conn, scope = facts.conn, facts.scope
    where, params = scope.where()
    said: list[str] = []
    for promise in conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND kind = 'promise' AND project_id = ? AND status = 'open'"
        " AND id NOT IN (SELECT obligation_id FROM plan_nodes WHERE obligation_id IS NOT NULL)",
        (*params, product["project_id"]),
    ).fetchall():
        stage = current_stage(conn, scope, product["id"])
        if stage is None:
            continue
        what = " ".join(str(promise["what"]).split())
        _insert(
            conn,
            scope,
            now,
            parent_id=stage["id"],
            level="step",
            project_id=product["project_id"],
            stage=stage["stage"],
            kind="promise",
            channel=promise_channel(what),
            title=f"Keep promise #{promise['id']}: {what}"[:160],
            check_kind="obligation",
            check_spec=json.dumps({"id": promise["id"]}),
            seq=900 + int(promise["id"]) % 100,
            source="promise",
            obligation_id=promise["id"],
            due=promise["due"],
        )
        said.append(f"Plan tree: promise #{promise['id']} is a step of product #{product['id']}.")
    for step in nodes(  # 0.35.0: and a decision's step, in whatever stage (a later stage done leaves it open)
        conn,
        scope,
        "level = 'step' AND obligation_id IS NOT NULL AND status = 'open' AND project_id = ?",
        (product["project_id"],),
    ):
        if _holds(facts, step):
            kept = "the promise was kept" if step["kind"] == "promise" else "the obligation was closed"
            _close(conn, step["id"], now, "done", f"Ember's code: {kept}")
    return said


def _owner_owed(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """0.37.0: a promise to the owner, or their decision, of no product line is a step of the Owner project, weighed
    like any step (until 0.36.0 only an obligation in OBLIGATIONS, its pressing ones coming first by a rule of their
    own): done once its obligation is closed. Laid out after a promise's words could name its product
    (_link_promises), and never moved there later (a node's place is fixed). 0.37.6: a promise of pins, a Bluesky
    post or a blog post has its channel, as one of a product has, so its cycle is a marketing cycle with that channel's
    tools (an ordinary one couldn't keep it, and took every cycle); one laid out before gets it at the next keep."""
    where, params = scope.where()
    said: list[str] = []
    owed = [
        row
        for row in conn.execute(
            f"SELECT * FROM obligations WHERE {where} AND kind IN ('promise', 'decision') AND status = 'open'"
            " AND id NOT IN (SELECT obligation_id FROM plan_nodes WHERE obligation_id IS NOT NULL) ORDER BY id",
            params,
        ).fetchall()
        if obligations.owed(conn, scope, row).line is None
    ]
    if owed:
        top = _platform(conn, scope, "owner", now)
        for row in owed:
            what = " ".join(str(row["what"]).split())
            promise = row["kind"] == "promise"
            _insert(
                conn,
                scope,
                now,
                parent_id=top,
                level="step",
                kind="promise" if promise else "fix",
                channel=promise_channel(what) if promise else None,
                title=(f"Keep promise #{row['id']}: {what}" if promise else f"Obligation #{row['id']}: {what}")[:160],
                check_kind="obligation",
                check_spec=json.dumps({"id": row["id"]}),
                source="promise" if promise else "code",
                obligation_id=row["id"],
                due=row["due"],
            )
            said.append(f"Plan tree: obligation #{row['id']} ({row['kind']}, of no product) is a step of your owner's.")
    top_row = nodes(conn, scope, "level = 'project' AND platform = 'owner'")
    if top_row:
        facts = Facts(conn, scope, now)
        for step in _open_steps(conn, scope, int(top_row[0]["id"])):
            if _holds(facts, step):
                kept = "the promise was kept" if step["kind"] == "promise" else "the obligation was closed"
                _close(conn, step["id"], now, "done", f"Ember's code: {kept}")
            elif step["kind"] == "promise" and step["channel"] is None and promise_channel(str(step["title"])):
                _update(conn, step["id"], now, channel=promise_channel(str(step["title"])))
    return said


def _owner_candidates(
    facts: Facts,
    today: date,
    channels: Mapping[str, bool],
    lately: Mapping[int, int],
    picks: Mapping[int, Any],
) -> list[Candidate]:
    """0.37.0: the Owner project's steps as the scorer sees them: worth weights.PROMISE_WORTH, urgent as their day
    nears (_owed_urgency). 0.37.6: one waits once a cycle took it without its obligation closing or, for a promise of a
    channel's work, the channel's requests made since it was promised moving ('tried', ``mark``), until they do or the
    next day."""
    conn, scope, now = facts.conn, facts.scope, facts.now
    top = nodes(conn, scope, "level = 'project' AND platform = 'owner'")
    if not top:
        return []
    found = []
    for step in _open_steps(conn, scope, int(top[0]["id"])):
        reason = _waiting(step, channels)
        stale = _stale(facts, step, picks)
        if reason is None and stale == today.isoformat():
            reason = "tried"
        found.append(
            Candidate(
                weights.Step(
                    step["id"],
                    None,
                    str(step["title"]),
                    _kind(step),
                    weights.PROMISE_WORTH,
                    urgency=_owed_urgency(step, today, lately),
                    age_days=round(_days(step["ready_since"], now), 3) if reason is None else 0.0,
                    pinned=bool(step["pinned"]),
                    blocked=reason is not None,
                ),
                "owner",
                reason,
                stale is not None,
            )
        )
    return found


def promise_channel(what: str) -> str | None:
    """0.35.1: the channel a promise's words name (PROMISE_CHANNELS), None for none. 0.37.6: none for a report ("Report
    the Bluesky reactions of the week"): a report is told in a message, not made with a channel's tools (live on
    2026-10-09, six of the nine promises open were reports; an Owner promise of a channel's work has its channel
    now)."""
    if REPORT.search(what):
        return None
    return next((channel for channel, words in PROMISE_CHANNELS if words.search(what)), None)


def _link_promises(conn: sqlite3.Connection, scope: AgentScope) -> list[str]:
    """0.35.1: an open promise made without naming its product gets the one its words name (obligations.promised_line:
    a listing's number, KDP), so it becomes a step of that product."""
    where, params = scope.where()
    said: list[str] = []
    for row in conn.execute(
        f"SELECT id, what FROM obligations WHERE {where} AND kind = 'promise' AND status = 'open'"
        " AND project_id IS NULL ORDER BY id",
        params,
    ).fetchall():
        line = obligations.promised_line(conn, scope, str(row["what"]))
        if line is not None and obligations.name_line(conn, int(row["id"]), line):
            said.append(f"Plan tree: promise #{row['id']} names line #{line}: a step of that product now.")
    return said


def _decided(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, list[tuple[Any, bool]]]:
    """0.35.0: the open decisions of the owner's that aren't steps yet, by the product line they are about
    (obligations.owed: a request's line), each with whether it is marketing work (a pin, a post, a blog post)."""
    where, params = scope.where()
    found: dict[int, list[tuple[Any, bool]]] = {}
    for row in conn.execute(
        f"SELECT * FROM obligations WHERE {where} AND kind = 'decision' AND status = 'open'"
        " AND id NOT IN (SELECT obligation_id FROM plan_nodes WHERE obligation_id IS NOT NULL) ORDER BY id",
        params,
    ).fetchall():
        owed = obligations.owed(conn, scope, row)
        if owed.line is not None:
            found.setdefault(owed.line, []).append((row, owed.marketing))
    return found


def _decisions(facts: Facts, product: Mapping[str, Any], decided: list[tuple[Any, bool]], now: str) -> list[str]:
    """0.35.0: an owner's decision on one of the product's requests (rejected, carried out, failed) is a step of its
    current stage, the reaction it asks for: done once its obligation is closed. Live, a rejected listing left its
    product waiting for an approval that never came; now the rejection itself is the next step."""
    conn, scope = facts.conn, facts.scope
    said: list[str] = []
    stage = current_stage(conn, scope, product["id"])
    if stage is None:
        return said
    for row, marketing in decided:
        what = " ".join(str(row["what"]).split())
        step = _insert(
            conn,
            scope,
            now,
            parent_id=stage["id"],
            level="step",
            project_id=product["project_id"],
            stage=stage["stage"],
            kind="market" if marketing else "fix",
            title=f"Obligation #{row['id']}: {what}"[:160],
            check_kind="obligation",
            check_spec=json.dumps({"id": row["id"]}),
            seq=800 + int(row["id"]) % 100,
            source="code",
            obligation_id=row["id"],
            due=row["due"],
        )
        change(conn, scope, step, None, "code", "add", f"your owner's decision: obligation #{row['id']}", now)
        said.append(f"Plan tree: obligation #{row['id']} (a decision) is a step of product #{product['id']}.")
    return said


def _again(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    """0.37.6: a request to the owner that a product's stage still needs, proposed again by a step of Ember's code's.
    Once the stage has no step of Ember's open, and none of the kinds of request its check or its steps need is
    pending, approved or carried out for the product, it gets one: "Propose ... again" when an earlier one expired, was
    withdrawn, rejected or failed (the step that proposed it had closed on it, and closed is final: in the analysis of
    0.37.0 the product never had a ready step again, and "You approve it" waited on the owner with nothing waiting
    for them), else the template's step that proposes it (the create stage's check needs the listing proposed, its
    files and photos made). Its check: a request made since then. One at a time."""
    conn, scope = facts.conn, facts.scope
    stage = current_stage(conn, scope, product["id"])
    if stage is None or stage["stage"] == "maintain":
        return []
    steps = [s for s in _steps(conn, scope, stage["id"]) if s["status"] != "dropped"]
    if any(s["status"] == "open" and s["obligation_id"] is None and s["kind"] != templates.OWNER_KIND for s in steps):
        return []  # a step of Ember's to take
    line = int(product["project_id"])
    needed = [*_asks(stage["check_kind"], _spec(stage)), *(e for s in steps for e in _asks(s["check_kind"], _spec(s)))]
    for executor in dict.fromkeys(needed):
        if passes(facts, line, "request", {"executor": executor}):
            continue  # one is on its way, or carried out
        where, params = scope.where("a")
        ended = conn.execute(
            "SELECT a.id, a.status FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
            f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor = ?"
            " ORDER BY a.id DESC LIMIT 1",
            (*params, line, executor),
        ).fetchone()
        title = _proposes(product, executor)
        if ended is not None:
            title = f"{title} again: request #{ended['id']} {ended['status']}"
        seq = next((int(s["seq"]) for s in steps if executor in _asks(s["check_kind"], _spec(s))), None)
        if seq is None:
            seq = max((int(s["seq"]) for s in steps if s["obligation_id"] is None), default=-1) + 1
        step = _insert(
            conn,
            scope,
            now,
            parent_id=stage["id"],
            level="step",
            project_id=line,
            template=f"propose/{executor}"[:80],
            stage=stage["stage"],
            kind="ship",
            title=_cut(title, 160),
            check_kind="request",
            check_spec=json.dumps({"executor": executor, "since": now}),
            seq=seq,
            source="code",
        )
        why = f"request #{ended['id']} {ended['status']}" if ended is not None else "its stage needs it proposed"
        change(conn, scope, step, None, "code", "add", f"{why}: none waits for your owner", now)
        return [f"Plan tree: line #{line}'s {stage['stage']} stage needs a request to your owner: {_quoted(title)}."]
    return []


def _asks(kind: str | None, spec: Mapping[str, Any]) -> tuple[str, ...]:
    """0.37.6: the executors whose request a check needs made (a request check, or one of an any)."""
    if kind == "any":
        return tuple(e for s in spec.get("of", ()) for e in _asks(s.get("check"), s))
    return (str(spec["executor"]),) if kind == "request" and spec.get("executor") else ()


def _proposes(product: Mapping[str, Any], executor: str) -> str:
    """The title of the step of a product's template that proposes a request by ``executor`` ("Propose the listing")."""
    template = templates.by_key(product["template"])
    return next(
        (
            step.title
            for stage in template.stages
            for step in stage.steps
            if step.check == "request" and step.spec.get("executor") == executor and step.kind != templates.OWNER_KIND
        ),
        "Propose it to your owner",
    )


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


# --- a product's own milestone, and the records the tree takes the place of (0.35.0) ---


def product_milestone(conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], now: str) -> int:
    """A product's own milestone (Ember's code's, of the kind first_test, linked to its line, due a year on): what the
    owner's Autonomy unlocks for the product stand on (policy.py keys them by milestone), made the first time one is
    needed. The roadmap's readers leave it out (roadmap.SHOWN)."""
    found = conn.execute("SELECT id FROM milestones WHERE product_node = ?", (product["id"],)).fetchone()
    if found is not None:
        return int(found["id"])
    made = roadmap.create(
        conn,
        scope,
        title=_cut(f"{product['title']}: its place in the plan", 100),
        measure="Ember's code keeps it while the product is in the plan: your unlocks for the product stand on it",
        due=(from_iso(now).date() + timedelta(days=365)).isoformat(),
        now=now,
        project_id=int(product["project_id"]),
        created_by="code",
        kind="first_test",
    )
    conn.execute("UPDATE milestones SET product_node = ? WHERE id = ?", (product["id"], made))
    return made


def _close_milestone(conn: sqlite3.Connection, product_id: int, now: str, why: str) -> None:
    conn.execute(
        "UPDATE milestones SET status = 'dropped', closed_by = 'code', closed_at = ?, updated_at = ?, result = ?"
        " WHERE product_node = ? AND status = 'open'",
        (now, now, why[:600], product_id),
    )


def _upto(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (MOVED_KEY,)).fetchone()
    try:
        return int(row["value"]) if row is not None else 0
    except ValueError:
        return 0


def move(conn: sqlite3.Connection, scope: AgentScope, now: str) -> list[str]:
    """Once (migration 0088 named the newest milestone it applies to): the milestones the plan tree takes the place
    of close, the owner's Autonomy unlocks on them carried to their product's milestone first: the listing test's bars
    and scale points, the goal's decision points, and Ember's own milestones (one of a product becomes a step of its
    current stage). A missed bar owes nothing any more. Returns what happened, for the owner's System log."""
    upto = _upto(conn)
    if upto <= 0:
        return []
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT * FROM milestones WHERE {where} AND status = 'open' AND id <= ? AND product_node IS NULL"
        " AND (created_by = 'agent' OR created_by = 'code' AND (id IN (SELECT milestone_id FROM listing_gates)"
        " OR kind = 'decision' AND venture_id IS NULL AND project_id IS NULL)) ORDER BY id",
        (*params, upto),
    ).fetchall()
    said: list[str] = []
    standing = policy.standing(conn, scope)
    for m in rows:
        products = _products_of(conn, scope, m)
        for g in standing.get(int(m["id"]), []):
            for product in products:
                carried = product_milestone(conn, scope, product, now)
                total, _ = policy.used(conn, int(g["id"]), _Day(now))
                policy.set_grant(
                    conn,
                    scope,
                    carried,
                    str(g["rule"]),
                    str(g["level"]),
                    now,
                    per_day=int(g["per_day"]),
                    budget=max(1, int(g["budget"]) - total),
                    by=str(g["by"]),
                    why=f"0.35.0: carried over from milestone #{m['id']}",
                )
                said.append(
                    f"Plan tree: your unlock for {policy.RULES[str(g['rule'])].short} on milestone #{m['id']} is now"
                    f" on product line #{product['project_id']}."
                )
        step = None
        if m["created_by"] == "agent" and products:
            stage = current_stage(conn, scope, products[0]["id"])
            if stage is not None:
                step = _insert(
                    conn,
                    scope,
                    now,
                    parent_id=stage["id"],
                    level="step",
                    project_id=products[0]["project_id"],
                    stage=stage["stage"],
                    kind="create",
                    title=_cut(f"From your milestone #{m['id']}: {m['title']}", 160),
                    check_kind="agent",
                    seq=700 + int(m["id"]) % 100,
                    source="agent",
                )
                change(conn, scope, step, None, "code", "add", f"your milestone #{m['id']} moved into the plan", now)
        why = (
            f"0.35.0: a step of your plan now (#{step})."
            if step is not None
            else "0.35.0: your plan's steps take the place of your own milestones."
            if m["created_by"] == "agent"
            else "0.35.0: the plan tree's decide-by dates take the place of the listing test's bars and the goal's"
            " decision points."
        )
        conn.execute(
            "UPDATE milestones SET status = 'dropped', closed_by = 'code', closed_at = ?, updated_at = ?, result = ?"
            " WHERE id = ? AND status = 'open'",
            (now, now, why, m["id"]),
        )
        said.append(f"Plan tree: milestone #{m['id']} {_quoted(m['title'])} closed: {why.removeprefix('0.35.0: ')}")
    owed, owed_params = scope.where()
    conn.execute(
        "UPDATE obligations SET status = 'closed', closed_by = 'code', closed_at = ?, result = ?"
        f" WHERE {owed} AND status = 'open' AND kind = 'miss' AND milestone_id IN (SELECT milestone_id FROM"
        " listing_gates)",
        (
            now,
            "0.35.0: the listing test's bars retired; the plan tree's decide-by dates ask what a miss would have",
            *owed_params,
        ),
    )
    return said


@dataclass(frozen=True)
class _Day:
    """policy.used's clock: today's start, from ``now``."""

    now: str

    def today(self) -> date:
        return from_iso(self.now).date()

    def day_start(self, day: date) -> Any:
        return from_iso(f"{day.isoformat()}T00:00:00Z")


def _products_of(conn: sqlite3.Connection, scope: AgentScope, milestone: Mapping[str, Any]) -> list[Any]:
    """The open products a milestone was about: its line's, or (a venture's) each of the venture's lines'."""
    if milestone["project_id"] is not None:
        lines = [int(milestone["project_id"])]
    elif milestone["venture_id"] is not None:
        where, params = scope.where()
        lines = [
            int(r[0])
            for r in conn.execute(
                f"SELECT id FROM projects WHERE {where} AND venture_id = ? ORDER BY id",
                (*params, milestone["venture_id"]),
            )
        ]
    else:
        return []
    found = []
    for line in lines:
        found += nodes(conn, scope, "level = 'product' AND status = 'open' AND project_id = ?", (line,))
    return found


def _decide_by(facts: Facts, product: Mapping[str, Any], now: str, today: date) -> list[str]:
    """A product's decide-by dates once a listing of its line is live (whatever its template: a line laid out before
    its first listing keeps the one it got), each read once on or after its day from the start of its test (the day
    its first listing was seen live; a listing test begun before 0.35.0 keeps its start and what its bars found).
    Their verdicts are kept on the product (decide_by), and a views bar missed makes its marketing urgent
    (pushed_until). What the owner decides comes as a step of theirs; a first order brings Ember a step to scale it.
    None is read while the owner's park or kill of its venture stops the line (they decided on it already)."""
    conn, scope = facts.conn, facts.scope
    line = int(product["project_id"])
    if ventures.project_stopped(conn, scope, line) is not None:
        return []
    found: dict[str, str] = json.loads(product["decide_by"]) if product["decide_by"] else {}
    start = product["live_since"]
    if start is None:
        start, found = _test_start(conn, scope, line, found)
        if start is None:
            if not facts.live(line):
                return []
            start = today.isoformat()
    day = (today - date.fromisoformat(start)).days
    funnel = facts.funnel(line)
    views, favorites, orders = (funnel.views, funnel.favorites, funnel.orders) if funnel else (0, 0, 0)
    traffic = funnel.traffic if funnel else 0
    pushed = product["pushed_until"]
    said: list[str] = []
    numbers = f"{views} views, {favorites} favorites, {orders} orders after {traffic} reach actions"

    def push(until_day: int) -> str:
        return (date.fromisoformat(start) + timedelta(days=until_day)).isoformat()

    if day >= 7 and "day7" not in found:
        found["day7"] = "met" if views >= DAY7_VIEWS else "missed"
        if found["day7"] == "missed":
            pushed = max(pushed or "", push(7 + PUSH_DAYS))
            said.append(
                f"Plan tree: line #{line} has {views} views on day 7 (of {DAY7_VIEWS}): its marketing comes first."
            )
    if day >= 14 and "day14" not in found:
        if views >= DAY14_VIEWS and favorites >= DAY14_FAVORITES:
            found["day14"] = "met"
        elif traffic < reach.ENOUGH:
            found["day14"] = "retry"  # not seen, so not tested: one more try, its marketing urgent until day 28
            pushed = max(pushed or "", push(28))
            said.append(f"Plan tree: line #{line} on day 14: {numbers}; too little reach to judge it: one more try.")
        else:
            found["day14"] = "decide"
            said += _owner_decides(conn, scope, product, now, f"day 14: {numbers}")
    if day >= 28 and found.get("day14") == "retry" and "day28" not in found:
        found["day28"] = "met" if views >= DAY14_VIEWS else "decide"
        if found["day28"] == "decide":
            said += _owner_decides(conn, scope, product, now, f"day 28: {numbers}")
    if day >= 21 and "day21" not in found:
        found["day21"] = "scale" if orders >= 1 else "decide"
        if found["day21"] == "scale":
            said += _scale(conn, scope, product, now)
        else:
            said += _owner_decides(conn, scope, product, now, f"day 21, no order yet: {numbers}")
    changes = {"live_since": start, "decide_by": json.dumps(found, sort_keys=True), "pushed_until": pushed or None}
    if any(product[k] != v for k, v in changes.items()):
        _update(conn, product["id"], now, **changes)
    return said


def _maintain(conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any]) -> Any | None:
    stages = _stages(conn, scope, product["id"])
    return next((s for s in stages if s["stage"] == "maintain" and s["status"] == "open"), None)


def _test_start(
    conn: sqlite3.Connection, scope: AgentScope, line: int, found: dict[str, str]
) -> tuple[str | None, dict[str, str]]:
    """A listing test begun before 0.35.0 (listing_gates): its start, and what its closed bars found."""
    where, params = scope.where("g")
    bars = conn.execute(
        "SELECT g.gate, g.started_on, m.status FROM listing_gates g JOIN milestones m ON m.id = g.milestone_id"
        f" WHERE {where} AND g.project_id = ? ORDER BY g.id",
        (*params, line),
    ).fetchall()
    if not bars:
        return None, found
    found = dict(found)
    status = {str(b["gate"]): str(b["status"]) for b in bars}
    if status.get("day7_views") in ("done", "missed"):
        found["day7"] = "met" if status["day7_views"] == "done" else "missed"
    views14, favorites14 = status.get("day14_views"), status.get("day14_favorites")
    if views14 == "missed" or favorites14 == "missed":
        found["day14"] = "decide"
    elif views14 == "done" and favorites14 == "done":
        found["day14"] = "met"
    if status.get("day21_sale") in ("done", "missed"):
        found["day21"] = "scale" if status["day21_sale"] == "done" else "decide"
    return str(min(b["started_on"] for b in bars)), found


def _owner_decides(
    conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], now: str, numbers: str
) -> list[str]:
    """A decide-by date that asks the owner: keep the product or drop it, as a step of theirs (one at a time), in the
    product's maintain stage (it never closes, so the step waits there without holding up its launch)."""
    line = int(product["project_id"])
    if nodes(
        conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND template = 'decide/owner'", (line,)
    ):
        return []
    stage = _maintain(conn, scope, product)
    if stage is None:
        return []
    step = _insert(
        conn,
        scope,
        now,
        parent_id=stage["id"],
        level="step",
        project_id=line,
        template="decide/owner",
        stage=stage["stage"],
        kind=templates.OWNER_KIND,
        title=_cut(f"You decide: keep line #{line} or drop it ({numbers})", 160),
        seq=850,
        source="code",
    )
    change(conn, scope, step, None, "code", "add", f"a decide-by date: {numbers}", now)
    return [f"Plan tree: line #{line} reached a decide-by date ({numbers}): your owner decides on it."]


def _scale(conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], now: str) -> list[str]:
    """A first order by day 21: Ember's step to scale the product (5 variants or a bundle), hers to split, in its
    maintain stage."""
    line = int(product["project_id"])
    stage = _maintain(conn, scope, product)
    if stage is None:
        return []
    step = _insert(
        conn,
        scope,
        now,
        parent_id=stage["id"],
        level="step",
        project_id=line,
        template="decide/scale",
        stage=stage["stage"],
        kind="create",
        title=SCALE,
        check_kind="agent",
        seq=300,
        source="code",
    )
    change(conn, scope, step, None, "code", "add", "a first order by day 21", now)
    return [f"Plan tree: line #{line} sold by day 21: a step to scale it."]


def _audience_channels(product: Mapping[str, Any]) -> list[str]:
    audience = product["audience"] or "both"
    return [c for c in CHANNELS if audience in CHANNEL_AUDIENCES[c]]


def _recurring(
    facts: Facts, product: Mapping[str, Any], now: str, today: date, channels: Mapping[str, bool]
) -> list[str]:
    """A live product's recurring steps: once every stage before its maintain stage is done (its launch, or a
    template's last before it), one open step per channel's duty (a missed one stays open and ages); and a fix
    whenever the critic says improve (it judges live listings only)."""
    conn, scope = facts.conn, facts.scope
    stages = _stages(conn, scope, product["id"])
    maintain = next((s for s in stages if s["stage"] == "maintain" and not _replaced(s, product)), None)
    if maintain is None or maintain["status"] != "open":
        return []
    said: list[str] = []
    title = str(product["title"])
    launched = not any(s["status"] == "open" for s in stages if s["stage"] != "maintain")
    if launched and templates.by_key(product["template"]).name != "kdp_book":  # a book can't be linked yet
        funnel = facts.funnel(product["project_id"])
        for channel in _audience_channels(product):
            if not channels.get(channel, False):
                continue
            check, days, words = RECURRING[channel]
            key = f"recurring/{channel}"
            if nodes(conn, scope, "parent_id = ? AND template = ? AND status = 'open'", (maintain["id"], key)):
                continue
            last = nodes(conn, scope, "parent_id = ? AND template = ? AND status <> 'open'", (maintain["id"], key))
            if last and last[-1]["due"] and date.fromisoformat(last[-1]["due"]) > today:
                continue  # this period's is done: the next comes when it ends
            have = 0 if funnel is None else {"pin": funnel.pins, "post": funnel.bluesky, "blog": funnel.posts}[check]
            _insert(
                conn,
                scope,
                now,
                parent_id=maintain["id"],
                level="step",
                project_id=product["project_id"],
                template=key,
                stage="maintain",
                kind="market",
                channel=channel,
                title=words.format(title=title)[:160],
                check_kind=check,
                check_spec=json.dumps({"count": have + 1}),
                seq=100 + CHANNELS.index(channel),
                source="code",
                due=(today + timedelta(days=days)).isoformat(),
            )
            said.append(f"Plan tree: product #{product['id']}'s {channel} step for this period.")
    if facts.verdict(product["project_id"]) == "improve" and not nodes(
        conn,
        scope,
        "project_id = ? AND level = 'step' AND check_kind = 'critic' AND status = 'open'",
        (product["project_id"],),
    ):
        _insert(
            conn,
            scope,
            now,
            parent_id=maintain["id"],
            level="step",
            project_id=product["project_id"],
            template="recurring/critic",
            stage="maintain",
            kind="fix",
            title="Fix what the critic found",
            check_kind="critic",
            check_spec="{}",
            seq=200,
            source="code",
        )
        said.append(f"Plan tree: product #{product['id']} has a critic's fix to make.")
    return said


# --- what is ready, and its weight ---


@dataclass(frozen=True)
class Candidate:
    """A step as the scorer sees it, with what the owner's tab shows of it."""

    step: weights.Step
    stage: str
    # why it can't be taken now ('owner', 'channel', 'upgrade', 'step', 'hold'; 0.37.6 'approved', 'tried'), None: ready
    waiting: str | None
    # 0.37.6: a cycle took it, and nothing its check reads moved since (it waits 'tried' while that was today; ready
    # again the next day for a try, it doesn't cut a cycle's sleep: busy)
    tried: bool = False


def _waiting(row: Mapping[str, Any], channels: Mapping[str, bool]) -> str | None:
    if row["kind"] == templates.OWNER_KIND:
        return "owner"
    if row["waiting"]:
        return str(row["waiting"])
    if row["channel"] and not channels.get(row["channel"], False):
        return "channel"
    return None


def _channel_factors(conn: sqlite3.Connection, scope: AgentScope, now: str) -> dict[str, float]:
    """How well each channel works, per item (an untested channel: 1.0): clicks per live pin, reactions per post,
    of those live RESULT_DAYS at least (0.35.3)."""
    where, params = scope.where()
    factors = {c: 1.0 for c in CHANNELS}
    settled = to_iso(from_iso(now) - timedelta(days=RESULT_DAYS))
    pins = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(clicks), 0) AS clicks FROM pinterest_pins WHERE {where}"
        " AND status = 'active' AND finished_at <= ?",
        (*params, settled),
    ).fetchone()
    if pins["n"] >= FRESH_PINS:
        factors["pinterest"] = round(
            min(weights.CHANNEL_MAX, max(weights.CHANNEL_MIN, pins["clicks"] / pins["n"] / CLICKS_PER_PIN)), 2
        )
    posts = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(likes, 0) + COALESCE(reposts, 0) + COALESCE(replies, 0)"
        f" + COALESCE(quotes, 0)), 0) AS reactions FROM bluesky_posts WHERE {where} AND status = 'active'"
        " AND finished_at <= ?",
        (*params, settled),
    ).fetchone()
    if posts["n"] >= FRESH_POSTS:
        factors["bluesky"] = round(
            min(weights.CHANNEL_MAX, max(weights.CHANNEL_MIN, posts["reactions"] / posts["n"] / REACTIONS_PER_POST)), 2
        )
    return factors


def _missed(conn: sqlite3.Connection, scope: AgentScope, today: date) -> set[int]:
    """The products whose marketing is urgent: a views bar of their decide-by dates missed lately (pushed_until;
    0.33.0: a miss owes a push for buyers, never title and tag edits)."""
    return {
        int(r["project_id"])
        for r in nodes(conn, scope, "level = 'product' AND status = 'open' AND pushed_until >= ?", (today.isoformat(),))
    }


def _worked(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, str]:
    """When each product line was last worked on: its newest cycle's start."""
    return {
        int(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT project_id, MAX(started_at) FROM cycles WHERE session = ? AND simulated = ?"
            " AND project_id IS NOT NULL GROUP BY project_id",
            (scope.session, 1 if scope.simulated else 0),
        )
    }


def _days(since: str | None, now: str) -> float:
    if not since:
        return 0.0
    return max(0.0, (from_iso(now) - from_iso(since)).total_seconds() / 86_400)


def candidates(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    exploring: bool = False,
    before: int | None = None,
) -> list[Candidate]:
    """Every open step of the open products, ready or waiting, weighed by weights.py's parts (the scorer leaves out
    the waiting ones). An owner's decision (``_decisions``) is a step of its own in whatever stage it stands, and
    (0.35.1) so is a promise; it waits while a request of its product made since it was promised waits on the owner.
    0.37.0: both are weighed like any step, worth at least weights.PROMISE_WORTH and urgent as their day nears, until
    cycles took them PROMISE_TRIES times (a decision once) in OBLIGATION_HOURS (until 0.36.0 they came first that
    often; 0.37.1: urgent only from weights.PROMISE_NEAR_DAYS before their day); those of no product are the Owner
    project's (``_owner_candidates``). The owner's word comes before Ember's hold: only their park or kill, or a
    closed line, stops these two. 0.36.0: and the ventures' steps (0.37.0: each venture's; ``exploring``: whether this
    cycle may explore; ``before``: the cycle it is for, None the next), which take turns with a new product while no
    product step is ready (a new install's first cycles: live until 0.35.3 the day's first cycle was an ordinary one,
    which started a product).

    0.37.6, the brake, one rule for every step: one a cycle took without what its check reads moving (``mark``) waits
    until that moves or the owner's next day ('tried'); a step of Ember's whose own requests are on their way, and all
    its check still needs, waits on them ('owner' while one waits for the owner's decision, 'approved' while they are
    carried out), without one of Ember's waits; an owner's step with nothing for them to decide waits on the step of
    Ember's before it ('step': in the analysis of 0.37.0, "You approve it" waited on the owner while no request
    did)."""
    facts = Facts(conn, scope, now)
    factors = _channel_factors(conn, scope, now)
    missed = _missed(conn, scope, today)
    worked = _worked(conn, scope)
    lately = _taken_counts(conn, scope, to_iso(from_iso(now) - timedelta(hours=OBLIGATION_HOURS)))
    asked = [(r["line"], r["created_at"]) for r in facts.requests() if r["status"] == "pending"]
    promised_at = _promised_at(conn, scope)
    picks = _last_picks(conn, scope)
    day = today.isoformat()
    found: list[Candidate] = []
    for product in nodes(conn, scope, "level = 'product' AND status = 'open'"):
        pid = int(product["project_id"])
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        stopped = project is None or ventures.project_stopped(conn, scope, pid) is not None
        held = bool(product["hold_reason"])
        current = current_stage(conn, scope, product["id"])
        if current is None:
            continue
        funnel = facts.funnel(pid)
        worth = weights.worth(
            float(product["could_earn"] or 0),
            str(current["stage"]),
            funnel.views if funnel else 0,
            funnel.favorites if funnel else 0,
            funnel.orders if funnel else 0,
            product["owner_worth"],
        )
        verdict = facts.verdict(pid)
        live = facts.live(pid) > 0  # its maintain steps (the critic's fix, a decide-by's) are ready once it is live
        rows = [
            (stage, step)
            for stage in _stages(conn, scope, product["id"])
            for step in _steps(conn, scope, stage["id"])
            if step["status"] == "open" and (stage["status"] == "open" or step["obligation_id"] is not None)
        ]
        owed = [(stage, step) for stage, step in rows if step["obligation_id"] is not None]
        work = [(stage, step) for stage, step in rows if step["obligation_id"] is None]
        steps: dict[int, weights.Step] = {}
        reasons: dict[int, str | None] = {}
        stale: dict[int, str | None] = {}
        first_taken = False
        for stage, step in work:
            sequential = stage["stage"] in SEQUENTIAL
            later = stage["id"] != current["id"] and not (
                stage["stage"] == "maintain" and (current["stage"] == "launch" or live)
            )
            stale[step["id"]] = _stale(facts, step, picks)
            reason: str | None
            if stopped or held:
                reason = "hold"
            elif later or (sequential and first_taken):
                reason = "step"
            elif step["kind"] == templates.OWNER_KIND:
                reason = _owners(facts, step, pid)  # 0.37.6: theirs while something waits for them
            else:
                reason = _waiting(step, channels) or _requested(facts, step, pid)
                if reason is None and stale[step["id"]] == day:
                    reason = "tried"
            if sequential and stage["id"] == current["id"] and reason is None:
                first_taken = True
            urgency = 0.0
            if step["kind"] == "market" and stage["stage"] == "launch" and live:  # 0.35.3: its first buyers
                urgency = weights.REACH
            if step["kind"] == "market" and pid in missed:
                urgency = max(urgency, weights.MISSED_BAR)
            if step["check_kind"] == "critic" and verdict == "improve":  # 0.35.3: a defect, or suggestions
                urgency = max(urgency, weights.DEFECT if facts.defect(pid) else weights.IMPROVE)
            if step["due"] and step["source"] == "code" and date.fromisoformat(step["due"]) <= today:
                urgency = max(urgency, weights.RECURRING_DUE)
            since = max(filter(None, (step["ready_since"], worked.get(pid))), default=None)
            steps[step["id"]] = weights.Step(
                step["id"],
                pid,
                str(step["title"]),
                _kind(step),
                worth,
                channel=factors.get(step["channel"], 1.0) if step["channel"] else 1.0,
                urgency=urgency,
                age_days=round(_days(since, now), 3) if reason is None else 0.0,
                pinned=bool(step["pinned"]),
                blocked=reason is not None,
            )
            reasons[step["id"]] = reason
        ordered = [steps[step["id"]] for _, step in work]
        for index, (stage, step) in enumerate(work):
            behind = ordered[index + 1 :] if stage["stage"] in SEQUENTIAL else []
            waiting = tuple(s for s in behind if s.product == pid)
            found.append(
                Candidate(
                    weights.Step(**{**_fields(steps[step["id"]]), "waiting": waiting}),
                    str(stage["stage"]),
                    reasons[step["id"]],
                    stale[step["id"]] is not None,
                )
            )
        for stage, step in owed:
            promise = step["kind"] == "promise"
            reason = "hold" if stopped else _waiting(step, channels)
            made = promised_at.get(int(step["obligation_id"]))
            if reason is None and promise and made and any(line == pid and at > made for line, at in asked):
                reason = "owner"  # a request for it waits on the owner's decision
            tried = _stale(facts, step, picks)
            if reason is None and tried == day:
                reason = "tried"
            since = max(filter(None, (step["ready_since"], worked.get(pid))), default=None)
            found.append(
                Candidate(
                    weights.Step(
                        step["id"],
                        pid,
                        str(step["title"]),
                        _kind(step),
                        max(worth, weights.PROMISE_WORTH),  # 0.37.0: the owner's, whatever its product is worth
                        urgency=_owed_urgency(step, today, lately),
                        age_days=round(_days(since, now), 3) if reason is None else 0.0,
                        blocked=reason is not None,
                    ),
                    str(stage["stage"]),
                    reason,
                    tried is not None,
                )
            )
    found += _owner_candidates(facts, today, channels, lately, picks)  # 0.37.0: what is owed of no product
    # 0.37.6: a step that waits for tomorrow since a cycle tried it is work of Ember's still: no new product's turn
    turn = exploring and not any(c.waiting in (None, "tried") for c in found) and _ventured_last(conn, scope, before)
    # 0.37.0: and the ventures' steps
    return found + _venture_candidates(conn, scope, now, today, exploring, turn, picks)


def _taken_counts(conn: sqlite3.Connection, scope: AgentScope, since: str) -> dict[int, int]:
    """How often cycles took each step since ``since`` (0.35.0: an owner's decision is taken first once in
    OBLIGATION_HOURS; 0.35.1: a promise PROMISE_TRIES times)."""
    where, params = scope.where()
    return {
        int(r[0]): int(r[1])
        for r in conn.execute(
            f"SELECT node_id, COUNT(*) FROM plan_picks WHERE {where} AND node_id IS NOT NULL AND created_at >= ?"
            " GROUP BY node_id",
            (*params, since),
        )
    }


def _promised_at(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, str]:
    """0.35.1: when each open promise was made."""
    where, params = scope.where()
    return {
        int(r["id"]): str(r["created_at"])
        for r in conn.execute(
            f"SELECT id, created_at FROM obligations WHERE {where} AND kind = 'promise' AND status = 'open'", params
        )
    }


# --- the brake (0.37.6) ---


def _last_picks(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, sqlite3.Row]:
    """Each step's latest pick by a cycle that ran its plan (FINISHED: a cycle that failed, was refused or still runs
    didn't get to work on it), with what its check read then (``mark``) and the owner's day it was taken on."""
    where, params = scope.where("k")
    found: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT k.node_id, k.cycle_id, k.mark, k.day FROM plan_picks k JOIN cycles c ON c.id = k.cycle_id"
        f" WHERE {where} AND k.node_id IS NOT NULL AND c.status IN {FINISHED} ORDER BY k.id",
        params,
    ):
        found[int(r["node_id"])] = r
    return found


def _stale(facts: Facts, row: Mapping[str, Any], picks: Mapping[int, Any]) -> str | None:
    """The owner's day a cycle last took a step on, while nothing moved for it since (``mark``; a promise's or a step's
    Ember says is done: and that cycle made nothing for its product, ``_made_in``): it waits 'tried' while that day is
    today. None when no cycle took it (since 0.37.6) or something moved."""
    pick = picks.get(int(row["id"]))
    if pick is None or not pick["mark"] or mark(facts, row) != pick["mark"]:
        return None
    return None if _made_in(facts.conn, int(pick["cycle_id"]), row) else str(pick["day"] or "")


COUNT_NOTHING = (None, "agent", "obligation")  # checks that count nothing of their own before they pass


def mark(facts: Facts, row: Mapping[str, Any]) -> str:
    """What a step's check reads now, as a short digest, kept with each pick (plan_picks.mark): something moved for the
    step once this changed. The numbers its check counts and its own requests with their states (a request made,
    decided, expired or carried out); for a promise, a decision or a step Ember says is done, which count nothing of
    their own, their obligation and what became of their product's requests (the owner's decisions: one made since
    doesn't move them; what the cycle that took a promise or Ember's step made for its product does, ``_made_in``), and
    for one of no product what became of the requests of its channel made since it was promised; for a venture's step
    its venture's records (stage, evidence, cases, the critic's answers; not a note or a rescore), for a brainstorm
    the ideas; and the owner's word on the step (a pin since)."""
    conn = facts.conn
    line = row["project_id"]
    kind, spec = row["check_kind"], _spec(row)
    read: Any
    if row["template"] in VENTURE_TEMPLATES:
        read = _venture_read(conn, facts.scope, row)
    elif kind in COUNT_NOTHING:
        owed = None
        if kind == "obligation":
            owed = conn.execute("SELECT status, created_at FROM obligations WHERE id = ?", (spec.get("id"),)).fetchone()
        own: Any = None
        if line is not None:
            own = _own(facts, int(line), None)
        elif owed is not None and row["channel"] in CHANNEL_EXECUTORS:
            own = _own(facts, None, (CHANNEL_EXECUTORS[str(row["channel"])],), since=str(owed["created_at"]))
        decided = [r for r in own or () if r[1] != "pending"]
        read = [owed["status"] if owed is not None else None, decided]
    else:
        read = _check_read(facts, int(line), kind, spec) if line is not None else None
    words = conn.execute("SELECT COUNT(*) FROM plan_words WHERE node_id = ?", (row["id"],)).fetchone()[0]
    return sha256(canonical([words, read]))[:16]


def _made_in(conn: sqlite3.Connection, cycle_id: int, row: Mapping[str, Any]) -> bool:
    """Whether the cycle that took a promise of a product or a step Ember says is done (checks that count nothing of
    their own before they pass: work toward them can take several cycles) made something for its product that its
    checks read: a file, a picture or a KDP package, a demand note, a request to the owner. Work on the product in
    other cycles doesn't move them (those take the product's own steps), and an owner's decision moves only once it is
    closed or something became of its product's requests (in a simulated week of dry run, decisions the fake model
    never closed were taken again after each request a cycle made)."""
    if row["check_kind"] not in (None, "agent") and row["kind"] != "promise":
        return False
    if row["project_id"] is None or row["template"] in VENTURE_TEMPLATES:
        return False
    marks = ", ".join("?" for _ in MADE_BY)
    made = conn.execute(
        f"SELECT 1 FROM tool_calls WHERE cycle_id = ? AND status = 'ok' AND tool IN ({marks}) LIMIT 1",
        (cycle_id, *MADE_BY),
    ).fetchone()
    noted = conn.execute(
        "SELECT 1 FROM demand_notes WHERE cycle_id = ? AND project_id = ? LIMIT 1", (cycle_id, row["project_id"])
    ).fetchone()
    asked = conn.execute("SELECT 1 FROM approvals WHERE cycle_id = ? LIMIT 1", (cycle_id,)).fetchone()
    return made is not None or noted is not None or asked is not None


def _check_read(facts: Facts, line: int, kind: str, spec: Mapping[str, Any]) -> Any:
    """What a check counts for a product, with its own requests (``mark``)."""
    if kind == "any":
        return [_check_read(facts, line, str(s.get("check")), s) for s in spec.get("of", ())]
    own = _own(facts, line, _executors(kind, spec))
    if kind in ("pin", "post", "blog", "live"):
        return [_have(facts, line, kind), own]
    if kind == "critic":
        return [_newest_check(facts, line), own]
    if kind == "demand":
        return _demand_notes(facts, line)
    if kind in ("built", "kdp_check"):
        tools = ("propose_kdp_book",) if kind == "kdp_check" else tuple(str(t) for t in spec.get("tools", ()))
        return _made(facts, line, tools)
    return own


def _venture_read(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any]) -> Any:
    """A venture step's venture's records (``mark``): its stage, its evidence (the numbers research found, graded by
    Ember's code), business cases and the critic's answers; not a note or a rescore alone (in a simulated week of dry
    run, the fake model's repeated research saved a note and new scores every cycle: 183 of 220 cycles). A brainstorm's,
    the ideas."""
    venture = venture_of(conn, scope, int(row["id"]))
    if venture is None:
        where, params = scope.where()
        return list(conn.execute(f"SELECT COUNT(*), MAX(id) FROM ventures WHERE {where}", params).fetchone())
    v = conn.execute("SELECT stage FROM ventures WHERE id = ?", (venture,)).fetchone()
    return [
        v["stage"] if v is not None else None,
        *(
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE venture_id = ?", (venture,)).fetchone()[0]
            for table in ("evidence", "venture_cases", "venture_critiques")
        ),
    ]


def _own(
    facts: Facts, line: int | None, executors: tuple[str, ...] | None, since: str | None = None
) -> list[list[Any]]:
    """A product line's requests (None: every line's) by these executors (None: any), made since ``since``: [id,
    status], oldest first."""
    return [
        [r["id"], r["status"]]
        for r in facts.requests()
        if (line is None or r["line"] == line)
        and (executors is None or r["executor"] in executors)
        and (since is None or r["created_at"] >= since)
    ]


def _made(facts: Facts, line: int, tools: tuple[str, ...]) -> int:
    """The calls of these tools that worked in a product's cycles (a check-line file counts: it moved)."""
    if not tools:
        return 0
    marks = ", ".join("?" for _ in tools)
    return int(
        facts.conn.execute(
            "SELECT COUNT(*) FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id WHERE c.project_id = ?"
            f" AND c.session = ? AND c.simulated = ? AND t.tool IN ({marks}) AND t.status = 'ok'",
            (line, facts.scope.session, 1 if facts.scope.simulated else 0, *tools),
        ).fetchone()[0]
    )


def _demand_notes(facts: Facts, line: int) -> list[Any]:
    where, params = facts.scope.where()
    return list(
        facts.conn.execute(
            f"SELECT COUNT(*), MAX(id) FROM demand_notes WHERE {where} AND project_id = ?", (*params, line)
        ).fetchone()
    )


def _newest_check(facts: Facts, line: int) -> int | None:
    where, params = facts.scope.where()
    return facts.conn.execute(
        f"SELECT MAX(id) FROM quality_checks WHERE {where} AND project_id = ?", (*params, line)
    ).fetchone()[0]


def _have(facts: Facts, line: int, kind: str) -> int:
    """How many of what a counted check needs a product has: its live listings, or the pins, Bluesky posts and blog
    posts live that link one of them."""
    if kind == "live":
        return facts.live(line)
    funnel = facts.funnel(line)
    return 0 if funnel is None else {"pin": funnel.pins, "post": funnel.bluesky, "blog": funnel.posts}[kind]


def _executors(kind: str | None, spec: Mapping[str, Any]) -> tuple[str, ...]:
    """The executors of the requests a check counts (CHECK_EXECUTORS; a request check names its own)."""
    if kind == "any":
        return tuple(dict.fromkeys(e for s in spec.get("of", ()) for e in _executors(s.get("check"), s)))
    if kind == "request":
        return (str(spec["executor"]),) if spec.get("executor") else ()
    return CHECK_EXECUTORS.get(kind or "", ())


def _requested(facts: Facts, row: Mapping[str, Any], line: int) -> str | None:
    """What a step of Ember's waits on while its own requests are on their way and they are all its check still needs
    (two pins: live or asked for): 'owner' while one waits for the owner's decision, 'approved' while the approved
    ones are carried out (by Ember's code, or the owner's own hand). None while it needs more than is asked, and for a
    check a request made passes (Ember's code closes it)."""
    return _flying(facts, line, row["check_kind"], _spec(row))


def _flying(facts: Facts, line: int, kind: str | None, spec: Mapping[str, Any]) -> str | None:
    if kind == "any":
        return next((w for s in spec.get("of", ()) if (w := _flying(facts, line, s.get("check"), s))), None)
    if kind == "request" and spec.get("status") != "done":
        return None
    executors = _executors(kind, spec)
    mine = [
        r for r in facts.requests() if r["line"] == line and r["executor"] in executors and r["status"] in IN_FLIGHT
    ]
    if not mine:
        return None
    if kind in ("pin", "post", "blog", "live") and _have(facts, line, kind) + len(mine) < int(spec.get("count", 1)):
        return None  # more to ask for
    return "owner" if any(r["status"] == "pending" for r in mine) else "approved"


def _owners(facts: Facts, row: Mapping[str, Any], line: int | None) -> str:
    """What an owner's step waits on: the owner ('owner'), while one of the requests its check reads is on its way or
    it reads none (keep or drop a product, back a venture); else the step of Ember's before it ('step'): "You approve
    it, and it goes live" waits for a listing proposed again (live, it waited on the owner while no request did)."""
    executors = _executors(row["check_kind"], _spec(row))
    if not executors or line is None:
        return "owner"
    flying = any(
        r["line"] == line and r["executor"] in executors and r["status"] in IN_FLIGHT for r in facts.requests()
    )
    return "owner" if flying else "step"


def busy(conn: sqlite3.Connection, scope: AgentScope, steered: Steer, now: str, cycle_id: int | None = None) -> bool:
    """Whether a cycle's plan had work worth the owner's shortest sleep (slack.sleep): a step ready when it began that a
    cycle can advance. The cycle's own step (``cycle_id``: the cycle) counts only if something moved for it during the
    cycle (otherwise it waits now); another only if something moved since a cycle last took it, or none did (ready
    again the next day, a step that moved nothing gets its try without cutting a sleep). Live on 2026-10-09, a step no
    cycle could advance cut every sleep to 30 minutes until the daily cap was spent."""
    tried = {c.step.id for c in steered.found if c.tried}
    own = steered.step.id if steered.step is not None else None
    for step, _ in steered.pick.ranked:
        if step.id == own:
            row = node(conn, scope, step.id)
            if steered.mark is None or row is None or mark(Facts(conn, scope, now), row) != steered.mark:
                return True
            if cycle_id is not None and _made_in(conn, cycle_id, row):
                return True
        elif step.id not in tried:
            return True
    return False


def _fields(step: weights.Step) -> dict[str, Any]:
    return {name: getattr(step, name) for name in weights.Step.__dataclass_fields__}


def _kind(row: Mapping[str, Any]) -> str:
    kind = str(row["kind"])
    return kind if kind in weights.KIND else "ship"


def _promise_urgency(row: Mapping[str, Any], today: date) -> float:
    if not row["due"]:
        return weights.PROMISE_FLOOR
    left = (date.fromisoformat(row["due"]) - today).days
    return weights.promise_urgency(max(left, 0) + 0.5, slips=1 if left < 0 else 0)


def _owed_urgency(row: Mapping[str, Any], today: date, lately: Mapping[int, int]) -> float:
    """0.37.0: a promise's or an owner's decision's urgency, as its day nears (weights.promise_urgency), until cycles
    took it PROMISE_TRIES times (a decision once) in OBLIGATION_HOURS without closing it: then none until those hours
    have passed, its worth alone (0.35.0 to 0.36.0 that many times it came first; 0.37.0 kept the floor, which still
    outweighed every product step, so a promise Ember couldn't keep yet took every cycle)."""
    tries = lately.get(int(row["id"]), 0)
    fresh = tries < PROMISE_TRIES if row["kind"] == "promise" else tries == 0
    return _promise_urgency(row, today) if fresh else 0.0


PASSING = ("mode", "turn")  # what a venture's step waits on for one cycle only (0.36.0: the Explore step's): it ages


def _clocks(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date, channels: Mapping[str, bool]) -> None:
    """A step's age runs from when it became ready; a step that waits starts again from nought once it is ready."""
    for candidate in candidates(conn, scope, now, today, channels, exploring=True):
        row = node(conn, scope, candidate.step.id)
        if row is None or candidate.waiting in PASSING:
            continue
        if candidate.waiting is None and row["ready_since"] is None:
            _update(conn, row["id"], now, ready_since=now)
        elif candidate.waiting is not None and row["ready_since"] is not None:
            _update(conn, row["id"], now, ready_since=None)


def history(conn: sqlite3.Connection, scope: AgentScope, before: int | None = None) -> list[int | None]:
    """The product lines of the latest cycles, newest first (for the streak)."""
    rows = conn.execute(
        "SELECT project_id FROM cycles WHERE session = ? AND simulated = ? AND (? IS NULL OR id < ?)"
        " ORDER BY id DESC LIMIT ?",
        (scope.session, 1 if scope.simulated else 0, before, before, HISTORY),
    ).fetchall()
    return [r["project_id"] for r in rows]


def choose(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    exploring: bool = False,
    before: int | None = None,
) -> tuple[weights.Pick, list[Candidate]]:
    found = candidates(conn, scope, now, today, channels, exploring, before)
    last, streak = weights.streak_of(history(conn, scope, before))
    return weights.choose([c.step for c in found], last, streak), found


def settings() -> dict[str, Any]:
    """0.35.2: the numbers a cycle's step is chosen with, for the diagnostics: weights.py's, and this module's that
    feed them (a channel's factor, the cycles read for the streak, how often a promise or a decision comes first)."""
    return {
        "weights.py": weights.settings(),
        "plan.py": {
            "FRESH_PINS": FRESH_PINS,
            "FRESH_POSTS": FRESH_POSTS,
            "RESULT_DAYS": RESULT_DAYS,  # 0.35.3
            "CLICKS_PER_PIN": CLICKS_PER_PIN,
            "REACTIONS_PER_POST": REACTIONS_PER_POST,
            "HISTORY": HISTORY,
            "OBLIGATION_HOURS": OBLIGATION_HOURS,
            "PROMISE_TRIES": PROMISE_TRIES,
        },
    }


@dataclass(frozen=True)
class Steer:
    """0.35.0: what the tree decided for a cycle: its step (None when nothing is ready), what the cycle is ('ordinary',
    'marketing' or 'venture', 0.37.0: for a step of the ventures: lines.py's kinds), every candidate it was chosen
    from, and (0.37.0) the venture a venture step is about (None: a brainstorm, or another kind of cycle). 0.37.6: what
    the step's check read when the cycle took it (``mark``) and the owner's day (``day``), kept with the pick."""

    pick: weights.Pick
    found: tuple[Candidate, ...]
    kind: str
    venture: int | None = None
    mark: str | None = None
    day: str | None = None

    @property
    def step(self) -> weights.Step | None:
        return self.pick.step

    @property
    def line(self) -> int | None:
        return self.pick.step.product if self.pick.step is not None else None


def steer(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    *,
    exploring: bool = False,
    cycle_id: int | None = None,
) -> Steer:
    """The step a cycle takes (weights.choose: a pin, else the heaviest step; 0.37.0: a promise or decision of the
    owner's weighed like any) and so what the cycle is: a venture cycle for a step of the ventures (0.37.0: aimed at
    its venture; 0.36.0: the Explore step, in place of the ventures' turn; ``exploring``: whether this cycle may
    explore), a marketing cycle for a marketing step, else an ordinary one (also when no step is ready: it may start
    a new product, or end)."""
    pick, found = choose(conn, scope, now, today, channels, exploring=exploring, before=cycle_id)
    row = node(conn, scope, pick.step.id) if pick.step is not None else None
    taken = {"mark": mark(Facts(conn, scope, now), row) if row is not None else None, "day": today.isoformat()}
    if row is not None and row["template"] in VENTURE_TEMPLATES:
        return Steer(pick, tuple(found), "venture", venture_of(conn, scope, int(row["id"])), **taken)
    if pick.step is not None and (pick.step.kind == "market" or _markets(row)):
        return Steer(pick, tuple(found), "marketing", **taken)
    return Steer(pick, tuple(found), "ordinary", **taken)


def on_channel(conn: sqlite3.Connection, scope: AgentScope, step: weights.Step | None) -> bool:
    """0.35.0: whether a step is one of a channel's own product (its setup, in an ordinary cycle that keeps the
    channel's section and tools)."""
    if step is None or step.product is None:
        return False
    found = nodes(conn, scope, "level = 'product' AND project_id = ?", (step.product,))
    return bool(found) and templates.by_key(found[0]["template"]).name == "channel"


def _markets(row: Mapping[str, Any] | None) -> bool:
    """Whether a step's check needs the marketing tools (only a marketing cycle carries them): a pin, a post or a blog
    post, or a request a marketing cycle makes (obligations.MARKETING_EXECUTORS: a website product's post)."""
    if row is None:
        return False
    if row["check_kind"] in ("pin", "post", "blog"):
        return True
    if row["kind"] == "promise" and row["channel"]:  # 0.35.1: a promise of pins, a post or a blog post
        return True
    return row["check_kind"] == "request" and _spec(row).get("executor") in obligations.MARKETING_EXECUTORS


def record(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str, steered: Steer) -> None:
    """A cycle's pick, with its weight's parts and the ranking it came from, so a week can be re-scored offline; 0.37.6:
    and what its step's check read then, on the owner's day (the brake: ``candidates``)."""
    logged = steered.pick.json()
    conn.execute(
        "INSERT INTO plan_picks (mode, session, cycle_id, created_at, kind, line, node_id, product, decided, weight,"
        " parts, ranked, mark, day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            cycle_id,
            now,
            steered.kind,
            steered.line,
            logged["step"],
            logged["product"],
            steered.pick.decided,
            logged["weight"],
            json.dumps(logged["parts"]) if logged["parts"] else None,
            json.dumps(logged["ranked"], ensure_ascii=False),
            steered.mark if steered.step is not None else None,
            steered.day,
        ),
    )
    step = steered.step
    if step is not None and step.pinned and steered.kind == "venture":
        _update(conn, step.id, now, pinned=0)  # 0.36.0: the owner's Explore next is one venture cycle (it never closes)


# --- what the plan sees of it ---

TAKEN = {  # 0.37.0: a promise or an owner's decision is weighed like any step (until 0.36.0 it came first)
    "pin": "your owner pinned it",
    "weight": "the heaviest step that is ready",
    "margin": f"you are on its product, and no other step is {round((weights.MARGIN - 1) * 100)}% heavier",
}
WAITS = {
    "owner": "your owner",
    "approved": "an approved request being carried out",  # 0.37.6
    "channel": "a channel that isn't set up",
    "upgrade": "an upgrade",
    "date": "a date",
    "step": "another step",
    "hold": "a hold",
    "tried": "a change since a cycle took it today",  # 0.37.6: the brake
}
QUESTIONS = "This week's questions (keep them in mind; not steps to take):"
NEW_PRODUCT = "Start a new product (project_create): Ember's code lays it out with its stages and steps."
TRIED = (  # 0.37.6: no step ready, as the brake holds steps a cycle took today without anything moving
    "Nothing new starts while steps of yours wait for a change or tomorrow (a cycle took them today and nothing they"
    " are checked by moved): deal with what is owed, then end the cycle."
)


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


def step_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    steered: Steer,
    *,
    explore: bool,
    questions: list[str] | None = None,
    cash_eur: float = 0.0,
    net_days: float | None = None,
    forecasts: str = "",
    today: date | None = None,
) -> str:
    """YOUR STEP, as an ordinary or marketing plan sees it: the step Ember's code took, what done means and why it was
    taken, what comes after it in its stage, and the next heaviest steps; with no step ready, what waits and whether a
    new product may start (the explore burn mode). Its most important lines come first: the context cuts the end.
    0.37.0: a venture plan's too (in place of READY): its venture's decision and what it needs now (desk.item, with
    the cash a venture may need, ``cash_eur``, the net runway, ``net_days``, and the day, ``today``), and the record of
    the agent's forecasts (``forecasts``)."""
    if steered.kind == "venture" and steered.step is not None:
        return _venture_step_text(conn, scope, steered, cash_eur, net_days, forecasts, today)
    lines: list[str] = []
    step = steered.step
    if step is not None:
        row = node(conn, scope, step.id)
        product = nodes(conn, scope, "level = 'product' AND project_id = ?", (step.product,)) if step.product else []
        where = ""
        if product:
            kind = templates.by_key(product[0]["template"]).title
            where = f" of product line #{step.product} {_quoted(product[0]['title'])} ({kind})"
        stage = f", stage {row['stage']}" if row is not None and row["stage"] else ""
        lines.append(f"Step #{step.id}{where}{stage}: {_cut(step.title, 160)}")
        if row is not None and row["kind"] == "promise":  # 0.35.1: the promise itself, with where its product stands
            lines.append("Done when: you kept it and closed it with obligation_done.")
            stages = _stages(conn, scope, int(product[0]["id"])) if product else []
            ahead = [
                s
                for part in stages
                if part["status"] == "open"
                for s in _steps(conn, scope, part["id"])
                if s["status"] == "open" and s["kind"] != "promise"
            ]
            if ahead:
                lines.append(
                    "Its product's open steps: "
                    + " · ".join(f"#{s['id']} {s['title']} ({s['stage']})" for s in ahead[:4])
                )
        elif row is not None and row["check_kind"]:
            done = (
                "you say it is done (plan_step done)"
                if row["check_kind"] == "agent"
                else f"{check_words(row['check_kind'], _spec(row))} (Ember's code checks it)"
            )
            lines.append(f"Done when: {done}.")
        if row is not None and row["check_kind"] == "critic" and step.product is not None:
            from . import quality  # here, not at the top: quality imports prompts, which imports tools, then this

            lines += [f"- {_cut(f, 260)}" for f in quality.fixes(conn, scope, step.product)[:3]]
        parts = steered.pick.parts
        if parts is not None:
            carried = node(conn, scope, parts.carried_from) if parts.carried_from else None
            lines.append(
                f"Why: {TAKEN.get(steered.pick.decided, steered.pick.decided)}. Weight "
                + parts.text(str(carried["title"]) if carried is not None else None)
            )
        if row is not None and row["parent_id"] is not None:
            after = [
                s
                for s in _steps(conn, scope, row["parent_id"])
                if s["status"] == "open" and s["id"] != step.id and s["seq"] >= row["seq"] and s["kind"] != "promise"
            ]
            if after:
                lines.append("Next in this stage: " + " · ".join(f"#{s['id']} {s['title']}" for s in after[:3]))
    else:
        waiting: dict[str, int] = {}
        for c in steered.found:
            if c.waiting is not None and c.stage != "venture":  # YOUR PLAN's Ventures line says what they wait on
                waiting[c.waiting] = waiting.get(c.waiting, 0) + 1
        said = ", ".join(f"{n} on {WAITS.get(why, why)}" for why, n in sorted(waiting.items(), key=lambda w: -w[1]))
        lines.append(f"No step of your plan is ready{f' ({said})' if said else ''}.")
        held = new_things_held(conn, scope) if explore else None
        if held:  # 0.36.0: the owner's "nothing new", which project_create keeps too
            lines.append(
                f"Nothing new starts: your owner holds new things ({_cut(held, 80)}): deal with what is owed, then end"
                " the cycle."
            )
        elif explore and any(c.waiting == "tried" for c in steered.found):
            lines.append(TRIED)  # 0.37.6: a step that waits for tomorrow is still the plan's work, not room for new
        else:
            lines.append(
                NEW_PRODUCT
                if explore
                else "Nothing new starts in this burn mode: deal with what is owed, then end the cycle."
            )
    others = [(s, p) for s, p in steered.pick.ranked if step is None or s.id != step.id][:ALTERNATIVES]
    if others:
        places = _places(conn, scope)
        lines.append(
            "Next heaviest: "
            + " · ".join(f"#{s.id} {_cut(s.title, 60)} ({_line_of(s, places)}, {p.total:.1f})" for s, p in others)
        )
    if questions:
        lines.append(QUESTIONS)
        lines += [f"- {_cut(q, 200)}" for q in questions]
    return "\n".join(lines)


VENTURE_DONE = {  # 0.37.0: when a venture's step is done (Ember's code reads the venture's stage and records)
    "triage": "you researched it (stage researching) or parked it with why (venture_update).",
    "appraise": (
        "you saved what your research found (evidence, venture_update learned) and rescored it; once its research is"
        " done, its business case (venture_case, stage proposed) or parked with why. 0.37.6: a cycle that saves no"
        " evidence, case or stage leaves the step until tomorrow (a note or a rescore alone moves nothing)."
    ),
    "answer": "you answered the critic with evidence or new numbers (venture_case), or parked it with why.",
    "brainstorm": "your brainstorm (the brainstorm tool) added its ideas to your tree.",
}


def _venture_step_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    steered: Steer,
    cash_eur: float,
    net_days: float | None,
    forecasts: str,
    today: date | None,
) -> str:
    """YOUR STEP for a venture cycle (0.37.0): the venture's decision Ember's code took, what it needs now, its next
    question, what done means, why it was taken, the record of the agent's forecasts and the next heaviest steps."""
    step = steered.step
    assert step is not None
    row = node(conn, scope, step.id)
    decision = DECISION_OF.get(str(row["template"]), "brainstorm") if row is not None else "brainstorm"
    v = ventures.get(conn, scope, steered.venture) if steered.venture is not None else None
    lines: list[str] = []
    if v is not None:
        lines.append(f"Step #{step.id} of venture #{v['id']} {_quoted(v['title'])} ({v['stage']}): {step.title}")
        everyone = ventures.all_ventures(conn, scope)
        room = sum(1 for x in everyone if x["stage"] in ventures.EXPLORED) < ventures.MAX_ACTIVE
        item = (
            desk.item(conn, v, today=today, cash_eur=cash_eur, net_days=net_days, room=room)
            if today is not None
            else None
        )
        if item is not None:
            lines.append(f"Now: {item.text}")
        if decision == "appraise" and v["next_question"]:
            lines.append(f"Its next question: {_cut(' '.join(str(v['next_question']).split()), 300)}")
    else:
        lines.append(f"Step #{step.id} of your ventures: {step.title}")
        due = desk.brainstorm_due(ventures.all_ventures(conn, scope))
        if due:
            lines.append(f"Now: {due}.")
    lines.append(f"Done when: {VENTURE_DONE.get(decision, VENTURE_DONE['brainstorm'])}")
    parts = steered.pick.parts
    if parts is not None:
        lines.append(f"Why: {TAKEN.get(steered.pick.decided, steered.pick.decided)}. Weight {parts.text(None)}")
    if forecasts:
        lines.append(f"Your forecasts, settled by Ember's code: {forecasts}.")
    others = [(s, p) for s, p in steered.pick.ranked if s.id != step.id][:ALTERNATIVES]
    if others:
        places = _places(conn, scope)
        lines.append(
            "Next heaviest: "
            + " · ".join(f"#{s.id} {_cut(s.title, 60)} ({_line_of(s, places)}, {p.total:.1f})" for s, p in others)
        )
    return "\n".join(lines)


def _line_of(step: weights.Step, places: Mapping[int, Place]) -> str:
    """Where a ranked step belongs: its product line, or (0.37.0) its venture, the ventures (a brainstorm) or the
    owner (a promise or decision of no product)."""
    if step.product is not None:
        return f"line #{step.product}"
    place = places.get(step.id)
    if place is not None and place.kind == "venture":
        return f"venture #{place.venture}"
    return "for your owner" if place is not None and place.kind == "owner" else "your ventures"


@dataclass(frozen=True)
class Place:
    """0.37.0: where a step of no product line belongs: a venture's (``venture``, its node's ``title``), the ventures'
    as a whole (a brainstorm) or the owner's (a promise or decision of no product)."""

    kind: str  # 'venture', 'ventures' or 'owner'
    venture: int | None = None
    title: str = ""


def _places(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, Place]:
    """Each step of no product line, open or closed, with where it belongs (Place)."""
    where, params = scope.where("s")
    found: dict[int, Place] = {}
    for r in conn.execute(
        "SELECT s.id, p.level, p.platform, p.venture_id, p.title FROM plan_nodes s JOIN plan_nodes p"
        f" ON p.id = s.parent_id WHERE {where} AND s.level = 'step' AND s.project_id IS NULL",
        params,
    ):
        if r["level"] == "venture":
            found[int(r["id"])] = Place("venture", int(r["venture_id"]), str(r["title"]))
        elif r["platform"] in ("ventures", "owner"):
            found[int(r["id"])] = Place(str(r["platform"]))
    return found


def plan_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    goal: str = "",
    milestones: list[str] | None = None,
    since: str | None = None,
    closed: list[str] | None = None,
) -> str:
    """The planner's YOUR PLAN (0.35.0, in place of the roadmap): the owner's goal and how far it got (``goal``:
    roadmap.root_line), what the owner did to the plan since the last cycle (``since``: its end), then each project's
    open products with their stage and numbers, what waits on the owner, and the milestones still open
    (``milestones``: a venture's first test, the owner's own) and those Ember's code closed since (``closed``). A
    product's steps are YOUR STEP's to show, and the owner's Plan tab shows the whole tree: this stays short however
    big the tree grows."""
    lines = [f"Today: {today:%A} {today.isoformat()}."]
    if goal:
        lines.append(goal)
    words = owner_words(conn, scope, since)
    if words:
        lines.append("Your owner since your last cycle: " + "; ".join(words) + ".")
    until = frozen(conn, scope, today)
    if until:  # 0.36.0: the owner's freeze, which Ember's code keeps (propose_etsy_edit refuses such a change)
        lines.append(f"Your owner froze the titles and tags of your listings until {until}: change neither.")
    head = len(lines)
    facts = Facts(conn, scope, now)
    for top, products in grouped(conn, scope, "status = 'open'"):
        lines.append(f"{top['title']}: " + " · ".join(_product_line(conn, scope, facts, p) for p in products))
    owners = [  # 0.37.6: those with something for them to decide (live, "You approve it" with no request waiting)
        f"#{s['id']} {_cut(s['title'], 50)} (line #{s['project_id']})"
        for s in nodes(conn, scope, "level = 'step' AND status = 'open' AND kind = ?", (templates.OWNER_KIND,))
        if (s["project_id"] is None or _product_open(conn, scope, int(s["project_id"])))
        and _owners(facts, s, s["project_id"]) == "owner"
    ]
    if owners:
        lines.append("Waiting on your owner: " + " · ".join(owners[:6]))
    if milestones:
        lines.append("Milestones still open:")
        lines += milestones
    if closed:
        lines.append("Since your last cycle, Ember's code closed from its records: " + "; ".join(closed) + ".")
    if len(lines) == head:
        lines.append("Your plan has no products yet: your first one starts it.")
    explore = _ventures_line(conn, scope)  # 0.36.0: the ventures, a step of the plan
    if explore:
        lines.append(explore)
    return "\n".join(lines)


def grouped(conn: sqlite3.Connection, scope: AgentScope, condition: str = "1") -> list[tuple[Any, list[Any]]]:
    """Each project with its products (``condition``: which), each under its type's project: a product re-typed
    from generic (``_retemplate``) stays under its first one in the records (a node's place is fixed)."""
    tops = nodes(conn, scope, "level = 'project'")
    homes = {str(t["platform"]): int(t["id"]) for t in tops}
    found: dict[int, list[Any]] = {int(t["id"]): [] for t in tops}
    for product in nodes(conn, scope, f"level = 'product' AND ({condition})"):
        found[homes.get(templates.by_key(product["template"]).platform, int(product["parent_id"]))].append(product)
    return [(t, found[int(t["id"])]) for t in tops if found[int(t["id"])]]


def _product_open(conn: sqlite3.Connection, scope: AgentScope, line: int) -> bool:
    found = nodes(conn, scope, "level = 'product' AND project_id = ?", (line,))
    return bool(found) and found[0]["status"] == "open"


def _product_line(conn: sqlite3.Connection, scope: AgentScope, facts: Facts, product: Mapping[str, Any]) -> str:
    """One product in PLAN: its line, title, stage with its steps done, its numbers once live, a promise due, a hold."""
    line = int(product["project_id"])
    stage = current_stage_name(conn, scope, line) or "-"
    funnel = facts.funnel(line)
    numbers = (
        f" · {funnel.views} views, {funnel.favorites} favorites, {funnel.orders} orders"
        if funnel is not None and funnel.listings
        else ""
    )
    promised = [
        f"promise #{s['obligation_id']} due {s['due']}"
        for s in nodes(
            conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND kind = 'promise'", (line,)
        )
    ]
    promise = f" · {', '.join(promised)}" if promised else ""
    held = f": {_cut(product['hold_reason'], 60)}" if product["hold_reason"] else ""
    own = conn.execute("SELECT id FROM milestones WHERE product_node = ?", (product["id"],)).fetchone()
    unlocked = policy.unlocked_text(policy.unlocked(conn, scope, int(own["id"])), True) if own is not None else ""
    unlocks = f" · {unlocked}" if unlocked else ""  # 0.16.3 (analysis bug 5): what your owner unlocked for it
    return f"#{line} {_cut(product['title'], 40)} ({stage}{held}{numbers}{promise}{unlocks})"


def focus_text(conn: sqlite3.Connection, scope: AgentScope, steered: Steer) -> str:
    """The brief's FOCUS on the cycle's step: what it is and what done means (the plan saw the rest)."""
    step = steered.step
    if step is None:
        return ""
    row = node(conn, scope, step.id)
    said = f"Your step this cycle: #{step.id} {_cut(step.title, 160)}"
    if row is not None and row["check_kind"]:
        done = (
            "say so with plan_step done"
            if row["check_kind"] == "agent"
            else f"{check_words(row['check_kind'], _spec(row))}, which Ember's code checks"
        )
        said += f"\nDone when: {done}."
    return said


def _quoted(text: Any) -> str:
    return json.dumps(_cut(str(text), 60), ensure_ascii=False)


def _cut(text: str, chars: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= chars else flat[: chars - 1].rstrip() + "…"


# --- the owner's word ---


class PlanError(ValueError):
    def __init__(self, field: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


def pin(conn: sqlite3.Connection, scope: AgentScope, node_id: int, pinned: bool, who: str | None, now: str) -> None:
    row = node(conn, scope, node_id)
    if row is None:
        raise PlanError("id", "no such step", 404)
    if row["level"] != "step" or row["kind"] in ("promise", templates.OWNER_KIND):
        raise PlanError("id", "only a step Ember takes can be pinned")
    if row["status"] != "open":
        raise PlanError("id", "this step is closed", 409)
    _update(conn, node_id, now, pinned=1 if pinned else 0)
    _word(conn, scope, node_id, "pin" if pinned else "unpin", None, who, now)


def set_worth(
    conn: sqlite3.Connection, scope: AgentScope, node_id: int, worth: float | None, who: str | None, now: str
) -> None:
    row = node(conn, scope, node_id)
    if row is None:
        raise PlanError("id", "no such product", 404)
    if row["level"] not in ("product", "venture") and not _is_ventures(row):  # 0.36.0: the ventures'; 0.37.0: each's
        raise PlanError("id", "a worth is a product's, a venture's, or all your ventures'")
    if row["status"] != "open":
        raise PlanError("id", "this product is closed", 409)
    if worth is not None and not weights.OWNER_WORTH_MIN <= worth <= weights.OWNER_WORTH_MAX:
        raise PlanError("worth", f"give a worth from {weights.OWNER_WORTH_MIN:g} to {weights.OWNER_WORTH_MAX:g}")
    _update(conn, node_id, now, owner_worth=worth)
    _word(conn, scope, node_id, "worth" if worth is not None else "clear_worth", worth, who, now)


def _word(
    conn: sqlite3.Connection,
    scope: AgentScope,
    node_id: int,
    action: str,
    value: float | None,
    who: str | None,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO plan_words (mode, session, node_id, action, value, signed, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, node_id, action, value, (who or None) and who[:60], now),
    )


# --- Ember's changes (0.35.0) ---

ACTIONS = ("add", "split", "replace", "done", "wait", "hold", "resume")
STEP_KINDS = ("create", "ship", "fix", "market", "chore", "report")  # the kinds of step Ember adds
WAIT_ON = ("owner", "upgrade", "step", "date")
MAX_NEW_STEPS = 4  # steps one change adds
TITLE_CHARS = 120
WAITS_A_DAY = 2  # steps Ember may say wait, a day (the owner sees each one)
WAIT_DAYS = 14  # a wait on a day, at most this far ahead
FIXED_SOURCES = ("owner", "promise", "code")  # what the owner, a promise or Ember's code put there stays


def _open_product(conn: sqlite3.Connection, scope: AgentScope, line: int) -> sqlite3.Row:
    found = nodes(conn, scope, "level = 'product' AND project_id = ?", (line,))
    if not found or found[0]["status"] != "open":
        raise PlanError("project_id", f"product line #{line} isn't an open product of your plan")
    return found[0]


def _line_step(conn: sqlite3.Connection, scope: AgentScope, step_id: int | None, line: int) -> sqlite3.Row:
    if step_id is None:
        raise PlanError("step_id", "name the step (step_id)")
    row = node(conn, scope, step_id)
    if row is None or row["level"] != "step":
        raise PlanError("step_id", f"there is no step #{step_id}", 404)
    if row["project_id"] != line:
        raise PlanError(
            "step_id", f"step #{step_id} is product line #{row['project_id']}'s: this cycle works on line #{line}"
        )
    if row["status"] != "open":
        raise PlanError("step_id", f"step #{step_id} is {row['status']}, which is final", 409)
    return row


def _changeable(row: Mapping[str, Any], what: str) -> None:
    """Whether Ember may split or replace a step: not what the owner, a promise or Ember's code put there (an owner's
    step, a promise, a decision, a recurring duty), nor a step the owner pinned."""
    if row["obligation_id"] is not None or row["kind"] in ("promise", templates.OWNER_KIND):
        raise PlanError(
            "step_id", f"step #{row['id']} is your owner's (a promise, a decision or theirs to take): no {what}"
        )
    if row["source"] in FIXED_SOURCES:
        raise PlanError("step_id", f"step #{row['id']} was put there by your owner or Ember's code: no {what}")
    if row["pinned"]:
        raise PlanError("step_id", f"your owner pinned step #{row['id']}: take it as it is")


def _make_room(conn: sqlite3.Connection, parent_id: int, seq: int, count: int, now: str) -> None:
    """Moves the steps from ``seq`` on along by ``count``, so new ones fit before them."""
    conn.execute(
        "UPDATE plan_nodes SET seq = seq + ?, updated_at = ? WHERE parent_id = ? AND seq >= ?",
        (count, now, parent_id, seq),
    )


def _new_steps(
    conn: sqlite3.Connection,
    scope: AgentScope,
    stage: Mapping[str, Any],
    seq: int,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
) -> list[int]:
    made = []
    for offset, step in enumerate(steps):
        kind = step["kind"] if step["kind"] in STEP_KINDS else "create"
        made.append(
            _insert(
                conn,
                scope,
                now,
                parent_id=stage["id"],
                level="step",
                project_id=stage["project_id"],
                stage=stage["stage"],
                kind=kind,
                title=_cut(step["title"], TITLE_CHARS),
                check_kind="agent",
                seq=seq + offset,
                source="agent",
            )
        )
        change(conn, scope, made[-1], cycle_id, "agent", "add", why, now)
    return made


def _stage_of(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any]) -> sqlite3.Row:
    stage = node(conn, scope, row["parent_id"]) if row["parent_id"] else None
    if stage is None or stage["level"] != "stage" or stage["status"] != "open":
        raise PlanError("step_id", f"step #{row['id']}'s stage is closed: add to an open one")
    return stage


def _ids(found: list[int]) -> str:
    return ", ".join(f"#{n}" for n in found)


def add_steps(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
    *,
    stage: str | None = None,
    before: int | None = None,
) -> str:
    """New steps of Ember's: before a step, or at the end of a stage (the current one unless she names one). She
    says when they are done (check 'agent')."""
    product = _open_product(conn, scope, line)
    if before is not None:
        anchor = _line_step(conn, scope, before, line)
        parent = _stage_of(conn, scope, anchor)
        seq = int(anchor["seq"])
        _make_room(conn, parent["id"], seq, len(steps), now)
    else:
        found = (
            next(
                (s for s in _stages(conn, scope, product["id"]) if s["stage"] == stage and not _replaced(s, product)),
                None,
            )
            if stage
            else current_stage(conn, scope, product["id"])
        )
        if found is None or found["status"] != "open":
            raise PlanError("stage", f"product line #{line} has no open {stage or 'current'} stage")
        parent = found
        kept = [int(s["seq"]) for s in _steps(conn, scope, parent["id"]) if s["obligation_id"] is None]
        seq = max(kept, default=-1) + 1
    made = _new_steps(conn, scope, parent, seq, steps, why, cycle_id, now)
    return f"Added {_ids(made)} to the {parent['stage']} stage of line #{line}."


def split_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """A step as smaller ones in its place. One Ember says is done (check 'agent') gives way to its parts; one Ember's
    code checks stays after them, and closes when its check passes."""
    row = _line_step(conn, scope, step_id, line)
    _changeable(row, "split")
    if len(steps) < 2:
        raise PlanError("steps", "a split needs at least 2 smaller steps (one new step: add or replace)")
    parent = _stage_of(conn, scope, row)
    seq = int(row["seq"])
    _make_room(conn, parent["id"], seq, len(steps), now)
    made = _new_steps(conn, scope, parent, seq, steps, why, cycle_id, now)
    change(conn, scope, row["id"], cycle_id, "agent", "split", f"into {_ids(made)}: {why}", now)
    if row["check_kind"] == "agent":
        _close(conn, row["id"], now, "dropped", f"split into {_ids(made)}: {why}", by="agent")
        return f"Step #{row['id']} is split into {_ids(made)}."
    return (
        f"Step #{row['id']} is split: {_ids(made)} come first, and #{row['id']} stays after them (Ember's code "
        "closes it when its check passes)."
    )


def replace_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    steps: list[Mapping[str, str]],
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """Other steps in a step's place. A step Ember's code checks can be replaced only where its stage has a check of
    its own (else its check is part of what makes the stage done: Ember can't loosen that)."""
    row = _line_step(conn, scope, step_id, line)
    _changeable(row, "replacing")
    parent = _stage_of(conn, scope, row)
    if row["check_kind"] not in (None, "agent") and not parent["check_kind"]:
        raise PlanError(
            "step_id",
            f"step #{row['id']}'s check is part of what makes its stage done: split it (plan_step split) instead",
        )
    seq = int(row["seq"])
    _make_room(conn, parent["id"], seq, len(steps), now)
    made = _new_steps(conn, scope, parent, seq, steps, why, cycle_id, now)
    _close(conn, row["id"], now, "dropped", f"replaced by {_ids(made)}: {why}", by="agent")
    change(conn, scope, row["id"], cycle_id, "agent", "replace", f"by {_ids(made)}: {why}", now)
    return f"Step #{row['id']} is replaced by {_ids(made)}."


def step_done(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    why: str,
    cycle_id: int | None,
    now: str,
) -> str:
    """A step Ember says is done: only one without a check Ember's code reads."""
    row = _line_step(conn, scope, step_id, line)
    if row["check_kind"] != "agent":
        words = check_words(row["check_kind"], _spec(row)) or "its check passes"
        raise PlanError("step_id", f"Ember's code checks step #{row['id']}: it closes once {words}")
    _close(conn, row["id"], now, "done", why, by="agent")
    change(conn, scope, row["id"], cycle_id, "agent", "done", why, now)
    return f"Step #{row['id']} is done."


def wait_step(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    step_id: int | None,
    on: str | None,
    ref: int | None,
    until: str | None,
    why: str,
    cycle_id: int | None,
    now: str,
    today: date,
) -> str:
    """A step that waits on a block Ember's code can check: a request to the owner still waiting, an upgrade request
    not released or declined, another open step, or a day at most WAIT_DAYS ahead (WAITS_A_DAY a day). Its age stops
    meanwhile, and the code takes it up again once the block is gone (``_lift_waits``)."""
    row = _line_step(conn, scope, step_id, line)
    if row["obligation_id"] is not None or row["kind"] == templates.OWNER_KIND:
        raise PlanError("step_id", f"step #{row['id']} is your owner's: what blocks it goes in your message to them")
    where, params = scope.where()
    waited = conn.execute(
        f"SELECT COUNT(*) FROM plan_changes WHERE {where} AND actor = 'agent' AND action = 'wait'"
        " AND substr(created_at, 1, 10) = ?",
        (*params, now[:10]),
    ).fetchone()[0]
    if waited >= WAITS_A_DAY:
        raise PlanError("action", f"{WAITS_A_DAY} steps were said to wait today already: do what you can of this one")
    blocked = ""
    if on == "owner":
        found = conn.execute(f"SELECT status FROM approvals WHERE id = ? AND {where}", (ref, *params)).fetchone()
        if found is None or found["status"] != "pending":
            raise PlanError("ref", f"request #{ref} isn't waiting for your owner")
        blocked = f"request #{ref}, until your owner decides it"
    elif on == "upgrade":
        found = conn.execute(f"SELECT status FROM upgrades WHERE id = ? AND {where}", (ref, *params)).fetchone()
        if found is None or found["status"] not in ("new", "accepted"):
            raise PlanError("ref", f"upgrade request #{ref} isn't open")
        blocked = f"upgrade request #{ref}, until your owner releases or declines it"
    elif on == "step":
        other = node(conn, scope, ref) if ref is not None else None
        if other is None or other["level"] != "step" or other["status"] != "open" or other["id"] == row["id"]:
            raise PlanError("ref", f"#{ref} isn't another open step")
        blocked = f"step #{ref}, until it is closed"
    elif on == "date":
        try:
            day = date.fromisoformat(until or "")
        except ValueError:
            raise PlanError("until", "give the day it waits for as YYYY-MM-DD") from None
        if not today < day <= today + timedelta(days=WAIT_DAYS):
            raise PlanError("until", f"a step waits for a day after today, at most {WAIT_DAYS} days ahead")
        blocked = f"{day.isoformat()}"
    else:
        raise PlanError("on", f"say what it waits on: {', '.join(WAIT_ON)}")
    _update(
        conn,
        row["id"],
        now,
        waiting=on,
        wait_ref=ref if on != "date" else None,
        wait_until=until if on == "date" else None,
        wait_why=_cut(why, 200),
        ready_since=None,
    )
    change(conn, scope, row["id"], cycle_id, "agent", "wait", f"on {blocked}: {why}", now)
    return f"Step #{row['id']} waits on {blocked} ({waited + 1} of {WAITS_A_DAY} waits today)."


def hold(conn: sqlite3.Connection, scope: AgentScope, line: int, why: str, cycle_id: int | None, now: str) -> str:
    """Ember halts a product to work elsewhere (never closes or drops it: the owner does): not while it has a promise
    open or a step the owner pinned. The owner's decisions on it still come first."""
    product = _open_product(conn, scope, line)
    if product["hold_reason"]:
        raise PlanError("action", f"product line #{line} is on hold already")
    pinned = nodes(conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND pinned = 1", (line,))
    promised = nodes(conn, scope, "project_id = ? AND level = 'step' AND status = 'open' AND kind = 'promise'", (line,))
    if pinned or promised:
        raise PlanError(
            "action", f"product line #{line} has {'a pinned step' if pinned else 'a promise'} open: no hold"
        )
    _update(conn, product["id"], now, hold_reason=_cut(why, 200), hold_by="agent")
    change(conn, scope, product["id"], cycle_id, "agent", "hold", why, now)
    return (
        f"Product line #{line} is on hold: Ember's code takes none of its steps until it is resumed (plan_step "
        "resume, or your owner). End this cycle's work on it."
    )


def resume(
    conn: sqlite3.Connection,
    scope: AgentScope,
    line: int,
    why: str,
    cycle_id: int | None,
    now: str,
    actor: str = "agent",
) -> str:
    product = _open_product(conn, scope, line)
    if not product["hold_reason"]:
        raise PlanError("project_id", f"product line #{line} isn't on hold")
    if actor == "agent" and product["hold_by"] == "owner":  # 0.36.0: the owner's hold is theirs to lift
        raise PlanError("project_id", f"your owner holds product line #{line}: only they resume it")
    _update(conn, product["id"], now, hold_reason=None, hold_by=None)
    change(conn, scope, product["id"], cycle_id, actor, "resume", why, now)
    return f"Product line #{line} is resumed: Ember's code weighs its steps again."


def _lift_waits(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date) -> list[str]:
    """A step Ember said waits is taken up again once its block is gone: the request decided, the upgrade request
    released or declined, the other step closed, the day come."""
    where, params = scope.where()
    said: list[str] = []
    for row in nodes(
        conn, scope, "level = 'step' AND status = 'open' AND (wait_ref IS NOT NULL OR wait_until IS NOT NULL)"
    ):
        gone = ""
        if row["waiting"] == "owner":
            found = conn.execute(f"SELECT status FROM approvals WHERE id = ? AND {where}", (row["wait_ref"], *params))
            status = (found.fetchone() or {"status": "gone"})["status"]
            gone = f"request #{row['wait_ref']} is {status}" if status != "pending" else ""
        elif row["waiting"] == "upgrade":
            found = conn.execute(f"SELECT status FROM upgrades WHERE id = ? AND {where}", (row["wait_ref"], *params))
            status = (found.fetchone() or {"status": "gone"})["status"]
            gone = f"upgrade request #{row['wait_ref']} is {status}" if status not in ("new", "accepted") else ""
        elif row["waiting"] == "step":
            other = node(conn, scope, row["wait_ref"])
            gone = f"step #{row['wait_ref']} is closed" if other is None or other["status"] != "open" else ""
        elif row["waiting"] == "date":
            gone = (
                f"{row['wait_until']} has come" if row["wait_until"] and row["wait_until"] <= today.isoformat() else ""
            )
        if gone:
            _update(conn, row["id"], now, waiting=None, wait_ref=None, wait_until=None, wait_why=None)
            change(conn, scope, row["id"], None, "code", "unwait", gone, now)
            said.append(f"Plan tree: step #{row['id']} is ready again ({gone}).")
    return said


# --- the owner's word on a product (0.35.0) ---


def _owner_product(conn: sqlite3.Connection, scope: AgentScope, node_id: int) -> sqlite3.Row:
    row = node(conn, scope, node_id)
    if row is None or row["level"] != "product":
        raise PlanError("id", "no such product", 404)
    if row["status"] != "open":
        raise PlanError("id", f"this product is {row['status']}", 409)
    return row


def end_product(
    conn: sqlite3.Connection, scope: AgentScope, node_id: int, done: bool, why: str, who: str | None, now: str
) -> int:
    """The owner closes a product: done (its line succeeded) or dropped (abandoned), with everything under it. Only
    the owner does (Ember holds a product at most). Returns its line."""
    product = _owner_product(conn, scope, node_id)
    line = int(product["project_id"])
    status = "succeeded" if done else "abandoned"
    words = " ".join(why.split())[:300] or ("you marked it done" if done else "you dropped it")
    conn.execute(
        "UPDATE projects SET status = ?, updated_at = ?, notes = substr(notes || ?, -2000) WHERE id = ?"
        " AND status IN ('idea', 'active', 'waiting')",
        (status, now, f"\n[your owner] {words}", line),
    )
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (line,)).fetchone()
    _close_product(conn, scope, product, project, now, by="owner")
    change(
        conn, scope, node_id, None, "owner", "close" if done else "drop", f"{(who or 'your owner')[:60]}: {words}", now
    )
    return line


def _is_ventures(row: Mapping[str, Any]) -> bool:
    return row["level"] == "project" and row["platform"] == "ventures"


def _owner_held(conn: sqlite3.Connection, scope: AgentScope, node_id: int) -> sqlite3.Row:
    """0.36.0: what the owner holds or resumes: a product, or the Ventures project (its Explore step)."""
    row = node(conn, scope, node_id)
    if row is not None and _is_ventures(row):
        return row
    return _owner_product(conn, scope, node_id)


def owner_resume(conn: sqlite3.Connection, scope: AgentScope, node_id: int, who: str | None, now: str) -> int | None:
    """The owner lifts a hold on a product (0.36.0: theirs or Ember's), or on the ventures. Returns its line (None:
    the ventures)."""
    held = _owner_held(conn, scope, node_id)
    why = f"{(who or 'your owner')[:60]} lifted it"
    if _is_ventures(held):
        if not held["hold_reason"]:
            raise PlanError("id", "your ventures aren't on hold")
        _update(conn, held["id"], now, hold_reason=None, hold_by=None)
        change(conn, scope, held["id"], None, "owner", "resume", why, now)
        return None
    resume(conn, scope, int(held["project_id"]), why, None, now, actor="owner")
    return int(held["project_id"])


def owner_hold(
    conn: sqlite3.Connection, scope: AgentScope, node_id: int, why: str, who: str | None, now: str
) -> int | None:
    """0.36.0: the owner holds a product (its steps wait until they resume it; Ember can't), theirs in place of a hold
    of Ember's, or new things on the Ventures ("nothing new": no venture cycle and no new product until they resume
    them). Returns its line (None: the ventures)."""
    held = _owner_held(conn, scope, node_id)
    if held["hold_by"] == "owner":
        raise PlanError("id", "you hold it already", 409)
    words = " ".join(why.split())[:200] or "your owner holds it"
    _update(conn, held["id"], now, hold_reason=words, hold_by="owner")
    change(conn, scope, held["id"], None, "owner", "hold", f"{(who or 'your owner')[:60]}: {words}", now)
    return None if _is_ventures(held) else int(held["project_id"])


# --- the owner's freezes (0.36.0) ---

FREEZE_DAYS = 60  # a freeze's last day is this many days ahead at most


def freeze(
    conn: sqlite3.Connection, scope: AgentScope, until: str | None, who: str | None, now: str, today: date
) -> str | None:
    """The owner freezes the titles and tags of the live listings until ``until`` (its last day), or lifts the freeze
    (None): live, "no title or tag edits before 10-20" was a sentence the plan and the critic never read. Returns the
    day it holds until, None once lifted."""
    if until is not None:
        try:
            day = date.fromisoformat(until)
        except ValueError:
            raise PlanError("until", "give the freeze's last day as YYYY-MM-DD") from None
        if not today <= day <= today + timedelta(days=FREEZE_DAYS):
            raise PlanError("until", f"give a last day from today to {FREEZE_DAYS} days ahead")
    where, params = scope.where()
    conn.execute(
        f"UPDATE plan_freezes SET lifted_at = ? WHERE {where} AND what = 'titles_tags' AND lifted_at IS NULL",
        (now, *params),
    )
    if until is not None:
        conn.execute(
            "INSERT INTO plan_freezes (mode, session, what, until, signed, created_at)"
            " VALUES (?, ?, 'titles_tags', ?, ?, ?)",
            (scope.mode, scope.session, until, who, now),
        )
    return until


def frozen(conn: sqlite3.Connection, scope: AgentScope, today: date) -> str | None:
    """The last day of the owner's freeze of the live listings' titles and tags while it holds, else None."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT until FROM plan_freezes WHERE {where} AND what = 'titles_tags' AND lifted_at IS NULL AND until >= ?"
        " ORDER BY id DESC LIMIT 1",
        (*params, today.isoformat()),
    ).fetchone()
    return str(row["until"]) if row else None


def owner_keep(conn: sqlite3.Connection, scope: AgentScope, node_id: int, why: str, who: str | None, now: str) -> int:
    """The owner keeps a product at its decide-by date: their step closes, the product goes on. Returns its line."""
    row = node(conn, scope, node_id)
    if row is None or row["level"] != "step" or row["template"] != "decide/owner":
        raise PlanError("id", "no such decision of yours", 404)
    if row["status"] != "open":
        raise PlanError("id", "you decided it already", 409)
    words = " ".join(why.split())[:200] or "keep it"
    _close(conn, row["id"], now, "done", f"Your owner kept it: {words}", by="owner")
    change(conn, scope, row["id"], None, "owner", "close", f"{(who or 'your owner')[:60]} kept it: {words}", now)
    return int(row["project_id"])


CLOSED_WORDS = {"close": "closed", "drop": "dropped", "resume": "resumed"}


def owner_words(conn: sqlite3.Connection, scope: AgentScope, since: str | None) -> list[str]:
    """What the owner did to the plan since ``since`` (the last cycle's end): a step pinned, a worth set, a product
    closed, dropped or resumed, a decide-by date answered. YOUR PLAN says it right after the goal."""
    if since is None:
        return []
    where, params = scope.where("w")
    said = [
        {
            "pin": "pinned step",
            "unpin": "unpinned step",
            "worth": "set the worth of",
            "clear_worth": "cleared the worth of",
        }[r["action"]]
        + f" #{r['node_id']} {_quoted(r['title'])}"
        + (f" to {r['value']:g}" if r["action"] == "worth" else "")
        for r in conn.execute(
            f"SELECT w.*, n.title FROM plan_words w JOIN plan_nodes n ON n.id = w.node_id WHERE {where}"
            " AND w.created_at > ? ORDER BY w.id",
            (*params, since),
        )
    ]
    where, params = scope.where("c")
    said += [
        f"{CLOSED_WORDS.get(r['action'], r['action'])} #{r['node_id']} {_quoted(r['title'])}: {_cut(r['why'], 80)}"
        for r in conn.execute(
            f"SELECT c.*, n.title FROM plan_changes c JOIN plan_nodes n ON n.id = c.node_id WHERE {where}"
            " AND c.actor = 'owner' AND c.created_at > ? ORDER BY c.id",
            (*params, since),
        )
    ]
    return said


# --- the owner's Plan tab ---


def view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    now: str,
    today: date,
    channels: Mapping[str, bool],
    exploring: bool = False,
) -> dict[str, Any]:
    """The tree for the owner's Plan tab: every project, product, stage and step with its state and weight, the step
    the tree would take now and the next ones, what waits on the owner, the latest picks, and (0.35.0) what changed
    today, what each channel does and the upgrades the steps wait on. 0.36.0: and the ventures (0.37.0: each venture's
    step; ``exploring``: whether the burn mode runs venture cycles), each step of a venture naming it."""
    pick, found = choose(conn, scope, now, today, channels, exploring)
    by_id = {c.step.id: c for c in found}
    parts = weights.parts_by_id(pick.ranked)
    titles = {r["id"]: str(r["title"]) for r in nodes(conn, scope, "level = 'step' AND status = 'open'")}
    facts = Facts(conn, scope, now)
    projects: list[dict[str, Any]] = []
    for top, rows in grouped(conn, scope):
        products = [_product_view(conn, scope, q, by_id, parts, titles, facts, now) for q in rows]
        projects.append({"id": top["id"], "platform": top["platform"], "title": top["title"], "products": products})
    ranked = [(s, p) for s, p in pick.ranked]
    places = _places(conn, scope)
    waiting_owner = [_step_brief(c, None, titles, places) for c in found if c.waiting == "owner"]
    goal = roadmap.root(conn, scope)
    return {
        "today": today.isoformat(),
        "goal": {"title": goal["title"], "due": goal["due"]} if goal is not None else None,
        "projects": projects,
        "now": _pick_view(pick, titles, places),
        "next": [
            _step_brief(by_id[s.id], p, titles, places) for s, p in ranked if pick.step is None or s.id != pick.step.id
        ][:3],
        "waiting": waiting_owner,
        "picks": _picks(conn, scope, places),
        "changes": _changes_today(conn, scope, today),
        "channels": _channels_view(conn, scope, by_id, parts, now),
        "upgrades": _upgrades_view(conn, scope, by_id),
        "ventures": ventures_view(conn, scope, found, parts, today),  # 0.36.0
        "freeze": frozen(conn, scope, today),  # 0.36.0: the last day titles and tags stay as they are, or None
        "stamp": stamp(conn, scope),
    }


def _changes_today(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[dict[str, Any]]:
    """0.35.0: the changes to the tree today that weren't a check passing: Ember's (a step added, split, replaced,
    done or waiting, a product held or resumed), Ember's code's (an owner's decision as a step, a wait lifted, a
    decide-by date) and the owner's, the newest first."""
    where, params = scope.where("c")
    return [
        {
            "at": r["created_at"],
            "actor": r["actor"],
            "action": r["action"],
            "id": r["node_id"],
            "title": r["title"],
            "line": r["project_id"],
            "why": r["why"],
        }
        for r in conn.execute(
            f"SELECT c.*, n.title, n.project_id FROM plan_changes c JOIN plan_nodes n ON n.id = c.node_id WHERE {where}"
            " AND substr(c.created_at, 1, 10) = ? ORDER BY c.id DESC LIMIT 40",
            (*params, today.isoformat()),
        )
    ]


def _channels_view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    by_id: Mapping[int, Candidate],
    parts: Mapping[int, weights.Parts],
    now: str,
) -> list[dict[str, Any]]:
    """0.35.0: each channel as a mirror of the products' marketing: what it does next and what it did lately, with
    how well it works (clicks per pin, reactions per post: its factor in the weights)."""
    factors = _channel_factors(conn, scope, now)
    since = to_iso(from_iso(now) - timedelta(days=14))
    found = []
    for channel in CHANNELS:
        steps = []
        for row in nodes(
            conn, scope, "level = 'step' AND channel = ? AND (status = 'open' OR closed_at >= ?)", (channel, since)
        ):
            candidate = by_id.get(row["id"])
            weight = parts.get(row["id"])
            steps.append(
                {
                    "id": row["id"],
                    "line": row["project_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "due": row["due"],
                    "closed_at": row["closed_at"],
                    "waiting": candidate.waiting if candidate else None,
                    "weight": round(weight.total, 2) if weight else None,
                }
            )
        found.append({"channel": channel, "factor": factors.get(channel, 1.0), "steps": steps})
    return found


def _upgrades_view(conn: sqlite3.Connection, scope: AgentScope, by_id: Mapping[int, Candidate]) -> list[dict[str, Any]]:
    """0.35.0: the upgrades open, each with the steps that wait on it and what they would weigh once it is built, so
    the owner sees what is worth building next; steps that wait on an ability nobody asked for yet come last."""
    where, params = scope.where()
    found = []
    for u in conn.execute(
        f"SELECT id, title, status FROM upgrades WHERE {where} AND status IN ('new', 'accepted') ORDER BY id",
        params,
    ):
        steps = nodes(
            conn, scope, "level = 'step' AND status = 'open' AND waiting = 'upgrade' AND wait_ref = ?", (u["id"],)
        )
        found.append({"id": u["id"], "title": u["title"], "status": u["status"], "steps": _waiting_steps(steps, by_id)})
    unasked = nodes(conn, scope, "level = 'step' AND status = 'open' AND waiting = 'upgrade' AND wait_ref IS NULL")
    if unasked:
        found.append({"id": None, "title": None, "status": None, "steps": _waiting_steps(unasked, by_id)})
    return found


def _waiting_steps(rows: list[Any], by_id: Mapping[int, Candidate]) -> list[dict[str, Any]]:
    shown = []
    for row in rows:
        candidate = by_id.get(row["id"])
        weight = weights.weigh(candidate.step).total if candidate is not None else None
        shown.append(
            {"id": row["id"], "line": row["project_id"], "title": row["title"], "weight": weight and round(weight, 2)}
        )
    return sorted(shown, key=lambda s: -(s["weight"] or 0))


def _product_view(
    conn: sqlite3.Connection,
    scope: AgentScope,
    product: Mapping[str, Any],
    by_id: Mapping[int, Candidate],
    parts: Mapping[int, weights.Parts],
    titles: Mapping[int, str],
    facts: Facts,
    now: str,
) -> dict[str, Any]:
    pid = int(product["project_id"])
    project = conn.execute("SELECT title, status FROM projects WHERE id = ?", (pid,)).fetchone()
    current = current_stage(conn, scope, product["id"]) if product["status"] == "open" else None
    funnel = facts.funnel(pid) if product["status"] == "open" else None
    stage_name = str(current["stage"]) if current is not None else "maintain"
    computed = weights.worth(
        float(product["could_earn"] or 0),
        stage_name,
        funnel.views if funnel else 0,
        funnel.favorites if funnel else 0,
        funnel.orders if funnel else 0,
    )
    stages = []
    for stage in _stages(conn, scope, product["id"]):
        if _replaced(stage, product):
            continue
        steps = []
        for step in _steps(conn, scope, stage["id"]):
            candidate = by_id.get(step["id"])
            weight = parts.get(step["id"])
            steps.append(
                {
                    "id": step["id"],
                    "title": step["title"],
                    "kind": step["kind"],
                    "channel": step["channel"],
                    "status": step["status"],
                    "waiting": candidate.waiting if candidate else None,
                    "due": step["due"],
                    "source": step["source"],
                    "template": step["template"],  # 0.35.0: decide/owner is the owner's keep-or-drop
                    "pinned": bool(step["pinned"]),
                    "check": templates.CHECKS.get(step["check_kind"] or "", ""),
                    "result": step["result"],
                    "weight": round(weight.total, 2) if weight else None,
                    "why": weight.text(titles.get(weight.carried_from or 0)) if weight else None,
                    "age_days": round(_days(step["ready_since"], now), 1) if step["ready_since"] else None,
                    "stale": step["status"] == "open"
                    and bool(step["ready_since"])
                    and _days(step["ready_since"], now) >= STALE_DAYS,
                }
            )
        stages.append(
            {
                "id": stage["id"],
                "stage": stage["stage"],
                "title": stage["title"],
                "status": stage["status"],
                "current": current is not None and stage["id"] == current["id"],
                "steps": steps,
                "done": sum(1 for s in steps if s["status"] == "done"),
                "total": sum(1 for s in steps if s["status"] != "dropped"),
            }
        )
    own = conn.execute("SELECT id FROM milestones WHERE product_node = ?", (product["id"],)).fetchone()
    unlocks = (
        [
            {"rule": g["rule"], "level": g["level"], "per_day": g["per_day"], "budget": g["budget"]}
            for g in policy.unlocked(conn, scope, int(own["id"]))
        ]
        if own is not None
        else []
    )
    return {
        "id": product["id"],
        "line": pid,
        "title": project["title"] if project is not None else product["title"],
        "hold": product["hold_reason"],  # 0.35.0: Ember's hold, with her reason
        "hold_by": product["hold_by"],  # 0.36.0: 'owner' for the owner's own (Ember can't lift it)
        "live_since": product["live_since"],
        "decide_by": json.loads(product["decide_by"]) if product["decide_by"] else {},
        "pushed_until": product["pushed_until"],
        "milestone": int(own["id"]) if own is not None else None,  # its own milestone (the Autonomy box's)
        "unlocks": unlocks,
        "autonomy": _autonomy(conn, scope, int(own["id"]) if own is not None else None, now),
        "template": templates.by_key(product["template"]).title,
        "status": product["status"],
        "stage": stage_name if current is not None else None,
        "audience": product["audience"],
        "could_earn": product["could_earn"],
        "worth": product["owner_worth"] if product["owner_worth"] is not None else round(computed, 2),
        "worth_code": round(computed, 2),
        "owner_worth": product["owner_worth"],
        "chance": weights.chance(stage_name, *(_numbers(funnel))),
        "numbers": {
            "views": funnel.views if funnel else 0,
            "favorites": funnel.favorites if funnel else 0,
            "orders": funnel.orders if funnel else 0,
            "pins": funnel.pins if funnel else 0,
            "posts": funnel.bluesky if funnel else 0,
            "blog": funnel.posts if funnel else 0,
        },
        "stages": stages,
    }


def _autonomy(conn: sqlite3.Connection, scope: AgentScope, milestone_id: int | None, now: str) -> list[dict[str, Any]]:
    """0.35.0: a product's Autonomy box: each rule's unlock on its own milestone, or (before one is set, and so its
    milestone made) every rule manual, as a milestone of its line would show it."""
    if milestone_id is not None:
        return policy.view(conn, scope, _Day(now), milestone_id)
    return [
        {
            "rule": rule.name,
            "label": rule.label,
            "action_class": rule.action_class,
            "fits": rule.action_class != "email.reply",  # a milestone of a line: its listings, no email replies
            "levels": list(policy.levels(rule.name)),
            "level": "manual",
            "per_day": policy.PER_DAY,
            "budget": policy.BUDGET,
            "used": 0,
            "used_today": 0,
            "by": None,
            "why": None,
            "since": None,
        }
        for rule in policy.RULES.values()
    ]


def _numbers(funnel: reach.Funnel | None) -> tuple[int, int, int]:
    return (funnel.views, funnel.favorites, funnel.orders) if funnel else (0, 0, 0)


def _venture_named(places: Mapping[int, Place], step_id: int | None) -> dict[str, Any]:
    """0.37.0: a venture's step names its venture (its title says only the decision)."""
    place = places.get(step_id) if step_id is not None else None
    if place is None or place.kind != "venture":
        return {"venture": None, "venture_title": None}
    return {"venture": place.venture, "venture_title": place.title}


def _step_brief(
    candidate: Candidate, parts: weights.Parts | None, titles: Mapping[int, str], places: Mapping[int, Place]
) -> dict[str, Any]:
    return {
        "id": candidate.step.id,
        "line": candidate.step.product,
        "title": candidate.step.title,
        "stage": candidate.stage,
        "waiting": candidate.waiting,
        "weight": round(parts.total, 2) if parts else None,
        "why": parts.text(titles.get(parts.carried_from or 0)) if parts else None,
        **_venture_named(places, candidate.step.id),
    }


def _pick_view(pick: weights.Pick, titles: Mapping[int, str], places: Mapping[int, Place]) -> dict[str, Any] | None:
    if pick.step is None:
        return {
            "decided": pick.decided,
            "id": None,
            "title": None,
            "line": None,
            "weight": None,
            "why": None,
            **_venture_named(places, None),
        }
    return {
        "decided": pick.decided,
        "id": pick.step.id,
        "title": pick.step.title,
        "line": pick.step.product,
        "weight": round(pick.parts.total, 2) if pick.parts else None,
        "why": pick.parts.text(titles.get(pick.parts.carried_from or 0)) if pick.parts else None,
        **_venture_named(places, pick.step.id),
    }


def _picks(conn: sqlite3.Connection, scope: AgentScope, places: Mapping[int, Place]) -> list[dict[str, Any]]:
    where, params = scope.where("k")
    rows = conn.execute(
        "SELECT k.*, n.title AS step_title, p.title AS line_title, q.title AS product_title, c.project_id AS worked"
        " FROM plan_picks k LEFT JOIN plan_nodes n ON n.id = k.node_id LEFT JOIN projects p ON p.id = k.line"
        f" LEFT JOIN projects q ON q.id = k.product LEFT JOIN cycles c ON c.id = k.cycle_id WHERE {where}"
        " ORDER BY k.id DESC LIMIT ?",
        (*params, PICKS_SHOWN),
    ).fetchall()
    return [
        {
            "cycle": r["cycle_id"],
            "at": r["created_at"],
            "kind": r["kind"],
            "line": r["line"],
            "line_title": r["line_title"],
            "worked": r["worked"],
            "step": r["node_id"],
            "step_title": r["step_title"],
            "product": r["product"],
            "product_title": r["product_title"],
            "decided": r["decided"],
            "weight": r["weight"],
            "agrees": r["product"] is not None and r["product"] == (r["worked"] or r["line"]),
            **_venture_named(places, r["node_id"]),
        }
        for r in rows
    ]


def stamp(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """Changes whenever the tree, its words or its picks do (the tab reloads then)."""
    where, params = scope.where()
    tree = conn.execute(f"SELECT COUNT(*), MAX(updated_at) FROM plan_nodes WHERE {where}", params).fetchone()
    picks = conn.execute(f"SELECT MAX(id) FROM plan_picks WHERE {where}", params).fetchone()
    words = conn.execute(f"SELECT MAX(id) FROM plan_words WHERE {where}", params).fetchone()
    freezes = conn.execute(f"SELECT MAX(id), MAX(lifted_at) FROM plan_freezes WHERE {where}", params).fetchone()
    return f"{tree[0]}:{tree[1] or ''}:{picks[0] or 0}:{words[0] or 0}:{freezes[0] or 0}:{freezes[1] or ''}"


def channels_from(settings: Any) -> dict[str, bool]:
    """Which channels Ember can market in now, from the owner's options."""
    return {
        "pinterest": bool(getattr(settings, "pinterest_enabled", False)),
        "bluesky": bool(getattr(settings, "bluesky_enabled", False)),
        "blog": bool(getattr(settings, "blog_enabled", False)) and bool(getattr(settings, "site_enabled", False)),
    }
