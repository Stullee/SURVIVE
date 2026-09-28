"""The agent's file jail: paths, links, limits and atomic writes."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app.agent.sandbox import Jail, Limits, Missing, SandboxError


@pytest.fixture
def jail(tmp_path: Path) -> Jail:
    return Jail(tmp_path / "workspace", Limits(max_file_bytes=1_000, max_files=5, max_total_bytes=3_000))


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "a/../../escape.md",
        "/etc/passwd.txt",
        "notes",  # no text extension
        "run.sh",
        ".hidden.md",
        "a/.git/config.md",
        "a b.md",
        "nul\x00.md",
        "a/b/c/d/e.md",  # too deep
        "x" * 70 + ".md",
        "",
        "ümlaut.md",
        "C:\\\\x.md",
    ],
)
def test_bad_paths_are_refused(jail: Jail, path: str) -> None:
    with pytest.raises(SandboxError):
        jail.write(path, "x")


def test_write_read_append_delete(jail: Jail) -> None:
    assert jail.write("ideas/plan.md", "one\n") == 4
    assert jail.write("ideas/plan.md", "two\n", append=True) == 8
    assert jail.read("ideas/plan.md") == "one\ntwo\n"
    assert [(e.path, e.is_dir) for e in jail.listing()] == [("ideas", True)]
    assert [(e.path, e.size) for e in jail.listing("ideas")] == [("ideas/plan.md", 8)]
    jail.delete("ideas/plan.md")
    assert jail.listing() == []  # the empty folder went too
    with pytest.raises(SandboxError, match="doesn't exist"):
        jail.read("ideas/plan.md")


def test_symlinks_are_never_followed(jail: Jail, tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("options and keys")
    jail.ensure_root()
    os.symlink(secret, jail.root / "link.md")
    os.symlink(tmp_path, jail.root / "up")
    with pytest.raises(SandboxError, match="links"):
        jail.read("link.md")
    with pytest.raises(SandboxError, match="links"):
        jail.write("link.md", "overwrite")
    with pytest.raises(SandboxError, match="links"):
        jail.read("up/secret.md")
    assert secret.read_text() == "options and keys"
    assert [e.path for e in jail.listing()] == []  # links are not listed


def test_a_link_swapped_in_after_the_check_is_not_followed(jail: Jail, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from app.agent import sandbox

    secret = tmp_path / "secret.md"
    secret.write_text("keep")
    jail.write("note.md", "x")
    real_info = sandbox.Jail._file_info

    def swap(self, folder, name):  # noqa: ANN001, ANN202
        info = real_info(self, folder, name)
        os.unlink(name, dir_fd=folder)
        os.symlink(secret, name, dir_fd=folder)
        return info

    monkeypatch.setattr(sandbox.Jail, "_file_info", swap)
    with pytest.raises(SandboxError, match="links"):
        jail.read("note.md")
    assert secret.read_text() == "keep"


def test_a_folder_swapped_for_a_link_is_not_followed(jail: Jail, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    jail.write("notes/a.md", "x")
    os.unlink(jail.root / "notes" / "a.md")
    os.rmdir(jail.root / "notes")
    os.symlink(outside, jail.root / "notes")
    with pytest.raises(SandboxError, match="links"):
        jail.write("notes/b.md", "escape")
    assert list(outside.iterdir()) == []


def test_hard_links_and_special_files_are_refused(jail: Jail, tmp_path: Path) -> None:
    database = tmp_path / "ember.db"
    database.write_text("secret rows")
    jail.ensure_root()
    os.link(database, jail.root / "db.md")
    with pytest.raises(SandboxError, match="hard links"):
        jail.read("db.md")
    with pytest.raises(SandboxError, match="hard links"):
        jail.write("db.md", "overwrite")
    assert database.read_text() == "secret rows"
    os.mkfifo(jail.root / "pipe.md")
    with pytest.raises(SandboxError, match="regular file"):
        jail.read("pipe.md")  # must not hang waiting for a writer


def test_create_only_and_leftover_temp_files(jail: Jail) -> None:
    jail.write("a.md", "x", create_only=True)
    with pytest.raises(SandboxError, match="already exists"):
        jail.write("a.md", "y", create_only=True)
    (jail.root / ".tmp-deadbeef").write_text("half")
    assert [e.path for e in jail.listing()] == ["a.md"]
    assert jail.remove_temporary_files() == 1


def test_limits(jail: Jail) -> None:
    with pytest.raises(SandboxError, match="at most 0 KB|at most"):
        jail.write("big.md", "x" * 1_001)
    for index in range(3):
        jail.write(f"f{index}.md", "x" * 1_000)
    with pytest.raises(SandboxError, match="MB|holds at most"):
        jail.write("f3.md", "x" * 100)
    jail.write("f0.md", "y" * 500)  # replacing a file only counts the difference
    jail.write("f3.md", "x" * 100)
    jail.write("f4.md", "x")
    with pytest.raises(SandboxError, match="files"):
        jail.write("f5.md", "x")


def test_a_failed_write_keeps_the_old_file(jail: Jail, monkeypatch) -> None:  # noqa: ANN001
    jail.write("keep.md", "old")

    def crash(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        jail.write("keep.md", "new")
    assert jail.read("keep.md") == "old"
    assert [e.path for e in jail.listing()] == ["keep.md"]  # no temporary file left


def test_a_file_where_a_folder_is_expected(jail: Jail) -> None:
    jail.write("a.md", "x")
    with pytest.raises(SandboxError, match="not a folder|is a file"):
        jail.write("a.md/b.md", "x")


# --- the whole tree ---


def test_walk_lists_every_folder_in_path_order(jail: Jail, tmp_path: Path) -> None:
    roomy = Jail(jail.root)
    for path in ("a/x.md", "a-first.md", "b/c/d/deep.md", "notes.md"):
        roomy.write(path, "x" * len(path))
    (jail.root / ".tmp-abc123").write_text("half-written")
    os.symlink(tmp_path, jail.root / "up")
    os.symlink(tmp_path / "secret.md", jail.root / "link.md")
    os.mkfifo(jail.root / "pipe.md")
    os.makedirs(jail.root / "b/c/d/e")  # deeper than a path can go: listed, never entered
    (jail.root / "b/c/d/e/hidden.md").write_text("x")
    tree = roomy.walk(100)
    assert [(e.path, e.is_dir, e.size) for e in tree.entries] == [
        ("a-first.md", False, 10),
        ("a", True, 0),
        ("a/x.md", False, 6),
        ("b", True, 0),
        ("b/c", True, 0),
        ("b/c/d", True, 0),
        ("b/c/d/deep.md", False, 13),
        ("b/c/d/e", True, 0),
        ("notes.md", False, 8),
    ]
    assert tree.truncated is False and all(e.modified > 0 for e in tree.entries)
    assert [e.path for e in tree.files] == ["a-first.md", "a/x.md", "b/c/d/deep.md", "notes.md"]
    cut = roomy.walk(3)
    assert [e.path for e in cut.entries] == ["a-first.md", "a", "a/x.md"] and cut.truncated is True
    assert roomy.walk(9).truncated is False
    assert roomy.usage() == (4, 5, 37)  # files, folders and bytes: folders aren't files
    os.symlink(jail.root, tmp_path / "linked")
    assert Jail(tmp_path / "linked").walk(10).entries == [] and Jail(tmp_path / "linked").usage() == (0, 0, 0)


def test_usage_counts_files_and_folders_apart(jail: Jail) -> None:
    assert jail.usage() == (0, 0, 0)
    jail.write("ideas/plan.md", "12345")
    used = jail.usage()
    assert (used.files, used.folders, used.size) == (1, 1, 5)
    for index in range(3):
        jail.write(f"f{index}.md", "x")  # 4 files and 1 folder: the limit of 5 entries
    with pytest.raises(SandboxError, match="at most 5 files"):
        jail.write("f3.md", "x")
    with pytest.raises(SandboxError, match="at most 5 entries"):
        jail.write("more/f3.md", "x")


def test_entries_deleted_while_listing_are_skipped(tmp_path: Path) -> None:
    """The agent deletes files while the owner's view lists them: an entry gone before its stat is left out."""
    jail = Jail(tmp_path / "workspace")
    jail.write("churn/keep.md", "x")
    folder = jail.root / "churn"
    done = threading.Event()

    def churn() -> None:
        try:
            for _ in range(150):
                paths = [folder / f"f{i}.md" for i in range(20)]
                for path in paths:
                    path.write_text("x")
                for path in paths:
                    path.unlink()
                (folder / "sub").mkdir()
                (folder / "sub").rmdir()
        finally:
            done.set()

    thread = threading.Thread(target=churn)
    thread.start()
    try:
        while not done.is_set():
            assert "churn/keep.md" in [e.path for e in jail.listing("churn")]
            assert "churn/keep.md" in [e.path for e in jail.walk(100).entries]
            assert jail.usage().files >= 1
    finally:
        thread.join()


