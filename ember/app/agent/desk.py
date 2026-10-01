"""The decision desk (0.13.0): the ventures' next decisions, ranked by Ember's code.

A venture cycle's planner chose freely what to look at, so research went where the plan's mood took it, and the
brainstorm never ran live. Now Ember's code ranks what the ventures need decided next, and each venture cycle's plan
gets it as READY: at most MAX_ITEMS items, the most pressing first.

* build: a venture your owner backed that has no open project yet: plan its first test;
* appraise: a venture being researched, whose next need Ember's code names (research, evidence, numbers, a knock-out
  to fix; then propose or park it);
* answer: a proposed venture whose critic said test or park: answer its flaw (evidence, new numbers) or park it;
* triage: an idea: research it or park it, while fewer than ventures.MAX_ACTIVE are worked on;
* brainstorm: while fewer than FUNNEL ideas wait and fewer than FUNNEL ventures have their numbers (explore only).

Their order: build; then your owner's wishes (a venture they want researched next, an idea they added); ventures
close to being parked by their stage's rule (within URGENT_DAYS, or with little research budget left, the soonest
first); answers to the critic; the other appraisals; triage; a brainstorm last. Within each, the highest expected net
first (the critic's where it is lower), then the heaviest.

The plan takes one (``ready``: its key, "appraise #3") or says why it takes none; Ember's code checks the key, aims
the cycle at its venture and keeps each pick with the list it came from (``desk_picks``, never changed). The desk's
cap is the owner's venture share: it runs in venture cycles only, and in the focus burn mode only for backed ventures.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..economy import burn
from . import critic, knockouts, ventures
from .store import OPEN_STATUSES, AgentScope

MAX_ITEMS = 5
FUNNEL = 5  # ideas waiting, and ventures with numbers, below which a brainstorm is ready
URGENT_DAYS = 7  # a venture parked by its stage's rule within this many days is urgent
URGENT_BUDGET = 0.25  # ... and one with this share of its research budget left
WHY_CHARS = 200  # why a plan takes no READY item, as kept
ITEM_CHARS = 220  # an item's text: READY holds MAX_ITEMS of them in its budget
KINDS = ("build", "appraise", "answer", "triage", "brainstorm")
_TIER = {"build": 0, "wish": 1, "urgent": 2, "answer": 3, "appraise": 4, "triage": 5, "brainstorm": 6}
_KEY = re.compile(r"^\s*(build|appraise|answer|triage|brainstorm)\b\s*(?:#\s*)?(\d+)?", re.IGNORECASE)


def _line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(frozen=True)
class Item:
    kind: str
    venture_id: int | None
    text: str  # what it is and why now, with the numbers (at most ITEM_CHARS characters)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _line(self.text, ITEM_CHARS))

    @property
    def key(self) -> str:
        return self.kind if self.venture_id is None else f"{self.kind} #{self.venture_id}"

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "venture_id": self.venture_id, "text": self.text}


def _ev_text(case_row: Mapping[str, Any] | None, judged: Mapping[str, Any] | None) -> str:
    if case_row is None:
        return "no numbers yet"
    ours = f"EUR {float(case_row['ev_eur']):.0f} a month expected"
    return ours + (f" (the critic's: EUR {float(judged['ev_eur']):.0f})" if judged is not None else "")


def _park_date(v: Mapping[str, Any]) -> date | None:
    began = v["research_from"]
    return date.fromisoformat(str(began)[:10]) + timedelta(days=ventures.RESEARCH_DAYS) if began else None


def _need(conn: sqlite3.Connection, v: Mapping[str, Any], *, cash_eur: float, net_days: float | None) -> str:
    """What a venture being researched needs next, as Ember's code sees it."""
    gaps = ventures.proposal_gaps(v, "researching")
    if gaps:
        more = f" (and {len(gaps) - 1} more)" if len(gaps) > 1 else ""
        return f"needs {gaps[0]}{more}"
    standing = knockouts.active(knockouts.check(conn, v, cash_eur=cash_eur, net_days=net_days))
    if standing:
        return f"knocked out ({', '.join(k.label for k in standing)}): fix its case or park it"
    return "ready to propose (stage proposed), or park it with the numbers"


