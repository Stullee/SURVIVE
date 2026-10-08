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
(``_decisions``), taken first like a promise due, and a promise nothing in front of it carries is a step of its own.
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
from . import metrics, obligations, policy, reach, roadmap, templates, ventures, weights
from .store import CLOSED_STATUSES, OPEN_STATUSES, AgentScope

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
CLICKS_PER_PIN, REACTIONS_PER_POST = 0.5, 3.0  # results that make a channel worth 1.0
STALE_DAYS = 14  # a step waiting this long is shown for the daily review's look (Release 2c asks it)
PICKS_SHOWN = 12
HISTORY = 12  # cycles read back for the streak
# 0.35.0: an owner's decision (or a promise nothing carries) is taken first once in this many hours; the rest of the
# time it is weighed (live, an obligation nobody closed took every cycle's line until 0.28.0 limited it to once a day)
OBLIGATION_HOURS = 24
ALTERNATIVES = 3  # the other steps YOUR STEP names
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

    def funnel(self, project_id: int) -> reach.Funnel | None:
        if self._funnels is None:
            self._funnels = reach.funnels(self.conn, self.scope)
        return self._funnels.get(project_id)

    def verdict(self, project_id: int) -> str:
        from . import quality  # 0.35.0: here, not at the top: quality imports prompts, which imports tools, then this

        if project_id not in self._verdicts:
            self._verdicts[project_id] = quality.verdict(self.conn, self.scope, project_id)
        return self._verdicts[project_id]

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
    if kind == "request":
        where, params = scope.where("a")
        statuses = [
            r[0]
            for r in conn.execute(
                "SELECT a.status FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
                f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor = ?",
                (*params, project_id, spec.get("executor")),
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
        said += _recurring(facts, product, now, today, channels)
        said += _decide_by(facts, product, now, today)
    said += _lift_waits(conn, scope, now, today)
    _clocks(conn, scope, now, today, channels)
    return said


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
    held = " (on hold)" if found[0]["hold_reason"] else ""
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
    waiting: str | None  # why it can't be taken now ('owner', 'channel', 'upgrade', 'step', 'hold'), None: ready


def _waiting(row: Mapping[str, Any], channels: Mapping[str, bool]) -> str | None:
    if row["kind"] == templates.OWNER_KIND:
        return "owner"
    if row["waiting"]:
        return str(row["waiting"])
    if row["channel"] and not channels.get(row["channel"], False):
        return "channel"
    return None


def _channel_factors(conn: sqlite3.Connection, scope: AgentScope) -> dict[str, float]:
    """How well each channel works, per item (an untested channel: 1.0): clicks per live pin, reactions per post."""
    where, params = scope.where()
    factors = {c: 1.0 for c in CHANNELS}
    pins = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(clicks), 0) AS clicks FROM pinterest_pins WHERE {where}"
        " AND status = 'active'",
        params,
    ).fetchone()
    if pins["n"] >= FRESH_PINS:
        factors["pinterest"] = round(
            min(weights.CHANNEL_MAX, max(weights.CHANNEL_MIN, pins["clicks"] / pins["n"] / CLICKS_PER_PIN)), 2
        )
    posts = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(likes, 0) + COALESCE(reposts, 0) + COALESCE(replies, 0)"
        f" + COALESCE(quotes, 0)), 0) AS reactions FROM bluesky_posts WHERE {where} AND status = 'active'",
        params,
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
    conn: sqlite3.Connection, scope: AgentScope, now: str, today: date, channels: Mapping[str, bool]
) -> list[Candidate]:
    """Every open step of the open products, ready or waiting, weighed by weights.py's parts (the scorer leaves out
    the waiting ones). A promise step is taken through the steps in front of it, which carry it. 0.35.0: when none of
    its product's steps is ready, the promise is a step of its own, and so is an owner's decision (``_decisions``),
    in whatever stage it stands; either is taken first at most once every OBLIGATION_HOURS (weights.choose's promise
    rule) and weighed the rest of the time. The owner's word comes before Ember's hold: only their park or kill, or a
    closed line, stops these two."""
    facts = Facts(conn, scope, now)
    factors = _channel_factors(conn, scope)
    missed = _missed(conn, scope, today)
    worked = _worked(conn, scope)
    lately = _taken_since(conn, scope, to_iso(from_iso(now) - timedelta(hours=OBLIGATION_HOURS)))
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
        promises = [
            weights.Step(
                step["id"],
                pid,
                str(step["title"]),
                "ship",
                worth,
                urgency=_promise_urgency(step, today),
                promise_hours=_promise_hours(step, today),
            )
            for _, step in owed
            if step["kind"] == "promise"
        ]
        work = [(stage, step) for stage, step in rows if step["obligation_id"] is None]
        steps: dict[int, weights.Step] = {}
        reasons: dict[int, str | None] = {}
        first_taken = False
        for stage, step in work:
            sequential = stage["stage"] in SEQUENTIAL
            later = stage["id"] != current["id"] and not (
                stage["stage"] == "maintain" and (current["stage"] == "launch" or live)
            )
            reason: str | None
            if stopped or held:
                reason = "hold"
            elif later or (sequential and first_taken):
                reason = "step"
            else:
                reason = _waiting(step, channels)
            if sequential and stage["id"] == current["id"] and reason is None:
                first_taken = True
            urgency = 0.0
            if step["kind"] == "market" and pid in missed:
                urgency = weights.MISSED_BAR
            if step["check_kind"] == "critic" and verdict == "improve":
                urgency = max(urgency, weights.DEFECT)
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
            waiting = tuple(s for s in behind if s.product == pid) + tuple(promises)
            found.append(
                Candidate(
                    weights.Step(**{**_fields(steps[step["id"]]), "waiting": waiting}),
                    str(stage["stage"]),
                    reasons[step["id"]],
                )
            )
        carried = any(reason is None for reason in reasons.values())
        for stage, step in owed:
            if step["kind"] == "promise" and carried:
                continue  # the steps in front of it carry it
            reason = "hold" if stopped else _waiting(step, channels)
            since = max(filter(None, (step["ready_since"], worked.get(pid))), default=None)
            found.append(
                Candidate(
                    weights.Step(
                        step["id"],
                        pid,
                        str(step["title"]),
                        _kind(step),
                        worth,
                        urgency=_promise_urgency(step, today),
                        age_days=round(_days(since, now), 3) if reason is None else 0.0,
                        promise_hours=_promise_hours(step, today) if step["id"] not in lately else None,
                        blocked=reason is not None,
                    ),
                    str(stage["stage"]),
                    reason,
                )
            )
    return found


