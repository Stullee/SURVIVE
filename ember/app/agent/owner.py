"""The owner's side: approvals, the inbox, standing instructions, upgrade requests, ventures, the roadmap, the kill
switch.

Only the owner's HTTP endpoints call this module; the agent's tools never import
it (a test checks), so the agent can't decide its own requests. Every change is
a compare-and-set on the row's current status (approvals also on a version
number), so two browser tabs can't both decide the same item, and the database
refuses status changes that aren't allowed (migration 0004).

An approved email is sent by Ember's code (app/integrations/executor.py), not
by the owner: approving it with changes edits its text only, and while it waits
the owner can only cancel it (mark it failed); once Ember started sending it,
Ember closes it with the result.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any

from .. import events
from ..db import Database
from ..economy.clock import Clock, to_iso
from ..economy.life import KILLED_KEY
from ..economy.service import Economy, Reply
from ..integrations import etsy, executor
from ..integrations.mail import BODY_MAX
from . import roadmap, store, ventures
from .store import AgentScope

KILL_RESET_KEY = "control.kill_reset"
REMOVED_TEXT = "[removed by the owner]"  # the only text a message may be changed to (migration 0006)
INSTRUCTIONS_MAX = 1_500  # characters of the standing instructions (migration 0007)
CANCELLED = "Cancelled by the owner before it was sent"
LISTING_CANCELLED = "Cancelled by the owner before it was listed"
CHANGE_CANCELLED = "Cancelled by the owner before the listing was changed"
DECISIONS = {"approve": "approved", "approve_with_changes": "approved_with_changes", "reject": "rejected"}
OUTCOMES = ("done", "failed")
UPGRADE_STATUSES = ("accepted", "declined", "released")
# The owner's word on a venture (0.10.0): the stages it may come from, and the stage it goes to (None: unchanged).
VENTURE_ACTIONS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "research": (("idea", "researching", "proposed", "parked", "killed"), "researching"),
    "back": (("idea", "researching", "proposed", "parked"), "building"),
    "park": (("idea", "researching", "proposed", "building", "live"), "parked"),
    "kill": (("idea", "researching", "proposed", "building", "live", "parked"), "killed"),
    "note": (ventures.STAGES, None),
}
VENTURE_COMMENT_MAX = 1_000
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


class OwnerError(ValueError):
    def __init__(self, field: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


def _text(body: dict[str, Any], name: str, limit: int, required: bool = False) -> str | None:
    value = body.get(name)
    if value in (None, ""):
        if required:
            raise OwnerError(name, f"please fill in {name.replace('_', ' ')}")
        return None
    if not isinstance(value, str):
        raise OwnerError(name, f"{name} must be text")
    value = value.strip()
    if not value and required:
        raise OwnerError(name, f"please fill in {name.replace('_', ' ')}")
    if len(value) > limit:
        raise OwnerError(name, f"keep {name.replace('_', ' ')} under {limit:,} characters")
    if _BAD_CHARS.search(value):
        raise OwnerError(name, f"{name.replace('_', ' ')} contains control characters")
    return value or None


def _body(body: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise OwnerError("body", "send a JSON object")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise OwnerError(unknown[0], f"unknown field {unknown[0]!r}")
    return body


def _reply(fn: Any) -> Reply:
    try:
        return fn()
    except OwnerError as exc:
        return Reply(exc.status, {"error": str(exc), "field": exc.field})


class Owner:
    def __init__(self, db: Database, clock: Clock, economy: Economy, scope: AgentScope, agent_name: str) -> None:
        self.db = db
        self.clock = clock
        self.economy = economy
        self.scope = scope
        self.agent_name = agent_name

    def _now(self) -> str:
        return to_iso(self.clock.now())

    def _approval(self, conn: sqlite3.Connection, approval_id: int) -> sqlite3.Row:
        where, params = self.scope.where()
        row = conn.execute(f"SELECT * FROM approvals WHERE id = ? AND {where}", (approval_id, *params)).fetchone()
        if row is None:
            raise OwnerError("id", "no such approval request", 404)
        return row

    # --- approvals ---

    def decide(self, approval_id: int, body: Any, who: str | None) -> Reply:
        def run() -> Reply:
            data = _body(body, {"decision", "final_payload", "comment", "expected_version"})
            decision = data.get("decision")
            if decision not in DECISIONS:
                raise OwnerError("decision", "choose approve, approve_with_changes or reject")
            comment = _text(data, "comment", 2_000)
            final = _text(data, "final_payload", 8_000, required=decision == "approve_with_changes")
            with self.db.transaction() as conn:
                row = self._approval(conn, approval_id)
                self._check_version(row, data)
                if row["status"] != "pending":
                    raise OwnerError("id", f"this request is already {row['status']}", 409)
                status = DECISIONS[decision]
                unchanged = row["payload"]
                if row["executor"] == "email" and final is not None:
                    # For an email the owner's version is the text Ember sends (recipient and subject stay).
                    if len(final) > BODY_MAX:
                        raise OwnerError("final_payload", f"keep the email's text under {BODY_MAX:,} characters")
                    try:
                        unchanged = executor.parse_action(row["action"])["body"]
                    except ValueError:
                        raise OwnerError("id", "this email request is broken; reject it", 409) from None
                if row["executor"] == "etsy_listing" and final is not None:
                    # For a listing the owner's version holds its words and price; photos, files and category stay.
                    try:
                        listing = etsy.listing_from_action(row["action"])
                        final = etsy.editable(etsy.with_changes(listing, final))
                    except etsy.EtsyError as exc:
                        raise OwnerError("final_payload", str(exc)) from None
                    unchanged = etsy.editable(listing)
                if row["executor"] == "etsy_edit" and final is not None:
                    # For a change to a listing: the words and the price it changes; photos, files and category stay.
                    try:
                        edit = etsy.edit_from_action(row["action"])
                        final = etsy.edit_editable(etsy.edit_with_changes(edit, final))
                    except etsy.EtsyError as exc:
                        raise OwnerError("final_payload", str(exc)) from None
                    unchanged = etsy.edit_editable(edit)
                if status == "approved_with_changes" and final == unchanged:
                    status, final = "approved", None
                conn.execute(
                    "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, decision_comment = ?,"
                    " final_payload = ?, version = version + 1, seen_cycle_id = NULL"
                    " WHERE id = ? AND status = 'pending' AND version = ?",
                    (status, self._now(), who, comment, final, approval_id, row["version"]),
                )
                events.record(
                    self.db, "info", "owner", f"{who or 'The owner'} {status.replace('_', ' ')} request #{approval_id}"
                )
                return Reply(200, {"approval": self._approval_json(self._approval(conn, approval_id))})

        return _reply(run)

    def close(self, approval_id: int, body: Any, who: str | None) -> Reply:
        def run() -> Reply:
            data = _body(body, {"outcome", "result_note", "result_link", "expected_version"})
            outcome = data.get("outcome")
            if outcome not in OUTCOMES:
                raise OwnerError("outcome", "choose done or failed")
            note = _text(data, "result_note", 2_000)
            link = _text(data, "result_link", 2_048)
            if link is not None and (not re.match(r"^https?://[^\s@/]+(/\S*)?$", link)):
                raise OwnerError("result_link", "use a plain http(s) link without spaces or user names")
            with self.db.transaction() as conn:
                row = self._approval(conn, approval_id)
                self._check_version(row, data)
                if row["status"] not in ("approved", "approved_with_changes"):
                    raise OwnerError("id", f"only an approved request can be closed (it is {row['status']})", 409)
                if row["executor"] == "email":
                    # In the same transaction as the executor's check: either this cancels it or Ember sends it.
                    if conn.execute("SELECT 1 FROM email_actions WHERE approval_id = ?", (approval_id,)).fetchone():
                        raise OwnerError("id", "Ember is already sending this email; it reports the result", 409)
                    if outcome == "done":
                        raise OwnerError("outcome", "Ember sends approved emails itself; to stop this one, cancel it")
                    note = note or CANCELLED
                elif row["executor"] == "etsy_listing":
                    if conn.execute("SELECT 1 FROM etsy_listings WHERE approval_id = ?", (approval_id,)).fetchone():
                        raise OwnerError("id", "Ember is already creating this listing; it reports the result", 409)
                    if outcome == "done":
                        raise OwnerError("outcome", "Ember creates approved listings itself; to stop one, cancel it")
                    note = note or LISTING_CANCELLED
                elif row["executor"] == "etsy_edit":
                    if conn.execute("SELECT 1 FROM etsy_edits WHERE approval_id = ?", (approval_id,)).fetchone():
                        raise OwnerError("id", "Ember is already making this change; it reports the result", 409)
                    if outcome == "done":
                        raise OwnerError("outcome", "Ember makes approved changes itself; to stop one, cancel it")
                    note = note or CHANGE_CANCELLED
                elif outcome == "failed" and note is None:
                    raise OwnerError("result_note", "please fill in result note")
                conn.execute(
                    "UPDATE approvals SET status = ?, closed_at = ?, closed_by = ?, result_note = ?, result_link = ?,"
                    " version = version + 1, seen_cycle_id = NULL WHERE id = ? AND version = ?",
                    (outcome, self._now(), who, note, link, approval_id, row["version"]),
                )
                events.record(self.db, "info", "owner", f"{who or 'The owner'} marked request #{approval_id} {outcome}")
                return Reply(200, {"approval": self._approval_json(self._approval(conn, approval_id))})

        return _reply(run)

    @staticmethod
    def _check_version(row: sqlite3.Row, data: dict[str, Any]) -> None:
        expected = data.get("expected_version")
        if expected is not None and expected != row["version"]:
            raise OwnerError("expected_version", "this request changed meanwhile; reload the page", 409)

    @staticmethod
    def _approval_json(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in row.keys()}  # noqa: SIM118 - sqlite3.Row has no items()

    # --- inbox ---

    def send_message(self, body: Any, who: str | None) -> Reply:
        def run() -> Reply:
            data = _body(body, {"text", "read_up_to"})
            text = _text(data, "text", 2_000, required=True)
            seen = data.get("read_up_to")  # the newest agent message the dashboard showed (0: none)
            if seen is not None and (not isinstance(seen, int) or isinstance(seen, bool) or seen < 0):
                raise OwnerError("read_up_to", "read_up_to must be a message number")
            now = self._now()
            where, params = self.scope.where()
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO messages (mode, session, life_id, created_at, sender, cycle_id, text, entered_by)"
                    " VALUES (?, ?, ?, ?, 'owner', NULL, ?, ?)",
                    (self.scope.mode, self.scope.session, self.scope.life_id, now, text, who),
                )
                message_id = int(cursor.lastrowid)
                # A reply reads what the agent wrote before (message_owner refuses while too many are unread),
                # up to what the owner was shown: one written meanwhile stays unread.
                up_to = message_id - 1 if seen is None else min(seen, message_id - 1)
                conn.execute(
                    f"UPDATE messages SET read_at = ? WHERE {where} AND sender = 'agent' AND read_at IS NULL"
                    " AND id <= ?",
                    (now, *params, up_to),
                )
            events.record(self.db, "info", "owner", f"{who or 'The owner'} sent a message to the agent")
            return Reply(201, {"id": message_id})

        return _reply(run)

    # --- standing instructions ---

    def set_instructions(self, body: Any, who: str | None) -> Reply:
        """Replace the owner's standing instructions (an empty text clears them); every version is kept."""

        def run() -> Reply:
            data = _body(body, {"text"})
            if "text" not in data:
                raise OwnerError("text", "send the instructions as text (empty to clear them)")
            text = _text(data, "text", INSTRUCTIONS_MAX) or ""
            with self.db.transaction() as conn:
                current = store.standing_instructions(conn, self.scope)
                if (current["text"] if current else "") == text:  # nothing changed (a second click): no new version
                    return Reply(200, {"instructions": store.instructions_json(current), "changed": False})
                conn.execute(
                    "INSERT INTO standing_instructions (mode, session, created_at, entered_by, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (self.scope.mode, self.scope.session, self._now(), who, text),
                )
                saved = store.standing_instructions(conn, self.scope)
            actor = who or "The owner"
            events.record(
                self.db, "info", "owner", f"{actor} {'changed' if text else 'cleared'} the standing instructions"
            )
            return Reply(200, {"instructions": store.instructions_json(saved), "changed": True})

        return _reply(run)

    def remove_message(self, message_id: int, who: str | None) -> Reply:
        """Blank the text of one of the owner's own messages (a password sent by mistake); the row stays."""

        def run() -> Reply:
            where, params = self.scope.where()
            with self.db.transaction() as conn:
                row = conn.execute(
                    f"SELECT sender, removed_at FROM messages WHERE id = ? AND {where}", (message_id, *params)
                ).fetchone()
                if row is None:
                    raise OwnerError("id", "no such message", 404)
                if row["sender"] != "owner":
                    raise OwnerError("id", "only your own messages can be removed", 409)
                if row["removed_at"] is not None:
                    raise OwnerError("id", "this message's text is already removed", 409)
                conn.execute(
                    "UPDATE messages SET text = ?, removed_at = ?, removed_by = ? WHERE id = ?",
                    (REMOVED_TEXT, self._now(), who, message_id),
                )
            events.record(self.db, "info", "owner", f"{who or 'The owner'} removed the text of message #{message_id}")
            return Reply(200, {"id": message_id, "removed": True})

        return _reply(run)

    def mark_read(self, body: Any) -> Reply:
        def run() -> Reply:
            data = _body(body, {"up_to_id"})
            up_to = data.get("up_to_id")
            if not isinstance(up_to, int) or isinstance(up_to, bool) or up_to < 1:
                raise OwnerError("up_to_id", "up_to_id must be a message number")
            where, params = self.scope.where()
            with self.db.connection() as conn:
                count = conn.execute(
                    f"UPDATE messages SET read_at = ? WHERE {where} AND sender = 'agent' AND read_at IS NULL"
                    " AND id <= ?",
                    (self._now(), *params, up_to),
                ).rowcount
            return Reply(200, {"marked": count})

        return _reply(run)

    # --- upgrades ---

    def update_upgrade(self, upgrade_id: int, body: Any, who: str | None) -> Reply:
        def run() -> Reply:
            data = _body(body, {"status", "note", "version"})
            status = data.get("status")
            if status not in UPGRADE_STATUSES:
                raise OwnerError("status", "choose accepted, declined or released")
            note = _text(data, "note", 2_000)
            version = _text(data, "version", 20)
            if status != "released" and version is not None:
                raise OwnerError("version", "only a released request has a version")
            if status == "released" and (version is None or not _VERSION.match(version)):
                raise OwnerError("version", "say in which version it was released, like 0.4.0")
            where, params = self.scope.where()
            with self.db.transaction() as conn:
                row = conn.execute(f"SELECT * FROM upgrades WHERE id = ? AND {where}", (upgrade_id, *params)).fetchone()
                if row is None:
                    raise OwnerError("id", "no such upgrade request", 404)
                if row["status"] == status or row["status"] not in ("new", "accepted"):
                    raise OwnerError("status", f"this request is already {row['status']}", 409)
                conn.execute(
                    "UPDATE upgrades SET status = ?, decided_at = ?, owner_note = COALESCE(?, owner_note),"
                    " released_version = ?, seen_cycle_id = NULL WHERE id = ?",
                    (status, self._now(), note, version, upgrade_id),
                )
            actor = who or "The owner"
            events.record(self.db, "info", "owner", f"{actor} marked upgrade request #{upgrade_id} {status}")
            return Reply(200, {"id": upgrade_id, "status": status})

        return _reply(run)

    # --- ventures (0.10.0) ---

    def add_venture(self, body: Any, who: str | None) -> Reply:
        """An idea of the owner's for the agent's venture tree (news for the agent until a plan showed it)."""

        def run() -> Reply:
            data = _body(body, {"title", "pitch", "parent_id"})
            title = _text(data, "title", ventures.LIMITS["title"], required=True) or ""
            if "\n" in title or "\r" in title:
                raise OwnerError("title", "keep the title on one line")
            pitch = _text(data, "pitch", ventures.LIMITS["pitch"], required=True) or ""
            parent_id = data.get("parent_id")
            if parent_id is not None and (
                not isinstance(parent_id, int) or isinstance(parent_id, bool) or parent_id < 1
            ):
                raise OwnerError("parent_id", "parent_id must be a venture number")
            with self.db.transaction() as conn:
                if parent_id is not None and ventures.get(conn, self.scope, parent_id) is None:
                    raise OwnerError("parent_id", "there is no such venture to branch from", 404)
                if ventures.count(conn, self.scope) >= ventures.MAX_VENTURES:
                    raise OwnerError(
                        "title", f"the tree holds {ventures.MAX_VENTURES} ventures, as many as it can", 409
                    )
                same = ventures.by_title(conn, self.scope, title)
                if same is not None:
                    raise OwnerError("title", f"venture #{same['id']} already has this title", 409)
                venture_id = ventures.create(
                    conn,
                    self.scope,
                    title=" ".join(title.split()),
                    pitch=pitch,
                    stage="idea",
                    now=self._now(),
                    parent_id=parent_id,
                    created_by="owner",
                    entered_by=who,
                )
            events.record(self.db, "info", "owner", f"{who or 'The owner'} added venture idea #{venture_id}")
            return Reply(201, {"id": venture_id})

        return _reply(run)

    def decide_venture(self, venture_id: int, body: Any, who: str | None) -> Reply:
        """Back, park, kill or have researched next one of the agent's ventures, or leave a note on it."""

        def run() -> Reply:
            data = _body(body, {"action", "comment", "expected_version"})
            action = data.get("action")
            if action not in VENTURE_ACTIONS:
                raise OwnerError("action", "choose research, back, park, kill or note")
            comment = _text(data, "comment", VENTURE_COMMENT_MAX, required=action == "note")
            expected = data.get("expected_version")
            if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
                raise OwnerError("expected_version", "expected_version must be a number")
            allowed, stage = VENTURE_ACTIONS[action]
            with self.db.transaction() as conn:
                row = ventures.get(conn, self.scope, venture_id)
                if row is None:
                    raise OwnerError("id", "no such venture", 404)
                if expected is not None and expected != row["owner_version"]:
                    raise OwnerError("expected_version", "this venture changed meanwhile", 409)
                if row["stage"] not in allowed:
                    raise OwnerError("action", f"this venture is {row['stage']}", 409)
                ventures.owner_word(conn, venture_id, self._now(), action, comment, who, stage)
                after = ventures.get(conn, self.scope, venture_id)
            done = {"research": "asked for research on", "back": "backed", "park": "parked", "kill": "killed"}
            what = done.get(action, "left a note on")
            events.record(self.db, "info", "owner", f"{who or 'The owner'} {what} venture #{venture_id}")
            return Reply(200, {"id": venture_id, "stage": after["stage"] if after else stage})

        return _reply(run)

    # --- the roadmap (0.11.0) ---

    def add_milestone(self, body: Any, who: str | None) -> Reply:
        """A milestone of the owner's on the agent's roadmap (news for the agent until a plan showed it)."""

        def run() -> Reply:
            data = _body(body, {"title", "measure", "due", "parent_id"})
            title = _text(data, "title", roadmap.LIMITS["title"], required=True) or ""
            measure = _text(data, "measure", roadmap.LIMITS["measure"], required=True) or ""
            for name, value in (("title", title), ("measure", measure)):
                if "\n" in value or "\r" in value:
                    raise OwnerError(name, f"keep the {name} on one line")
            today = self.clock.today()
            due = roadmap.parse_day(data.get("due"))
            if due is None:
                raise OwnerError("due", "give the date as YYYY-MM-DD")
            last = today + timedelta(days=roadmap.AHEAD_DAYS)
            if due < today or due > last:
                raise OwnerError("due", f"choose a date from today to {last.isoformat()}")
            parent_id = data.get("parent_id")
            if parent_id is not None and (
                not isinstance(parent_id, int) or isinstance(parent_id, bool) or parent_id < 1
            ):
                raise OwnerError("parent_id", "parent_id must be a milestone number")
            with self.db.transaction() as conn:
                if parent_id is not None:
                    parent = roadmap.get(conn, self.scope, parent_id)
                    if parent is None or parent["status"] != "open":
                        raise OwnerError("parent_id", "there is no such open milestone", 404)
                    if due.isoformat() > parent["due"]:
                        raise OwnerError(
                            "due", f"the milestone it leads to is due {parent['due']}: choose that or earlier"
                        )
                if roadmap.count(conn, self.scope, "open") >= roadmap.MAX_OPEN:
                    raise OwnerError("title", f"{roadmap.MAX_OPEN} milestones are open already", 409)
                if roadmap.count(conn, self.scope) >= roadmap.MAX_MILESTONES:
                    raise OwnerError("title", "the roadmap holds as many milestones as it can", 409)
                same = roadmap.open_by_title(conn, self.scope, title)
                if same is not None:
                    raise OwnerError("title", f"open milestone #{same['id']} already has this title", 409)
                milestone_id = roadmap.create(
                    conn,
                    self.scope,
                    title=" ".join(title.split()),
                    measure=" ".join(measure.split()),
                    due=due.isoformat(),
                    now=self._now(),
                    parent_id=parent_id,
                    created_by="owner",
                    entered_by=who,
                )
            events.record(self.db, "info", "owner", f"{who or 'The owner'} added milestone #{milestone_id}")
            return Reply(201, {"id": milestone_id})

        return _reply(run)

    def decide_milestone(self, milestone_id: int, body: Any, who: str | None) -> Reply:
        """Leave a note on a milestone, or drop an open one."""

        def run() -> Reply:
            data = _body(body, {"action", "comment", "expected_version"})
            action = data.get("action")
            if action not in ("note", "drop"):
                raise OwnerError("action", "choose note or drop")
            comment = _text(data, "comment", roadmap.LIMITS["comment"], required=action == "note")
            expected = data.get("expected_version")
            if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
                raise OwnerError("expected_version", "expected_version must be a number")
            with self.db.transaction() as conn:
                row = roadmap.get(conn, self.scope, milestone_id)
                if row is None:
                    raise OwnerError("id", "no such milestone", 404)
                if expected is not None and expected != row["owner_version"]:
                    raise OwnerError("expected_version", "this milestone changed meanwhile", 409)
                if action == "drop" and row["status"] != "open":
                    raise OwnerError("action", f"this milestone is {row['status']} already", 409)
                roadmap.owner_word(conn, milestone_id, self._now(), action, comment, who)
                after = roadmap.get(conn, self.scope, milestone_id)
            what = "dropped" if action == "drop" else "left a note on"
            events.record(self.db, "info", "owner", f"{who or 'The owner'} {what} milestone #{milestone_id}")
            return Reply(200, {"id": milestone_id, "status": after["status"] if after else None})

        return _reply(run)


