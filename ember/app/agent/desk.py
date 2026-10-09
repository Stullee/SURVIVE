"""The ventures' next decisions (0.13.0, the decision desk), as Ember's code names them.

A venture cycle's planner chose freely what to look at, so research went where the plan's mood took it, and the
brainstorm never ran live. So Ember's code names what each venture needs decided next:

* appraise: a venture being researched, whose next need Ember's code names (research, evidence, numbers, a knock-out
  to fix; then propose or park it);
* answer: a proposed venture whose critic said test or park: answer its flaw (evidence, new numbers) or park it;
* triage: an idea: research it or park it, while fewer than ventures.MAX_ACTIVE are researched or proposed;
* brainstorm: while fewer than FUNNEL ideas wait and fewer than FUNNEL ventures have their numbers.

0.19.3 (the owner's view: a venture is something new to try; once they back it, it is a project): a backed venture
has no next decision here (it was "build": set up its first test). Ember's code opens its project (stages.keep) and
ordinary cycles run its first test, so venture cycles go to finding and deciding new ones.

Until 0.36.0 they were READY, ranked (the owner's wishes, ventures close to being parked, answers to the critic, the
other appraisals by expected net, triage, a brainstorm last); a venture cycle's plan took one (``ready``) or said why
it took none, and Ember's code kept each pick with the list it came from (``desk_picks``, never changed). 0.37.0:
each venture's decision is a step of the plan tree under its venture's node (plan.py), weighed like any step: ``item``
says what it needs now (YOUR STEP shows it), its tier makes it urgent (the owner's wish, a park soon: within
URGENT_DAYS, or with little research budget left), and ``brainstorm_due`` lays out a brainstorm step.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import critic, knockouts, ventures
from .store import AgentScope

FUNNEL = 5  # ideas waiting, and ventures with numbers, below which a brainstorm is ready
URGENT_DAYS = 7  # a venture parked by its stage's rule within this many days is urgent
URGENT_BUDGET = 0.25  # ... and one with this share of its research budget left
ITEM_CHARS = 220  # an item's text (YOUR STEP's "Now:", READY's until 0.36.0)


def _line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(frozen=True)
class Item:
    kind: str
    venture_id: int | None
    text: str  # what it is and why now, with the numbers (at most ITEM_CHARS characters)
    tier: str = ""  # 0.36.0: 'wish' (the owner's) or 'urgent' (parked soon): the plan tree's urgency (plan.py)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _line(self.text, ITEM_CHARS))

    @property
    def key(self) -> str:
        return self.kind if self.venture_id is None else f"{self.kind} #{self.venture_id}"


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


def item(
    conn: sqlite3.Connection,
    v: Mapping[str, Any],
    *,
    today: date,
    cash_eur: float = 0.0,
    net_days: float | None = None,
    room: bool = True,
) -> Item | None:
    """0.37.0: a venture's next decision, as READY showed it until 0.36.0 (None: it has none, or an idea while
    ventures.MAX_ACTIVE are researched or proposed): the plan tree's step of the venture says it (plan.py), and its
    tier makes it urgent there."""
    vid, stage, title = int(v["id"]), v["stage"], _line(v["title"], 50)
    if stage not in ventures.EXPLORING:
        return None  # 0.19.3: a backed or live venture is its project's, an ordinary cycle's work
    case_row = ventures.latest_case(conn, vid) if v["cases"] else None
    judged = critic.latest(conn, vid) if case_row is not None else None
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
            return Item("appraise", vid, f"your owner's wish: {text}", "wish")
        return Item("appraise", vid, text, "urgent" if urgent else "")
    if stage == "proposed" and judged is not None and judged["verdict"] in ("test", "park"):
        flaw = _line(judged["fatal_flaw"], 120)
        text = (
            f"{title} · the critic says {judged['verdict']} (case #{judged['case_id']}): {flaw} · answer it with "
            f"evidence or new numbers, or park it · {_ev_text(case_row, judged)}"
        )
        return Item("answer", vid, text)
    if stage == "idea":
        text = f"{title} · an idea, {ventures.scores_text(v)}: research it (stage researching) or park it with why"
        triage = ventures.triage_date(v)  # 0.13.0: an idea of the agent's no one takes up is parked then
        if triage is not None:
            text += f" · parked by Ember's code on {triage.isoformat()}"
        if wish:
            return Item("triage", vid, f"your owner's idea: {text}", "wish")
        if room and triage is not None and (triage - today).days <= URGENT_DAYS:
            return Item("triage", vid, text, "urgent")
        if room:
            return Item("triage", vid, text)
    return None


def brainstorm_due(rows: list[sqlite3.Row]) -> str:
    """0.37.0 (READY's last item until then): why a brainstorm is due, while fewer than FUNNEL ideas wait and fewer
    than FUNNEL ventures have their numbers and the tree has room ("" when none is)."""
    if len(rows) + ventures.BRAINSTORM_IDEAS > ventures.MAX_VENTURES:
        return ""
    waiting = sum(1 for v in rows if v["stage"] == "idea")
    cased = sum(1 for v in rows if v["stage"] in ventures.OPEN_STAGES and v["cases"])
    if waiting >= FUNNEL or cased >= FUNNEL:
        return ""
    return (
        f"{waiting} idea{'s' if waiting != 1 else ''} wait and {cased} venture{'s' if cased != 1 else ''} "
        "have their numbers: brainstorm new ones"
    )


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
