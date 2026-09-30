"""0.14.0, the product factory: print-size pictures (FIX NOW 6), the workshop's jobs done by Ember's code (FIX NOW
12), the workshop's PDF check (FIX NOW 22) and Office files that unpack to too much (FIX NOW 26).
"""

from __future__ import annotations

import base64
import io
import json
import struct
import tracemalloc
import zipfile
import zlib
from pathlib import Path

import pypdfium2
import pypdfium2.raw as pdfium_c
import pytest
from PIL import Image

from app.agent import library, netguard, tools
from app.agent.sandbox import Jail
from app.integrations import etsy, qa
from app.products import checks, images, make, sheets
from tests.test_etsy import a_listing
from tests.test_owner_loop import owner
from tests.test_product_tools import ctx_for, make_agent
from tests.test_products import CV, jail, spec
from tests.test_workshop import a_docx, with_parts
from tests.test_workshop_checks import a_pdf_with, stream

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
    assert looked.ok and looked.text.startswith("shop/poster.png (709 x 1000 pixels)")
    assert "3510 x 4950 pixels" in tools.HANDLERS["workspace_read"](ctx, {"path": "shop/poster.png"}, None).text
    for name in ("look", "workspace_read"):
        with pytest.raises(tools.ToolError, match=r"shop/huge\.png can't be .*7000 x 7000 = 49\.0 MP"):
            tools.HANDLERS[name](ctx, {"path": "shop/huge.png"}, None)


def test_a_print_size_poster_can_be_proposed_to_printify(data_dir: Path) -> None:
    from tests.test_printify import a_proposal, listed, pod_context, read_catalog

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


def test_a_spreadsheet_comes_with_a_picture_of_each_sheet(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    three_sheets(ws)
    made = make.spreadsheet(ws, "b.json", "shop/b.xlsx")
    assert made.paths == ["shop/b.xlsx", "shop/b-preview.png", "shop/b-sheet2.png", "shop/b-sheet3.png"]
    assert (
        "Pictures of its sheets: shop/b-preview.png (Budget), shop/b-sheet2.png (Costs), shop/b-sheet3.png"
        in (made.report[1])
    )
    assert images.png_size(ws.read_bytes("shop/b-sheet2.png")) != images.png_size(ws.read_bytes("shop/b-sheet3.png"))
    data = json.loads(ws.read("b.json"))
    data["sheets"] = data["sheets"][:1]
    ws.write("b.json", json.dumps(data))
    fewer = make.spreadsheet(ws, "b.json", "shop/b.xlsx")  # a sheet it no longer has loses its picture
    assert fewer.removed == ["shop/b-sheet2.png", "shop/b-sheet3.png"] and ws.size_of("shop/b-sheet2.png") is None


def test_make_image_shows_a_sheet_by_number_or_name(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    three_sheets(ws)
    make.spreadsheet(ws, "b.json", "shop/b.xlsx")
    made = make.image(ws, "shop/photo.png", "shop/b.xlsx#Costs, shop/b.xlsx#4", "Three sheets")
    assert made.paths == ["shop/photo.png"]
    with pytest.raises(make.ProductError, match=r"shop/b\.xlsx#9: the workbook has no sheet '9'; its sheets are 1 "):
        make.image(ws, "shop/photo.png", "shop/b.xlsx#9", "Title")


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
    write = tools.HANDLERS["workspace_write"]
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


def uploads(ws: Jail, names: list[str]) -> tuple[etsy.Upload, ...]:
    return tuple(etsy.upload(n, ws.read_bytes(n), frozenset({".png", ".jpg"}), "photos") for n in names)


def test_qa_counts_distinct_photos_and_names_the_copies(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    a_cv(ws)
    same = []
    for number, title in enumerate(("Modern CV", "Easy to edit", "Stand out", "Recruiter approved", "A4 + Letter")):
        make.image(ws, f"shop/same{number}.png", "shop/cv.pdf#1", title, badge="Instant download")
        same.append(f"shop/same{number}.png")
    photos = uploads(ws, same)
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in photos])
    short = qa.defects("etsy.create_listing", a_listing(photos=photos), looks)
    assert short == [
        "1 distinct photo, fewer than 5 (Etsy shows up to 10); shop/same1.png repeats shop/same0.png, shop/same2.png "
        "repeats shop/same0.png, shop/same3.png repeats shop/same0.png, shop/same4.png repeats shop/same0.png: a copy "
        "adds no photo"
    ]
    # Five different photos of a one-page product: the page, two details, what is included and what it does.
    make.image(ws, "shop/top.png", "shop/cv.pdf#1@top", "Modern CV")
    make.image(ws, "shop/bottom.png", "shop/cv.pdf#1@bottom-left", "Modern CV")
    make.image(ws, "shop/included.png", "", "What's included", "CV in Word|CV in PDF", layout="text")
    make.image(ws, "shop/features.png", "", "Easy to edit", "Change colours|Free fonts", layout="text")
    distinct = uploads(
        ws, ["shop/same0.png", "shop/top.png", "shop/bottom.png", "shop/included.png", "shop/features.png"]
    )
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in distinct])
    assert qa.defects("etsy.create_listing", a_listing(photos=distinct), looks) == []
    # A copy made elsewhere (smaller, as a JPEG) looks the same; the same file is a copy without looking.
    copy = io.BytesIO()
    Image.open(io.BytesIO(ws.read_bytes("shop/included.png"))).convert("RGB").resize((1500, 1125)).save(copy, "JPEG")
    ws.write_bytes("shop/copy.jpg", copy.getvalue())
    with_copy = (*distinct, *uploads(ws, ["shop/copy.jpg"]))
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in with_copy])
    assert qa.repeats(with_copy, looks) == ["shop/copy.jpg repeats shop/included.png"]
    assert qa.repeats((photos[0], photos[0])) == ["shop/same0.png repeats shop/same0.png"]


