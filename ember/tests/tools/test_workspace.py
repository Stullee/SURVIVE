"""The owner's view of the agent's workspace: the file list, reading one file, and who may ask."""

from __future__ import annotations

import io
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import paths
from app.agent import views
from app.agent.sandbox import Jail
from app.agent.service import Agent
from app.config import LoadedSettings, Settings
from app.products import images
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
    jail.write_bytes("shop/cv.pdf", b"%PDF-1.7 made by Ember")
    (jail.root / ".tmp-abc123").write_text("half-written")
    (jail.root / "run.sh").write_bytes(b"#!/bin/sh")
    (jail.root / ".hidden.md").write_text("not a name the agent can use")
    data = ingress_client.get("api/workspace").json()
    assert data["mode"] == "dry_run"
    assert [(f["path"], f["size"], f["kind"]) for f in data["files"]] == [
        ("a-first.txt", 1, "text"),
        ("notes.md", 2, "text"),
        ("projects/drafts/week-1.md", 13, "text"),
        ("projects/printable-meal-planning-templates.md", 13, "text"),
        ("shop/cv.pdf", 22, "product"),
    ]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", f["modified_at"]) for f in data["files"])
    assert data["file_count"] == 5 and data["total_bytes"] == 51 and data["truncated"] is False
    assert data["text_bytes"] == 29 and data["product_bytes"] == 22


def test_an_empty_workspace(ingress_client: TestClient) -> None:
    assert ingress_client.get("api/workspace").json() == {
        "mode": "dry_run",
        "files": [],
        "file_count": 0,
        "folder_count": 0,
        "total_bytes": 0,
        "text_bytes": 0,
        "product_bytes": 0,
        "truncated": False,
        "limits": {
            "files": 5_000,
            "folders": 1_000,
            "text_bytes": 50 * 1024 * 1024,
            "product_bytes": 2 * 1024 * 1024 * 1024,
            "text_file_bytes": 64 * 1024,
            "product_file_bytes": 15 * 1024 * 1024,
        },
        "owners": {"projects": [], "ventures": []},
    }


def test_the_list_counts_folders_for_the_overview(ingress_client: TestClient) -> None:
    """0.26.0: the overview shows how full the workspace is, folders included (they have a limit of their own)."""
    jail = workspace(ingress_client)
    jail.write("notes.md", "x")
    jail.write("shop/cv.md", "x")
    jail.write("workshop/out/chart.md", "x")
    jail.write_bytes("workshop/scripts/old.png", b"\x89PNG")
    data = ingress_client.get("api/workspace").json()
    assert data["file_count"] == 4 and data["folder_count"] == 4  # shop, workshop, workshop/out, workshop/scripts
    assert data["limits"]["folders"] == jail.limits.max_folders and data["limits"]["files"] == jail.limits.max_files


def test_the_list_stops_at_the_file_limit(ingress_client: TestClient) -> None:
    jail = workspace(ingress_client)
    jail.ensure_root()
    for n in range(jail.limits.max_entries + 5):  # more than the agent could write, put there from outside
        (jail.root / f"f{n:04}.md").write_text("x")
    data = ingress_client.get("api/workspace").json()
    assert data["truncated"] is True
    assert data["file_count"] == len(data["files"]) == jail.limits.max_entries
    assert data["files"][0]["path"] == "f0000.md" and data["files"][-1]["path"] == "f5999.md"


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


# --- products (PDF, Word, Excel and PNG files Ember's code made) ---------------------


def product(client: TestClient, path: str, inline: bool = False):  # noqa: ANN201
    return client.get("api/workspace/product", params={"path": path, **({"inline": "true"} if inline else {})})


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("cv.pdf", "application/pdf"),
        ("cv.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("budget.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("cv-page1.png", "image/png"),
    ],
)
def test_a_product_downloads_with_its_type(ingress_client: TestClient, name: str, content_type: str) -> None:
    workspace(ingress_client).write_bytes(f"shop/{name}", b"made by Ember's code")
    response = product(ingress_client, f"shop/{name}")
    assert response.status_code == 200
    assert response.content == b"made by Ember's code"
    assert response.headers["content-type"] == content_type
    assert response.headers["content-disposition"] == f"attachment; filename=\"{name}\"; filename*=UTF-8''{name}"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["cache-control"] == "no-store"


