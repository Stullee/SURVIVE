"""The evaluation export (0.43.0, vision/evaluation.md): what an evaluation of Ember's work needs, in one zip.

An evaluation scores Ember week by week: what she spent, built, decided, learned and explored, from her records. The
diagnostics report can't carry that: it shows 12 cycles whole and the newest rows of each table, the database prunes
the model's replies and the tool calls' texts after db.TEXT_DAYS, and the products she made are in neither. The
database itself can't be shared as it is (other people's emails, the web text Ember read, the owner's messages
unmasked), so the export is shareable like the report:

* ``ember.db``: the whole database, copied with SQLite's online backup (a consistent snapshot while Ember runs). In the
  copy other people's text is left out with its length: the emails' subjects, texts and attachments, an opt-out's
  reason, an email Ember proposed, the research digests, and the owner's library with what its study drew from it.
  Every other text is masked as the shareable report masks it (diagnostics.shareable_masker: addresses, codes, link
  tokens, phone numbers, IBANs, streets, the words the owner removed, their name and user ID, the personal options),
  and what each plan saw (plan_contexts) becomes text, shareable as the report's PLANNER CONTEXT. The redaction's salt
  and the removed words' hashes are left out, the copy's triggers are dropped (its history needs no guard there) and
  it is vacuumed, so no unmasked text stays in its free pages;
* ``report.txt``: the shareable diagnostics report as the export was made (the next plan's context, the plan tree's
  ranking and waits);
* ``memory/``: her strategy, identity and lessons, masked;
* ``workspace/``: her files changed in the last PRODUCT_DAYS, the newest first up to PRODUCT_BYTES: the products as she
  made them (PDF, Word, Excel, pictures), as buyers and the platforms get them, and her text files masked;
* ``outside.csv`` and ``week.csv``: for the numbers only the owner sees (KDP's sales, the ads) and their week;
* ``manifest.json``: what is in it and how to read it, every table's rows (live and dry run), every file's size and
  SHA-256, and what was left out.

The owner makes it on the Diagnostics tab (``Exporter.start``): it is built in a thread of its own (copying and masking
a large database takes minutes on a Raspberry Pi; Ember works on meanwhile), kept in /data/exports (the newest KEEP)
and downloaded from there. Home Assistant's backups leave the folder out (config.yaml).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import diagnostics, events, paths
from .agent.sandbox import SandboxError, kind_of
from .agent.store import context_text
from .agent.tools_limits import LIBRARY_TOOLS
from .db import TEXT_DAYS, utcnow
from .economy.metering import RESEARCH, STUDY
from .logging_setup import redact
from .version import app_version, build_id

if TYPE_CHECKING:
    from .state import AppState

log = logging.getLogger(__name__)

KEEP = 2  # exports kept in /data/exports, the newest
PRODUCT_DAYS = 35  # her files changed in these days go in: the week evaluated and the four before it
PRODUCT_BYTES = 300 * 1024 * 1024  # of them, the newest first
WORKSPACE_ENTRIES = 10_000  # the most a walk of her workspace meets (diagnostics.WORKSPACE_ALL)
BATCH = 500  # rows masked at a time, each batch its own transaction
NAME = re.compile(r"ember-evaluation-\d{8}T\d{6}Z(?:-[0-9A-Za-z.+_-]{1,40})?\.zip")
# The columns whose text is other people's or the owner's library's: only their length goes in.
LEFT_OUT = {
    "emails": ("subject", "body", "attachments"),
    "library_parts": ("text",),
    "library_documents": ("summary", "note"),
    "learnings": ("topic", "text"),
}
OUTSIDE_CSV = "source,item,metric,value,from,to,note\n"
WEEK_CSV = "week_from,week_to,minutes_on_ember,instructions_given,feeling_1_to_5,why\n"
HOW_TO_READ = (
    "ember.db is Ember's database at the schema version in `schema`, masked for sharing: other people's text is left"
    " out with its length ('[1,234 characters left out]'), addresses are '[email N]' (the same address, the same N in"
    " ember.db, memory/ and workspace/; report.txt numbers them on its own), and the owner's name, user ID and"
    " personal options are masked.",
    "Live rows only: tables with a mode column, mode = 'live'; cycles, llm_calls and what hangs off them,"
    " simulated = 0 (a dry run's are kept, simulated = 1).",
    "Older than db.TEXT_DAYS (`texts_since`), the model's replies (call_texts.text) and the tool calls' inputs and"
    " results read '[pruned]' and '{}', and plan_contexts.context is NULL: their costs, tokens, purposes, statuses"
    " and lengths stay.",
    "plan_contexts.context holds what each cycle's plan saw, as text here (zlib in Ember's own database), with the"
    " sections of mail, research and the library left out as in the report's PLANNER CONTEXT.",
    "Times end in Z (UTC); the owner's days (day, due, local_day) are Berlin's.",
    "The copy's triggers are dropped and its CHECK constraints were not enforced while it was masked: a masked text"
    " may be longer than its column allows in Ember.",
    "outside.csv and week.csv are for the owner to fill in: the numbers Ember can't see (KDP's units and royalties,"
    " the ads' impressions and clicks, the Anthropic Console's spend) and their week (minutes, instructions given,"
    " how it felt).",
)


class ExportError(ValueError):
    """An export that can't be made now; the message is for the owner."""


