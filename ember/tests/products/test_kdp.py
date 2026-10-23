"""0.25.0: Amazon KDP, phase A: books the agent makes and the owner publishes at KDP from their own account (Amazon
has no API for KDP). Ember's code checks the package against KDP's rules: the words, the price, a paperback's interior
(its trim, page count and margins) and its full cover (as wide as the pages make the spine), an ebook's Word file and
its JPEG cover; the card gives the owner every field and file, and the plan lists the books.

Also tests from 0.40.2 (formerly test_fixes_0402.py)."""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from app import web
from app.agent import never, news, obligations, prompts, tools
from app.agent.fake_llm import FakeTransport, request_kind
from app.agent.sandbox import Jail
from app.economy.clock import to_iso
from app.integrations import connectors, kdp
from app.products import images, make, markup, pdf
from tests.agent_helpers import ROOMY, owner, rows, run
from tests.etsy_helpers import call, views_approval
from tests.plan.test_plan_tree import request

PUBLISHING = ROOMY.model_copy(update={"kdp_enabled": True, "kdp_author": "Jane Doe"})
BACK = "A gentle daily journal for noticing what went well.|One page a day: three good things and room to write."


def journal(jail: Jail, path: str = "books/journal", days: int = 120, page: str = "6x9", margin: int = 12) -> str:
    """A journal's interior PDF of ``days`` pages, made with make_document."""
    day = "::: pagebreak\n## Day {}\n\nThree good things:\n\n::: lines 12\n"
    days_text = "\n".join(day.format(i) for i in range(2, days + 1))
    jail.write(f"{path}.md", f"---\npage: {page}\nmargin: {margin}\ntheme: minimal\n---\n# My Journal\n\n{days_text}")
    make.document(jail, f"{path}.md", f"{path}.pdf", word_copy=False, previews=False)
    return f"{path}.pdf"


def front(jail: Jail, path: str = "books/front.png") -> str:
    make.image(jail, path, "", "My Journal", "120 days of small joys", layout="poster", shape="pin")
    return path


def paperback(jail: Jail, days: int = 120) -> str:
    """A journal's interior and its front picture: the interior's path."""
    interior = journal(jail, days=days)
    front(jail)
    return interior


def ebook(jail: Jail) -> str:
    """A guide's Word manuscript and its front picture: the manuscript's path."""
    jail.write("books/guide.md", "# A Small Guide\n\nSome useful words.\n\n## Part two\n\nMore of them.\n")
    make.document(jail, "books/guide.md", "books/guide.pdf", previews=False)
    front(jail)
    return "books/guide.docx"


def spec(jail: Jail, manuscript: str, path: str = "books/journal.json", **extra: Any) -> str:
    """A book's spec as the agent writes it (a key given as None is left out)."""
    data = {
        "format": "paperback",
        "title": "My Gratitude Journal",
        "subtitle": "120 Days of Small Joys",
        "description": "A gentle daily journal: one page a day, three good things and room to write.",
        "keywords": ["gratitude journal", "daily journal for women", "mindfulness notebook"],
        "categories": ["Self-Help > Journal Writing", "Self-Help > Happiness"],
        "language": "English",
        "price": "9.99",
        "manuscript": manuscript,
        "paper": "white",
        "low_content": True,
        "cover": {"front": "books/front.png", "back": BACK},
        "reason": "Gratitude journals sell steadily at 7 to 10 USD with few reviews needed.",
        **extra,
    }
    jail.write(path, json.dumps({k: v for k, v in data.items() if v is not None}))
    return path


def publishing(data_dir: Path) -> tuple[Any, tools.ToolContext]:
    """A dry-run agent after one cycle with KDP on, and the tools' context of that cycle."""
    agent, _ = run(data_dir, FakeTransport(), settings=PUBLISHING)
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
        kdp=tools.KdpAccess("Jane Doe"),
    )
    return agent, ctx


def project(agent: Any) -> int:
    return rows(agent, "SELECT MAX(id) AS id FROM projects")[0]["id"]


# 0.40.0: what the agent saw on a paperback's fresh pages, for the owner
QA = (
    "White space: none; each day's lines reach the bottom margin. Rows: writing lines 8 mm apart. Calendar: no "
    "dates. AI line: on the last page with the day's lines."
)


def next_reply(ctx: tools.ToolContext) -> None:
    """0.40.0: the model's next reply in the cycle (call runs a tool as the newest reply's): a check's pages have
    reached it."""
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO llm_calls (boot_id, cycle_id, purpose, model, simulated, status, ts, local_day)"
            " VALUES ('test', ?, 'work', 'scripted', 1, 'ok', ?, ?)",
            (ctx.cycle_id, ctx.now(), ctx.clock.today().isoformat()),
        )


def proposed(agent: Any, ctx: tools.ToolContext, path: str) -> int:
    """A book proposed as the agent proposes it: a paperback (0.40.0) after a check drew its pages, in a later reply."""
    if json.loads(ctx.workspace.read(path))["format"] == "paperback":
        checked = call(ctx, "propose_kdp_book", {"spec": path, "check": True})
        assert checked.ok, checked.text
        next_reply(ctx)
    made = call(ctx, "propose_kdp_book", {"spec": path, "qa": QA})
    assert made.ok, made.text
    return rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'kdp_package'")[0]["id"]