def test_only_a_picture_is_shown_inline(ingress_client: TestClient) -> None:
    jail = workspace(ingress_client)
    jail.write_bytes("shop/cv-page1.png", b"\x89PNG\r\n\x1a\n")
    jail.write_bytes("shop/cv.pdf", b"%PDF-1.7")
    picture = product(ingress_client, "shop/cv-page1.png", inline=True)
    assert picture.headers["content-disposition"].startswith('inline; filename="cv-page1.png"')
    assert picture.headers["content-type"] == "image/png"
    document = product(ingress_client, "shop/cv.pdf", inline=True)
    assert document.headers["content-disposition"].startswith('attachment; filename="cv.pdf"')


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("notes.md", 400),  # text is read with api/workspace/file
        ("../ember.db", 400),
        ("/data/ember.db", 400),
        ("run.sh", 400),
        ("photo.gif", 400),
        ("", 400),
        ("missing.pdf", 404),
        ("shop/missing.png", 404),
    ],
)
def test_bad_product_paths_are_refused(ingress_client: TestClient, path: str, status: int) -> None:
    jail = workspace(ingress_client)
    jail.write("notes.md", "a note")
    jail.write_bytes("shop/cv.pdf", b"%PDF-1.7")
    response = product(ingress_client, path)
    assert response.status_code == status
    assert isinstance(response.json()["error"], str)
    assert "content-disposition" not in response.headers


def test_a_product_is_not_opened_as_text(ingress_client: TestClient) -> None:
    workspace(ingress_client).write_bytes("shop/cv.pdf", b"%PDF-1.7")
    response = read(ingress_client, "shop/cv.pdf")
    assert response.status_code == 400
    assert response.json() == {"error": "shop/cv.pdf is a product file: open it with /api/workspace/product"}


def test_product_links_are_refused(ingress_client: TestClient, data_dir: Path) -> None:
    jail = workspace(ingress_client)
    jail.ensure_root()
    outside = data_dir / "secret.pdf"
    outside.write_text(SECRET)
    os.symlink(outside, jail.root / "link.pdf")
    os.link(outside, jail.root / "copy.pdf")
    for path in ("link.pdf", "copy.pdf"):
        response = product(ingress_client, path)
        assert response.status_code in (400, 404), path  # a link isn't even listed
        assert SECRET not in response.text


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


# --- thumbnails (0.26.0): what the overview and the lists show of a picture ---------------------------------------


def picture(width: int, height: int, kind: str = "PNG", mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), (200, 40, 40, 0) if mode == "RGBA" else (200, 40, 40)).save(out, kind)
    return out.getvalue()


BROKEN = "shop/broken.png can't be shown: it isn't a PNG or JPEG picture Ember's code can read"


def thumb(client: TestClient, path: str):  # noqa: ANN201
    return client.get("api/workspace/thumb", params={"path": path, "v": "2026-10-06T10:00:00Z"})


