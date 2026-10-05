"""The policy engine (0.13.0): how much the owner lets Ember's code carry out without their click.

Every action waited for the owner (about 46 clicks a day live), also the small, safe ones. Now the owner can grant a
milestone they back autonomy for a few rules (RULES), all off by default:

* qa_fix: a change that only brings a live listing of Ember's up to the QA registry's photos (qa.MIN_PHOTOS;
  0.15.0: distinct photos, none a copy of another);
* price_change: a change of a live listing's price only, within PRICE_BAND;
* listing_variant: a new listing in a backed leg (its venture building or live) once the owner approved
  VARIANTS_FIRST of that leg's listings without changes;
* deactivate: taking a listing of Ember's off Etsy (nothing else changes);
* email_reply: an email that answers someone in a thread they started (Ember's footer always says an AI wrote it).

Each grant has a level (LEVELS): manual (as before), veto_window (approved VETO_HOURS after the request unless the
owner decided first) or auto (approved at once), a daily limit and a budget of actions. A request that fits a granted
rule is carried by the grant of a milestone it belongs to (``apply``). 0.15.0: that is what the request acts on, not
the cycle's focus (``carrier``): a listing of the milestone's project, or of its venture's projects; an email reply
belongs to a milestone of no project or venture. Every request that fits a rule is kept as a candidate. Ember's code
revokes a grant (``keep``) on an unclear result, a spent budget, a milestone closed (done, missed or dropped) or the
owner's veto (rejecting a request during its window), and only proposes a promotion (``suggestions``): a rule whose
requests the owner approved PROMOTE_AFTER times without changes or a comment in PROMOTE_DAYS. The owner decides.

Whatever is unlocked, no unlock carries a NEVER request (never.py: an account, money, a first contact, a first
publication, a community post, tax, VAT, a Gewerbe or a contract, what only the owner carries out), and only the owner
unlocks. The database checks the same (migration 0051); what it refuses is undone alone and waits for the owner.

0.15.0: an unlock carries only a request that passes its class's QA (qa.CHECKS), and unlocks act only while Ember
knows its owner (owner_user_ids) outside safe mode (``off``). An unlock taken back (by the owner, the kill switch or
Ember's code) also stops what it approved and Ember's code hasn't begun: it waits for the owner again (``_stop``).
A spent budget only ends an unlock: what it approved runs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ..economy.clock import Clock, to_iso
from ..integrations import connectors, etsy, etsy_publisher, mailstore, qa
from ..products import images
from . import never
from .store import AgentScope


@dataclass(frozen=True)
class Rule:
    name: str
    action_class: str  # connectors.CLASSES
    label: str
    short: str  # 0.16.3 (analysis bug 5): its name in the plan's ROADMAP, which says every plan what is unlocked


RULES: dict[str, Rule] = {
    r.name: r
    for r in (
        Rule("qa_fix", "etsy.edit_listing", f"QA fixes: photos up to {qa.MIN_PHOTOS} on a live listing", "QA fixes"),
        Rule(
            "price_change",
            "etsy.edit_listing",
            "price changes within 15% of the approved price on a live listing",
            "price changes",
        ),
        Rule(
            "listing_variant",
            "etsy.create_listing",
            "new listings in a backed leg after 5 approved unchanged",
            "new listings",
        ),
        Rule("deactivate", "etsy.deactivate", "taking a listing of Ember's off Etsy", "taking listings off Etsy"),
        Rule("email_reply", "email.reply", "email replies in threads the other person started", "email replies"),
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
TAKEN_BACK = "you took back every unlock"  # 0.13.0: the owner's switch (a grant's why, said to the owner)
KILLED = "you used the kill switch"  # 0.15.0: it takes back every unlock too
# 0.16.3 (analysis bug 5): how the agent hears the owner's two switches in its news (their why is said to the owner)
SWITCHES = {TAKEN_BACK: "with Take back every unlock", KILLED: "with the kill switch"}
# 0.15.0: a normal end, not a take-back: the one revocation that isn't for cause (promotions may come back, and what
# it approved runs). Always SPENT.format(budget=...).
SPENT = "its budget of {budget} actions is spent"


def _action(row: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        data = json.loads(row["action"] or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def off(owner_ids: Sequence[str], safe_mode: bool) -> str:
    """0.15.0: why unlocks don't act now ("" when they do). Only while owner_user_ids names the owner, so that only
    they unlocked, and never in safe mode (its options are built-in defaults)."""
    if safe_mode:
        return "the app runs in safe mode"
    return "" if owner_ids else "owner_user_ids names no one"


def short(row: Mapping[str, Any], read: Callable[[str], bytes] | None = None) -> list[str]:
    """0.15.0: what a request falls short of by its class's QA (qa.CHECKS, as its tool and card show it; ``read`` reads
    a workspace file, so photos that repeat one another count once)."""
    action = _action(row)
    if action is None:
        return ["its action isn't readable"]
    kind = connectors.class_of(row["executor"], action, row["type"]).name
    try:
        if row["executor"] == "etsy_listing":
            listing = etsy.listing_from_action(action)
            looks = images.looks(read, [(u.path, u.sha256) for u in listing.photos]) if read else None
            return qa.defects(kind, listing, looks)
        if row["executor"] == "etsy_edit":
            edit = etsy.edit_from_action(action)
            photos = edit.photos or ()
            looks = images.looks(read, [(u.path, u.sha256) for u in photos]) if read and photos else None
            return qa.defects(kind, edit, looks)
    except etsy.EtsyError as exc:
        return [str(exc)]
    return qa.defects(kind, action)


def _started_by_them(conn: sqlite3.Connection, scope: AgentScope, action: Mapping[str, Any]) -> bool:
    """An email answers someone in a thread they started: its thread's first message is theirs, a person's email
    (0.15.0: mailstore.person, its sender verified)."""
    to = str(action.get("to") or "").lower()
    chain = str(action.get("references") or action.get("in_reply_to") or "").split()
    if not to or not chain:
        return False
    where, params = scope.where()
    root = conn.execute(
        f"SELECT {mailstore.person()} AS person, from_addr FROM emails WHERE {where} AND message_id = ?"
        " ORDER BY id LIMIT 1",
        (*params, chain[0]),
    ).fetchone()
    return root is not None and bool(root["person"]) and str(root["from_addr"]).lower() == to


def _clean(alias: str = "") -> str:
    """0.15.0: an approval without changes and without a comment (an objection like "approved, but the pictures look
    alike" is no clean approval), as promotions and listing variants count them."""
    a = f"{alias}." if alias else ""
    return f"{a}final_payload IS NULL AND COALESCE(trim({a}decision_comment), '') = ''"


def _clean_approvals(conn: sqlite3.Connection, scope: AgentScope, venture_id: int) -> int:
    """The owner's approvals without changes or a comment of listings for a venture."""
    where, params = scope.where()
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM approvals WHERE {where} AND executor = 'etsy_listing' AND venture_id = ?"
            f" AND status IN ('approved', 'done') AND {_clean()} AND decided_by IS NOT ?",
            (*params, venture_id, POLICY_BY),
        ).fetchone()[0]
    )


