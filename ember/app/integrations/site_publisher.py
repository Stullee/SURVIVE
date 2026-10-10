"""Publishing the blog on the owner's website (0.14.0): what the owner approved, uploaded by Ember's code over SFTP.

The agent proposes a post (executor 'site_post') or a new link page ('site_links'); Ember's code renders the page
when it is proposed (products/blog.py) and keeps exactly those bytes (site_uploads), which the owner previews and
approves. Approved, the page is uploaded once, like an Etsy listing or a pin: before anything is sent its rows are
committed as 'running' with the files they replace, so a crash never uploads twice ('unclear' when it can't be known
whether the server has the new file; the app's next start marks what a crash left running). A post goes up first,
then the blog's list with it (the server's list read first, so posts the owner uploaded themselves stay listed): the
list never links to a page that isn't there. Each page is checked again before it goes up (blog.audit), and must be
exactly the approved one (its SHA-256). Every upload is journaled (site.publish_post, site.publish_links), and the
owner's Undo ('site_restore') puts back what it replaced: the old version, or no file for a new post, and the list
without it. Only these files are ever written (blog.allowed): never the owner's home pages, Impressum, privacy page or
stylesheet. Without a connection the approved requests wait (a new try every RETRY_MINUTES) and fail after
GIVE_UP_HOURS. The dry run's server is a fake one: nothing leaves the app. 0.16.0: the live view's files
(blog.LIVE_FILES, made by live_view.py without an approval) go up over the same connection (``put``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from .. import events
from ..agent import website
from ..agent.store import AgentScope
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, from_iso, to_iso
from ..products import blog
from . import connectors, sftp

log = logging.getLogger(__name__)

POST = "site_post"
LINKS = "site_links"
RESTORE = "site_restore"
EXECUTORS = (POST, LINKS, RESTORE)
APPROVED = "('approved', 'approved_with_changes')"
CLOSED_BY = "Ember"
INTERRUPTED = "the app stopped while uploading"
RETRY_MINUTES = 30
GIVE_UP_HOURS = 24
_ADDRESS_LINES = re.compile(r"\n|\\n|,")


def meta_key(mode: str, name: str) -> str:
    return f"integrations.site.{mode}.{name}"


def owner_of(settings: Settings) -> blog.Owner:
    """The blog's owner data, from the options (the website's: site_*)."""
    lines = tuple(part.strip() for part in _ADDRESS_LINES.split(settings.site_address) if part.strip())
    return blog.Owner(
        name=settings.site_name.strip() or settings.agent_name,
        legal_name=settings.site_owner_name.strip(),
        town=blog.town(lines),
        email=settings.site_email.strip(),
        url=website.address(settings),  # 0.15.0: without a home page's file name
        agent=settings.agent_name,
    )


def login_of(settings: Settings) -> sftp.Login:
    """The owner's SFTP login, checked. Raises ValueError."""
    pin = settings.blog_sftp_host_key.strip()
    return sftp.Login(
        host=settings.blog_sftp_host.strip(),
        port=settings.blog_sftp_port,
        user=settings.blog_sftp_user.strip(),
        password=settings.blog_sftp_password.get_secret_value(),
        folder=sftp.folder_of(settings.blog_sftp_folder),
        host_key=sftp.pin_of(pin) if pin else "",
    )


def problems(settings: Settings, mode: str) -> list[str]:
    """What keeps the blog from being published (none: it can be). A dry run needs no login: its server is fake."""
    if not settings.blog_enabled:
        return ["the blog is off (blog_enabled)"]
    found = owner_of(settings).problems()
    if mode == "live":
        for name, value in (
            ("blog_sftp_host", settings.blog_sftp_host),
            ("blog_sftp_user", settings.blog_sftp_user),
            ("blog_sftp_password", settings.blog_sftp_password.get_secret_value()),
        ):
            if not value.strip():
                found.append(f"{name} is missing")
        try:
            login_of(settings)
        except ValueError as exc:
            found.append(str(exc))
    return found


# --- what Ember's code knows of the site -----------------------------------------------------------------------------


def posts(conn: sqlite3.Connection, scope: AgentScope) -> list[sqlite3.Row]:
    """The blog's list as Ember's code last read or wrote it on the server, the newest first."""
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM blog_posts WHERE {where} ORDER BY day DESC, position", params).fetchall()


def known(conn: sqlite3.Connection, scope: AgentScope, slug: str) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM blog_posts WHERE {where} AND slug = ?", (*params, slug)).fetchone()


