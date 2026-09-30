"""The audit (0.13.0): what Ember's code did, the owner's Undo, a daily digest, and taking back every unlock.

* ``feed``: the action journal's newest entries (integrations/connectors.py) for the owner: what each did, before and
  after, who decided it (the owner's click, their unlock, their Undo, or Ember's code on its own), how it ended and
  whether it can be undone;
* ``undo``: the owner's Undo of an action on one of Ember's listings, as a request of theirs approved at once, which
  Ember's code carries out like any change: deactivating a listing it created, changing a changed listing back,
  renewing a deactivated one (Etsy's fee), turning an automatic renewal off. It is journaled like any action and
  linked to what it undoes (action_undos). Only the newest action on a listing can be undone, and not while another
  change of it waits; an email can't be unsent. 0.13.0 (Phase E2): a pin's Undo deletes it at Pinterest;
* ``digest``: one of the owner's days: what Ember's code carried out and on whose decision, what the unlocks approved,
  hold and lost, and what waited whatever was unlocked (NEVER). Written once after the day ended (owner_digests),
  shown on the Approvals tab, in the System log and in the sensors;
* taking back every unlock at once is ``policy.revoke_all``, the owner's switch.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, timedelta
from typing import Any

from ..economy.clock import Clock, from_iso, to_iso
from ..integrations import connectors, etsy, etsy_publisher, pinterest, printify_publisher
from . import never, policy, store
from .store import AgentScope

FEED = 30  # entries on the Approvals tab
# What the owner's Undo does, by what would undo an action (connectors.undo_of): the button, the request's title and
# what it costs.
UNDO = {
    "deactivate": ("Deactivate it", "deactivate", "none"),
    "restore": ("Change it back", "change back", "none: Etsy charges nothing for changing a listing"),
    "renew": (
        f"Renew it ({etsy.RENEWAL_FEE})",
        "renew",
        f"Etsy's listing fee for the renewal ({etsy.RENEWAL_FEE} at most), and its fees on each sale",
    ),
    "auto_renew_off": ("Turn automatic renewal off", "turn off automatic renewal of", "none"),
    "delete_pin": ("Delete the pin", "delete", "none"),  # 0.13.0 (Phase E2)
    "delete_product": ("Delete the product", "delete", "none"),  # 0.13.0 (Phase E4)
}
WHO = {
    "unlock": "your unlock",
    "owner": "you approved it",
    "undo": "your Undo",
    "code": "Ember's code on its own",
}
_PARTS = (
    ("title", "Title"),
    ("price", "Price"),
    ("tags", "Tags"),
    ("category", "Category"),
    ("description", "Description"),
    ("photos", "Photos"),
    ("files", "Files"),
    ("auto_renew", "Automatic renewal"),
)


class Refused(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def _load(raw: str | None) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _who(row: sqlite3.Row) -> str:
    if row["undoes"] is not None:
        return "undo"
    if row["approval_id"] is None:
        return "code"
    return "unlock" if row["decided_by"] == policy.POLICY_BY else "owner"


def _short(part: str, value: Any, currency: str) -> str:
    if value is None:
        return "none"
    if part in ("photos", "files"):
        names = ", ".join(str(u.get("path", "")).rsplit("/", 1)[-1] for u in value if isinstance(u, dict))
        return f"{len(value)}: {names}" if names else "0"
    if part == "tags":
        value = ", ".join(str(t) for t in value)
    if part == "auto_renew":
        return "on" if value else "off"
    if part == "price" and currency:
        value = f"{value} {currency}"
    text = " ".join(str(value).split())
    return text if len(text) <= 160 else text[:159] + "…"


def _changes(row: sqlite3.Row) -> list[dict[str, str]]:
    """What an action changed, part by part (before and after, short)."""
    if row["class"] == "etsy.auto_renew_off" and row["status"] in ("done", "partial", "simulated"):
        return [{"part": "Automatic renewal", "before": "on", "after": "off"}]
    before, after = _load(row["before"]), _load(row["after"])
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    currency = str(after.get("currency") or before.get("currency") or "")
    return [
        {
            "part": label,
            "before": _short(key, before.get(key), currency),
            "after": _short(key, after.get(key), currency),
        }
        for key, label in _PARTS
        if (key in before or key in after) and before.get(key) != after.get(key)
    ]


def _journal(
    conn: sqlite3.Connection, scope: AgentScope, where_more: str = "", params_more: tuple[Any, ...] = ()
) -> Any:
    where, params = scope.where("j")
    return conn.execute(
        "SELECT j.*, a.title AS request_title, a.decided_by, u.journal_id AS undoes FROM action_journal j"
        " LEFT JOIN approvals a ON a.id = j.approval_id LEFT JOIN action_undos u ON u.approval_id = j.approval_id"
        f" WHERE {where}{where_more}",
        (*params, *params_more),
    )


def _last_undo(conn: sqlite3.Connection, journal_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT u.approval_id, a.status, a.result_note, (SELECT COUNT(*) FROM action_journal j WHERE"
        " j.approval_id = u.approval_id AND j.status <> 'failed') AS made FROM action_undos u JOIN approvals a"
        " ON a.id = u.approval_id WHERE u.journal_id = ? ORDER BY u.id DESC LIMIT 1",
        (journal_id,),
    ).fetchone()


def _listing(raw: str | None) -> etsy.Listing | None:
    try:
        return etsy.listing_from_action(raw) if raw else None
    except (etsy.EtsyError, ValueError):
        return None


def _restore(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> etsy.Edit | None:
    """The change that brings a listing back to how it was before an action changed it (None: nothing to change)."""
    before, after = _listing(row["before"]), _listing(row["after"])
    listing_id = int(row["subject"])
    current = etsy_publisher.current_listing(conn, scope, listing_id)
    if before is None or after is None or current is None:
        return None
    changes: dict[str, Any] = {
        name: getattr(before, name)
        for name in ("title", "description", "price", "tags", "photos", "files")
        if getattr(before, name) != getattr(after, name)
    }
    if before.taxonomy_id != after.taxonomy_id:
        changes["taxonomy_id"], changes["category"] = before.taxonomy_id, before.category
    return etsy.Edit(listing_id=listing_id, currency=current.currency, **changes) if changes else None


def _why_not(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> str | None:
    """Why the owner can't undo an action now (None: they can)."""
    undo = _load(row["undo"])
    if row["status"] == "running":
        return "it is being carried out"
    if not isinstance(undo, dict) or undo.get("action") not in UNDO:
        if row["class"] == "email.send":
            return "an email can't be unsent"
        if row["status"] == "failed":
            return "nothing was done"
        if row["status"] == "unclear":
            place = {"pinterest": "Pinterest", "printify": "Printify"}.get(str(row["class"]).split(".")[0], "Etsy")
            return f"it is unclear what happened: check it at {place}"
        return "Ember's code can't undo it"
    last = _last_undo(conn, int(row["id"]))
    if last is not None and not (last["status"] == "failed" and last["made"] == 0):
        return "it is undone" if last["status"] == "done" else "your Undo of it is under way"
    where, params = scope.where()
    if undo["action"] == "delete_pin":  # 0.13.0 (Phase E2)
        pin = conn.execute(
            f"SELECT status FROM pinterest_pins WHERE {where} AND pin_id = ?", (*params, row["subject"])
        ).fetchone()
        return None if pin is not None and pin["status"] == "active" else "the pin isn't on Pinterest anymore"
    if undo["action"] == "delete_product":  # 0.13.0 (Phase E4)
        product = conn.execute(
            f"SELECT status FROM printify_products WHERE {where} AND product_id = ?", (*params, row["subject"])
        ).fetchone()
        live = product is not None and product["status"] in printify_publisher.LIVE
        return None if live else "the product isn't at Printify anymore"
    later = conn.execute(
        f"SELECT id FROM action_journal WHERE {where} AND subject = ? AND class LIKE 'etsy.%' AND id > ?"
        " AND status <> 'failed' ORDER BY id LIMIT 1",
        (*params, row["subject"], row["id"]),
    ).fetchone()
    if later is not None:
        return f"a later action changed this listing (#{later['id']}): undo that one first"
    listing_id = int(row["subject"])
    waiting = etsy_publisher.open_edit(conn, scope, listing_id)
    if waiting is not None:
        return f"change #{waiting} of this listing waits: decide it first"
    listing = etsy_publisher.listing_row(conn, scope, listing_id)
    if listing is None:
        return "it isn't one of Ember's live listings"
    state = etsy_publisher.etsy_state(listing)
    kind = undo["action"]
    if kind == "deactivate" and state != etsy.LIVE_STATE:
        return "it isn't live at Etsy anymore"
    if kind == "renew" and state not in etsy.RENEWABLE:
        return "it is live at Etsy again"
    if kind == "auto_renew_off" and not listing["auto_renew"]:
        return "its automatic renewal is off already"
    if kind == "restore" and _restore(conn, scope, row) is None:
        return "nothing to change back"
    return None


