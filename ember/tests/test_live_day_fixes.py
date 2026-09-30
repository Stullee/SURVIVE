"""Fixes from the first live day: removing message text, the PDF guard, effort, the owner's to-do count."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import privacy
from app.agent import prompts, tools
from app.agent.owner import REMOVED_TEXT, Owner
from app.agent.service import Agent
from app.config import Settings
from tests.economy_helpers import ScriptedTransport
from tests.test_agent import make_agent, plan, rows, text
from tests.test_agent import tools as tool_calls
from tests.test_owner_api import post

# An obviously fake login with the shape of a generated password (three groups of six, one digit, one capital),
# which the dashboard's login warning looks for. Never put a real one here: this repository is public.
PASSWORD = "aaaaaa-0bbbbb-ccccCc"
SECRET = f"example-user - {PASSWORD}"


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
    assert PASSWORD in ingress_client.get("api/diagnostics").text
    message_id = ingress_client.get("api/dashboard").json()["inbox"][0]["id"]
    assert post(ingress_client, f"api/inbox/{message_id}/remove", {}).json() == {"id": message_id, "removed": True}
    report = ingress_client.get("api/diagnostics").text
    assert PASSWORD not in report and REMOVED_TEXT in report
    assert post(ingress_client, f"api/inbox/{message_id}/remove", {}, headers={}).status_code == 403  # CSRF


def test_a_removed_password_is_scrubbed_wherever_the_agent_copied_it(ingress_client: TestClient) -> None:
    """0.11.2: the live report still showed a removed password in 10 places (the model's replies, the journal, memory,
    the planner preview). Its words are registered as salted hashes: the report redacts every copy, and what the agent
    keeps and reads again (memory, open projects, workspace files) is scrubbed. The history itself can't change."""
    agent = ingress_client.app.state.ember.agent
    assert post(ingress_client, "api/inbox", {"text": f"Here is the login: {SECRET}"}).status_code == 201
    copy = f"The owner's login is example-user / {PASSWORD}"
    project = {"title": "Etsy shop", "hypothesis": "Printables sell", "next_step": "log in", "status": "active"}
    agent.transport = ScriptedTransport(
        simulated=True,
        outcomes=[
            plan(steps=[f"log in with {PASSWORD}"]),
            tool_calls(
                ("memory_update", {"file": "lessons", "mode": "append", "content": copy}),
                ("project_create", project),
                ("workspace_write", {"path": "notes/login.md", "mode": "create", "content": f"# Login\n\n{copy}\n"}),
                ("message_owner", {"text": f"I can't log in anywhere, so I won't use {PASSWORD}."}),
            ),
            tool_calls(("project_update", {"project_id": 1, "note": copy})),
            text(f"Noted {PASSWORD}."),
            tool_calls(("write_journal", {"summary": "Got a login", "entry": copy})),
        ],
    )
    agent.meter = agent.economy.metered(agent.transport)
    assert agent.run_cycle("owner").status == "completed"
    before = ingress_client.get("api/diagnostics?full=1").text
    assert before.count(PASSWORD) >= 8  # the message, the plan, the tool inputs, a reply, memory, journal, file ...

    message_id = rows(agent, "SELECT id FROM messages WHERE sender = 'owner'")[0]["id"]
    assert post(ingress_client, f"api/inbox/{message_id}/remove", {}).status_code == 200
    for query in ("", "?full=1"):
        report = ingress_client.get(f"api/diagnostics{query}").text
        assert PASSWORD not in report and REMOVED_TEXT in report and privacy.REMOVED in report
        assert "aaaaaa" not in report  # no part of it either
        meta = diagnostics_section(report, "META")
        assert "secret.redaction_salt | (set) |" in meta
    salt = rows(agent, f"SELECT value FROM meta WHERE key = '{privacy.SALT_KEY}'")[0]["value"]
    assert len(salt) == 32 and salt not in report
    registered = rows(agent, "SELECT message_id, digest FROM redactions")
    assert registered and all(r["message_id"] == message_id and len(r["digest"]) == 64 for r in registered)

    # Scrubbed where the agent keeps and reads it again ...
    assert PASSWORD not in agent.memory().read("lessons") and privacy.REMOVED in agent.memory().read("lessons")
    assert PASSWORD not in agent.roots()[0].read("notes/login.md")
    notes = rows(agent, "SELECT next_step, notes FROM projects")[0]
    assert PASSWORD not in notes["notes"] and privacy.REMOVED in notes["notes"]
    versions = rows(agent, "SELECT source, content FROM memory_versions WHERE file = 'lessons' ORDER BY id")
    assert versions[-1]["source"] == "external" and PASSWORD in versions[-2]["content"]  # a new version; history stays
    # ... and the history can't change, so it is only redacted where it is shown.
    assert PASSWORD in rows(agent, "SELECT entry FROM journal")[0]["entry"]
    assert agent.scrub_removed() is True and rows(agent, "SELECT COUNT(*) AS n FROM redactions")[0]["n"] == len(
        registered
    )


def diagnostics_section(text: str, title: str) -> str:
    return text.split(f"\n## {title}", 1)[1].split("\n## ", 1)[0]


def test_a_removed_message_is_scrubbed_when_the_running_cycle_ends(data_dir: Path) -> None:
    """A cycle that saw the message before it was removed may still copy it: the scrub runs again when it ends."""
    agent, _ = make_agent(data_dir, [])
    who = owner(agent)
    message_id = who.send_message({"text": f"login {SECRET}"}, None).body["id"]
    memory = agent.memory()
    memory.jail.write("lessons.md", f"# Lessons\n\n- The login is {PASSWORD}\n")  # what the running cycle wrote
    assert agent._lock.acquire(blocking=False)  # a cycle is running
    try:
        assert who.remove_message(message_id, None).status == 200
        assert agent.scrub_removed() is False and PASSWORD in memory.read("lessons")  # not while it runs ...
    finally:
        agent._lock.release()
    agent.transport.outcomes.extend([plan(steps=[]), text("Done."), tool_calls(("write_journal", {"summary": "s"}))])
    agent.run_cycle("owner")
    assert PASSWORD not in memory.read("lessons")  # ... but when it has ended


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
        research=lambda question, u, cycle_id, site, venture_id: calls.append(u) or tools.Outcome(True, "digest", "ok"),
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
    assert migrate(db_file, backup_dir=tmp_path / "backups") == [
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        51,
        52,
        53,
        54,
        55,
        56,
    ]
    upgraded = Database(db_file)
    with upgraded.transaction() as conn:
        conn.execute(
            "UPDATE messages SET text = ?, removed_at = 'now', removed_by = 'MVP' WHERE id = 12", (REMOVED_TEXT,)
        )
        assert conn.execute("SELECT text FROM messages WHERE id = 12").fetchone()[0] == REMOVED_TEXT
    upgraded.close()
