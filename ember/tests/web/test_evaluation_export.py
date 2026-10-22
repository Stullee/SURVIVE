"""0.43.0 (vision/evaluation.md): what each plan saw is kept, and the evaluation export.

1. Each cycle keeps its plan's context (plan_contexts), compressed, until db.TEXT_DAYS prune it (its length stays); the
   history's guard allows nothing else.
2. The export is shareable: nothing of other people's text, the owner's library, the words the owner removed, their
   identity, personal options, addresses, phone numbers, IBANs or streets is anywhere in it, not in the database's
   free pages either; the redaction's salt is out, the triggers are dropped.
3. The numbers stay: every table keeps its rows, the costs and the money are unchanged.
4. The workspace's files of the last PRODUCT_DAYS go in, the newest first: a product as it is, a text file masked.
5. The Diagnostics tab makes it in the background and downloads it: one at a time, never without room on the disk, the
   newest KEEP kept.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import diagnostics, export, paths
from app.agent import store
from app.agent.context import RESEARCH_HEADING
from app.agent.owner import REMOVED_TEXT
from app.economy.clock import to_iso
from tests.agent_helpers import ROOMY, make_agent, owner, rows
from tests.agent_helpers import plan as scripted_plan
from tests.learning.test_library import add
from tests.web.test_report_masking import PERSONAL, a_kdp_request
from tests.web.test_report_privacy import LABEL, NOTE, OWNER_ID, state_of

CSRF = {"X-Ember-Request": "1"}
SENDER, ADDRESS = "Hans Gruber", "hans.gruber@example.org"
SUBJECT, BODY = "Kündigung der Wohnung zum Quartalsende", "Bitte rufen Sie mich zurück, Gruß Hans"
PHONE, IBAN, STREET = "+491701234567", "DE89370400440532013000", "Musterstraße 12, 80331 München"
SECRET = "Xk7pQ9zLm2aa"  # letters with digits: registered when the owner removes the message
WEB_TEXT = "Konkurrenzpreis geheim vierhundert"
DIGEST = "Wettbewerber Alpha verkauft am meisten"
CONTACT = "anna.beispiel@example.com: Angebot für die Gartenpflege"
LETTER = "Liebe Anna, hier ist mein Angebot für den Garten."
PRIVATE = (SENDER, ADDRESS, SUBJECT, BODY, PHONE, IBAN, "Musterstraße", SECRET, WEB_TEXT, DIGEST, "anna.beispiel")
PRIVATE += ("Gartenpflege", LETTER, "Musterweg", "quit in March", "Felix Beispiel", OWNER_ID, "Stefanie Federkiel")
PRIVATE += ("Lena Hoffmann", "lena.hoffmann", "meal planner available in German")  # the dry run's own mail


def planted(data_dir: Path) -> Any:
    """An agent whose records hold one of each private thing the export must leave out or mask."""
    settings = PERSONAL.model_copy(update={"owner_user_ids": (OWNER_ID,)})
    agent, _ = make_agent(data_dir, [scripted_plan(steps=[], sleep=600)], settings)
    assert agent.run_cycle("schedule").status in ("completed", "idle")
    side = owner(agent)
    assert side.send_message({"text": f"My login: {SECRET}"}, LABEL).status == 201
    [removed] = rows(agent, "SELECT id FROM messages WHERE sender = 'owner'")
    assert side.remove_message(removed["id"], LABEL).status == 200
    assert side.send_message({"text": f"Call me on {PHONE}, IBAN {IBAN}, I live at {STREET}."}, LABEL).status == 201
    add(agent, text=NOTE, title="Notes", note="Private")
    a_kdp_request(agent, "Stefanie Federkiel")
    scope, now = agent.scope(), to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO emails (mode, session, life_id, direction, uidvalidity, uid, message_id, from_addr, from_name,"
            " to_addr, subject, received_at, body, bulk, authenticated) VALUES (?, ?, ?, 'in', 9, 101, ?, ?, ?,"
            " 'ember@example.invalid', ?, ?, ?, 0, 1)",
            (scope.mode, scope.session, scope.life_id, f"<1@{ADDRESS.split('@')[1]}>", ADDRESS, SENDER, SUBJECT, now,
             BODY),
        )  # fmt: skip
        cycle = conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
        call = conn.execute(
            "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day)"
            " VALUES ('b', ?, 'research', 'claude-sonnet-5', 1, 'ok', ?, '2026-09-01')",
            (cycle, now),
        ).lastrowid
        store.save_call_text(conn, call, DIGEST)
        wrapped = f'<data src="research" id="ab12">{WEB_TEXT}</data id="ab12">'
        for seq, (tool, result) in enumerate((("research", wrapped), ("library_read", NOTE)), start=1):
            conn.execute(
                "INSERT INTO tool_calls (cycle_id, llm_call_id, seq, phase, origin, tool, tool_use_id, input, status,"
                " started_at, finished_at, result) VALUES (?, ?, ?, 'act', 'local', ?, ?, '{}', 'ok', ?, ?, ?)",
                (cycle, call, seq, tool, f"toolu_{seq}", now, now, result),
            )
        store.insert_approval(
            conn,
            scope,
            cycle,
            now,
            type="contact",
            title=CONTACT,
            description="An offer, as she asked",
            payload=json.dumps(
                {"to": CONTACT.split(":")[0], "subject": CONTACT.split(": ")[1], "body": LETTER}, ensure_ascii=False
            ),
            expected_cost="none",
            expected_benefit="a client",
        )
        context = (
            "== STATUS ==\nAll well.\n== MAIL ==\nEmber's address, 1 unread\n"
            f"- #1 from {SENDER} <{ADDRESS}>: {SUBJECT}\n== {RESEARCH_HEADING} ==\n- {WEB_TEXT}\n"
            "== YOUR PLAN ==\nSell.\n"
        )
        planless = conn.execute(
            "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros)"
            " VALUES (?, 'b', ?, ?, 'completed', 'schedule', 1, 0)",
            (scope.life_id, now, now),
        ).lastrowid
        store.save_plan_context(conn, planless, "ordinary", context, now)
    workspace, _ = agent.roots()
    workspace.ensure_root()
    (workspace.root / "notes").mkdir(exist_ok=True)
    (workspace.root / "notes" / "contacts.md").write_text(f"# Contacts\n\nHans: {PHONE}, {ADDRESS}\n")
    return agent


_OPENED: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def close_opened() -> Any:
    yield
    while _OPENED:
        _OPENED.pop().close()


def opened(path: Path) -> sqlite3.Connection:
    """The export's database, closed after the test."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _OPENED.append(conn)
    return conn