def exports_dir() -> Path:
    return paths.data_dir() / "exports"


def build(state: AppState, progress: Callable[[str], None] | None = None) -> Path:
    """Makes an export in exports_dir() and returns its path; ``progress`` hears each step's name. Raises ExportError
    (no database) and anything the copy, the masking or the disk raise; a failed export leaves nothing behind."""
    if state.db_error is not None:
        raise ExportError(f"the database is unavailable ({state.db_error})")
    say = progress or (lambda step: None)
    folder = exports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _tidy(folder, KEEP - 1)
    now = datetime.now(UTC)
    version = re.sub(r"[^0-9A-Za-z.+_-]", "", app_version())[:40]
    name = f"ember-evaluation-{now.strftime('%Y%m%dT%H%M%SZ')}" + (f"-{version}" if version else "") + ".zip"
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=folder))
    try:
        say("copying the database")
        copy = work / "ember.db"
        _copy_database(state.db.path, copy)
        say("masking the database")
        mask = _mask_fn(state)
        tables = mask_database(copy, mask)
        say("writing the report")
        files: dict[str, bytes] = {"report.txt": diagnostics.report(state).encode()}
        agent = state.agent
        left_out: list[dict[str, Any]] = []
        if agent is not None:
            say("collecting her memory and files")
            for key, text in agent.memory_files().items():
                files[f"memory/{key}.md"] = mask(text).encode()
            workspace, _ = agent.roots()
            since = (now - timedelta(days=PRODUCT_DAYS)).timestamp()
            for path, data in _workspace_files(workspace, since, mask, left_out):
                files[f"workspace/{path}"] = data
        files["outside.csv"] = OUTSIDE_CSV.encode()
        files["week.csv"] = WEEK_CSV.encode()
        say("packing the zip")
        manifest = _manifest(state, now, copy, tables, files, left_out, agent is not None)
        part = folder / f".{name}.part"
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            zipped.writestr("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False))
            zipped.write(copy, "ember.db")
            for path, data in files.items():
                zipped.writestr(path, data)
        target = folder / name
        os.replace(part, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with contextlib.suppress(OSError):
            (folder / f".{name}.part").unlink(missing_ok=True)
    _tidy(folder, KEEP)
    return target


def _copy_database(source: Path, target: Path) -> None:
    """A consistent snapshot of the database at ``source`` (one step of SQLite's online backup, on a connection of its
    own: in WAL mode Ember's writes go on meanwhile)."""
    src = sqlite3.connect(source, timeout=30)
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


def _mask_fn(state: AppState) -> Callable[[str], str]:
    """The shareable report's mask (its masker, after the log's redaction of secrets)."""
    masker = diagnostics.shareable_masker(state)
    return lambda text: masker(redact(text))


def mask_database(path: Path, mask: Callable[[str], str]) -> dict[str, dict[str, int]]:
    """Masks the database copy at ``path`` in place for sharing (see the module's docstring), and returns each table's
    rows: in all, and live and dry run where it has a mode."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = DELETE")  # one file to pack, no WAL beside it
        conn.execute("PRAGMA secure_delete = ON")  # a masked text's old bytes are overwritten, not left in free pages
        conn.execute("PRAGMA ignore_check_constraints = ON")  # '[email 1]' can be longer than an address it masks
        conn.execute("PRAGMA foreign_keys = OFF")
        for (trigger,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            conn.execute(f'DROP TRIGGER "{trigger}"')
        conn.execute("DELETE FROM meta WHERE key LIKE 'secret.%'")  # the redaction's salt
        conn.execute("DELETE FROM redactions")  # the removed words' salted hashes
        purposes = {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, purpose FROM llm_calls")}
        names = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in names:
            _mask_table(conn, table, mask, purposes)
        conn.execute("VACUUM")
        return {table: _counts(conn, table) for table in names}
    finally:
        conn.close()


def _mask_table(conn: sqlite3.Connection, table: str, mask: Callable[[str], str], purposes: dict[int, str]) -> None:
    """Every row of ``table`` with its texts shared as _shared says, a batch of rows a transaction."""
    keys = _keys(conn, table)
    for batch in _batches(conn, table, keys):
        conn.execute("BEGIN")
        for row in batch:
            changed = {
                column: value for column, value in _shared(table, row, mask, purposes).items() if value != row[column]
            }
            if changed:
                _update(conn, table, keys, row, changed)
        conn.execute("COMMIT")


def _keys(conn: sqlite3.Connection, table: str) -> list[str]:
    """What names a row of ``table``: its rowid, or a WITHOUT ROWID table's primary key."""
    try:
        conn.execute(f'SELECT rowid FROM "{table}" LIMIT 1')
    except sqlite3.OperationalError:
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [str(c["name"]) for c in sorted((c for c in info if c["pk"]), key=lambda c: c["pk"])]
    return ["rowid"]


def _batches(conn: sqlite3.Connection, table: str, keys: list[str]) -> Iterator[list[dict[str, Any]]]:
    """The rows of ``table``, BATCH at a time, each with its key (``_rowid`` for a rowid table)."""
    if keys != ["rowid"]:  # a WITHOUT ROWID table (the library's parts, the workspace's files): few rows
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
        for start in range(0, len(rows), BATCH):
            yield rows[start : start + BATCH]
        return
    last = None
    while True:
        query = f'SELECT rowid AS _rowid, * FROM "{table}"'
        rows = [
            dict(r)
            for r in conn.execute(
                query + (" WHERE rowid > ?" if last is not None else "") + " ORDER BY rowid LIMIT ?",
                (last, BATCH) if last is not None else (BATCH,),
            )
        ]
        if not rows:
            return
        yield rows
        last = rows[-1]["_rowid"]


def _shared(table: str, row: dict[str, Any], mask: Callable[[str], str], purposes: dict[int, str]) -> dict[str, Any]:
    """A row's texts as the export shares them: other people's left out with its length, the rest masked."""
    shown = {column: value for column, value in row.items() if column != "_rowid"}
    if table == "approvals":
        shown = {**shown, **diagnostics.shareable_approval(shown)}  # an email to someone: its subject and text out
    elif table == "tool_calls" and isinstance(shown.get("input"), str):
        shown["input"] = diagnostics.shareable_input(str(shown["tool"]), shown["input"])
        if shown["tool"] in LIBRARY_TOOLS and isinstance(shown.get("result"), str):
            shown["result"] = f"[{len(shown['result']):,} characters from the owner's library left out]"
    elif table == "call_texts" and isinstance(shown.get("text"), str):
        purpose = purposes.get(int(shown["llm_call_id"]))
        if purpose == RESEARCH:
            shown["text"] = f"[{len(shown['text']):,} characters of web text left out]"
        elif purpose == STUDY:
            shown["text"] = f"[{len(shown['text']):,} characters from the owner's library left out]"
    elif table == "email_suppressions":
        shown["reason"] = diagnostics.shareable_reason(shown.get("reason"))
    elif table == "plan_contexts" and shown.get("context") is not None:
        shown["context"] = diagnostics.shareable_context(context_text(shown["context"]) or "")
    for column in LEFT_OUT.get(table, ()):
        value = shown.get(column)
        if isinstance(value, (str, bytes)) and value:
            shown[column] = _left_out(value)
    return {column: _masked(value, mask) for column, value in shown.items()}


def _left_out(value: str | bytes) -> str:
    """Other people's text as its length; a JSON text (an email's attachments) stays JSON, as its column checks."""
    shown = f"[{len(value):,} characters left out]"
    try:
        json.loads(value)
    except ValueError:
        return shown
    return json.dumps(shown)


def _masked(value: Any, mask: Callable[[str], str]) -> Any:
    """A cell masked: a text, or the text a blob holds (a website's file); a blob that isn't text is left out with its
    size."""
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return f"[{len(value):,} bytes left out]"
    return mask(value) if isinstance(value, str) and value else value


def _update(
    conn: sqlite3.Connection, table: str, keys: list[str], row: dict[str, Any], changed: dict[str, Any]
) -> None:
    """Writes a row's masked texts. Two texts masked alike in a UNIQUE column ('[email 1]' for one address in two
    spellings) would collide: each then keeps its row's number."""
    where = "rowid = ?" if keys == ["rowid"] else " AND ".join(f'"{k}" IS ?' for k in keys)
    names = list(changed)
    sets = ", ".join(f'"{c}" = ?' for c in names)
    key = [row["_rowid"]] if keys == ["rowid"] else [row[k] for k in keys]
    try:
        conn.execute(f'UPDATE "{table}" SET {sets} WHERE {where}', [changed[c] for c in names] + key)
    except sqlite3.IntegrityError:
        mark = f" (row {'/'.join(str(k) for k in key)})"
        values = [f"{changed[c]}{mark}" if isinstance(changed[c], str) else changed[c] for c in names]
        conn.execute(f'UPDATE "{table}" SET {sets} WHERE {where}', values + key)


def _counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    counts = {"rows": int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])}
    columns = {str(c["name"]) for c in conn.execute(f'PRAGMA table_info("{table}")')}
    if "mode" in columns:
        for mode, number in conn.execute(f'SELECT mode, COUNT(*) FROM "{table}" GROUP BY mode'):
            counts[str(mode)] = int(number)
    elif "simulated" in columns:
        for simulated, number in conn.execute(f'SELECT simulated, COUNT(*) FROM "{table}" GROUP BY simulated'):
            counts["dry_run" if simulated else "live"] = int(number)
    return counts


