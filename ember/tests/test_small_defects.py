"""0.12.0: six small defects the analysis found, each with its fix."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import store
from app.agent.news import News
from app.agent.service import Agent
from app.db import MigrationError, discover_migrations, migrate
from app.economy.clock import from_iso, to_iso
from app.integrations import etsy_publisher
from tests.test_agent import make_agent, plan, rows
from tests.test_owner_api import grant, post


def decided(executor: str | None) -> dict[str, Any]:
    return {
        "id": 7,
        "type": "sell",
        "title": "A listing",
        "status": "approved",
        "final_payload": None,
        "decision_comment": None,
        "executor": executor,
        "result_note": None,
        "result_link": None,
    }


def test_the_agent_hears_who_carries_out_an_approved_request() -> None:
    lines = News(decided=[decided(e) for e in ("etsy_listing", "etsy_edit", "reddit_link", None)]).approval_lines()
    assert lines[0].endswith(": approved. Ember's code carries it out in the Etsy shop and you'll hear the result.")
    assert lines[1].endswith("carries it out in the Etsy shop and you'll hear the result.")
    assert lines[2].endswith(": approved. Your owner posts it and reports back.")
    assert lines[3].endswith(": approved. Your owner will carry it out and report back.")


def test_a_failure_while_scheduling_never_leaves_the_old_wake_time(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600), plan(steps=[], sleep=600)])
    agent.run_cycle("schedule")
    before = agent.db.get_meta("agent.dry_run.next_wake_at") or agent.db.get_meta("agent.live.next_wake_at")
    agent.clock.advance(days=1)  # the scheduled time is long past

    def broken(self: Agent, trigger: str, end: Any) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(Agent, "_after", broken)
    agent.run_cycle("schedule")
    key = "agent.dry_run.next_wake_at" if agent.mode == "dry_run" else "agent.live.next_wake_at"
    after = agent.db.get_meta(key)
    assert after is not None and after != before
    assert from_iso(after) > agent.clock.now()  # never a time already past, which woke the agent again at once
    reason = agent.db.get_meta(key.replace("next_wake_at", "next_wake_reason")) or ""
    assert reason.startswith("working out the next wake failed")
    assert any("Working out the next wake failed" in e["message"] for e in agent.db.recent_events(limit=10))


def test_migrations_are_matched_by_name_not_only_by_number(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    for folder, name in ((ours, "0001_first.sql"), (theirs, "0001_other.sql")):
        folder.mkdir()
        (folder / name).write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    db_file = tmp_path / "ember.db"
    assert migrate(db_file, discover_migrations(theirs), backup_dir=tmp_path / "b") == [1]
    with pytest.raises(
        MigrationError, match="migration 0001 in the database is 'other', but this version's is 'first'"
    ):
        migrate(db_file, discover_migrations(ours), backup_dir=tmp_path / "b")
    (theirs / "0001_other.sql").write_text("CREATE TABLE a (x INTEGER); -- edited", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert migrate(db_file, discover_migrations(theirs), backup_dir=tmp_path / "b") == []
    assert "Migration 0001_other changed since it was applied" in caplog.text


def test_the_ledger_goes_further_back_than_its_newest_twenty(ingress_client: TestClient) -> None:
    for _ in range(25):
        assert post(ingress_client, "api/ledger/grant", grant("1")).status_code == 201
    newest = ingress_client.get("api/dashboard").json()["ledger"]["entries"]
    assert len(newest) == 20
    older = ingress_client.get(f"api/ledger?limit=100&before={newest[-1]['id']}").json()["entries"]
    assert older and all(e["id"] < newest[-1]["id"] for e in older)
    assert {e["id"] for e in newest} | {e["id"] for e in older} >= set(range(newest[-1]["id"] - len(older), 1))
    script = (Path(__file__).parents[1] / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert '"api/ledger?limit=" + LEDGER_PAGE + "&before="' in script and "Show older entries" in script


def test_every_open_request_is_listed_for_the_owner(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)])
    agent.run_cycle("schedule")
    now = to_iso(agent.clock.now())
    fields = {"type": "other", "description": "d", "expected_cost": "none", "expected_benefit": "b"}
    with agent.db.transaction() as conn:
        for n in range(35):
            store.insert_approval(conn, agent.scope(), 1, now, title=f"#{n}", payload=f"p{n}", **fields)
        conn.execute("UPDATE approvals SET status = 'rejected', decided_at = ? WHERE id > 3", (now,))
    listed = [a["id"] for a in agent.dashboard()["approvals"]]
    assert listed[:30] == list(range(35, 5, -1)) and listed[30:] == [3, 2, 1]  # the three oldest still wait


def test_older_orders_still_to_record_keep_their_button(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [plan(steps=[], sleep=600)])
    scope = agent.scope()
    with agent.db.transaction() as conn:
        for n in range(25):
            day = f"2026-09-{n + 1:02d}T10:00:00Z"
            status = "paid" if n < 2 else "fully refunded"
            conn.execute(
                "INSERT INTO etsy_orders (mode, session, receipt_id, ordered_at, total, total_cents, currency, items,"
                " synced_at, status) VALUES (?, ?, ?, ?, '4.90 EUR', 490, 'EUR', ?, ?, ?)",
                (scope.mode, scope.session, 100 + n, day, json.dumps([]), day, status),
            )
        orders = etsy_publisher.orders_json(conn, scope)
    receipts = [o["receipt_id"] for o in orders]
    assert receipts[:20] == list(range(124, 104, -1)) and receipts[20:] == [101, 100]  # older, but not recorded yet
    assert all(o["recordable"] for o in orders[20:])
    assert rows(agent, "SELECT COUNT(*) AS n FROM etsy_orders")[0]["n"] == 25
