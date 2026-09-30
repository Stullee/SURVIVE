"""The agent's files: a jailed folder with size limits.

The agent names files by a short relative path of plain name components. Every
operation walks from the root folder's directory descriptor, one component at a
time, opening each with ``O_NOFOLLOW`` (and ``O_DIRECTORY`` for folders), so a
symlink anywhere on the path is refused, even one swapped in after an earlier
check. A file must be a regular file with no other hard link (a hard link to a
file outside the workspace, such as the database, is refused), and FIFOs or
devices are never opened for reading. Writes go to a temporary file created
with ``O_EXCL`` next to the target, are flushed to disk, and replace the old
file atomically, so a crash never leaves a half-written file. Size, file-count
and total quotas are checked before a write. A name is used exactly as given
(nothing is trimmed, no control characters), so no other name on disk can pass
for a valid one, and listings leave any other name out.

The agent works while the owner reads: listings skip an entry deleted between
reading its folder and looking at it, a file replaced while it is opened reads
as either version, and a path that doesn't exist (or stops existing while it is
used) raises ``Missing``.

Roots: the live agent works in /data/workspace and /data/memory; a dry run in
/data/dry_run/workspace and /data/dry_run/memory, which start empty with every
dry-run session, so fake output never mixes with the live agent's files.

Two kinds of files live in the workspace: text the agent writes (``write``), and
products (PDF, Word, Excel, PowerPoint, PNG, JPEG) that only Ember's own code
writes (``write_bytes``): made from the agent's text by app.products, or made in
the workshop and checked by app.products.checks before they are kept. Each kind
has its own size limits.
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
from typing import NamedTuple

NAME_CHARS = 64  # a file or folder name (0.12.0: the workshop is told it)
NAME = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{NAME_CHARS - 1}}}"
)  # used with fullmatch: "$" also matches before a "\n"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# .py: scripts the workshop wrote and the agent keeps (text only here: nothing in Ember ever runs them).
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".html", ".css", ".xml", ".py"})
PRODUCT_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg"})
KINDS = {"text": TEXT_EXTENSIONS, "product": PRODUCT_EXTENSIONS, "any": TEXT_EXTENSIONS | PRODUCT_EXTENSIONS}
MAX_PATH_BYTES = 200
MAX_DEPTH = 4
TEMP_PREFIX = ".tmp-"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class SandboxError(ValueError):
    """A refused path or write; the message is shown to the agent."""


class QuotaError(SandboxError):
    """0.12.0: a write refused because the workspace (or the file) is full: a limit, not a bad path, so it is never a
    strike against the agent."""


class Missing(SandboxError):
    """A file or folder that doesn't exist (or was deleted while it was used)."""


@dataclass(frozen=True)
class Limits:
    """0.12.0: files count, folders have their own bound (300 entries, folders included, stopped production at about 21
    products, each a PDF, a Word copy, photos and the folders they sit in)."""

    max_file_bytes: int = 64 * 1024  # a text file
    max_files: int = 5_000
    max_folders: int = 1_000
    max_total_bytes: int = 50 * 1024 * 1024  # all text files
    max_product_bytes: int = 15 * 1024 * 1024  # one product file
    max_product_total_bytes: int = 2 * 1024 * 1024 * 1024  # all product files

    @property
    def max_entries(self) -> int:
        """Everything the workspace can hold, files and folders: what a walk of the whole of it may meet."""
        return self.max_files + self.max_folders


def kind_of(path: str) -> str | None:
    """ "text" or "product" by the file's ending, or None."""
    suffix = Path(path).suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in PRODUCT_EXTENSIONS:
        return "product"
    return None


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    is_dir: bool
    modified: float = 0.0


@dataclass(frozen=True)
class Tree:
    """What walk() found, sorted by path, and whether it stopped at its limit."""

    entries: list[Entry]
    truncated: bool = False

    @property
    def files(self) -> list[Entry]:
        return [e for e in self.entries if not e.is_dir]


