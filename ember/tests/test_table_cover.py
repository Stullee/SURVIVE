"""0.38.0, upgrade request #10: make_image's layout table, the workshop's cover script built into Ember.

The agent's workshop drew the Nebenkostenabrechnung's v9 and v10 covers with workshop/scripts/create-a-single-png-41.py:
the title and subtitle on a dark blue band across the top 30%, and under it a table of four columns, full bleed, with
a header row on the same blue, rows in turn white and tinted, and grid lines. Each fix of such a cover's words or
numbers was a workshop run of up to $2 against the $7 daily cap, so on a day at $5.60 a cover the critic flagged
waited for the next day. make_image draws it now, free, from the agent's lines of cells, and adds up each row named
Summe, Gesamt or Total, so a cover's sum can't disagree with its rows unnoticed.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.agent import tools
from app.agent.sandbox import Jail
from app.integrations import qa
from app.products import images, make, tables
from tests.test_agent import make_agent, plan, rows, text
from tests.test_agent import tools as tool_calls

# The script's cover, cell for cell: its title, subtitle, columns and rows (the Summe row bold).
TITLE = "NEBENKOSTENABRECHNUNG"
SUBTITLE = "Excel-Vorlage · automatische Umlage · BetrKV"
SCRIPT = """Mieter | m² | Anteil % | Kostenanteil €
EG links | 90 | 30% | 1.462,50 €
1.OG | 120 | 40% | 1.910,00 €
Summe | 210 | 70% | 3.372,50 €"""
DARK_BLUE = (44, 74, 110)  # the script's band and header
WHITE = (255, 255, 255)


def jail(tmp_path: Path) -> Jail:
    root = tmp_path / "workspace"
    root.mkdir()
    return Jail(root)


def fills(picture: Image.Image, x: int, accent: tuple[int, int, int] = DARK_BLUE) -> list[tuple[int, ...]]:
    """The colours down the column ``x``, each once where it repeats, the grid's lines (the accent's shade) left
    out."""
    grid = images._mix(accent, (0, 0, 0), 0.55)
    seen: list[tuple[int, ...]] = []
    for y in range(picture.height):
        colour = picture.getpixel((x, y))
        if colour != grid and (not seen or seen[-1] != colour):
            seen.append(colour)
    return seen


def test_the_workshop_scripts_cover(tmp_path: Path) -> None:
    """The script's cover at its square shape: the band and the header on the accent, the rows white and tinted in
    turn, the Summe row on a deeper tint; its words are what it shows, for QA."""
    j = jail(tmp_path)
    made = make.image(j, "shop/nk-cover-v11.png", "", TITLE, SUBTITLE, "", None, "#2C4A6E", "square", "table", SCRIPT)
    assert made.paths == ["shop/nk-cover-v11.png"]
    picture = Image.open(io.BytesIO(j.read_bytes("shop/nk-cover-v11.png")))
    assert picture.format == "PNG" and picture.size == (2400, 2400) and picture.mode == "RGB"
    zebra, deeper = images._mix(WHITE, DARK_BLUE, 0.1), images._mix(WHITE, DARK_BLUE, 0.24)
    # left of every text: the band and header (one blue between their grid line), then the three rows
    assert fills(picture.convert("RGB"), 30) == [DARK_BLUE, WHITE, zebra, deeper]
    assert made.report == [
        "Made shop/nk-cover-v11.png: a square table cover, 2400 x 2400 pixels, "
        f"{len(j.read_bytes('shop/nk-cover-v11.png')) / 1024:.0f} KB.",
        "Its table: 3 rows under the header, 4 columns; Ember's code added up its total row.",
    ]
    look = images.look(j.read_bytes("shop/nk-cover-v11.png"))
    assert look.startswith("table-")


@pytest.mark.parametrize("shape", images.SHAPE_NAMES)
def test_every_shape(tmp_path: Path, shape: str) -> None:
    j = jail(tmp_path)
    make.image(j, "cover.png", "", TITLE, SUBTITLE, "Sofort-Download", shape=shape, layout="table", table=SCRIPT)
    assert Image.open(io.BytesIO(j.read_bytes("cover.png"))).size == images.SHAPES[shape]


def test_a_light_accent_keeps_its_rows_apart(tmp_path: Path) -> None:
    """A light accent's own tints barely show on white: the rows are tinted with its shade."""
    j = jail(tmp_path)
    make.image(j, "cover.png", "", TITLE, SUBTITLE, "", None, "#F4D35E", "square", "table", SCRIPT)
    picture = Image.open(io.BytesIO(j.read_bytes("cover.png"))).convert("RGB")
    yellow = (244, 211, 94)
    shade = images._mix(yellow, (0, 0, 0), 0.55)
    zebra, deeper = images._mix(WHITE, shade, 0.1), images._mix(WHITE, shade, 0.24)
    assert fills(picture, 30, yellow) == [yellow, WHITE, zebra, deeper]


