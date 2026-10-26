"""0.52.0, upgrade request #14: a book's front with its lines centred, the workshop's front script built into Ember.

The twin covers of "Haushaltsbuch 2027" and "Household Budget Book 2027" printed their titles off-centre (flush left,
on make_image's poster), and the owner saw it before a buyer did (case #c414). The agent drew both fronts again with
workshop/scripts/create-a-kdp-front-78.py: the title in the accent, a rule under it and the subtitle's lines at the
bottom, each at (width - text width) / 2, and a report of 0.00 px between each line's margins. That report came from
the numbers the script drew with: Pillow's textbbox begins at the pen, not at the ink, and on the pixels a line sat
up to 11 pixels off the middle of 4000. And a front picture centred to the pixel still printed 0.0625 in right of the
middle of the trimmed front: the wrap drew it from the spine's fold, with bleed at the outside edge only.

Now make_image's layout front centres each line by its ink and measures it on the pixels, the wrap centres the
front picture on the trimmed front, and propose_kdp_book refuses a front (a paperback's or an ebook's cover) whose
lines' margins differ by more than 0.2% of its width, so every KDP cover is centred by construction and checked.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.agent import tools
from app.agent.sandbox import Jail
from app.integrations import kdp, qa
from app.products import images, make, pdf
from tests.etsy_helpers import call
from tests.products.test_kdp import BACK, ebook, journal, paperback, publishing, spec

SAGE, GREEN = "#E8F0EA", "#2E7D52"  # the script's background and title colour
TITLE = "Haushaltsbuch 2027"
SUBTITLE = "Budgetplaner fürs ganze Jahr | Jahresübersicht · 12 Monate · 53 Wochen-Logs"


def jail(tmp_path: Path) -> Jail:
    root = tmp_path / "workspace"
    root.mkdir()
    return Jail(root)


def margins(picture: Image.Image, background: tuple[int, int, int]) -> list[tuple[int, int]]:
    """Each line's left and right margin in pixels, read from the picture."""
    return [(left, picture.width - right) for left, _, right, _ in images.ink_lines(picture, background, 48)]


def png(picture: Image.Image) -> bytes:
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue()


# --- the front make_image draws ------------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", images.SHAPE_NAMES)
def test_every_line_of_a_front_is_centred_by_its_ink(tmp_path: Path, shape: str) -> None:
    j = jail(tmp_path)
    made = make.image(j, "books/front.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, shape, "front")
    width, height = images.poster_size(shape)
    assert made.report[0].startswith(f"Made books/front.png: a book's front, {width} x {height} pixels ({shape}), ")
    picture = Image.open(io.BytesIO(j.read_bytes("books/front.png"))).convert("RGB")
    found = margins(picture, images.hex_rgb(SAGE))
    assert len(found) >= 5  # the title's lines, its rule and the subtitle's lines
    assert all(abs(left - right) <= 1 for left, right in found), found
    assert made.report[1] == (
        "Measured on its pixels, its title, rule and lines stand centred: the left and right margins of each of its "
        f"{len(found)} lines differ by at most 1 pixel of {width:,}. As a KDP book's cover.front, its centre is the "
        "trimmed front's."
    )


def test_the_scripts_centring_was_off_by_up_to_11_pixels() -> None:
    """The script's (width - text width) / 2 of Pillow's textbbox centres a line by its pen, not its ink: its report
    of 0.00 px was its own numbers. Ember's code centres by the ink and reads the pixels."""
    background = (232, 240, 234)
    font = images._font("display", "B", 480)
    for text, off in (("Ty", 11), ("2027", 6)):
        canvas = Image.new("RGB", (4000, 700), background)
        draw = ImageDraw.Draw(canvas)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((4000 - (box[2] - box[0])) / 2 - box[0], 50), text, font=font, fill=(46, 125, 82))
        [(left, right)] = margins(canvas, background)
        assert abs(left - right) >= off - 1, (text, left, right)
        left_ink, right_ink = images._ink(text, font)
        canvas = Image.new("RGB", (4000, 700), background)
        ImageDraw.Draw(canvas).text(((4000 - (right_ink - left_ink)) / 2 - left_ink, 50), text, font=font, fill=0)
        [(left, right)] = margins(canvas, background)
        assert abs(left - right) <= 1, (text, left, right)


