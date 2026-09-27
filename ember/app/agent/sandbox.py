"""The agent's files: a jailed folder with size limits.

The agent can only name files by a short relative path made of plain name
components; the path is resolved inside a root folder, and every component is
checked to be a real directory or regular file (never a symlink, never outside
the root). Files are opened without following symlinks and written through a
temporary file that replaces the old one atomically, so a crash never leaves a
half-written file. Size, file-count and total quotas are checked before a
write.

Roots: the live agent works in /data/workspace and /data/memory; a dry run in
/data/dry_run/workspace and /data/dry_run/memory, which start empty with every
dry-run session, so fake output never mixes with the live agent's files.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".html", ".xml"})
MAX_PATH_BYTES = 200
MAX_DEPTH = 4


class SandboxError(ValueError):
    """A refused path or write; the message is shown to the agent."""


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 64 * 1024
    max_files: int = 200
    max_total_bytes: int = 5 * 1024 * 1024


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    is_dir: bool


class Jail:
    """File access below one root folder."""

    def __init__(self, root: Path, limits: Limits | None = None) -> None:
        self.root = root
        self.limits = limits or Limits()

    # --- paths ---

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise SandboxError("the workspace folder is not usable")

    def parts(self, path: str, *, want_file: bool = True) -> list[str]:
        """Validate a relative path and return its components."""
        if not isinstance(path, str):
            raise SandboxError("the path must be text")
        text = path.strip()
        if text.startswith(("/", "~")):
            raise SandboxError("use a path inside the workspace, like notes/ideas.md (no leading '/')")
        text = text.rstrip("/")
        if not text:
            if want_file:
                raise SandboxError("give a file name, for example notes.md")
            return []
        if len(text.encode("utf-8")) > MAX_PATH_BYTES:
            raise SandboxError(f"the path is longer than {MAX_PATH_BYTES} bytes")
        parts = text.split("/")
        if len(parts) > MAX_DEPTH:
            raise SandboxError(f"at most {MAX_DEPTH} folder levels")
        for part in parts:
            if not NAME.match(part) or part in (".", ".."):
                raise SandboxError(
                    f"{part!r} is not allowed: use letters, digits, '.', '_' and '-', starting with a letter or digit"
                )
        if want_file and Path(parts[-1]).suffix.lower() not in TEXT_EXTENSIONS:
            raise SandboxError(f"only text files: {', '.join(sorted(TEXT_EXTENSIONS))}")
        return parts

    def resolve(self, path: str, *, want_file: bool = True, must_exist: bool = False) -> Path:
        """The real location of ``path`` inside the root; no component may be a symlink."""
        self.ensure_root()
        parts = self.parts(path, want_file=want_file)
        current = self.root
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                if must_exist:
                    raise SandboxError(f"{'/'.join(parts[: index + 1])} doesn't exist") from None
                break
            if stat.S_ISLNK(info.st_mode):
                raise SandboxError("links are not allowed in the workspace")
            last = index == len(parts) - 1
            if not last and not stat.S_ISDIR(info.st_mode):
                raise SandboxError(f"{'/'.join(parts[: index + 1])} is a file, not a folder")
            if last and want_file and not stat.S_ISREG(info.st_mode):
                raise SandboxError(f"{'/'.join(parts)} is not a file")
            if last and not want_file and not stat.S_ISDIR(info.st_mode):
                raise SandboxError(f"{'/'.join(parts)} is not a folder")
        root = os.path.realpath(self.root)
        target = os.path.realpath(self.root.joinpath(*parts)) if parts else root
        if target != root and not target.startswith(root + os.sep):
            raise SandboxError("that path leaves the workspace")
        return Path(target)

    # --- reading ---

    def read(self, path: str) -> str:
        target = self.resolve(path, must_exist=True)
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise SandboxError(_os_problem(exc)) from None
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SandboxError("not a regular file")
            data = handle.read(self.limits.max_file_bytes + 1)
        return data[: self.limits.max_file_bytes].decode("utf-8", errors="replace")

    def exists(self, path: str) -> bool:
        try:
            self.resolve(path, must_exist=True)
        except SandboxError:
            return False
        return True

    def listing(self, path: str = "") -> list[Entry]:
        folder = self.resolve(path, want_file=False, must_exist=bool(path))
        if not folder.exists():
            return []
        entries = []
        for child in sorted(os.scandir(folder), key=lambda e: e.name):
            if child.is_symlink():
                continue
            relative = str(Path(child.path).relative_to(os.path.realpath(self.root)))
            if child.is_dir(follow_symlinks=False):
                entries.append(Entry(relative, 0, True))
            elif child.is_file(follow_symlinks=False):
                entries.append(Entry(relative, child.stat(follow_symlinks=False).st_size, False))
        return entries

    def usage(self) -> tuple[int, int]:
        """(files, bytes) in the whole root."""
        files = total = 0
        if not self.root.exists():
            return 0, 0
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                info = os.lstat(os.path.join(dirpath, name))
                if stat.S_ISREG(info.st_mode):
                    files += 1
                    total += info.st_size
        return files, total

    # --- writing ---

    def write(self, path: str, content: str, *, append: bool = False) -> int:
        """Write (or append to) a text file atomically; returns the new size in bytes."""
        if not isinstance(content, str):
            raise SandboxError("the content must be text")
        target = self.resolve(path)
        old = b""
        if target.exists():
            old = self.read(path).encode("utf-8") if append else b""
        data = old + content.encode("utf-8")
        if len(data) > self.limits.max_file_bytes:
            raise SandboxError(f"a file can hold at most {self.limits.max_file_bytes // 1024} KB")
        files, total = self.usage()
        previous = target.stat().st_size if target.exists() else 0
        if not target.exists() and files + 1 > self.limits.max_files:
            raise SandboxError(f"the workspace holds at most {self.limits.max_files} files; delete some first")
        if total - previous + len(data) > self.limits.max_total_bytes:
            raise SandboxError(f"the workspace holds at most {self.limits.max_total_bytes // (1024 * 1024)} MB")
        target.parent.mkdir(parents=True, exist_ok=True)
        # The parent chain may have been created just now; check it again before writing into it.
        self.resolve(path)
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
            raise
        return len(data)

    def delete(self, path: str) -> None:
        target = self.resolve(path, must_exist=True)
        os.unlink(target)
        # Remove folders left empty, up to the root.
        folder = target.parent
        root = Path(os.path.realpath(self.root))
        while folder != root and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent


def _os_problem(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "links are not allowed in the workspace"
    if exc.errno == errno.ENOENT:
        return "that file doesn't exist"
    return "the file can't be opened"