def approved_price(conn: sqlite3.Connection, scope: AgentScope, listing_id: int) -> str | None:
    """0.15.0: the price a listing of Ember's had when the owner last approved one: after the newest price change they
    approved (their Undo included), else as it was listed. The price band is measured from it, so the changes an unlock
    carries can't add up past PRICE_BAND (three cuts of 14% took EUR 4.50 to 2.86 in a day)."""
    where, params = scope.where("e")
    changed = conn.execute(
        f"SELECT e.listing FROM etsy_edits e JOIN approvals a ON a.id = e.approval_id WHERE {where}"
        " AND e.listing_id = ? AND e.status IN ('done', 'partial') AND e.listing IS NOT NULL"
        " AND json_extract(a.action, '$.price') IS NOT NULL AND a.decided_by IS NOT ? ORDER BY e.id DESC LIMIT 1",
        (*params, listing_id, POLICY_BY),
    ).fetchone()
    try:
        if changed is not None:
            return etsy.listing_from_action(changed["listing"]).price
        listed = etsy_publisher.listing_row(conn, scope, listing_id)
        if listed is not None:
            approval = conn.execute("SELECT * FROM approvals WHERE id = ?", (listed["approval_id"],)).fetchone()
            return etsy_publisher.approved_listing(approval).price
    except etsy.EtsyError:
        return None
    return None