def test_a_file_deleted_before_it_is_read_is_missing(jail: Jail) -> None:
    jail.write("notes/a.md", "x")
    jail.delete("notes/a.md")  # the folder went too
    for path in ("notes/a.md", "a.md"):
        with pytest.raises(Missing, match="doesn't exist"):
            jail.read(path)
    with pytest.raises(Missing):
        jail.listing("notes")
    with pytest.raises(Missing):
        jail.delete("a.md")


# --- names are used exactly as given ---


@pytest.mark.parametrize(
    "path",
    ["drafts\n/post.md", "post.md\n", "drafts/post.md\n", " post.md", "post.md ", "a\tb.md", "a\r/x.md", "x\x7f.md"],
)
def test_no_other_name_passes_for_a_valid_one(jail: Jail, path: str) -> None:
    jail.ensure_root()
    with pytest.raises(SandboxError, match="not allowed|control character"):
        jail.write(path, "x")
    with pytest.raises(SandboxError):
        jail.parts(path)
    assert os.listdir(jail.root) == []  # nothing was created, not even a folder


def test_a_name_on_disk_is_never_read_as_another(jail: Jail) -> None:
    jail.write("notes.md", "the real notes")
    (jail.root / " notes.md").write_text("put there from outside")
    os.mkdir(jail.root / "drafts\n")
    (jail.root / "drafts\n" / "post.md").write_text("x")
    for path in (" notes.md", "drafts\n/post.md"):
        with pytest.raises(SandboxError):
            jail.read(path)
    assert jail.read("notes.md") == "the real notes"
