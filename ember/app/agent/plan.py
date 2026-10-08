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

In 0.34.0 the tree only runs in the shadow: Ember's cycles go as before. ``shadow_pick`` records, every cycle, the step
the tree would have taken (weights.py) next to what READY took, and the owner's Plan tab shows the tree as a preview
(``view``), where they can pin a step and set a product's worth. Release 2b lets the tree steer.
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
from . import metrics, quality, reach, roadmap, templates, ventures, weights
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
MISSED_DAYS = 7  # a views bar missed this recently makes the product's marketing urgent
MISSED_GATES = ("day7_views", "day14_views", "retry_views")
FRESH_PINS, FRESH_POSTS = 4, 4  # a channel's results count once this many items are live
CLICKS_PER_PIN, REACTIONS_PER_POST = 0.5, 3.0  # results that make a channel worth 1.0
STALE_DAYS = 14  # a step waiting this long is shown for the daily review's look (Release 2c asks it)
PICKS_SHOWN = 12
HISTORY = 12  # cycles read back for the streak
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


def infer_template(conn: sqlite3.Connection, scope: AgentScope, project: Mapping[str, Any]) -> templates.Template:
    """A product line's type, from its records first (its requests to the owner, its tools), then its words."""
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
            _insert(
                conn,
                scope,
                now,
                parent_id=stage_id,
                level="step",
                project_id=project["id"],
                template=f"{template.key}/{stage.stage}/{step.key}",
                stage=stage.stage,
                kind=step.kind,
                channel=step.channel,
                title=step.title,
                check_kind=step.check,
                check_spec=json.dumps(step.spec),
                seq=number,
                source="template",
                effort=step.effort,
                waiting=step.waiting,
            )
    return product


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
            ended = "done" if project["status"] == "succeeded" else "dropped"
            for row in _descendants(conn, scope, product["id"]):
                _close(conn, row["id"], now, ended, f"its line closed: {project['status']}")
            _close(conn, product["id"], now, ended, f"its line closed: {project['status']}")
            said.append(f"Plan tree: product #{product['id']} closed with line #{project['id']}.")
    for product in nodes(conn, scope, "level = 'product' AND status = 'open'"):
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (product["project_id"],)).fetchone()
        if project is not None:
            template = templates.by_key(product["template"])
            earn = could_earn(conn, scope, project, template.could_earn)
            if earn != product["could_earn"]:
                _update(conn, product["id"], now, could_earn=earn)
        closed = _close_done(facts, product, now)
        said += closed if product["id"] not in laid else []  # a new product's stages already done: no news
        said += _promises(facts, product, now)
        said += _recurring(facts, product, now, today, channels)
    _clocks(conn, scope, now, today, channels)
    return said


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


def _steps(conn: sqlite3.Connection, scope: AgentScope, stage_id: int) -> list[Any]:
    return nodes(conn, scope, "parent_id = ? AND level = 'step'", (stage_id,))


def _close_done(facts: Facts, product: Mapping[str, Any], now: str) -> list[str]:
    conn, scope = facts.conn, facts.scope
    said: list[str] = []
    stages = _stages(conn, scope, product["id"])
    for stage in stages:
        if stage["status"] != "open":
            continue
        for step in _steps(conn, scope, stage["id"]):
            if step["status"] == "open" and _holds(facts, step):
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
                if step["kind"] != "promise":
                    _close(conn, step["id"], now, "done", f"Ember's code: {moot}")
            _close(conn, stage["id"], now, "done", "Ember's code: its check passed" if index == done_upto else moot)
            said.append(f"Plan tree: product #{product['id']}'s {stage['stage']} stage is done.")
    return said


def current_stage(conn: sqlite3.Connection, scope: AgentScope, product_id: int) -> Any | None:
    return next((s for s in _stages(conn, scope, product_id) if s["status"] == "open"), None)


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
    for step in nodes(
        conn,
        scope,
        "level = 'step' AND kind = 'promise' AND status = 'open' AND project_id = ?",
        (product["project_id"],),
    ):
        if _holds(facts, step):
            _close(conn, step["id"], now, "done", "Ember's code: the promise was kept")
    return said


def _audience_channels(product: Mapping[str, Any]) -> list[str]:
    audience = product["audience"] or "both"
    return [c for c in CHANNELS if audience in CHANNEL_AUDIENCES[c]]