def _taken_since(conn: sqlite3.Connection, scope: AgentScope, since: str) -> set[int]:
    """The steps cycles took since ``since`` (0.35.0: an owner's decision or a promise's own step is taken first once
    in OBLIGATION_HOURS)."""
    where, params = scope.where()
    return {
        int(r[0])
        for r in conn.execute(
            f"SELECT node_id FROM plan_picks WHERE {where} AND node_id IS NOT NULL AND created_at >= ?",
            (*params, since),
        )
    }


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


def _promise_hours(row: Mapping[str, Any], today: date) -> float | None:
    if not row["due"]:
        return None
    return (date.fromisoformat(row["due"]) - today).days * 24 + 12.0  # by the end of its day


def _clocks(conn: sqlite3.Connection, scope: AgentScope, now: str, today: date, channels: Mapping[str, bool]) -> None:
    """A step's age runs from when it became ready; a step that waits starts again from nought once it is ready."""
    for candidate in candidates(conn, scope, now, today, channels):
        row = node(conn, scope, candidate.step.id)
        if row is None:
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
    venture_turn: bool = False,
    before: int | None = None,
) -> tuple[weights.Pick, list[Candidate]]:
    found = candidates(conn, scope, now, today, channels)
    last, streak = weights.streak_of(history(conn, scope, before))
    return weights.choose([c.step for c in found], last, streak, venture_turn), found


@dataclass(frozen=True)
class Steer:
    """0.35.0: what the tree decided for a cycle: its step (None on the ventures' turn, or when nothing is ready), what
    the cycle is ('ordinary', 'marketing' or 'venture': lines.py's kinds) and every candidate it was chosen from."""

    pick: weights.Pick
    found: tuple[Candidate, ...]
    kind: str

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
    venture_turn: bool = False,
    cycle_id: int | None = None,
) -> Steer:
    """The step a cycle takes (weights.choose: a pin, a promise or decision due, the ventures' turn, the heaviest step)
    and so what the cycle is: a venture cycle on the ventures' turn, a marketing cycle for a marketing step, else an
    ordinary one (also when no step is ready: it may start a new product, or end)."""
    pick, found = choose(conn, scope, now, today, channels, venture_turn=venture_turn, before=cycle_id)
    if pick.decided == "venture":
        kind = "venture"
    elif pick.step is not None and (pick.step.kind == "market" or _markets(node(conn, scope, pick.step.id))):
        kind = "marketing"
    else:
        kind = "ordinary"
    return Steer(pick, tuple(found), kind)


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
    return row["check_kind"] == "request" and _spec(row).get("executor") in obligations.MARKETING_EXECUTORS