# --- KDP's numbers -------------------------------------------------------------------------------------------------


def test_the_spine_and_the_cover_follow_kdp_s_formula() -> None:
    assert kdp.spine_width(120, "white") == Decimal("0.270240")
    assert kdp.spine_width(121, "cream") == kdp.spine_width(122, "cream") == Decimal("0.3050")  # odd: one blank more
    assert kdp.cover_size("6x9", 120, "white") == (Decimal("12.520240"), Decimal("9.250"))
    assert kdp.interior_size("6x9", True) == (Decimal("6.125"), Decimal("9.25"))
    assert [kdp.inside_margin(n) for n in (24, 150, 151, 300, 500, 700, 701)] == [
        Decimal(v) for v in ("0.375", "0.375", "0.5", "0.5", "0.625", "0.75", "0.875")
    ]
    assert kdp.trim_size("6 x 9 in") == kdp.trim_size("6x9") == (Decimal("6"), Decimal("9"))
    assert kdp.page_limits("cream", "8.5x11") == (24, 550)
    with pytest.raises(kdp.KdpError, match="trim must be one of"):
        kdp.trim_size("A5")


def test_a_price_must_cover_printing_and_stay_in_kdp_s_bands() -> None:
    assert kdp.printing_cost(108, "white", "6x9") == Decimal("2.30")  # flat up to 108 pages (0.40.3)
    assert kdp.printing_cost(120, "white", "6x9") == Decimal("2.44")
    assert kdp.printing_cost(120, "white", "8.5x11") == Decimal("3.04")  # a large trim
    assert kdp.least_price(120, "white", "6x9") == Decimal("4.88")  # 50% below 9.99 USD
    assert kdp.least_price(300, "color", "8.5x11") == Decimal("41.67")  # 60% from 9.99 USD on
    with pytest.raises(kdp.KdpError, match="must be 4.88 to 250 USD"):
        kdp.check_price("4.50", "paperback", pages=120, paper="white", trim="6x9")
    assert kdp.check_price("$9,99", "paperback", pages=120, paper="white", trim="6x9") == "9.99"
    assert kdp.check_price("0.99", "ebook", file_bytes=500_000) == "0.99"
    with pytest.raises(kdp.KdpError, match="must be 1.99 to 200 USD"):
        kdp.check_price("0.99", "ebook", file_bytes=4 * 1024 * 1024)  # 3 MB and more


def test_the_words_are_checked_as_kdp_takes_them() -> None:
    assert kdp.check_title(" My  Journal ", "") == ("My Journal", "")
    with pytest.raises(kdp.KdpError, match="together have more than 199"):
        kdp.check_title("T" * 120, "S" * 80)
    assert kdp.check_keywords("a, b, A, c") == ("a", "b", "c")
    with pytest.raises(kdp.KdpError, match="at most 7 keywords"):
        kdp.check_keywords(",".join(f"k{i}" for i in range(8)))
    with pytest.raises(kdp.KdpError, match="more than 50 characters"):
        kdp.check_keywords("x" * 51)
    assert kdp.check_categories("Self-Help>Journal Writing; Self-Help > Journal Writing") == (
        "Self-Help > Journal Writing",
    )
    with pytest.raises(kdp.KdpError, match="at most 3 categories"):
        kdp.check_categories("a; b; c; d")
    assert kdp.check_description(f"Words.\n\n{kdp.DISCLOSURE}") == "Words."  # Ember's code adds the line once


# --- the interior and the cover ------------------------------------------------------------------------------------


def test_a_document_can_be_a_book_s_interior(tmp_path: Path) -> None:
    assert markup.KDP_PAGES == kdp.TRIM_NAMES  # the same sizes in both
    jail = Jail(tmp_path)
    interior = journal(jail, days=120)
    data = jail.read_bytes(interior)
    assert images.page_sizes(data)[0] == pytest.approx((432.0, 648.0), abs=0.1)  # 6 x 9 in
    found = kdp.check_interior(
        images.page_sizes(data), lambda: images.ink_boxes(data, kdp.INK_DPI, kdp.INK_LEVEL), "6x9", "white"
    )
    assert found == kdp.Interior(120, False, ())
    assert pdf.max_pages("6x9in") == pdf.BOOK_PAGES and pdf.max_pages("A4") == pdf.MAX_PAGES
    jail.write("long.md", "---\npage: A4\n---\n" + "Text.\n\n::: pagebreak\n" * 45)
    with pytest.raises(make.ProductError, match="longer than 40 pages"):
        make.document(jail, "long.md", "long.pdf")


