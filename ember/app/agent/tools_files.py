"""0.42.2 (analysis-0.40.1, 5.6): the tools of the workspace's files, the memory and the library, split out of tools.py.

workspace_list, workspace_read and workspace_write (with the versions kept for restore: ``keep_version``, and
``prune_versions``, which reads VERSIONS_MAX_BYTES through tools, where tests patch it), memory_read and
memory_update, knowledge_search and library_read (the owner's library), the making tools (make_document,
make_spreadsheet, make_image, resize_image, make_cost_statement), look, and guide (``guide_text``, the manuals'
numbers filled in).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import paths
from ..economy.clock import from_iso, to_iso
from ..integrations import bluesky, etsy, kdp, pinterest, printify, qa
from ..products import blog, checks, images, make, pdf, sheets, site
from ..version import app_version
from . import learning, library
from .memory import CAPS
from .sandbox import Jail, SandboxError, kind_of
from .store import AgentScope
from .tools_context import Outcome, ToolContext, ToolError, _local, _unstop, wrap
from .tools_limits import FOLDER_SHOWN, LOOK_PIXELS, MAX_RESULT_CHARS, READ_DEFAULT_CHARS, READ_ROOM
from .tools_specs import SPECS

# --- handlers ---


def _workspace_list(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    limits = ctx.workspace.limits
    path = args.get("path")
    if path:
        # 0.40.1 (analysis-0.40.0, 3.2): a folder's subfolders, then its files the newest first with their time,
        # FOLDER_SHOWN at most. Its first 100 by name took 4 KB a listing: four listings of books/ (185 files,
        # versions v3 to v22) took 16 KB of a cycle's conversation, and still didn't say which file was the newest.
        found = ctx.workspace.listing(path)
        entries = [e for e in found if e.is_dir] + sorted((e for e in found if not e.is_dir), key=lambda e: -e.modified)
        shown = entries[:FOLDER_SHOWN]
        lines = [f"{e.path}/" if e.is_dir else f"{e.path}  {e.size:,} B  {_mtime(ctx, e.modified)}" for e in shown]
        more = f"\n… {len(entries) - len(shown)} older" if len(entries) > len(shown) else ""
    else:
        # Without a path, the files in every folder (their paths name the folders).
        entries = ctx.workspace.walk(limits.max_entries).files
        lines = [f"{e.path}/" if e.is_dir else f"{e.path}  {e.size:,} B" for e in entries[:100]]
        more = f"\n… {len(entries) - 100} more" if len(entries) > 100 else ""
    used = ctx.workspace.usage()
    text_bytes, product_bytes = ctx.workspace.sizes()
    usage = (
        f"Using {text_bytes / 1024:.1f} KB of {limits.max_total_bytes // (1024 * 1024)} MB and "
        f"{used.files:,} of {limits.max_files:,} files (in {used.folders} folder{'s' if used.folders != 1 else ''})"
    )
    if product_bytes:
        usage += (
            f"; PDF, Word, Excel and PNG files use {product_bytes / (1024 * 1024):.1f} of "
            f"{limits.max_product_total_bytes // (1024 * 1024):,} MB"
        )
    body = "\n".join(lines) if lines else "(empty)"
    return Outcome(True, f"{body}{more}\n{usage}", f"{len(entries)} entries")


def _mtime(ctx: ToolContext, modified: float) -> str:
    """When a file was last written, in the owner's time ('10-18 01:11')."""
    return datetime.fromtimestamp(modified, ctx.clock.tz).strftime("%m-%d %H:%M") if modified else "-"