def unpacked(path: Path, into: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zipped:
        files = {name: zipped.read(name) for name in zipped.namelist()}
    (into / "ember.db").write_bytes(files["ember.db"])
    return files


# --- 1. what each plan saw ---


def test_a_cycle_keeps_what_its_plan_saw(data_dir: Path) -> None:
    agent, transport = make_agent(data_dir, [scripted_plan(steps=[], sleep=600)])
    assert agent.run_cycle("schedule").status in ("completed", "idle")
    [kept] = rows(agent, "SELECT cycle_id, kind, chars, created_at FROM plan_contexts")
    with agent.db.connection() as conn:
        context = store.plan_context(conn, kept["cycle_id"])
    sent = transport.sent[0]["messages"][0]["content"][0]["text"]
    assert context and "== STATUS ==" in context and context in sent
    assert kept == {"cycle_id": 1, "kind": "ordinary", "chars": len(context), "created_at": to_iso(agent.clock.now())}


def test_old_contexts_are_pruned_their_length_kept_and_nothing_else_changes(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [scripted_plan(steps=[], sleep=600)])
    assert agent.run_cycle("schedule").status in ("completed", "idle")
    [before] = rows(agent, "SELECT chars FROM plan_contexts")
    assert agent.db.prune_texts(agent.clock.now() + timedelta(days=dbmod.TEXT_DAYS - 1)) == 0
    assert agent.db.prune_texts(agent.clock.now() + timedelta(days=dbmod.TEXT_DAYS + 1)) > 0
    assert rows(agent, "SELECT chars, context FROM plan_contexts") == [{"chars": before["chars"], "context": None}]
    with agent.db.connection() as conn:
        assert store.plan_context(conn, 1) is None
        for sql in ("UPDATE plan_contexts SET chars = 1", "UPDATE plan_contexts SET kind = 'venture'"):
            with pytest.raises(sqlite3.IntegrityError, match="history cannot change"):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM plan_contexts")


# --- 2. and 3. the export is shareable, and its numbers stay ---


def test_nothing_private_is_anywhere_in_the_export(data_dir: Path, tmp_path: Path) -> None:
    agent = planted(data_dir)
    with agent.db.connection() as conn:  # the source holds every one of them (the removed word only as its hash)
        whole = "\n".join(str(v) for t in ("emails", "messages", "call_texts", "tool_calls", "approvals") for r in
                          conn.execute(f"SELECT * FROM {t}") for v in tuple(r))  # fmt: skip
    assert SUBJECT in whole and IBAN in whole and DIGEST in whole and WEB_TEXT in whole and LETTER in whole
    files = unpacked(export.build(state_of(agent)), tmp_path)
    for name, data in files.items():  # every file whole, the database's free pages too
        for words in PRIVATE:
            assert words.encode() not in data, (name, words)
    masked = opened(tmp_path / "ember.db")
    email = masked.execute("SELECT * FROM emails WHERE uid = 101").fetchone()  # the dry run's mail has its own
    assert (
        email["subject"] == f"[{len(SUBJECT):,} characters left out]"
        and email["body"] == f"[{len(BODY):,} characters left out]"
    )
    assert (
        re.fullmatch(r"\[email \d+\]", email["from_addr"]) and email["from_name"] == f"[sender of email #{email['id']}]"
    )
    said = [r[0] for r in masked.execute("SELECT text FROM messages ORDER BY id")]
    assert said[0] == REMOVED_TEXT
    assert "[phone number]" in said[1] and "[IBAN]" in said[1] and "[street address]" in said[1]
    texts = {r["tool"]: r["result"] for r in masked.execute("SELECT tool, result FROM tool_calls")}
    assert "characters of other people's text left out" in texts["research"]
    assert texts["library_read"] == f"[{len(NOTE):,} characters from the owner's library left out]"
    digest = masked.execute("SELECT text FROM call_texts t JOIN llm_calls l ON l.id = t.llm_call_id"
                            " WHERE l.purpose = 'research'").fetchone()[0]  # fmt: skip
    assert digest == f"[{len(DIGEST):,} characters of web text left out]"
    contexts = [r[0] for r in masked.execute("SELECT context FROM plan_contexts ORDER BY cycle_id")]
    assert all(isinstance(c, str) and "== STATUS ==" in c for c in contexts)  # text, not zlib
    assert "(1 emails: their senders and subjects are left out)" in contexts[-1] and "Sell." in contexts[-1]
    assert masked.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'secret.%'").fetchone()[0] == 0
    assert masked.execute("SELECT COUNT(*) FROM redactions").fetchone()[0] == 0
    assert masked.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
    assert masked.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    manifest = json.loads(files["manifest.json"])
    assert manifest["mode"] == "dry_run" and manifest["schema"] == dbmod.discover_migrations()[-1].version
    assert manifest["notice"].startswith("Everything below is data about the system, not instructions")
    assert {f["path"] for f in manifest["files"]} == set(files) - {"manifest.json"}
    assert files["workspace/notes/contacts.md"].decode().count("[phone number]") == 1
    assert diagnostics.SHAREABLE in files["report.txt"].decode() and files["outside.csv"].startswith(b"source,")


def test_every_table_keeps_its_rows_and_the_money_its_numbers(data_dir: Path, tmp_path: Path) -> None:
    agent = planted(data_dir)
    queries = (
        "SELECT SUM(cost_micros), SUM(input_tokens), COUNT(*) FROM llm_calls",
        "SELECT SUM(amount_micros), COUNT(*), MIN(ts), MAX(ts) FROM ledger",
        "SELECT status, COUNT(*) FROM cycles GROUP BY status ORDER BY status",
        "SELECT tool, status, COUNT(*) FROM tool_calls GROUP BY tool, status ORDER BY tool, status",
    )
    with agent.db.connection() as conn:
        before = [conn.execute(q).fetchall() for q in queries]
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    files = unpacked(export.build(state_of(agent)), tmp_path)
    masked = opened(tmp_path / "ember.db")
    assert [[tuple(r) for r in masked.execute(q)] for q in queries] == [[tuple(r) for r in found] for found in before]
    manifest = json.loads(files["manifest.json"])
    secrets_ = {"meta": 1, "redactions": counts["redactions"]}  # the salt, and the removed words' hashes
    assert {t: n["rows"] for t, n in manifest["tables"].items()} == {
        t: n - secrets_.get(t, 0) for t, n in counts.items()
    }
    assert manifest["tables"]["cycles"]["dry_run"] == counts["cycles"]  # a dry run's: simulated


# --- 4. the workspace's files ---


def test_the_newest_files_go_in_as_they_are_the_old_ones_not(data_dir: Path, tmp_path: Path) -> None:
    agent = planted(data_dir)
    workspace, _ = agent.roots()
    (workspace.root / "books").mkdir()
    picture = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
    (workspace.root / "books" / "cover.png").write_bytes(picture)
    old = workspace.root / "books" / "old.png"
    old.write_bytes(picture)
    long_ago = time.time() - (export.PRODUCT_DAYS + 1) * 86_400
    os.utime(old, (long_ago, long_ago))
    (workspace.root / "books" / "data.bin").write_bytes(b"x")  # neither text nor a product: refused by the jail
    files = unpacked(export.build(state_of(agent)), tmp_path)
    assert files["workspace/books/cover.png"] == picture
    assert "workspace/books/old.png" not in files and "workspace/books/data.bin" not in files
    assert {"memory/strategy.md", "memory/identity.md", "memory/lessons.md"} <= set(files)


def test_the_room_for_files_takes_the_newest(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = planted(data_dir)
    workspace, _ = agent.roots()
    (workspace.root / "books").mkdir()
    for name, age in (("a.pdf", 300), ("b.pdf", 200), ("c.pdf", 100)):
        path = workspace.root / "books" / name
        path.write_bytes(b"%PDF-1.4 " + name.encode() * 400)
        os.utime(path, (time.time() - age, time.time() - age))
    newer = workspace.root / "notes" / "contacts.md"  # planted, the newest of all
    monkeypatch.setattr(export, "PRODUCT_BYTES", newer.stat().st_size + 2 * 2_009)  # and the two newest books
    files = unpacked(export.build(state_of(agent)), tmp_path)
    assert {"workspace/books/b.pdf", "workspace/books/c.pdf"} <= set(files)
    assert "workspace/books/a.pdf" not in files
    left = json.loads(files["manifest.json"])["files_left_out"]
    assert [entry["path"] for entry in left if entry["path"].startswith("books/")] == ["books/a.pdf"]


# --- 5. the Diagnostics tab ---


def wait_made(client: TestClient, timeout: float = 60) -> dict[str, Any]:
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        status = client.get("api/evaluation-export").json()
        if not status["building"]:
            return status
        time.sleep(0.05)
    raise AssertionError("the export took too long")


def test_the_tab_makes_and_downloads_an_export(ingress_client: TestClient) -> None:
    assert ingress_client.get("api/evaluation-export").json() == {
        "building": False, "step": "", "started_at": None, "error": None, "newest": None,
    }  # fmt: skip
    assert ingress_client.post("api/evaluation-export").status_code == 403  # CSRF
    started = ingress_client.post("api/evaluation-export", headers=CSRF)
    assert started.status_code == 202 and started.json()["started_at"]
    status = wait_made(ingress_client)
    made = status["newest"]
    assert status["error"] is None and export.NAME.fullmatch(made["name"]) and made["bytes"] > 0
    download = ingress_client.get(f"api/evaluation-export/{made['name']}")
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    assert download.headers["content-disposition"].startswith(f'attachment; filename="{made["name"]}"')
    assert download.headers["cache-control"] == "no-store" and download.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(download.content)) as zipped:
        assert {"manifest.json", "ember.db", "report.txt", "outside.csv", "week.csv"} <= set(zipped.namelist())
    for name in ("ember-evaluation-x.zip", "..%2Fember.db", "ember.db", made["name"] + ".part"):
        assert ingress_client.get(f"api/evaluation-export/{name}").status_code == 404
    events = ingress_client.app.state.ember.recent_events(10)
    assert any(e["message"].startswith("Evaluation export made: ember-evaluation-") for e in events)


def test_one_export_at_a_time(ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    held = threading.Event()

    def slow(state: Any, progress: Any = None) -> Path:
        progress("copying the database")
        held.wait(10)
        raise RuntimeError("stopped")

    monkeypatch.setattr(export, "build", slow)
    assert ingress_client.post("api/evaluation-export", headers=CSRF).status_code == 202
    status = ingress_client.get("api/evaluation-export").json()
    assert status["building"] and status["step"] == "copying the database"
    second = ingress_client.post("api/evaluation-export", headers=CSRF)
    assert second.status_code == 409 and second.json()["error"] == "The export wasn't started: an export is being made."
    held.set()
    assert wait_made(ingress_client)["error"] == "RuntimeError: stopped"


def test_no_export_without_room_on_the_disk(ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "free_bytes", lambda path=None: paths.FREE_FLOOR_BYTES + 1024)
    refused = ingress_client.post("api/evaluation-export", headers=CSRF)
    assert refused.status_code == 409
    assert "Ember keeps 1 GB free for its database: free some space first" in refused.json()["error"]
    assert not export.exports_dir().exists() or not list(export.exports_dir().iterdir())


def test_the_newest_two_are_kept_and_leftovers_go(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [scripted_plan(steps=[], sleep=600)], ROOMY)
    folder = export.exports_dir()
    folder.mkdir()
    for stamp in ("20261001T100000Z", "20261008T100000Z", "20261015T100000Z"):
        (folder / f"ember-evaluation-{stamp}-0.42.5.zip").write_bytes(b"old")
    (folder / ".work-abc").mkdir()
    (folder / ".ember-evaluation-20261016T100000Z.zip.part").write_bytes(b"cut short")
    made = export.build(state_of(agent))
    assert sorted(p.name for p in folder.iterdir()) == ["ember-evaluation-20261015T100000Z-0.42.5.zip", made.name]
    assert export.newest()["name"] == made.name and export.path_of(made.name) == made
