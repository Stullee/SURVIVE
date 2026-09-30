"""The policy engine (0.13.0): how much the owner lets Ember's code carry out without their click.

Every action waited for the owner (about 46 clicks a day live), also the small, safe ones. Now the owner can grant a
milestone they back autonomy for a few rules (RULES), all off by default:

* qa_fix: a change that only brings a live listing of Ember's up to the QA registry's photos (qa.MIN_PHOTOS);
* price_change: a change of a live listing's price only, within PRICE_BAND;
* listing_variant: a new listing in a backed leg (its venture building or live) once the owner approved
  VARIANTS_FIRST of that leg's listings without changes;
* deactivate: taking a listing of Ember's off Etsy (nothing else changes);
* email_reply: an email that answers someone in a thread they started (Ember's footer always says an AI wrote it).

Each grant has a level (LEVELS): manual (as before), veto_window (approved VETO_HOURS after the request unless the
owner decided first) or auto (approved at once), a daily limit and a budget of actions. A request of the milestone the
cycle worked for that fits a granted rule is carried by it (``apply``); every request that fits a rule is kept as a
candidate. Ember's code revokes a grant (``keep``) on an unclear result, a spent budget, a missed or dropped milestone
or the owner's veto (rejecting a request during its window), and only proposes a promotion (``suggestions``): a rule
whose requests the owner approved PROMOTE_AFTER times without changes in PROMOTE_DAYS. The owner decides.

Whatever is unlocked, no unlock carries a NEVER request (never.py: an account, money, a first contact, a first
publication, a community post, tax, VAT, a Gewerbe or a contract, what only the owner carries out), and only the owner
unlocks. The database checks the same (migration 0051); what it refuses is undone alone and waits for the owner.

0.14.0: an unlock carries only a request that passes its class's QA (qa.CHECKS), and unlocks act only while Ember
knows its owner (owner_user_ids) outside safe mode (``off``). An unlock taken back (by the owner, the kill switch or
Ember's code) also stops what it approved and Ember's code hasn't begun: it waits for the owner again (``_stop``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ..economy.clock import Clock, to_iso
from ..integrations import connectors, etsy, etsy_publisher, qa
from . import never
from .store import AgentScope


@dataclass(frozen=True)
class Rule:
    name: str
    action_class: str  # connectors.CLASSES
    label: str


RULES: dict[str, Rule] = {
    r.name: r
    for r in (
        Rule("qa_fix", "etsy.edit_listing", f"QA fixes: photos up to {qa.MIN_PHOTOS} on a live listing"),
        Rule("price_change", "etsy.edit_listing", "price changes within 15% on a live listing"),
        Rule("listing_variant", "etsy.create_listing", "new listings in a backed leg after 5 approved unchanged"),
        Rule("deactivate", "etsy.deactivate", "taking a listing of Ember's off Etsy"),
        Rule("email_reply", "email.reply", "email replies in threads the other person started"),
    )
}
LEVELS = ("manual", "veto_window", "auto")
VETO_HOURS = 12
PRICE_BAND = Decimal("0.15")
VARIANTS_FIRST = 5
PROMOTE_AFTER = 5
PROMOTE_DAYS = 30
PER_DAY, BUDGET = 3, 10  # a grant's defaults
POLICY_BY = "Ember's code (your unlock)"
REVOKED_BY = "Ember's code"
CODE = (POLICY_BY, REVOKED_BY, "Ember")  # who never unlocks anything (migration 0051 names them too)
STOPPED = "Approved by your unlock, which was taken back ({why}) before Ember's code carried it out: it waits for you."


def _action(row: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        data = json.loads(row["action"] or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def off(owner_ids: Sequence[str], safe_mode: bool) -> str:
    """0.14.0: why unlocks don't act now ("" when they do). Only while owner_user_ids names the owner, so that only
    they unlocked, and never in safe mode (its options are built-in defaults)."""
    if safe_mode:
        return "the app runs in safe mode"
    return "" if owner_ids else "owner_user_ids names no one"


def short(row: Mapping[str, Any]) -> list[str]:
    """0.14.0: what a request falls short of by its class's QA (qa.CHECKS, as its tool and card show it)."""
    action = _action(row)
    if action is None:
        return ["its action isn't readable"]
    kind = connectors.class_of(row["executor"], action, row["type"]).name
    try:
        if row["executor"] == "etsy_listing":
            return qa.defects(kind, etsy.listing_from_action(action))
        if row["executor"] == "etsy_edit":
            return qa.defects(kind, etsy.edit_from_action(action))
    except etsy.EtsyError as exc:
        return [str(exc)]
    return qa.defects(kind, action)


