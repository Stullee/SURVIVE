"""SQLite storage and migrations.

Migrations are numbered SQL files in ``app/migrations`` (``0001_initial.sql``,
``0002_....sql``). Each one runs inside its own transaction together with the
row that records it, so a failing migration leaves the database unchanged.
Before migrating an existing database the file is copied to ``/data/backups``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
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


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode; use :func:`transaction` for writes that belong together."""
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(db_file: Path, migrations: list[Migration] | None = None, backup_dir: Path | None = None) -> list[int]:
    """Bring the database up to date. Returns the versions that were applied."""
    migrations = discover_migrations() if migrations is None else migrations
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_file)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        done = applied_versions(conn)
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
        if done:
            _backup(conn, backup_dir or paths.backups_dir(), max(done))
        for migration in pending:
            _apply(conn, migration)
            log.info("Applied database migration %04d_%s", migration.version, migration.name)
        return [m.version for m in pending]
    finally:
        conn.close()


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    sql = migration.path.read_text(encoding="utf-8")
    try:
        # executescript runs the statements as written; the explicit BEGIN/COMMIT
        # keeps the migration and its bookkeeping row in one transaction.
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\n;")
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, utcnow()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"migration {migration.path.name} failed: {exc}") from exc


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
    """Opens a short-lived connection per unit of work (safe across threads with WAL)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn, transaction(conn):
            yield conn

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
