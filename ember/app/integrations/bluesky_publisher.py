"""Carrying out approved Bluesky posts, and reading how they do (0.19.0).

An approved post (executor 'bluesky_post') is carried out once, like a pin: its row is committed as 'running' before
anything is sent, so a crash never posts it twice ('unclear' when it can't be known; the app's next start marks what a
crash left running). Its picture must be exactly the file the owner approved (its SHA-256); Ember's code sends a
smaller copy when it is larger than Bluesky takes (images.within: the same picture always gives the same copy). The
link must still be Ember's: a listing still live, as the pin's, or (0.19.2) a page of the owner's website that Ember's
code knows is there; 0.25.1: its second link too. At most bluesky_posts_per_day posts a day. Before the first post of a
round Ember's code logs in: while Bluesky refuses the login, approved posts wait (nothing is begun). The owner's Undo of
a post is a request of theirs (executor 'bluesky_delete'), carried out here too. The sync reads the account's followers
and each live post's numbers (likes, reposts, replies, quotes, moderation's labels) at most every SYNC_HOURS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from .. import events
from ..agent import website
from ..agent.sandbox import Jail, SandboxError
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from ..products import images
from . import connectors, etsy, etsy_publisher, site_publisher
from .bluesky import (
    BLOB_LONGEST,
    BLOB_MAX_BYTES,
    Account,
    BlueskyError,
    Gone,
    NotSent,
    Picture,
    Post,
    full_text,
    post_from_action,
    post_url,
)
from .etsy import Upload

log = logging.getLogger(__name__)

APPROVED = "('approved', 'approved_with_changes')"
CLOSED_BY = "Ember"
SYNC_HOURS = 6
STATS_BATCH = 25  # posts read in one request (Bluesky's limit)
STATS_POSTS = 100  # the newest live posts read at a sync
INTERRUPTED = "the app stopped while posting it"
DELETE_INTERRUPTED = "the app stopped while deleting it"
GONE = "Deleted at Bluesky, not by Ember's code"
LISTING_LINK = re.compile(r"^https://www\.etsy\.com/listing/(\d{1,18})$")  # etsy.listing_url: what a post links


def meta_key(mode: str, name: str) -> str:
    return f"integrations.bluesky.{mode}.{name}"


def posts(conn: sqlite3.Connection, scope: AgentScope, limit: int = 20) -> list[sqlite3.Row]:
    """Ember's posts, the newest first."""
    where, params = scope.where()
    return conn.execute(
        f"SELECT * FROM bluesky_posts WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def url_of(r: sqlite3.Row) -> str | None:
    return post_url(r["did"], r["rkey"]) if r["rkey"] and r["did"] else None


def post_json(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "approval_id": r["approval_id"],
        "rkey": r["rkey"],
        "text": r["text"],
        "link": r["link"],
        "second_link": r["second_link"],  # 0.25.1
        "status": r["status"],
        "url": url_of(r),
        "likes": r["likes"],
        "reposts": r["reposts"],
        "replies": r["replies"],
        "quotes": r["quotes"],
        "labels": r["labels"],
        "synced_at": r["synced_at"],
        "result": r["result"],
    }


def reactions(r: sqlite3.Row) -> int:
    return sum(int(r[k] or 0) for k in ("likes", "reposts", "replies", "quotes"))


def totals(conn: sqlite3.Connection, scope: AgentScope) -> tuple[int, int]:
    """Ember's live posts, and the likes, reposts, replies and quotes they had at the last sync (the metrics
    bluesky_posts_live and bluesky_reactions)."""
    where, params = scope.where()
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(COALESCE(likes, 0) + COALESCE(reposts, 0) + COALESCE(replies, 0)"
        f" + COALESCE(quotes, 0)), 0) FROM bluesky_posts WHERE {where} AND status = 'active'",
        params,
    ).fetchone()
    return int(row[0]), int(row[1])


def created_today(conn: sqlite3.Connection, clock: Clock, scope: AgentScope) -> int:
    """The posts sent to Bluesky today, for bluesky_posts_per_day: not one its checks refused before anything was sent,
    so it doesn't hold a valid post back a day."""
    where, params = scope.where()
    start = to_iso(clock.day_start(clock.today()))
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM bluesky_posts WHERE {where} AND started_at >= ? AND sent = 1", (*params, start)
        ).fetchone()[0]
    )


