"""Rules the database itself enforces, whatever the Python code does."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db import Database, migrate
from app.economy.metering import CallRefused, Completed
from tests.economy_helpers import make_economy, message, metered, owner, request

NOW = "2026-09-01T12:00:00Z"
DAY = "2026-09-01"


@pytest.fixture
def db(data_dir: Path) -> Iterator[Database]:
    database = Database(data_dir / "ember.db")
    migrate(database.path)
    yield database
    database.close()


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


def test_an_api_cost_is_only_what_a_finished_call_cost_on_its_day(data_dir: Path) -> None:
    # 0.42.3 (analysis-0.40.1 audit): a call still in flight or refused has no cost to book, and a cost is booked on
    # the day of its call (the case above names no reason, so each is checked by the trigger's own)
    economy = make_economy(data_dir)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    held = model.reserve(cycle, "work", request())
    with pytest.raises(CallRefused):
        model.call(cycle, "work", request(model="claude-opus-9"))
    cost = "INSERT INTO ledger (ts, occurred_on, type, amount_micros, simulated, llm_call_id, created_by) VALUES (?, ?,"
    cost += " 'api_cost', ?, 1, ?, 'system')"
    unmatched = pytest.raises(sqlite3.IntegrityError, match="ledger: api_cost does not match its llm_calls row")
    with economy.db.connection() as conn:
        for status in ("pending", "refused"):
            call = conn.execute("SELECT * FROM llm_calls WHERE status = ?", (status,)).fetchone()
            with unmatched:
                conn.execute(cost, (NOW, call["local_day"], call["cost_micros"], call["id"]))
    model.finalize(held, Completed(message()))
    with economy.db.connection() as conn:
        call = conn.execute("SELECT * FROM llm_calls WHERE id = ?", (held.call_id,)).fetchone()
        with unmatched:
            conn.execute(cost, (NOW, "2026-09-02", call["cost_micros"], call["id"]))  # its day is 2026-09-01
        assert [r[0] for r in conn.execute("SELECT llm_call_id FROM ledger WHERE type = 'api_cost'")] == [call["id"]]


def test_a_refused_call_is_history_too(data_dir: Path) -> None:
    # 0.42.3 (analysis-0.40.1 audit): the guard records a refusal; it can't be turned into a reservation afterwards
    economy = make_economy(data_dir)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    with pytest.raises(CallRefused):
        model.call(cycle, "work", request(model="claude-opus-9"))
    with economy.db.connection() as conn, pytest.raises(sqlite3.IntegrityError, match="a finalized call cannot change"):
        conn.execute("UPDATE llm_calls SET status = 'pending', estimate_micros = 1 WHERE status = 'refused'")


def test_a_cycles_cap_and_identity_are_fixed_and_a_finished_cycle_is_final(data_dir: Path) -> None:
    # 0.42.3 (analysis-0.40.1 audit): the cap a cycle opened with is what the guard holds its calls to
    economy = make_economy(data_dir)
    model, _ = metered(economy)
    cycle = model.open_cycle("test")
    fixed = "cycles: identity and cap are fixed; a finished cycle is final"
    changes = (
        "cap_micros = cap_micros + 1",
        "life_id = life_id + 1",
        "boot_id = 'another'",
        "started_at = '2026-08-01T00:00:00Z'",
        "trigger = 'owner'",
        "simulated = 0",
        "session = session + 1",
    )
    with economy.db.connection() as conn:
        for change in changes:
            with pytest.raises(sqlite3.IntegrityError, match=fixed):
                conn.execute(f"UPDATE cycles SET {change} WHERE id = ?", (cycle,))
        with pytest.raises(sqlite3.IntegrityError, match="cycles: the version that ran a cycle is fixed"):
            conn.execute("UPDATE cycles SET app_version = 'another' WHERE id = ?", (cycle,))
        conn.execute("UPDATE cycles SET step = 2 WHERE id = ?", (cycle,))  # its progress is written as it runs
    model.close_cycle(cycle)
    with economy.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match=fixed):
            conn.execute("UPDATE cycles SET status = 'running', ended_at = NULL WHERE id = ?", (cycle,))
        with pytest.raises(sqlite3.IntegrityError, match="cycles: rows cannot be deleted"):
            conn.execute("DELETE FROM cycles")
        assert conn.execute("SELECT status FROM cycles").fetchone()[0] == "completed"


def test_a_lifes_history_is_never_rewritten(data_dir: Path) -> None:
    # 0.42.3 (analysis-0.40.1 audit): the runway measures active time from the transitions, and revival by its grant
    economy = make_economy(data_dir)
    with economy.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="life_transitions: history cannot change"):
            conn.execute("UPDATE life_transitions SET ts = '2026-08-01T00:00:00Z'")
        with pytest.raises(sqlite3.IntegrityError, match="lives: rows cannot be deleted"):
            conn.execute("DELETE FROM lives")
        for change in ("mode = 'live'", "born_mark = 99", "revived_by_ledger_id = 1"):
            with pytest.raises(sqlite3.IntegrityError, match="lives: mode, birth and revival cannot change"):
                conn.execute(f"UPDATE lives SET {change}")


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