_POST_PATH = re.compile(rf"{blog.POSTS}/([a-z0-9-]{{1,{blog.SLUG_MAX}}})(?:\.html|/)?")


def _live_pages(db: Database, mode: str) -> list[str]:
    """The live view's pages on the server (its .html files of the last upload)."""
    raw = db.get_meta(f"integrations.live.{mode}.on_server")  # live_view.key(mode, "on_server")
    try:
        files = json.loads(raw) if raw else []
    except ValueError:
        return []
    shown = {str(path) for path in files} if isinstance(files, list) else set()
    return [path for path in blog.LIVE_FILES if path.endswith(".html") and path in shown]


def known_page(
    conn: sqlite3.Connection, db: Database, scope: AgentScope, base: str, link: str, site_pages: bool = False
) -> tuple[str, sqlite3.Row | None] | None:
    """0.19.2: the page of the owner's website (``base``: website.address) that ``link`` names, as Ember's records
    know it: its address as the server has it and, for a post of the blog, its row (a card's title and description);
    None when Ember's code knows no such page. Live, two Bluesky posts linked blog posts without their ".html", one by
    its file's name instead of its slug: both addresses were missing pages. Known: the home page, the blog's list and
    its posts (a post's address without ".html" or with a closing slash is its address), the link page Ember's code
    uploaded, the live view's pages once on the server and, with ``site_pages`` (site_enabled), the site's pages."""
    if not base:
        return None
    site, parts = urlsplit(base), urlsplit(link.strip())
    if parts.scheme != "https" or parts.netloc.lower() != site.netloc.lower() or parts.query or parts.fragment:
        return None
    folder = site.path.rstrip("/")
    if parts.path != folder and not parts.path.startswith(f"{folder}/"):
        return None
    path = parts.path[len(folder) :].lstrip("/")
    if path in ("", "index.html"):
        return f"{base}/", None
    if path in (blog.POSTS, f"{blog.POSTS}/", blog.INDEX):
        return f"{base}/{blog.POSTS}/", None
    post = _POST_PATH.fullmatch(path)
    if post is not None:
        row = known(conn, scope, post[1]) if post[1] not in blog.RESERVED else None
        return (f"{base}/{blog.post_path(post[1])}", row) if row is not None else None
    if path == blog.LINKS and links(db, scope.mode) is not None:
        return f"{base}/{blog.LINKS}", None
    if path in _live_pages(db, scope.mode):
        return f"{base}/{path}", None
    if site_pages and path.endswith(".html") and path[:-5] in {str(r["slug"]) for r in website.pages(conn, scope)}:
        return f"{base}/{path}", None
    return None


def known_pages(
    conn: sqlite3.Connection, db: Database, scope: AgentScope, base: str, posts_shown: int = 8
) -> list[str]:
    """0.19.2: the addresses of the owner's website a post may link, for the agent: the blog's newest posts first."""
    if not base:
        return []
    found = [f"{base}/{blog.post_path(str(r['slug']))}" for r in posts(conn, scope)[:posts_shown]]
    found += [f"{base}/{blog.POSTS}/", f"{base}/"]
    if links(db, scope.mode) is not None:
        found.append(f"{base}/{blog.LINKS}")
    return found + [f"{base}/{path}" for path in _live_pages(db, scope.mode)]


def remember(
    conn: sqlite3.Connection,
    scope: AgentScope,
    index: blog.Index,
    now: str,
    by: dict[str, int | None] | None = None,
) -> None:
    """The server's list as it is now (``by``: the requests that published posts in it, by slug)."""
    where, params = scope.where()
    before = {r["slug"]: r["approval_id"] for r in posts(conn, scope)}
    conn.execute(f"DELETE FROM blog_posts WHERE {where}", params)
    for position, entry in enumerate(index.entries):
        approval_id = (by or {}).get(entry.slug, before.get(entry.slug))
        conn.execute(
            "INSERT INTO blog_posts (mode, session, slug, title, description, day, position, approval_id, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope.mode,
                scope.session,
                entry.slug,
                entry.title[:200],
                entry.description[:340],
                entry.date,
                position,
                approval_id,
                now,
            ),
        )


def propose(conn: sqlite3.Connection, scope: AgentScope, approval_id: int, path: str, data: bytes) -> None:
    """The page a request uploads, rendered when it is proposed: the owner approves exactly these bytes."""
    conn.execute(
        "INSERT INTO site_uploads (mode, session, approval_id, path, op, content, sha256, status)"
        " VALUES (?, ?, ?, ?, 'write', ?, ?, 'proposed')",
        (scope.mode, scope.session, approval_id, path, data, blog.sha256(data)),
    )


