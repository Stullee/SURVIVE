"""Rules the database itself enforces, whatever the Python code does."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import Database, migrate
from tests.economy_helpers import make_economy, metered, owner, request

NOW = "2026-09-01T12:00:00Z"
DAY = "2026-09-01"


@pytest.fixture
def db(data_dir: Path) -> Database:
    database = Database(data_dir / "ember.db")
    migrate(database.path)
    return database


def insert(conn: sqlite3.Connection, **row: object) -> int:
    values = {"ts": NOW, "occurred_on": DAY, "created_by": "owner", "idempotency_key": None, **row}
    if values["created_by"] == "owner" and values["idempotency_key"] is None:
        values["idempotency_key"] = f"k{values}"
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return int(conn.execute(f"INSERT INTO ledger ({columns}) VALUES ({marks})", tuple(values.values())).lastrowid)


def test_ledger_is_append_only(db: Database) -> None:
    with db.connection() as conn:
        entry = insert(conn, type="owner_grant", amount_micros=5_000_000, idempotency_key="a")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE ledger SET amount_micros = 1 WHERE id = ?", (entry,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM ledger WHERE id = ?", (entry,))
        # REPLACE deletes the conflicting row first; with recursive triggers that fires the DELETE trigger.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO ledger (id, ts, occurred_on, type, amount_micros, created_by, idempotency_key)"
                " VALUES (?, ?, ?, 'owner_grant', 99000000, 'owner', 'b')",
                (entry, NOW, DAY),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO ledger (ts, occurred_on, type, amount_micros, created_by, idempotency_key)"
                " VALUES (?, ?, 'owner_grant', 99000000, 'owner', 'a')",
                (NOW, DAY),
            )
        assert [tuple(r) for r in conn.execute("SELECT amount_micros FROM ledger")] == [(5_000_000,)]


@pytest.mark.parametrize(
    "row",
    [
        {"type": "revenue", "amount_micros": 1, "created_by": "system"},  # revenue can't be self-reported
        {"type": "owner_grant", "amount_micros": 1, "created_by": "system"},
        {"type": "expense", "amount_micros": 1, "created_by": "system"},
        {"type": "revenue", "amount_micros": 0},
        {"type": "revenue", "amount_micros": -5},
        {"type": "adjustment", "amount_micros": 0},
        {"type": "api_cost", "amount_micros": 5, "created_by": "owner"},
        {"type": "api_cost_correction", "amount_micros": 5, "simulated": 1},
        {"type": "bonus", "amount_micros": 5},
        {"type": "owner_grant", "amount_micros": 5, "orig_currency": "EUR"},
        {"type": "owner_grant", "amount_micros": 5, "orig_currency": "GBP", "orig_amount": "1", "fx_rate": "1"},
        {"type": "owner_grant", "amount_micros": 5, "created_by": "owner", "idempotency_key": None},
    ],
)
def test_ledger_rejects_invalid_rows(db: Database, row: dict) -> None:
    with db.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        values = {"ts": NOW, "occurred_on": DAY, "created_by": "owner", "idempotency_key": "x", **row}
        columns = ", ".join(values)
        conn.execute(
            f"INSERT INTO ledger ({columns}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values())
        )


def test_corrections_must_fit_their_original(db: Database) -> None:
    with db.connection() as conn:
        grant = insert(conn, type="owner_grant", amount_micros=10_000_000, idempotency_key="g")
        revenue = insert(conn, type="revenue", amount_micros=3_000_000, idempotency_key="r")
        insert(conn, type="owner_grant", amount_micros=-4_000_000, corrects_id=grant, idempotency_key="c1")
        with pytest.raises(sqlite3.IntegrityError, match="exceed"):
            insert(conn, type="owner_grant", amount_micros=-7_000_000, corrects_id=grant, idempotency_key="c2")
        with pytest.raises(sqlite3.IntegrityError, match="same type"):
            insert(conn, type="revenue", amount_micros=-1, corrects_id=grant, idempotency_key="c3")
        with pytest.raises(sqlite3.IntegrityError):
            insert(conn, type="revenue", amount_micros=1, corrects_id=revenue, idempotency_key="c4")  # must be negative
        first = conn.execute("SELECT id FROM ledger WHERE idempotency_key = 'c1'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="same type"):
            insert(conn, type="owner_grant", amount_micros=-1, corrects_id=first, idempotency_key="c5")
        with pytest.raises(sqlite3.IntegrityError, match="same type"):
            insert(conn, type="owner_grant", amount_micros=-1, corrects_id=grant, simulated=1, idempotency_key="c6")
        insert(conn, type="owner_grant", amount_micros=-6_000_000, corrects_id=grant, idempotency_key="c7")


def test_api_cost_must_match_a_finalized_call(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    result = model.call(cycle, "plan", request())
    with economy.db.connection() as conn:
        call = conn.execute("SELECT * FROM llm_calls WHERE id = ?", (result.call_id,)).fetchone()
        # A second cost row for the same call, a different amount or mode, or a call that isn't finished: refused.
        for amount, simulated, call_id in (
            (call["cost_micros"], 1, result.call_id),
            (call["cost_micros"] + 1, 1, result.call_id),
            (call["cost_micros"], 0, result.call_id),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ledger (ts, occurred_on, type, amount_micros, simulated, llm_call_id, created_by)"
                    " VALUES (?, ?, 'api_cost', ?, ?, ?, 'system')",
                    (NOW, call["local_day"], amount, simulated, call_id),
                )


def test_finalized_calls_and_ended_lives_are_history(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    result = model.call(cycle, "plan", request())
    with economy.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="finalized"):
            conn.execute("UPDATE llm_calls SET cost_micros = 0 WHERE id = ?", (result.call_id,))
        with pytest.raises(sqlite3.IntegrityError, match="deleted"):
            conn.execute("DELETE FROM llm_calls")
        with pytest.raises(sqlite3.IntegrityError, match="history"):
            conn.execute("DELETE FROM life_transitions")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM lives")
        life = conn.execute("SELECT id FROM lives").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
            conn.execute("UPDATE lives SET born_at = '2020-01-01T00:00:00Z' WHERE id = ?", (life,))
        conn.execute("UPDATE lives SET state = 'dead', ended_at = ?, end_reason = 'test' WHERE id = ?", (NOW, life))
        with pytest.raises(sqlite3.IntegrityError, match="ended life"):
            conn.execute("UPDATE lives SET state = 'alive', ended_at = NULL, end_reason = NULL WHERE id = ?", (life,))


def test_only_one_cycle_runs_at_a_time(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    with economy.db.connection() as conn:
        life = conn.execute("SELECT id FROM lives").fetchone()[0]
        row = (life, "boot", NOW, "running", "t", 1, 0)
        sql = (
            "INSERT INTO cycles (life_id, boot_id, started_at, status, trigger, simulated, cap_micros)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        conn.execute(sql, row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, row)


def test_owner_entry_rows_record_who_and_what(data_dir: Path) -> None:
    economy = make_economy(data_dir)
    body = owner(economy, "grant", "10,5", currency="EUR", fx_rate="1.08", note="coffee money")
    entry = body["entry"]
    assert entry["amount_usd"] == 11.34
    assert (entry["orig_amount"], entry["orig_currency"], entry["fx_rate"]) == ("10.50", "EUR", "1.08")
