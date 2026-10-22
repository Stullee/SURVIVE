"""0.42.2 (analysis-0.40.1, 5.6): laying a product out, split out of plan.py.

A product line becomes a product under its platform, laid out from its type's template (templates.py) with its stages
and steps the first time the keeper sees it (``lay_out``): its type and audience read from its words
(``infer_template``, ``infer_audience``), its worth from what it could earn (``could_earn``). A generic product takes
the type its line's records name once they do (``_retemplate``), one typed a KDP book by a check alone is generic
again (``_untype``), and one whose audience changed gets the channels' steps that reach it (``_reaudience``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from . import metrics, templates, ventures
from .plan_model import (
    _ENGLISH,
    _GERMAN,
    CHANNEL_AUDIENCES,
    STAGE_TITLES,
    _close,
    _insert,
    _replaced,
    _stages,
    _steps,
    _update,
    change,
)
from .store import OPEN_STATUSES, AgentScope

# --- laying a product out ---


KDP_WORDS = re.compile(r"\b(kdp|amazon|paperback|ebook|e-book)\b")  # a line's words that name a KDP book


def infer_template(
    conn: sqlite3.Connection, scope: AgentScope, project: Mapping[str, Any], records_only: bool = False
) -> templates.Template:
    """A product line's type, from its records first (its requests to the owner, its tools), then its words.
    ``records_only`` (0.35.0, a generic product re-typed): its requests and the tools of one type only. 0.40.2
    (analysis-0.40.1, 3.5): a propose_kdp_book check proposes nothing, and may be another line's book: re-typing reads
    the KDP requests only; and a line with an Etsy listing (made for it or moved to it) is no KDP book or Printify
    product by its tools. Live, line #12, the coach licence bundle's Etsy listing, became a KDP book on 10-19: a
    check of the Haushaltsbuch had run in one of its cycles."""
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
    if KDP_WORDS.search(text):
        return templates.KDP_BOOK
    used = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT t.tool FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id"
            " WHERE c.project_id = ? AND c.session = ? AND c.simulated = ? AND t.status = 'ok'",
            (pid, scope.session, 1 if scope.simulated else 0),
        )
    }
    if ventures.lists_in(conn, "etsy_listings", pid):  # 0.40.2: its listing says what it is
        return templates.ETSY_DIGITAL if not records_only or "propose_etsy_listing" in used else templates.GENERIC
    if "propose_kdp_book" in used and not records_only:
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
    No record holds a listing's language yet. 0.37.8: a German market or a lone "DE" names German buyers, a lone "EN"
    English ones: live, "Nebenkostenabrechnung Excel Template (DE landlord utility bill tool)" for "landlords in
    Germany" was taken for both, as its words were English, and got English Bluesky posts its buyers don't read."""
    text = f"{project['title']} {project['hypothesis']}"
    low = text.lower()
    if re.search(r"\b(en\s*[+/&]\s*de|de\s*[+/&]\s*en|bilingual|zweisprachig)\b", low):
        return "both"
    german, english = len(_GERMAN.findall(text)), len(_ENGLISH.findall(text))
    names_german = bool(re.search(r"\b(german|germany|deutsch|deutschland)\b", low) or re.search(r"\bDE\b", text))
    names_english = bool(re.search(r"\benglish\b", low) or re.search(r"\bEN\b", text))
    if names_german and not names_english:
        return "de"
    if names_english and not names_german:
        return "en"
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
    template its line's records name once they do (its first request to the owner, the tools its cycles used), laid
    out anew (``_relay``). A node's place is fixed, so the product stays under its first project: the owner's Plan tab
    shows it under its type's."""
    if templates.by_key(product["template"]).name != templates.GENERIC.name:
        return []
    template = infer_template(conn, scope, project, records_only=True)
    if template.name == templates.GENERIC.name:
        return []
    line = int(project["id"])
    _relay(conn, scope, product, line, template, f"Ember's code: its type is {template.title} now", now)
    why = f"its records show its type, {template.title}: that type's stages and steps"
    change(conn, scope, int(product["id"]), None, "code", "replace", why, now)
    return [
        f"Plan tree: line #{line}'s records show its type, {template.title}: it takes that type's stages and steps."
    ]