def test_the_sums_are_checked() -> None:
    """A total row's numbers are added up as shown: wrong ones are named with the right sum, in the cell's own
    notation, currency and unit."""
    assert tables.mismatches(tables.parse(SCRIPT)) == []
    wrong = SCRIPT.replace("Summe | 210 | 70% | 3.372,50 €", "Summe | 300 | 100% | 3.372,49 €")
    assert tables.mismatches(tables.parse(wrong)) == [
        "Summe shows 300 under m², but its rows above add up to 210",
        "Summe shows 100% under Anteil %, but its rows above add up to 70%",
        "Summe shows 3.372,49 € under Kostenanteil €, but its rows above add up to 3.372,50 €",
    ]
    english = tables.parse("Item | Cost\nA | $1,234.50\nB | $15.25\nTotal | $1,249.70")
    assert tables.mismatches(english) == ["Total shows $1,249.70 under Cost, but its rows above add up to $1,249.75"]


@pytest.mark.parametrize(
    ("table", "found"),
    [
        # no number shows its notation: 1.500 is one and a half thousand, or one and a half
        ("Item | Menge\nA | 1.500\nB | 2.500\nGesamt | 4.000", []),
        (
            "Item | Menge\nA | 1.500\nB | 2.500\nGesamt | 4.001",
            ["Gesamt shows 4.001 under Menge, but its rows above add up to 4.000"],
        ),
        # a rate between its rows' least and greatest is no sum
        ("Monat | Gespart | Quote\nJan | 469,65 € | 19%\nFeb | 520,00 € | 21%\nSumme | 989,65 € | 20%", []),
        # a grand total under subtotals adds up every row that is no total
        ("Posten | Betrag\nA | 10,00 €\nB | 5,50 €\nZwischensumme | 15,50 €\nC | 4,50 €\nGesamt | 20,00 €", []),
        (
            "Posten | Betrag\nA | 10,00 €\nB | 5,50 €\nZwischensumme | 15,50 €\nC | 4,50 €\nGesamt | 20,50 €",
            [
                "Gesamt shows 20,50 € under Betrag, but the rows since Zwischensumme add up to 4,50 €, all rows above "
                "to 20,00 €"
            ],
        ),
        # words, an empty cell or a first row named Summe: nothing to add up
        ("Plan | Preis\nBasic | ab 5 €\nPro | 12 €\nSumme | 17 €", []),
        ("Plan | Preis | Notiz\nBasic | 5 € |\nPro | 12 € | beliebt\nSumme | 17 € |", []),
        ("Summe | Preis\nSumme | 5 €\nA | 12 €", []),
        ("Jahr | Preis\n2025 | -5,00 €\n2026 | −7,50 €\nGesamt | -12,50 €", []),
    ],
)
def test_what_the_check_adds_up(table: str, found: list[str]) -> None:
    assert tables.mismatches(tables.parse(table)) == found


def test_a_wrong_sum_is_named_in_the_report(tmp_path: Path) -> None:
    """The photo is made (a total may be meant otherwise); the Check line says what differs and what to do."""
    j = jail(tmp_path)
    wrong = SCRIPT.replace("| 210 |", "| 300 |")
    report = make.image(j, "cover.png", "", TITLE, SUBTITLE, layout="table", table=wrong).text()
    assert j.size_of("cover.png") > 0
    assert (
        "Check: Summe shows 300 under m², but its rows above add up to 210. Copy the numbers from your product's "
        "file, or name a row that isn't their sum for what it is (e.g. 'Ganzes Haus')." in report
    )


def test_a_markdown_table_reads_the_same() -> None:
    markdown = tables.parse(
        "| Mieter | m² | Anteil % | Kostenanteil € |\n|---|--:|:-:|---:|\n| EG links | 90 | 30% | 1.462,50 € |\n"
        "| 1.OG | 120 | 40% | 1.910,00 € |\n| **Summe** | **210** | **70%** | **3.372,50 €** |"
    )
    assert markdown == tables.parse(SCRIPT)
    assert markdown.totals == frozenset({2}) and markdown.cells[-4:] == ["Summe", "210", "70%", "3.372,50 €"]