def _started_by_them(conn: sqlite3.Connection, scope: AgentScope, action: Mapping[str, Any]) -> bool:
    """An email answers someone in a thread they started: its thread's first message is theirs."""
    to = str(action.get("to") or "").lower()
    chain = str(action.get("references") or action.get("in_reply_to") or "").split()
    if not to or not chain:
        return False
    where, params = scope.where()
    root = conn.execute(
        f"SELECT direction, from_addr FROM emails WHERE {where} AND message_id = ? ORDER BY id LIMIT 1",
        (*params, chain[0]),
    ).fetchone()
    return root is not None and root["direction"] == "in" and str(root["from_addr"]).lower() == to


def _clean_approvals(conn: sqlite3.Connection, scope: AgentScope, venture_id: int) -> int:
    """The owner's approvals without changes of listings for a venture."""
    where, params = scope.where()
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM approvals WHERE {where} AND executor = 'etsy_listing' AND venture_id = ?"
            " AND status IN ('approved', 'done') AND final_payload IS NULL AND decided_by IS NOT ?",
            (*params, venture_id, POLICY_BY),
        ).fetchone()[0]
    )


def match(conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any]) -> str | None:
    """The rule a request fits, or None."""
    action = _action(row)
    if action is None:
        return None
    if row["executor"] == "email":
        return "email_reply" if action.get("in_reply_to") and _started_by_them(conn, scope, action) else None
    if row["executor"] == "etsy_edit":
        try:
            edit = etsy.edit_from_action(action)
        except etsy.EtsyError:
            return None
        parts = edit.parts()
        if parts == ["deactivate"]:
            return "deactivate"
        current = etsy_publisher.current_listing(conn, scope, edit.listing_id)
        if current is None:
            return None
        if parts == ["price"] and edit.price is not None:
            try:
                old, new = Decimal(current.price), Decimal(edit.price)
            except InvalidOperation:
                return None
            return "price_change" if old > 0 and abs(new / old - 1) <= PRICE_BAND else None
        if parts == ["photos"] and edit.photos is not None:
            return "qa_fix" if len(current.photos) < qa.MIN_PHOTOS <= len(edit.photos) else None
        return None
    if row["executor"] == "etsy_listing" and row["venture_id"] is not None:
        leg = conn.execute("SELECT stage FROM ventures WHERE id = ?", (row["venture_id"],)).fetchone()
        backed = leg is not None and leg["stage"] in ("building", "live")
        if backed and _clean_approvals(conn, scope, int(row["venture_id"])) >= VARIANTS_FIRST:
            return "listing_variant"
    return None


# --- grants ---


def grants(conn: sqlite3.Connection, scope: AgentScope, milestone_id: int | None = None) -> list[sqlite3.Row]:
    """The standing grant of each milestone and rule (the newest), a milestone's alone if given."""
    where, params = scope.where("g")
    mine = " AND g.milestone_id = ?" if milestone_id is not None else ""
    return conn.execute(
        f"SELECT g.* FROM policy_grants g WHERE {where}{mine} AND g.id = (SELECT MAX(h.id) FROM policy_grants h"
        " WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule) ORDER BY g.milestone_id, g.rule",
        (*params, *((milestone_id,) if milestone_id is not None else ())),
    ).fetchall()


def grant(conn: sqlite3.Connection, scope: AgentScope, milestone_id: int, rule: str) -> sqlite3.Row | None:
    """The standing grant of a milestone for a rule, None while it is manual."""
    found = [g for g in grants(conn, scope, milestone_id) if g["rule"] == rule]
    return found[0] if found and found[0]["level"] != "manual" else None


