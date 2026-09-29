"""SQLite storage and migrations.

Migrations are numbered SQL files in ``app/migrations`` (``0001_initial.sql``,
``0002_....sql``). Each one runs inside its own transaction together with the
row that records it, so a failing migration leaves the database unchanged.
Migrations run with foreign-key enforcement off (so SQLite's usual
"create new table, copy, drop, rename" rebuild works without cascading deletes)
and must pass ``PRAGMA foreign_key_check`` before they commit. They must not
contain transaction statements (BEGIN, COMMIT, ...); the runner refuses them.
Before migrating an existing database the file is copied to ``/data/backups``.

At runtime the whole process shares one connection (see :class:`Database`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_TRANSACTION_STATEMENT = re.compile(r"^(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE)
_LEADING_COMMENTS = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)
MAX_BACKUPS = 10


class MigrationError(RuntimeError):
    pass


class DatabaseTooNewError(MigrationError):
    """The database was written by a newer version of the app (downgrade)."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def utcnow() -> str:
    """Current UTC time as ISO 8601 with a ``Z`` suffix, e.g. ``2026-09-27T19:00:00Z``.

    One fixed format everywhere so timestamps sort correctly as text.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_migrations(directory: Path = paths.MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(f"badly named migration file: {path.name}")
        migrations.append(Migration(int(match.group(1)), match.group(2), path))
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be 1..N without gaps, found {versions}")
    return migrations


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into complete statements.

    Uses SQLite's own tokenizer (``complete_statement``), so semicolons inside
    strings, comments and CREATE TRIGGER bodies don't split a statement.
    """
    statements: list[str] = []
    start = 0
    for index, char in enumerate(sql):
        if char == ";" and sqlite3.complete_statement(sql[start : index + 1]):
            statement = sql[start : index + 1]
            if _LEADING_COMMENTS.sub("", statement).strip(" \t\r\n;"):
                statements.append(statement.strip())
            start = index + 1
    rest = sql[start:]
    if _LEADING_COMMENTS.sub("", rest).strip():
        raise MigrationError(f"incomplete SQL statement at the end: {rest.strip()[:80]!r}")
    return statements


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode; use :func:`transaction` for writes that belong together."""
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Without this, INSERT OR REPLACE deletes rows without firing DELETE triggers,
    # which would get around the append-only ledger.
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _refuse_on_event_loop() -> None:
    """The database layer is synchronous. On the event loop thread, coroutines
    would share the one connection and silently join each other's transactions,
    so database work there must go through asyncio.to_thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("database access on the event loop; use asyncio.to_thread")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # SQLite may already have rolled back (e.g. disk full); don't mask the real error.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def _check_applied(conn: sqlite3.Connection, migrations: list[Migration]) -> None:
    """0.12.0: an applied migration must be this version's migration of that number, by name, not only by number
    (a database migrated by another build's 0024 would skip this one's), and by content once its checksum is known."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    if "sha256" not in columns:
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN sha256 TEXT")
    by_version = {m.version: m for m in migrations}
    for version, name, digest in conn.execute("SELECT version, name, sha256 FROM schema_migrations").fetchall():
        known = by_version.get(version)
        if known is None:
            continue  # a newer version's: refused by migrate
        if name != known.name:
            raise MigrationError(
                f"migration {version:04d} in the database is {name!r}, but this version's is {known.name!r}: the "
                "database was written by another build (restore a backup, or use that build)"
            )
        if digest is None:
            conn.execute("UPDATE schema_migrations SET sha256 = ? WHERE version = ?", (_digest(known), version))
        elif digest != _digest(known):
            log.warning("Migration %04d_%s changed since it was applied", version, name)


def _digest(migration: Migration) -> str:
    return hashlib.sha256(migration.path.read_bytes()).hexdigest()


def migrate(db_file: Path, migrations: list[Migration] | None = None, backup_dir: Path | None = None) -> list[int]:
    """Bring the database up to date. Returns the versions that were applied."""
    migrations = discover_migrations() if migrations is None else migrations
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_file)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        done = applied_versions(conn)
        _check_applied(conn, migrations)
        known = {m.version for m in migrations}
        unknown = [v for v in done if v not in known]
        if unknown:
            raise DatabaseTooNewError(
                f"database has migrations {unknown} that this version does not know; "
                "it was written by a newer version (restore a backup or upgrade the app)"
            )
        pending = [m for m in migrations if m.version not in done]
        if not pending:
            return []
        # Parse everything first: a malformed later file must not leave earlier ones applied.
        scripts = [(m, _statements_of(m)) for m in pending]
        if done:
            _backup(conn, backup_dir or paths.backups_dir(), max(done))
        for migration, statements in scripts:
            _apply(conn, migration, statements)
            log.info("Applied database migration %04d_%s", migration.version, migration.name)
        return [m.version for m in pending]
    finally:
        conn.close()


def _statements_of(migration: Migration) -> list[str]:
    statements = split_statements(migration.path.read_text(encoding="utf-8"))
    for statement in statements:
        if _TRANSACTION_STATEMENT.match(_LEADING_COMMENTS.sub("", statement)):
            raise MigrationError(
                f"migration {migration.path.name} contains a transaction statement "
                f"({statement.split()[0].upper()}); the runner wraps each migration in its own transaction"
            )
    return statements


def _apply(conn: sqlite3.Connection, migration: Migration, statements: list[str]) -> None:
    # Foreign-key enforcement can only be switched outside a transaction.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            conn.execute(statement)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            first = dict(violations[0])
            raise MigrationError(
                f"migration {migration.path.name} would leave {len(violations)} broken foreign key(s), e.g. {first}"
            )
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at, sha256) VALUES (?, ?, ?, ?)",
            (migration.version, migration.name, utcnow(), _digest(migration)),
        )
        conn.execute("COMMIT")
    except (sqlite3.Error, MigrationError) as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"migration {migration.path.name} failed: {exc}") from exc
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _backup(conn: sqlite3.Connection, backup_dir: Path, current_version: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"ember-schema{current_version:04d}-{stamp}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    backups = sorted(backup_dir.glob("ember-schema*.db"))
    for old in backups[:-MAX_BACKUPS]:
        old.unlink(missing_ok=True)
    log.info("Backed up database to %s before migrating", target)
    return target


class Database:
    """The process's single SQLite connection, shared by all threads under a lock.

    One long-lived connection keeps the WAL files in place (no create/delete and
    checkpoint on every request, fewer writes to an SD card), and it lets code
    inside a transaction call other helpers without waiting on its own write lock:
    a nested :meth:`transaction` joins the outer one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def _open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(self.path)
        return self._conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        _refuse_on_event_loop()
        with self._lock:
            yield self._open()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        _refuse_on_event_loop()
        with self._lock:
            conn = self._open()
            if conn.in_transaction:
                # Called from inside another transaction() in this thread: join it.
                yield conn
                return
            with transaction(conn):
                yield conn

    def close(self) -> None:
        _refuse_on_event_loop()
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def schema_version(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            return int(row[0])

    # --- meta: small key/value facts about the agent's life (birth time, ...) ---

    def get_meta(self, key: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, utcnow()),
            )

    def set_meta_if_missing(self, key: str, value: str) -> str:
        """Store ``value`` unless the key exists; return whichever value is stored."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value, utcnow()),
            )
            return conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()["value"]

    # --- events: the system log shown in the dashboard ---

    def add_event(self, level: str, kind: str, message: str, details: dict[str, Any] | None = None) -> int:
        with self.connection() as conn:
            cur = conn.execute(
                "INSERT INTO events (ts, level, kind, message, details) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), level, kind, message, json.dumps(details, default=str) if details else None),
            )
            return int(cur.lastrowid)

    def prune_events(self, keep: int) -> int:
        """Delete all but the newest ``keep`` events. Returns how many were deleted."""
        with self.connection() as conn:
            cur = conn.execute(
                "DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (keep,),
            )
            return cur.rowcount

    def recent_events(self, limit: int = 50, min_level: str = "info") -> list[dict[str, Any]]:
        levels = ["debug", "info", "warning", "error"]
        allowed = levels[levels.index(min_level) :] if min_level in levels else levels
        placeholders = ",".join("?" for _ in allowed)
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT id, ts, level, kind, message, details FROM events WHERE level IN ({placeholders})"
                " ORDER BY id DESC LIMIT ?",
                (*allowed, limit),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"]) if row["details"] else None} for row in rows]