class Usage(NamedTuple):
    """What the whole root holds: files count toward ``Limits.max_files``, folders toward ``Limits.max_folders``."""

    files: int
    folders: int
    size: int  # bytes in the files


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

    def parts(self, path: str, *, want_file: bool = True, kinds: str = "text") -> list[str]:
        """Validate a relative path and return its components, exactly as given (nothing is trimmed).

        ``kinds`` says which files the caller handles: "text" (the agent's own writing), "product" or "any".
        """
        if not isinstance(path, str):
            raise SandboxError("the path must be text")
        if _CONTROL.search(path):
            raise SandboxError("the path contains a control character, such as a line break")
        if path.startswith(("/", "~")):
            raise SandboxError("use a path inside the workspace, like notes/ideas.md (no leading '/')")
        text = path.rstrip("/")
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
            if not NAME.fullmatch(part) or part in (".", ".."):
                raise SandboxError(
                    f"{part!r} is not allowed: use letters, digits, '.', '_' and '-', starting with a letter or digit"
                )
        allowed = KINDS[kinds]
        if want_file and Path(parts[-1]).suffix.lower() not in allowed:
            if kinds == "text":
                extra = (
                    " (PDF, Word, Excel and picture files are made with make_document, make_spreadsheet, "
                    "make_image or the workshop)"
                )
                raise SandboxError(f"only text files: {', '.join(sorted(TEXT_EXTENSIONS))}{extra}")
            raise SandboxError(f"only these files: {', '.join(sorted(allowed))}")
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
                        raise Missing(f"{'/'.join(parts[: index + 1])} doesn't exist") from None
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
        # No links left (0): replaced or deleted just now. Opening the name then finds the new file or nothing.
        if info.st_nlink > 1:
            raise SandboxError(f"{name} has other hard links and can't be used")
        return info

    # --- reading ---

    def read(self, path: str) -> str:
        *folders, name = self.parts(path)
        with self._folder(folders) as folder:
            if self._file_info(folder, name) is None:
                raise Missing(f"{path} doesn't exist")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
            except FileNotFoundError:
                raise Missing(f"{path} doesn't exist") from None
            except OSError as exc:
                raise SandboxError(_os_problem(exc)) from None
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                # No links left: deleted or replaced since it was opened, and still the text it had then.
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    raise SandboxError("not a plain file")
                data = handle.read(self.limits.max_file_bytes + 1)
        return data[: self.limits.max_file_bytes].decode("utf-8", errors="replace")

    def read_bytes(self, path: str) -> bytes:
        """A product file's bytes (never a text file's: the agent's text is read with read())."""
        *folders, name = self.parts(path, kinds="product")
        with self._folder(folders) as folder:
            if self._file_info(folder, name) is None:
                raise Missing(f"{path} doesn't exist")
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
            except FileNotFoundError:
                raise Missing(f"{path} doesn't exist") from None
            except OSError as exc:
                raise SandboxError(_os_problem(exc)) from None
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    raise SandboxError("not a plain file")
                if info.st_size > self.limits.max_product_bytes:
                    raise SandboxError(f"{path} is larger than {self.limits.max_product_bytes // (1024 * 1024)} MB")
                return handle.read(self.limits.max_product_bytes + 1)[: self.limits.max_product_bytes]

    def size_of(self, path: str, kinds: str = "any") -> int | None:
        """The size of a file, or None if it doesn't exist."""
        *folders, name = self.parts(path, kinds=kinds)
        try:
            with self._folder(folders) as folder:
                info = self._file_info(folder, name)
        except Missing:
            return None
        return None if info is None else info.st_size

    def exists(self, path: str) -> bool:
        try:
            self.read(path)
        except SandboxError:
            return False
        return True

    def listing(self, path: str = "") -> list[Entry]:
        """The files and folders in one folder (the root by default)."""
        parts = self.parts(path, want_file=False)
        try:
            with self._folder(parts) as folder:
                return _children(folder, "/".join(parts))
        except SandboxError:
            if parts:
                raise
            return []

    def walk(self, limit: int) -> Tree:
        """Every file and folder below the root, at most ``limit`` of them, sorted by path.

        A folder comes right before what it holds. Like listing(), the walk goes folder by folder through
        descriptors opened without following links, as deep as a path can go (MAX_DEPTH), and leaves out links,
        names the jail refuses (temporary files among them) and entries deleted while it looks.
        """
        entries: list[Entry] = []
        with contextlib.closing(self._tree()) as tree:
            for entry in tree:
                if len(entries) >= limit:
                    return Tree(entries, truncated=True)
                entries.append(entry)
        return Tree(entries)

    def _tree(self) -> Iterator[Entry]:
        try:
            with self._folder([]) as root:
                yield from _below(root, "", 1)
        except SandboxError:  # no usable root folder: nothing to list
            return

    def usage(self) -> Usage:
        """Files, folders and bytes in the whole root."""
        files = folders = size = 0
        with contextlib.closing(self._tree()) as tree:
            for entry in tree:
                if entry.is_dir:
                    folders += 1
                else:
                    files += 1
                    size += entry.size
        return Usage(files, folders, size)

    def sizes(self) -> tuple[int, int]:
        """Bytes in text files and in product files, in the whole root."""
        text = product = 0
        with contextlib.closing(self._tree()) as tree:
            for entry in tree:
                if entry.is_dir:
                    continue
                if kind_of(entry.path) == "product":
                    product += entry.size
                else:
                    text += entry.size
        return text, product

    # --- writing ---

    def write(self, path: str, content: str, *, append: bool = False, create_only: bool = False) -> int:
        """Write (or append to) a text file atomically; returns the new size in bytes."""
        if not isinstance(content, str):
            raise SandboxError("the content must be text")
        *folders, name = self.parts(path)
        self._room_for_folders(folders)
        with self._folder(folders, create=True) as folder:
            info = self._file_info(folder, name)
            if info is not None and create_only:
                raise SandboxError(f"{path} already exists")
            old = b""
            if info is not None and append:
                old = self.read(path).encode("utf-8")
            data = old + content.encode("utf-8")
            if len(data) > self.limits.max_file_bytes:
                raise QuotaError(f"a file can hold at most {self.limits.max_file_bytes // 1024} KB")
            previous = info.st_size if info is not None else 0
            if info is None:
                self._room_for_a_file()
            text_bytes, _ = self.sizes()
            if text_bytes - previous + len(data) > self.limits.max_total_bytes:
                raise QuotaError(
                    f"text files take at most {self.limits.max_total_bytes // (1024 * 1024)} MB in the workspace; "
                    "delete old ones first"
                )
            _replace(folder, name, data)
        return len(data)

    def write_bytes(self, path: str, data: bytes) -> int:
        """Write a product file atomically (only Ember's own code calls this, never with the agent's bytes)."""
        if not isinstance(data, bytes | bytearray):
            raise SandboxError("a product is bytes")
        *folders, name = self.parts(path, kinds="product")
        limits = self.limits
        if len(data) > limits.max_product_bytes:
            raise QuotaError(f"a product file can hold at most {limits.max_product_bytes // (1024 * 1024)} MB")
        self._room_for_folders(folders)
        with self._folder(folders, create=True) as folder:
            info = self._file_info(folder, name)
            if info is None:
                self._room_for_a_file()
            _, product_bytes = self.sizes()
            previous = info.st_size if info is not None else 0
            if product_bytes - previous + len(data) > limits.max_product_total_bytes:
                raise QuotaError(
                    f"products take at most {limits.max_product_total_bytes // (1024 * 1024)} MB in the workspace; "
                    "delete old ones first"
                )
            _replace(folder, name, bytes(data))
        return len(data)

    def _room_for_folders(self, folders: list[str]) -> None:
        """Refuse a path whose new folders would be more than the workspace may hold."""
        missing = self._missing_folders(folders)
        if missing and self.usage().folders + missing > self.limits.max_folders:
            raise QuotaError(f"the workspace holds at most {self.limits.max_folders:,} folders; use the ones you have")

    def _room_for_a_file(self) -> None:
        """Refuse a new file when the workspace holds as many as it may (folders don't count, 0.12.0)."""
        if self.usage().files + 1 > self.limits.max_files:
            raise QuotaError(f"the workspace holds at most {self.limits.max_files:,} files; delete some first")

    def _missing_folders(self, folders: list[str]) -> int:
        """How many folders of the path don't exist yet (they count toward the folder limit)."""
        for depth in range(len(folders), -1, -1):
            try:
                with self._folder(folders[:depth]):
                    return len(folders) - depth
            except SandboxError:
                continue
        return len(folders)

    def delete(self, path: str) -> None:
        *folders, name = self.parts(path, kinds="any")
        with self._folder(folders) as folder:
            if self._file_info(folder, name) is None:
                raise Missing(f"{path} doesn't exist")
            try:
                os.unlink(name, dir_fd=folder)
            except FileNotFoundError:
                raise Missing(f"{path} doesn't exist") from None
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