def _workspace_files(
    workspace: Any, since: float, mask: Callable[[str], str], left_out: list[dict[str, Any]]
) -> Iterator[tuple[str, bytes]]:
    """Her files changed since ``since`` (a timestamp), the newest first up to PRODUCT_BYTES: a product's bytes as
    they are, a text file masked. What doesn't go in is added to ``left_out`` with why."""
    files = [e for e in workspace.walk(WORKSPACE_ENTRIES).entries if not e.is_dir and e.modified >= since]
    room = PRODUCT_BYTES
    for entry in sorted(files, key=lambda e: (-e.modified, e.path)):
        kind = kind_of(entry.path)
        why = None
        if kind is None:
            why = "neither a text nor a product file"
        elif entry.size > room:
            why = f"the export's room for files ({paths.size_text(PRODUCT_BYTES)}) was used by newer ones"
        if why is None:
            try:
                data = mask(workspace.read(entry.path)).encode() if kind == "text" else workspace.read_bytes(entry.path)
            except SandboxError as exc:  # deleted or replaced while the export read it
                why = f"unreadable: {exc}"
            else:
                room -= entry.size
                yield entry.path, data
                continue
        left_out.append({"path": entry.path, "bytes": entry.size, "why": why})


def _manifest(
    state: AppState,
    now: datetime,
    copy: Path,
    tables: dict[str, dict[str, int]],
    files: dict[str, bytes],
    left_out: list[dict[str, Any]],
    agent: bool,
) -> dict[str, Any]:
    """What the export holds and how to read it."""
    digest = hashlib.sha256()
    with copy.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    listed = [{"path": "ember.db", "bytes": copy.stat().st_size, "sha256": digest.hexdigest()}]
    listed += [{"path": p, "bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()} for p, d in files.items()]
    schema = sqlite3.connect(copy)
    try:
        version = int(schema.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
    finally:
        schema.close()
    return {
        "what": "Ember's evaluation export (vision/evaluation.md in Ember's repository)",
        "notice": diagnostics.NOT_INSTRUCTIONS,
        "made_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": app_version(),
        "build": build_id(),
        "schema": version,
        "mode": "dry_run" if state.loaded.settings.dry_run else "live",
        "texts_since": (now - timedelta(days=TEXT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files_since": (now - timedelta(days=PRODUCT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "how_to_read": list(HOW_TO_READ),
        "tables": tables,
        "files": listed,
        "files_left_out": left_out if agent else [{"path": "memory/, workspace/", "why": "the agent isn't running"}],
    }


def _tidy(folder: Path, keep: int) -> None:
    """The newest ``keep`` exports stay; older ones, and what an export cut short left, go."""
    for leftover in [*folder.glob(".*.part"), *folder.glob(".work-*")]:
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)
    made = sorted(p for p in folder.iterdir() if NAME.fullmatch(p.name))
    for old in made[: max(len(made) - keep, 0)]:
        old.unlink(missing_ok=True)


def newest() -> dict[str, Any] | None:
    """The newest export made: its name, size and when it was made, or None."""
    folder = exports_dir()
    if not folder.is_dir():
        return None
    made = sorted(p for p in folder.iterdir() if NAME.fullmatch(p.name))
    if not made:
        return None
    info = made[-1].stat()
    return {
        "name": made[-1].name,
        "bytes": info.st_size,
        "made_at": datetime.fromtimestamp(info.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def path_of(name: str) -> Path | None:
    """An export's file by its name, or None (not an export's name, or gone)."""
    if not NAME.fullmatch(name):
        return None
    path = exports_dir() / name
    return path if path.is_file() else None


def room_needed(state: AppState) -> int:
    """About the bytes an export takes while it is made: the database's copy twice (the copy and the zip) and her
    recent files once."""
    size = 0
    with contextlib.suppress(OSError):
        size = os.path.getsize(state.db.path)
    files = 0
    if state.agent is not None:
        since = (datetime.now(UTC) - timedelta(days=PRODUCT_DAYS)).timestamp()
        workspace, _ = state.agent.roots()
        files = sum(e.size for e in workspace.walk(WORKSPACE_ENTRIES).entries if not e.is_dir and e.modified >= since)
    return 2 * size + min(files, PRODUCT_BYTES) + 16 * 1024 * 1024


class Exporter:
    """The owner's exports, made one at a time in a thread of its own, and how the last one went."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.started_at: str | None = None
        self.step = ""
        self.error: str | None = None

    @property
    def building(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, state: AppState) -> None:
        """Starts an export. Raises ExportError while one is being made, without a database, or when the disk would
        be left with less than Ember keeps free for its database."""
        with self._lock:
            if self.building:
                raise ExportError("an export is being made")
            if state.db_error is not None:
                raise ExportError(f"the database is unavailable ({state.db_error})")
            needed = room_needed(state)
            try:
                free = paths.free_bytes()
            except OSError as exc:
                raise ExportError(f"the free space on the disk can't be read ({type(exc).__name__})") from None
            made = _made()
            freed = sum(p.stat().st_size for p in made[: max(len(made) - (KEEP - 1), 0)])  # the ones it deletes
            if free + freed - needed < paths.FREE_FLOOR_BYTES:
                raise ExportError(
                    f"it needs about {paths.size_text(needed)} while it is made; the device has "
                    f"{paths.size_text(free + freed)} free for it, and Ember keeps "
                    f"{paths.size_text(paths.FREE_FLOOR_BYTES)} free for its database: free some space first"
                )
            self.started_at, self.step, self.error = utcnow(), "starting", None
            self._thread = threading.Thread(target=self._run, args=(state,), name="evaluation-export", daemon=True)
            self._thread.start()

    def _run(self, state: AppState) -> None:
        try:
            made = build(state, progress=self._progress)
        except Exception as exc:  # noqa: BLE001 - reported to the owner, never raised in the thread
            log.exception("The evaluation export failed")
            self.error = f"{type(exc).__name__}: {exc}"[:300]
            with contextlib.suppress(Exception):
                events.record(state.db, "warning", "system", f"The evaluation export failed: {self.error}")
        else:
            with contextlib.suppress(Exception):
                size = made.stat().st_size
                shown = paths.size_text(size) if size >= 1024**2 else "under 1 MB"
                events.record(state.db, "info", "system", f"Evaluation export made: {made.name} ({shown})")
        finally:
            self.step = ""

    def _progress(self, step: str) -> None:
        self.step = step

    def status(self) -> dict[str, Any]:
        """For the Diagnostics tab: whether one is being made (and its step), the last one's error, the newest made."""
        return {
            "building": self.building,
            "step": self.step if self.building else "",
            "started_at": self.started_at,
            "error": None if self.building else self.error,
            "newest": newest(),
        }


def _made() -> list[Path]:
    folder = exports_dir()
    return sorted(p for p in folder.iterdir() if NAME.fullmatch(p.name)) if folder.is_dir() else []
