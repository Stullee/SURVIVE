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

import base64
import binascii
import re
import sqlite3
from collections.abc import Collection
from datetime import timedelta
from typing import Any

from .. import events, privacy
from ..db import Database
from ..economy.clock import Clock, to_iso
from ..economy.life import KILLED_KEY
from ..economy.service import Economy, Reply
from ..integrations import etsy, executor, mailstore
from ..integrations.mail import BODY_MAX, valid_address
from . import audit, knockouts, library, memory, metrics, plan, policy, predictions, roadmap, stages, store, ventures
from .store import AgentScope

KILL_RESET_KEY = "control.kill_reset"
REMOVED_TEXT = "[removed by the owner]"  # the only text a message may be changed to (migration 0006)
INSTRUCTIONS_MAX = 1_500  # characters of the standing instructions (migration 0007)
CANCELLED = "Cancelled by the owner before it was sent"
LISTING_CANCELLED = "Cancelled by the owner before it was listed"
CHANGE_CANCELLED = "Cancelled by the owner before the listing was changed"
# 0.13.0 (Phase E2, E4): what Ember's code carries out as approved, with no version of the owner's
CODE_CANCELLED = "Cancelled by the owner before Ember's code carried it out"
AS_IS_EXECUTORS = (
    "pinterest_pin",
    "pinterest_delete",
    "pinterest_test_pin",  # 0.30.2: the sandbox's test pin
    "bluesky_post",  # 0.19.0: the post as it was proposed, with its AI line
    "bluesky_delete",
    "printify_product",
    "printify_delete",
    "site_post",  # 0.14.0: the page as it was rendered and previewed
    "site_links",
    "site_restore",
    "live_will",  # the last will as the live page would show it, never in another wording
)
_AS_IS_WHAT = {
    "pinterest": ("a pin", "pins"),
    "bluesky": ("a post", "posts"),
    "printify": ("a product", "products"),
    "site": ("a page", "pages"),
    "live": ("the text", "texts"),
}
TAKEN_BACK = policy.TAKEN_BACK  # 0.13.0: the owner's switch
KILLED = policy.KILLED  # 0.15.0: it takes back every unlock too
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


_THOUSANDS = re.compile(r"^\d{1,3}(,\d{3})+(\.\d{1,2})?$")  # 0.29.0: an amount written with thousands separators


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


def _signed(who: str | None) -> str:
    """How the owner signs a decision: their name, or "the owner" (never a name Ember's code signs with)."""
    return who if who and who not in policy.CODE else "the owner"


def _reply(fn: Any) -> Reply:
    try:
        return fn()
    except OwnerError as exc:
        return Reply(exc.status, {"error": str(exc), "field": exc.field})


