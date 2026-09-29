"""The making tools in a wake cycle: make_document, make_spreadsheet, make_image, look and guide.

They run sealed like every tool, but outside the database transaction the other tools share; a picture the agent
looks at goes to the model as an image and is counted by its pixels, not its bytes; the dry-run fake uses them all;
and memory notes that the new abilities made wrong ask for a rewrite.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.agent import loop, netguard, prompts, tools
from app.agent.fake_llm import FakeTransport, validate_request
from app.economy.metering import picture_tokens, rough_token_count
from app.products import make
from tests.test_agent import check_conversations, make_agent, plan, rows, text
from tests.test_agent import tools as tool_calls
from tests.test_loop_shapes import run

PAGE = "# Weekly planner\n\n- [ ] Plan the week\n\n::: lines 4\n"


def tool_rows(agent: Any, name: str) -> list[dict[str, Any]]:
    return rows(agent, f"SELECT status, summary, result FROM tool_calls WHERE tool = '{name}' ORDER BY id")


def png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (250, 250, 250)).save(buffer, "PNG")
    return buffer.getvalue()


def test_a_cycle_makes_a_document_and_a_listing_photo(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sealed: list[bool] = []
    document = make.document

    def watched(*args: Any, **kwargs: Any) -> make.Made:
        sealed.append(netguard.is_sealed())
        return document(*args, **kwargs)

    monkeypatch.setattr(make, "document", watched)
    agent, transport = make_agent(
        data_dir,
        [
            plan(steps=["make the planner"]),
            tool_calls(("make_document", {"source": "drafts/planner.md", "output": "shop/planner.pdf"})),
            tool_calls(
                (
                    "make_image",
                    {"output": "shop/planner-photo.png", "pages": "shop/planner.pdf#1", "title": "Weekly planner"},
                )
            ),
            text("Made the planner and its photo."),
            text("Reflected."),
        ],
    )
    agent.roots()[0].write("drafts/planner.md", PAGE)
    assert agent.run_cycle("schedule").status == "completed"
    assert sealed == [True]
    made = tool_rows(agent, "make_document")[0]
    assert made["status"] == "ok" and made["result"].startswith("Made shop/planner.pdf: 1 page (A4 portrait)")
    assert made["summary"] == "made shop/planner.pdf, shop/planner.docx, shop/planner-page1.png"
    assert tool_rows(agent, "make_image")[0]["status"] == "ok"
    files = [e.path for e in agent.roots()[0].walk(300).files]
    assert files == [
        "drafts/planner.md",
        "shop/planner-page1.png",
        "shop/planner-photo.png",
        "shop/planner.docx",
        "shop/planner.pdf",
    ]
    check_conversations(transport.sent)


def test_a_layout_mistake_is_an_error_to_fix_not_a_strike(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            plan(steps=["make it"]),
            tool_calls(("make_document", {"source": "drafts/bad.md", "output": "shop/bad.pdf"})),
            tool_calls(("make_document", {"source": "drafts/bad.md", "output": "shop/bad.pdf"})),
            tool_calls(("make_document", {"source": "drafts/bad.md", "output": "shop/bad.pdf"})),
            tool_calls(("make_document", {"source": "drafts/bad.md", "output": "shop/bad.pdf"})),
            text("Gave up."),
            text("Reflected."),
        ],
    )
    agent.roots()[0].write("drafts/bad.md", "# Fine\n::: wobble\n")
    agent.run_cycle("schedule")
    results = tool_rows(agent, "make_document")
    assert [r["status"] for r in results] == ["error"] * 4  # three strikes would have ended the act phase
    assert results[0]["result"] == (
        "Error: drafts/bad.md: line 2: unknown layout line '::: wobble'; use sidebar, main, box, columns, column, "
        "center, photo, lines, space or pagebreak."
    )
    assert agent.roots()[0].size_of("shop/bad.pdf") is None


def test_look_shows_the_model_the_picture(data_dir: Path) -> None:
    agent, transport = make_agent(
        data_dir,
        [
            plan(steps=["check the photo"]),
            tool_calls(("look", {"path": "shop/photo.png"})),
            text("It looks right."),
            text("Reflected."),
        ],
    )
    agent.roots()[0].write_bytes("shop/photo.png", png(3000, 2250))
    agent.run_cycle("schedule")
    result = transport.sent[2]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "is_error" not in result
    words, picture = result["content"]
    assert words == {"type": "text", "text": "shop/photo.png (1000 x 750 pixels), shown here:"}
    assert picture["type"] == "image" and picture["source"]["media_type"] == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(picture["source"]["data"]))).size == (1000, 750)
    stored = tool_rows(agent, "look")[0]
    assert stored["status"] == "ok" and "data" not in stored["result"]  # the database keeps the words, not the picture
    check_conversations(transport.sent)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("shop/cv.pdf", "look shows .png and .jpg pictures"),
        ("notes.md", "look shows .png and .jpg pictures"),
        ("shop/missing.png", "shop/missing.png doesn't exist"),
    ],
)
def test_look_refuses_what_isnt_a_picture(data_dir: Path, path: str, message: str) -> None:
    agent, _ = make_agent(
        data_dir, [plan(steps=["look"]), tool_calls(("look", {"path": path})), text("Ok."), text("Reflected.")]
    )
    agent.roots()[0].write_bytes("shop/cv.pdf", b"%PDF-1.7")
    agent.run_cycle("schedule")
    result = tool_rows(agent, "look")[0]
    assert result["status"] == "error" and message in result["result"]


def ctx_for(agent: Any) -> tools.ToolContext:
    return tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=0,
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )


def test_reading_a_product_says_what_it_is(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    ctx = ctx_for(agent)
    ctx.workspace.write("d.md", "# One\n::: pagebreak\n# Two\n")
    make.document(ctx.workspace, "d.md", "shop/d.pdf")
    read = tools.HANDLERS["workspace_read"]
    assert read(ctx, {"path": "shop/d.pdf"}, None).text == (
        f"shop/d.pdf is a PDF with 2 pages, {ctx.workspace.size_of('shop/d.pdf') / 1024:,.0f} KB. Its source is the "
        "text you made it from."
    )
    assert "a PNG picture, 827 x 1170 pixels" in read(ctx, {"path": "shop/d-page1.png"}, None).text
    assert "a Word document" in read(ctx, {"path": "shop/d.docx"}, None).text
    listed = tools.HANDLERS["workspace_list"](ctx, {}, None).text
    assert "Using 0.0 KB of 50 MB and 5 of 5,000 files (in 1 folder); PDF, Word, Excel and PNG files use" in listed


def test_text_tools_refuse_products_with_a_pointer() -> None:
    jail_error = pytest.raises(tools.SandboxError, match="made with make_document, make_spreadsheet, make_image")
    from app.agent.sandbox import Jail

    j = Jail(Path("/nonexistent"))
    with jail_error:
        j.parts("shop/cv.pdf")


def test_guides_fit_in_one_result_and_the_tools_are_offered() -> None:
    for topic in tools.GUIDES:
        guide = tools.guide_text(topic)
        assert 1_000 < len(guide) <= tools.MAX_RESULT_CHARS
    names = [d["name"] for d in tools.definitions()]
    assert {"make_document", "make_spreadsheet", "make_image", "look", "guide"} <= set(names)
    assert tools.SPECS["request_upgrade"].reflect  # a blocker noticed while reflecting can still be reported


def test_pictures_count_by_pixels_not_bytes() -> None:
    data = base64.b64encode(png(1000, 750)).decode()
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data + "A" * 0}}
    assert picture_tokens(data) == 1_000 * 750 * 125 // (750 * 100) + 100
    request = {"messages": [{"role": "user", "content": [block]}]}
    empty = {"messages": [{"role": "user", "content": [{"type": "image"}]}]}
    assert rough_token_count(request) == rough_token_count(empty) + picture_tokens(data)
    assert picture_tokens("bm90IGEgcGljdHVyZQ==") == 5_000  # unknown: the generous allowance


def test_the_conversation_counts_a_picture_by_its_pixels() -> None:
    """A look's picture counts like text in proportion to its pixels, so a wide spreadsheet picture doesn't use up
    the room of a whole page (0.9.0: every look counted as the largest, and four ended the cycle)."""

    def size(data: str) -> int:
        block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
        return loop._size([{"role": "user", "content": [block]}]) - loop._size([{"role": "user", "content": [{}]}])

    largest = tools.LOOK_PIXELS
    assert size(base64.b64encode(png(largest, largest)).decode()) == loop.IMAGE_EQUIVALENT_BYTES
    assert size(base64.b64encode(png(1000, 750)).decode()) == 3_750
    assert size(base64.b64encode(png(1000, 180)).decode()) == 900
    assert size("bm90IGEgcGljdHVyZQ==") == loop.IMAGE_EQUIVALENT_BYTES  # unreadable: counted as the largest
    # Eight listing photos and a dozen tool results fit, with room for the model's replies.
    assert loop.MAX_CONVERSATION_BYTES > 8 * 3_750 + 12 * 2_000
    assert tools.SPECS["look"].per_cycle >= 5  # a listing needs 5 to 10 photos, each looked at


def test_the_fake_checks_pictures_like_the_api() -> None:
    def request(source: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": "claude-sonnet-5",
            "max_tokens": 10,
            "messages": [
                {"role": "user", "content": [{"type": "image", "source": source}, {"type": "text", "text": "?"}]}
            ],
        }

    good = {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}
    assert validate_request(request(good)) is None
    assert "media_type" in (validate_request(request({**good, "media_type": "image/tiff"})) or "")
    assert "base64" in (validate_request(request({"type": "url", "url": "https://example.org/a.png"})) or "")


def test_a_dry_run_makes_products_every_second_cycle(data_dir: Path) -> None:
    agent, ends = run(data_dir, FakeTransport(seed=3, scenario="founder"), cycles=2)
    assert [e.status for e in ends] == ["completed", "completed"]
    by_cycle = rows(
        agent, "SELECT cycle_id, tool, status FROM tool_calls WHERE tool IN ('make_document', 'look', 'make_image')"
    )
    assert {(r["tool"], r["status"]) for r in by_cycle} == {
        ("make_document", "ok"),
        ("look", "ok"),
        ("make_image", "ok"),
    }
    assert {r["cycle_id"] for r in by_cycle} == {2}
    files = [e.path for e in agent.roots()[0].walk(300).files]
    assert any(f.endswith(".pdf") for f in files) and any(f.endswith("-photo-1.png") for f in files)


def test_the_prompts_tell_the_agent_to_make_files_and_ask_for_what_is_missing() -> None:
    work = prompts.work_request(context_settings(), "brief", [])
    rules = work["system"][2]["text"]
    assert "You make finished files yourself" in rules and "file request_upgrade" in rules
    assert "file request_upgrade" in prompts.REFLECT_PROMPT
    knowledge = prompts.knowledge()
    assert "Designed by" in knowledge and "make_document" in knowledge


def context_settings() -> Any:
    from app.config import Settings

    return Settings()


def test_a_spreadsheet_spec_error_reaches_the_agent(data_dir: Path) -> None:
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["make the budget"]),
            tool_calls(("make_spreadsheet", {"source": "drafts/budget.json", "output": "shop/budget.xlsx"})),
            text("Ok."),
            text("Reflected."),
        ],
    )
    spec = {"sheets": [{"name": "B", "columns": [{"title": "A"}], "rows": [['=HYPERLINK("https://x.example")']]}]}
    agent.roots()[0].write("drafts/budget.json", json.dumps(spec))
    agent.run_cycle("schedule")
    result = tool_rows(agent, "make_spreadsheet")[0]
    assert result["status"] == "error" and "HYPERLINK" in result["result"]
