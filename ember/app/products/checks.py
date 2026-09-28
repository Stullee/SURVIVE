"""Checks for files made in the workshop, before they enter the workspace.

Ember's own products are made by Ember's code from the agent's text. Workshop files are different: code a model wrote
made them, in Anthropic's sandbox, so their bytes are the agent's choice. Before one is kept:

* a picture is decoded and saved again, so nothing but its pixels survives (no metadata, no trailing data);
* a PDF is refused if anything in it can act on its own: JavaScript, launch or submit actions, embedded files, rich
  media, XFA forms, links that open other files (compressed object streams are searched too);
* a Word, Excel or PowerPoint file is refused if it holds macros, ActiveX or OLE objects, links to other files or
  templates, DDE, or actions that start programs; only web links may point outside the file;
* text must be UTF-8 and small enough for a text file;
* anything else (SVG, archives, programs, fonts, ...) is refused.

``check`` returns what to save, or raises Refused with the reason, in words the agent can act on.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from pathlib import PurePosixPath

from defusedxml import ElementTree
from PIL import Image

from ..agent.sandbox import TEXT_EXTENSIONS

PICTURES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
OFFICE = frozenset({".docx", ".xlsx", ".pptx"})
KEPT = frozenset({*TEXT_EXTENSIONS, *PICTURES, ".pdf", *OFFICE})
MAX_TEXT_BYTES = 64 * 1024
MAX_PICTURE_PIXELS = 40_000_000
MAX_PDF_PAGES = 300
MAX_UNPACKED_BYTES = 100 * 1024 * 1024  # what the streams of a PDF or the parts of an Office file may unpack to
MAX_PARTS = 3_000
MAX_RATIO = 200  # an Office part that unpacks to more than this many times its size is a zip bomb
# PDF names that make a file act on its own (a name's #xx escapes are decoded before the comparison).
ACTIVE_PDF = frozenset(
    {
        "JavaScript",
        "JS",
        "Launch",
        "EmbeddedFile",
        "EmbeddedFiles",
        "RichMedia",
        "XFA",
        "SubmitForm",
        "ImportData",
        "GoToE",
        "GoToR",
        "AA",
    }
)
_PDF_NAME = re.compile(rb"/([^\s/<>\[\]()%{}]{1,127})")
_PDF_STREAM = re.compile(rb"stream\r?\n")
_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
# Parts of an Office file that run code, embed other programs' objects or pull in other files.
_ACTIVE_PART = re.compile(
    r"(?:^|/)(?:vbaProject\.bin|vbaData\.xml|activeX|embeddings/|oleObject|externalLinks/|customUI|attachedToolbars)",
    re.IGNORECASE,
)
_FIELD_CODES = re.compile(r"\b(?:DDE|DDEAUTO|INCLUDETEXT|INCLUDEPICTURE|IMPORT|LINK)\b")
_EXCEL_ACTIVE = re.compile(
    r"\||\[\d+\]|\b(?:WEBSERVICE|FILTERXML|CALL|REGISTER|EXEC|RTD|HYPERLINK)\s*\(", re.IGNORECASE
)
_PPT_ACTIONS = re.compile(r"ppaction://(?:program|ole|macro)", re.IGNORECASE)
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_EXCEL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATIONSHIP = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_HYPERLINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


class Refused(ValueError):
    """A workshop file Ember doesn't keep; the message says why."""


def check(name: str, data: bytes) -> bytes | str:
    """What to save for the workshop file ``name``: text (str) or bytes. Raises Refused."""
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in KEPT:
        raise Refused(
            f"{suffix or 'a file without an ending'} files aren't kept; the workshop can make text, PNG and JPEG "
            "pictures, PDF, Word, Excel and PowerPoint files"
        )
    if suffix in TEXT_EXTENSIONS:
        return _text(data)
    if suffix in PICTURES:
        return _picture(data, PICTURES[suffix])
    if suffix == ".pdf":
        return _pdf(data)
    return _office(data, suffix)