def set_grant(
    conn: sqlite3.Connection,
    scope: AgentScope,
    milestone_id: int,
    rule: str,
    level: str,
    now: str,
    *,
    per_day: int = PER_DAY,
    budget: int = BUDGET,
    by: str,
    why: str | None = None,
) -> int:
    if rule not in RULES or level not in LEVELS:
        raise ValueError("unknown rule or level")
    if level != "manual" and by in CODE:
        raise ValueError("only the owner unlocks (NEVER: the policies)")
    cursor = conn.execute(
        "INSERT INTO policy_grants (mode, session, milestone_id, rule, level, per_day, budget, by, why, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, milestone_id, rule, level, per_day, budget, by[:60], why, now),
    )
    if level == "manual":  # 0.14.0: in the same transaction, before an executor can begin what it approved
        _stop(conn, milestone_id, rule, now, why or "you took it back")
    return int(cursor.lastrowid)


def _stop(conn: sqlite3.Connection, milestone_id: int, rule: str, now: str, why: str) -> list[int]:
    """0.14.0: what the unlocks of a milestone's rule approved and Ember's code hasn't begun (no journal entry) waits
    for the owner again, saying why. The executors check the status when they begin; what began runs on, once. The
    same request waiting already: this one is closed instead. Returns the requests stopped."""
    said = STOPPED.format(why=why)
    stopped = []
    for r in conn.execute(
        "SELECT a.id, a.mode, a.session, a.payload_sha256 FROM policy_uses u JOIN policy_grants g ON g.id = u.grant_id"
        " JOIN approvals a ON a.id = u.approval_id WHERE g.milestone_id = ? AND g.rule = ? AND a.status = 'approved'"
        " AND a.decided_by = ? AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = a.id)"
        " ORDER BY a.id",
        (milestone_id, rule, POLICY_BY),
    ).fetchall():
        twin = conn.execute(
            "SELECT id FROM approvals WHERE mode = ? AND session = ? AND payload_sha256 = ? AND status = 'pending'",
            (r["mode"], r["session"], r["payload_sha256"]),
        ).fetchone()
        if twin is None:
            conn.execute(
                "UPDATE approvals SET status = 'pending', decided_at = NULL, decided_by = NULL, decision_comment = ?,"
                " version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status = 'approved'",
                (said[:2000], r["id"]),
            )
        else:
            conn.execute(
                "UPDATE approvals SET status = 'failed', closed_at = ?, closed_by = ?, result_note = ?,"
                " version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status = 'approved'",
                (now, REVOKED_BY, f"{said} The same request waits as #{twin['id']}."[:2000], r["id"]),
            )
        stopped.append(int(r["id"]))
    return stopped


def used(conn: sqlite3.Connection, grant_id: int, clock: Clock) -> tuple[int, int]:
    """The requests a grant carried, in all and today (the owner's day)."""
    start = to_iso(clock.day_start(clock.today()))
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) FROM policy_uses"
        " WHERE grant_id = ?",
        (start, grant_id),
    ).fetchone()
    return int(row[0]), int(row[1])


def _stands(conn: sqlite3.Connection, grant_id: int) -> bool:
    """Whether a grant still stands: the newest of its milestone and rule, and not manual (not taken back)."""
    row = conn.execute(
        "SELECT g.level, (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id"
        " AND h.rule = g.rule) AS newest FROM policy_grants g WHERE g.id = ?",
        (grant_id,),
    ).fetchone()
    return row is not None and row["level"] != "manual" and row["newest"] == grant_id


@contextmanager
def _refusable(conn: sqlite3.Connection) -> Iterator[None]:
    """What the database refuses (migration 0051) is undone alone, and the request waits for the owner."""
    conn.execute("SAVEPOINT policy_carry")
    try:
        yield
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO policy_carry")
        conn.execute("RELEASE policy_carry")
        raise
    conn.execute("RELEASE policy_carry")