def _recurring(
    facts: Facts, product: Mapping[str, Any], now: str, today: date, channels: Mapping[str, bool]
) -> list[str]:
    """A live product's recurring steps, once its launch is done: one open step per channel's duty (a missed one stays
    open and ages), and a fix whenever the critic says improve."""
    conn, scope = facts.conn, facts.scope
    stages = {s["stage"]: s for s in _stages(conn, scope, product["id"])}
    maintain, launch = stages.get("maintain"), stages.get("launch")
    if maintain is None or maintain["status"] != "open" or launch is None or launch["status"] == "open":
        return []
    said: list[str] = []
    title = str(product["title"])
    if templates.by_key(product["template"]).name != "kdp_book":  # a book can't be linked yet (Amazon links)
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
    """The products that missed a views bar of their listing test lately: their marketing is urgent (0.33.0: a miss
    owes a push for buyers, never title and tag edits)."""
    where, params = scope.where("g")
    since = (today - timedelta(days=MISSED_DAYS)).isoformat()
    marks = ", ".join("?" for _ in MISSED_GATES)
    return {
        int(r[0])
        for r in conn.execute(
            f"SELECT g.project_id FROM listing_gates g JOIN milestones m ON m.id = g.milestone_id WHERE {where}"
            f" AND g.gate IN ({marks}) AND m.status = 'missed' AND substr(m.closed_at, 1, 10) >= ?",
            (*params, *MISSED_GATES, since),
        )
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
    the waiting ones). A promise step is never taken itself: the steps in front of it carry it."""
    facts = Facts(conn, scope, now)
    factors = _channel_factors(conn, scope)
    missed = _missed(conn, scope, today)
    worked = _worked(conn, scope)
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
        rows = [
            (stage, step)
            for stage in _stages(conn, scope, product["id"])
            if stage["status"] == "open"
            for step in _steps(conn, scope, stage["id"])
            if step["status"] == "open"
        ]
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
            for _, step in rows
            if step["kind"] == "promise"
        ]
        work = [(stage, step) for stage, step in rows if step["kind"] != "promise"]
        steps: dict[int, weights.Step] = {}
        reasons: dict[int, str | None] = {}
        first_taken = False
        for stage, step in work:
            sequential = stage["stage"] in SEQUENTIAL
            later = stage["id"] != current["id"] and not (stage["stage"] == "maintain" and current["stage"] == "launch")
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
    return found


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


def shadow_pick(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int,
    now: str,
    today: date,
    kind: str,
    line: int | None,
    channels: Mapping[str, bool],
) -> weights.Pick:
    """The step the tree would take this cycle, recorded next to what READY took (the cycle's kind and line)."""
    pick, _ = choose(conn, scope, now, today, channels, venture_turn=kind == "venture", before=cycle_id)
    logged = pick.json()
    conn.execute(
        "INSERT INTO plan_picks (mode, session, cycle_id, created_at, kind, line, node_id, product, decided, weight,"
        " parts, ranked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.mode,
            scope.session,
            cycle_id,
            now,
            kind,
            line,
            logged["step"],
            logged["product"],
            pick.decided,
            logged["weight"],
            json.dumps(logged["parts"]) if logged["parts"] else None,
            json.dumps(logged["ranked"], ensure_ascii=False),
        ),
    )
    return pick


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


# --- the owner's Plan tab ---


def view(
    conn: sqlite3.Connection, scope: AgentScope, now: str, today: date, channels: Mapping[str, bool]
) -> dict[str, Any]:
    """The tree for the owner's Plan tab: every project, product, stage and step with its state and weight, the step
    the tree would take now and the next ones, what waits on the owner, and the latest shadow picks."""
    pick, found = choose(conn, scope, now, today, channels)
    by_id = {c.step.id: c for c in found}
    parts = weights.parts_by_id(pick.ranked)
    titles = {r["id"]: str(r["title"]) for r in nodes(conn, scope, "level = 'step' AND status = 'open'")}
    facts = Facts(conn, scope, now)
    projects: list[dict[str, Any]] = []
    for top in nodes(conn, scope, "level = 'project'"):
        products = []
        for product in nodes(conn, scope, "parent_id = ? AND level = 'product'", (top["id"],)):
            products.append(_product_view(conn, scope, product, by_id, parts, titles, facts, now))
        if products:
            projects.append({"id": top["id"], "platform": top["platform"], "title": top["title"], "products": products})
    ranked = [(s, p) for s, p in pick.ranked]
    waiting_owner = [_step_brief(c, None, titles) for c in found if c.waiting == "owner"]
    goal = roadmap.root(conn, scope)
    return {
        "preview": True,
        "today": today.isoformat(),
        "goal": {"title": goal["title"], "due": goal["due"]} if goal is not None else None,
        "projects": projects,
        "now": _pick_view(pick, titles),
        "next": [_step_brief(by_id[s.id], p, titles) for s, p in ranked if pick.step is None or s.id != pick.step.id][
            :3
        ],
        "waiting": waiting_owner,
        "picks": _picks(conn, scope),
        "stamp": stamp(conn, scope),
    }


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
                    "pinned": bool(step["pinned"]),
                    "check": templates.CHECKS.get(step["check_kind"] or "", ""),
                    "result": step["result"],
                    "weight": round(weight.total, 2) if weight else None,
                    "why": weight.text(titles.get(weight.carried_from or 0)) if weight else None,
                    "age_days": round(_days(step["ready_since"], now), 1) if step["ready_since"] else None,
                    "stale": bool(step["ready_since"]) and _days(step["ready_since"], now) >= STALE_DAYS,
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
    return {
        "id": product["id"],
        "line": pid,
        "title": project["title"] if project is not None else product["title"],
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
