"""0.17.0, upgrade request #4: resize_image, the workshop's resize script built into Ember.

The agent's workshop made the bauhaus poster's print files (3508 x 4961 for A3 at 300 dpi, and 2480 x 3508) from a
picture it had already paid for, with a script that cut the picture to the size's proportions at its centre and
resized it with Lanczos. Ember's code does the same now, free and without a run: exact pixels, nothing stretched,
300 dpi noted, what make_image noted the picture shows kept, and a report that says what was cut off and when a print
file is drawn so much larger than its picture that it may look soft.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.agent import tools
from app.agent.sandbox import Jail
from app.products import images, make
from tests.test_agent import make_agent, plan, rows, text
from tests.test_agent import tools as tool_calls

RED, BLUE = (220, 30, 30), (30, 60, 200)


def banded(width: int, height: int, band: int, fmt: str = "PNG") -> bytes:
    """A blue picture with red bands ``band`` pixels wide at its left and right."""
    picture = Image.new("RGB", (width, height), BLUE)
    picture.paste(RED, (0, 0, band, height))
    picture.paste(RED, (width - band, 0, width, height))
    buffer = io.BytesIO()
    picture.save(buffer, fmt)
    return buffer.getvalue()


def jail(tmp_path: Path) -> Jail:
    root = tmp_path / "workspace"
    root.mkdir()
    return Jail(root)


@pytest.mark.parametrize(
    ("size", "target", "kept"),
    [
        ((2000, 2000), (3508, 4961), (293, 0, 1707, 2000)),  # wider than A3: the sides are cut
        ((1000, 2000), (3508, 4961), (0, 293, 1000, 1707)),  # taller: the top and bottom
        ((2480, 3508), (2480, 3508), (0, 0, 2480, 3508)),  # the same proportions: nothing
        ((1, 5000), (5000, 1), (0, 2499, 1, 2500)),  # never less than a pixel
    ],
)
def test_the_centre_in_the_size_proportions_is_kept(size: tuple, target: tuple, kept: tuple) -> None:
    assert images.centre_part(*size, *target) == kept


def test_the_bauhaus_posters_print_files(tmp_path: Path) -> None:
    """The two sizes the workshop script made, from one picture: exact pixels, 300 dpi, the centre kept."""
    j = jail(tmp_path)
    j.write_bytes("workshop/out/bauhaus_poster_fixed.png", banded(2048, 2048, 200))
    for width, height in ((3508, 4961), (2480, 3508)):
        output = f"shop/bauhaus_poster_{width}x{height}.png"
        made = make.resize(j, "workshop/out/bauhaus_poster_fixed.png", output, width, height)
        assert made.paths == [output]
        picture = Image.open(io.BytesIO(j.read_bytes(output)))
        assert picture.format == "PNG" and picture.size == (width, height) and picture.mode == "RGB"
        assert tuple(round(v) for v in picture.info["dpi"]) == (300, 300)
        # 2048 x 2048 cut to 1:1.41 keeps 1448 pixels across: the red bands (200 each side) are gone
        assert picture.getpixel((5, height // 2)) == BLUE and picture.getpixel((width - 5, height // 2)) == BLUE
        report = made.text()
        assert report.startswith(f"Made {output}: {width} x {height} pixels, ")
        assert "from workshop/out/bauhaus_poster_fixed.png (2048 x 2048)" in report
        assert "29% of it was cut off at the left and right." in report


def test_a_print_file_says_when_it_may_look_soft(tmp_path: Path) -> None:
    j = jail(tmp_path)
    j.write_bytes("small.jpg", banded(700, 990, 0, "JPEG"))
    report = make.resize(j, "small.jpg", "shop/a3.png", 3508, 4961).text()
    assert "prints 29.7 x 42.0 cm" in report  # A3
    assert "cut off" not in report  # the same proportions, within a pixel
    assert "It is drawn 5.0 times larger than the picture" in report and "look at it first" in report
    assert "larger" not in make.resize(j, "small.jpg", "shop/thumb.png", 350, 495).text()


def test_what_a_photo_shows_stays_noted(tmp_path: Path) -> None:
    """The QA registry counts a print file of a photo as that photo, not a new one."""
    j = jail(tmp_path)
    j.write_bytes("shop/photo.png", images.marked(banded(1200, 900, 0), "poster:Bauhaus"))
    make.resize(j, "shop/photo.png", "shop/print.png", 2400, 1800)
    before, after = images.look(j.read_bytes("shop/photo.png")), images.look(j.read_bytes("shop/print.png"))
    assert after.partition(".")[0] == before.partition(".")[0] != ""
    j.write_bytes("plain.png", banded(1200, 900, 0))
    make.resize(j, "plain.png", "shop/plain-print.png", 600, 450)
    assert images.look(j.read_bytes("shop/plain-print.png")).startswith(".")


@pytest.mark.parametrize(
    ("source", "output", "width", "height", "message"),
    [
        ("pic.png", "shop/out.jpg", 1000, 1000, "output must be a path ending in .png"),
        ("notes.md", "shop/out.png", 1000, 1000, "source must be a .png or .jpg picture"),
        ("pic.png", "shop/out.png", 99, 1000, "width must be 100 to 10,000 pixels"),
        ("pic.png", "shop/out.png", 1000, 10_001, "height must be 100 to 10,000 pixels"),
        ("pic.png", "shop/out.png", 7016, 9933, "pic.png: the print file would be 7016 x 9933 = 69.7 MP, more than 40"),
        ("fake.png", "shop/out.png", 1000, 1000, "fake.png: it isn't a PNG or JPEG picture"),
    ],
)
def test_a_mistake_says_what_to_change(
    tmp_path: Path, source: str, output: str, width: int, height: int, message: str
) -> None:
    j = jail(tmp_path)
    j.write_bytes("pic.png", banded(400, 400, 0))
    j.write_bytes("fake.png", b"not a picture")
    j.write("notes.md", "# notes\n")
    with pytest.raises(make.ProductError, match=message):
        make.resize(j, source, output, width, height)
    assert j.size_of(output) is None


def test_the_tool_in_a_cycle(data_dir: Path) -> None:
    """Offered in ordinary cycles with the other making tools (sealed, outside the shared transaction), free."""
    assert "resize_image" in tools.MAKERS and "resize_image" in tools.ORDINARY_TOOLS
    assert "resize_image" not in tools.CALLING_TOOLS
    poster = {"source": "workshop/out/poster.png", "output": "shop/poster-a3.png", "width": 3508, "height": 4961}
    agent, _ = make_agent(
        data_dir,
        [
            plan(steps=["make the A3 print file"]),
            tool_calls(("resize_image", poster)),
            tool_calls(("resize_image", {**poster, "output": "shop/x.png", "width": 50})),
            text("Done."),
            text("Reflected."),
        ],
    )
    agent.roots()[0].write_bytes("workshop/out/poster.png", banded(1240, 1754, 0))
    agent.run_cycle("schedule")
    query = "SELECT status, summary, result FROM tool_calls WHERE tool = 'resize_image' ORDER BY id"
    done, refused = rows(agent, query)
    assert done["status"] == "ok" and done["summary"] == "made shop/poster-a3.png"
    assert done["result"].startswith("Made shop/poster-a3.png: 3508 x 4961 pixels")
    assert refused["status"] == "error" and "width" in refused["result"]
    picture = Image.open(io.BytesIO(agent.roots()[0].read_bytes("shop/poster-a3.png")))
    assert picture.size == (3508, 4961)