def record(conn: sqlite3.Connection, scope: AgentScope, cycle_id: int, now: str, steered: Steer) -> None:
    """A cycle's pick, with its weight's parts and the ranking it came from, so a week can be re-scored offline."""
    logged = steered.pick.json()
    conn.execute(
        "INSERT INTO plan_picks (mode, session, cycle_id, created_at, kind, line, node_id, product, decided, weight,"
        " parts, ranked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )


# --- what the plan sees of it ---

TAKEN = {
    "pin": "your owner pinned it",
    "promise": "a promise to your owner, or their decision, is due",
    "weight": "the heaviest step that is ready",
    "margin": f"you are on its product, and no other step is {round((weights.MARGIN - 1) * 100)}% heavier",
}
WAITS = {
    "owner": "your owner",
    "channel": "a channel that isn't set up",
    "upgrade": "an upgrade",
    "date": "a date",
    "step": "another step",
    "hold": "a hold",
}
QUESTIONS = "This week's questions (keep them in mind; not steps to take):"
NEW_PRODUCT = "Start a new product (project_create): Ember's code lays it out with its stages and steps."


def check_words(kind: str | None, spec: Mapping[str, Any]) -> str:
    """What a check reads, in words (templates.CHECKS with its parameters filled in)."""
    text = templates.CHECKS.get(kind or "", "")
    if kind == "any":
        return " or ".join(check_words(s.get("check"), s) for s in spec.get("of", ())) or text
    for name, value in spec.items():
        shown = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        text = text.replace(f"`{name}`", shown)
    return re.sub(r"`(\w+)`", r"\1", text)


def step_text(
    conn: sqlite3.Connection,
    scope: AgentScope,
    steered: Steer,
    *,
    explore: bool,
    questions: list[str] | None = None,
) -> str:
    """YOUR STEP, as an ordinary or marketing plan sees it: the step Ember's code took, what done means and why it was
    taken, what comes after it in its stage, and the next heaviest steps; with no step ready, what waits and whether a
    new product may start (the explore burn mode). Its most important lines come first: the context cuts the end."""
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
        if row is not None and row["check_kind"]:
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
            if c.waiting is not None:
                waiting[c.waiting] = waiting.get(c.waiting, 0) + 1
        said = ", ".join(f"{n} on {WAITS.get(why, why)}" for why, n in sorted(waiting.items(), key=lambda w: -w[1]))
        lines.append(f"No step of your plan is ready{f' ({said})' if said else ''}.")
        lines.append(
            NEW_PRODUCT
            if explore
            else "Nothing new starts in this burn mode: deal with what is owed, then end the cycle."
        )
    others = [(s, p) for s, p in steered.pick.ranked if step is None or s.id != step.id][:ALTERNATIVES]
    if others:
        lines.append(
            "Next heaviest: "
            + " · ".join(f"#{s.id} {_cut(s.title, 60)} (line #{s.product}, {p.total:.1f})" for s, p in others)
        )
    if questions:
        lines.append(QUESTIONS)
        lines += [f"- {_cut(q, 200)}" for q in questions]
    return "\n".join(lines)


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
    facts = Facts(conn, scope, now)
    for top, products in grouped(conn, scope, "status = 'open'"):
        lines.append(f"{top['title']}: " + " · ".join(_product_line(conn, scope, facts, p) for p in products))
    owners = [
        f"#{s['id']} {_cut(s['title'], 50)} (line #{s['project_id']})"
        for s in nodes(conn, scope, "level = 'step' AND status = 'open' AND kind = ?", (templates.OWNER_KIND,))
        if s["project_id"] is None or _product_open(conn, scope, int(s["project_id"]))
    ]
    if owners:
        lines.append("Waiting on your owner: " + " · ".join(owners[:6]))
    if milestones:
        lines.append("Milestones still open:")
        lines += milestones
    if closed:
        lines.append("Since your last cycle, Ember's code closed from its records: " + "; ".join(closed) + ".")
    if len(lines) == 1 + bool(goal) + bool(words):
        lines.append("Your plan has no products yet: your first one starts it.")
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
    if row["level"] != "product":
        raise PlanError("id", "a worth is a product's")
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
    _update(conn, product["id"], now, hold_reason=_cut(why, 200))
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
    _update(conn, product["id"], now, hold_reason=None)
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


def owner_resume(conn: sqlite3.Connection, scope: AgentScope, node_id: int, who: str | None, now: str) -> int:
    """The owner lifts Ember's hold on a product. Returns its line."""
    product = _owner_product(conn, scope, node_id)
    resume(conn, scope, int(product["project_id"]), f"{(who or 'your owner')[:60]} lifted it", None, now, actor="owner")
    return int(product["project_id"])


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
    conn: sqlite3.Connection, scope: AgentScope, now: str, today: date, channels: Mapping[str, bool]
) -> dict[str, Any]:
    """The tree for the owner's Plan tab: every project, product, stage and step with its state and weight, the step
    the tree would take now and the next ones, what waits on the owner, the latest picks, and (0.35.0) what changed
    today, what each channel does and the upgrades the steps wait on."""
    pick, found = choose(conn, scope, now, today, channels)
    by_id = {c.step.id: c for c in found}
    parts = weights.parts_by_id(pick.ranked)
    titles = {r["id"]: str(r["title"]) for r in nodes(conn, scope, "level = 'step' AND status = 'open'")}
    facts = Facts(conn, scope, now)
    projects: list[dict[str, Any]] = []
    for top, rows in grouped(conn, scope):
        products = [_product_view(conn, scope, q, by_id, parts, titles, facts, now) for q in rows]
        projects.append({"id": top["id"], "platform": top["platform"], "title": top["title"], "products": products})
    ranked = [(s, p) for s, p in pick.ranked]
    waiting_owner = [_step_brief(c, None, titles) for c in found if c.waiting == "owner"]
    goal = roadmap.root(conn, scope)
    return {
        "today": today.isoformat(),
        "goal": {"title": goal["title"], "due": goal["due"]} if goal is not None else None,
        "projects": projects,
        "now": _pick_view(pick, titles),
        "next": [_step_brief(by_id[s.id], p, titles) for s, p in ranked if pick.step is None or s.id != pick.step.id][
            :3
        ],
        "waiting": waiting_owner,
        "picks": _picks(conn, scope),
        "changes": _changes_today(conn, scope, today),
        "channels": _channels_view(conn, scope, by_id, parts, now),
        "upgrades": _upgrades_view(conn, scope, by_id),
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
    factors = _channel_factors(conn, scope)
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


def _step_brief(candidate: Candidate, parts: weights.Parts | None, titles: Mapping[int, str]) -> dict[str, Any]:
    return {
        "id": candidate.step.id,
        "line": candidate.step.product,
        "title": candidate.step.title,
        "stage": candidate.stage,
        "waiting": candidate.waiting,
        "weight": round(parts.total, 2) if parts else None,
        "why": parts.text(titles.get(parts.carried_from or 0)) if parts else None,
    }


def _pick_view(pick: weights.Pick, titles: Mapping[int, str]) -> dict[str, Any] | None:
    if pick.step is None:
        return {"decided": pick.decided, "id": None, "title": None, "line": None, "weight": None, "why": None}
    return {
        "decided": pick.decided,
        "id": pick.step.id,
        "title": pick.step.title,
        "line": pick.step.product,
        "weight": round(pick.parts.total, 2) if pick.parts else None,
        "why": pick.parts.text(titles.get(pick.parts.carried_from or 0)) if pick.parts else None,
    }


def _picks(conn: sqlite3.Connection, scope: AgentScope) -> list[dict[str, Any]]:
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
        }
        for r in rows
    ]


def stamp(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """Changes whenever the tree, its words or its picks do (the tab reloads then)."""
    where, params = scope.where()
    tree = conn.execute(f"SELECT COUNT(*), MAX(updated_at) FROM plan_nodes WHERE {where}", params).fetchone()
    picks = conn.execute(f"SELECT MAX(id) FROM plan_picks WHERE {where}", params).fetchone()
    words = conn.execute(f"SELECT MAX(id) FROM plan_words WHERE {where}", params).fetchone()
    return f"{tree[0]}:{tree[1] or ''}:{picks[0] or 0}:{words[0] or 0}"


def channels_from(settings: Any) -> dict[str, bool]:
    """Which channels Ember can market in now, from the owner's options."""
    return {
        "pinterest": bool(getattr(settings, "pinterest_enabled", False)),
        "bluesky": bool(getattr(settings, "bluesky_enabled", False)),
        "blog": bool(getattr(settings, "blog_enabled", False)) and bool(getattr(settings, "site_enabled", False)),
    }
