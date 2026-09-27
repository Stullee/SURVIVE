"""The agent's files: a jailed folder with size limits.

The agent names files by a short relative path of plain name components. Every
operation walks from the root folder's directory descriptor, one component at a
time, opening each with ``O_NOFOLLOW`` (and ``O_DIRECTORY`` for folders), so a
symlink anywhere on the path is refused, even one swapped in after an earlier
check. A file must be a regular file with a single hard link (a hard link to a
file outside the workspace, such as the database, is refused), and FIFOs or
devices are never opened for reading. Writes go to a temporary file created
with ``O_EXCL`` next to the target, are flushed to disk, and replace the old
file atomically, so a crash never leaves a half-written file. Size, file-count
and total quotas are checked before a write.

Roots: the live agent works in /data/workspace and /data/memory; a dry run in
/data/dry_run/workspace and /data/dry_run/memory, which start empty with every
dry-run session, so fake output never mixes with the live agent's files.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".html", ".css", ".xml"})
MAX_PATH_BYTES = 200
MAX_DEPTH = 4
TEMP_PREFIX = ".tmp-"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class SandboxError(ValueError):
    """A refused path or write; the message is shown to the agent."""


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 64 * 1024
    max_files: int = 300
    max_total_bytes: int = 5 * 1024 * 1024


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    is_dir: bool
    modified: float = 0.0


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

    @contextlib.contextmanager
    def _folder(self, parts: list[str], *, create: bool = False) -> Iterator[int]:
        """A descriptor of the folder ``parts`` below the root, walked without following links."""
        self.ensure_root()
        fds = [_open_dir(str(self.root))]
        try:
            for index, part in enumerate(parts):
                try:
                    fds.append(_open_dir(part, fds[-1]))
                except FileNotFoundError:
                    if not create:
                        raise SandboxError(f"{'/'.join(parts[: index + 1])} doesn't exist") from None
                    os.mkdir(part, 0o755, dir_fd=fds[-1])
                    fds.append(_open_dir(part, fds[-1]))
            yield fds[-1]
        finally:
            for fd in reversed(fds):
                os.close(fd)

    def _file_info(self, folder: int, name: str) -> os.stat_result | None:
        try:
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise SandboxError("links are not allowed in the workspace")
        if not stat.S_ISREG(info.st_mode):
            raise SandboxError(f"{name} is not a regular file")
        if info.st_nlink != 1:
            raise SandboxError(f"{name} has other hard links and can't be used")
        return info

    # --- reading ---

    def read(self, path: str) -> str:
        *folders, name = self.parts(path)
        with self._folder(folders) as folder:
            if self._file_info(folder, name) is None:
                raise SandboxError(f"{path.strip()} doesn't exist")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
            except OSError as exc:
                raise SandboxError(_os_problem(exc)) from None
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SandboxError("not a plain file")
                data = handle.read(self.limits.max_file_bytes + 1)
        return data[: self.limits.max_file_bytes].decode("utf-8", errors="replace")

    def exists(self, path: str) -> bool:
        try:
            self.read(path)
        except SandboxError:
            return False
        return True

    def listing(self, path: str = "") -> list[Entry]:
        parts = self.parts(path, want_file=False)
        try:
            with self._folder(parts) as folder:
                prefix = "/".join(parts)
                entries = []
                with os.scandir(folder) as scan:
                    for child in sorted(scan, key=lambda e: e.name):
                        if child.name.startswith(TEMP_PREFIX) or child.is_symlink():
                            continue
                        info = child.stat(follow_symlinks=False)
                        relative = f"{prefix}/{child.name}" if prefix else child.name
                        if stat.S_ISDIR(info.st_mode):
                            entries.append(Entry(relative, 0, True, info.st_mtime))
                        elif stat.S_ISREG(info.st_mode):
                            entries.append(Entry(relative, info.st_size, False, info.st_mtime))
                return entries
        except SandboxError:
            if parts:
                raise
            return []

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

    def write(self, path: str, content: str, *, append: bool = False, create_only: bool = False) -> int:
        """Write (or append to) a text file atomically; returns the new size in bytes."""
        if not isinstance(content, str):
            raise SandboxError("the content must be text")
        *folders, name = self.parts(path)
        with self._folder(folders, create=True) as folder:
            info = self._file_info(folder, name)
            if info is not None and create_only:
                raise SandboxError(f"{path.strip()} already exists")
            old = b""
            if info is not None and append:
                old = self.read(path).encode("utf-8")
            data = old + content.encode("utf-8")
            if len(data) > self.limits.max_file_bytes:
                raise SandboxError(f"a file can hold at most {self.limits.max_file_bytes // 1024} KB")
            files, total = self.usage()
            previous = info.st_size if info is not None else 0
            if info is None and files + 1 > self.limits.max_files:
                raise SandboxError(f"the workspace holds at most {self.limits.max_files} files; delete some first")
            if total - previous + len(data) > self.limits.max_total_bytes:
                raise SandboxError(f"the workspace holds at most {self.limits.max_total_bytes // (1024 * 1024)} MB")
            temp = f"{TEMP_PREFIX}{secrets.token_hex(6)}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(temp, flags, 0o644, dir_fd=folder)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=folder)
                raise
            os.fsync(folder)
        return len(data)

    def delete(self, path: str) -> None:
        *folders, name = self.parts(path)
        with self._folder(folders) as folder:
            if self._file_info(folder, name) is None:
                raise SandboxError(f"{path.strip()} doesn't exist")
            os.unlink(name, dir_fd=folder)
            os.fsync(folder)
        # Remove folders left empty, deepest first.
        for depth in range(len(folders), 0, -1):
            parent, child = folders[: depth - 1], folders[depth - 1]
            with self._folder(parent) as folder:
                try:
                    os.rmdir(child, dir_fd=folder)
                except OSError:
                    break

    def remove_temporary_files(self) -> int:
        """Leftovers of writes interrupted by a crash (at startup)."""
        removed = 0
        if not self.root.exists():
            return 0
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                if name.startswith(TEMP_PREFIX):
                    with contextlib.suppress(OSError):
                        os.unlink(os.path.join(dirpath, name))
                        removed += 1
        return removed


def _open_dir(name: str, parent: int | None = None) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent) if parent is not None else os.open(name, _DIR_FLAGS)
    except NotADirectoryError:
        # O_DIRECTORY | O_NOFOLLOW on a symlink also fails this way: say which it is.
        info = os.stat(name, dir_fd=parent, follow_symlinks=False) if parent is not None else os.lstat(name)
        if stat.S_ISLNK(info.st_mode):
            raise SandboxError("links are not allowed in the workspace") from None
        raise SandboxError(f"{name} is a file, not a folder") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SandboxError("links are not allowed in the workspace") from None
        raise


def _os_problem(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "links are not allowed in the workspace"
    if exc.errno == errno.ENOENT:
        return "that file doesn't exist"
    return "the file can't be opened"