def _approve(conn: sqlite3.Connection, approval_id: int, now: str, why: str) -> bool:
    return (
        conn.execute(
            "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ?, decision_comment = ?,"
            " version = version + 1 WHERE id = ? AND status = 'pending'",
            (now, POLICY_BY, why[:2000], approval_id),
        ).rowcount
        == 1
    )


def apply(conn: sqlite3.Connection, scope: AgentScope, approval_id: int, clock: Clock, off: str = "") -> str:
    """A request just made: kept as a candidate if it fits a rule, and carried by the grant of the milestone its cycle
    worked for, if one stands and has room (0.14.0: and unlocks act, ``off`` empty, and it passes its QA). Returns
    what the agent is told ("" when nothing changes)."""
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    rule = match(conn, scope, row) if row is not None and row["status"] == "pending" else None
    if row is None or rule is None:
        return ""
    now = to_iso(clock.now())
    conn.execute(
        "INSERT OR IGNORE INTO policy_candidates (approval_id, rule, milestone_id, created_at) VALUES (?, ?, ?, ?)",
        (approval_id, rule, row["milestone_id"], now),
    )
    if row["milestone_id"] is None:
        return ""
    milestone = conn.execute("SELECT status FROM milestones WHERE id = ?", (row["milestone_id"],)).fetchone()
    granted = grant(conn, scope, int(row["milestone_id"]), rule)
    if milestone is None or milestone["status"] != "open" or granted is None:
        return ""
    label = RULES[rule].label
    if off:
        return f" It waits for your owner: unlocks are off while {off}."
    found = never.reasons(conn, row)
    if found:
        return f" It waits for your owner whatever they unlocked: never automatic for {never.text(found)}."
    falls = short(row)
    if falls:
        return f" It waits for your owner: an unlock carries only what passes QA ({'; '.join(falls)})."
    total, today = used(conn, int(granted["id"]), clock)
    if total >= int(granted["budget"]):
        return ""  # spent: keep() revokes it
    if today >= int(granted["per_day"]):
        return f" Your owner's unlock for {label} has carried {today} today, its limit: this one waits for them."
    unlock = f"your owner unlocked {label} for milestone #{row['milestone_id']}"
    auto = granted["level"] == "auto"
    until = None if auto else to_iso(clock.now() + timedelta(hours=VETO_HOURS))
    try:
        with _refusable(conn):
            conn.execute(
                "INSERT INTO policy_uses (approval_id, grant_id, level, created_at, veto_until, approved_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (approval_id, granted["id"], granted["level"], now, until, now if auto else None),
            )
            if auto:
                _approve(conn, approval_id, now, f"Approved at once: {unlock} (auto).")
    except sqlite3.IntegrityError as exc:
        return f" It waits for your owner: the database refused the unlock ({exc})."
    if auto:
        return f" Ember's code approved it at once: {unlock} (auto)."
    return f" It is approved {VETO_HOURS} hours from now unless your owner decides first: {unlock} (veto window)."


def run_due(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, off: str = "") -> list[str]:
    """Approve the requests whose veto window has passed while the owner didn't decide; returns what happened. 0.14.0:
    none while unlocks are off, and none that falls short of its QA."""
    if off:
        return []
    now = to_iso(clock.now())
    where, params = scope.where("a")
    happened = []
    for use in conn.execute(
        f"SELECT u.*, a.status FROM policy_uses u JOIN approvals a ON a.id = u.approval_id WHERE {where}"
        " AND u.level = 'veto_window' AND u.approved_at IS NULL AND u.veto_until <= ? ORDER BY u.id",
        (*params, now),
    ).fetchall():
        if use["status"] != "pending" or not _stands(conn, int(use["grant_id"])):
            continue  # the owner decided first, or the unlock was taken back: it waits for them
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (use["approval_id"],)).fetchone()
        if never.reasons(conn, row) or short(row):
            continue
        why = f"Approved: no veto within {VETO_HOURS} hours (your unlock, grant #{use['grant_id']})."
        try:
            with _refusable(conn):
                if _approve(conn, int(use["approval_id"]), now, why):
                    conn.execute("UPDATE policy_uses SET approved_at = ? WHERE id = ?", (now, use["id"]))
                    happened.append(
                        f"Request #{use['approval_id']} approved by your unlock: no veto within {VETO_HOURS} hours"
                    )
        except sqlite3.IntegrityError as exc:
            happened.append(f"Request #{use['approval_id']} waits for you: the database refused the unlock ({exc})")
    return happened