class Owner:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        economy: Economy,
        scope: AgentScope,
        agent_name: str,
        unlocks_off: str = "",
        ready: Collection[str] = (),
    ) -> None:
        self.db = db
        self.clock = clock
        self.economy = economy
        self.scope = scope
        self.agent_name = agent_name
        self.unlocks_off = unlocks_off  # 0.15.0: why no unlock may be granted now (policy.off), "" when one may
        self.ready = ready  # 0.15.0: the channels set up now (a channel venture's first test waits for its channel)

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
                if row["executor"] in AS_IS_EXECUTORS and final is not None:  # 0.13.0 (Phase E2, E4), 0.14.0
                    what = _AS_IS_WHAT[row["executor"].split("_")[0]][0]
                    raise OwnerError("decision", f"approve {what} as it is, or reject it and say what should change")
                if row["executor"] == "kdp_package" and final is not None:  # 0.25.0: its files can't take changes
                    raise OwnerError(
                        "decision",
                        "approve the book as it is, or reject it and say what should change (change its words as you "
                        "enter them at KDP, and say so when you mark it done)",
                    )
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
                elif row["executor"] in AS_IS_EXECUTORS:  # 0.13.0 (Phase E2, E4)
                    started = conn.execute(
                        "SELECT 1 FROM pinterest_pins WHERE approval_id = ?"
                        " UNION ALL SELECT 1 FROM bluesky_posts WHERE approval_id = ?"
                        " UNION ALL SELECT 1 FROM printify_products WHERE approval_id = ?"
                        " UNION ALL SELECT 1 FROM site_uploads WHERE approval_id = ? AND status <> 'proposed'"
                        " UNION ALL SELECT 1 FROM action_journal WHERE approval_id = ?",
                        (approval_id, approval_id, approval_id, approval_id, approval_id),
                    ).fetchone()
                    if started:
                        raise OwnerError("id", "Ember is already carrying this out; it reports the result", 409)
                    if outcome == "done":
                        what = _AS_IS_WHAT[row["executor"].split("_")[0]][1]
                        raise OwnerError("outcome", f"Ember carries approved {what} out itself; to stop one, cancel it")
                    note = note or CODE_CANCELLED
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

    # --- email (0.12.0) ---

    def suppress_email(self, body: Any, who: str | None) -> Reply:
        """Ember never emails ``address`` again: someone asked the owner, or asked in words Ember's check missed.
        Final, like every opt-out (the database refuses changes)."""

        def run() -> Reply:
            data = _body(body, {"address"})
            address = str(data.get("address") or "").strip().lower()
            if not valid_address(address):
                raise OwnerError("address", "give one plain email address, like name@example.org")
            actor = who or "The owner"
            with self.db.transaction() as conn:
                added = mailstore.suppress(conn, self.scope, address, self._now(), f"{actor} added it", None)
            if added:
                events.record(self.db, "info", "owner", f"{actor} added an address Ember never emails")
            return Reply(201 if added else 200, {"address": address, "added": added})

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

    def pin_lesson(self, body: Any, who: str | None, lessons: str) -> Reply:
        """0.12.0: pin one of the agent's lessons (``lessons``: its lessons file now): it is never dropped, a rewrite
        of the lessons must keep it, and every plan shows it first."""

        def run() -> Reply:
            data = _body(body, {"text"})
            line = _text(data, "text", 2_000, required=True) or ""
            with self.db.transaction() as conn:
                try:
                    pin_id = memory.pin(conn, self.scope, lessons, line, who, self._now())
                except memory.MemoryError_ as exc:
                    raise OwnerError("text", str(exc), 409) from None
            text = memory.lesson_text(line)
            events.record(self.db, "info", "owner", f"{who or 'The owner'} pinned a lesson: {text}"[:300])
            return Reply(201, {"id": pin_id, "text": text})

        return _reply(run)

    def unpin_lesson(self, pin_id: int, who: str | None) -> Reply:
        """0.12.0: unpin a lesson (final: pin it again to keep it)."""

        def run() -> Reply:
            with self.db.transaction() as conn:
                try:
                    text = memory.unpin(conn, self.scope, pin_id, self._now())
                except memory.MemoryError_ as exc:
                    raise OwnerError("id", str(exc), 404) from None
            events.record(self.db, "info", "owner", f"{who or 'The owner'} unpinned a lesson: {text}"[:300])
            return Reply(200, {"id": pin_id})

        return _reply(run)

    def remove_message(self, message_id: int, who: str | None) -> Reply:
        """Blank the text of one of the owner's own messages (a password sent by mistake); the row stays. Its
        secret-looking words are registered (as salted hashes) so their copies are found elsewhere (0.11.2): the
        caller has the agent scrub them (Agent.scrub_removed)."""

        def run() -> Reply:
            where, params = self.scope.where()
            with self.db.transaction() as conn:
                row = conn.execute(
                    f"SELECT sender, removed_at, text FROM messages WHERE id = ? AND {where}", (message_id, *params)
                ).fetchone()
                if row is None:
                    raise OwnerError("id", "no such message", 404)
                if row["sender"] != "owner":
                    raise OwnerError("id", "only your own messages can be removed", 409)
                if row["removed_at"] is not None:
                    raise OwnerError("id", "this message's text is already removed", 409)
                privacy.register(conn, message_id, row["text"], self._now())
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
            data = _body(body, {"action", "comment", "expected_version", "confirm"})
            action = data.get("action")
            if action not in VENTURE_ACTIONS:
                raise OwnerError("action", "choose research, back, park, kill or note")
            comment = _text(data, "comment", VENTURE_COMMENT_MAX, required=action == "note")
            expected = data.get("expected_version")
            if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
                raise OwnerError("expected_version", "expected_version must be a number")
            confirm = data.get("confirm", False)
            if not isinstance(confirm, bool):
                raise OwnerError("confirm", "confirm must be true or false")
            allowed, stage = VENTURE_ACTIONS[action]
            net_days = self.economy.life.evaluate().runway.net_days if action == "back" else None
            with self.db.transaction() as conn:
                row = ventures.get(conn, self.scope, venture_id)
                if row is None:
                    raise OwnerError("id", "no such venture", 404)
                if expected is not None and expected != row["owner_version"]:
                    raise OwnerError("expected_version", "this venture changed meanwhile", 409)
                if row["stage"] not in allowed:
                    raise OwnerError("action", f"this venture is {row['stage']}", 409)
                now = self._now()
                # 0.15.0: Back checks it as Ember's code would propose it: no numbers or a standing knock-out stop it,
                # unless the owner confirms (their call). A proposal that doesn't hold goes back to researching.
                why = ""
                if action == "back" and not confirm:
                    cash = self.economy.settings.venture_cash_eur
                    why = stages.backing_problem(conn, row, cash_eur=cash, net_days=net_days)
                if why and row["stage"] != "proposed":
                    raise OwnerError(
                        "confirm",
                        f"Ember's code wouldn't back venture #{venture_id}: {why}. Back it with confirm to back it "
                        "anyway, or lift the knock-out on its card first",
                        409,
                    )
                if why:
                    stages.reopen(conn, row, now, why, comment)
                    said = f"Ember's code put venture #{venture_id} back in researching when it was backed: {why}"
                    events.record(self.db, "info", "owner", said[:300])
                    return Reply(200, {"id": venture_id, "stage": "researching", "not_backed": why})
                ventures.owner_word(conn, venture_id, now, action, comment, who, stage)
                after = ventures.get(conn, self.scope, venture_id)
                # 0.12.0: a backed venture's first test becomes a milestone; a parked or killed one's milestones go.
                if action == "back" and after is not None:
                    # 0.15.0: a channel's venture only once its channel is set up (else stages.keep sets it then)
                    if not stages.waits_for_channel(after, self.ready):
                        stages.first_test(conn, self.scope, after, self.clock.today(), now)
                    # 0.13.0: its case's first sale, as a prediction Ember's code settles
                    case = ventures.latest_case(conn, venture_id)
                    predictions.add_first_sale(conn, self.scope, venture_id, case, self.clock.today(), now)
                elif action in ("park", "kill"):
                    unlocked = policy.standing(conn, self.scope)
                    dropped = stages.drop_milestones(
                        conn, self.scope, venture_id, now, f"Your owner {stage} venture #{venture_id}.", "owner"
                    )
                    for mid in dropped:  # 0.23.2: what their unlocks approved stops at once, not at the next policy run
                        for grant in unlocked.get(mid, []):
                            policy.set_grant(
                                conn, self.scope, mid, grant["rule"], "manual", now, by=policy.REVOKED_BY,
                                why=f"your owner {stage} venture #{venture_id}",
                            )  # fmt: skip
                    stages.stop_projects(conn, venture_id, stage or "", now)  # 0.22.0
                    stages.hold_requests(conn, self.scope, venture_id, stage or "", now)  # 0.23.3
                elif action == "research":  # more research, asked for: the research clock starts again
                    conn.execute("UPDATE ventures SET stage_at = ? WHERE id = ?", (now, venture_id))
                if action in ("back", "research") and row["stage"] == "parked":  # 0.23.1: its projects too
                    stages.resume_projects(conn, venture_id)
                after = ventures.get(conn, self.scope, venture_id)
            done = {"research": "asked for research on", "back": "backed", "park": "parked", "kill": "killed"}
            what = done.get(action, "left a note on")
            events.record(self.db, "info", "owner", f"{who or 'The owner'} {what} venture #{venture_id}")
            return Reply(200, {"id": venture_id, "stage": after["stage"] if after else stage})

        return _reply(run)

    def override_knockout(self, venture_id: int, body: Any, who: str | None) -> Reply:
        """0.13.0: lift a knock-out Ember's code found in a venture's case (``lift`` true), or restore it; the agent
        hears it as the owner's note on the venture."""

        def run() -> Reply:
            data = _body(body, {"rule", "lift", "comment"})
            rule = data.get("rule")
            if rule not in knockouts.RULES:
                raise OwnerError("rule", f"choose one of {', '.join(knockouts.RULES)}")
            lift = data.get("lift")
            if not isinstance(lift, bool):
                raise OwnerError("lift", "lift must be true or false")
            comment = _text(data, "comment", VENTURE_COMMENT_MAX)
            with self.db.transaction() as conn:
                row = ventures.get(conn, self.scope, venture_id)
                if row is None:
                    raise OwnerError("id", "no such venture", 404)
                if lift == (rule in knockouts.overridden(conn, venture_id)):
                    raise OwnerError("lift", "it is so already", 409)
                now = self._now()
                knockouts.set_override(conn, venture_id, rule, lift, who, comment, now)
                said = f"{'Lifted' if lift else 'Restored'} the knock-out '{knockouts.LABELS[rule]}'" + (
                    f": {comment}" if comment else ""
                )
                ventures.owner_word(conn, venture_id, now, "note", said[:VENTURE_COMMENT_MAX], who)
            verb = "lifted" if lift else "restored"
            events.record(
                self.db, "info", "owner", f"{who or 'The owner'} {verb} the knock-out {rule} of venture #{venture_id}"
            )
            return Reply(200, {"id": venture_id, "rule": rule, "lifted": lift})

        return _reply(run)

    # --- the plan tree (0.34.0: a preview; Ember's cycles don't read it yet) ---

    def pin_step(self, node_id: int, body: Any, who: str | None) -> Reply:
        """Pin a step of the plan tree (it comes first once the tree steers), or unpin it."""

        def run() -> Reply:
            data = _body(body, {"pinned"})
            pinned = data.get("pinned", True)
            if not isinstance(pinned, bool):
                raise OwnerError("pinned", "pinned is true or false")
            with self.db.transaction() as conn:
                try:
                    plan.pin(conn, self.scope, node_id, pinned, _signed(who), self._now())
                except plan.PlanError as exc:
                    raise OwnerError(exc.field, str(exc), exc.status) from exc
            said = "pinned" if pinned else "unpinned"
            events.record(self.db, "info", "owner", f"{who or 'The owner'} {said} plan step #{node_id}")
            return Reply(200, {"id": node_id, "pinned": pinned})

        return _reply(run)

    def set_worth(self, node_id: int, body: Any, who: str | None) -> Reply:
        """Set a product's worth (from 0.5 to 10; it replaces the one Ember's code computes), or clear it (null)."""

        def run() -> Reply:
            data = _body(body, {"worth"})
            worth = data.get("worth")
            if worth is not None and (isinstance(worth, bool) or not isinstance(worth, int | float)):
                raise OwnerError("worth", "worth is a number, or null to clear it")
            value = None if worth is None else round(float(worth), 2)
            with self.db.transaction() as conn:
                try:
                    plan.set_worth(conn, self.scope, node_id, value, _signed(who), self._now())
                except plan.PlanError as exc:
                    raise OwnerError(exc.field, str(exc), exc.status) from exc
            said = (
                f"set product #{node_id}'s worth to {value:g}"
                if value is not None
                else f"cleared product #{node_id}'s worth"
            )
            events.record(self.db, "info", "owner", f"{who or 'The owner'} {said} in the plan tree")
            return Reply(200, {"id": node_id, "worth": value})

        return _reply(run)

    def set_autonomy(self, milestone_id: int, body: Any, who: str | None) -> Reply:
        """0.13.0: unlock a rule of the policy engine for a milestone (veto_window or auto, with a daily limit and a
        budget of actions), or take it back (manual). Kept as history; the agent hears it in its news. 0.16.3 (analysis
        bug 5): not as the owner's note on the milestone any more: each click overwrote the one before (and the owner's
        own note), and no take-back by Ember's code changed it, so a note said "Unlocked" long after."""

        def run() -> Reply:
            data = _body(body, {"rule", "level", "per_day", "budget"})
            rule, level = data.get("rule"), data.get("level")
            if rule not in policy.RULES:
                raise OwnerError("rule", f"choose one of {', '.join(policy.RULES)}")
            if level not in policy.LEVELS:
                raise OwnerError("level", f"choose one of {', '.join(policy.LEVELS)}")
            if level not in policy.levels(rule):  # 0.22.0: email replies at most with a veto window
                raise OwnerError(
                    "level",
                    "an email reply runs at most unless you veto it within 12 h: Ember's code checks only its thread"
                    " and its words, not what it may quote",
                )
            if level != "manual" and self.unlocks_off:  # 0.15.0: only the owner, known by their user ID, unlocks
                raise OwnerError(
                    "level",
                    f"No unlock while {self.unlocks_off}: put your Home Assistant user ID in owner_user_ids"
                    " (Configuration tab), outside safe mode. Until then every request waits for your click.",
                    409,
                )
            limits = {}
            for name, default, most in (("per_day", policy.PER_DAY, 20), ("budget", policy.BUDGET, 100)):
                value = data.get(name, default)
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= most:
                    raise OwnerError(name, f"{name} is a whole number from 1 to {most}")
                limits[name] = value
            with self.db.transaction() as conn:
                row = roadmap.get(conn, self.scope, milestone_id)
                if row is None:
                    raise OwnerError("id", "no such milestone", 404)
                if row["status"] != "open":
                    raise OwnerError("id", f"this milestone is {row['status']}", 409)
                if level != "manual" and not policy.fits(conn, milestone_id, rule):  # 0.15.0: it would carry nothing
                    need = "no project or venture" if rule == "email_reply" else "a project or venture"
                    raise OwnerError("rule", f"this milestone never covers {rule}: it needs a milestone of {need}", 409)
                policy.set_grant(conn, self.scope, milestone_id, rule, level, self._now(), by=_signed(who), **limits)
            events.record(
                self.db, "info", "owner", f"{who or 'The owner'} set {rule} to {level} for milestone #{milestone_id}"
            )
            return Reply(200, {"id": milestone_id, "rule": rule, "level": level, **limits})

        return _reply(run)

    def take_back_unlocks(self, body: Any, who: str | None) -> Reply:
        """0.13.0: the owner's switch: every unlock that stands is taken back at once (what they held waits for the
        owner again). The agent hears it in its news (0.16.3, analysis bug 5: no longer as a note on each milestone)."""

        def run() -> Reply:
            _body(body, set())
            with self.db.transaction() as conn:
                taken = policy.revoke_all(conn, self.scope, self._now(), by=_signed(who), why=TAKEN_BACK)
            events.record(
                self.db, "warning", "owner", f"{who or 'The owner'} took back every unlock ({len(taken)} in all)"
            )
            return Reply(200, {"taken_back": len(taken)})

        return _reply(run)

    def undo(self, journal_id: int, who: str | None) -> Reply:
        """0.13.0: undo an action of Ember's code on a listing: a request of the owner's, approved at once, which
        Ember's code carries out in its next round (audit.undo). 0.15.0: also while the agent is paused or waits for
        money; refused once the kill switch is on (or the life is over), where it would wait for good."""

        def run() -> Reply:
            state = self.economy.life.evaluate().state
            if state not in audit.UNDO_WHILE:
                stopped = "the kill switch is on" if state == "killed" else f"the agent is {state}"
                raise OwnerError(
                    "id", f"Ember's code carries nothing out while {stopped}, an Undo neither: undo it by hand", 409
                )
            with self.db.transaction() as conn:
                try:
                    approval_id, what = audit.undo(conn, self.scope, self._now(), journal_id, _signed(who))
                except audit.Refused as exc:
                    raise OwnerError("id", str(exc), exc.status) from None
            events.record(
                self.db,
                "info",
                "owner",
                f"{who or 'The owner'} undid action #{journal_id}: {what}, request #{approval_id}",
            )
            return Reply(200, {"journal_id": journal_id, "approval_id": approval_id, "what": what})

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
                if parent_id is None:  # 0.29.0: everything leads to the goal, the owner's milestones too
                    top = roadmap.root(conn, self.scope)
                    parent_id = int(top["id"]) if top is not None else None
                if parent_id is not None:
                    parent = roadmap.get(conn, self.scope, parent_id)
                    if parent is None or parent["status"] != "open":
                        raise OwnerError("parent_id", "there is no such open milestone", 404)
                    # the money goal stands in for the owner's goal: what leads to it may be due later
                    if due.isoformat() > parent["due"] and parent["kind"] != "money_goal":
                        raise OwnerError(
                            "due", f"the milestone it leads to is due {parent['due']}: choose that or earlier"
                        )
                if roadmap.placed(conn, self.scope) >= roadmap.MAX_OPEN:  # 0.15.0: Ember's code's take no place
                    raise OwnerError(
                        "title", f"{roadmap.MAX_OPEN} of your and the agent's milestones are open already", 409
                    )
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

    def set_goal(self, body: Any, who: str | None) -> Reply:
        """0.29.0: the owner's goal at the roadmap's root: earn an amount (USD) a month or in total, by a date, which
        everything on the roadmap leads to. One set while another stands takes its place; ``replaces`` names the goal
        the owner saw (none: they saw none), so one set meanwhile isn't replaced unseen."""

        def run() -> Reply:
            data = _body(body, {"amount_usd", "per", "due", "comment", "replaces"})
            per = data.get("per")
            if per not in roadmap.GOAL_METRICS:
                raise OwnerError("per", "choose month or total")
            amount = data.get("amount_usd")
            if isinstance(amount, bool) or not isinstance(amount, str | int | float):
                raise OwnerError("amount_usd", "give the amount in USD, e.g. 1000")
            if isinstance(amount, str) and _THOUSANDS.match(amount.strip().removeprefix("$")):
                amount = amount.strip().removeprefix("$").replace(",", "")  # "1,000" is a thousand, not one
            try:
                target = metrics.parse_target(metrics.CATALOGUE[roadmap.GOAL_METRICS[per]], amount)
            except metrics.TargetError:
                raise OwnerError(
                    "amount_usd", f"give the amount in USD, from 1 to {metrics.MAX_USD:,}, in cents at most"
                ) from None
            if target < metrics.MICROS:
                raise OwnerError("amount_usd", "give at least $1")
            comment = _text(data, "comment", roadmap.LIMITS["comment"])
            today = self.clock.today()
            due = roadmap.parse_day(data.get("due"))
            if due is None:
                raise OwnerError("due", "give the date as YYYY-MM-DD")
            first = today + timedelta(days=roadmap.GOAL_DAYS_MIN)
            last = today + timedelta(days=roadmap.AHEAD_DAYS)
            if due < first or due > last:
                raise OwnerError("due", f"choose a date from {first.isoformat()} to {last.isoformat()}")
            seen = data.get("replaces")
            if seen is not None and (not isinstance(seen, int) or isinstance(seen, bool) or seen < 1):
                raise OwnerError("replaces", "replaces must be the number of the goal you change")
            with self.db.transaction() as conn:
                old = roadmap.owner_goal(conn, self.scope)
                if "replaces" in data and (old["id"] if old is not None else None) != seen:
                    raise OwnerError(
                        "replaces",
                        f"your goal changed meanwhile (#{old['id']} stands now)"
                        if old
                        else "your goal was closed meanwhile",
                        409,
                    )
                if roadmap.count(conn, self.scope) >= roadmap.MAX_MILESTONES:
                    raise OwnerError("amount_usd", "the roadmap holds as many milestones as it can", 409)
                goal_id, happened = roadmap.set_goal(
                    conn,
                    self.scope,
                    per=per,
                    target=target,
                    due=due,
                    today=today,
                    now=self._now(),
                    who=who,
                    comment=comment,
                )
            what = f"{who or 'The owner'} set the goal #{goal_id}" + (f" in place of #{old['id']}" if old else "")
            events.record(self.db, "info", "owner", what)
            for line in happened:
                events.record(self.db, "info", "agent", line[:300])
            return Reply(200, {"id": goal_id, "replaced": old["id"] if old is not None else None})

        return _reply(run)

    # --- the library (0.12.0) ---

    def add_document(self, body: Any, who: str | None) -> Reply:
        """A document for the library: pasted text, or a file (its contents base64-encoded) that Ember's code turns
        into text. Ember studies it at its next wake cycles, within the daily study budget."""

        def run() -> Reply:
            data = _body(
                body, {"title", "source", "note", "venture_id", "project_id", "text", "file_name", "file_data"}
            )
            pasted, encoded = data.get("text"), data.get("file_data")
            if (pasted in (None, "")) == (encoded in (None, "")):
                raise OwnerError("text", "paste the text or choose a file")
            file_name = None
            if encoded not in (None, ""):
                name = data.get("file_name")
                if not isinstance(name, str) or not name.strip() or len(name) > 255:
                    raise OwnerError("file_name", "name the file (at most 255 characters)")
                if not isinstance(encoded, str) or len(encoded) > library.FILE_BYTES * 4 // 3 + 4:
                    raise OwnerError("file_data", f"a file can have at most {library.FILE_BYTES // 1_000_000} MB")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except binascii.Error as exc:
                    raise OwnerError("file_data", "the file didn't arrive whole (it isn't base64)") from exc
                try:
                    text, title = library.from_file(name.strip(), raw)
                except library.LibraryError as exc:
                    raise OwnerError("file_data", str(exc), exc.status) from exc
                file_name = name.strip()
            else:
                if not isinstance(pasted, str):
                    raise OwnerError("text", "text must be text")
                if len(pasted) > library.DOCUMENT_CHARS * 2:  # far over the limit even before its spaces go
                    raise OwnerError("text", f"a document can have at most {library.DOCUMENT_CHARS:,} characters")
                text = library.plain(pasted)
                title = next((line for line in text.split("\n") if line.strip()), "")
            given = _text(data, "title", library.LIMITS["title"])
            title = " ".join((given or title or "Untitled").split())[: library.LIMITS["title"]]
            source = _text(data, "source", library.LIMITS["source"]) or ""
            note = _text(data, "note", library.LIMITS["note"]) or ""
            with self.db.transaction() as conn:
                venture_id = self._link(conn, "ventures", "venture_id", data.get("venture_id"))
                project_id = self._link(conn, "projects", "project_id", data.get("project_id"))
                try:
                    document_id = library.add(
                        conn,
                        self.scope,
                        title=title,
                        text=text,
                        now=self._now(),
                        source=" ".join(source.split()),
                        note=note,
                        venture_id=venture_id,
                        project_id=project_id,
                        file_name=file_name,
                        who=who,
                    )
                except library.LibraryError as exc:
                    raise OwnerError(exc.field, str(exc), exc.status) from exc
                row = library.get(conn, self.scope, document_id)
            events.record(self.db, "info", "owner", f"{who or 'The owner'} added library document #{document_id}")
            return Reply(201, {"id": document_id, "title": title, "chars": row["chars"], "parts": row["parts"]})

        return _reply(run)

    def _link(self, conn: sqlite3.Connection, table: str, field: str, value: Any) -> int | None:
        """A venture or project of this scope the document is for (None: none)."""
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise OwnerError(field, f"{field} must be a number")
        where, params = self.scope.where()
        if conn.execute(f"SELECT 1 FROM {table} WHERE id = ? AND {where}", (value, *params)).fetchone() is None:
            raise OwnerError(field, f"there is no {table[:-1]} #{value}", 404)
        return value

    def remove_document(self, document_id: int, who: str | None) -> Reply:
        """Take a document out of the library: its text goes, its learnings are no longer shown."""

        def run() -> Reply:
            with self.db.transaction() as conn:
                row = library.get(conn, self.scope, document_id)
                if row is None:
                    raise OwnerError("id", "no such document", 404)
                if row["removed_at"] is not None:
                    raise OwnerError("id", "this document was removed already", 409)
                library.remove(conn, document_id, self._now(), who)
            events.record(self.db, "info", "owner", f"{who or 'The owner'} removed library document #{document_id}")
            return Reply(200, {"id": document_id, "removed": True})

        return _reply(run)

    def study_document_again(self, document_id: int, who: str | None) -> Reply:
        """Another try at a document whose study failed: it goes on from where it stopped."""

        def run() -> Reply:
            with self.db.transaction() as conn:
                row = library.get(conn, self.scope, document_id)
                if row is None or row["removed_at"] is not None:
                    raise OwnerError("id", "no such document", 404)
                if row["study"] != "failed":
                    raise OwnerError("id", f"its study is {row['study']}, not failed", 409)
                library.study_again(conn, document_id)
            events.record(
                self.db, "info", "owner", f"{who or 'The owner'} asked to study library document #{document_id} again"
            )
            return Reply(200, {"id": document_id, "study": "waiting"})

        return _reply(run)

    def decide_milestone(self, milestone_id: int, body: Any, who: str | None) -> Reply:
        """Leave a note on a milestone, drop an open one (its open steps with it), or accept or reject the date the
        agent proposed for one of the owner's (0.12.0; ``proposed_due`` names the date the owner saw)."""

        def run() -> Reply:
            data = _body(body, {"action", "comment", "expected_version", "proposed_due"})
            action = data.get("action")
            if action not in ("note", "drop", "accept", "reject"):
                raise OwnerError("action", "choose note, drop, accept or reject")
            comment = _text(data, "comment", roadmap.LIMITS["comment"], required=action == "note")
            expected = data.get("expected_version")
            if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
                raise OwnerError("expected_version", "expected_version must be a number")
            seen = data.get("proposed_due")
            if action == "accept" and roadmap.parse_day(seen) is None:
                raise OwnerError("proposed_due", "name the proposed date you accept (YYYY-MM-DD)")
            if seen is not None and roadmap.parse_day(seen) is None:
                raise OwnerError("proposed_due", "proposed_due must be a date written YYYY-MM-DD")
            with self.db.transaction() as conn:
                row = roadmap.get(conn, self.scope, milestone_id)
                if row is None:
                    raise OwnerError("id", "no such milestone", 404)
                if expected is not None and expected != row["owner_version"]:
                    raise OwnerError("expected_version", "this milestone changed meanwhile", 409)
                if action != "note" and row["status"] != "open":
                    raise OwnerError("action", f"this milestone is {row['status']} already", 409)
                if action in ("accept", "reject"):
                    self._check_proposal(conn, row, action, seen)
                dropped = roadmap.owner_word(conn, milestone_id, self._now(), action, comment, who)
                after = roadmap.get(conn, self.scope, milestone_id)
            what = {
                "drop": "dropped",
                "accept": "accepted the proposed date of",
                "reject": "kept the date of",
            }.get(action, "left a note on")
            also = f" and, with it, {', '.join(f'#{i}' for i in dropped)}" if dropped else ""
            events.record(self.db, "info", "owner", f"{who or 'The owner'} {what} milestone #{milestone_id}{also}")
            reply: dict[str, Any] = {"id": milestone_id, "status": after["status"] if after else None}
            if action == "drop":
                reply["dropped_with"] = dropped
            if action == "accept" and after is not None:
                reply["due"] = after["due"]
            return Reply(200, reply)

        return _reply(run)

    def _check_proposal(self, conn: Any, row: Any, action: str, seen: str | None) -> None:
        """A proposed date the owner can answer: the one they saw, and for an accept, one that still fits the dates
        of the milestones it leads to and that lead to it."""
        proposed = row["proposed_due"]
        if proposed is None:
            raise OwnerError("action", "there is no proposed date to decide on", 409)
        if seen is not None and seen.strip() != proposed:
            raise OwnerError("proposed_due", f"the agent has proposed another date meanwhile ({proposed})", 409)
        if action != "accept":
            return
        if proposed < self.clock.today().isoformat():
            raise OwnerError("proposed_due", f"the proposed date ({proposed}) has passed: keep the date instead", 409)
        parent = roadmap.get(conn, self.scope, row["parent_id"]) if row["parent_id"] else None
        if parent is not None and parent["status"] == "open" and proposed > parent["due"]:
            raise OwnerError(
                "proposed_due", f"it leads to milestone #{parent['id']}, which is due earlier ({parent['due']})", 409
            )
        later = [k for k in roadmap.children(conn, row["id"]) if k["status"] == "open" and k["due"] > proposed]
        if later:
            raise OwnerError(
                "proposed_due", f"milestone #{later[0]['id']} leads to it and is due later ({later[0]['due']})", 409
            )


def kill(db: Database, economy: Economy, agent_name: str, body: Any, who: str | None) -> Reply:
    """The kill switch: the agent stops for good until the ``kill_switch_reset`` option changes.

    The metering refuses every further model call of a killed agent, so a running
    cycle ends at its next call; the process keeps running so the dashboard stays up.
    0.15.0: it takes back every unlock in the same transaction, so its reset approves nothing an unlock held.
    """

    def run() -> Reply:
        data = _body(body, {"confirm_name", "reason"})
        name = data.get("confirm_name")
        if not isinstance(name, str) or name.strip() != agent_name:
            raise OwnerError("confirm_name", f"type the agent's name ({agent_name}) to confirm")
        reason = _text(data, "reason", 300)
        with db.transaction() as conn:
            policy.revoke_everywhere(conn, to_iso(economy.clock.now()), by=_signed(who), why=KILLED)
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
