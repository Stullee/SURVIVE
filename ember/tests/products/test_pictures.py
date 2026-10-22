"""The pictures Ember's code makes and reads: print-size posters within one picture limit, make_image's
zooms, text photos and posters, a listing's photos, a subtitle's lines and a region after a hash.

Tests from 0.15.0 and 0.19.2 (formerly test_fixes_0140_factory.py, test_fixes_0192.py)."""

from __future__ import annotations

import io
import json
import tracemalloc
from pathlib import Path

import pytest
from PIL import Image

from app.agent import tools
from app.agent.sandbox import Jail
from app.integrations import qa
from app.products import checks, images, make
from tests.products.test_product_tools import ctx_for, make_agent
from tests.products.test_products import CV, jail, spec

# --- From 0.15.0 (formerly test_fixes_0140_factory.py) ---


A3 = (3510, 4950)  # Printify's A3 print area: 17.4 MP


def png(width: int, height: int, colour: tuple[int, int, int] = (250, 245, 235)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, "PNG")
    return buffer.getvalue()


def a_cv(ws: Jail) -> None:
    ws.write("cv.md", CV)
    make.document(ws, "cv.md", "shop/cv.pdf")


# --- FIX NOW 6: one picture limit, and a refusal that says why ---------------------------------------------------


def test_a_print_size_poster_is_read_looked_at_and_kept_by_the_workshop_check() -> None:
    poster = png(*A3)
    assert images.png_size(poster) == A3
    small, width, height = images.thumbnail(poster, 1_000)
    assert (width, height) == (709, 1000) and Image.open(io.BytesIO(small)).size == (709, 1000)
    assert checks.check("out/poster.png", poster)  # the workshop's check and the tools agree
    assert images.MAX_PIXELS == 40_000_000 < Image.MAX_IMAGE_PIXELS  # Pillow's own guard never refuses first


def test_a_picture_over_the_limit_says_its_size() -> None:
    huge = png(7000, 7000)
    with pytest.raises(images.ImageError, match=r"7000 x 7000 = 49\.0 MP, more than 40 MP"):
        images.png_size(huge)
    with pytest.raises(checks.Refused, match=r"7000 x 7000 = 49\.0 MP, more than 40 MP"):
        checks.check("out/huge.png", huge)
    with pytest.raises(images.ImageError, match="isn't a PNG or JPEG"):
        images.png_size(b"not a picture")


def test_a_large_jpeg_is_reduced_while_it_is_decoded() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (6000, 6000), (10, 120, 200)).save(buffer, "JPEG", quality=70)
    tracemalloc.start()
    try:
        _, width, height = images.thumbnail(buffer.getvalue(), 1_000)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (width, height) == (1000, 1000)
    assert peak < 40_000_000  # decoded whole, 6000 x 6000 in RGB is 108 MB