def kill(db: Database, economy: Economy, agent_name: str, body: Any, who: str | None) -> Reply:
    """The kill switch: the agent stops for good until the ``kill_switch_reset`` option changes.

    The metering refuses every further model call of a killed agent, so a running
    cycle ends at its next call; the process keeps running so the dashboard stays up.
    """

    def run() -> Reply:
        data = _body(body, {"confirm_name", "reason"})
        name = data.get("confirm_name")
        if not isinstance(name, str) or name.strip() != agent_name:
            raise OwnerError("confirm_name", f"type the agent's name ({agent_name}) to confirm")
        reason = _text(data, "reason", 300)
        status = economy.life.set_switch(KILLED_KEY, True)
        message = f"{who or 'The owner'} used the kill switch" + (f": {reason}" if reason else "")
        events.record(db, "error", "control", message)
        return Reply(200, {"state": status.state})

    return _reply(run)


def apply_kill_switch_reset(db: Database, economy: Economy, reset_value: int) -> bool:
    """At startup: a changed ``kill_switch_reset`` option re-enables a killed agent. Returns True if it did."""
    stored = db.get_meta(KILL_RESET_KEY)
    if stored is None:
        db.set_meta(KILL_RESET_KEY, str(reset_value))
        return False
    if stored == str(reset_value):
        return False
    db.set_meta(KILL_RESET_KEY, str(reset_value))
    if db.get_meta(KILLED_KEY) == "1":
        economy.life.set_switch(KILLED_KEY, False)
        events.record(db, "warning", "control", "The kill switch was reset in the options; the agent may run again")
        return True
    return False