def _text(data: bytes) -> str:
    if len(data) > MAX_TEXT_BYTES:
        raise Refused(f"a text file can hold at most {MAX_TEXT_BYTES // 1024} KB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("the text isn't UTF-8") from None
    if "\x00" in text:
        raise Refused("the text contains NUL characters")
    return text


def _picture(data: bytes, expected: str) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != expected:
                raise Refused(f"the file is a {image.format or 'unknown'} picture, not {expected}")
            if image.width * image.height > MAX_PICTURE_PIXELS:
                raise Refused(f"the picture has more than {MAX_PICTURE_PIXELS // 1_000_000} million pixels")
            pixels = image.convert("RGBA" if expected == "PNG" and image.mode in ("RGBA", "LA", "P") else "RGB")
            pixels.load()
    except Refused:
        raise
    except Exception:  # noqa: BLE001 - whatever breaks the decoder, the file isn't kept
        raise Refused(f"the picture can't be read as {expected}") from None
    out = io.BytesIO()
    if expected == "PNG":
        pixels.save(out, "PNG", optimize=True)
    else:
        pixels.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


def _pdf(data: bytes) -> bytes:
    if not data.startswith(b"%PDF-"):
        raise Refused("the file isn't a PDF")
    found = _active_names(data)
    unpacked = 0
    for match in _PDF_STREAM.finditer(data):
        end = data.find(b"endstream", match.end())
        if end < 0:
            break
        decompressor = zlib.decompressobj()
        try:
            chunk = decompressor.decompress(data[match.end() : end], MAX_UNPACKED_BYTES - unpacked + 1)
        except zlib.error:
            continue  # not Flate-compressed (an image, say): its raw bytes were searched already
        unpacked += len(chunk)
        if unpacked > MAX_UNPACKED_BYTES:
            raise Refused("the PDF unpacks to too much data")
        found |= _active_names(chunk)
    if found:
        raise Refused(f"the PDF has active content ({', '.join(sorted(found))}), which Ember never keeps")
    from . import images  # the PDF renderer, loaded at startup (see app.agent.tools)

    try:
        pages = images.page_count(data)
    except Exception:  # noqa: BLE001
        raise Refused("the PDF can't be opened") from None
    if not 1 <= pages <= MAX_PDF_PAGES:
        raise Refused(f"the PDF has {pages} pages; at most {MAX_PDF_PAGES}")
    return data


def _active_names(data: bytes) -> set[str]:
    found = set()
    for match in _PDF_NAME.finditer(data):
        name = _ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), match.group(1)).decode("latin-1")
        if name in ACTIVE_PDF:
            found.add(name)
    return found


def _office(data: bytes, suffix: str) -> bytes:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Refused(f"the {suffix} file isn't a valid Office file") from None
    with archive:
        parts = archive.infolist()
        if len(parts) > MAX_PARTS:
            raise Refused("the file has too many parts")
        if sum(p.file_size for p in parts) > MAX_UNPACKED_BYTES:
            raise Refused("the file unpacks to too much data")
        for part in parts:
            if part.compress_size and part.file_size / part.compress_size > MAX_RATIO and part.file_size > 1_000_000:
                raise Refused("the file unpacks to far more than its size (a zip bomb)")
            if _ACTIVE_PART.search(part.filename):
                raise Refused(
                    f"the file holds {part.filename}: macros, ActiveX, embedded objects or links to other files"
                )
        names = {p.filename for p in parts}
        if "[Content_Types].xml" not in names:
            raise Refused(f"the {suffix} file isn't a valid Office file")
        types = archive.read("[Content_Types].xml").decode("utf-8", "replace")
        if "macroEnabled" in types or "vbaProject" in types:
            raise Refused("the file is macro-enabled")
        for part in parts:
            if part.filename.endswith(".rels"):
                _relationships(archive.read(part), part.filename)
        for part in parts:
            if not part.filename.endswith(".xml"):
                continue
            xml = archive.read(part).decode("utf-8", "replace")
            if suffix == ".docx" and part.filename.startswith("word/"):
                _word_fields(xml, part.filename)
            elif suffix == ".xlsx" and part.filename.startswith("xl/worksheets/"):
                _excel_formulas(xml, part.filename)
            elif suffix == ".pptx" and _PPT_ACTIONS.search(xml):
                raise Refused(f"{part.filename} has an action that starts a program or macro")
    return data


def _parse(xml: bytes | str, where: str):  # noqa: ANN202 - an ElementTree element
    try:
        return ElementTree.fromstring(xml)
    except Exception:  # noqa: BLE001 - malformed or hostile XML (entities) is refused either way
        raise Refused(f"{where} isn't valid XML") from None


def _relationships(xml: bytes, where: str) -> None:
    for relation in _parse(xml, where).iter(_RELATIONSHIP):
        if relation.get("TargetMode") == "External" and relation.get("Type") != _HYPERLINK:
            raise Refused(f"{where} links to another file ({relation.get('Target', '')[:80]}); only web links may")
        target = relation.get("Target", "")
        web = target.lower().startswith(("https://", "http://", "mailto:"))
        if relation.get("TargetMode") == "External" and not web:
            raise Refused(f"{where} has a link that isn't a web or mail link ({target[:80]})")


def _word_fields(xml: str, where: str) -> None:
    root = _parse(xml, where)
    codes = [node.text or "" for node in root.iter(f"{_WORD_NS}instrText")]
    codes += [node.get(f"{_WORD_NS}instr", "") for node in root.iter(f"{_WORD_NS}fldSimple")]
    for code in codes:
        if _FIELD_CODES.search(code.upper()):
            raise Refused(f"{where} has a field that pulls in other files or programs ({code.strip()[:60]})")


def _excel_formulas(xml: str, where: str) -> None:
    for node in _parse(xml, where).iter(f"{_EXCEL_NS}f"):
        if _EXCEL_ACTIVE.search(node.text or ""):
            raise Refused(f"{where} has a formula that reaches outside the workbook ({(node.text or '')[:60]})")
