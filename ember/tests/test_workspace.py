"""The owner's view of the agent's workspace: the file list, reading one file, and who may ask."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.sandbox import Jail
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.security import AccessPolicy
from app.web import _attachment
from tests.conftest import HA_CORE, INGRESS

SECRET = "anthropic key and the ledger"


def workspace(client: TestClient) -> Jail:
    return client.app.state.ember.agent.roots()[0]


def read(client: TestClient, path: str):  # noqa: ANN201
    return client.get("api/workspace/file", params={"path": path})


# --- the file list -------------------------------------------------------------


def test_the_list_walks_every_folder_sorted_by_path(ingress_client: TestClient) -> None:
    jail = workspace(ingress_client)
    jail.write("projects/printable-meal-planning-templates.md", "# Meal plans\n")
    jail.write("projects/drafts/week-1.md", "Monday: soup\n")
    jail.write("a-first.txt", "x")
    jail.write("notes.md", "é")  # sizes are bytes
    (jail.root / ".tmp-abc123").write_text("half-written")
    (jail.root / "photo.png").write_bytes(b"\x89PNG")
    (jail.root / ".hidden.md").write_text("not a name the agent can use")
    data = ingress_client.get("api/workspace").json()
    assert data["mode"] == "dry_run"
    assert [(f["path"], f["size"]) for f in data["files"]] == [
        ("a-first.txt", 1),
        ("notes.md", 2),
        ("projects/drafts/week-1.md", 13),
        ("projects/printable-meal-planning-templates.md", 13),
    ]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", f["modified_at"]) for f in data["files"])
    assert data["file_count"] == 4 and data["total_bytes"] == 29 and data["truncated"] is False


def test_an_empty_workspace(ingress_client: TestClient) -> None:
    assert ingress_client.get("api/workspace").json() == {
        "mode": "dry_run",
        "files": [],
        "file_count": 0,
        "total_bytes": 0,
        "truncated": False,
    }


def test_the_list_stops_at_the_file_limit(ingress_client: TestClient) -> None:
    jail = workspace(ingress_client)
    jail.ensure_root()
    for n in range(jail.limits.max_files + 5):  # more than the agent could write, put there from outside
        (jail.root / f"f{n:03}.md").write_text("x")
    data = ingress_client.get("api/workspace").json()
    assert data["truncated"] is True
    assert data["file_count"] == len(data["files"]) == jail.limits.max_files
    assert data["files"][0]["path"] == "f000.md" and data["files"][-1]["path"] == "f299.md"


def test_links_are_not_listed(ingress_client: TestClient, data_dir: Path) -> None:
    jail = workspace(ingress_client)
    jail.write("real.md", "mine")
    outside = data_dir / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(SECRET)
    os.symlink(outside / "secret.md", jail.root / "link.md")
    os.symlink(outside, jail.root / "folder")
    body = ingress_client.get("api/workspace").text
    assert [f["path"] for f in ingress_client.get("api/workspace").json()["files"]] == ["real.md"]
    assert "secret" not in body


def test_dry_run_and_live_have_their_own_folders(client_factory: Callable, data_dir: Path) -> None:
    with client_factory(LoadedSettings(Settings(dry_run=True))) as client:
        workspace(client).write("dry.md", "test run")
        assert workspace(client).root == data_dir / "dry_run" / "workspace"
        data = client.get("api/workspace").json()
        assert data["mode"] == "dry_run" and [f["path"] for f in data["files"]] == ["dry.md"]
        assert read(client, "dry.md").text == "test run"
    with client_factory(LoadedSettings(Settings(dry_run=False))) as client:
        workspace(client).write("live.md", "for real")
        assert workspace(client).root == data_dir / "workspace"
        data = client.get("api/workspace").json()
        assert data["mode"] == "live" and [f["path"] for f in data["files"]] == ["live.md"]
        assert read(client, "live.md").text == "for real"
        assert read(client, "dry.md").status_code == 404


# --- one file ------------------------------------------------------------------


def test_a_file_is_plain_text_that_always_downloads(ingress_client: TestClient) -> None:
    html = '<!doctype html><script>fetch("/auth/token")</script><svg onload="alert(1)"/>\n'
    workspace(ingress_client).write("site/index.html", html)
    response = read(ingress_client, "site/index.html")
    assert response.status_code == 200
    assert response.text == html
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Replaces the dashboard's policy: nothing in it may run or load, even if a browser did render it.
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["content-disposition"] == "attachment; filename=\"index.html\"; filename*=UTF-8''index.html"
    assert response.headers["cache-control"] == "no-store"


def test_text_is_utf8(ingress_client: TestClient) -> None:
    workspace(ingress_client).write("notes/hello.md", "Grüße, 你好 👋\n")
    response = read(ingress_client, "notes/hello.md")
    assert response.content == "Grüße, 你好 👋\n".encode()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("plan.md", "attachment; filename=\"plan.md\"; filename*=UTF-8''plan.md"),
        ("a\"b\\c/d;e%f'g.md", "attachment; filename=\"abcdefg.md\"; filename*=UTF-8''abcdefg.md"),
        ("a\r\nSet: b.md", "attachment; filename=\"aSet: b.md\"; filename*=UTF-8''aSet%3A%20b.md"),  # no header split
        ("\u202egpj.md", "attachment; filename=\"gpj.md\"; filename*=UTF-8''gpj.md"),
        ("grüße.md", "attachment; filename=\"gr__e.md\"; filename*=UTF-8''gr%C3%BC%C3%9Fe.md"),
        ('\x07"', "attachment; filename=\"file.txt\"; filename*=UTF-8''file.txt"),
    ],
)
def test_download_names_are_safe(name: str, expected: str) -> None:
    assert _attachment(name) == expected


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("../ember.db", 400),
        ("../options.json", 400),
        ("notes/../../ember.db", 400),
        ("..\\ember.db", 400),
        ("/data/ember.db", 400),
        ("/etc/passwd", 400),
        ("~/secret.md", 400),
        ("", 400),
        ("projects", 400),  # a folder
        ("projects/", 400),
        ("run.sh", 400),
        ("a/b/c/d/e.md", 400),
        ("nul\x00.md", 400),
        ("x" * 300, 400),
        ("missing.md", 404),
        ("projects/missing.md", 404),
        ("nowhere/missing.md", 404),
        ("projects/plan.md/inside.md", 404),
    ],
)
def test_bad_paths_are_refused(ingress_client: TestClient, path: str, status: int) -> None:
    workspace(ingress_client).write("projects/plan.md", "a plan")
    response = read(ingress_client, path)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    error = response.json()["error"]
    assert isinstance(error, str) and error
    assert "content-disposition" not in response.headers


def test_a_path_that_is_a_folder_with_a_text_name(ingress_client: TestClient) -> None:
    workspace(ingress_client).write("notes.md/inside.md", "x")
    response = read(ingress_client, "notes.md")
    assert response.status_code == 400 and response.json() == {"error": "notes.md is a folder, not a file"}


def test_escaping_through_the_query_string_is_refused(ingress_client: TestClient, data_dir: Path) -> None:
    (data_dir / "dry_run" / "secret.md").write_text(SECRET)
    for raw in ("..%2Fsecret.md", "%2E%2E/secret.md", "%2Fdata%2Fdry_run%2Fsecret.md"):
        response = ingress_client.get("api/workspace/file?path=" + raw)
        assert response.status_code == 400, raw
        assert SECRET not in response.text


def test_symlinks_are_refused(ingress_client: TestClient, data_dir: Path) -> None:
    jail = workspace(ingress_client)
    jail.ensure_root()
    outside = data_dir / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(SECRET)
    os.symlink(outside / "secret.md", jail.root / "link.md")
    os.symlink(outside, jail.root / "folder")
    for path in ("link.md", "folder/secret.md"):
        response = read(ingress_client, path)
        assert response.status_code in (400, 404), path
        assert SECRET not in response.text


def test_hard_links_are_refused(ingress_client: TestClient, data_dir: Path) -> None:
    """A hard link to a file outside the workspace (like the database) is refused even though it is listed."""
    jail = workspace(ingress_client)
    jail.ensure_root()
    outside = data_dir / "secret.md"
    outside.write_text(SECRET)
    os.link(outside, jail.root / "copy.md")
    response = read(ingress_client, "copy.md")
    assert response.status_code == 400
    assert "hard links" in response.json()["error"]
    assert SECRET not in response.text


def test_too_big_files_are_refused(ingress_client: TestClient) -> None:
    jail = workspace(ingress_client)
    jail.ensure_root()
    (jail.root / "huge.md").write_text("x" * (jail.limits.max_file_bytes + 1))
    (jail.root / "full.md").write_text("y" * jail.limits.max_file_bytes)
    response = read(ingress_client, "huge.md")
    assert response.status_code == 400
    assert response.json() == {"error": "huge.md is larger than 64 KB, so it can't be opened here"}
    assert len(read(ingress_client, "full.md").content) == jail.limits.max_file_bytes
    sizes = {f["path"]: f["size"] for f in ingress_client.get("api/workspace").json()["files"]}
    assert sizes == {"full.md": 65536, "huge.md": 65537}


def test_a_name_on_disk_is_never_shown_or_opened_as_another(ingress_client: TestClient) -> None:
    """Only names the agent could write are listed, and each opens exactly itself (nothing is trimmed)."""
    jail = workspace(ingress_client)
    jail.write("notes.md", "the real notes")
    (jail.root / " notes.md").write_text(SECRET)
    (jail.root / "notes.md\n").write_text(SECRET)
    os.mkdir(jail.root / "drafts\n")
    (jail.root / "drafts\n" / "post.md").write_text(SECRET)
    assert [f["path"] for f in ingress_client.get("api/workspace").json()["files"]] == ["notes.md"]
    for path in (" notes.md", "notes.md\n", "drafts\n/post.md", "notes.md "):
        response = read(ingress_client, path)
        assert response.status_code == 400, repr(path)
        assert SECRET not in response.text
    assert read(ingress_client, "notes.md").text == "the real notes"


def test_the_owner_lists_and_opens_while_the_agent_writes_and_deletes(ingress_client: TestClient) -> None:
    """A file (or its folder) deleted between listing and reading is a 404; one replaced meanwhile still opens."""
    jail = workspace(ingress_client)
    jail.write("keep.md", "stays")
    done = threading.Event()

    def agent_at_work() -> None:
        for n in range(20_000):  # bounded, though it stops as soon as the owner is done
            if done.is_set():
                return
            jail.write("drafts/post.md", f"draft {n}")
            jail.write("keep.md", f"stays, version {n}")  # replaced, never missing
            jail.delete("drafts/post.md")  # the empty folder goes too

    thread = threading.Thread(target=agent_at_work)
    thread.start()
    statuses = []
    try:
        for _ in range(150):
            listed = ingress_client.get("api/workspace")
            assert listed.status_code == 200
            assert "keep.md" in [f["path"] for f in listed.json()["files"]]
            kept = read(ingress_client, "keep.md")
            assert kept.status_code == 200 and kept.text.startswith("stays"), kept.text
            response = read(ingress_client, "drafts/post.md")
            statuses.append(response.status_code)
            assert response.status_code in (200, 404), response.text
            if response.status_code == 200:
                assert response.text.startswith("draft ")
    finally:
        done.set()
        thread.join()
    assert 404 in statuses  # the race was really run


def test_fifos_and_devices_are_never_opened(ingress_client: TestClient) -> None:
    jail = workspace(ingress_client)
    jail.ensure_root()
    os.mkfifo(jail.root / "pipe.md")  # opening it for reading would block forever
    assert ingress_client.get("api/workspace").json()["files"] == []
    assert read(ingress_client, "pipe.md").status_code == 404


# --- who may ask ---------------------------------------------------------------


def test_the_host_network_cannot_read_the_workspace(client_factory: Callable) -> None:
    """Unlike api/sensors, these answers contain agent-written text: Ingress only."""
    policy = AccessPolicy()
    for path in ("/api/workspace", "/api/workspace/file"):
        assert policy.allows(INGRESS[0], path, "GET")
        assert not policy.allows(HA_CORE[0], path, "GET")
        assert not policy.allows(HA_CORE[0], path, "HEAD")
    with client_factory(client=HA_CORE) as client:
        workspace(client).write("plan.md", SECRET)
        assert client.get("api/sensors").status_code == 200
        for response in (client.get("api/workspace"), read(client, "plan.md")):
            assert response.status_code == 403
            assert SECRET not in response.text
    with client_factory(client=("172.30.33.9", 1234)) as client:
        assert client.get("api/workspace").status_code == 403
        assert read(client, "plan.md").status_code == 403


def test_the_list_carries_the_dashboard_headers(ingress_client: TestClient) -> None:
    response = ingress_client.get("api/workspace")
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_without_the_agent_both_answer_503(client_factory: Callable, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Agent) -> None:
        raise RuntimeError("the workspace folder is not usable")

    monkeypatch.setattr(Agent, "recover", broken)
    with client_factory() as client:
        assert client.app.state.ember.agent is None
        for response in (client.get("api/workspace"), read(client, "plan.md")):
            assert response.status_code == 503
            assert response.json() == {"error": "the agent is not available, see the system log"}


def test_the_dashboard_has_a_workspace_tab_after_mind(ingress_client: TestClient) -> None:
    page = ingress_client.get("/").text
    tabs = re.findall(r'role="tab" class="tab" id="tab-([a-z]+)"', page)
    assert tabs[tabs.index("mind") + 1] == "workspace"
    assert 'id="panel-workspace"' in page