def _relay(
    conn: sqlite3.Connection,
    scope: AgentScope,
    product: Mapping[str, Any],
    line: int,
    template: templates.Template,
    moved: str,
    now: str,
) -> None:
    """A product laid out as ``template`` (0.35.0, from _retemplate; 0.40.2: _untype too): its open stages close, and
    the template's stages and steps take their place under the same product (a stage done before stays done): what
    Ember, the owner or Ember's code added there moves along (to the type's next stage when it has none of that name: a
    site post has no create stage), a promise's or a decision's step stays where it is (it is taken in any stage)."""
    audience = str(product["audience"] or "both")
    old = {str(s["stage"]): s for s in _stages(conn, scope, product["id"]) if s["status"] != "dropped"}
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


def _untype(
    conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], project: Mapping[str, Any], now: str
) -> list[str]:
    """0.40.2 (analysis-0.40.1, 3.5): a product re-typed a KDP book by a propose_kdp_book check alone (until 0.40.2,
    infer_template), whose line has an Etsy listing, no KDP request and no words naming a KDP book, is the generic
    product it was again (``_relay``): the KDP stages it hadn't done close with their steps of the template, and what
    Ember, the owner or Ember's code added there moves to its product's stages. Live, line #12 (the coach licence
    bundle's Etsy listing) became a KDP book on 10-19, with a blog step "naming the book", and its critic's fix moved
    under the KDP stages."""
    if templates.by_key(product["template"]).name != templates.KDP_BOOK.name:
        return []
    pid = int(project["id"])
    where, params = scope.where("a")
    asked = conn.execute(
        "SELECT 1 FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
        f" WHERE {where} AND COALESCE(a.project_id, y.project_id) = ? AND a.executor = 'kdp_package' LIMIT 1",
        (*params, pid),
    ).fetchone()
    words = f"{project['title']} {project['hypothesis']}".lower()
    if asked is not None or KDP_WORDS.search(words) or not ventures.lists_in(conn, "etsy_listings", pid):
        return []
    why = "Ember's code: its line is an Etsy listing's, not a KDP book's"
    _relay(conn, scope, product, pid, templates.GENERIC, why, now)
    change(conn, scope, int(product["id"]), None, "code", "replace", why, now)
    return [
        f"Plan tree: line #{pid} holds an Etsy listing: it is no KDP book. Its KDP stages are closed, and the steps"
        " added there move to its product's stages."
    ]


AUDIENCE_WORDS = {"en": "English", "de": "German", "both": "English and German"}


def _reaudience(
    conn: sqlite3.Connection, scope: AgentScope, product: Mapping[str, Any], project: Mapping[str, Any], now: str
) -> list[str]:
    """0.37.8: a product speaks to the audience its words name now (infer_audience): Ember's code's steps of a channel
    that doesn't reach it (an English Bluesky post for a German product) drop out, and the template's steps of the open
    stages for a channel that does reach it now come in. The agent's own steps, promises and steps done stay."""
    audience, before = infer_audience(project), str(product["audience"] or "both")
    if audience == before:
        return []
    _update(conn, product["id"], now, audience=audience)
    template = templates.by_key(product["template"])
    line = int(product["project_id"])
    why = f"Ember's code: its buyers read {AUDIENCE_WORDS[audience]}"
    dropped, added = [], []
    for stage in _stages(conn, scope, product["id"]):
        steps = _steps(conn, scope, stage["id"])
        for step in steps:
            fits = audience in CHANNEL_AUDIENCES.get(str(step["channel"]), (audience,))
            if step["status"] == "open" and step["source"] in ("template", "code") and step["channel"] and not fits:
                _close(conn, step["id"], now, "dropped", f"{why}: {step['channel']} doesn't reach them")
                dropped.append(int(step["id"]))
        stage_t = next((t for t in template.stages if t.stage == stage["stage"]), None)
        if stage["status"] != "open" or stage_t is None or _replaced(stage, product):
            continue
        have = {str(s["template"]) for s in steps if s["status"] != "dropped"}
        for number, step_t in enumerate(templates.steps_for(stage_t, audience)):
            if f"{template.key}/{stage_t.stage}/{step_t.key}" not in have:
                added.append(_template_step(conn, scope, now, stage["id"], line, template, stage_t, step_t, number))
    change(conn, scope, int(product["id"]), None, "code", "replace", f"{why} (it was {AUDIENCE_WORDS[before]})", now)
    said = f"Plan tree: line #{line}'s buyers read {AUDIENCE_WORDS[audience]}"
    said += f"; its steps {', '.join(f'#{n}' for n in dropped)} for other readers dropped" if dropped else ""
    said += f"; steps {', '.join(f'#{n}' for n in added)} for its channels added" if added else ""
    return [said + "."]