def _entry(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> dict[str, Any]:
    kind = connectors.CLASSES.get(str(row["class"]))
    undo = _load(row["undo"])
    last = _last_undo(conn, int(row["id"])) if undo is not None else None
    who = _who(row)
    return {
        "id": row["id"],
        "class": row["class"],
        "what": kind.what if kind else row["class"],
        "subject": row["subject"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "note": row["note"],
        "approval_id": row["approval_id"],
        "request_title": row["request_title"],
        "by": who,
        "by_text": WHO[who],
        "undoes": row["undoes"],
        "changes": _changes(row),
        "undo": {
            "label": UNDO[undo["action"]][0] if isinstance(undo, dict) and undo.get("action") in UNDO else None,
            "why_not": _why_not(conn, scope, row),
            "request": None
            if last is None
            else {"approval_id": last["approval_id"], "status": last["status"], "note": last["result_note"]},
        },
    }


def feed(conn: sqlite3.Connection, scope: AgentScope, limit: int = FEED) -> list[dict[str, Any]]:
    """What Ember's code did, the newest first."""
    rows = _journal(conn, scope, " ORDER BY j.id DESC LIMIT ?", (limit,)).fetchall()
    return [_entry(conn, scope, r) for r in rows]


def _latest_cycle(conn: sqlite3.Connection, scope: AgentScope) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) FROM cycles WHERE session = ? AND simulated = ?",
        (scope.session, 1 if scope.mode == "dry_run" else 0),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def undo(conn: sqlite3.Connection, scope: AgentScope, now: str, journal_id: int, by: str) -> tuple[int, str]:
    """The owner's Undo of an action: a request of theirs, approved at once, that Ember's code carries out in its next
    round. Returns the request and what it does. Raises Refused."""
    row = _journal(conn, scope, " AND j.id = ?", (journal_id,)).fetchone()
    if row is None:
        raise Refused("no such action", 404)
    why = _why_not(conn, scope, row)
    if why is not None:
        raise Refused(f"this action can't be undone: {why}")
    kind = _load(row["undo"])["action"]
    button, verb, cost = UNDO[kind]
    original, cycle_id = _filed_under(conn, scope, row)
    what = connectors.CLASSES[str(row["class"])].what if str(row["class"]) in connectors.CLASSES else row["class"]
    because = (
        f"Your owner's Undo of action #{journal_id} ({what}, {row['finished_at'][:16].replace('T', ' ')} UTC)."
        " Ember's code carries it out like any change they approved."
    )
    if kind == "delete_pin":  # 0.13.0 (Phase E2): Ember's code deletes the pin at Pinterest
        pin_id = str(row["subject"])
        approval_id = store.insert_approval(
            conn,
            scope,
            cycle_id,
            now,
            project_id=original["project_id"] if original is not None else None,
            payload=f"Delete pin {pin_id} from your Pinterest account: {pinterest.pin_url(pin_id)}",
            action=store.canonical({"pin_id": pin_id}),
            type="publish",
            title=f"Undo: delete pin {pin_id}"[:120],
            description=because,
            expected_cost=cost,
            expected_benefit="The pin is gone from Pinterest.",
            executor="pinterest_delete",
        )
        _approve(conn, now, by, button, journal_id, approval_id)
        return approval_id, f"delete the pin ({pin_id})"
    if kind == "delete_product":  # 0.13.0 (Phase E4): Ember's code deletes it at Printify, which takes its listing down
        product_id = str(row["subject"])
        approval_id = store.insert_approval(
            conn,
            scope,
            cycle_id,
            now,
            project_id=original["project_id"] if original is not None else None,
            payload=f"Delete product {product_id} at Printify (its Etsy listing goes with it)",
            action=store.canonical({"product_id": product_id}),
            type="sell",
            title=f"Undo: delete Printify product {product_id}"[:120],
            description=because,
            expected_cost=cost,
            expected_benefit="The product is gone from Printify and from the shop.",
            executor="printify_delete",
        )
        _approve(conn, now, by, button, journal_id, approval_id)
        return approval_id, f"delete the product ({product_id})"
    listing_id = int(row["subject"])
    current = etsy_publisher.current_listing(conn, scope, listing_id)
    listing = etsy_publisher.listing_row(conn, scope, listing_id)
    if current is None or listing is None:
        raise Refused("it isn't one of Ember's live listings")
    edit = {
        "deactivate": etsy.Edit(listing_id=listing_id, currency=current.currency, state="deactivate"),
        "renew": etsy.Edit(listing_id=listing_id, currency=current.currency, state="renew"),
        "auto_renew_off": etsy.Edit(listing_id=listing_id, currency=current.currency, auto_renew=False),
    }.get(kind) or _restore(conn, scope, row)
    assert edit is not None  # _why_not checked it
    approval_id = store.insert_approval(
        conn,
        scope,
        cycle_id,
        now,
        project_id=original["project_id"] if original is not None else None,
        payload=etsy.edit_payload(edit, current, etsy_publisher.state_text(listing)),
        action=store.canonical(edit.to_action()),
        type="sell",
        title=f"Undo: {verb} Etsy listing #{listing_id}"[:120],
        description=because,
        expected_cost=cost[:300],
        expected_benefit="The listing is as it was before that action.",
        executor="etsy_edit",
    )
    _approve(conn, now, by, button, journal_id, approval_id)
    return approval_id, f"{button.lower()} (Etsy listing #{listing_id})"


def _filed_under(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> tuple[sqlite3.Row | None, int]:
    """The request an action carried out (None: Ember's code acted on its own) and the cycle an Undo is filed under."""
    original = (
        conn.execute("SELECT * FROM approvals WHERE id = ?", (row["approval_id"],)).fetchone()
        if row["approval_id"] is not None
        else None
    )
    cycle_id = original["cycle_id"] if original is not None else _latest_cycle(conn, scope)
    if cycle_id is None:
        raise Refused("Ember has no cycle yet to file it under")
    return original, int(cycle_id)


def _approve(conn: sqlite3.Connection, now: str, by: str, button: str, journal_id: int, approval_id: int) -> None:
    """The Undo's request is the owner's, approved at once, and linked to what it undoes."""
    conn.execute(
        "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ?, decision_comment = ?,"
        " version = version + 1 WHERE id = ? AND status = 'pending'",
        (now, by, f"{button}: your Undo of action #{journal_id}.", approval_id),
    )
    conn.execute(
        "INSERT INTO action_undos (journal_id, approval_id, by, created_at) VALUES (?, ?, ?, ?)",
        (journal_id, approval_id, by[:60], now),
    )


# --- the daily digest ---


def _waited(conn: sqlite3.Connection, scope: AgentScope, start: str, end: str) -> int:
    """Requests of a day that fit an unlocked rule but waited for the owner whatever was unlocked (NEVER)."""
    where, params = scope.where("a")
    rows = conn.execute(
        f"SELECT a.* FROM policy_candidates c JOIN approvals a ON a.id = c.approval_id WHERE {where}"
        " AND c.created_at >= ? AND c.created_at < ? AND EXISTS (SELECT 1 FROM policy_grants g WHERE"
        " g.milestone_id = c.milestone_id AND g.rule = c.rule AND g.level <> 'manual' AND g.id = (SELECT MAX(h.id)"
        " FROM policy_grants h WHERE h.milestone_id = c.milestone_id AND h.rule = c.rule"
        " AND h.created_at <= c.created_at))",
        (*params, start, end),
    ).fetchall()
    return sum(1 for r in rows if never.reasons(conn, r))


def digest(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, day: date) -> tuple[str, dict[str, Any]]:
    """One of the owner's days, in words and numbers."""
    start, end = to_iso(clock.day_start(day)), to_iso(clock.day_start(day + timedelta(days=1)))
    actions = _journal(conn, scope, " AND j.finished_at >= ? AND j.finished_at < ?", (start, end)).fetchall()
    by = Counter(_who(r) for r in actions)
    trouble = Counter(str(r["status"]) for r in actions if r["status"] in ("failed", "partial", "unclear"))
    where, params = scope.where("a")
    approved = dict(
        conn.execute(
            f"SELECT u.level, COUNT(*) FROM policy_uses u JOIN approvals a ON a.id = u.approval_id WHERE {where}"
            " AND u.approved_at >= ? AND u.approved_at < ? GROUP BY u.level",
            (*params, start, end),
        ).fetchall()
    )
    held = policy.held(conn, scope)
    grant_where, grant_params = scope.where("g")
    revoked = conn.execute(
        f"SELECT g.rule, g.milestone_id, g.why, g.by FROM policy_grants g WHERE {grant_where} AND g.level = 'manual'"
        " AND g.created_at >= ? AND g.created_at < ? AND EXISTS (SELECT 1 FROM policy_grants p WHERE"
        " p.milestone_id = g.milestone_id AND p.rule = g.rule AND p.id < g.id AND p.level <> 'manual' AND p.id ="
        " (SELECT MAX(q.id) FROM policy_grants q WHERE q.milestone_id = g.milestone_id AND q.rule = g.rule AND"
        " q.id < g.id)) ORDER BY g.id",
        (*grant_params, start, end),
    ).fetchall()
    waited = _waited(conn, scope, start, end)
    data = {
        "actions": dict(by),
        "trouble": dict(trouble),
        "approved_by_unlocks": {"auto": approved.get("auto", 0), "veto_window": approved.get("veto_window", 0)},
        "held": len(held),
        "first_held_until": held[0]["veto_until"] if held else None,
        "taken_back": [{"rule": r["rule"], "milestone_id": r["milestone_id"], "why": r["why"]} for r in revoked],
        "waited_never": waited,
    }
    return _digest_text(day, data, clock), data


def _digest_text(day: date, data: dict[str, Any], clock: Clock) -> str:
    by: dict[str, int] = data["actions"]
    total = sum(by.values())
    parts = []
    if total:
        whose = [
            f"{by[k]} {words}"
            for k, words in (
                ("unlock", "on your unlocks"),
                ("code", "on its own"),
                ("owner", "you approved"),
                ("undo", "your Undos"),
            )
            if by.get(k)
        ]
        trouble = ", ".join(f"{n} {status}" for status, n in sorted(data["trouble"].items()))
        parts.append(
            f"Ember's code carried out {total} action{'s' if total != 1 else ''} ({', '.join(whose)})"
            + (f": {trouble}" if trouble else "")
        )
    else:
        parts.append("Ember's code carried out nothing")
    approved = data["approved_by_unlocks"]
    if approved["auto"] or approved["veto_window"]:
        parts.append(
            f"your unlocks approved {approved['auto']} request(s) at once and {approved['veto_window']} after their"
            " veto window"
        )
    if data["held"]:
        until = from_iso(data["first_held_until"]).astimezone(clock.tz)
        parts.append(f"{data['held']} held for your veto now (the first approved {until:%Y-%m-%d %H:%M})")
    if data["taken_back"]:
        taken = "; ".join(
            f"{policy.RULES[t['rule']].label} for milestone #{t['milestone_id']} ({t['why'] or 'by you'})"
            for t in data["taken_back"][:5]
        )
        more = f" and {len(data['taken_back']) - 5} more" if len(data["taken_back"]) > 5 else ""
        parts.append(f"unlocks taken back: {taken}{more}")
    if data["waited_never"]:
        parts.append(f"{data['waited_never']} request(s) waited for you whatever you unlocked (never automatic)")
    text = f"{day.isoformat()}: " + ". ".join(p[0].upper() + p[1:] for p in parts) + "."
    return text[:2000]


def write_due(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> str | None:
    """Yesterday's digest, written once (at the first round of the owner's day); its text when written now."""
    day = clock.today() - timedelta(days=1)
    written = conn.execute(
        "SELECT 1 FROM owner_digests WHERE mode = ? AND session = ? AND day = ?",
        (scope.mode, scope.session, day.isoformat()),
    ).fetchone()
    if written is not None:
        return None
    text, data = digest(conn, scope, clock, day)
    conn.execute(
        "INSERT INTO owner_digests (mode, session, day, text, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, day.isoformat(), text, json.dumps(data, sort_keys=True), to_iso(clock.now())),
    )
    return text


def latest(conn: sqlite3.Connection, scope: AgentScope) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT day, text, data FROM owner_digests WHERE mode = ? AND session = ? ORDER BY day DESC LIMIT 1",
        (scope.mode, scope.session),
    ).fetchone()
    return None if row is None else {"day": row["day"], "text": row["text"], "data": _load(row["data"])}


def view(conn: sqlite3.Connection, scope: AgentScope) -> dict[str, Any]:
    """The Approvals tab's audit card: the feed, the newest digest, and what the unlocks stand for now."""
    return {
        "feed": feed(conn, scope),
        "digest": latest(conn, scope),
        "unlocks": sum(1 for g in policy.grants(conn, scope) if g["level"] != "manual"),
        "held": len(policy.held(conn, scope)),
    }
