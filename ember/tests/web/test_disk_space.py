"""0.42.0: disk space (analysis-0.40.1 codebase, 6.2 "Disk space").

Ember runs on small devices and keeps everything in /data, but its quotas were absolute and blind to the device: 2 GB
of products and 50 MB of text on a Raspberry Pi's card, while the database grows with every cycle.

1. A free-space floor (paths.FREE_FLOOR_BYTES, 1 GB): a workspace write (text or product) and a workshop file that
   would leave less free are refused, and the agent is told what to do. The dashboard's System part and the
   diagnostics report show the free space and warn under the floor.
2. The kept texts of deleted files (workspace_versions) were kept for good, and versioned names (-v3 ... -v22)
   multiplied them: they are pruned VERSIONS_GONE_DAYS after their newest, all of them together take at most
   VERSIONS_MAX_BYTES, and restore still brings back a file deleted lately.
3. Home Assistant's backups leave out the database's copies from before its upgrades (backup_exclude), and nothing
   else: never the database, the options, the tokens or the workspace.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("httpx2")

from app import db as dbmod  # noqa: E402
from app import paths  # noqa: E402
from app.agent import scheduler, tools  # noqa: E402
from app.agent.fake_llm import FakeTransport  # noqa: E402
from app.agent.sandbox import Jail, QuotaError  # noqa: E402
from app.agent.service import Decision  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from tests.channels.test_etsy import call  # noqa: E402
from tests.cycle.test_agent import rows  # noqa: E402
from tests.tools.test_calls_in_texts import quiet  # noqa: E402
from tests.tools.test_workshop import result, workshop_cycle  # noqa: E402
from tests.web.test_diagnostics import report, section  # noqa: E402
from tests.web.test_manifest import MANIFEST  # noqa: E402

MB = 1024 * 1024
FLOOR = paths.FREE_FLOOR_BYTES
FULL = (
    "the device has 900 MB free; Ember keeps 1 GB free for its database: delete files you no longer need"
    " (workspace_write delete) or ask your owner"
)


# --- 1. the free-space floor -----------------------------------------------------------------------------------------


def test_a_write_that_would_leave_less_than_the_floor_is_refused(tmp_path: Path) -> None:
    asked: list[Path] = []

    def free(root: Path) -> int:
        asked.append(root)
        return 900 * MB

    jail = Jail(tmp_path / "workspace", free=free)
    with pytest.raises(QuotaError) as refused:
        jail.write("notes/idea.md", "A note.")
    assert str(refused.value) == FULL
    with pytest.raises(QuotaError, match="the device has 900 MB free"):
        jail.write_bytes("shop/cover.png", b"\x89PNG")
    assert jail.walk(100).files == [] and asked == [jail.root, jail.root]  # one look at the disk per write
    memory = Jail(tmp_path / "memory", floor=0, free=free)  # the memory's files: written whatever is free
    assert memory.write("lessons.md", "- a lesson\n") == 11


def test_the_whole_new_file_must_fit_above_the_floor(tmp_path: Path) -> None:
    jail = Jail(tmp_path / "workspace", free=lambda root: FLOOR + 1_000)
    assert jail.write("a.md", "x" * 1_000) == 1_000  # leaves the floor exactly
    with pytest.raises(QuotaError):
        jail.write("a.md", "x" * 1_001)  # an overwrite too: the new file is written before the old one goes
    assert jail.write_bytes("b.png", b"x" * 1_000) == 1_000
    assert jail.read("a.md") == "x" * 1_000


def test_the_jail_reads_the_disk_at_each_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jail = Jail(tmp_path / "workspace")
    assert jail.write("a.md", "fits") == 4  # the real disk: this machine has room
    monkeypatch.setattr(paths, "free_bytes", lambda path=None: 900 * MB)
    with pytest.raises(QuotaError, match="the device has 900 MB free"):
        jail.write("b.md", "doesn't")


def test_the_agent_is_told_what_to_do_and_it_is_no_strike(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, ctx = quiet(data_dir)
    assert call(ctx, "workspace_write", {"path": "books/old.md", "mode": "create", "content": "Old."}).ok
    monkeypatch.setattr(paths, "free_bytes", lambda path=None: 900 * MB)
    refused = call(ctx, "workspace_write", {"path": "books/new.md", "mode": "create", "content": "New."})
    assert not refused.ok and refused.text == f"Error: {FULL}."
    assert ctx.state.strikes == 0
    gone = call(ctx, "workspace_write", {"path": "books/old.md", "mode": "delete"})  # deleting makes room
    assert gone.ok and "kept for 14 days: restore brings it back" in gone.text


def test_a_workshop_file_without_room_is_not_downloaded(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    downloads: list[str] = []
    download = FakeTransport.download_file

    def counted(self: FakeTransport, file_id: str, limit: int) -> bytes:
        downloads.append(file_id)
        return download(self, file_id, limit)

    monkeypatch.setattr(FakeTransport, "download_file", counted)
    agent, _ = workshop_cycle(
        data_dir,
        {"prices.csv": b"shop,price\n" + b"A,4.5\n" * 2_000},  # 12 KB
        {"task": "Make the price table: prices.csv."},
        before=lambda agent: monkeypatch.setattr(paths, "free_bytes", lambda path=None: FLOOR + 4_096),
    )
    answer = result(agent)["result"]
    assert "Not kept: prices.csv: the device has 1 GB free; Ember keeps 1 GB free for its database" in answer
    assert downloads == [] and agent.roots()[0].size_of("workshop/out/prices.csv") is None


def test_the_dashboard_and_the_report_show_the_free_space(
    ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    roomy = ingress_client.get("api/dashboard").json()["system"]["storage"]
    assert roomy["floor_bytes"] == FLOOR and roomy["free_bytes"] % MB == 0 and roomy["low"] is False
    monkeypatch.setattr(paths, "free_bytes", lambda path=None: 900 * MB + 12_345)
    low = ingress_client.get("api/dashboard").json()["system"]["storage"]
    assert low == {"free_bytes": 900 * MB, "floor_bytes": FLOOR, "low": True}
    database = section(report(ingress_client), "DATABASE")
    assert (
        "disk: 900 MB free on the data folder's disk (Ember keeps 1 GB free; LOW: workspace writes and workshop files"
        " are refused)" in database
    )
    assert "backups: 0 copies from before upgrades, 0 KB (the newest 10 are kept)" in database
    monkeypatch.setattr(paths, "free_bytes", lambda path=None: 15 * 1024 * MB + 600 * MB)
    assert "disk: 15.6 GB free on the data folder's disk (Ember keeps 1 GB free)\n" in report(ingress_client)


# --- 2. the kept texts of deleted files ------------------------------------------------------------------------------


def age(agent: Any, days: int, path: str) -> None:
    """The kept texts of ``path`` as if written ``days`` ago."""
    when = to_iso(agent.clock.now() - timedelta(days=days))
    with agent.db.transaction() as conn:
        conn.execute("UPDATE workspace_versions SET created_at = ? WHERE path = ?", (when, path))


def kept(agent: Any) -> dict[tuple[str, str], int]:
    found = rows(agent, "SELECT mode, path, COUNT(*) AS n FROM workspace_versions GROUP BY mode, session, path")
    return {(r["mode"], r["path"]): r["n"] for r in found}


def keep(agent: Any, mode: str, session: int, path: str, days: int, content: str = "Kept.") -> None:
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO workspace_versions (mode, session, path, content, reason, created_at)"
            " VALUES (?, ?, ?, ?, 'overwrite', ?)",
            (mode, session, path, content, to_iso(agent.clock.now() - timedelta(days=days))),
        )


def test_the_kept_texts_of_deleted_files_are_pruned_after_14_days(data_dir: Path) -> None:
    agent, ctx = quiet(data_dir)  # a dry run
    for version in ("v3", "v4"):  # a versioned name, renamed and deleted
        path = f"books/book-{version}.md"
        assert call(ctx, "workspace_write", {"path": path, "mode": "create", "content": f"# {version}\n"}).ok
        assert call(ctx, "workspace_write", {"path": path, "mode": "overwrite", "content": "# header\n"}).ok
    assert call(ctx, "workspace_write", {"path": "books/book-v4.md", "mode": "copy", "content": "books/book-v3.md"}).ok
    for path in ("books/book-v3.md", "books/book-v4.md"):
        assert call(ctx, "workspace_write", {"path": path, "mode": "delete"}).ok
    assert call(ctx, "workspace_write", {"path": "books/kept.md", "mode": "create", "content": "# kept\n"}).ok
    assert call(ctx, "workspace_write", {"path": "books/kept.md", "mode": "overwrite", "content": "# kept 2\n"}).ok
    age(agent, 15, "books/book-v3.md")  # deleted 15 days ago
    age(agent, 13, "books/book-v4.md")  # deleted 13 days ago
    age(agent, 30, "books/kept.md")  # still there: its texts stay however old
    scope = agent.scope()
    keep(agent, "dry_run", scope.session + 1, "books/other-session.md", 15)  # a dry-run session's folder is gone
    Jail(data_dir / "workspace").write("books/live.md", "# live\n")  # the live workspace is looked up in a dry run
    keep(agent, "live", 0, "books/live.md", 40)
    keep(agent, "live", 0, "books/live-deleted.md", 15)
    assert agent.prune_versions() == 4  # book-v3's two, the other session's and the deleted live file's
    assert kept(agent) == {
        ("dry_run", "books/book-v4.md"): 3,
        ("dry_run", "books/kept.md"): 1,
        ("live", "books/live.md"): 1,
    }
    back = call(ctx, "workspace_write", {"path": "books/book-v4.md", "mode": "restore"})  # deleted lately: restored
    assert back.ok and ctx.workspace.read("books/book-v4.md") == "# header\n"
    assert agent.prune_versions() == 0


def test_the_kept_texts_take_at_most_their_cap_the_oldest_going_first(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, ctx = quiet(data_dir)
    ctx.workspace.write("books/a.md", "# a\n")
    for number in range(5):
        keep(agent, "dry_run", ctx.scope.session, "books/a.md", 5 - number, content=str(number) * 1_000)
    monkeypatch.setattr(tools, "VERSIONS_MAX_BYTES", 2_500)
    assert agent.prune_versions() == 3
    assert [r["content"][0] for r in rows(agent, "SELECT content FROM workspace_versions ORDER BY id")] == ["3", "4"]


def test_the_scheduler_prunes_them_once_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    pruned: list[str] = []
    monkeypatch.setattr(scheduler, "PRUNE_EVERY_SECONDS", 0.0)  # the old texts every round, the kept ones daily

    async def main() -> None:
        loop: list[scheduler.Scheduler] = []

        def decide() -> Decision:
            if len(pruned) < 3:
                loop[0].poke()
            return Decision(False, reason="waiting")

        agent = SimpleNamespace(
            running_cycle=False,
            stop=threading.Event(),
            prune_versions=lambda: pruned.append("versions"),
            run_policy=lambda: None,
            execute_approved=lambda: None,
            sync_shop=lambda: None,
            publish_live=lambda: None,
            check_events=lambda: None,
            decide=decide,
        )
        economy = SimpleNamespace(tick=lambda: None, clock=SimpleNamespace(now=lambda: datetime.now(UTC)))
        db = SimpleNamespace(prune_events=lambda keep: None, prune_texts=lambda now: pruned.append("texts"))
        loop.append(scheduler.Scheduler(db, economy, agent))  # type: ignore[arg-type]
        loop[0].start()
        for _ in range(100):
            if len(pruned) >= 3:
                break
            await asyncio.sleep(0.02)
        await loop[0].stop()

    asyncio.run(main())
    assert pruned[:3] == ["texts", "versions", "texts"]
    assert pruned.count("versions") == 1


def test_a_copy_of_the_database_cut_short_is_not_left_behind(tmp_path: Path) -> None:
    source = sqlite3.connect(tmp_path / "ember.db")
    source.close()  # its backup fails, as a full disk makes it fail
    with pytest.raises(sqlite3.Error):
        dbmod._backup(source, tmp_path / "backups", 7)
    assert list((tmp_path / "backups").iterdir()) == []


# --- 3. what Home Assistant's backups leave out ----------------------------------------------------------------------


def excluded(relative: str) -> bool:
    """As the Supervisor filters an app's /data: each pattern matched (PurePath.match, from the right) against the full
    path of the file as it sees it, and of each folder above it (an excluded folder is not entered)."""
    full = PurePath("/data/addons/data/local_ember", relative)
    above = [full, *full.parents][: len(PurePath(relative).parts)]
    return any(path.match(pattern) for path in above for pattern in MANIFEST["backup_exclude"])


@pytest.mark.parametrize(
    "path",
    [
        "ember.db",
        "ember.db-wal",
        "ember.db-shm",
        "ember.lock",
        "options.json",
        "etsy/tokens.json",
        "etsy/taxonomy.json",
        "pinterest/tokens.json",
        "workspace/books/haushaltsbuch.md",
        "workspace/books/haushaltsbuch-spec.json",
        "workspace/books/haushaltsbuch.pdf",
        "workspace/books/haushaltsbuch-page1.png",
        "workspace/backups/ember-schema-notes.md",  # a folder of the agent's named "backups"
        "workspace/workshop/scripts/make-a-chart-1.py",
        "memory/lessons.md",
        "dry_run/workspace/books/a.md",
        "backups",  # the folder itself, so a future copy of another name would still be kept
    ],
)
def test_backups_keep_the_database_the_options_the_tokens_and_the_workspace(path: str) -> None:
    assert not excluded(path)


def test_backups_leave_out_only_the_copies_from_before_upgrades(tmp_path: Path) -> None:
    source = sqlite3.connect(tmp_path / "ember.db")
    try:
        copy = dbmod._backup(source, tmp_path / "backups", 89)  # named as a migration's copy is
    finally:
        source.close()
    assert excluded(f"backups/{copy.name}") and not excluded("ember.db")