def ready(
    conn: sqlite3.Connection,
    scope: AgentScope,
    *,
    mode: str,
    today: date,
    cash_eur: float,
    net_days: float | None,
) -> list[Item]:
    """The READY list for a venture cycle in burn ``mode``: at most MAX_ITEMS, the most pressing first."""
    rows = ventures.all_ventures(conn, scope)
    room = sum(1 for v in rows if v["stage"] in ventures.ACTIVE_STAGES) < ventures.MAX_ACTIVE
    ranked: list[tuple[tuple[Any, ...], Item]] = []
    for v in rows:
        vid, stage, title = int(v["id"]), v["stage"], _line(v["title"], 50)
        if stage == "building":
            projects = ventures.projects_of(conn, vid)
            if not any(p["status"] in OPEN_STATUSES for p in projects):
                test = f" (milestone #{v['test_milestone_id']})" if v["test_milestone_id"] else ""
                text = f"{title} · backed by your owner: set up its first test{test}: no open project for it yet"
                ranked.append(((_TIER["build"], vid), Item("build", vid, text)))
            continue
        if mode != burn.EXPLORE or stage not in ventures.EXPLORING:
            continue  # focus: only the tests already running
        case_row = ventures.latest_case(conn, vid) if v["cases"] else None
        judged = critic.latest(conn, vid) if case_row is not None else None
        ev = critic.ranking_ev(case_row, judged)
        by_value = (0, -ev) if ev is not None else (1, 0.0)
        heavy = -(ventures.weight(v) or -1)
        wish = v["owner_action"] in ("research", "added")
        if stage == "researching":
            park = _park_date(v)
            left = ventures.research_left(v)
            short = left is not None and left <= ventures.RESEARCH_BUDGET_USD * 1_000_000 * URGENT_BUDGET
            urgent = short or (park is not None and (park - today).days <= URGENT_DAYS)
            deadline = [f"parked by Ember's code on {park.isoformat()}"] if park else []
            budget = ventures.budget_text(v)
            need = _need(conn, v, cash_eur=cash_eur, net_days=net_days)
            text = " · ".join([title, need, *deadline, _ev_text(case_row, judged), *([budget] if budget else [])])
            if wish:
                ranked.append(
                    ((_TIER["wish"], 0, *by_value, heavy, vid), Item("appraise", vid, f"your owner's wish: {text}"))
                )
            elif urgent:
                soon = park.toordinal() if park else today.toordinal()
                ranked.append(((_TIER["urgent"], soon, *by_value, heavy, vid), Item("appraise", vid, text)))
            else:
                ranked.append(((_TIER["appraise"], *by_value, heavy, vid), Item("appraise", vid, text)))
        elif stage == "proposed" and judged is not None and judged["verdict"] in ("test", "park"):
            flaw = _line(judged["fatal_flaw"], 120)
            text = (
                f"{title} · the critic says {judged['verdict']} (case #{judged['case_id']}): {flaw} · answer it with "
                f"evidence or new numbers, or park it · {_ev_text(case_row, judged)}"
            )
            ranked.append(((_TIER["answer"], *by_value, vid), Item("answer", vid, text)))
        elif stage == "idea":
            text = f"{title} · an idea, {ventures.scores_text(v)}: research it (stage researching) or park it with why"
            triage = ventures.triage_date(v)  # 0.13.0: an idea of the agent's no one takes up is parked then
            if triage is not None:
                text += f" · parked by Ember's code on {triage.isoformat()}"
            if wish:
                ranked.append(
                    ((_TIER["wish"], 1, 1, 0.0, heavy, vid), Item("triage", vid, f"your owner's idea: {text}"))
                )
            elif room and triage is not None and (triage - today).days <= URGENT_DAYS:
                ranked.append(((_TIER["urgent"], triage.toordinal(), 1, 0.0, heavy, vid), Item("triage", vid, text)))
            elif room:
                ranked.append(((_TIER["triage"], heavy, vid), Item("triage", vid, text)))
    if mode == burn.EXPLORE and len(rows) + ventures.BRAINSTORM_IDEAS <= ventures.MAX_VENTURES:
        waiting = sum(1 for v in rows if v["stage"] == "idea")
        cased = sum(1 for v in rows if v["stage"] in ventures.OPEN_STAGES and v["cases"])
        if waiting < FUNNEL and cased < FUNNEL:
            text = (
                f"{waiting} idea{'s' if waiting != 1 else ''} wait and {cased} venture{'s' if cased != 1 else ''} "
                "have their numbers: brainstorm new ones"
            )
            ranked.append(((_TIER["brainstorm"],), Item("brainstorm", None, text)))
    ranked.sort(key=lambda pair: pair[0])
    return [item for _, item in ranked[:MAX_ITEMS]]


