from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import db as dbmod
from app.db import Database, DatabaseTooNewError, MigrationError, discover_migrations, migrate, split_statements


def _write(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


def test_fresh_database_gets_all_migrations(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    applied = migrate(db_file, backup_dir=tmp_path / "backups")
    assert applied == [m.version for m in discover_migrations()]
    database = Database(db_file)
    assert database.schema_version() == discover_migrations()[-1].version
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


def test_a_number_another_branch_holds_may_be_skipped_until_it_comes(tmp_path: Path) -> None:
    """The 0.16.1 analysis's fixes took 0071 while 0070 was Google Search Console's, on a branch not merged yet: the
    numbers may skip a reserved one while its file isn't here, and it applies after the later ones once it comes."""
    mig_dir = tmp_path / "migrations"
    _write(mig_dir, "0001_one.sql", "CREATE TABLE one (x INTEGER);")
    _write(mig_dir, "0003_three.sql", "CREATE TABLE three (x INTEGER);")
    with pytest.raises(MigrationError, match="without gaps"):
        discover_migrations(mig_dir, reserved=frozenset({4}))  # only the number reserved
    db_file = tmp_path / "ember.db"
    assert migrate(db_file, discover_migrations(mig_dir, reserved=frozenset({2}))) == [1, 3]
    _write(mig_dir, "0002_two.sql", "CREATE TABLE two (x INTEGER);")
    assert migrate(db_file, discover_migrations(mig_dir, reserved=frozenset({2})), tmp_path / "backups") == [2]
    _write(mig_dir, "0002_twice.sql", "")
    with pytest.raises(MigrationError, match="without gaps"):  # two of a number are refused as before
        discover_migrations(mig_dir, reserved=frozenset({2}))


def test_a_reserved_number_goes_once_its_migration_is_merged() -> None:
    """A number stays reserved only while its branch isn't merged: then the numbers have no gap again."""
    here = {m.version for m in discover_migrations()}
    assert not dbmod.RESERVED & here, "remove the merged migration's number from app/db.py RESERVED"
    assert all(0 < number < max(here) for number in dbmod.RESERVED)  # a gap below the newest one


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


def test_table_rebuild_migration_keeps_child_rows(tmp_path: Path) -> None:
    """SQLite's documented rebuild procedure must not cascade-delete rows (e.g. ledger history)."""
    mig_dir = tmp_path / "migrations"
    _write(
        mig_dir,
        "0001_first.sql",
        "CREATE TABLE cycles (id INTEGER PRIMARY KEY, name TEXT);\n"
        "CREATE TABLE ledger (id INTEGER PRIMARY KEY, cycle_id INTEGER REFERENCES cycles(id) ON DELETE CASCADE);\n",
    )
    db_file = tmp_path / "ember.db"
    migrate(db_file, discover_migrations(mig_dir))
    database = Database(db_file)
    with database.transaction() as conn:
        conn.execute("INSERT INTO cycles (id, name) VALUES (1, 'a'), (2, 'b')")
        conn.execute("INSERT INTO ledger (cycle_id) VALUES (1), (1), (2)")
    database.close()

    _write(
        mig_dir,
        "0002_rebuild.sql",
        "CREATE TABLE cycles_new (id INTEGER PRIMARY KEY, name TEXT, note TEXT NOT NULL DEFAULT '');\n"
        "INSERT INTO cycles_new (id, name) SELECT id, name FROM cycles;\n"
        "DROP TABLE cycles;\n"
        "ALTER TABLE cycles_new RENAME TO cycles;\n",
    )
    migrate(db_file, discover_migrations(mig_dir), backup_dir=tmp_path / "backups")
    database = Database(db_file)
    with database.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 3
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # enforcement is back on
    database.close()


def test_migration_leaving_broken_foreign_keys_is_rolled_back(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    _write(
        mig_dir,
        "0001_first.sql",
        "CREATE TABLE cycles (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE ledger (id INTEGER PRIMARY KEY, cycle_id INTEGER REFERENCES cycles(id));\n",
    )
    db_file = tmp_path / "ember.db"
    migrate(db_file, discover_migrations(mig_dir))
    _write(mig_dir, "0002_orphan.sql", "INSERT INTO ledger (cycle_id) VALUES (99);\n")
    with pytest.raises(MigrationError, match="foreign key"):
        migrate(db_file, discover_migrations(mig_dir), backup_dir=tmp_path / "backups")
    database = Database(db_file)
    assert database.schema_version() == 1
    with database.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
    database.close()


@pytest.mark.parametrize("statement", ["COMMIT;", "BEGIN;", "  -- note\nEND TRANSACTION;", "savepoint x;", "ROLLBACK;"])
def test_transaction_statements_in_migrations_are_refused(tmp_path: Path, statement: str) -> None:
    mig_dir = tmp_path / "migrations"
    _write(mig_dir, "0001_first.sql", f"CREATE TABLE a (x INTEGER);\n{statement}\nCREATE TABLE b (x INTEGER);\n")
    db_file = tmp_path / "ember.db"
    with pytest.raises(MigrationError, match="transaction statement"):
        migrate(db_file, discover_migrations(mig_dir))
    with Database(db_file).connection() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "a" not in tables and "b" not in tables


def test_bad_later_migration_prevents_earlier_pending_ones(tmp_path: Path) -> None:
    """All pending files are checked before any is applied."""
    mig_dir = tmp_path / "migrations"
    _write(mig_dir, "0001_first.sql", "CREATE TABLE a (x INTEGER);\n")
    _write(mig_dir, "0002_second.sql", "CREATE TABLE b (x INTEGER);\nCOMMIT;\n")
    db_file = tmp_path / "ember.db"
    with pytest.raises(MigrationError):
        migrate(db_file, discover_migrations(mig_dir))
    with Database(db_file).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


def test_split_statements() -> None:
    sql = (
        "-- header\nCREATE TABLE a (x TEXT); INSERT INTO a VALUES ('semi;colon');\n"
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1; SELECT 2; END;\n-- trailing comment\n"
    )
    statements = split_statements(sql)
    assert len(statements) == 3
    assert statements[1] == "INSERT INTO a VALUES ('semi;colon');"
    assert statements[2].startswith("CREATE TRIGGER") and statements[2].endswith("END;")
    with pytest.raises(MigrationError, match="incomplete"):
        split_statements("CREATE TABLE a (x INTEGER)")


def test_shipped_migrations_are_valid() -> None:
    for migration in discover_migrations():
        for statement in split_statements(migration.path.read_text(encoding="utf-8")):
            assert not statement.upper().lstrip().startswith(("BEGIN", "COMMIT", "END", "ROLLBACK"))


def test_nested_transaction_joins_the_outer_one(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    database = Database(db_file)
    with database.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value, updated_at) VALUES ('outer', '1', 'now')")
        with database.transaction() as inner:
            inner.execute("INSERT INTO meta (key, value, updated_at) VALUES ('inner', '1', 'now')")
        database.set_meta("helper", "1")  # a helper call inside the transaction doesn't deadlock
    assert {database.get_meta(k) for k in ("outer", "inner", "helper")} == {"1"}

    with pytest.raises(RuntimeError), database.transaction():
        database.set_meta("rolled_back", "1")
        raise RuntimeError("abort")
    assert database.get_meta("rolled_back") is None
    database.close()


def test_rollback_does_not_mask_the_real_error(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    database = Database(db_file)
    database.set_meta("k", "v")
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        # INSERT OR ROLLBACK ends the transaction itself before the error reaches us.
        conn.execute("INSERT OR ROLLBACK INTO meta (key, value, updated_at) VALUES ('k', 'v', 'now')")
    database.close()


def test_one_connection_for_the_process(tmp_path: Path) -> None:
    db_file = tmp_path / "ember.db"
    migrate(db_file)
    database = Database(db_file)
    with database.connection() as first, database.connection() as second:
        assert first is second
    database.close()
    assert database.get_meta("anything") is None  # reopens after close
    database.close()