def waiting_for(conn: sqlite3.Connection, scope: AgentScope, executor: str, slug: str | None) -> list[sqlite3.Row]:
    """Requests of this kind (for this post) that wait for the owner or for the upload."""
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT * FROM approvals WHERE {where} AND executor = ? AND (status = 'pending' OR (status IN {APPROVED}"
        " AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id))) ORDER BY id",
        (*params, executor),
    ).fetchall()
    return [r for r in rows if slug is None or _action(r).get("slug") == slug]


def page(conn: sqlite3.Connection, scope: AgentScope, approval_id: int) -> tuple[str, bytes] | None:
    """The page a request uploads (its path and bytes), for the owner's preview."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT path, content FROM site_uploads WHERE {where} AND approval_id = ? AND path <> ? AND content IS NOT"
        " NULL ORDER BY id LIMIT 1",
        (*params, approval_id, blog.INDEX),
    ).fetchone()
    return (str(row["path"]), bytes(row["content"])) if row is not None else None


def links(db: Database, mode: str) -> dict[str, Any] | None:
    """The link page as Ember's code last uploaded it (bio, links, request, day), or None."""
    raw = db.get_meta(meta_key(mode, "links"))
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _action(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["action"] or "null")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def execution(conn: sqlite3.Connection, row: sqlite3.Row, db: Database, settings: Settings) -> dict[str, Any] | None:
    """What happened to an approved upload, or to the owner's Undo of one, for the dashboard (None before approval)."""
    entry = conn.execute(
        "SELECT * FROM action_journal WHERE approval_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
    ).fetchone()
    path = str(_action(row).get("path") or "")
    url = f"{owner_of(settings).url}/{path}" if path and settings.site_url else None
    if entry is not None:
        status = {"simulated": "done"}.get(str(entry["status"]), str(entry["status"]))
        return {
            "status": status,
            "started_at": entry["started_at"],
            "finished_at": entry["finished_at"],
            "result": entry["note"],
            "error": None,
            "url": url
            if status in ("done", "partial") and row["executor"] != RESTORE and row["mode"] == "live"
            else None,
        }
    if row["status"] not in ("approved", "approved_with_changes"):
        return None
    error = db.get_meta(meta_key(row["mode"], "last_error")) or None
    return {
        "status": "waiting",
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": error,
        "url": None,
    }


def why_not(conn: sqlite3.Connection, scope: AgentScope, row: sqlite3.Row) -> str | None:
    """Why the owner's Undo of an upload can't be carried out now (None: it can): only the newest change of a page."""
    where, params = scope.where("j")
    later = conn.execute(
        f"SELECT j.id FROM action_journal j WHERE {where} AND j.subject = ? AND j.class IN ('site.publish_post',"
        " 'site.publish_links') AND j.id > ? AND j.status NOT IN ('failed', 'running') AND NOT EXISTS (SELECT 1"
        " FROM action_undos u JOIN approvals a ON a.id = u.approval_id WHERE u.journal_id = j.id AND a.status ="
        " 'done') ORDER BY j.id LIMIT 1",
        (*params, row["subject"], row["id"]),
    ).fetchone()
    where, params = scope.where()
    if later is not None:
        return f"a later change of this page (#{later['id']}) is newer: undo that one first"
    waiting = conn.execute(
        f"SELECT 1 FROM site_uploads WHERE {where} AND status = 'running' LIMIT 1", params
    ).fetchone()
    return "an upload is under way: try again in a minute" if waiting else None


def text(conn: sqlite3.Connection, db: Database, scope: AgentScope, settings: Settings) -> str:
    """The plan's BLOG: the problems first (if any), then the site's posts, the link page and what waits."""
    owner = owner_of(settings)
    lines = []
    trouble = problems(settings, scope.mode)
    if trouble:
        lines.append(f"Your owner's blog can't be published yet: {'; '.join(trouble)}. Tell them.")
    error = db.get_meta(meta_key(scope.mode, "last_error"))
    if error:
        lines.append(f"The last connection to the server failed: {error}")
    shown = posts(conn, scope)
    seen = shown[0]["seen_at"][:10] if shown else None
    head = f"Posts on {owner.url}/blog/" + (f" (as read {seen})" if seen else "")
    if shown:
        # 0.19.2: each at its address (live, a post linked one as the blog's address plus its slug: a missing page)
        lines.append(
            f"{head}: "
            + "; ".join(
                f"{owner.url}/{blog.post_path(r['slug'])} {json.dumps(r['title'], ensure_ascii=False)} ({r['day']})"
                for r in shown[:15]
            )
            + (f"; and {len(shown) - 15} more" if len(shown) > 15 else "")
            + "."
        )
    else:
        lines.append(f"{head}: none known yet (Ember's code reads the list when it uploads the first post).")
    page_now = links(db, scope.mode)
    if page_now:
        listed = ", ".join(f"{x.get('label')} -> {x.get('url')}" for x in page_now.get("links", []))
        lines.append(
            f"Link page ({owner.url}/{blog.LINKS}, uploaded {page_now.get('day')}): bio "
            f"{json.dumps(page_now.get('bio', ''), ensure_ascii=False)}; links: {listed}."
        )
    else:
        lines.append("Link page: your owner's own; Ember's code hasn't changed it yet.")
    for executor in (POST, LINKS):
        for r in waiting_for(conn, scope, executor, None):
            state = "waits for your owner" if r["status"] == "pending" else "approved: Ember's code uploads it soon"
            lines.append(f"Request #{r['id']} ({r['title']}) {state}.")
    return "\n".join(lines)


class Publisher:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        scope: Callable[[], AgentScope],
        mode: str,
        connect: Callable[[sftp.Login, str | None], sftp.Server] | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.scope = scope
        self.mode = mode
        self.fake = sftp.FakeServer() if mode == "dry_run" else None
        self._connect_fn = connect or sftp.connect
        self._lock = threading.Lock()  # one run (or check) at a time in this process

    # --- the connection ---

    def _pinned(self, login: sftp.Login) -> str | None:
        if login.host_key:
            return login.host_key
        raw = self.db.get_meta(meta_key(self.mode, "host_key"))
        try:
            stored = json.loads(raw) if raw else None
        except ValueError:
            stored = None
        if isinstance(stored, dict) and stored.get("where") == login.where:
            return str(stored.get("fingerprint") or "") or None
        return None

    def _connect(self) -> sftp.Server:
        """The server: the fake one in a dry run, the owner's live (its key pinned at the first connection, unless
        the owner pinned one). Raises sftp.NotSent."""
        if self.fake is not None:
            return self.fake
        try:
            login = login_of(self.settings)
        except ValueError as exc:
            raise sftp.NotSent(str(exc)) from None
        if not (login.host and login.user and login.password):
            raise sftp.NotSent("the SFTP login is missing (blog_sftp_host, blog_sftp_user, blog_sftp_password)")
        pinned = self._pinned(login)
        server = self._connect_fn(login, pinned)
        if pinned is None:
            now = to_iso(self.clock.now())
            stored = {"where": login.where, "fingerprint": server.fingerprint, "since": now}
            self.db.set_meta(meta_key(self.mode, "host_key"), json.dumps(stored))
            events.record(
                self.db,
                "info",
                "website",
                f"Pinned the SFTP server's key of {login.where}: {server.fingerprint}. Ember's code sends nothing to a "
                "server with another key; compare it with your host's (see the Documentation tab).",
            )
        return server

    def host_key(self) -> str | None:
        """The key the server must show: the owner's pin, or the one pinned at the first connection."""
        if self.fake is not None:
            return self.fake.fingerprint
        try:
            return self._pinned(login_of(self.settings))
        except ValueError:
            return None

    def _failed_connection(self, error: str) -> None:
        key = meta_key(self.mode, "last_error")
        if self.db.get_meta(key) != error:
            events.record(self.db, "warning", "website", f"No SFTP connection: {error}"[:300])
        self.db.set_meta(key, error[:500])
        self.db.set_meta(meta_key(self.mode, "last_attempt_at"), to_iso(self.clock.now()))

    def _may_try(self) -> bool:
        last = self.db.get_meta(meta_key(self.mode, "last_attempt_at"))
        if not last or not self.db.get_meta(meta_key(self.mode, "last_error")):
            return True
        return self.clock.now() - from_iso(last) >= timedelta(minutes=RETRY_MINUTES)

    # --- carrying out ---

    def run(self) -> list[tuple[int, str]]:
        """Upload the approved pages (and carry out the owner's Undos) that are due."""
        if not self.settings.blog_enabled or not self._lock.acquire(blocking=False):
            return []
        try:
            scope = self.scope()
            due = self._approved(scope)
            if not due or not self._may_try():
                return []
            try:
                server = self._connect()
            except sftp.NotSent as exc:
                self._failed_connection(str(exc))
                return self._give_up(scope, due, str(exc))
            self.db.set_meta(meta_key(self.mode, "last_error"), "")
            done = []
            try:
                for approval_id, executor in due:
                    if executor == RESTORE:
                        outcome = self._restore(server, scope, approval_id)
                    else:
                        outcome = self._publish(server, scope, approval_id)
                    done.append((approval_id, outcome))
                    if outcome == connectors.HALTED:
                        break  # 0.37.1: the kill switch came on: the rest waits
            finally:
                if server is not self.fake:
                    server.close()
            return done
        finally:
            self._lock.release()

    def _approved(self, scope: AgentScope) -> list[tuple[int, str]]:
        where, params = scope.where()
        marks = ", ".join("?" for _ in EXECUTORS)
        with self.db.connection() as conn:
            return [
                (int(r["id"]), str(r["executor"]))
                for r in conn.execute(
                    f"SELECT id, executor FROM approvals WHERE {where} AND executor IN ({marks})"
                    f" AND status IN {APPROVED}"
                    " AND NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = approvals.id)"
                    " AND NOT EXISTS (SELECT 1 FROM site_uploads u WHERE u.approval_id = approvals.id"
                    " AND u.status <> 'proposed') ORDER BY id",
                    (*params, *EXECUTORS),
                )
            ]

    def _give_up(self, scope: AgentScope, due: list[tuple[int, str]], error: str) -> list[tuple[int, str]]:
        """Approved requests that waited GIVE_UP_HOURS for a connection fail (nothing was sent); the rest wait."""
        now = self.clock.now()
        given_up = []
        for approval_id, _ in due:
            with self.db.transaction() as conn:
                row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
                if row is None or row["status"] not in ("approved", "approved_with_changes"):
                    continue
                if now - from_iso(row["decided_at"]) < timedelta(hours=GIVE_UP_HOURS):
                    continue
                stamp = to_iso(now)
                note = f"Not uploaded: no connection to the server for {GIVE_UP_HOURS} hours ({error})"
                connectors.begin(conn, approval_id, stamp, subject=_action(row).get("path") or None)
                connectors.finish(conn, approval_id, "failed", stamp, note=note[:500])
                self._close(conn, approval_id, "failed", note, None)
            given_up.append((approval_id, "failed"))
        return given_up

    def _publish(self, server: sftp.Server, scope: AgentScope, approval_id: int) -> str:
        """A post (with the blog's list) or the link page, as the owner approved it."""
        owner = owner_of(self.settings)
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            upload = conn.execute(
                "SELECT * FROM site_uploads WHERE approval_id = ? AND path <> ? ORDER BY id LIMIT 1",
                (approval_id, blog.INDEX),
            ).fetchone()
            stopped = connectors.halted(conn)
        if row is None or row["status"] not in ("approved", "approved_with_changes"):
            return "skipped"
        if stopped:
            return connectors.HALTED  # 0.37.1: the kill switch came on while this round ran (the server isn't read)
        action = _action(row)
        path = str(action.get("path") or "")
        content = bytes(upload["content"]) if upload is not None and upload["content"] is not None else b""
        problem = None
        if upload is None or not content or upload["path"] != path or not blog.allowed(path):
            problem = "the approved page is missing"
        elif blog.sha256(content) != action.get("sha256") or upload["sha256"] != action.get("sha256"):
            problem = "the page isn't the one that was approved"
        else:
            found = blog.audit(content, owner)
            problem = f"the page didn't pass the check: {'; '.join(found)}" if found else None
        files: list[tuple[str, bytes]] = [(path, content)]
        before: dict[str, bytes | None] = {}
        merged: blog.Index | None = None
        if problem is None:
            try:
                before[path] = server.read(path)
                if row["executor"] == POST:
                    before[blog.INDEX] = server.read(blog.INDEX)
                    entry = blog.Entry(
                        str(action["slug"]), str(action["title"]), str(action["description"]), str(action["date"])
                    )
                    merged = blog.merge(blog.read_index(before[blog.INDEX]), entry)
                    listed = blog.render_index(merged, owner)
                    found = blog.audit(listed, owner)
                    if found:
                        raise blog.BlogError(f"the blog's list didn't pass the check: {'; '.join(found)}")
                    files.append((blog.INDEX, listed))
            except (sftp.NotSent, blog.BlogError, KeyError) as exc:
                problem = str(exc) if not isinstance(exc, KeyError) else "the request is broken"
        stamp = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in ("approved", "approved_with_changes"):
                return "skipped"  # cancelled meanwhile
            started = conn.execute(
                "SELECT 1 FROM site_uploads WHERE approval_id = ? AND status <> 'proposed'", (approval_id,)
            ).fetchone()
            if started is not None:
                return "skipped"
            if connectors.halted(conn):
                return connectors.HALTED  # 0.37.1: pressed while the server was read
            fingerprints = {name: (blog.sha256(data) if data is not None else None) for name, data in before.items()}
            connectors.begin(conn, approval_id, stamp, subject=path or None, before={"pages": fingerprints})
            if problem is not None:
                note = f"Not uploaded: {problem}"
                connectors.finish(conn, approval_id, "failed", stamp, note=note[:500])
                self._close(conn, approval_id, "failed", note, None)
                events.record(self.db, "warning", "website", f"Request #{approval_id}: {note}"[:300])
                return "failed"
            for name, data in files:
                self._running(conn, scope, approval_id, name, data, before.get(name), stamp)
        # Committed: from here on these files are never uploaded a second time, whatever happens.
        sent: list[str] = []
        try:
            for name, data in files:
                if before.get(name) != data:
                    server.write(name, data)
                sent.append(name)
        except Exception as exc:  # noqa: BLE001 - reported on the request, never raised
            return self._broken(scope, approval_id, path, sent, files, exc)
        url = f"{owner.url}/{path}"
        unchanged = all(before.get(name) == data for name, data in files)
        note = (
            f"Uploaded {path}" + (f" and the blog's list ({blog.INDEX})" if len(files) > 1 else "") + f": {url}"
            if not unchanged
            else f"{path} was on the server as approved already: nothing to upload ({url})"
        )
        if server.simulated:
            note = f"Uploaded {path} to the dry run's fake server; nothing reached your website."
        after = {"pages": {name: blog.sha256(data) for name, data in files}}
        link = None if server.simulated else url  # the dry run's page isn't at the address
        self._done(scope, approval_id, path, "simulated" if server.simulated else "done", after, note, link, merged)
        if row["executor"] == LINKS:
            self.db.set_meta(
                meta_key(self.mode, "links"),
                json.dumps(
                    {
                        "approval_id": approval_id,
                        "day": stamp[:10],
                        "bio": action.get("bio", ""),
                        "links": action.get("links", []),
                    },
                    ensure_ascii=False,
                ),
            )
        events.record(self.db, "info", "website", f"Request #{approval_id}: {note}"[:300])
        return "done"

    @staticmethod
    def _running(
        conn: sqlite3.Connection,
        scope: AgentScope,
        approval_id: int,
        path: str,
        data: bytes | None,
        before: bytes | None,
        stamp: str,
        op: str = "write",
    ) -> None:
        """A file's row, committed as 'running' with the file it replaces, before anything is sent."""
        values = (before, blog.sha256(before) if before is not None else None, stamp)
        updated = conn.execute(
            "UPDATE site_uploads SET status = 'running', before = ?, before_sha256 = ?, started_at = ?"
            " WHERE approval_id = ? AND path = ? AND status = 'proposed'",
            (*values, approval_id, path),
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO site_uploads (mode, session, approval_id, path, op, content, sha256, before,"
                " before_sha256, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    scope.mode,
                    scope.session,
                    approval_id,
                    path,
                    op,
                    data,
                    blog.sha256(data) if data is not None else None,
                    *values,
                ),
            )

    def _done(
        self,
        scope: AgentScope,
        approval_id: int,
        path: str,
        status: str,
        after: dict[str, Any],
        note: str,
        link: str | None,
        index: blog.Index | None,
        by: dict[str, int | None] | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            conn.execute(
                "UPDATE site_uploads SET status = 'done', finished_at = ? WHERE approval_id = ? AND status = 'running'",
                (now, approval_id),
            )
            if index is not None:
                slug = re.fullmatch(r"blog/([a-z0-9-]+)\.html", path)
                mine: dict[str, int | None] = {slug[1]: approval_id} if slug else {}
                remember(conn, scope, index, now, by if by is not None else mine)
            connectors.finish(conn, approval_id, status, now, after, note, subject=path)
            self._close(conn, approval_id, "done", note, link)

    def _broken(
        self,
        scope: AgentScope,
        approval_id: int,
        path: str,
        sent: list[str],
        files: list[tuple[str, bytes]] | list[tuple[str, bytes | None]],
        exc: Exception,
    ) -> str:
        """An upload that stopped: failed (nothing changed), partial (the post is up, its list isn't) or unclear."""
        error = str(exc) if isinstance(exc, sftp.SftpError) else type(exc).__name__
        if not isinstance(exc, sftp.SftpError):
            log.exception("Uploading request #%d failed", approval_id)
        failing = files[len(sent)][0] if len(sent) < len(files) else path
        if isinstance(exc, sftp.NotSent) and not sent:
            status, note = "failed", f"Not uploaded: {error}"
        elif isinstance(exc, sftp.NotSent):
            status = "partial"
            note = f"{', '.join(sent)} uploaded, but not {failing}: {error}. Check the blog's list on your site."
        else:
            status = "unclear"
            note = f"It is unclear whether {failing} is on your server ({error}). Ember won't try again; check it."
        with self.db.transaction() as conn:
            now = to_iso(self.clock.now())
            for name, _ in files:
                state = "done" if name in sent else ("failed" if status != "unclear" else "unclear")
                conn.execute(
                    "UPDATE site_uploads SET status = ?, finished_at = ?, error = ? WHERE approval_id = ? AND path = ?"
                    " AND status = 'running'",
                    (state, now, None if state == "done" else error[:500], approval_id, name),
                )
            connectors.finish(conn, approval_id, status, now, note=note[:500], subject=path)
            self._close(conn, approval_id, "done" if status == "partial" else "failed", note, None)
        events.record(self.db, "warning", "website", f"Request #{approval_id}: {note}"[:300])
        return status

    def _restore(self, server: sftp.Server, scope: AgentScope, approval_id: int) -> str:
        """The owner's Undo of an upload: the old version back (or no file, for a new post), and the blog's list
        without the post (or with its old entry). Only if the page is still the one Ember's code uploaded."""
        owner = owner_of(self.settings)
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None or row["status"] not in ("approved", "approved_with_changes"):
                return "skipped"
            if connectors.halted(conn):
                return connectors.HALTED  # 0.37.1: an Undo neither, once the kill switch is on (the server isn't read)
            action = _action(row)
            original = int(action.get("approval_id") or 0)
            ups = {
                str(u["path"]): u
                for u in conn.execute(
                    "SELECT * FROM site_uploads WHERE approval_id = ? AND status = 'done'", (original,)
                ).fetchall()
            }
            published = conn.execute("SELECT action FROM approvals WHERE id = ?", (original,)).fetchone()
        path = str(action.get("path") or "")
        slug = _action(published).get("slug") if published is not None else None
        main = ups.get(path)
        problem = None
        files: list[tuple[str, bytes | None, str]] = []  # (path, new content or None to remove, op)
        before: dict[str, bytes | None] = {}
        merged: blog.Index | None = None
        if main is None or not blog.allowed(path):
            problem = "Ember's code has no record of that upload"
        else:
            try:
                before[path] = server.read(path)
                if before[path] is None or blog.sha256(before[path]) != main["sha256"]:
                    raise sftp.NotSent(
                        f"{path} on your server isn't the page Ember's code uploaded (#{original}) anymore: nothing "
                        "was changed"
                    )
                old = bytes(main["before"]) if main["before"] is not None else None
                files.append((path, old, "write" if old is not None else "remove"))
                if blog.INDEX in ups and slug:
                    listed = ups[blog.INDEX]
                    previous_list = bytes(listed["before"]) if listed["before"] is not None else None
                    previous = next((e for e in blog.read_index(previous_list).entries if e.slug == slug), None)
                    if old is None:
                        previous = None  # a new post: off the list
                    before[blog.INDEX] = server.read(blog.INDEX)
                    merged = blog.without(blog.read_index(before[blog.INDEX]), str(slug), previous)
                    files.append((blog.INDEX, blog.render_index(merged, owner), "write"))
            except (sftp.NotSent, blog.BlogError) as exc:
                problem = str(exc)
        stamp = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            current = conn.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if current is None or current["status"] not in ("approved", "approved_with_changes"):
                return "skipped"
            if connectors.halted(conn):
                return connectors.HALTED  # 0.37.1: pressed while the server was read
            fingerprints = {name: (blog.sha256(data) if data is not None else None) for name, data in before.items()}
            connectors.begin(conn, approval_id, stamp, subject=path or None, before={"pages": fingerprints})
            if problem is not None:
                note = f"Not undone: {problem}"
                connectors.finish(conn, approval_id, "failed", stamp, note=note[:500])
                self._close(conn, approval_id, "failed", note, None)
                return "failed"
            for name, data, op in files:
                self._running(conn, scope, approval_id, name, data, before.get(name), stamp, op)
        sent: list[str] = []
        try:
            for name, data, _ in files:
                if data is None:
                    server.remove(name)
                else:
                    server.write(name, data)
                sent.append(name)
        except Exception as exc:  # noqa: BLE001 - reported on the request, never raised
            return self._broken(scope, approval_id, path, sent, [(n, d) for n, d, _ in files], exc)
        what = "took it off your server" if files[0][1] is None else "put the version before it back"
        note = f"Undid request #{original} ({path}): {what}" + (" and changed the blog's list" if merged else "")
        if server.simulated:
            note += " (dry run: the fake server)"
        after = {"pages": {name: (blog.sha256(d) if d is not None else None) for name, d, _ in files}}
        by: dict[str, int | None] | None = None
        if merged is not None and slug:
            by = {} if files[0][1] is None else {str(slug): None}  # the old version: not published by a request known
        self._done(scope, approval_id, path, "simulated" if server.simulated else "done", after, note, None, merged, by)
        if path == blog.LINKS:
            self.db.set_meta(meta_key(self.mode, "links"), "")
        events.record(self.db, "info", "website", f"Request #{approval_id}: {note}"[:300])
        return "done"

    def _close(self, conn: sqlite3.Connection, approval_id: int, outcome: str, note: str, link: str | None) -> None:
        """Close the approval (unless the owner did meanwhile): the agent hears the result at its next wake."""
        conn.execute(
            "UPDATE approvals SET status = ?, closed_at = ?, closed_by = ?, result_note = ?, result_link = ?,"
            f" version = version + 1, seen_cycle_id = NULL WHERE id = ? AND status IN {APPROVED}",
            (outcome, to_iso(self.clock.now()), CLOSED_BY, note[:2000], link, approval_id),
        )

    def recover(self) -> int:
        """Uploads left 'running' by a crash: unclear, never retried."""
        with self.db.transaction() as conn:
            left = [
                int(r[0])
                for r in conn.execute("SELECT DISTINCT approval_id FROM site_uploads WHERE status = 'running'")
            ]
            now = to_iso(self.clock.now())
            for approval_id in left:
                note = f"It is unclear whether the upload finished ({INTERRUPTED}). Ember won't try again; check it."
                conn.execute(
                    "UPDATE site_uploads SET status = 'unclear', finished_at = ?, error = ? WHERE approval_id = ?"
                    " AND status = 'running'",
                    (now, INTERRUPTED, approval_id),
                )
                connectors.finish(conn, approval_id, "unclear", now, note=note)
                self._close(conn, approval_id, "failed", note, None)
        return len(left)

    # --- the live view (0.16.0) ---

    def put(self, files: dict[str, bytes]) -> bool | None:
        """Write the live view's files (live_view.py checked them), over this publisher's connection: True if the
        server was the dry run's fake one, None if an upload of the blog is under way (the next round). Raises
        sftp.SftpError."""
        if any(path not in blog.LIVE_FILES for path in files):
            raise sftp.NotSent("only the live view's files are written this way")
        if not self._lock.acquire(blocking=False):
            return None
        try:
            server = self._connect()
            try:
                for path, data in files.items():
                    server.write(path, data)
            finally:
                if server is not self.fake:
                    server.close()
            return server.simulated
        finally:
            self._lock.release()

    # --- the owner's check ---

    def check(self) -> dict[str, Any]:
        """The owner's "Check the connection": log in (pinning the key at the first time), read the blog's list.
        Raises sftp.SftpError or blog.BlogError with what is wrong."""
        if not self._lock.acquire(timeout=5):
            raise sftp.NotSent("Ember's code is uploading right now: try again in a minute")
        try:
            server = self._connect()
            try:
                listed = server.read(blog.INDEX)
                has_links = server.read(blog.LINKS) is not None
            finally:
                if server is not self.fake:
                    server.close()
            self.db.set_meta(meta_key(self.mode, "last_error"), "")
            index = blog.read_index(listed)
            with self.db.transaction() as conn:
                remember(conn, self.scope(), index, to_iso(self.clock.now()))
            return {
                "fingerprint": server.fingerprint,
                "simulated": server.simulated,
                "posts": len(index.entries),
                "index": listed is not None,
                "links": has_links,
            }
        finally:
            self._lock.release()
