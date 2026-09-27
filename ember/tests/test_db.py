from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import db as dbmod
from app.db import Database, DatabaseTooNewError, MigrationError, discover_migrations, migrate


def _write(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


def test_fresh_database_gets_all_migrations(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    applied = migrate(db_file, backup_dir=tmp_path / "backups")
    assert applied == [m.version for m in discover_migrations()]
    database = Database(db_file)
    assert database.schema_version() == len(discover_migrations())
    with database.connection() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"schema_migrations", "meta", "events"} <= tables
    # A brand-new database has nothing to back up.
    assert not (tmp_path / "backups").exists()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file, backup_dir=tmp_path / "backups")
    assert migrate(db_file, backup_dir=tmp_path / "backups") == []


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    with Database(db_file).connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_new_migration_applies_and_backs_up(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    _write(mig_dir, "0001_first.sql", "CREATE TABLE a (x INTEGER);")
    db_file = tmp_path / "ember.db"
    migrate(db_file, discover_migrations(mig_dir), backup_dir=tmp_path / "backups")
    with Database(db_file).connection() as conn:
        conn.execute("INSERT INTO a (x) VALUES (42)")

    _write(mig_dir, "0002_second.sql", "ALTER TABLE a ADD COLUMN y TEXT;")
    assert migrate(db_file, discover_migrations(mig_dir), backup_dir=tmp_path / "backups") == [2]

    backups = list((tmp_path / "backups").glob("ember-schema0001-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT x FROM a").fetchone()[0] == 42
        columns = [row[1] for row in conn.execute("PRAGMA table_info(a)")]
    assert columns == ["x"]  # the backup is the pre-migration state


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    _write(mig_dir, "0001_first.sql", "CREATE TABLE a (x INTEGER);")
    db_file = tmp_path / "ember.db"
    migrate(db_file, discover_migrations(mig_dir))

    _write(mig_dir, "0002_broken.sql", "CREATE TABLE b (y INTEGER);\nTHIS IS NOT SQL;")
    with pytest.raises(MigrationError, match="0002_broken.sql"):
        migrate(db_file, discover_migrations(mig_dir), backup_dir=tmp_path / "backups")

    database = Database(db_file)
    assert database.schema_version() == 1
    with database.connection() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "b" not in tables  # the half-applied migration left nothing behind


def test_refuses_database_from_newer_version(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    _write(mig_dir, "0001_first.sql", "CREATE TABLE a (x INTEGER);")
    _write(mig_dir, "0002_second.sql", "CREATE TABLE b (x INTEGER);")
    db_file = tmp_path / "ember.db"
    migrate(db_file, discover_migrations(mig_dir))
    (mig_dir / "0002_second.sql").unlink()
    with pytest.raises(DatabaseTooNewError):
        migrate(db_file, discover_migrations(mig_dir))


def test_migration_names_are_validated(tmp_path: Path) -> None:
    _write(tmp_path, "0001_ok.sql", "")
    _write(tmp_path, "2_bad.sql", "")
    with pytest.raises(MigrationError, match="badly named"):
        discover_migrations(tmp_path)


def test_migration_gaps_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "0001_one.sql", "")
    _write(tmp_path, "0003_three.sql", "")
    with pytest.raises(MigrationError, match="without gaps"):
        discover_migrations(tmp_path)


def test_old_backups_are_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbmod, "MAX_BACKUPS", 2)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for i in range(3):
        (backup_dir / f"ember-schema0001-2026010{i}T000000Z.db").write_bytes(b"")
    mig_dir = tmp_path / "migrations"
    _write(mig_dir, "0001_one.sql", "CREATE TABLE a (x INTEGER);")
    db_file = tmp_path / "ember.db"
    migrate(db_file, discover_migrations(mig_dir), backup_dir=backup_dir)
    _write(mig_dir, "0002_two.sql", "CREATE TABLE b (x INTEGER);")
    migrate(db_file, discover_migrations(mig_dir), backup_dir=backup_dir)
    backups = sorted(p.name for p in backup_dir.glob("ember-schema*.db"))
    assert len(backups) == 2
    assert backups[0] == "ember-schema0001-20260102T000000Z.db"  # oldest ones went first


def test_meta_and_events(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    database = Database(db_file)
    assert database.set_meta_if_missing("born_at", "2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    assert database.set_meta_if_missing("born_at", "2027-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    database.set_meta("last_started_at", "x")
    database.set_meta("last_started_at", "y")
    assert database.get_meta("last_started_at") == "y"
    assert database.get_meta("missing") is None

    database.add_event("info", "system", "hello", {"a": 1})
    database.add_event("debug", "system", "noise")
    database.add_event("error", "log", "boom")
    events = database.recent_events()
    assert [e["message"] for e in events] == ["boom", "hello"]  # newest first, debug hidden
    assert events[1]["details"] == {"a": 1}
    assert len(database.recent_events(min_level="debug")) == 3


def test_event_level_is_constrained(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    with pytest.raises(sqlite3.IntegrityError):
        Database(db_file).add_event("catastrophic", "x", "y")


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    database = Database(db_file)
    with pytest.raises(RuntimeError), database.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value, updated_at) VALUES ('k', 'v', 'now')")
        raise RuntimeError("abort")
    assert database.get_meta("k") is None


def test_utcnow_format() -> None:
    stamp = dbmod.utcnow()
    assert len(stamp) == 20 and stamp.endswith("Z") and stamp[10] == "T"


def test_prune_events_keeps_newest(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    database = Database(db_file)
    for i in range(12):
        database.add_event("info", "system", f"event {i}")
    assert database.prune_events(5) == 7
    assert [e["message"] for e in database.recent_events()] == [f"event {i}" for i in range(11, 6, -1)]
    assert database.prune_events(5) == 0