def execution(
    conn: sqlite3.Connection, row: sqlite3.Row, scope: AgentScope, clock: Clock, daily_limit: int, refused: str = ""
) -> dict[str, Any] | None:
    """What happened to an approved post, or to the owner's Undo of one, for the dashboard (None before approval).
    ``refused``: why Bluesky refuses Ember's login now ("" when it doesn't), which a waiting post waits for."""
    if row["executor"] == "bluesky_delete":
        entry = conn.execute(
            "SELECT * FROM action_journal WHERE approval_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        if entry is not None:
            status = {"done": "deleted", "simulated": "deleted"}.get(str(entry["status"]), str(entry["status"]))
            return {
                "status": status,
                "started_at": entry["started_at"],
                "finished_at": entry["finished_at"],
                "result": entry["note"],
                "error": None,
                "url": None,
            }
    else:
        post = conn.execute("SELECT * FROM bluesky_posts WHERE approval_id = ?", (row["id"],)).fetchone()
        if post is not None:
            return {
                "status": post["status"],
                "started_at": post["started_at"],
                "finished_at": post["finished_at"],
                "result": post["result"],
                "error": post["error"],
                "url": url_of(post),
            }
    if row["status"] not in ("approved", "approved_with_changes"):
        return None
    limited = row["executor"] == "bluesky_post" and created_today(conn, clock, scope) >= daily_limit
    return {
        "status": "waiting_limit" if limited else "waiting",
        "started_at": None,
        "finished_at": None,
        "result": f"It waits: {refused}" if refused else None,
        "error": None,
        "url": None,
    }


def summary(conn: sqlite3.Connection, scope: AgentScope) -> str:
    """0.37.1: the live posts and their reactions in one line, for an ordinary cycle's BLUESKY and the daily review:
    what the channel is judged by (an ordinary cycle's plan had none of its numbers, so the agent couldn't tell its
    owner)."""
    live, reacted = totals(conn, scope)
    if not live:
        return "no post live"
    return (
        f"{live} post{'' if live == 1 else 's'} live with {reacted} reaction{'' if reacted == 1 else 's'}"
        f" ({reacted / live:.1f} a post)"
    )


def text(conn: sqlite3.Connection, scope: AgentScope, limit: int = 6) -> str:
    """The plan's BLUESKY: Ember's newest posts with their numbers. 0.37.1: each by its request's number and day, as the
    owner and the agent name it (its record key at Bluesky said nothing to either)."""
    recent = posts(conn, scope, limit)
    if not recent:
        return "No post of yours yet."
    lines = []
    for r in recent:
        numbers = (
            f"{r['likes'] or 0} likes, {r['reposts'] or 0} reposts, {r['replies'] or 0} replies, {r['quotes'] or 0}"
            " quotes"
            if r["synced_at"]
            else "not read yet"
        )
        if r["labels"]:
            numbers += f"; labelled by moderation: {r['labels']}"
        words = " ".join(str(r["text"]).split())[:60]
        link = f" -> {r['link']}" if r["link"] else ""
        link += f" and {r['second_link']}" if r["second_link"] else ""  # 0.25.1
        day = str(r["finished_at"] or r["started_at"])[:10]
        lines.append(f"- request #{r['approval_id']}, {day} ({r['status']}): {words}{link}: {numbers}")
    return "\n".join(lines)


class Publisher:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Callable[[], AgentScope],
        account: Callable[[], Account | None],
        workspace: Callable[[], Jail],
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.account = account
        self.workspace = workspace
        self._lock = threading.Lock()  # one run (or sync) at a time in this process

    def run(self, undos: bool = False) -> list[tuple[int, str]]:
        """Carry out the approved posts (and the owner's Undos of posts) that are due; with ``undos`` (while the agent
        is paused or waits for money), only the Undos."""
        account = self.account()
        if account is None or not self._lock.acquire(blocking=False):
            return []
        try:
            scope = self.scope()
            due = [] if undos else self._approved(scope, "bluesky_post")
            undoing = self._approved(scope, "bluesky_delete")
            if (due or undoing) and not self._logged_in(account, scope):
                return []  # Bluesky refused the login: what is approved waits, nothing begun
            done = []
            for approval_id in due:
                outcome = self._one(account, scope, approval_id)
                done.append((approval_id, outcome))
                if outcome == "waiting_limit":
                    break  # the rest waits for tomorrow too, in order
            for approval_id in undoing:
                done.append((approval_id, self._delete(account, scope, approval_id)))
            return done
        finally:
            self._lock.release()

    def _logged_in(self, account: Account, scope: AgentScope) -> bool:
        """Log in (or renew the login) before anything is begun: while Bluesky refuses it (or can't be reached), what is
        approved waits."""
        try:
            account.keep_alive()
        except BlueskyError as exc:
            self.db.set_meta(meta_key(scope.mode, "last_error"), str(exc)[:300])
            return False
        return True

    def _approved(self, scope: AgentScope, executor: str) -> list[int]:
        where, params = scope.where()
        with self.db.connection() as conn:
            return [
                int(r[0])
                for r in conn.execute(
                    f"SELECT id FROM approvals WHERE {where} AND executor = ? AND status IN {APPROVED}"
                    " AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id)"
                    " AND NOT EXISTS (SELECT 1 FROM bluesky_posts p WHERE p.approval_id = approvals.id) ORDER BY id",
                    (*params, executor),
                )
            ]

    def _link(self, conn: sqlite3.Connection, scope: AgentScope, link: str | None, now: str) -> str | None:
        """A post's link to one of Ember's listings must still be live when the post is made (an approved post can
        wait days for its turn); 0.19.2: one to the owner's website must be a page Ember's code knows is there (live,
        an approved post linked a blog post by its file's name: a missing page), and a blog post's address without its
        ".html" is posted with it. Returns the link to post; raises BlueskyError."""
        found = LISTING_LINK.match(link or "")
        if found is None:
            base = website.address(self.settings)
            if link is None or not base or urlsplit(link).netloc.lower() != urlsplit(base).netloc.lower():
                return link
            page = site_publisher.known_page(conn, self.db, scope, base, link, self.settings.site_enabled)
            if page is None:
                raise BlueskyError(
                    f"{link} isn't a page of your owner's website that Ember's code knows is there (a blog post's "
                    "address ends in .html, as BLOG gives it)"
                )
            return page[0]
        listing_id = int(found.group(1))
        row = etsy_publisher.shop_listing_row(conn, scope, listing_id)  # 0.32.0: one Printify made too
        if row is None:
            raise BlueskyError(f"#{listing_id} isn't one of your live listings any more")
        if etsy_publisher.etsy_state(row) != etsy.LIVE_STATE:
            raise BlueskyError(f"#{listing_id} isn't live at Etsy any more ({etsy_publisher.state_text(row)})")
        if not row["auto_renew"] and row["ends_at"] and from_iso(row["ends_at"]) <= from_iso(now):
            raise BlueskyError(f"#{listing_id} isn't live at Etsy any more (it ended on {row['ends_at'][:10]})")
        return link

    def _read(self, upload: Upload, what: str) -> bytes:
        try:
            data = self.workspace().read_bytes(upload.path)
        except SandboxError as exc:
            raise BlueskyError(f"{upload.path} can't be read: {exc}") from None
        if hashlib.sha256(data).hexdigest() != upload.sha256:
            raise BlueskyError(f"{upload.path} changed after it was approved: propose the {what} again")
        return data

    def _picture(self, upload: Upload) -> Picture:
        """A picture as the owner approved it, as Bluesky takes it (a smaller copy when it is larger). Raises
        BlueskyError."""
        try:
            sized = images.within(self._read(upload, "post"), BLOB_MAX_BYTES, BLOB_LONGEST)
        except images.ImageError as exc:
            raise BlueskyError(f"{upload.path} can't be sent: {exc}") from None
        return Picture(sized.data, sized.mime, sized.width, sized.height)

    def _pictures(self, post: Post) -> tuple[Picture | None, Picture | None, str]:
        """The picture and the card's photo as Bluesky takes them, and a note on a card's photo left out. The picture
        must be what the owner approved (else BlueskyError); a card without its photo is still a card."""
        image = self._picture(post.image) if post.image is not None else None
        thumb = None
        note = ""
        if post.card() and post.card_photo is not None:
            try:
                thumb = self._picture(post.card_photo)
            except BlueskyError as exc:
                note = f" (its card without the listing's photo: {exc})"
        return image, thumb, note

    @staticmethod
    def _due(conn: sqlite3.Connection, approval_id: int) -> sqlite3.Row | None:
        """The approved request, unless it was cancelled or decided meanwhile, or its post was begun."""
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        started = conn.execute("SELECT 1 FROM bluesky_posts WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None or row["status"] not in ("approved", "approved_with_changes") or started is not None:
            return None
        return row

    def _one(self, account: Account, scope: AgentScope, approval_id: int) -> str:
        with self.db.connection() as conn:
            row = self._due(conn, approval_id)
            if row is None:
                return "skipped"
            if created_today(conn, self.clock, scope) >= self.settings.bluesky_posts_per_day:
                return "waiting_limit"
        problem = ""
        image = thumb = None
        note = ""
        try:  # outside any transaction: a picture's copy for Bluesky takes a moment to make
            post = post_from_action(row["action"])
            image, thumb, note = self._pictures(post)
        except BlueskyError as exc:
            problem = str(exc)
        stamp = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            row = self._due(conn, approval_id)
            if row is None:
                return "skipped"
            if not problem:
                try:
                    fixed = self._link(conn, scope, post.link, stamp)
                    second = self._link(conn, scope, post.second_link, stamp)  # 0.25.1: checked as the link is
                except BlueskyError as exc:
                    problem = str(exc)
                else:
                    for what, approved, sent in (("link", post.link, fixed), ("second link", post.second_link, second)):
                        if sent != approved:  # 0.19.2: a blog post's address without its ".html"
                            note += f" Its {what} went out as {sent}: the approved address named no page there."
                    post = replace(post, link=fixed, second_link=second)
            if problem:
                self._start(conn, scope, approval_id, stamp, str(row["title"]), None, None, sent=False)
                connectors.begin(conn, approval_id, stamp)
                return self._failed(conn, approval_id, problem)
            if created_today(conn, self.clock, scope) >= self.settings.bluesky_posts_per_day:
                return "waiting_limit"
            self._start(conn, scope, approval_id, stamp, full_text(post), post.link, post.second_link, sent=True)
            connectors.begin(conn, approval_id, stamp)
        # Committed: from here on this post is never made a second time, whatever happens.
        try:
            made = account.create_post(post, image, thumb)
        except NotSent as exc:
            return self._after(approval_id, "failed", None, f"Not posted: Bluesky refused it ({exc})", str(exc))
        except Exception as exc:  # noqa: BLE001 - Unclear, or anything else: a post may exist
            error = str(exc) if isinstance(exc, BlueskyError) else type(exc).__name__
            if not isinstance(exc, BlueskyError):
                log.exception("Posting request #%d on Bluesky failed", approval_id)
            note = f"It is unclear whether Bluesky took the post ({error}). Ember won't try again; check the account."
            return self._after(approval_id, "unclear", None, note, error)
        did = made.uri.removeprefix("at://").split("/", 1)[0]
        url = post_url(did, made.rkey)
        done = f"Posted: {url}{note}"
        if account.simulated:
            done = f"Posted in the dry run's fake account as {made.rkey}{note}; nothing reached Bluesky."
        return self._after(approval_id, "active", made, done, simulated=account.simulated, did=did)

    @staticmethod
    def _start(
        conn: sqlite3.Connection,
        scope: AgentScope,
        approval_id: int,
        stamp: str,
        text: str,
        link: str | None,
        second_link: str | None,
        *,
        sent: bool,
    ) -> None:
        conn.execute(
            "INSERT INTO bluesky_posts (mode, session, approval_id, text, link, second_link, status, sent, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (
                scope.mode,
                scope.session,
                approval_id,
                text[:3000],
                (link or "")[:500] or None,
                (second_link or "")[:500] or None,
                int(sent),
                stamp,
            ),
        )

    def _failed(self, conn: sqlite3.Connection, approval_id: int, reason: str) -> str:
        """A post its checks refused before anything was sent (it doesn't count toward the day's limit)."""
        note = f"Not posted: {reason}"
        now = to_iso(self.clock.now())
        conn.execute(
            "UPDATE bluesky_posts SET status = 'failed', finished_at = ?, result = ?, error = ? WHERE approval_id = ?",
            (now, note[:500], reason[:500], approval_id),
        )
        connectors.finish(conn, approval_id, "failed", now, note=note)
        self._close(conn, approval_id, "failed", note, None)
        return "failed"

    def _after(
        self,
        approval_id: int,
        status: str,
        made: Any,
        note: str,
        error: str | None = None,
        simulated: bool = False,
        did: str | None = None,
    ) -> str:
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            conn.execute(
                "UPDATE bluesky_posts SET status = ?, finished_at = ?, uri = ?, rkey = ?, did = ?, result = ?,"
                " error = ? WHERE approval_id = ?",
                (
                    status,
                    now,
                    made.uri if made is not None else None,
                    made.rkey if made is not None else None,
                    did,
                    note[:500],
                    (error or "")[:500] or None,
                    approval_id,
                ),
            )
            journaled = {"active": "simulated" if simulated else "done"}.get(status, status)
            connectors.finish(
                conn,
                approval_id,
                journaled,
                now,
                {"uri": made.uri, "cid": made.cid} if made is not None else None,
                note,
                subject=made.rkey if made is not None else None,
            )
            link = post_url(did, made.rkey) if made is not None and did else None
            self._close(conn, approval_id, "done" if status == "active" else "failed", note, link)
        level = "info" if status == "active" else "warning"
        events.record(self.db, level, "bluesky", f"Request #{approval_id}: {note}"[:300])
        return status

    def _close(self, conn: sqlite3.Connection, approval_id: int, outcome: str, note: str, link: str | None) -> None:
        """Close the approval (unless the owner did meanwhile): the agent hears the result at its next wake."""
        conn.execute(
            "UPDATE approvals SET status = ?, closed_at = ?, closed_by = ?, result_note = ?, result_link = ?,"
            f" version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status IN {APPROVED}",
            (outcome, to_iso(self.clock.now()), CLOSED_BY, note[:2000], link, approval_id),
        )

    def _delete(self, account: Account, scope: AgentScope, approval_id: int) -> str:
        """The owner's Undo of a post: Ember's code deletes it at Bluesky."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in ("approved", "approved_with_changes"):
                return "skipped"
            try:
                rkey = str(json.loads(row["action"])["rkey"])
            except (ValueError, KeyError, TypeError):
                rkey = ""
            connectors.begin(conn, approval_id, to_iso(self.clock.now()), subject=rkey or None)
        gone = False
        try:
            if not rkey:
                raise NotSent("the request names no post")
            account.delete_post(rkey)
        except Gone:
            gone = True  # deleted at Bluesky already: what the Undo wanted
        except Exception as exc:  # noqa: BLE001 - reported on the request, never raised
            error = str(exc) if isinstance(exc, BlueskyError) else type(exc).__name__
            status = "failed" if isinstance(exc, NotSent) else "unclear"
            with self.db.transaction() as conn:
                note = f"Not deleted: {error}"
                connectors.finish(conn, approval_id, status, to_iso(self.clock.now()), note=note)
                self._close(conn, approval_id, "failed", note, None)
            return status
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            where, params = scope.where()
            conn.execute(f"UPDATE bluesky_posts SET status = 'deleted' WHERE {where} AND rkey = ?", (*params, rkey))
            note = f"Deleted post {rkey} at Bluesky" + (" (dry run: the fake account)" if account.simulated else "")
            if gone:
                note = f"Post {rkey} was gone from Bluesky already"
            connectors.finish(
                conn, approval_id, "simulated" if account.simulated else "done", now, {"deleted": rkey}, note
            )
            self._close(conn, approval_id, "done", note, None)
        events.record(self.db, "info", "bluesky", f"Request #{approval_id}: {note}"[:300])
        return "done"

    def recover(self) -> int:
        """Rows left 'running' by a crash: unclear, never retried; the owner's Undo of a post too (the owner may press
        it again: a post gone already counts as deleted)."""
        with self.db.transaction() as conn:
            left = conn.execute("SELECT approval_id FROM bluesky_posts WHERE status = 'running'").fetchall()
            deleting = conn.execute(
                "SELECT j.approval_id, j.subject FROM action_journal j JOIN approvals a ON a.id = j.approval_id"
                " WHERE j.status = 'running' AND a.executor = 'bluesky_delete'"
            ).fetchall()
            for row in deleting:
                note = (
                    f"It is unclear whether post {row['subject']} was deleted ({DELETE_INTERRUPTED}). Ember won't try"
                    " again on its own: check Bluesky, or press Delete the post again (a post gone already counts as"
                    " deleted)."
                )
                connectors.finish(conn, row["approval_id"], "unclear", to_iso(self.clock.now()), note=note)
                self._close(conn, row["approval_id"], "failed", note, None)
        for row in left:
            note = f"It is unclear whether the post was made ({INTERRUPTED}). Ember won't try again; check the account."
            self._after(row["approval_id"], "unclear", None, note, INTERRUPTED)
        return len(left) + len(deleting)

    # --- how the posts do ---

    def sync(self, force: bool = False) -> str | None:
        """Read the account's followers and each live post's numbers, at most every SYNC_HOURS. Returns an error, or
        None."""
        account = self.account()
        if account is None or not self._lock.acquire(blocking=False):
            return None
        try:
            scope = self.scope()
            now = self.clock.now()
            last = self.db.get_meta(meta_key(scope.mode, "last_sync_at"))
            if not force and last and now - from_iso(last) < timedelta(hours=SYNC_HOURS):
                return None
            error = None
            try:
                info = account.info()  # logs in (or renews the login) first, posts or no posts
            except BlueskyError as exc:
                error = str(exc)[:300]
            else:
                for name, value in (
                    ("handle", info.handle),
                    ("did", info.did),
                    ("followers", str(info.followers)),
                    ("labels", ", ".join(info.labels)),
                    ("automated", "1" if info.automated else "0"),
                ):
                    self.db.set_meta(meta_key(scope.mode, name), value)
                error = self._read_posts(account, scope, now)
            self.db.set_meta(meta_key(scope.mode, "last_sync_at"), to_iso(now))
            self.db.set_meta(meta_key(scope.mode, "last_error"), error or "")
            return error
        finally:
            self._lock.release()

    def _read_posts(self, account: Account, scope: AgentScope, now: Any) -> str | None:
        where, params = scope.where()
        with self.db.connection() as conn:
            live = conn.execute(
                f"SELECT approval_id, uri FROM bluesky_posts WHERE {where} AND status = 'active' AND uri IS NOT NULL"
                " ORDER BY id DESC LIMIT ?",
                (*params, STATS_POSTS),
            ).fetchall()
        for start in range(0, len(live), STATS_BATCH):
            batch = live[start : start + STATS_BATCH]
            try:
                stats = account.post_stats([str(r["uri"]) for r in batch])
            except BlueskyError as exc:
                return str(exc)[:300]
            with self.db.transaction() as conn:
                for row in batch:
                    found = stats.get(str(row["uri"]))
                    if found is None:  # deleted at Bluesky (by the owner or moderation): the others are still read
                        conn.execute(
                            "UPDATE bluesky_posts SET status = 'deleted', result = ? WHERE approval_id = ?",
                            (GONE, row["approval_id"]),
                        )
                        continue
                    conn.execute(
                        "UPDATE bluesky_posts SET likes = ?, reposts = ?, replies = ?, quotes = ?, labels = ?,"
                        " synced_at = ? WHERE approval_id = ?",
                        (
                            found.likes,
                            found.reposts,
                            found.replies,
                            found.quotes,
                            ", ".join(found.labels)[:300] or None,
                            to_iso(now),
                            row["approval_id"],
                        ),
                    )
        return None