def match(
    conn: sqlite3.Connection, scope: AgentScope, row: Mapping[str, Any], read: Callable[[str], bytes] | None = None
) -> str | None:
    """The rule a request fits, or None. ``read`` reads a workspace file (0.15.0: a photo that repeats another)."""
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
            try:  # 0.15.0: from the price the owner approved, not the last change (which an unlock may have made)
                old, new = Decimal(approved_price(conn, scope, edit.listing_id) or "0"), Decimal(edit.price)
            except InvalidOperation:
                return None
            return "price_change" if old > 0 and abs(new / old - 1) <= PRICE_BAND else None
        if parts == ["photos"] and edit.photos is not None:
            # 0.15.0: distinct photos, as the QA registry counts them: copies of one photo fix nothing.
            looks = images.looks(read, [(u.path, u.sha256) for u in edit.photos]) if read else None
            copies = qa.repeats(edit.photos, looks)
            return "qa_fix" if qa.distinct(current.photos) < qa.MIN_PHOTOS <= len(edit.photos) and not copies else None
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


def standing(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, list[sqlite3.Row]]:
    """0.16.3 (analysis bug 5): the unlocks that stand, by milestone (each rule's newest grant, not manual, of an open
    milestone), in RULES' order. What the planner and the cards say is unlocked comes from them: 0.13.0 wrote each
    unlock into the milestone's note, and no take-back by Ember's code (the upgrade to 0.15.0's either) changed it."""
    where, params = scope.where("g")
    found: dict[int, list[sqlite3.Row]] = {}
    for g in conn.execute(
        f"SELECT g.* FROM policy_grants g JOIN milestones m ON m.id = g.milestone_id WHERE {where}"
        " AND g.level <> 'manual' AND m.status = 'open' AND g.id = (SELECT MAX(h.id) FROM policy_grants h"
        " WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule) ORDER BY g.milestone_id, g.id",
        params,
    ).fetchall():
        found.setdefault(int(g["milestone_id"]), []).append(g)
    order = list(RULES)
    return {mid: sorted(rows, key=lambda g: order.index(g["rule"])) for mid, rows in found.items()}


def unlocked(conn: sqlite3.Connection, scope: AgentScope, milestone_id: int) -> list[sqlite3.Row]:
    """0.21.0: a milestone's unlocks that stand (each rule's newest grant, not manual), open or closed. An unlock covers
    what its milestone is linked to, so while one stands its links stay (the tool refuses, migration 0075 too): the
    agent re-linked a milestone, and its unlock carried a request for another product line at once."""
    return [g for g in grants(conn, scope, milestone_id) if g["level"] != "manual"]


def venture_unlock(conn: sqlite3.Connection, scope: AgentScope, venture_id: int | None) -> int | None:
    """0.21.0: an open milestone of a venture (of no project) whose unlock stands: it covers the listings of the
    venture's projects (approvals_scope), so no project moves into or out of it (the tool refuses). None without one."""
    if venture_id is None:
        return None
    where, params = scope.where("m")
    found = conn.execute(
        f"SELECT m.id FROM milestones m JOIN policy_grants g ON g.milestone_id = m.id WHERE {where}"
        " AND m.venture_id = ? AND m.project_id IS NULL AND m.status = 'open' AND g.level <> 'manual'"
        " AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)"
        " ORDER BY m.id LIMIT 1",
        (*params, venture_id),
    ).fetchone()
    return int(found[0]) if found is not None else None


def granted_text(g: Mapping[str, Any], short: bool = False) -> str:
    """0.16.3 (analysis bug 5): one unlock in words: its rule, level, daily limit and budget (``short``: its rule's
    short name and level, as the plan's ROADMAP says it every plan)."""
    rule, level = RULES[g["rule"]], str(g["level"]).replace("_", " ")
    if short:
        return f"{rule.short} ({level})"
    return f"{rule.label} ({level}, at most {g['per_day']} a day, {g['budget']} in all)"