def test_look_and_workspace_read_show_a_print_size_poster(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    ctx = ctx_for(agent)
    ctx.workspace.write_bytes("shop/poster.png", png(*A3))
    ctx.workspace.write_bytes("shop/huge.png", png(7000, 7000))
    looked = tools.HANDLERS["look"](ctx, {"path": "shop/poster.png"}, None)
    assert looked.ok and looked.text.startswith("shop/poster.png (709 x 1000 pixels, made ")  # 0.39.2: when
    assert "3510 x 4950 pixels" in tools.HANDLERS["workspace_read"](ctx, {"path": "shop/poster.png"}, None).text
    for name in ("look", "workspace_read"):
        with pytest.raises(tools.ToolError, match=r"shop/huge\.png can't be .*7000 x 7000 = 49\.0 MP"):
            tools.HANDLERS[name](ctx, {"path": "shop/huge.png"}, None)


def test_a_print_size_poster_can_be_proposed_to_printify(data_dir: Path) -> None:
    from tests.channels.test_printify import a_proposal, listed, pod_context, read_catalog

    agent, _ = listed(data_dir)
    ctx = pod_context(agent)
    read_catalog(ctx)
    agent.roots()[0].write_bytes("shop/huge.png", png(7000, 7000))
    refused = a_proposal(agent, ctx, image="shop/huge.png")
    assert not refused.ok and "shop/huge.png can't be used: it is 7000 x 7000 = 49.0 MP, more than 40 MP" in (
        refused.text
    )
    agent.roots()[0].write_bytes("shop/bauhaus.png", png(*A3))
    proposed = a_proposal(agent, ctx, image="shop/bauhaus.png")
    assert proposed.ok, proposed.text


# --- FIX NOW 12: the workshop's jobs, done by Ember's code for $0 -------------------------------------------------


def three_sheets(ws: Jail) -> None:
    data = json.loads(spec())
    data["sheets"].append({"name": "Costs", "columns": [{"title": "Cost"}], "rows": [[12], [30]]})
    data["sheets"].append({"name": "Summary", "columns": [{"title": "Total"}], "rows": [["=1+2"]]})
    ws.write("b.json", json.dumps(data))


def test_make_image_zooms_in_and_makes_text_photos_and_posters(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    a_cv(ws)
    whole = make.image(ws, "shop/whole.png", "shop/cv.pdf#1", "CV")
    top = make.image(ws, "shop/top.png", "shop/cv.pdf#1@top", "CV")
    assert whole.report[0].startswith("Made shop/whole.png: a landscape listing photo, 3000 x 2250 pixels")
    assert ws.read_bytes("shop/top.png") != ws.read_bytes("shop/whole.png") and top.paths == ["shop/top.png"]
    with pytest.raises(make.ProductError, match="the region must be one of top, middle"):
        make.image(ws, "shop/x.png", "shop/cv.pdf#1@corner", "CV")
    text = make.image(ws, "shop/text.png", "", "What's included", "CV in Word|CV in PDF|Cover letter", layout="text")
    assert text.report[0].startswith("Made shop/text.png: a landscape text listing photo, 3000 x 2250 pixels")
    poster = make.image(
        ws, "shop/poster.png", "", "Bauhaus", "Weimar 1919", accent="#C0392B", shape="portrait", layout="poster"
    )
    width, height = images.png_size(ws.read_bytes("shop/poster.png"))
    assert (width, height) == (4800, 6000) and "a poster, 4800 x 6000 pixels" in poster.report[0]
    assert width * height <= images.MAX_PIXELS and min(width / 11.7, height / 16.5) >= qa.SHARP_DPI  # A3 at 150 dpi
    tall = make.image(ws, "shop/pin.png", "", "Bauhaus", layout="poster", shape="pin")
    assert images.png_size(ws.read_bytes("shop/pin.png")) == (4000, 6000) and tall.paths == ["shop/pin.png"]
    with pytest.raises(make.ProductError, match="a poster layout shows no pages"):
        make.image(ws, "shop/x.png", "shop/cv.pdf#1", "CV", layout="poster")


def test_make_image_can_make_a_listings_photos_in_one_cycle() -> None:
    assert tools.SPECS["make_image"].per_cycle >= max(qa.MIN_PHOTOS, 10)


# --- From 0.19.2 (formerly test_fixes_0192.py) ---


# --- small things --------------------------------------------------------------------------------------------------


def test_a_subtitles_parts_begin_lines_of_their_own() -> None:
    draw = Image.new("RGB", (10, 10))
    from PIL import ImageDraw  # noqa: PLC0415

    font = images._font("sans", "", 40)
    lines = images._wrap(ImageDraw.Draw(draw), "Unlimited clients\nNo resale of files\nPlain English", font, 2000)
    assert lines == ["Unlimited clients", "No resale of files", "Plain English"]
    photo = images.listing([Image.new("RGB", (800, 1100), "white")], "Bundle", "One\nTwo\nThree\nFour\nFive")
    assert Image.open(io.BytesIO(photo)).size == images.SHAPES["landscape"]