def keep(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> list[str]:
    """Revoke the grants whose milestone was missed or dropped, whose budget is spent, which carried a request that
    ended unclear, whose request the owner vetoed, or (email replies) whose reply the person answered by asking to
    stop. Returns what happened."""
    now = to_iso(clock.now())
    happened = []
    for g in grants(conn, scope):
        if g["level"] == "manual":  # 0.14.0: taken back (by an upgrade too): what it approved waits for the owner
            for approval_id in _stop(conn, int(g["milestone_id"]), str(g["rule"]), now, g["why"] or "by you"):
                happened.append(f"Request #{approval_id} waits for you: its unlock was taken back before it ran")
            continue
        why = _revocation(conn, g, clock)
        if why:
            set_grant(conn, scope, int(g["milestone_id"]), str(g["rule"]), "manual", now, by=REVOKED_BY, why=why[:300])
            happened.append(
                f"Ember's code revoked the unlock for {RULES[g['rule']].label} (milestone #{g['milestone_id']}): {why}"
            )
    return happened


def _revocation(conn: sqlite3.Connection, g: sqlite3.Row, clock: Clock) -> str:
    milestone = conn.execute("SELECT status FROM milestones WHERE id = ?", (g["milestone_id"],)).fetchone()
    if milestone is None or milestone["status"] in ("missed", "dropped"):
        return f"milestone #{g['milestone_id']} was {milestone['status'] if milestone else 'removed'}"
    for use in conn.execute(
        "SELECT u.approval_id, a.status, a.closed_by, (SELECT j.status FROM action_journal j WHERE j.approval_id ="
        " u.approval_id ORDER BY j.id DESC LIMIT 1) AS carried FROM policy_uses u JOIN approvals a"
        " ON a.id = u.approval_id WHERE u.grant_id = ? ORDER BY u.id",
        (g["id"],),
    ):
        if use["status"] == "rejected":
            return f"your owner vetoed request #{use['approval_id']}"
        if use["status"] == "failed" and use["carried"] is None and use["closed_by"] not in CODE:
            return f"your owner cancelled request #{use['approval_id']}"  # 0.14.0: before it ran, a veto too
        if use["carried"] == "unclear":
            return f"request #{use['approval_id']} ended unclear"
    if g["rule"] == "email_reply":  # 0.13.0 (Phase E1): the channel's kill rule
        stopped = conn.execute(
            "SELECT u.approval_id, s.address FROM policy_uses u JOIN approvals a ON a.id = u.approval_id"
            " JOIN email_suppressions s ON s.mode = a.mode AND s.session = a.session"
            " AND s.address = lower(json_extract(a.action, '$.to')) WHERE u.grant_id = ? AND u.approved_at IS NOT NULL"
            " AND s.since >= u.approved_at ORDER BY u.id LIMIT 1",
            (g["id"],),
        ).fetchone()
        if stopped is not None:
            return f"{stopped['address']} asked to stop after an automatic reply (request #{stopped['approval_id']})"
    total, _ = used(conn, int(g["id"]), clock)
    if total >= int(g["budget"]):
        return f"its budget of {g['budget']} actions is spent"
    return ""


def suggestions(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> list[dict[str, Any]]:
    """The promotions Ember's code proposes: a rule whose requests for an open milestone the owner approved
    PROMOTE_AFTER times without changes in PROMOTE_DAYS, while nothing is granted for it. The owner decides."""
    since = to_iso(clock.now() - timedelta(days=PROMOTE_DAYS))
    where, params = scope.where("a")
    rows = conn.execute(
        "SELECT c.rule, c.milestone_id, COUNT(*) AS approved FROM policy_candidates c JOIN approvals a"
        f" ON a.id = c.approval_id JOIN milestones m ON m.id = c.milestone_id WHERE {where} AND m.status = 'open'"
        " AND a.status IN ('approved', 'done') AND a.final_payload IS NULL AND a.decided_by IS NOT ?"
        " AND c.created_at >= ? GROUP BY c.rule, c.milestone_id HAVING COUNT(*) >= ? ORDER BY c.milestone_id, c.rule",
        (*params, POLICY_BY, since, PROMOTE_AFTER),
    ).fetchall()
    return [
        {
            "milestone_id": r["milestone_id"],
            "rule": r["rule"],
            "label": RULES[r["rule"]].label,
            "approved": r["approved"],
            "suggested": "veto_window",
        }
        for r in rows
        if grant(conn, scope, int(r["milestone_id"]), str(r["rule"])) is None
    ]


def view(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, milestone_id: int) -> list[dict[str, Any]]:
    """A milestone's autonomy for its card: each rule's standing grant (manual while there is none) and its use."""
    standing = {g["rule"]: g for g in grants(conn, scope, milestone_id)}
    items = []
    for rule in RULES.values():
        g = standing.get(rule.name)
        total, today = used(conn, int(g["id"]), clock) if g is not None else (0, 0)
        items.append(
            {
                "rule": rule.name,
                "label": rule.label,
                "action_class": rule.action_class,
                "level": g["level"] if g is not None else "manual",
                "per_day": g["per_day"] if g is not None else PER_DAY,
                "budget": g["budget"] if g is not None else BUDGET,
                "used": total,
                "used_today": today,
                "by": g["by"] if g is not None else None,
                "why": g["why"] if g is not None else None,
                "since": g["created_at"] if g is not None else None,
            }
        )
    return items


def held(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The requests unlocks hold for their veto window now (waiting, under an unlock that stands), the first due
    first."""
    where, params = scope.where("a")
    rows = conn.execute(
        f"SELECT u.approval_id, u.veto_until, u.grant_id FROM policy_uses u JOIN approvals a ON a.id = u.approval_id"
        f" WHERE {where} AND u.level = 'veto_window' AND u.approved_at IS NULL AND a.status = 'pending'"
        " ORDER BY u.veto_until, u.id",
        params,
    ).fetchall()
    return [r for r in rows if _stands(conn, int(r["grant_id"]))]


def revoke_all(conn: sqlite3.Connection, scope: AgentScope, now: str, *, by: str, why: str) -> list[sqlite3.Row]:
    """The owner's switch (0.13.0): every unlock that stands is taken back at once; what they held, and (0.14.0) what
    they approved that hasn't begun, waits for the owner. Returns the grants taken back."""
    taken = [g for g in grants(conn, scope) if g["level"] != "manual"]
    for g in taken:
        set_grant(
            conn,
            scope,
            int(g["milestone_id"]),
            str(g["rule"]),
            "manual",
            now,
            per_day=int(g["per_day"]),
            budget=int(g["budget"]),
            by=by,
            why=why,
        )
    return taken


def revoke_everywhere(conn: sqlite3.Connection, now: str, *, by: str, why: str) -> list[sqlite3.Row]:
    """0.14.0: the kill switch takes back every unlock that stands, in every mode and session."""
    taken = []
    for r in conn.execute("SELECT DISTINCT mode, session FROM policy_grants ORDER BY mode, session").fetchall():
        scope = AgentScope(str(r["mode"]), int(r["session"]), 0)  # a grant names no life
        taken += revoke_all(conn, scope, now, by=by, why=why)
    return taken


def veto_until(conn: sqlite3.Connection, approval_id: int) -> str | None:
    """When a request held for its veto window is approved (None if it isn't held, or its unlock was taken back)."""
    row = conn.execute(
        "SELECT veto_until, grant_id FROM policy_uses WHERE approval_id = ? AND approved_at IS NULL", (approval_id,)
    ).fetchone()
    held = row is not None and row["veto_until"] and _stands(conn, int(row["grant_id"]))
    return str(row["veto_until"]) if held else None