def unlocked_text(rows: Sequence[Mapping[str, Any]], short: bool = False) -> str:
    """0.16.3 (analysis bug 5): a milestone's unlocks that stand (``standing``) in words ("" when none does)."""
    return (", " if short else "; ").join(granted_text(g, short) for g in rows)


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
    stop: bool = True,
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
    if level == "manual" and stop:  # 0.15.0: in the same transaction, before an executor can begin what it approved
        _stop(conn, milestone_id, rule, now, why or "you took it back")
    return int(cursor.lastrowid)


def _stop(conn: sqlite3.Connection, milestone_id: int, rule: str, now: str, why: str) -> list[str]:
    """0.15.0: what the unlocks of a milestone's rule approved and Ember's code hasn't begun (no journal entry) waits
    for the owner again, saying why. The executors check the status when they begin; what began runs on, once. The
    same request waiting already: this one is closed instead. Returns what happened to each."""
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
            stopped.append(f"Request #{r['id']} waits for you again: its unlock was taken back before it ran")
        else:
            conn.execute(
                "UPDATE approvals SET status = 'failed', closed_at = ?, closed_by = ?, result_note = ?,"
                " version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status = 'approved'",
                (now, REVOKED_BY, f"{said} The same request waits as #{twin['id']}."[:2000], r["id"]),
            )
            stopped.append(f"Request #{r['id']} closed: the same request waits for you as #{twin['id']}")
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
    """Whether a grant still stands: the newest of its milestone and rule, not manual (not taken back), and (0.15.0)
    its milestone still open."""
    row = conn.execute(
        "SELECT g.level, m.status, (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id"
        " AND h.rule = g.rule) AS newest FROM policy_grants g JOIN milestones m ON m.id = g.milestone_id"
        " WHERE g.id = ?",
        (grant_id,),
    ).fetchone()
    return row is not None and row["level"] != "manual" and row["newest"] == grant_id and row["status"] == "open"


def _covers(conn: sqlite3.Connection, approval_id: int, grant_id: int) -> bool:
    """0.15.0: whether a grant's milestone covers what a request acts on (an unlock of 0.13.0 held what its cycle aimed
    at, whatever it touched)."""
    return (
        conn.execute(
            "SELECT 1 FROM approvals_scope s JOIN policy_grants g ON g.milestone_id = s.milestone_id"
            " WHERE s.approval_id = ? AND g.id = ?",
            (approval_id, grant_id),
        ).fetchone()
        is not None
    )


def carrier(
    conn: sqlite3.Connection, scope: AgentScope, approval_id: int, rule: str
) -> tuple[int | None, sqlite3.Row | None]:
    """0.15.0: the milestone whose unlock carries a request, by what the request acts on (the view approvals_scope,
    which the database checks too): the open milestones it belongs to, the one its cycle worked for first, then a
    project's before a venture's, the oldest first. The first whose grant for the rule stands, with it; else the first
    of them and None (no milestone: None)."""
    covering = [
        int(r[0])
        for r in conn.execute(
            "SELECT m.id FROM approvals_scope s JOIN milestones m ON m.id = s.milestone_id JOIN approvals a"
            " ON a.id = s.approval_id WHERE s.approval_id = ? AND m.status = 'open'"
            " ORDER BY m.id IS NOT a.milestone_id, m.project_id IS NULL, m.id",
            (approval_id,),
        )
    ]
    for milestone_id in covering:
        granted = grant(conn, scope, milestone_id, rule)
        if granted is not None:
            return milestone_id, granted
    return (covering[0] if covering else None), None


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