def test_the_poster_stays_flush_left_and_a_front_is_its_own_photo(tmp_path: Path) -> None:
    j = jail(tmp_path)
    make.image(j, "shop/poster.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, "pin", "poster")
    make.image(j, "shop/front.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, "pin", "front")
    poster = Image.open(io.BytesIO(j.read_bytes("shop/poster.png"))).convert("RGB")
    lefts = {left for left, _ in margins(poster, images.hex_rgb(SAGE))}
    margin = int(4000 * images.POSTER_MARGIN)
    assert max(lefts) - min(lefts) <= margin * 0.1 and abs(min(lefts) - margin) <= margin * 0.1  # from its margin
    # QA tells the two apart by what they are, though they show the same words
    looks = images.looks(j.read_bytes, [("shop/poster.png", ""), ("shop/front.png", "")])
    assert not qa._alike(*looks)


def test_a_line_too_long_for_the_front_is_named(tmp_path: Path) -> None:
    j = jail(tmp_path)
    long = "Donaudampfschifffahrtsgesellschaftskapitänswitwenrentenversicherungsbescheid"
    made = make.image(j, "books/front.png", "", "Fahrtenbuch 2027", long, "", SAGE, GREEN, "pin", "front")
    # the title's two lines, its rule, then the word: as far from either edge as it runs off them
    assert made.report[1].startswith("Check: measured on its pixels, line 4 from the top is 0 pixels from the left")
    assert made.report[1].endswith("shorten its longest word, or the title.")


def test_a_front_shows_no_pages(tmp_path: Path) -> None:
    with pytest.raises(make.ProductError, match="a front layout shows no pages: leave pages empty"):
        make.image(jail(tmp_path), "books/front.png", "books/a.pdf#1", TITLE, layout="front")


# --- the wrap: the front picture's centre is the trimmed front's ---------------------------------------------------


def test_the_front_picture_is_centred_on_the_trimmed_front() -> None:
    """A block centred on its picture prints as far from the spine's fold as from the trim at the outside edge: drawn
    from the fold with bleed at the outside edge only, it printed 0.0625 in (19 pixels) right of the middle."""
    panels, _ = make._panels("6x9", 120, "white")
    for size in ((1250, 1850), (1225, 1850), (2000, 3000), (2400, 2400)):
        picture = Image.new("RGB", size, (250, 246, 238))
        w, h = size
        ImageDraw.Draw(picture).rectangle([w // 4, h // 10, w - w // 4 - 1, h // 3], fill=(0, 0, 0))
        wrap = images.book_wrap(png(picture), panels, [], "")
        front = wrap.picture.crop((panels.spine[1], 0, panels.trim[2], panels.size[1]))
        [(left, right)] = margins(front, (250, 246, 238))
        assert abs(left - right) <= 1, (size, left, right)
    # the shape nearest a front: with as much bleed at the spine, the same as before
    assert make.front_shape("6x9") == "pin" and make.front_shape("8.5x11") == "portrait"
    assert {make.front_shape(trim) for trim in kdp.TRIM_NAMES} == set(images.SHAPE_NAMES)


@pytest.mark.parametrize("trim", kdp.TRIM_NAMES)
def test_a_front_in_the_shape_nearest_its_trim_passes_at_every_trim(tmp_path: Path, trim: str) -> None:
    j = jail(tmp_path)
    interior = journal(j, "books/b", days=60, page=trim)
    make.image(j, "books/front.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, make.front_shape(trim), "front")
    make.kdp_cover(j, "books/b-cover.pdf", "books/front.png", interior, "white", BACK)
    pages = images.page_count(j.read_bytes(interior))
    fold = float(kdp.BLEED + kdp.trim_size(trim)[0] + kdp.spine_width(pages, "white"))
    lines = images.front_lines(j.read_bytes("books/b-cover.pdf"), fold, kdp.CENTRE_DPI, kdp.FRONT_CONTRAST)
    assert lines is not None and len(lines) >= 5
    assert kdp.check_centred(lines, trim, pages, "white") == []
    width = float(kdp.cover_size(trim, pages, "white")[0])
    worst = max(abs((left - fold) - (width - 0.125 - right)) for left, _, right, _ in lines)
    assert worst <= 0.007, worst  # two pixels at 300 dpi: the panels' inches rounded to pixels, and the measure's


# --- propose_kdp_book's check --------------------------------------------------------------------------------------


def test_kdp_refuses_a_front_whose_lines_are_flush_left(data_dir: Path) -> None:
    _, ctx = publishing(data_dir)
    j = ctx.workspace
    interior = paperback(j)
    make.image(j, "books/front.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, "pin", "poster")
    refused = call(ctx, "propose_kdp_book", {"spec": spec(j, interior), "check": True})
    assert not refused.ok
    assert refused.text.startswith(
        "Error: books/journal-cover.pdf: the cover's front doesn't centre its words: line 1 (0."
    ), refused.text
    assert " from the spine's fold and " in refused.text and " from the trim at the outside edge, " in refused.text
    assert "and 3 more lines: Ember's code takes 0.012 in apart at most (0.2% of its width): make books/front.png " in (
        refused.text
    )
    assert refused.text.endswith(
        "again with make_image's layout front (each line centred by its ink), then propose the book again."
    )
    make.image(j, "books/front.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, "pin", "front")
    checked = call(ctx, "propose_kdp_book", {"spec": spec(j, interior), "check": True})
    assert checked.ok and "KDP would take this paperback as it is" in checked.text, checked.text


def test_a_cover_file_of_its_own_is_held_to_the_same_rule(data_dir: Path) -> None:
    """A 6x9 cover of 120 pages on white is 12.52 x 9.25 in: the front's fold 6.395 in from its left edge, its trim at
    the outside edge 12.395 in."""
    _, ctx = publishing(data_dir)
    j = ctx.workspace
    interior = paperback(j)
    wrap = Image.new("RGB", (2504, 1850), (255, 255, 255))
    ImageDraw.Draw(wrap).rectangle([round(7.0 * 200), 200, round(11.5 * 200) - 1, 400], fill=(0, 0, 0))
    j.write_bytes("books/own.pdf", pdf.picture_page(images.jpeg(wrap), 12.52 * 72, 9.25 * 72, "My Journal"))
    own = call(ctx, "propose_kdp_book", {"spec": spec(j, interior, cover="books/own.pdf"), "check": True})
    assert not own.ok
    assert own.text == (
        "Error: books/own.pdf: the cover's front doesn't centre its words: line 1 (1 in from the top) prints 0.605 in "
        "from the spine's fold and 0.895 in from the trim at the outside edge, 0.29 in apart: Ember's code takes "
        "0.012 in apart at most (0.2% of its width): centre each line."
    )
    ImageDraw.Draw(wrap).rectangle([0, 0, 2503, 1849], fill=(255, 255, 255))
    ImageDraw.Draw(wrap).rectangle([round(7.0 * 200), 200, round(11.79 * 200) - 1, 400], fill=(0, 0, 0))
    j.write_bytes("books/own.pdf", pdf.picture_page(images.jpeg(wrap), 12.52 * 72, 9.25 * 72, "My Journal"))
    assert call(ctx, "propose_kdp_book", {"spec": spec(j, interior, cover="books/own.pdf"), "check": True}).ok


def test_an_ebook_s_cover_is_held_to_it_too(data_dir: Path) -> None:
    _, ctx = publishing(data_dir)
    j = ctx.workspace
    manuscript = ebook(j)
    words = {"format": "ebook", "price": "2.99", "paper": None, "low_content": None, "cover": {"front": "books/f.png"}}
    make.image(j, "books/f.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, "pin", "poster")
    refused = call(ctx, "propose_kdp_book", {"spec": spec(j, manuscript, "books/g.json", **words), "check": True})
    assert not refused.ok
    assert (
        refused.text.startswith("Error: books/g-cover.jpg: the cover doesn't centre its words: line 1 (")
        and "pixels from the left edge and " in refused.text
    ), refused.text
    assert "Ember's code takes 3 pixels apart at most (0.2% of its width): make books/f.png again" in refused.text
    make.image(j, "books/f.png", "", TITLE, SUBTITLE, "", SAGE, GREEN, "pin", "front")
    checked = call(ctx, "propose_kdp_book", {"spec": spec(j, manuscript, "books/g.json", **words), "check": True})
    assert checked.ok, checked.text
    assert "Note:" not in checked.text  # a plain cover: nothing to look at for its lines
    # a cover whose art reaches its edges is a note: Ember's code can't tell its words from it
    art = Image.linear_gradient("L").resize((1600, 2560)).convert("RGB")
    j.write_bytes("books/f.png", png(art))
    noted = call(ctx, "propose_kdp_book", {"spec": spec(j, manuscript, "books/g.json", **words), "check": True})
    assert noted.ok and (
        "Note: the cover's picture reaches its edges, so Ember's code can't tell its words from it: look at it, each "
        "line centred." in noted.text
    ), noted.text


def test_the_check_s_numbers() -> None:
    """kdp.check_centred: margins from the front's fold (6.395 in on a 6x9 cover of 120 pages on white) and its trim
    at the outside edge (12.395 in), 0.012 in apart at most; a line the trim or the fold cuts is the safe zone's."""
    assert kdp.check_centred([(7.0, 1.0, 11.79, 1.5)], "6x9", 120, "white") == []
    assert kdp.check_centred([(7.0, 1.0, 11.785, 1.5), (7.2, 2.0, 11.59, 2.4)], "6x9", 120, "white") == []
    assert kdp.check_centred([(7.0, 1.0, 11.5, 1.5)], "6x9", 120, "white") == [
        "the cover's front doesn't centre its words: line 1 (1 in from the top) prints 0.605 in from the spine's fold "
        "and 0.895 in from the trim at the outside edge, 0.29 in apart: Ember's code takes 0.012 in apart at most "
        "(0.2% of its width)"
    ]
    beyond = [(7.0, 0.05, 12.0, 0.5), (6.0, 1.0, 11.0, 1.5), (7.0, 9.0, 11.0, 9.2)]  # the top, the fold, the bottom
    assert kdp.check_centred(beyond, "6x9", 120, "white") == []
    many = [(6.5, top, 11.0, top + 0.2) for top in (1.0, 2.0, 3.0, 4.0, 5.0)]
    assert kdp.check_centred(many, "6x9", 120, "white")[0].endswith(
        "line 3 (3 in from the top) prints 0.105 in from the spine's fold and 1.395 in from the trim at the outside "
        "edge, 1.29 in apart and 2 more lines: Ember's code takes 0.012 in apart at most (0.2% of its width)"
    )
    assert kdp.check_ebook_centred(1600, [(400, 300, 1200, 400)]) == []
    assert kdp.check_ebook_centred(1600, [(400, 300, 1196, 400)]) == [
        "the cover doesn't centre its words: line 1 (300 pixels from the top) prints 400 pixels from the left edge and "
        "404 pixels from the right, 4 pixels apart: Ember's code takes 3 pixels apart at most (0.2% of its width)"
    ]


# --- the tool and its manuals --------------------------------------------------------------------------------------


def test_the_tool_and_the_manuals_offer_it(data_dir: Path) -> None:
    _, ctx = publishing(data_dir)
    made = call(
        ctx,
        "make_image",
        {"output": "books/front.png", "title": TITLE, "subtitle": SUBTITLE, "layout": "front", "shape": "pin"},
    )
    assert made.ok and "a book's front, 4000 x 6000 pixels (pin)" in made.text and "stand centred" in made.text
    spec = {d["name"]: d for d in tools.definitions()}["make_image"]
    assert "front" in spec["input_schema"]["properties"]["layout"]["enum"]
    assert "a print-size poster or book front" in spec["description"]
    flat = {topic: " ".join(tools.guide_text(topic).split()) for topic in ("kdp", "listing_photos", "workshop")}
    assert "The front: make_image, layout front (each line centred: Ember's code checks), shape pin" in flat["kdp"]
    assert "front (no pages: a book's front, the poster with each line and its rule centred" in flat["listing_photos"]
    assert "a book's front with its lines centred (layout front)" in flat["workshop"]
    assert all(len(tools.guide_text(topic)) <= tools.MAX_RESULT_CHARS for topic in flat)