def text(items: list[Item], record: str = "") -> str:
    """The plan's READY section ("" without items), with the record of the agent's forecasts (``record``,
    predictions.calibration: 0.13.0) for its triage."""
    if not items:
        return ""
    lines = [f"{n}. {item.key}: {item.text}" for n, item in enumerate(items, 1)]
    forecasts = f"\nYour forecasts, settled by Ember's code: {record}." if record else ""
    return "Ranked by Ember's code. Take one (ready: its key), or say why none:\n" + "\n".join(lines) + forecasts


def choose(items: list[Item], answer: str) -> tuple[Item | None, str]:
    """The plan's ``ready``: the item it takes, or None and why (its own words, or what was wrong with its answer)."""
    said = " ".join(str(answer or "").split())
    found = _KEY.match(said)
    if found:
        key = found[1].lower() + (f" #{int(found[2])}" if found[2] else "")
        for item in items:
            if item.key == key:
                return item, ""
    if said.lower().startswith("none"):
        why = said[4:].lstrip(" :-–").strip()
        return None, (why or "no reason given")[:WHY_CHARS]
    return None, (f"{said[:80]!r} isn't on the READY list" if said else "the plan named none")[:WHY_CHARS]


def record(conn: sqlite3.Connection, cycle_id: int, items: list[Item], pick: Item | None, why: str, now: str) -> None:
    """A venture plan's pick and the READY list it came from (never changed)."""
    conn.execute(
        "INSERT INTO desk_picks (cycle_id, created_at, items, pick, venture_id, why_not) VALUES (?, ?, ?, ?, ?, ?)",
        (
            cycle_id,
            now,
            json.dumps([i.to_json() for i in items], ensure_ascii=False),
            pick.key if pick else None,
            pick.venture_id if pick else None,
            None if pick else why,
        ),
    )


def recent(conn: sqlite3.Connection, scope: AgentScope, limit: int = 10) -> list[sqlite3.Row]:
    """The latest venture plans' picks, the newest first."""
    return conn.execute(
        "SELECT d.* FROM desk_picks d JOIN cycles y ON y.id = d.cycle_id WHERE y.session = ? AND y.simulated = ?"
        " ORDER BY d.id DESC LIMIT ?",
        (scope.session, 1 if scope.simulated else 0, limit),
    ).fetchall()


def decided(conn: sqlite3.Connection, scope: AgentScope, since: str) -> int:
    """Ventures whose stage became proposed, parked or killed since ``since`` (the desk's watch: 2 a week): the agent's
    and the owner's decisions (0.15.0: not Ember's code's parks by a stage's rule, which aren't decisions)."""
    where, params = scope.where()
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM ventures WHERE {where} AND stage IN ('proposed', 'parked', 'killed')"
            " AND stage_at >= ? AND NOT (stage = 'parked' AND parked_by IS 'code')",
            (*params, since),
        ).fetchone()[0]
    )