def apply(
    conn: sqlite3.Connection,
    scope: AgentScope,
    approval_id: int,
    clock: Clock,
    off: str = "",
    read: Callable[[str], bytes] | None = None,
) -> str:
    """A request just made: kept as a candidate if it fits a rule, and carried by the grant of the milestone it belongs
    to (0.15.0: by what it acts on, ``carrier``), if one stands and has room (0.15.0: and unlocks act, ``off`` empty,
    and it passes NEVER and its QA). ``read`` reads a workspace file (0.15.0: a photo that repeats another). Returns
    what the agent is told ("" when nothing changes)."""
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    rule = match(conn, scope, row, read) if row is not None and row["status"] == "pending" else None
    if row is None or rule is None:
        return ""
    now = to_iso(clock.now())
    milestone_id, granted = carrier(conn, scope, approval_id, rule)
    conn.execute(
        "INSERT OR IGNORE INTO policy_candidates (approval_id, rule, milestone_id, created_at) VALUES (?, ?, ?, ?)",
        (approval_id, rule, milestone_id, now),
    )
    if granted is None:
        return ""
    label = RULES[rule].label
    if off:
        return f" It waits for your owner: unlocks are off while {off}."
    if never.reasons(conn, row):  # 0.15.0: which kind is the owner's to see (it named the words to avoid)
        return " It waits for your owner whatever they unlocked (never automatic)."
    falls = short(row, read)
    if falls:
        return f" It waits for your owner: an unlock carries only what passes QA ({'; '.join(falls)})."
    total, today = used(conn, int(granted["id"]), clock)
    if total >= int(granted["budget"]):
        return ""  # spent: keep() revokes it
    if today >= int(granted["per_day"]):
        return f" Your owner's unlock for {label} has carried {today} today, its limit: this one waits for them."
    unlock = f"your owner unlocked {label} for milestone #{milestone_id}"
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


def _held_back(conn: sqlite3.Connection, row: Mapping[str, Any]) -> str:
    """0.15.0: why no unlock carries a request now ("" when one may): NEVER, or its QA."""
    found = never.reasons(conn, row)
    if found:
        return f"never automatic for {never.text(found)}"
    falls = short(row)
    return f"an unlock carries only what passes QA ({'; '.join(falls)})" if falls else ""


def run_due(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, off: str = "") -> list[str]:
    """Approve the requests whose veto window has passed while the owner didn't decide; returns what happened. 0.15.0:
    none while unlocks are off, and none that falls short of NEVER or its QA: that one waits for the owner, saying
    why."""
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
            continue  # the owner decided first, or the unlock ended (taken back, its milestone closed): it waits
        if not _covers(conn, int(use["approval_id"]), int(use["grant_id"])):
            continue  # 0.15.0: held by an unlock of 0.13.0 whose milestone doesn't cover it: it waits
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (use["approval_id"],)).fetchone()
        held_back = _held_back(conn, row)
        if held_back:  # said once, on its card too
            said = f"Held by your unlock, but not approved when its veto window passed: {held_back}. It waits for you."
            if row["decision_comment"] != said[:2000]:
                conn.execute(
                    "UPDATE approvals SET decision_comment = ?, version = version + 1 WHERE id = ?"
                    " AND status = 'pending'",
                    (said[:2000], row["id"]),
                )
                happened.append(f"Request #{row['id']} waits for you: {held_back}")
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
    """Revoke the grants whose milestone closed (0.15.0: done too, so success ends an unlock visibly), whose budget
    is spent, which carried a request that ended unclear, whose request the owner vetoed, or (email replies) whose
    reply the person answered by asking to stop. Returns what happened, with the requests it held for their veto
    window, which wait for the owner now. 0.15.0: what a revoked grant approved and Ember's code hasn't begun waits
    for the owner too, unless its budget is spent (a normal end)."""
    now = to_iso(clock.now())
    happened = []
    for g in grants(conn, scope):
        if g["level"] == "manual":
            continue
        why = _revocation(conn, g, clock)
        if why:
            milestone_id, rule = int(g["milestone_id"]), str(g["rule"])
            set_grant(conn, scope, milestone_id, rule, "manual", now, by=REVOKED_BY, why=why[:300], stop=False)
            held = [
                f"#{r[0]}"
                for r in conn.execute(
                    "SELECT u.approval_id FROM policy_uses u JOIN approvals a ON a.id = u.approval_id"
                    " WHERE u.grant_id = ? AND u.approved_at IS NULL AND a.status = 'pending' ORDER BY u.id",
                    (g["id"],),
                )
            ]
            waits = f"; what it held waits for you: request {', '.join(held)}" if held else ""
            happened.append(
                f"Ember's code revoked the unlock for {RULES[rule].label} (milestone #{milestone_id}): {why}" + waits
            )
            if why != SPENT.format(budget=g["budget"]):  # 0.15.0: a spent budget ends it; what it approved runs
                happened += _stop(conn, milestone_id, rule, now, why[:300])
    return happened


