"""Fixes from the first live day: removing message text, the PDF guard, effort, the owner's to-do count."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import prompts, tools
from app.agent.owner import REMOVED_TEXT, Owner
from app.agent.service import Agent
from app.config import Settings
from tests.test_agent import make_agent, plan, rows, text
from tests.test_agent import tools as tool_calls
from tests.test_owner_api import post

SECRET = "EmberTheHelper - miwkoh-9febso-vetHyz"


def owner(agent: Agent) -> Owner:
    return Owner(agent.db, agent.clock, agent.economy, agent.scope(), agent.settings.agent_name)


def test_the_owner_can_remove_the_text_of_their_own_message(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    who = owner(agent)
    sent = who.send_message({"text": f"Here is the login: {SECRET}"}, "MVP")
    message_id = sent.body["id"]
    removed = who.remove_message(message_id, "MVP")
    assert removed.status == 200
    row = rows(agent, "SELECT text, removed_at, removed_by FROM messages")[0]
    assert row["text"] == REMOVED_TEXT and row["removed_at"] and row["removed_by"] == "MVP"
    assert who.remove_message(message_id, "MVP").status == 409  # once
    assert who.remove_message(999, None).status == 404
    assert agent.dashboard()["inbox"][0]["removed"] is True


def test_the_database_allows_only_the_removal(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    who = owner(agent)
    message_id = who.send_message({"text": "hello"}, None).body["id"]
    for sql in (
        "UPDATE messages SET text = 'edited' WHERE id = ?",
        "UPDATE messages SET text = '[removed by the owner]' WHERE id = ?",  # without removed_at
        "UPDATE messages SET removed_at = 'x' WHERE id = ?",  # without blanking the text
    ):
        with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
            conn.execute(sql, (message_id,))
    who.remove_message(message_id, None)
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
        conn.execute("UPDATE messages SET removed_by = 'someone else' WHERE id = ?", (message_id,))
    with pytest.raises(sqlite3.IntegrityError), agent.db.transaction() as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))


def test_the_agents_messages_cant_be_removed(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir, [plan(steps=["tell"]), tool_calls(("message_owner", {"text": "Hi"})), text("Done."), text("Ok.")]
    )
    agent.run_cycle("owner")
    agent_message = rows(agent, "SELECT id FROM messages WHERE sender = 'agent'")[0]["id"]
    assert owner(agent).remove_message(agent_message, None).status == 409


def test_a_removed_password_is_gone_from_the_diagnostics(ingress_client: TestClient) -> None:
    assert post(ingress_client, "api/inbox", {"text": f"login {SECRET}"}).status_code == 201
    assert "miwkoh-9febso-vetHyz" in ingress_client.get("api/diagnostics").text
    message_id = ingress_client.get("api/dashboard").json()["inbox"][0]["id"]
    assert post(ingress_client, f"api/inbox/{message_id}/remove", {}).json() == {"id": message_id, "removed": True}
    report = ingress_client.get("api/diagnostics").text
    assert "miwkoh-9febso-vetHyz" not in report and REMOVED_TEXT in report
    assert post(ingress_client, f"api/inbox/{message_id}/remove", {}, headers={}).status_code == 403  # CSRF


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/report.pdf",
        "https://example.com/files/Report.PDF",
        "https://example.com/a/b.docx",
        "https://example.com/deck.pptx",
    ],
)
def test_documents_are_never_fetched(data_dir: Path, url: str) -> None:
    agent, _ = make_agent(data_dir, [])
    calls: list[str | None] = []
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=0,
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
        research=lambda question, u, cycle_id, site: calls.append(u) or tools.Outcome(True, "digest", "ok"),
    )
    ctx.state.seen_urls.add(url)
    with pytest.raises(tools.ToolError, match="PDFs can't be read"):
        tools.HANDLERS["research"](ctx, {"question": "q", "url": url})
    page = "https://example.com/pricing"
    ctx.state.seen_urls.add(page)
    tools.HANDLERS["research"](ctx, {"question": "q", "url": page})
    assert calls == [page]


def test_the_effort_option() -> None:
    assert "output_config" not in prompts.work_request(Settings(), "brief", [])
    medium = prompts.work_request(Settings(worker_effort="medium"), "brief", [])
    assert medium["output_config"] == {"effort": "medium"}
    reflect = prompts.reflect_request(Settings(worker_effort="low"), "brief", [], [])
    assert reflect["output_config"] == {"effort": "low"}  # the same for every step of a cycle
    haiku = Settings(
        worker_effort="medium",
        worker_model="claude-haiku-4-5-20251001",
        price_table=(
            *Settings().price_table,
            {
                "model": "claude-haiku-4-5-20251001",
                "input": 1,
                "output": 5,
                "cache_write_5m": 1.25,
                "cache_write_1h": 2,
                "cache_read": 0.1,
            },
        ),
    )
    assert "output_config" not in prompts.work_request(haiku, "brief", [])  # Haiku 4.5 refuses effort


def test_a_message_from_before_the_update_can_be_removed(tmp_path: Path) -> None:
    # The owner's case: a password sent under 0.3.x (schema 4) is removed after updating.
    from app.db import Database, discover_migrations, migrate

    db_file = tmp_path / "ember.db"
    migrate(db_file, [m for m in discover_migrations() if m.version <= 4], backup_dir=tmp_path / "backups")
    old = Database(db_file)
    with old.transaction() as conn:
        conn.execute(
            "INSERT INTO lives (id, mode, born_at, started_reason, state) VALUES (1, 'live', 'then', 'born', 'alive')"
        )
        conn.execute(
            "INSERT INTO messages (id, mode, session, life_id, created_at, sender, cycle_id, text)"
            f" VALUES (12, 'live', 0, 1, 'then', 'owner', NULL, 'login {SECRET}')"
        )
    old.close()
    assert migrate(db_file, backup_dir=tmp_path / "backups") == [5, 6, 7, 8, 9, 10, 11, 12]
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        conn.execute(
            "UPDATE messages SET text = ?, removed_at = 'now', removed_by = 'MVP' WHERE id = 12", (REMOVED_TEXT,)
        )
        assert conn.execute("SELECT text FROM messages WHERE id = 12").fetchone()[0] == REMOVED_TEXT
    upgraded.close()
