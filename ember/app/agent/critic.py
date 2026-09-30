"""An independent critic (0.13.0): a second opinion on a venture's business case before the owner backs it.

The agent argued its own business cases, so a case's weak point reached the owner only if the agent itself saw it.
Now, before the next plan after a venture is proposed, a separate call on the strategy model (``critic_request``)
reviews the newest case with its evidence: the fatal flaw, its own numbers for the same case, a verdict (back; test:
only a cheaper first test; park) and what would change its mind. Ember's code checks its answer, computes the
economics of its numbers like the agent's (econ.py, with the agent's setup, hours and API spend) and keeps it. The owner
sees it with the case, the agent in FOCUS, and a venture ranks by the lower of the two expected nets. A critique that
failed (cut off, not usable) is kept with why, and the case is tried again at the next cycle, MAX_ATTEMPTS times in all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from . import econ, evidence, ventures
from .store import AgentScope

VERDICTS = ("back", "test", "park")
TEXT_CHARS = 300  # its fatal flaw and what would change its mind
# Its numbers for the same case (the agent's setup, hours and API spend stay the agent's).
NUMBERS = (
    "price_eur",
    "unit_cost_eur",
    "monthly_costs_eur",
    "sales_low",
    "sales_mid",
    "sales_high",
    "first_sale_months",
)
CLAIMS_SHOWN = 12  # the evidence it reads (the newest)
MAX_SALES = 100_000
MAX_EUR = 1_000_000
MAX_ATTEMPTS = 2  # critiques of one case, failed ones included


def due(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    """The proposed venture whose newest case has no critique yet, nor MAX_ATTEMPTS failed ones (the oldest proposal
    first), with that case's id (``case_id``), or None."""
    where, params = scope.where("v")
    return conn.execute(
        "WITH newest AS (SELECT venture_id, MAX(id) AS case_id FROM venture_cases GROUP BY venture_id)"
        f" SELECT v.*, n.case_id FROM ventures v JOIN newest n ON n.venture_id = v.id WHERE {where}"
        " AND v.stage = 'proposed' AND NOT EXISTS (SELECT 1 FROM venture_critiques k WHERE k.case_id = n.case_id"
        " AND k.status = 'ok') AND (SELECT COUNT(*) FROM venture_critiques k WHERE k.case_id = n.case_id) < ?"
        " ORDER BY v.proposed_at, v.id LIMIT 1",
        (*params, MAX_ATTEMPTS),
    ).fetchone()


def case_text(
    conn: sqlite3.Connection, venture: Mapping[str, Any], case_row: Mapping[str, Any], record: str = ""
) -> str:
    """What the critic reads: the venture's pitch and business case, its numbers with Ember's code's economics, its
    evidence by grade (the newest CLAIMS_SHOWN) and the record of the agent's forecasts (``record``,
    predictions.calibration)."""
    case, result = ventures.case_of(case_row)
    prose = "\n".join(f"{label}: {' '.join(str(venture[name] or '-').split())}" for name, label, _ in ventures.CASE)
    claims = evidence.of_venture(conn, int(venture["id"]), CLAIMS_SHOWN)
    graded = evidence.counts(conn, int(venture["id"]))
    listed = "\n".join(f"- [{r['source']}] {evidence.value_text(r)}: {' '.join(r['claim'].split())}" for r in claims)
    return (
        f"Venture #{venture['id']}: {venture['title']}\nPitch: {' '.join(str(venture['pitch']).split())}\n{prose}\n\n"
        f"The agent's numbers (case #{case_row['id']}): channel {case.channel}, price EUR {case.price_eur:.2f}, cost "
        f"per sale EUR {case.unit_cost_eur:.2f}, fixed costs EUR {case.monthly_costs_eur:.2f} a month, sales a month "
        f"{case.sales[0]} (P10) / {case.sales[1]} (P50) / {case.sales[2]} (P90), cash to start EUR "
        f"{case.setup_eur:.2f}, the owner's hours {case.owner_hours:g} a month, first sale in {case.first_sale_months} "
        f"months, API spend USD {case.api_usd:.2f} a month.\nEmber's code's economics of them: {result.text(case)}\n\n"
        f"Evidence: {sum(graded.values())} claims ({graded['independent']} independent, {graded['marketing']} "
        f"marketing, {graded['unchecked']} unchecked)"
        + (f"; the newest:\n{listed}" if listed else ".")
        + (f"\n\nThe agent's forecasts, settled by Ember's code: {record}." if record else "")
    )


