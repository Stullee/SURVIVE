"""0.24.0: Amazon KDP, phase A: books the agent makes and the owner publishes at KDP from their own account (Amazon
has no API for KDP). Ember's code checks the package against KDP's rules: the words, the price, a paperback's interior
(its trim, page count and margins) and its full cover (as wide as the pages make the spine), an ebook's Word file and
its JPEG cover; the card gives the owner every field and file, and the plan lists the books."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.agent import never, news, obligations, prompts, tools
from app.agent.fake_llm import FakeTransport, request_kind
from app.agent.sandbox import Jail
from app.economy.clock import to_iso
from app.integrations import connectors, kdp
from app.products import images, make, markup, pdf
from tests.test_agent import ROOMY, rows
from tests.test_etsy import call, views_approval
from tests.test_loop_shapes import run
from tests.test_owner_loop import owner

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


def proposed(agent: Any, ctx: tools.ToolContext, path: str) -> int:
    made = call(ctx, "propose_kdp_book", {"spec": path})
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
    assert kdp.printing_cost(110, "white", "6x9") == Decimal("2.30")  # flat up to 110 pages
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
    made = call(ctx, "propose_kdp_book", {"spec": book_spec})
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
    assert "proposed in the last 7 days: 0 ebooks, 1 paperback." in section


def test_check_makes_the_cover_and_checks_without_asking(data_dir: Path) -> None:
    agent, ctx = publishing(data_dir)
    book_spec = spec(ctx.workspace, paperback(ctx.workspace), cover={"front": "books/front.png", "spine": "Spine"})
    checked = call(ctx, "propose_kdp_book", {"spec": book_spec, "check": True})
    assert checked.ok, checked.text
    assert "Checked books/journal.json: KDP would take this paperback as it is." in checked.text
    assert "Nothing went to your owner: propose it with check false." in checked.text
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