def _revocation(conn: sqlite3.Connection, g: sqlite3.Row, clock: Clock) -> str:
    milestone = conn.execute("SELECT status FROM milestones WHERE id = ?", (g["milestone_id"],)).fetchone()
    if milestone is None or milestone["status"] in ("done", "missed", "dropped"):
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
            return f"your owner cancelled request #{use['approval_id']}"  # 0.15.0: before it ran, a veto too
        if use["carried"] == "unclear":
            return f"request #{use['approval_id']} ended unclear"
    if g["rule"] == "email_reply":  # 0.13.0 (Phase E1): the channel's kill rule
        stopped = conn.execute(
            "SELECT u.approval_id FROM policy_uses u JOIN approvals a ON a.id = u.approval_id"
            " JOIN email_suppressions s ON s.mode = a.mode AND s.session = a.session"
            " AND s.address = lower(json_extract(a.action, '$.to')) WHERE u.grant_id = ? AND u.approved_at IS NOT NULL"
            " AND s.since >= u.approved_at ORDER BY u.id LIMIT 1",
            (g["id"],),
        ).fetchone()
        if stopped is not None:  # 0.15.0: never their address (the digest goes to the open sensor and notifications)
            return f"the person you answered in request #{stopped['approval_id']} asked to stop"
    total, _ = used(conn, int(g["id"]), clock)
    holding = conn.execute(  # 0.15.0: a spent budget ends it once what it holds for its veto window is decided
        "SELECT u.approval_id FROM policy_uses u JOIN approvals a ON a.id = u.approval_id WHERE u.grant_id = ?"
        " AND u.level = 'veto_window' AND u.approved_at IS NULL AND a.status = 'pending'",
        (g["id"],),
    ).fetchall()
    if total >= int(g["budget"]) and not any(_holds(conn, int(g["id"]), int(u["approval_id"])) for u in holding):
        return SPENT.format(budget=g["budget"])
    return ""