def parse(answer: Any, agents: econ.Case) -> tuple[dict[str, str], econ.Case] | None:
    """The critic's answer checked: its texts and its numbers as a case (the agent's setup, hours and API spend), or
    None when it isn't usable."""
    if not isinstance(answer, Mapping) or answer.get("verdict") not in VERDICTS:
        return None
    texts = {name: " ".join(str(answer.get(name) or "").split())[:TEXT_CHARS] for name in ("fatal_flaw", "change_mind")}
    numbers = answer.get("numbers")
    if not all(texts.values()) or not isinstance(numbers, Mapping):
        return None
    try:
        amounts = {name: float(numbers[name]) for name in ("price_eur", "unit_cost_eur", "monthly_costs_eur")}
        sales = tuple(int(numbers[name]) for name in ("sales_low", "sales_mid", "sales_high"))
        first = int(numbers["first_sale_months"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < amounts["price_eur"] <= MAX_EUR or not all(0 <= amounts[n] <= MAX_EUR for n in amounts):
        return None
    if not 0 <= sales[0] <= sales[1] <= sales[2] <= MAX_SALES or not 0 <= first <= econ.MAX_FIRST_SALE_MONTHS:
        return None
    case = econ.Case(
        channel=agents.channel,
        price_eur=amounts["price_eur"],
        unit_cost_eur=amounts["unit_cost_eur"],
        monthly_costs_eur=amounts["monthly_costs_eur"],
        sales=(sales[0], sales[1], sales[2]),
        setup_eur=agents.setup_eur,
        owner_hours=agents.owner_hours,
        first_sale_months=first,
        api_usd=agents.api_usd,
    )
    return {"verdict": str(answer["verdict"]), **texts}, case


def add(
    conn: sqlite3.Connection,
    venture_id: int,
    case_id: int,
    llm_call_id: int | None,
    texts: Mapping[str, str],
    case: econ.Case,
    result: econ.Economics,
    now: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO venture_critiques (venture_id, case_id, llm_call_id, created_at, status, verdict, fatal_flaw,"
        " change_mind, price_eur, unit_cost_eur, monthly_costs_eur, sales_low, sales_mid, sales_high,"
        " first_sale_months, net_eur, break_even, ev_eur)"
        " VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            venture_id,
            case_id,
            llm_call_id,
            now,
            texts["verdict"],
            texts["fatal_flaw"],
            texts["change_mind"],
            case.price_eur,
            case.unit_cost_eur,
            case.monthly_costs_eur,
            *case.sales,
            case.first_sale_months,
            result.net_eur,
            result.break_even,
            result.ev_eur,
        ),
    )
    return int(cursor.lastrowid)


def add_failed(
    conn: sqlite3.Connection, venture_id: int, case_id: int, llm_call_id: int | None, note: str, now: str
) -> None:
    """A critique that failed (``note``: why), so the case is tried at most MAX_ATTEMPTS times."""
    conn.execute(
        "INSERT INTO venture_critiques (venture_id, case_id, llm_call_id, created_at, status, note)"
        " VALUES (?, ?, ?, ?, 'failed', ?)",
        (venture_id, case_id, llm_call_id, now, note[:TEXT_CHARS]),
    )


def latest(conn: sqlite3.Connection, venture_id: int) -> sqlite3.Row | None:
    """The critique of the venture's newest case, or None (a newer case awaits its own)."""
    return conn.execute(
        "SELECT k.* FROM venture_critiques k WHERE k.venture_id = ? AND k.status = 'ok' AND k.case_id = (SELECT"
        " MAX(c.id) FROM venture_cases c WHERE c.venture_id = ?) ORDER BY k.id DESC LIMIT 1",
        (venture_id, venture_id),
    ).fetchone()


def failures(conn: sqlite3.Connection, venture_id: int) -> int:
    """How many critiques of the venture's newest case failed."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM venture_critiques k WHERE k.venture_id = ? AND k.status = 'failed' AND k.case_id ="
            " (SELECT MAX(c.id) FROM venture_cases c WHERE c.venture_id = ?)",
            (venture_id, venture_id),
        ).fetchone()[0]
    )


def ranking_ev(case_row: Mapping[str, Any] | None, critique: Mapping[str, Any] | None) -> float | None:
    """What ranks a venture: the lower of the agent's and the critic's expected net a month (None without a case)."""
    if case_row is None:
        return None
    ours = float(case_row["ev_eur"])
    return min(ours, float(critique["ev_eur"])) if critique is not None else ours


def text(critique: Mapping[str, Any] | None, case_row: Mapping[str, Any] | None) -> str:
    """FOCUS's critic line ("" without a critique of the newest case)."""
    if critique is None or case_row is None:
        return ""
    return (
        f"Critic (a separate call on case #{critique['case_id']}): {critique['verdict']}; fatal flaw: "
        f"{critique['fatal_flaw']}; its numbers: a sale keeps EUR {float(critique['net_eur']):.2f}, expected EUR "
        f"{float(critique['ev_eur']):.0f} a month (yours: EUR {float(case_row['ev_eur']):.0f}); it would change its "
        f"mind if: {critique['change_mind']}"
    )