def _replace(folder: int, name: str, data: bytes) -> None:
    """Write ``data`` to a temporary file next to ``name``, flush it to disk, and put it in place atomically."""
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


def _children(folder: int, prefix: str) -> list[Entry]:
    """The folders and regular files in ``folder`` (named below ``prefix``), sorted as walk() lists them.

    Only names the jail accepts: never temporary files, and no name put there from outside (with a line break,
    say) reaches a listing, the brief or the owner's view.
    """
    with os.scandir(folder) as scan:
        names = [child.name for child in scan if NAME.fullmatch(child.name)]
    entries = []
    for name in names:
        try:
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            continue  # deleted since the folder was read: the agent works while the owner looks
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISDIR(info.st_mode):
            entries.append(Entry(relative, 0, True, info.st_mtime))
        elif stat.S_ISREG(info.st_mode):  # never links, FIFOs or devices
            entries.append(Entry(relative, info.st_size, False, info.st_mtime))
    # A folder sorts as "name/": walked depth first, every path then comes in order.
    return sorted(entries, key=lambda e: e.path + "/" if e.is_dir else e.path)


def _below(folder: int, prefix: str, depth: int) -> Iterator[Entry]:
    """The entries in ``folder`` (``depth`` parts long) and, depth first, everything below them."""
    for entry in _children(folder, prefix):
        yield entry
        if not entry.is_dir or depth >= MAX_DEPTH:
            continue
        try:
            child = _open_dir(entry.path.rpartition("/")[2], folder)
        except (OSError, SandboxError):
            continue  # deleted, or swapped for a link or a file, since it was listed
        try:
            yield from _below(child, entry.path, depth + 1)
        finally:
            os.close(child)


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
    return "the file can't be opened"
