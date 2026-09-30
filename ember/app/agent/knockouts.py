"""Knock-outs (0.13.0): what rules a venture out, checked by Ember's code before it is proposed.

What ruled a venture out was the agent's judgement, and a dropshipping case that rested on vendors' pages reached the
owner. Now each business case is checked against six knock-outs:

* cold_outreach: it needs writing to people who didn't ask first (illegal advertising in Germany, UWG section 7): the
  case says so (``needs``) or its words do;
* ember_accounts: it needs accounts Ember itself would create (big platforms block automated sign-ups, and
  Anthropic's usage policy forbids them): the case says so;
* cash: it needs more cash to start than the owner's venture budget (the ``venture_cash_eur`` option);
* slow: its first sale comes later than half the net runway;
* losing: a sale loses money (its net per sale, after the fees, is not above 0);
* vendor_only: no independent page backs its demand (its evidence is vendors', affiliates' or unchecked, or none).

A knock-out is reversible: it goes when the case changes (new numbers, new evidence), and the owner can override one
for a venture on the Ventures tab (and restore it). A knocked-out venture isn't proposed; the agent parks it with the
numbers, or fixes what can be fixed.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import evidence, ventures

RULES = ("cold_outreach", "ember_accounts", "cash", "slow", "losing", "vendor_only")
LABELS = {
    "cold_outreach": "cold outreach",
    "ember_accounts": "accounts Ember would create",
    "cash": "cash beyond the budget",
    "slow": "a first sale after half the runway",
    "losing": "a sale loses money",
    "vendor_only": "no independent source for its demand",
}
NEEDS = ("cold_outreach", "ember_accounts")  # what a case says it needs (venture_case's ``needs``)
DAYS_A_MONTH = 30.4
# Words of a case that plan writing to people who didn't ask first (the owner can override a wrong match).
_COLD = re.compile(
    r"\bcold[- ](?:e-?mails?|call(?:s|ing)?|outreach|messages?|dms?|pitch(?:es)?)\b|\bkaltakquise\b"
    r"|\b(?:e-?mail|contact|write to|message|dm)(?:ing)? (?:local |small )?(?:businesses|companies|shops|shop owners"
    r"|leads|prospects|agencies)\b|\blead (?:lists?|generation)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class KnockOut:
    rule: str
    why: str  # with the numbers
    overridden: bool  # the owner lifted it for this venture

    @property
    def label(self) -> str:
        return LABELS[self.rule]


def overridden(conn: sqlite3.Connection, venture_id: int) -> set[str]:
    """The knock-outs the owner has lifted for the venture (their newest word on each)."""
    rows = conn.execute(
        "SELECT rule, overridden FROM knockout_overrides WHERE venture_id = ? ORDER BY id", (venture_id,)
    ).fetchall()
    state: dict[str, bool] = {}
    for r in rows:
        state[str(r["rule"])] = bool(r["overridden"])
    return {rule for rule, on in state.items() if on}


def set_override(
    conn: sqlite3.Connection, venture_id: int, rule: str, on: bool, by: str | None, comment: str | None, now: str
) -> None:
    """The owner lifts (``on``) or restores a knock-out for a venture: kept as history."""
    if rule not in RULES:
        raise ValueError("unknown knock-out")
    conn.execute(
        "INSERT INTO knockout_overrides (venture_id, rule, overridden, by, comment, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (venture_id, rule, 1 if on else 0, by, comment, now),
    )


def check(
    conn: sqlite3.Connection, venture: Mapping[str, Any], *, cash_eur: float, net_days: float | None
) -> list[KnockOut]:
    """Every knock-out that applies to the venture's newest case, lifted by the owner or not (``overridden``)."""
    vid = int(venture["id"])
    lifted = overridden(conn, vid)
    case = ventures.latest_case(conn, vid)
    found: list[tuple[str, str]] = []
    needs = set(str(case["needs"] or "").split(",")) if case is not None else set()
    words = " ".join(str(venture[name] or "") for name in ("pitch", *ventures.CASE_FIELDS))
    cold = _COLD.search(words)
    if "cold_outreach" in needs or cold:
        why = "its case says it needs it" if "cold_outreach" in needs else f"its case plans it ({cold[0]!r})"
        found.append(("cold_outreach", f"{why}: advertising to people who didn't ask first is illegal in Germany"))
    if "ember_accounts" in needs:
        found.append(("ember_accounts", "its case needs accounts Ember itself would create: your owner creates them"))
    if case is not None:
        if float(case["setup_eur"]) > cash_eur:
            found.append(("cash", f"EUR {float(case['setup_eur']):.0f} to start, the budget is EUR {cash_eur:.0f}"))
        days = int(case["first_sale_months"]) * DAYS_A_MONTH
        if net_days is not None and days > net_days / 2:
            found.append(
                (
                    "slow",
                    f"its first sale in {int(case['first_sale_months'])} months, half the runway is "
                    f"{net_days / 2:.0f} days",
                )
            )
        if float(case["net_eur"]) <= 0:
            found.append(("losing", f"a sale keeps EUR {float(case['net_eur']):.2f} after fees and its cost"))
    graded = evidence.counts(conn, vid)
    if graded["independent"] == 0:
        what = (
            f"its evidence is {graded['marketing']} vendors' or affiliates' and {graded['unchecked']} unchecked"
            if sum(graded.values())
            else "it has no evidence yet"
        )
        found.append(("vendor_only", f"{what}: save an independent page's numbers with evidence"))
    return [KnockOut(rule, why, rule in lifted) for rule, why in found]


def active(ko: list[KnockOut]) -> list[KnockOut]:
    return [k for k in ko if not k.overridden]


def text(ko: list[KnockOut]) -> str:
    """FOCUS's knock-out line ("" without any)."""
    if not ko:
        return ""
    on = "; ".join(f"{k.label} ({k.why})" for k in active(ko))
    lifted = ", ".join(k.label for k in ko if k.overridden)
    parts = ([f"Knock-outs (Ember's code; it isn't proposed while one stands): {on}"] if on else []) + (
        [f"lifted by your owner: {lifted}"] if lifted else []
    )
    return ". ".join(parts)