def test_the_owners_card_names_repeated_photos(data_dir: Path) -> None:
    from app.agent import views

    agent, _ = make_agent(data_dir, [])
    ws = agent.roots()[0]
    a_cv(ws)
    for number in range(2):
        make.image(ws, f"shop/p{number}.png", "shop/cv.pdf#1", f"Title {number}")
    photos = uploads(ws, ["shop/p0.png", "shop/p1.png"])
    waiting, decided = {"status": "pending"}, {"status": "approved"}
    assert views._looks(agent, waiting, photos) == images.looks(ws.read_bytes, [(p.path, p.sha256) for p in photos])
    assert qa.repeats(photos, views._looks(agent, waiting, photos)) == ["shop/p1.png repeats shop/p0.png"]
    assert views._looks(agent, decided, photos) is None  # a decided request's photos aren't read again


# --- FIX NOW 22: the workshop's PDF check ----------------------------------------------------------------------


def objects_in_a_stream(filter_value: bytes = b"/FlateDecode", line_end: bytes = b"\n", script: bool = True) -> bytes:
    """A one-page PDF whose catalog and pages (and with ``script``, a JavaScript action it runs when it opens) sit in
    a compressed object stream, found through a cross-reference stream whose rows are PNG-predicted."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R"
        + (b" /OpenAction 4 0 R /Names << /JavaScript 5 0 R >>" if script else b"")
        + b" >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        4: b"<< /S /JavaScript /JS (app.alert\\(1\\)) >>" if script else b"<< /Kind /Nothing >>",
        5: b"<< /Names [(a) 4 0 R] >>" if script else b"<< >>",
    }
    offsets, body = [], b""
    for number, text in objects.items():
        offsets.append(b"%d %d" % (number, len(body)))
        body += text + b"\n"
    head = b" ".join(offsets) + b"\n"
    packed = zlib.compress(head + body)
    out = b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n"
    at = {6: len(out)}
    out += b"6 0 obj\n<< /Type /ObjStm /N 5 /First %d /Length %d /Filter %s >>\nstream" % (
        len(head),
        len(packed),
        filter_value,
    )
    out += line_end + packed + b"\nendstream\nendobj\n"
    at[9] = len(out)
    out += b"9 0 obj\n/FlateDecode\nendobj\n"
    at[10] = len(out)
    rows = [b"\x00" + bytes(4) + b"\xff\xff"]
    rows += [b"\x02" + struct.pack(">IH", 6, n) for n in range(5)]
    rows += [b"\x01" + struct.pack(">IH", at[6], 0), b"\x00" * 7, b"\x00" * 7]
    rows += [b"\x01" + struct.pack(">IH", at[9], 0), b"\x01" + struct.pack(">IH", at[10], 0)]
    predicted, previous = b"", bytes(7)
    for row in rows:  # PNG "up" rows, as qpdf and others write them
        predicted += b"\x02" + bytes((a - b) & 255 for a, b in zip(row, previous, strict=True))
        previous = row
    xref = zlib.compress(predicted)
    out += (
        b"10 0 obj\n<< /Type /XRef /Size 11 /W [1 4 2] /Root 1 0 R /Filter /FlateDecode "
        b"/DecodeParms << /Columns 7 /Predictor 12 >> /Length %d >>\nstream\n" % len(xref)
    )
    out += xref + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % at[10]
    return out


def scripts_in(data: bytes) -> int:
    document = pypdfium2.PdfDocument(data)
    try:
        assert len(document) == 1
        return pdfium_c.FPDFDoc_GetJavaScriptActionCount(document.raw)
    finally:
        document.close()


@pytest.mark.parametrize(
    ("filter_value", "line_end"),
    [(b"9 0 R", b"\n"), (b"[9 0 R]", b"\n"), (b"/FlateDecode", b"\r"), (b"/Fl#61teDecode", b"\r")],
)
def test_javascript_in_an_object_stream_is_found_however_it_is_written(filter_value: bytes, line_end: bytes) -> None:
    data = objects_in_a_stream(filter_value, line_end)
    assert scripts_in(data) == 1  # a viewer runs it
    with pytest.raises(checks.Refused, match="active content|names a stream's encoding in a way Ember can't check"):
        checks.check("guide.pdf", data)


def predicted(text: bytes, columns: int = 8) -> bytes:
    """``text`` in PNG 'sub' rows: each byte less the one before it, so no name shows in the bytes."""
    text += b" " * (-len(text) % columns)
    rows = b""
    for at in range(0, len(text), columns):
        row = text[at : at + columns]
        rows += b"\x01" + bytes((row[i] - (row[i - 1] if i else 0)) & 255 for i in range(columns))
    return rows


SCRIPT = b"<< /S /JavaScript /JS (app.alert(1)) >>"


@pytest.mark.parametrize(
    "body",
    [
        stream(
            b"/Type /ObjStm /Filter /FlateDecode /DecodeParms << /Predictor 11 /Columns 8 >>",
            zlib.compress(predicted(SCRIPT)),
        ),
        stream(b"/Subtype /Image /Filter [/FlateDecode /DCTDecode]", zlib.compress(SCRIPT)),
    ],
)
def test_javascript_behind_a_predictor_or_a_picture_encoding_is_found(body: bytes) -> None:
    with pytest.raises(checks.Refused, match="active content"):
        checks.check("page.pdf", a_pdf_with(body))


def test_a_clean_pdf_with_object_streams_is_kept() -> None:
    data = objects_in_a_stream(script=False)
    assert scripts_in(data) == 0 and checks.check("guide.pdf", data) == data
    parms = data.replace(b"/FlateDecode >>\nstream", b"/FlateDecode /DecodeParms 9 0 R >>\nstream", 1)
    with pytest.raises(checks.Refused, match="names a stream's decoding parameters in a way Ember can't check"):
        checks.check("guide.pdf", parms)


# --- FIX NOW 26: Office files that unpack to too much, or don't read -------------------------------------------


def lying_about(data: bytes, name: str, size: int) -> bytes:
    """A zip whose central directory says ``name`` unpacks to ``size`` bytes."""
    out = bytearray(data)
    at = out.find(b"PK\x01\x02")
    while at >= 0:
        length = struct.unpack("<H", out[at + 28 : at + 30])[0]
        if out[at + 46 : at + 46 + length] == name.encode():
            out[at + 24 : at + 28] = struct.pack("<I", size)
        at = out.find(b"PK\x01\x02", at + 4)
    return bytes(out)


def test_a_word_bomb_is_refused_before_it_is_unpacked() -> None:
    bomb = with_parts(a_docx(), {"word/document.xml": b" " * 60_000_000})
    assert len(bomb) < 200_000
    with pytest.raises(library.LibraryError, match="this Word file can't be read: it unpacks to too much data"):
        library.from_file("guide.docx", bomb)
    small = with_parts(a_docx(), {"word/media/x.xml": b" " * 30_000_000})
    liar = lying_about(small, "word/media/x.xml", 1_000)
    tracemalloc.start()
    try:
        with pytest.raises(library.LibraryError, match="this Word file can't be read: it can't be read whole"):
            library.from_file("guide.docx", liar)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 10_000_000  # zipfile's read() unpacked all 30 MB before it cut the part to the size it declared


def test_a_malformed_word_file_is_a_clear_refusal_not_an_error(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    broken = with_parts(a_docx(), {"word/document.xml": "<w:document><unclosed"})
    reply = owner(agent).add_document(
        {"file_name": "notes.docx", "file_data": base64.b64encode(broken).decode()}, "Owner"
    )
    assert reply.status == 422 and "this Word file can't be read" in json.dumps(reply.body)


def test_an_excel_bomb_is_refused_where_the_agent_reads_or_shows_it(data_dir: Path) -> None:
    agent, _ = make_agent(data_dir, [])
    ctx = ctx_for(agent)
    ws = ctx.workspace
    three_sheets(ws)
    make.spreadsheet(ws, "b.json", "shop/b.xlsx")
    ws.write_bytes("shop/bomb.xlsx", with_parts(ws.read_bytes("shop/b.xlsx"), {"xl/x.xml": b" " * 60_000_000}))
    with pytest.raises(tools.ToolError, match=r"shop/bomb\.xlsx can't be read: the Excel file unpacks to too much"):
        tools.HANDLERS["workspace_read"](ctx, {"path": "shop/bomb.xlsx"}, None)
    with pytest.raises(make.ProductError, match="unpacks to too much data"):
        make.image(ws, "shop/photo.png", "shop/bomb.xlsx#1", "Title")
    with pytest.raises(sheets.SheetError, match="can't be read"):
        sheets.workbook_text(zipfile_of({"[Content_Types].xml": b"<Types/>"}))


def zipfile_of(parts: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return out.getvalue()
