"""The agent's file tools in a wake cycle: workspace_read, workspace_write, workspace_edit, workspace_list,
look and restoring a file, and what they say when a file is not where it is asked for.

Tests from 0.15.0, 0.32.0, 0.33.0, 0.39.2 and 0.40.1 (formerly test_fixes_0140_factory.py, test_fixes_0320.py,
test_fixes_0330.py, test_fixes_0392.py, test_fixes_0401.py)."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agent import netguard, tools
from app.agent.fake_llm import FakeTransport
from app.products import make
from app.version import app_version
from tests.channels.test_etsy import call
from tests.cycle.test_agent import ROOMY, rows
from tests.cycle.test_loop_shapes import run
from tests.plan.test_cycle_line import lined, working
from tests.products.test_kdp import publishing
from tests.products.test_pictures import a_cv, three_sheets
from tests.products.test_product_tools import ctx_for, make_agent, png
from tests.products.test_products import CV
from tests.ventures import test_ventures

# --- From 0.15.0 (formerly test_fixes_0140_factory.py) ---


def test_workspace_read_reads_excel_word_and_pdf_files(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    ctx = ctx_for(agent)
    three_sheets(ctx.workspace)
    make.spreadsheet(ctx.workspace, "b.json", "shop/b.xlsx")
    a_cv(ctx.workspace)
    read = tools.HANDLERS["workspace_read"]
    with netguard.sealed():  # as every tool runs
        cells = read(ctx, {"path": "shop/b.xlsx", "max_chars": 6_000}, None).text
        word = read(ctx, {"path": "shop/cv.docx"}, None).text
        page = read(ctx, {"path": "shop/cv.pdf", "offset": 5, "max_chars": 20}, None).text
    assert "an Excel workbook" in cells and "Sheet 2 'Budget'" in cells and "Sheet 4 'Summary'" in cells
    assert "=SUM(B4:B8) → 1300" in cells and "=1+2 → 3" in cells
    assert "a Word document" in word and "Anna Bergmann" in word and "Müller GmbH" in word
    assert page.startswith("shop/cv.pdf (a PDF with 1 page, ") and "; its text, characters 5–25 of " in page
    assert "More from offset 25." in page


def test_workspace_write_copies_a_file_for_free(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    ctx = ctx_for(agent)
    a_cv(ctx.workspace)
    handler = tools.HANDLERS["workspace_write"]

    def write(ctx: Any, args: dict[str, Any], _: None) -> Any:
        with agent.db.transaction() as conn:  # 0.33.0: a text file it replaces keeps its text (workspace_versions)
            return handler(ctx, args, conn)

    copied = write(ctx, {"path": "shop/de/cv.pdf", "mode": "copy", "content": "shop/cv.pdf"}, None)
    assert copied.ok and copied.text.startswith("Copied shop/cv.pdf to shop/de/cv.pdf (")
    assert ctx.workspace.read_bytes("shop/de/cv.pdf") == ctx.workspace.read_bytes("shop/cv.pdf")
    again = write(ctx, {"path": "notes/cv.md", "mode": "copy", "content": "cv.md"}, None)
    assert again.ok and ctx.workspace.read("notes/cv.md") == CV
    assert (
        "It replaced the file that was there."
        in write(ctx, {"path": "notes/cv.md", "mode": "copy", "content": "cv.md"}, None).text
    )
    for source, path in (("cv.md", "shop/cv2.pdf"), ("shop/cv.pdf", "shop/cv.png")):
        with pytest.raises(tools.ToolError, match="a copy keeps its file ending"):
            write(ctx, {"path": path, "mode": "copy", "content": source}, None)


# --- From 0.32.0 (formerly test_fixes_0320.py) ---


def calling(agent: Any, tool: str, *, venture: bool = False, **args: Any) -> tools.Outcome:
    """A call of the agent's newest cycle, an ordinary one unless ``venture``."""
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
        venture=venture,
    )
    llm_call = rows(agent, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, tool, args, f"toolu_{tool}", llm_call, "act")


# --- 4. an edit changes one passage of a file, however long it is ----------------------------------------------------


def a_spec(net: str = "=B3-B4") -> str:
    """The budget planner of cycle #129, its Summary's Net as ``net`` (live: "=B3-B4", the header less the income)."""

    def table(name: str, empty: int) -> dict[str, Any]:
        columns = [{"title": "Source", "width": 22}, {"title": "Planned", "format": "usd"}]
        columns.append({"title": "Actual", "format": "usd"})
        rows = [["Salary", 2800, 2800]]
        return {"name": name, "title": f"Monthly {name}", "columns": columns, "rows": rows, "empty_rows": empty}

    summary = {
        "name": "Summary",
        "title": "Monthly Summary",
        "columns": [{"title": "Item", "width": 24}, {"title": "Amount", "format": "usd", "width": 16}],
        "rows": [
            ["Total Income", "=SUM(Income!C4:C12)"],
            ["Total Expenses", "=SUM(Expenses!C4:C26)"],
            ["Net (Income - Expenses)", net],
        ],
    }
    notes = [f"Step {i}: type your planned and actual amounts, then read the summary and the year." for i in range(30)]
    sheets = [
        {**table("Income", 8), "totals": {"Actual": "sum"}},
        {**table("Expenses", 22), "totals": {"Actual": "sum"}},
    ]
    return json.dumps({"title": "Budget Planner", "notes": notes, "sheets": [*sheets, summary]}, indent=2) + "\n"