@pytest.mark.parametrize(("name", "kind"), [("shop/photo.png", "PNG"), ("books/cover.jpg", "JPEG")])
def test_a_picture_has_a_small_thumbnail(ingress_client: TestClient, name: str, kind: str) -> None:
    workspace(ingress_client).write_bytes(name, picture(3000, 2250, kind))
    response = thumb(ingress_client, name)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    stem = name.rsplit("/", 1)[1].rsplit(".", 1)[0]
    assert response.headers["content-disposition"].startswith(f'inline; filename="{stem}-thumbnail.jpg"')
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Its address carries the file's time and size, so the browser may keep it.
    assert response.headers["cache-control"] == "private, max-age=86400"
    small = Image.open(io.BytesIO(response.content))
    assert small.format == "JPEG" and small.size == (views.THUMB_SIDE, views.THUMB_SIDE * 3 // 4)
    assert len(response.content) < 20_000


def test_a_thumbnail_is_white_where_the_picture_is_clear(ingress_client: TestClient) -> None:
    workspace(ingress_client).write_bytes("shop/badge.png", picture(400, 300, mode="RGBA"))
    small = Image.open(io.BytesIO(thumb(ingress_client, "shop/badge.png").content)).convert("RGB")
    assert small.size == (400, 300)  # never larger than the picture
    assert min(small.getpixel((200, 150))) > 245


def test_a_thumbnail_is_made_once_and_again_when_its_picture_changes(
    ingress_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    jail = workspace(ingress_client)
    jail.write_bytes("shop/photo.png", picture(1200, 900))
    made = []
    real = images.small_jpeg
    monkeypatch.setattr(images, "small_jpeg", lambda data, side: made.append(len(data)) or real(data, side))
    first = thumb(ingress_client, "shop/photo.png").content
    assert thumb(ingress_client, "shop/photo.png").content == first and len(made) == 1
    jail.write_bytes("shop/photo.png", picture(600, 900))
    assert Image.open(io.BytesIO(thumb(ingress_client, "shop/photo.png").content)).size == (320, 480)
    assert len(made) == 2


@pytest.mark.parametrize(
    ("path", "status", "error"),
    [
        ("shop/cv.pdf", 400, "shop/cv.pdf isn't a picture: only a PNG or JPEG has a thumbnail"),
        ("notes.md", 400, None),
        ("shop/missing.png", 404, "shop/missing.png doesn't exist"),
        ("../ember.db", 400, None),
        ("/data/dry_run/workspace/shop/photo.png", 400, None),
        ("shop", 400, None),
        ("", 400, None),
        ("shop/broken.png", 400, BROKEN),
    ],
)
def test_only_pictures_have_thumbnails(ingress_client: TestClient, path: str, status: int, error: str | None) -> None:
    jail = workspace(ingress_client)
    jail.write("notes.md", "a note")
    jail.write_bytes("shop/cv.pdf", b"%PDF-1.7")
    jail.write_bytes("shop/photo.png", picture(40, 30))
    jail.write_bytes("shop/broken.png", b"\x89PNG\r\n\x1a\n not really")
    response = thumb(ingress_client, path)
    assert response.status_code == status
    assert isinstance(response.json()["error"], str)
    if error:
        assert response.json()["error"] == error
    assert "content-disposition" not in response.headers


def test_a_thumbnail_is_ingress_only(client_factory: Callable) -> None:
    assert not AccessPolicy().allows(HA_CORE[0], "/api/workspace/thumb", "GET")
    with client_factory(client=HA_CORE) as client:
        workspace(client).write_bytes("shop/photo.png", picture(40, 30))
        assert thumb(client, "shop/photo.png").status_code == 403


def test_the_viewer_draws_the_agents_text_without_links_or_pictures() -> None:
    """0.26.0: Markdown and CSV are drawn element by element from the agent's text, never parsed as HTML (the page
    shares Home Assistant's origin): a link shows its words and its address as text, and nothing the agent wrote
    becomes a link, a picture or anything that loads."""
    script = (paths.WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    start = script.index("  function wsFormatted(ext, text) {")
    end = script.index("  // ------------------------------------------------------------------ ventures", start)
    drawing = script[start:end]
    for sink in ('h("a"', 'h("img"', "href", "src:", "setAttribute(", ".style"):
        assert sink not in drawing, sink
    assert 'h("span", { class: "md-url" }, " (", address, ")")' in drawing  # the address, as text
    assert 'h("code", { class: "md-code-inline", text: code })' in drawing
    index = (paths.WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert '<dialog class="ws-viewer" id="ws-viewer"' in index


def test_a_thumbnail_drawn_before_is_found_without_drawing(ingress_client: TestClient) -> None:
    """The route looks for one drawn before first; only a new one waits for its turn to be drawn."""
    agent = ingress_client.app.state.ember.agent
    workspace(ingress_client).write_bytes("shop/photo.png", picture(800, 600))
    assert agent.workspace_thumb("shop/photo.png", draw=False) == ("photo-thumbnail.jpg", None)
    name, drawn = agent.workspace_thumb("shop/photo.png")
    assert name == "photo-thumbnail.jpg" and drawn
    assert agent.workspace_thumb("shop/photo.png", draw=False) == (name, drawn)
    assert thumb(ingress_client, "shop/photo.png").content == drawn