@pytest.mark.parametrize(
    ("table", "layout", "pages", "message"),
    [
        ("", "table", "", "layout table needs table: one row a line, cells split by |"),
        (SCRIPT, "photo", "shop/cv.pdf", "only layout table shows a table"),
        (SCRIPT, "text", "", "only layout table shows a table"),
        (SCRIPT, "table", "shop/cv.pdf", "a table layout shows no pages"),
        ("Mieter | m²", "table", "", "table needs its header line and at least one row under it"),
        ("A | B\n" + "x | 1\n" * 9, "table", "", "table has 9 rows under its header: at most 8"),
        ("A\nx", "table", "", "table's header has 1 cell: give 2 to 5 columns"),
        ("A | B | C | D | E | F\n1 | 2 | 3 | 4 | 5 | 6", "table", "", "table's header has 6 cells"),
        ("| | |\n| x | 1 |", "table", "", "table's header is empty: name its columns"),
        ("A | B\nx | 1 | 2", "table", "", r"table's row 1 \(x \| 1 \| 2\) has 3 cells, its header 2: give each row"),
        ("A | B\nx | " + "9" * 41, "table", "", "has 41 characters: at most 40"),
        ("A | B\nx | ✓", "table", "", "the table has characters Ember's fonts can't draw"),
        ("A | B | C | D | E\n" + " | ".join(["W" * 40] * 5), "table", "", "the table is too wide to read on a"),
    ],
)
def test_a_mistake_says_what_to_change(tmp_path: Path, table: str, layout: str, pages: str, message: str) -> None:
    j = jail(tmp_path)
    with pytest.raises(make.ProductError, match=message):
        make.image(j, "cover.png", pages, TITLE, SUBTITLE, layout=layout, table=table)
    assert j.size_of("cover.png") is None


def test_qa_tells_table_covers_apart_by_their_words(tmp_path: Path) -> None:
    """Two table covers with other numbers are two photos, whatever their pixels; the same one again is a copy."""
    j = jail(tmp_path)
    other = SCRIPT.replace("EG links", "EG rechts")
    for name, table in (("a.png", SCRIPT), ("b.png", other), ("c.png", SCRIPT)):
        make.image(j, name, "", TITLE, SUBTITLE, layout="table", table=table)
    a, b, c = (images.look(j.read_bytes(name)) for name in ("a.png", "b.png", "c.png"))
    assert not qa._alike(a, b) and qa._alike(a, c)


def test_the_tool_in_a_cycle(data_dir: Path) -> None:
    """make_image takes table (layout table needs no word with it), free and sealed like the other making tools; the
    workshop's guide sends covers to it."""
    spec = tools.SPECS["make_image"]
    assert "table" in images.LAYOUTS and "table" in spec.fields and not spec.fields["table"].required
    assert "covers with a small table (layout table)" in tools.guide_text("workshop").replace("\n", " ")
    assert "A COVER WITH A SMALL TABLE (layout table" in tools.guide_text("listing_photos")
    call = {"output": "shop/nk-cover-v11.png", "title": TITLE, "subtitle": SUBTITLE, "shape": "square"}
    wrong = SCRIPT.replace("1.910,00 €", "1.900,00 €")
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["fix the Nebenkostenabrechnung's cover"]),
            tool_calls(
                ("make_image", {**call, "table": SCRIPT}),
                ("make_image", {**call, "output": "shop/v12.png", "table": wrong, "layout": "table"}),
                ("make_image", {**call, "output": "shop/v13.png", "table": SCRIPT, "layout": "photo"}),
            ),
            text("Done."),
            text("Reflected."),
        ],
    )
    agent.run_cycle("schedule")
    query = "SELECT status, summary, result FROM tool_calls WHERE tool = 'make_image' ORDER BY id"
    done, checked, refused = rows(agent, query)
    assert done["status"] == "ok" and done["summary"] == "made shop/nk-cover-v11.png"
    assert done["result"].startswith("Made shop/nk-cover-v11.png: a square table cover, 2400 x 2400 pixels")
    assert "Check" not in done["result"]
    assert checked["status"] == "ok" and "Check: Summe shows 3.372,50 € under Kostenanteil €" in checked["result"]
    assert refused["status"] == "error" and "only layout table shows a table" in refused["result"]
    workspace = agent.roots()[0]
    assert workspace.size_of("shop/nk-cover-v11.png") > 0 and workspace.size_of("shop/v13.png") is None