def _workspace_read(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    what = ""
    if kind_of(args["path"]) == "product":
        data = ctx.workspace.read_bytes(args["path"])
        text = _product_text(args["path"], data)
        what = f"{_product(args['path'], data)}; "
        if text is None:
            return Outcome(True, f"{args['path']} is {what[:-2]}.", f"about {args['path']}")
        what += "its text, " if text.strip() else "no text in it, "
    else:
        text = ctx.workspace.read(args["path"])
    offset = args.get("offset", 0)
    size = min(
        args.get("max_chars", READ_DEFAULT_CHARS), MAX_RESULT_CHARS - READ_ROOM - 2 * len(args["path"]) - len(what)
    )
    part = text[offset : offset + size]
    end = offset + len(part)
    more = f"\nMore from offset {end}." if end < len(text) else ""
    header = f"{args['path']} ({what}characters {offset:,}–{end:,} of {len(text):,})"
    return Outcome(True, f"{header}\n{wrap(ctx, 'workspace:' + args['path'], part)}{more}", f"read {args['path']}")


def _product_text(path: str, data: bytes) -> str | None:
    """0.15.0: what a PDF, Word or Excel file says (an Excel file's cells, sheet by sheet), read by Ember's code within
    bounds; None for a picture or a presentation. The workshop was paid to read them."""
    kind = path.rsplit(".", 1)[-1].lower()
    try:
        if kind == "xlsx":
            return sheets.workbook_text(data)
        if kind in ("pdf", "docx"):
            return library.pdf_text(data) if kind == "pdf" else library.word_text(data)
    except (library.LibraryError, sheets.SheetError, checks.Refused) as exc:
        raise ToolError(f"{path} can't be read: {_unstop(str(exc))}") from None
    return None


def _product(path: str, data: bytes) -> str:
    """What a product file is: its kind, size and pages or pixels."""
    kind = path.rsplit(".", 1)[-1].lower()
    size = f"{len(data) / 1024:,.0f} KB"
    if kind == "pdf":
        pages = images.page_count(data)
        return f"a PDF with {pages} page{'s' if pages != 1 else ''}, {size}"
    if kind in ("png", "jpg"):
        try:
            width, height = images.png_size(data)
        except images.ImageError as exc:  # 0.15.0: it said "the tool failed"
            raise ToolError(f"{path} can't be read: {exc}") from None
        return f"a {kind.upper()} picture, {width} x {height} pixels, {size} (use look to see it)"
    names = {"docx": "a Word document", "xlsx": "an Excel workbook", "pptx": "a PowerPoint presentation"}
    return f"{names[kind]}, {size}"


def _workspace_write(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    path, mode = args["path"], args["mode"]
    if mode == "restore":
        return _restore(ctx, conn, path)
    if args.get("count") is not None and mode != "edit":
        raise ToolError("count is for mode edit: how often find's passage is there")
    kept = _keep_version(ctx, conn, path, mode)  # 0.33.0: what it held, before it changes
    if mode == "delete":
        ctx.workspace.delete(path)
        return Outcome(True, f"Deleted {path}.{_restorable(kept, VERSIONS_GONE_DAYS)}", f"deleted {path}")
    content = args.get("content")
    if not content:
        raise ToolError("content is required unless mode is delete or restore")
    if mode == "copy":
        return _copy(ctx, content.strip(), path)
    if mode == "edit":
        return _edit(ctx, path, args.get("find") or "", content, args.get("count"))
    if args.get("find"):
        raise ToolError("find is for mode edit: the passage it replaces")
    size = ctx.workspace.write(path, content, append=mode == "append", create_only=mode == "create")
    total = ctx.workspace.sizes()[0]
    return Outcome(
        True,
        f"Wrote {path} ({size:,} bytes). Using {total / (1024 * 1024):.2f} of "
        f"{ctx.workspace.limits.max_total_bytes // (1024 * 1024)} MB.{_restorable(kept)}",
        f"{mode} {path}",
    )


# 0.33.0: the earlier texts of each file Ember's code keeps (migration 0086), the newest first
VERSIONS_KEPT = 5
CHANGES = ("overwrite", "edit", "copy", "delete")


def _keep_version(ctx: ToolContext, conn: Any, path: str, reason: str) -> bool:
    """0.33.0: the text a workspace file holds, kept before workspace_write changes it (``reason``: overwrite, edit,
    copy, delete or restore), so restore can bring it back; the newest VERSIONS_KEPT of each file. False when there is
    nothing to keep (no such text file yet, or a change that loses nothing: create, append)."""
    return keep_version(conn, ctx.scope, ctx.workspace, path, reason, ctx.cycle_id, ctx.now())


def keep_version(
    conn: Any, scope: AgentScope, jail: Jail, path: str, reason: str, cycle_id: int | None, now: str
) -> bool:
    """_keep_version for any writer of the workspace. 0.40.3 (analysis-0.40.1 codebase, T2): draft's overwrite and
    a workshop run's file of an existing text's name replace it too, and kept nothing: a cut-off 14-byte draft
    replaced a 22 KB manuscript, and restore had nothing to bring back (cycle #134's loss, through another tool)."""
    if reason not in (*CHANGES, "restore") or kind_of(path) != "text" or not jail.exists(path):
        return False
    where, params = scope.where()
    conn.execute(
        "INSERT INTO workspace_versions (mode, session, path, content, reason, cycle_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*params, path, jail.read(path), reason, cycle_id or None, now),
    )
    conn.execute(
        f"DELETE FROM workspace_versions WHERE {where} AND path = ? AND id NOT IN (SELECT id FROM workspace_versions"
        f" WHERE {where} AND path = ? ORDER BY id DESC LIMIT ?)",
        (*params, path, *params, path, VERSIONS_KEPT),
    )
    return True


# 0.42.0 (analysis-0.40.1 codebase, 6.2 disk): the kept texts of a file that is gone (deleted, or renamed: a copy and a
# delete; or a past dry-run session's) stay this many days after the newest of them, so restore brings back a file
# deleted lately; then Ember's code prunes them (once a day, scheduler.py). They were kept for good, and versioned names
# (-v3 ... -v22) multiplied them. All kept texts together take at most VERSIONS_MAX_BYTES, the oldest going first.
VERSIONS_GONE_DAYS = 14
VERSIONS_MAX_BYTES = 50 * 1024 * 1024


def prune_versions(conn: Any, workspaces: Mapping[tuple[str, int], Jail], now: datetime) -> int:
    """0.42.0: delete the kept texts of the files that are gone, VERSIONS_GONE_DAYS after their newest, and the oldest
    beyond VERSIONS_MAX_BYTES; returns how many were deleted. ``workspaces``: the folder of each (mode, session) whose
    files still exist (the live workspace, the dry-run session's): any other's are gone."""
    from .tools import VERSIONS_MAX_BYTES  # 0.42.2: read through tools, where tests patch it

    stale = conn.execute(
        "SELECT mode, session, path FROM workspace_versions GROUP BY mode, session, path HAVING MAX(created_at) < ?",
        (to_iso(now - timedelta(days=VERSIONS_GONE_DAYS)),),
    ).fetchall()
    deleted = 0
    for mode, session, path in stale:
        if not _still_there(workspaces.get((mode, session)), path):
            deleted += conn.execute(
                "DELETE FROM workspace_versions WHERE mode = ? AND session = ? AND path = ?", (mode, session, path)
            ).rowcount
    deleted += conn.execute(
        "DELETE FROM workspace_versions WHERE id IN (SELECT id FROM (SELECT id, SUM(length(CAST(content AS BLOB)))"
        " OVER (ORDER BY id DESC) AS kept FROM workspace_versions) WHERE kept > ?)",
        (VERSIONS_MAX_BYTES,),
    ).rowcount
    return deleted


def _still_there(jail: Jail | None, path: str) -> bool:
    try:
        return jail is not None and jail.size_of(path, "text") is not None
    except SandboxError:  # a folder of that name now, or a link: no text of it to restore
        return False


def _restorable(kept: bool, days: int | None = None) -> str:
    """0.42.0: ``days``, for a deleted file: how long its text is kept (VERSIONS_GONE_DAYS)."""
    if not kept:
        return ""
    return f" Its text before is kept{f' for {days} days' if days else ''}: restore brings it back."


def _restore(ctx: ToolContext, conn: Any, path: str) -> Outcome:
    """0.33.0: a text file as it was before workspace_write last changed it; what it held until now is kept in turn,
    so a second restore undoes the first."""
    where, params = ctx.scope.where()
    row = conn.execute(
        f"SELECT * FROM workspace_versions WHERE {where} AND path = ? ORDER BY id DESC LIMIT 1", (*params, path)
    ).fetchone()
    if row is None:
        raise ToolError(
            f"no earlier text of {path} is kept: only what workspace_write overwrote, edited or deleted, or what draft"
            " or a workshop run replaced"  # 0.40.3 (analysis-0.40.1 codebase, T2)
        )
    _keep_version(ctx, conn, path, "restore")
    size = ctx.workspace.write(path, row["content"])
    when = str(row["created_at"])[:16].replace("T", " ")
    return Outcome(
        True,
        f"Restored {path} ({size:,} bytes) as it was before the {row['reason']} of {when} UTC"
        f" (cycle #{row['cycle_id']}). A second restore undoes this one.",
        f"restore {path}",
    )


def _edit(ctx: ToolContext, path: str, find: str, content: str, expected: int | None = None) -> Outcome:
    """0.32.0: one passage of a text file replaced, the rest kept as it is: a small fix of a long file is one small
    call (it took rewriting the file in parts of WRITE_CHARS). 0.33.0: ``expected`` (count) times, each replaced: one
    call changes a passage repeated through a file (live, a book's 48 weekly tables)."""
    if not find:
        raise ToolError("edit needs find: the passage to replace, exactly as the file holds it")
    text = ctx.workspace.read(path)
    count = text.count(find)
    if count == 0:
        loose = " ".join(find.split())
        near = loose and loose in " ".join(text.split())
        raise ToolError(
            f"{path} doesn't hold find's passage"
            + (": it does with other spaces or line breaks, so copy them as workspace_read shows them" if near else "")
        )
    if expected is not None and count != expected:
        raise ToolError(f"find's passage is in {path} {count} times, not {expected}: nothing was changed")
    if count > 1 and expected is None:
        raise ToolError(
            f"find's passage is in {path} {count} times: give more of it around, so it is there once, or count"
            f" {count} to change each"
        )
    size = ctx.workspace.write(path, text.replace(find, content))
    times = "one passage" if count == 1 else f"{count} passages"
    return Outcome(True, f"Edited {path}: {times} replaced ({size:,} bytes now).{_restorable(True)}", f"edit {path}")


def _copy(ctx: ToolContext, source: str, path: str) -> Outcome:
    """0.15.0: a copy of a workspace file (the workshop was paid to copy one into shop/). A product file stays the
    kind it is: its bytes are Ember's code's or were checked when the workshop made them."""
    if source.rsplit(".", 1)[-1].lower() != path.rsplit(".", 1)[-1].lower():
        raise ToolError("a copy keeps its file ending")
    replaced = " It replaced the file that was there." if ctx.workspace.size_of(path) is not None else ""
    if kind_of(source) == "product":
        size = ctx.workspace.write_bytes(path, ctx.workspace.read_bytes(source))
    else:
        size = ctx.workspace.write(path, ctx.workspace.read(source))
    return Outcome(True, f"Copied {source} to {path} ({size:,} bytes).{replaced}", f"copied {source} to {path}")


def _memory_update(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    seen = ctx.state.read_memory.get("lessons")
    if args["file"] == "lessons" and args["mode"] == "replace" and seen in (None, ctx.state.reply):
        # 0.12.0: rewrites ordered from what the plan showed (9 of 44 lessons) dropped rules the owner relied on.
        kept = [line for line in args["content"].splitlines() if line.strip()]
        known = [line for line in ctx.memory.read("lessons").splitlines() if line.strip()]
        if len(kept) * 2 < len(known):
            why = "you haven't read it whole in this cycle" if seen is None else "you haven't seen it yet"
            raise ToolError(
                f"this keeps {len(kept)} of the {len(known)} lines of lessons.md, and {why}: read it with memory_read "
                "(free) while working, then replace it in a later reply of the same cycle, keeping what still helps"
            )
    text = ctx.memory.update(conn, args["file"], args["mode"], args["content"], ctx.cycle_id, ctx.now(), SPECS)
    return Outcome(True, text, f"{args['mode']} {args['file']}")


def _memory_read(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.12.0: a memory file whole, for the agent to rewrite it knowing what is in it."""
    name = args["file"]
    text = ctx.memory.read(name)
    ctx.state.read_memory[name] = ctx.state.reply
    size = len(text.encode("utf-8"))
    body = wrap(ctx, "memory", text.rstrip("\n")) if text.strip() else "(empty)"
    return Outcome(True, f"{name}.md, {size:,} of {CAPS[name]:,} bytes, whole:\n{body}", f"read {name}")


def _knowledge_search(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.12.0: what the agent learned from its owner's library that matches, then the best passages of the texts."""
    query = " ".join(args["query"].split())
    words = library.terms(query)
    if not words:
        raise ToolError("query: give words to look for (not only short or common ones)")
    found = library.search_learnings(conn, ctx.scope, words, limit=10)
    passages = library.search_parts(conn, ctx.scope, words, limit=3)
    own = learning.relevant(conn, ctx.scope, query, limit=5)  # 0.18.0: your own principles and cases first
    quoted = json.dumps(query, ensure_ascii=False)
    if not found and not passages and not own:
        return Outcome(
            True,
            f"Nothing in your owner's library matches {quoted}. Try other words, or library_read for its documents.",
            f"nothing for {query[:60]}",
        )
    text = []
    if own:
        text.append("From your own playbook and cases:\n" + "\n".join(own))
    if found:
        lines = "\n".join(library.learning_line(r) for r in found)
        text.append(f"What you learned ({len(found)}, the best match first):\n{wrap(ctx, 'library', lines)}")
    if passages:
        lines = "\n".join(
            f"#{p.document_id}.{p.part} {json.dumps(p.title, ensure_ascii=False)} (part {p.part} of {p.parts}): "
            f"{p.snippet}"
            for p in passages
        )
        text.append(f"Passages:\n{wrap(ctx, 'library', lines)}")
    text.append("#3.2 is document 3, part 2: library_read shows a whole part.")
    return Outcome(True, "\n".join(text), f"{len(found)} learnings, {len(passages)} passages for {query[:60]}")


def _library_read(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.12.0: the owner's library: its documents, or one part of one."""
    document_id = args.get("document_id")
    if document_id is None:
        rows = library.documents(conn, ctx.scope)
        counts = library.learning_counts(conn, ctx.scope)
        lines = []
        for r in rows[:40]:
            state = {"done": f"{counts.get(r['id'], 0)} learnings", "waiting": "not studied yet"}.get(
                r["study"], "not studied"
            )
            if r["study"] == "done" and r["study_note"]:  # 0.15.0: its learnings were full before its last part
                state += f" · {r['study_note']}"
            links = "".join(
                f" · {name} #{r[f'{name}_id']}" for name in ("venture", "project") if r[f"{name}_id"] is not None
            )
            note = f" · your owner's note: {json.dumps(r['note'], ensure_ascii=False)}" if r["note"] else ""
            lines.append(
                f"#{r['id']} {json.dumps(r['title'], ensure_ascii=False)} · {r['parts']} part"
                f"{'s' if r['parts'] != 1 else ''}, {r['chars']:,} characters · {state}{links}{note}"
            )
        if len(rows) > 40:
            lines.append(f"(and {len(rows) - 40} older documents)")
        return Outcome(
            True,
            "Your owner's library, the newest first:\n" + "\n".join(lines)
            if rows
            else "Your owner's library is empty.",
            f"{len(rows)} documents",
        )
    row = library.get(conn, ctx.scope, document_id)
    if row is None or row["removed_at"] is not None:
        raise ToolError(f"there is no document #{document_id} in your owner's library")
    part = args.get("part") or 1
    if part > row["parts"]:
        raise ToolError(f"document #{document_id} has {row['parts']} part{'s' if row['parts'] != 1 else ''}")
    body = library.part_text(conn, document_id, part) or ""
    head = f"Document #{document_id} {json.dumps(row['title'], ensure_ascii=False)}, part {part} of {row['parts']}"
    if row["source"]:
        head += f" (source: {json.dumps(row['source'], ensure_ascii=False)})"
    if row["note"]:
        head += f"\nYour owner's note on it: {json.dumps(row['note'], ensure_ascii=False)}"
    after = f"\nNext: part {part + 1}." if part < row["parts"] else ""
    return Outcome(
        True, f"{head}\n{wrap(ctx, 'library', body)}{after}", f"#{document_id} part {part} of {row['parts']}"
    )


# --- making files (0.6.0) ---


def _made(made: make.Made, what: str) -> Outcome:
    return Outcome(True, made.text(), f"made {', '.join(made.paths[:3])}"[:300] if made.paths else what)


def _make_document(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    made = make.document(
        ctx.workspace,
        args["source"],
        args["output"],
        word_copy=args.get("word", True),
        previews=args.get("pictures", True),
        pages=args.get("pages") or "",  # 0.39.1
    )
    _made_pdf(ctx, args["source"], args["output"])
    return _made(made, "made a document")


def _made_pdf(ctx: ToolContext, source: str, output: str) -> None:
    """0.40.1 (analysis-0.40.0, 3.2): which file a PDF was made from, and that file's text then (made_pdfs), for a KDP
    check (_made_from). In the database, not in the PDF: Ember's files go to buyers and to KDP as they are."""
    pdf = hashlib.sha256(ctx.workspace.read_bytes(output)).hexdigest()
    text = hashlib.sha256(ctx.workspace.read(source).encode()).hexdigest()
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO made_pdfs (mode, session, path, sha256, source, source_sha256, cycle_id, made_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ctx.scope.mode, ctx.scope.session, output, pdf, source, text, ctx.cycle_id, ctx.now()),
        )


def _make_spreadsheet(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    return _made(make.spreadsheet(ctx.workspace, args["source"], args["output"]), "made a spreadsheet")


def _make_image(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    made = make.image(
        ctx.workspace,
        args["output"],
        args.get("pages") or "",
        # 0.18.1: an HTML entity in the text is drawn as the character (live, "Word &amp;" stood on a cover)
        html.unescape(args["title"]),
        html.unescape(args.get("subtitle", "")),
        html.unescape(args.get("badge", "")),
        args.get("background"),
        args.get("accent"),
        args.get("shape", "landscape"),
        # 0.38.0: a table is drawn by layout table, which it needs no word for
        args.get("layout") or ("table" if args.get("table") else "photo"),
        html.unescape(args.get("table", "")),
    )
    return _made(made, "made a listing photo")


def _resize_image(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    """0.17.0: upgrade request #4, the workshop's resize script built in."""
    made = make.resize(ctx.workspace, args["source"], args["output"], args["width"], args["height"])
    return _made(made, "made a print file")


def _make_cost_statement(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    """0.20.0: upgrade request #7, the workshop's Nebenkostenabrechnung script built in."""
    return _made(make.cost_statement(ctx.workspace, args["source"], args["output"]), "made a cost statement")


def _look(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    path = args["path"]
    if not path.lower().endswith((".png", ".jpg")):
        raise ToolError("look shows .png and .jpg pictures; make_document and make_spreadsheet make them for you")
    try:
        picture, width, height = images.thumbnail(ctx.workspace.read_bytes(path), LOOK_PIXELS)
    except images.ImageError as exc:
        raise ToolError(f"{path} can't be shown: {exc}") from None
    made = _picture_made(ctx, conn, path)
    return Outcome(True, f"{path} ({width} x {height} pixels{made}), shown here:", f"looked at {path}", image=picture)


def _picture_made(ctx: ToolContext, conn: Any, path: str) -> str:
    """0.39.2: when a picture was made, and that it was before the running version's first cycle when it was: live,
    the first cycle under 0.39.1 judged its renderer by pictures of pages 0.38.0 had drawn two days before, took their
    2.2 mm rows for the new renderer's and rebuilt the book on that (analysis-0.39.1, 3.1). Only Ember's own tools and
    its workshop write pictures into the workspace."""
    modified = ctx.workspace.modified(path)
    if modified is None:
        return ""
    made = datetime.fromtimestamp(modified, UTC)
    text = f", made {_local(ctx, to_iso(made))}"
    running = app_version()
    sql, params = "SELECT MIN(started_at) FROM cycles WHERE app_version = ?", (running[:40],)
    if conn is None:
        with ctx.db.connection() as own:
            first = own.execute(sql, params).fetchone()[0]
    else:
        first = conn.execute(sql, params).fetchone()[0]
    if first is not None and made < from_iso(first):
        text += f", before version {running} ran: it shows what Ember's tools made then"
    return text


def _guide(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    return Outcome(True, guide_text(args["topic"]), f"read the {args['topic']} guide")


def guide_text(topic: str) -> str:
    """A guide, with the numbers Ember's code keeps filled in (0.13.0: the QA registry's photos). 0.32.0: "{CAP:tool}"
    is how often a cycle may use the tool (live, the agent met those of make_spreadsheet, propose_pin and
    propose_bluesky_post only in their refusals, mid-plan; the fixed prompt had no room to say them)."""
    text = (paths.APP_DIR / "agent" / "guides" / f"{topic}.md").read_text(encoding="utf-8").strip()
    text = re.sub(r"\{CAP:([a-z_]+)\}", lambda found: str(SPECS[found[1]].per_cycle), text)
    return (
        text.replace("{MIN_PHOTOS}", str(qa.MIN_PHOTOS))
        .replace("{SHARP_DPI}", str(qa.SHARP_DPI))
        .replace("{MAX_MP}", str(images.MAX_PIXELS // 1_000_000))
        .replace("{MIN_MARGIN}", f"{printify.MIN_MARGIN * 100:.0f}")
        .replace("{MAX_VARIANTS}", str(printify.MAX_VARIANTS))
        .replace("{MAX_PHOTOS}", str(etsy.MAX_PHOTOS))
        .replace("{REPLY_WORDS}", str(qa.REPLY_WORDS))
        .replace("{PIN_TITLE}", str(pinterest.TITLE_MAX))
        .replace("{PIN_DESCRIPTION}", str(pinterest.DESCRIPTION_CHARS))
        .replace("{POST_CHARS}", str(bluesky.TEXT_MAX))
        .replace("{WORDS_EN}", str(bluesky.WORDS_CHARS["en"]))  # 0.32.0
        .replace("{WORDS_DE}", str(bluesky.WORDS_CHARS["de"]))
        .replace("{POST_TAGS}", str(qa.POST_TAGS))
        .replace("{SITE_PAGES}", str(site.MAX_PAGES))
        .replace("{BLOG_BODY_MIN}", str(blog.BODY_MIN))
        .replace("{BLOG_LINKS}", str(blog.LINKS_MAX))
        .replace("{KDP_WEEKLY}", str(kdp.WEEKLY_TITLES))
        .replace("{BOOK_PAGES}", str(pdf.BOOK_PAGES))
        .replace("{KDP_LEAST}", str(kdp.LEAST_PAGES))
        .replace("{SPINE_PAGES}", str(kdp.SPINE_TEXT_PAGES))
    )


def image_block(picture: bytes) -> dict[str, Any]:
    """A PNG as an image content block for the model."""
    data = base64.b64encode(picture).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