def test_kdp_s_margins_page_size_and_page_count_are_checked(tmp_path: Path) -> None:
    jail = Jail(tmp_path)

    def check(path: str, trim: str = "6x9") -> tuple[str, ...]:
        data = jail.read_bytes(path)
        ink = lambda: images.ink_boxes(data, kdp.INK_DPI, kdp.INK_LEVEL)  # noqa: E731
        return kdp.check_interior(images.page_sizes(data), ink, trim, "white").findings

    (narrow,) = check(journal(jail, "narrow", days=30, margin=7))  # 0.276 in: wide enough outside, not inside
    assert narrow.startswith("page 1 prints within its inside margin; page 2 prints within its inside margin;")
    assert "(30 pages in all)" in narrow and "a margin of at least 10 mm" in narrow
    (wrong,) = check(journal(jail, "letter", days=30, page="A4"))
    assert wrong.startswith("page 1 is 8.268 in x 11.693 in, not 6 in x 9 in (the 6x9 trim; 30 such pages)")
    (short,) = check(journal(jail, "short", days=10))
    assert short == "it has 10 pages; KDP prints 24 to 828 on white paper"


def test_a_paperback_s_cover_is_its_full_wrap(tmp_path: Path) -> None:
    jail = Jail(tmp_path)
    interior = journal(jail, days=120)
    picture = front(jail)
    made = make.kdp_cover(
        jail, "books/c.pdf", picture, interior, "white", BACK, "My Journal · Jane Doe", background="#1F2A44"
    )
    assert made.paths == ["books/c.pdf", "books/c-preview.png"]
    text = made.text()
    assert text.startswith("Made books/c.pdf: a paperback cover for 120 pages at 6x9 on white paper, 12.52 in x ")
    assert "(the spine 0.27 in), at 300 dpi" in text and "The back and spine are #1F2A44." in text
    (size,) = images.page_sizes(jail.read_bytes("books/c.pdf"))
    assert kdp.check_cover(size, 120, "6x9", "white") == []
    assert kdp.check_cover(size, 140, "6x9", "white")[0].startswith("the cover is 12.52 in x 9.25 in; for 140 pages")
    assert images.png_size(jail.read_bytes("books/c-preview.png"))[0] == images.PREVIEW_WIDTH
    with pytest.raises(make.ProductError, match="spine text only on books of more than 79 pages"):
        make.kdp_cover(jail, "books/d.pdf", picture, journal(jail, "thin", days=40), "white", spine_text="Thin")
    with pytest.raises(make.ProductError, match="needs its interior PDF"):
        make.kdp_cover(jail, "books/e.pdf", picture)
    jail.write("wide.md", "---\npage: A4\nlandscape: true\n---\n" + "Text.\n\n::: pagebreak\n" * 30)
    make.document(jail, "wide.md", "wide.pdf", word_copy=False, previews=False)
    with pytest.raises(make.ProductError, match="wide.pdf's pages are 11.693 in x 8.268 in: no KDP trim size"):
        make.kdp_cover(jail, "books/e.pdf", picture, "wide.pdf", "white")
    assert kdp.trim_of(images.page_sizes(jail.read_bytes(journal(jail, "a4", days=30, page="A4")))[0]) == (
        "8.27x11.69",  # A4 is one of KDP's trims
        False,
    )


def test_an_ebook_s_cover_is_a_jpeg_in_kdp_s_proportions(tmp_path: Path) -> None:
    jail = Jail(tmp_path)
    picture = front(jail)
    made = make.kdp_cover(jail, "books/e.jpg", picture)
    data = jail.read_bytes("books/e.jpg")
    assert Image.open(io.BytesIO(data)).format == "JPEG" and images.png_size(data) == kdp.EBOOK_COVER
    assert made.text().startswith("Made books/e.jpg: an ebook cover, 1600 x 2560 pixels (KDP's ideal)")
    with pytest.raises(make.ProductError, match="an ebook's cover is its front alone"):
        make.kdp_cover(jail, "books/f.jpg", picture, spine_text="No")
    assert kdp.check_ebook_cover(data, 1600, 2560, "c.jpg") == []
    with pytest.raises(kdp.KdpError, match="1.6 high to 1 wide"):
        kdp.check_ebook_cover(data, 2000, 2000, "c.jpg")
    with pytest.raises(kdp.KdpError, match="isn't a JPEG picture"):
        kdp.check_ebook_cover(jail.read_bytes(picture), 1600, 2560, "c.jpg")  # a PNG, whatever its name
    assert kdp.check_ebook_cover(data, 1000, 1600, "c.jpg") == [
        "the cover has 1000 x 1600 pixels, fewer than KDP's ideal 1600 x 2560"
    ]


# --- the proposal, its card and the owner's part -------------------------------------------------------------------