def test_an_edit_changes_one_passage_of_a_long_file_in_one_small_call(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(script=[test_ventures.plan(steps=[])]), settings=ROOMY)
    workspace = agent.roots()[0]
    spec = a_spec()
    assert len(spec) > tools.WRITE_CHARS  # live: rewritten in parts, twice, until the cycle's writes ran out
    workspace.write("drafts/budget.json", spec)
    edit = {"path": "drafts/budget.json", "mode": "edit"}
    fixed = calling(agent, "workspace_write", **edit, find='"=B3-B4"', content='"=B4-B5"')
    assert fixed.ok and fixed.text.startswith("Edited drafts/budget.json: one passage replaced"), fixed.text
    assert workspace.read("drafts/budget.json") == spec.replace('"=B3-B4"', '"=B4-B5"')
    for find, why in (
        ('"=B3-B4"', "drafts/budget.json doesn't hold find's passage"),  # it was replaced
        ('"Net (Income -   Expenses)",', "it does with other spaces or line breaks"),
        ('"usd"', f"find's passage is in drafts/budget.json {spec.count('"usd"')} times: give more of it around"),
    ):
        refused = calling(agent, "workspace_write", **edit, find=find, content='"x"')
        assert not refused.ok and why in refused.text, (find, refused.text)
    missing = calling(agent, "workspace_write", **edit, content='"x"')
    assert not missing.ok and "edit needs find" in missing.text
    stray = calling(agent, "workspace_write", path="drafts/other.json", mode="create", content="{}", find="x")
    assert not stray.ok and "find is for mode edit" in stray.text
    [spec_field] = [d for d in tools.definitions() if d["name"] == "workspace_write"]
    assert "edit" in spec_field["input_schema"]["properties"]["mode"]["enum"]
    assert "maxLength" not in spec_field["input_schema"]["properties"]["find"]  # a call's texts hold CALL_CHARS
    long = calling(agent, "workspace_write", **edit, find="x" * tools.CALL_CHARS, content="y")
    assert not long.ok and f"one call holds at most {tools.CALL_CHARS:,}" in long.text


# --- From 0.33.0 (formerly test_fixes_0330.py) ---


# --- the file tools: an edit of every copy, a restore, the workshop's hold ---


WEEK = "| | | | |\n" * 15 + "\nWochensumme: ______\n"