def suggestions(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> list[dict[str, Any]]:
    """The promotions Ember's code proposes: a rule whose requests for an open milestone the owner approved
    PROMOTE_AFTER times without changes or a comment (0.15.0) in PROMOTE_DAYS, while nothing is granted for it and
    (0.15.0) Ember's code didn't take its unlock back for cause in PROMOTE_DAYS (only a spent budget isn't a cause;
    the owner's own "Ask me" doesn't count). The owner decides."""
    since = to_iso(clock.now() - timedelta(days=PROMOTE_DAYS))
    where, params = scope.where("a")
    rows = conn.execute(
        "SELECT c.rule, c.milestone_id, COUNT(*) AS approved FROM policy_candidates c JOIN approvals a"
        f" ON a.id = c.approval_id JOIN milestones m ON m.id = c.milestone_id WHERE {where} AND m.status = 'open'"
        f" AND a.status IN ('approved', 'done') AND {_clean('a')} AND a.decided_by IS NOT ?"
        " AND c.created_at >= ? AND NOT EXISTS (SELECT 1 FROM policy_grants g WHERE g.milestone_id = c.milestone_id"
        " AND g.rule = c.rule AND g.level = 'manual' AND g.by = ? AND g.created_at >= ?"
        " AND COALESCE(g.why, '') NOT LIKE ?)"
        " GROUP BY c.rule, c.milestone_id HAVING COUNT(*) >= ? ORDER BY c.milestone_id, c.rule",
        (*params, POLICY_BY, since, REVOKED_BY, since, SPENT.format(budget="%"), PROMOTE_AFTER),
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
        and fits(conn, int(r["milestone_id"]), str(r["rule"]))
    ]


def fits(conn: sqlite3.Connection, milestone_id: int, rule: str) -> bool:
    """0.15.0: whether a milestone's scope can ever cover a rule's requests (approvals_scope): email replies only on a
    milestone of no project and no venture, listings only on one of a project or venture."""
    row = conn.execute("SELECT project_id, venture_id FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    if row is None:
        return False
    linked = row["project_id"] is not None or row["venture_id"] is not None
    return linked != (RULES[rule].action_class == "email.reply")


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
                "fits": fits(conn, milestone_id, rule.name),
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


def _holds(conn: sqlite3.Connection, grant_id: int, approval_id: int) -> bool:
    """Whether an unlock still holds a request for its veto window: it stands, and (0.15.0) may carry the request."""
    if not _stands(conn, grant_id):
        return False
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return row is not None and not _held_back(conn, row)


def held(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The requests unlocks hold for their veto window now (waiting, under an unlock that stands and may carry them),
    the first due first."""
    where, params = scope.where("a")
    rows = conn.execute(
        f"SELECT u.approval_id, u.veto_until, u.grant_id FROM policy_uses u JOIN approvals a ON a.id = u.approval_id"
        f" WHERE {where} AND u.level = 'veto_window' AND u.approved_at IS NULL AND a.status = 'pending'"
        " ORDER BY u.veto_until, u.id",
        params,
    ).fetchall()
    return [r for r in rows if _holds(conn, int(r["grant_id"]), int(r["approval_id"]))]


def revoke_all(conn: sqlite3.Connection, scope: AgentScope, now: str, *, by: str, why: str) -> list[sqlite3.Row]:
    """The owner's switch (0.13.0): every unlock that stands is taken back at once; what they held, and (0.15.0) what
    an unlock approved that hasn't begun (one that ended with its budget too), waits for the owner. Returns the grants
    taken back."""
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
    for g in grants(conn, scope):  # 0.15.0: all manual now; the rest of what an unlock approved
        _stop(conn, int(g["milestone_id"]), str(g["rule"]), now, why)
    return taken


def revoke_everywhere(conn: sqlite3.Connection, now: str, *, by: str, why: str) -> list[sqlite3.Row]:
    """0.15.0: the kill switch takes back every unlock that stands, in every mode and session."""
    taken = []
    for r in conn.execute("SELECT DISTINCT mode, session FROM policy_grants ORDER BY mode, session").fetchall():
        scope = AgentScope(str(r["mode"]), int(r["session"]), 0)  # a grant names no life
        taken += revoke_all(conn, scope, now, by=by, why=why)
    return taken


def veto_until(conn: sqlite3.Connection, approval_id: int) -> str | None:
    """When a request held for its veto window is approved (None if it isn't held, its unlock was taken back, or 0.15.0
    it may not carry the request: NEVER or its QA)."""
    row = conn.execute(
        "SELECT veto_until, grant_id FROM policy_uses WHERE approval_id = ? AND approved_at IS NULL", (approval_id,)
    ).fetchone()
    held = row is not None and row["veto_until"] and _holds(conn, int(row["grant_id"]), approval_id)
    return str(row["veto_until"]) if held and _covers(conn, approval_id, int(row["grant_id"])) else None


def ended(conn: sqlite3.Connection, approval_id: int) -> str | None:
    """0.15.0: why a request an unlock held for its veto window waits for the owner after all (None: it doesn't): its
    milestone closed, the unlock was taken back, or its milestone doesn't cover it. For its card."""
    row = conn.execute(
        "SELECT u.grant_id, g.milestone_id, g.rule, m.status FROM policy_uses u"
        " JOIN approvals a ON a.id = u.approval_id JOIN policy_grants g ON g.id = u.grant_id"
        " JOIN milestones m ON m.id = g.milestone_id WHERE u.approval_id = ? AND u.approved_at IS NULL"
        " AND a.status = 'pending'",
        (approval_id,),
    ).fetchone()
    if row is None:
        return None
    if _stands(conn, int(row["grant_id"])):
        if _covers(conn, approval_id, int(row["grant_id"])):
            return None
        return f"milestone #{row['milestone_id']} doesn't cover what it acts on, so its unlock doesn't carry it"
    if row["status"] != "open":
        return f"milestone #{row['milestone_id']} was {row['status']}, so the unlock that held it ended"
    newest = conn.execute(
        "SELECT why FROM policy_grants WHERE milestone_id = ? AND rule = ? ORDER BY id DESC LIMIT 1",
        (row["milestone_id"], row["rule"]),
    ).fetchone()
    return f"the unlock that held it was taken back ({newest['why'] or 'by you'})"