def test_a_paperback_is_proposed_and_its_card_has_everything_to_publish_it(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    book_spec = spec(ctx.workspace, paperback(ctx.workspace), project_id=project(agent))
    assert call(ctx, "propose_kdp_book", {"spec": book_spec, "check": True}).ok  # 0.40.0: its pages first
    next_reply(ctx)
    made = call(ctx, "propose_kdp_book", {"spec": book_spec, "qa": QA})
    assert made.ok and made.text.startswith("Made books/journal-cover.pdf: a paperback cover for 120 pages")
    request = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'kdp_package'")[0]["id"]
    assert f"Approval request #{request} is waiting for your owner. Nothing is at Amazon yet." in made.text
    row = rows(agent, f"SELECT * FROM approvals WHERE id = {request}")[0]
    assert (row["type"], row["executor"], row["project_id"]) == ("sell", "kdp_package", project(agent))
    assert row["title"] == "KDP paperback: My Gratitude Journal"
    book = kdp.book_from_action(row["action"])
    assert (book.trim, book.pages, book.bleed, book.author, book.low_content) == ("6x9", 120, False, "Jane Doe", True)
    assert book.categories == ("Self-Help > Journal Writing", "Self-Help > Happiness")
    assert book.cover.path == "books/journal-cover.pdf"  # made next to the spec
    assert (
        "Price: 9.99 USD at Amazon.com (about 3.55 USD a sale: 60% of 9.99 USD less the printing cost of about "
        in (row["payload"])
    )
    assert row["payload"].endswith(kdp.DISCLOSURE) and "KDP's AI question: Yes." in row["payload"]
    with agent.db.connection() as conn:
        assert never.reasons(conn, row) == ["owner_only"]  # never on an unlock
    assert connectors.class_of(row["executor"]).by_owner
    card = views_approval(agent, request)
    assert card["kdp"]["bookshelf_url"] == "https://kdp.amazon.com/en_US/bookshelf"
    assert [(f["role"], f["path"], f["unchanged"]) for f in card["kdp"]["files"]] == [
        ("manuscript", "books/journal.pdf", True),
        ("cover", "books/journal-cover.pdf", True),
    ]
    ctx.workspace.write_bytes(book.cover.path, b"%PDF-1.7 changed")  # the owner downloads it: the card says it changed
    assert [f["unchanged"] for f in views_approval(agent, request)["kdp"]["files"]] == [True, False]
    who = owner(agent)
    changed = who.decide(request, {"decision": "approve_with_changes", "final_payload": "Other words"}, "Owner")
    assert changed.status == 422 and "change its words as you enter them at KDP" in changed.body["error"]
    assert who.decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == []  # Ember's code never carries it out
    link = "https://www.amazon.com/dp/B0TESTBOOK"
    assert who.close(request, {"outcome": "done", "result_link": link}, "Owner").status == 200
    with agent.db.transaction() as conn:
        assert obligations.keep(conn, agent.scope(), to_iso(agent.clock.now()), "2000-01-01") == [
            f"Obligation: react to request #{request} (done)"
        ]
        section = kdp.text(conn, agent.scope(), agent.clock.now(), "Jane Doe")
    assert f'#{request} paperback "My Gratitude Journal" (6x9, 120 pages, 9.99 USD): published at {link}' in section
    assert "its week from Sunday 00:00 UTC; proposed this week: 0 ebooks, 1 paperback." in section


def test_check_makes_the_cover_and_checks_without_asking(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    book_spec = spec(ctx.workspace, paperback(ctx.workspace), cover={"front": "books/front.png", "spine": "Spine"})
    checked = call(ctx, "propose_kdp_book", {"spec": book_spec, "check": True})
    assert checked.ok, checked.text
    assert "Checked books/journal.json: KDP would take this paperback as it is." in checked.text
    assert "Nothing went to your owner. Propose it with check false in a later reply of this cycle" in checked.text
    assert ctx.workspace.size_of("books/journal-cover-preview.png") is not None  # to look at
    assert rows(agent, "SELECT COUNT(*) AS n FROM approvals WHERE executor = 'kdp_package'")[0]["n"] == 0
    thin = spec(ctx.workspace, journal(ctx.workspace, "books/thin", days=40), "books/thin.json",
                cover={"front": "books/front.png", "spine": "Thin"})  # fmt: skip
    refused = call(ctx, "propose_kdp_book", {"spec": thin, "check": True})
    assert not refused.ok and "leave out cover.spine" in refused.text
    jail = ctx.workspace
    jail.write("books/bad.json", '{"format": "paperback", "title": "T"')
    assert "isn't valid JSON" in call(ctx, "propose_kdp_book", {"spec": "books/bad.json", "check": True}).text
    jail.write("books/odd.json", json.dumps({"format": "ebook", "colour": "red"}))
    assert "unknown key 'colour'" in call(ctx, "propose_kdp_book", {"spec": "books/odd.json", "check": True}).text


def test_a_package_kdp_would_refuse_never_reaches_the_owner(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    jail = ctx.workspace
    interior = paperback(jail)
    assert call(ctx, "propose_kdp_book", {"spec": spec(jail, interior), "check": True}).ok
    thicker = journal(jail, "books/thicker", days=140)  # with the 120 pages' cover, made before
    wrong = call(ctx, "propose_kdp_book", {"spec": spec(jail, thicker, cover="books/journal-cover.pdf")})
    assert not wrong.ok and "for 140 pages on white paper at 6x9 it must be 12.565 in x 9.25 in" in wrong.text
    cheap = call(ctx, "propose_kdp_book", {"spec": spec(jail, interior, price="3.99")})
    assert not cheap.ok and "this paperback's price must be 4.88 to 250 USD" in cheap.text
    narrow = journal(jail, "books/narrow", days=120, margin=6)
    tight = call(ctx, "propose_kdp_book", {"spec": spec(jail, narrow)})
    assert not tight.ok and "isn't ready for KDP: page 1 prints within its inside" in tight.text
    no_paper = call(ctx, "propose_kdp_book", {"spec": spec(jail, interior, paper=None)})
    assert not no_paper.ok and "a paperback needs paper" in no_paper.text
    assert rows(agent, "SELECT COUNT(*) AS n FROM approvals WHERE executor = 'kdp_package'")[0]["n"] == 0


def test_a_cover_s_front_keeps_its_words_inside_kdp_s_safe_zone(data_dir: Path) -> None:
    """0.45.0 (evaluation 2026-10-22, C6): live, a front picture in shape pin (2:3) on an 8.5x11 book lost 13% to the
    cut that fits it, its title printed 0.048 in inside the trim, and the check said KDP would take it. 0.45.2: the
    safe zone is 0.25 in inside the trim (0.375 in from the cover's edges), as KDP's reviewers asked."""
    _, ctx = publishing(data_dir)
    jail = ctx.workspace
    letter = journal(jail, "books/letter", days=120, page="8.5x11")
    words = ("Haushaltsbuch 2027", "Wochen-Logs | Monatsübersicht")
    make.image(jail, "books/front.png", "", *words, layout="poster", shape="pin")
    refused = call(ctx, "propose_kdp_book", {"spec": spec(jail, letter), "check": True})
    assert not refused.ok
    assert refused.text.startswith("Error: books/journal-cover.pdf: the cover's front prints words or pictures 0.")
    assert "in inside the trim at the top, " in refused.text and "in inside the trim at the bottom" in refused.text
    assert (
        "KDP asks for 0.25 in inside the trim (0.375 in from the cover's edges) and Ember's code for 0.25 in from the "
        "spine's fold, or the cut and the fold may take them" in refused.text
    )
    assert "make books/front.png again in make_image's shape portrait (the nearest to a front at 8.5x11" in refused.text
    assert "with its words 0.375 in from its edges, then propose the book again" in refused.text
    # a cover file of its own is held to the same rule
    jail.write_bytes("books/own.pdf", jail.read_bytes("books/journal-cover.pdf"))
    own = call(ctx, "propose_kdp_book", {"spec": spec(jail, letter, cover="books/own.pdf"), "check": True})
    assert not own.ok and own.text.endswith("or the cut and the fold may take them: move them that far in.")
    make.image(jail, "books/front.png", "", *words, layout="poster", shape="portrait")
    checked = call(ctx, "propose_kdp_book", {"spec": spec(jail, letter), "check": True})
    assert checked.ok and "KDP would take this paperback as it is" in checked.text, checked.text
    assert "keep its words 0.375 in (4% of its width) from them and 0.25 in from the spine." in checked.text
    # one whose art reaches its edges is a note: Ember's code can't tell its words from its picture
    art = Image.linear_gradient("L").resize((2000, 2500)).convert("RGB")
    out = io.BytesIO()
    art.save(out, "PNG")
    jail.write_bytes("books/art.png", out.getvalue())
    noted = call(ctx, "propose_kdp_book", {"spec": spec(jail, letter, cover={"front": "books/art.png"}), "check": True})
    assert noted.ok and "Note: the front's picture reaches its edges, so Ember's code can't tell" in noted.text
    assert "its words keep 0.375 in from the cover's edges and 0.25 in from the spine's fold." in noted.text
    assert "the back's picture" not in noted.text  # the back Ember's code made is one colour
    # the numbers: a 6x9 cover of 120 pages on white is 12.52 x 9.25 in, its trim 0.125 in inside, the back's fold
    # 6.125 in from its left edge and the front's 6.395 in
    assert kdp.check_panel("front", None, "6x9", 120, "white") == []
    assert kdp.check_panel("front", (6.65, 0.375, 12.145, 8.875), "6x9", 120, "white") == []
    assert kdp.check_panel("front", (7.0, 0.173, 12.4, 9.0), "6x9", 120, "white") == [
        "the cover's front prints words or pictures 0.048 in inside the trim at the top, 0.125 in inside the trim at "
        "the bottom, 0.005 in beyond the trim at the outside edge: KDP asks for 0.25 in inside the trim (0.375 in from "
        "the cover's edges) and Ember's code for 0.25 in from the spine's fold, or the cut and the fold may take them"
    ]
    assert kdp.check_panel("front", (6.495, 1.0, 11.0, 8.0), "6x9", 120, "white")[0].startswith(
        "the cover's front prints words or pictures 0.1 in from the spine's fold: "
    )
    assert kdp.check_panel("back", (0.225, 1.0, 6.025, 8.0), "6x9", 120, "white")[0].startswith(
        "the cover's back prints words or pictures 0.1 in inside the trim at the outside edge, 0.1 in from the "
        "spine's fold: "
    )
    assert kdp.check_panel("back", (0.375, 0.375, 5.875, 8.875), "6x9", 120, "white") == []
    assert make.front_shape("6x9") == "pin" and make.front_shape("8.5x11") == "portrait"
    assert make.front_shape("8.5x8.5") == "square" and make.front_shape("8.25x6") == "landscape"


def test_words_0_2_in_inside_the_trim_are_refused_as_kdp_s_reviewers_refused_them(data_dir: Path) -> None:
    """0.45.2: KDP's reviewers asked two live covers (Ember's emails #64 and #65, books #140 and #141) to move all
    text 0.375 in from the cover's edges, 0.25 in inside the trim; 0.45.0's 0.125 in had passed them. Words 0.2 in
    inside the trim are refused now, on the front and on the back of a cover file of the agent's own, and so are words
    0.1 in from the spine's fold."""
    _, ctx = publishing(data_dir)
    jail = ctx.workspace
    interior = paperback(jail)  # 6x9, 120 pages on white: the cover is 12.52 x 9.25 in, the front 6.125 in wide

    def block(picture: Image.Image, per_inch: int, box: tuple[float, float, float, float]) -> None:
        """Words: a black box at ``box`` (left, top, right, bottom, in inches)."""
        corners = [round(value * per_inch) for value in box]
        ImageDraw.Draw(picture).rectangle([*corners[:2], corners[2] - 1, corners[3] - 1], fill=(0, 0, 0))

    # a front picture in the front's proportions with its bleed (nothing cut off), its words 0.2 in inside the trim
    picture = Image.new("RGB", (1225, 1850), (250, 246, 238))
    block(picture, 200, (1.0, 0.125 + 0.2, 5.0, 3.0))
    out = io.BytesIO()
    picture.save(out, "PNG")
    jail.write_bytes("books/close.png", out.getvalue())
    close = call(
        ctx, "propose_kdp_book", {"spec": spec(jail, interior, cover={"front": "books/close.png"}), "check": True}
    )
    assert not close.ok
    found = re.search(
        r"the cover's front prints words or pictures (0\.\d+) in inside the trim at the top: ", close.text
    )
    assert found and abs(float(found[1]) - 0.2) <= 0.015, close.text
    assert "with its words 0.375 in from its edges, then propose the book again" in close.text
    # 0.4 in inside the trim passes
    picture = Image.new("RGB", (1225, 1850), (250, 246, 238))
    block(picture, 200, (1.0, 0.125 + 0.4, 5.0, 3.0))
    out = io.BytesIO()
    picture.save(out, "PNG")
    jail.write_bytes("books/far.png", out.getvalue())
    far = call(ctx, "propose_kdp_book", {"spec": spec(jail, interior, cover={"front": "books/far.png"}), "check": True})
    assert far.ok, far.text
    # a cover file of its own, its back's words 0.2 in inside the trim at the outside edge and its front's 0.1 in from
    # the spine's fold (the back's fold is 6.125 in from the cover's left edge, the front's 6.395 in)
    wrap = Image.new("RGB", (2504, 1850), (255, 255, 255))
    block(wrap, 200, (0.125 + 0.2, 1.0, 5.0, 8.0))
    block(wrap, 200, (6.5, 1.0, 11.0, 4.0))
    jail.write_bytes("books/own.pdf", pdf.picture_page(images.jpeg(wrap), 12.52 * 72, 9.25 * 72, "My Journal"))
    own = call(ctx, "propose_kdp_book", {"spec": spec(jail, interior, cover="books/own.pdf"), "check": True})
    assert not own.ok
    back = re.search(
        r"the cover's back prints words or pictures (0\.\d+) in inside the trim at the outside edge: ", own.text
    )
    front = re.search(r"; the cover's front prints words or pictures (0\.\d+) in from the spine's fold: ", own.text)
    assert back and abs(float(back[1]) - 0.2) <= 0.015 and front and abs(float(front[1]) - 0.105) <= 0.015, own.text
    assert own.text.count("or the cut and the fold may take them: move them that far in") == 2
    guide = " ".join(tools.guide_text("kdp").split())
    assert "Keep words 0.375 in from a cover's edges and 0.25 in from the spine (KDP's reviewers)." in guide


def test_a_spine_in_another_colour_than_the_front_is_not_read_as_words(data_dir: Path) -> None:
    """0.45.2: the pixels at a fold are partly the spine's: with cover.background unlike the front's colour, 0.45.0
    read them as words down the front's whole height ("0.125 in beyond the trim at the top, ... at the bottom") and
    refused the cover. Each panel is read from a pixel past its fold now."""
    _, ctx = publishing(data_dir)
    jail = ctx.workspace
    interior = paperback(jail)
    for colour in ("#FF0000", "#FFFFFF", "#000000"):
        cover = {"front": "books/front.png", "back": BACK, "background": colour}
        checked = call(ctx, "propose_kdp_book", {"spec": spec(jail, interior, cover=cover), "check": True})
        assert checked.ok and "KDP would take this paperback as it is" in checked.text, (colour, checked.text)
    data = jail.read_bytes("books/journal-cover.pdf")
    back, front = images.cover_ink(data, (6.125, 6.125 + 0.27024), kdp.FRONT_DPI, kdp.FRONT_CONTRAST)
    assert back[0] and front[0] and back[1] is not None and front[1] is not None
    assert front[1][0] > 6.5 and back[1][2] < 5.7  # the words, not the folds


def test_an_ebook_is_proposed_and_kdp_s_weekly_limit_holds(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    jail = ctx.workspace
    manuscript = ebook(jail)
    words = {"format": "ebook", "price": "2.99", "paper": None, "low_content": None, "subtitle": None,
             "cover": {"front": "books/front.png"}, "project_id": project(agent)}  # fmt: skip
    first = proposed(agent, ctx, spec(jail, manuscript, "books/guide.json", **words))
    book = kdp.book_from_action(rows(agent, f"SELECT action FROM approvals WHERE id = {first}")[0]["action"])
    assert (book.format, book.trim, book.pages, book.manuscript.path) == ("ebook", "", 0, manuscript)
    assert book.cover.path == "books/guide-cover.jpg" and "at the 70% royalty" in kdp.royalty(book)
    ctx.state = tools.CycleTools()  # a new cycle's counts
    proposed(agent, ctx, spec(jail, manuscript, "books/guide-2.json", **{**words, "title": "A Second Guide"}))
    ctx.state = tools.CycleTools()
    third = call(ctx, "propose_kdp_book", {"spec": spec(jail, manuscript, "books/g3.json", **words, title="Third")})
    assert not third.ok and "KDP lets an account create at most 2 new ebooks a week" in third.text
    paper = call(ctx, "propose_kdp_book", {"spec": spec(jail, manuscript, "books/g4.json", **{**words, "paper": "x"})})
    assert not paper.ok and "paper and low_content are a paperback's" in paper.text
    back = call(ctx, "propose_kdp_book", {"spec": spec(jail, manuscript, "books/g5.json", **{
        **words, "cover": {"front": "books/front.png", "back": "No"}})})  # fmt: skip
    assert not back.ok and "an ebook's cover is its front alone: leave out cover.back and cover.spine" in back.text


def test_the_kdp_tool_and_manual_come_with_the_owner_s_option(data_dir: Path) -> None:
    names = {d["name"] for d in tools.definitions(kdp=True)}
    assert names >= tools.KDP_TOOLS == {"propose_kdp_book"}
    assert not {d["name"] for d in tools.definitions()} & tools.KDP_TOOLS
    assert not {d["name"] for d in tools.definitions(kdp=True, venture=True)} & tools.KDP_TOOLS
    topics = next(d for d in tools.definitions() if d["name"] == "guide")["input_schema"]["properties"]["topic"]
    assert "kdp" not in topics["enum"]
    assert (
        "kdp"
        in next(d for d in tools.definitions(kdp=True) if d["name"] == "guide")["input_schema"]["properties"]["topic"][
            "enum"
        ]
    )
    work = prompts.work_request(PUBLISHING, "brief", [], kdp=True)
    assert {d["name"] for d in work["tools"]} >= tools.KDP_TOOLS
    manual = tools.guide_text("kdp")
    assert "{KDP" not in manual and "{BOOK" not in manual and "at most 2 new titles of each format a week" in manual
    agent, ctx = publishing(data_dir)
    ctx.kdp = None
    off = call(ctx, "propose_kdp_book", {"spec": "books/journal.json"})
    assert not off.ok and "there is no tool called 'propose_kdp_book'" in off.text


def test_the_owner_hears_what_happens_to_a_book_in_their_words(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    request = proposed(agent, ctx, spec(ctx.workspace, paperback(ctx.workspace), project_id=project(agent)))
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    row = rows(agent, f"SELECT * FROM approvals WHERE id = {request}")[0]
    lines = news.News(decided=[row]).approval_lines()
    assert lines == [
        f'Request #{request} (sell) "KDP paperback: My Gratitude Journal": approved. Your owner publishes it at KDP '
        "and reports back with its link."
    ]


def test_a_cycle_with_kdp_on_shows_the_section_and_the_tool(data_dir: Path) -> None:
    fake = FakeTransport()
    run(data_dir, fake, settings=PUBLISHING)
    plans = [r for r in fake.sent if request_kind(r) == "plan"]
    works = [r for r in fake.sent if request_kind(r) == "work"]
    assert plans and works
    planner = json.dumps(plans[0]["messages"], ensure_ascii=False)
    assert "== KDP ==\\nYour owner publishes your books at KDP by hand (Amazon has no API)" in planner
    assert "Author name: set by your owner." in planner and "Jane Doe" not in planner  # their name stays home
    assert {t["name"] for t in works[0]["tools"]} >= tools.KDP_TOOLS


# --- From 0.40.2 (formerly test_fixes_0402.py) ---


# --- 3. KDP's week ---


def test_kdps_week_begins_on_sunday_at_midnight_utc() -> None:
    sunday = datetime(2026, 8, 30, tzinfo=UTC)
    assert kdp.week_start(datetime(2026, 9, 1, 12, tzinfo=UTC)) == sunday  # a Tuesday
    assert kdp.week_start(datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)) == sunday
    assert kdp.week_start(datetime(2026, 9, 6, tzinfo=UTC)) == sunday + timedelta(days=7)
    berlin = ZoneInfo("Europe/Berlin")
    assert kdp.week_start(datetime(2026, 9, 6, 1, 30, tzinfo=berlin)) == sunday  # Saturday 23:30 in UTC
    # live: the two books of Monday 10-19 held the Kassenbuch until Monday 10-26; KDP's week began on Sunday 10-25
    assert kdp.next_week(datetime(2026, 10, 21, 14, 57, tzinfo=UTC), berlin) == "Sunday 10-25 at 02:00"
    assert kdp.next_week(datetime(2026, 10, 21, 14, 57, tzinfo=UTC)) == "Sunday 10-25 at 00:00"


def test_the_weekly_limit_ends_with_kdps_week(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    agent.clock.current = datetime(2026, 9, 5, 22, tzinfo=UTC)  # a Saturday
    jail = ctx.workspace
    manuscript = ebook(jail)
    words = {"format": "ebook", "price": "2.99", "paper": None, "low_content": None, "subtitle": None,
             "cover": {"front": "books/front.png"}, "project_id": project(agent)}  # fmt: skip
    for n in (1, 2):
        ctx.state = tools.CycleTools()
        proposed(agent, ctx, spec(jail, manuscript, f"books/guide-{n}.json", **{**words, "title": f"Guide {n}"}))
    ctx.state = tools.CycleTools()
    third = spec(jail, manuscript, "books/guide-3.json", **{**words, "title": "Guide 3"})
    refused = call(ctx, "propose_kdp_book", {"spec": third})
    assert not refused.ok
    assert (
        "you proposed 2 ebooks this week (KDP's, from Sunday 00:00 UTC), and KDP lets an account create at most 2 new"
        " ebooks a week: propose the next one from Sunday 09-06 at 00:00"
    ) in refused.text
    with agent.db.connection() as conn:
        section = kdp.text(conn, agent.scope(), agent.clock.now(), "Jane Doe")
    assert "proposed this week: 2 ebooks, 0 paperbacks (the next week begins Sunday 09-06 at 00:00)." in section
    agent.clock.advance(hours=2)  # Sunday 00:00 UTC: KDP's next week
    proposed(agent, ctx, third)
    with agent.db.connection() as conn:
        section = kdp.text(conn, agent.scope(), agent.clock.now(), "Jane Doe")
    assert "proposed this week: 1 ebook, 0 paperbacks. Royalties" in section


# --- 8. a KDP book's link ---


def test_the_owner_adds_a_books_link_to_a_request_marked_done_without_one(data_dir: Path) -> None:
    """Live, both Haushaltsbuch books were marked done with "published" alone, and the daily review planned pins and
    posts "linking to Amazon" that nothing could make."""
    agent, ctx = publishing(data_dir)
    words = {"format": "ebook", "price": "2.99", "paper": None, "low_content": None, "subtitle": None,
             "cover": {"front": "books/front.png"}, "project_id": project(agent)}  # fmt: skip
    asked = proposed(agent, ctx, spec(ctx.workspace, ebook(ctx.workspace), "books/guide.json", **words))
    who = owner(agent)
    assert who.add_link(asked, {"result_link": "https://www.amazon.com/dp/B0TEST"}, "Owner").status == 409  # not done
    assert who.decide(asked, {"decision": "approve"}, "Owner").status == 200
    assert who.close(asked, {"outcome": "done", "result_note": "published"}, "Owner").status == 200
    with agent.db.connection() as conn:
        section = kdp.text(conn, agent.scope(), agent.clock.now(), "Jane Doe")
    assert (
        "published (no link at Amazon yet: your owner can add it on the request; until then nothing can link it)"
        in (section)
    )
    bad = who.add_link(asked, {"result_link": "amazon dot com"}, "Owner")
    assert bad.status == 422 and "plain http(s) link" in bad.body["error"]
    link = "https://www.amazon.com/dp/B0TESTBOOK"
    added = who.add_link(asked, {"result_link": link, "expected_version": 99}, "Owner")
    assert added.status == 409 and "reload the page" in added.body["error"]
    added = who.add_link(asked, {"result_link": link}, "Owner")
    assert added.status == 200 and added.body["approval"]["result_link"] == link
    assert added.body["approval"]["seen_cycle_id"] is None  # Ember reads it on its next wake
    with agent.db.connection() as conn:
        section = kdp.text(conn, agent.scope(), agent.clock.now(), "Jane Doe")
    assert f"published at {link}" in section and "no link at Amazon yet" not in section
    again = who.add_link(asked, {"result_link": link}, "Owner")
    assert again.status == 409 and "has its link already" in again.body["error"]
    other = request(agent, project(agent), "etsy_listing")
    assert who.add_link(other, {"result_link": link}, "Owner").status == 409
    assert any(f"added the link of request #{asked}" in e["message"] for e in agent.db.recent_events(5))
    assert "/api/approvals/{approval_id}/link" in {getattr(r, "path", "") for r in web.router.routes}
    script = (Path(__file__).parents[2] / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'panelButton(it, "link", "Add its Amazon link")' in script and 'url: url + "link"' in script