def test_an_edit_changes_every_copy_of_a_passage_when_it_says_how_many(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    ctx = working(agent)
    book = "# Haushaltsbuch\n\n" + "## Woche\n\n" + "\n## Woche\n\n".join([WEEK] * 3)
    assert call(ctx, "workspace_write", {"path": "books/b.md", "mode": "create", "content": book}).ok
    more = {"path": "books/b.md", "mode": "edit", "find": WEEK, "content": "| | | | |\n" * 22 + WEEK[-21:]}
    once = call(ctx, "workspace_write", more)
    assert not once.ok and "is in books/b.md 3 times: give more of it around" in once.text and "count 3" in once.text
    wrong = call(ctx, "workspace_write", {**more, "count": 4})
    assert not wrong.ok and "3 times, not 4: nothing was changed" in wrong.text
    done = call(ctx, "workspace_write", {**more, "count": 3})
    assert done.ok and "3 passages replaced" in done.text
    assert agent.roots()[0].read("books/b.md").count("| | | | |\n" * 22) == 3
    refused = call(ctx, "workspace_write", {"path": "books/b.md", "mode": "append", "content": "x", "count": 2})
    assert not refused.ok and "count is for mode edit" in refused.text


def test_an_overwrite_or_a_delete_can_be_restored(data_dir: Path) -> None:
    agent, _ = lined(data_dir)
    ctx = working(agent)
    whole = "# Haushaltsbuch 2027\n\n" + "## Januar\n\n" * 200
    workspace = agent.roots()[0]
    assert call(ctx, "workspace_write", {"path": "books/i.md", "mode": "create", "content": whole}).ok
    header = "---\ntitle: Haushaltsbuch 2027\n---\n"
    gone = call(ctx, "workspace_write", {"path": "books/i.md", "mode": "overwrite", "content": header})
    assert gone.ok and "Its text before is kept: restore brings it back." in gone.text  # live: cycle #134
    back = call(ctx, "workspace_write", {"path": "books/i.md", "mode": "restore"})
    assert back.ok and "as it was before the overwrite of " in back.text and workspace.read("books/i.md") == whole
    undone = call(ctx, "workspace_write", {"path": "books/i.md", "mode": "restore"})
    assert undone.ok and workspace.read("books/i.md") == header  # a second restore undoes the first
    assert call(ctx, "workspace_write", {"path": "books/i.md", "mode": "delete"}).ok
    assert call(ctx, "workspace_write", {"path": "books/i.md", "mode": "restore"}).ok
    assert workspace.read("books/i.md") == header
    none = call(ctx, "workspace_write", {"path": "books/new.md", "mode": "restore"})
    assert not none.ok and "no earlier text of books/new.md is kept" in none.text
    kept = rows(agent, "SELECT COUNT(*) AS n FROM workspace_versions WHERE path = 'books/i.md'")[0]["n"]
    assert kept <= tools.VERSIONS_KEPT


# --- From 0.39.2 (formerly test_fixes_0392.py) ---


# --- 2. a picture's age ---


def test_look_says_when_a_picture_was_made_and_that_it_came_before_this_version(data_dir: Path) -> None:
    """Live, books/check-kw-page6.png (made 10-12 under 0.38.0) showed the old 2.2 mm rows, and the first cycle under
    0.39.1 took them for its renderer's."""
    agent, _ = lined(data_dir, titles=())  # a cycle under this version, at the clock's start
    ctx = working(agent)
    jail = agent.roots()[0]
    started = agent.clock.now()
    before, after = started - timedelta(days=2), started + timedelta(minutes=5)
    for path, made in (("books/check-kw-page6.png", before), ("books/v14-page6.png", after)):
        jail.write_bytes(path, png(600, 900))
        os.utime(jail.root / path, (made.timestamp(), made.timestamp()))
    old = call(ctx, "look", {"path": "books/check-kw-page6.png"})
    assert old.ok and old.text == (
        f"books/check-kw-page6.png (600 x 900 pixels, made {before:%Y-%m-%d %H:%M}, before version {app_version()}"
        " ran: it shows what Ember's tools made then), shown here:"
    )
    new = call(ctx, "look", {"path": "books/v14-page6.png"})
    assert new.ok and new.text == f"books/v14-page6.png (600 x 900 pixels, made {after:%Y-%m-%d %H:%M}), shown here:"


# --- 3. a file kept in another folder ---


def test_a_missing_file_names_the_file_of_its_name_elsewhere(data_dir: Path) -> None:
    """Live, make_document was asked for books/…-v14.md, which the workshop had kept in workshop/out/."""
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    ctx.workspace.write("books/haushaltsbuch-v13.md", "# Haushaltsbuch\n")
    ctx.workspace.write("workshop/out/haushaltsbuch-v14.md", "# Haushaltsbuch\n")
    made = call(ctx, "make_document", {"source": "books/haushaltsbuch-v14.md", "output": "books/haushaltsbuch.pdf"})
    assert not made.ok and made.text == (
        "Error: books/haushaltsbuch-v14.md doesn't exist; a file of that name is at workshop/out/haushaltsbuch-v14.md."
    )
    deeper = call(ctx, "workspace_read", {"path": "drafts/new/haushaltsbuch-v14.md"})  # its folder is missing too
    assert not deeper.ok and deeper.text == (
        "Error: drafts doesn't exist; a file of that name is at workshop/out/haushaltsbuch-v14.md."
    )
    other = call(ctx, "workspace_read", {"path": "books/haushaltsbuch-v15.md"})
    assert not other.ok and other.text == "Error: books/haushaltsbuch-v15.md doesn't exist."


# --- From 0.40.1 (formerly test_fixes_0401.py) ---


def test_a_folder_lists_its_newest_files_first(data_dir: Path) -> None:
    """Live (analysis-0.40.0, 3.2), books/ held 185 files, versions v3 to v22: listed by name, its first 100 took 4 KB a
    listing, and four listings in one cycle didn't say which file was the newest."""
    _, ctx = publishing(data_dir)
    jail = ctx.workspace
    jail.write("books/drafts/old.md", "a draft")
    for n in range(1, 51):
        jail.write(f"books/hb-v{n}.md", f"version {n}")
        os.utime(jail.root / "books" / f"hb-v{n}.md", (1_760_000_000 + n * 60, 1_760_000_000 + n * 60))
    listed = call(ctx, "workspace_list", {"path": "books"})
    assert listed.ok
    lines = listed.text.splitlines()
    assert lines[0] == "books/drafts/"  # its folders first
    assert lines[1].startswith("books/hb-v50.md  10 B  ") and lines[2].startswith("books/hb-v49.md  10 B  ")
    assert lines[tools.FOLDER_SHOWN - 1].startswith("books/hb-v12.md")  # 40 entries: a folder and 39 files
    assert lines[tools.FOLDER_SHOWN] == "… 11 older"
    everything = call(ctx, "workspace_list", {})  # the whole workspace: by path, as before
    assert everything.ok and "books/drafts/old.md" in everything.text and " older\n" not in everything.text
