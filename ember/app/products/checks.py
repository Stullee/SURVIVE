"""Checks for files made in the workshop, before they enter the workspace.

Ember's own products are made by Ember's code from the agent's text. Workshop files are different: code a model wrote
made them, in Anthropic's sandbox, so their bytes are the agent's choice. Before one is kept:

* a picture is decoded and saved again, so nothing but its pixels survives (no metadata, no trailing data);
* a PDF is refused if anything in it can act on its own: JavaScript, launch or submit actions, embedded files, rich
  media, XFA forms, links that open other files, an action when it opens other than going to a page, links that
  aren't web or mail links (compressed object streams are searched too, and since 0.12.0 a stream Ember can't decode
  is refused, as is an encrypted file). 0.15.0: JavaScript still hid in an object stream whose encoding was named
  indirectly (/Filter 5 0 R), escaped (/Fil#74er) or twice, behind a PNG predictor, a "stream" line ending in a lone
  CR, a false "obj" or "endstream" inside the data, or a picture encoding after Flate. Now a stream's dictionary is
  everything since the stream before it, its encoding must be named directly and once, predictors are undone, a
  stream must decode to its end, and pdfium (Chrome's PDF engine) is asked last what it finds;
* a Word, Excel or PowerPoint file is refused if it holds macros, ActiveX or OLE objects, links to other files or
  templates, DDE, data connections or web queries, or actions that start programs; only web links may point outside
  the file, and (0.12.0) every part must be of a kind known to be safe: the formats' own XML, and PNG, JPEG or GIF
  pictures;
* text must be UTF-8 and small enough for a text file;
* anything else (SVG, archives, programs, fonts, ...) is refused, and so is a file that can't be read whole (0.12.0: a
  malformed Office file escaped the checks and its run went unrecorded).

``unzipped`` (0.15.0) reads an Office file within bounds for anyone who opens one (the workshop's check, the owner's
library, workspace_read): a small .docx could make Ember unpack gigabytes.

``check`` returns what to save, or raises Refused with the reason, in words the agent can act on.
"""

from __future__ import annotations

import base64
import bisect
import io
import re
import zipfile
import zlib
from pathlib import PurePosixPath

from defusedxml import ElementTree
from PIL import Image

from ..agent.sandbox import TEXT_EXTENSIONS
from . import images  # the PDF renderer, loaded at startup (see app.agent.tools)

PICTURES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
OFFICE = frozenset({".docx", ".xlsx", ".pptx"})
KEPT = frozenset({*TEXT_EXTENSIONS, *PICTURES, ".pdf", *OFFICE})
MAX_TEXT_BYTES = 64 * 1024
MAX_PDF_PAGES = 300
# 0.21.0: a page's sides in points (1/72 inch): from a quarter inch to PDF's own largest page, 200 inches
MIN_PAGE_POINTS = 18
MAX_PAGE_POINTS = 14_400
MAX_PAGE_RATIO = 20  # a bookmark is 4 times as long as it is wide; a page 1000 x 10 points wide was kept
MAX_UNPACKED_BYTES = 100 * 1024 * 1024  # what the streams of a PDF or the parts of an Office file may unpack to
READ_BYTES = 50_000_000  # 0.15.0: at most what an Office file Ember reads (library, workspace_read) unpacks to
MAX_PREDICTED_BYTES = 4 * 1024 * 1024  # 0.15.0: a PDF's streams whose predictor Ember undoes (about 1 s a MB)
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
_REGULAR = rb"[^\x00\t\n\x0c\r /<>\[\]()%{}]"  # a character of a PDF word (not white space or a delimiter)
# A stream's data starts after the line its keyword is on (0.12.0: not the "stream" in "endstream"; 0.15.0: that line
# may end in CR, LF or both, as pdfium reads it: a lone CR hid a stream). The keyword follows its dictionary's ">>", as
# pdfium reads it, and only the keyword is matched: a false "stream" before it on its line (/stream) hid it.
_PDF_STREAM = re.compile(rb">>(?:[\x00\t\n\x0c\r ]|%[^\r\n]*+)*+(stream)(?!" + _REGULAR + rb")")
_LINE_END = re.compile(rb"[^\r\n]*(?:\r\n|\r|\n)")
_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
# 0.12.0: the encodings a PDF stream may use. Ember decodes these to search what they hold ...
_DECODED = {
    "FlateDecode": "flate",
    "Fl": "flate",
    "ASCII85Decode": "a85",
    "A85": "a85",
    "ASCIIHexDecode": "hex",
    "AHx": "hex",
}
# ... and these picture encodings stay as they are, on pictures only (as the last encoding).
_PICTURE_FILTERS = frozenset({"DCTDecode", "DCT", "JPXDecode", "JBIG2Decode", "CCITTFaxDecode", "CCF"})
# 0.15.0: a stream's /Filter and /DecodeParms, read as pdfium reads them (white space and comments between).
_WS = rb"(?:[\x00\t\n\x0c\r ]|%[^\r\n]*)*"
_KEY_END = rb"(?!" + _REGULAR + rb")"
_FILTER = re.compile(rb"/Filter" + _KEY_END + _WS)
_PARMS = re.compile(rb"/DecodeParms" + _KEY_END + _WS)
_FILTER_NAME = rb"/" + _REGULAR + rb"+"
_FILTER_VALUE = re.compile(_FILTER_NAME + rb"|\[(?:" + _WS + _FILTER_NAME + rb")*" + _WS + rb"\]")
_PARMS_VALUE = re.compile(rb"null\b|<<[^<>]*>>|\[(?:[^\[\]<>]|<<[^<>]*>>)*\]")
_REFERENCE = re.compile(rb"\d+" + _WS + rb"\d+" + _WS + rb"R" + _KEY_END)
_INDIRECT_TYPE = re.compile(rb"/(?:Sub)?[Tt]ype" + _KEY_END + _WS + rb"\d")
_IMAGE = re.compile(rb"/Subtype\s*/Image\b")
_ACTION_TYPE = re.compile(rb"/S\s*/([^\s/<>\[\]()%{}]+)")
_WEB_LINK = re.compile(rb"(?:https?://|mailto:)", re.IGNORECASE)
# Parts of an Office file that run code, embed other programs' objects or pull in other files (0.12.0: data
# connections and web queries too).
_ACTIVE_PART = re.compile(
    r"(?:^|/)(?:vbaProject\.bin|vbaData\.xml|activeX|embeddings/|oleObject|externalLinks/|customUI|attachedToolbars"
    r"|connections\.xml|queryTables/)",
    re.IGNORECASE,
)
# 0.12.0: the kinds of parts an Office file may hold: the formats' own XML, a printer's settings, and pictures.
_SAFE_TYPE = re.compile(
    r"^(?:application/vnd\.openxmlformats-[a-z.-]+\+xml|application/xml|text/xml|image/(?:png|jpeg|gif)"
    r"|application/vnd\.openxmlformats-officedocument\.(?:spreadsheetml|wordprocessingml|presentationml)"
    r"\.printerSettings|application/vnd\.ms-office\.(?:chartstyle|chartcolorstyle)\+xml"
    r"|application/vnd\.ms-word\.stylesWithEffects\+xml)$",
    re.IGNORECASE,
)
_UNSAFE_TYPE = re.compile(r"macro|vba|activeX|oleObject|connections|queryTable|externalLink|customUI", re.IGNORECASE)
_TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
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
    try:
        if suffix in TEXT_EXTENSIONS:
            return _text(data)
        if suffix in PICTURES:
            return _picture(data, PICTURES[suffix])
        if suffix == ".pdf":
            return _pdf(data)
        return _office(data, suffix)
    except Refused:
        raise
    except Exception:  # noqa: BLE001 - 0.12.0: what can't be read whole isn't kept (it escaped, and the run was lost)
        raise Refused(f"the {suffix} file can't be read whole") from None


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
            if images.too_large(image.width, image.height):  # 0.15.0: the size, as Ember's tools say it
                raise Refused(f"the picture is {images.too_large(image.width, image.height)}")
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
    if "Encrypt" in _names(data):
        raise Refused("the PDF is encrypted, so Ember can't check it")
    found = _active_names(data)
    unpacked = 0
    searched = [data]
    ends: list[int] = []  # where the streams' data ended, in order
    predictable = [MAX_PREDICTED_BYTES]
    for match in _PDF_STREAM.finditer(data):
        line = _LINE_END.match(data, match.end())
        end = data.find(b"endstream", line.end()) if line else -1
        if end < 0:
            continue  # no stream at all: pdfium reads none without an end either
        # 0.15.0: the stream's dictionary is somewhere after the last stream that ended before it. The last "N G obj"
        # before the keyword could be a false one, in a string of the dictionary itself, which hid its /Filter.
        at = bisect.bisect_right(ends, match.start(1)) - 1
        head = data[ends[at] if at >= 0 else 0 : match.start(1)]
        bisect.insort(ends, end)
        chunk = _decoded(head, data[line.end() : end], MAX_UNPACKED_BYTES - unpacked, predictable)
        if chunk is None:
            continue  # a picture's own encoding, or no encoding (its raw bytes were searched already)
        unpacked += len(chunk)
        if unpacked > MAX_UNPACKED_BYTES:
            raise Refused("the PDF unpacks to too much data")
        found |= _active_names(chunk)
        searched.append(chunk)
    if found:
        raise Refused(f"the PDF has active content ({', '.join(sorted(found))}), which Ember never keeps")
    for chunk in searched:
        _check_actions(chunk, data)
    try:
        pages = images.page_count(data)
        active = images.pdf_active(data)
        sizes = images.page_sizes(data) if 1 <= pages <= MAX_PDF_PAGES else []
    except Exception:  # noqa: BLE001
        raise Refused("the PDF can't be opened") from None
    if not 1 <= pages <= MAX_PDF_PAGES:
        raise Refused(f"the PDF has {pages} pages; at most {MAX_PDF_PAGES}")
    for number, (width, height) in enumerate(sizes, 1):  # 0.21.0: no page any printer or screen shows
        short, long = min(width, height), max(width, height)
        if short < MIN_PAGE_POINTS or long > MAX_PAGE_POINTS or long > MAX_PAGE_RATIO * short:
            raise Refused(
                f"page {number} is {width:.0f} x {height:.0f} points: a page must be {MIN_PAGE_POINTS} to"
                f" {MAX_PAGE_POINTS} points (0.25 to 200 inches) wide and high, at most {MAX_PAGE_RATIO} times as long"
                " as it is wide"
            )
    if active:  # 0.15.0: what a viewer finds, whatever the search above missed
        raise Refused(f"the PDF has active content ({', '.join(active)}), which Ember never keeps")
    return data


def _names(data: bytes) -> set[str]:
    """The PDF names in ``data``, their #xx escapes decoded."""
    return {
        _ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), m.group(1)).decode("latin-1")
        for m in _PDF_NAME.finditer(data)
    }


def _active_names(data: bytes) -> set[str]:
    return _names(data) & ACTIVE_PDF


def _decoded(head: bytes, raw: bytes, room: int, predictable: list[int]) -> bytes | None:
    """What a stream holds, decoded for the search (0.12.0); None when there is nothing to search: no encoding (its raw
    bytes are searched with the file), or a picture in its own encoding. Refuses an encoding Ember can't decode (it
    hid JavaScript from the search) and a stream that doesn't decode.

    0.15.0: ``head`` is everything since the stream before it, its #xx escapes decoded. Every /Filter and
    /DecodeParms in it must say the same, directly (an indirect one hid an object stream). A stream decodes to its end
    (a false "endstream" inside it cut it short), and a PNG or TIFF predictor is undone, except on a picture (it hid
    the names in an object stream): at most ``predictable[0]`` bytes more, which is lowered by what is undone."""
    head = _ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), head)
    filters = _one_value(head, _FILTER, _FILTER_VALUE, "encoding")
    if filters is None:
        return None
    names = [f.decode("latin-1") for f in re.findall(rb"/(" + _REGULAR + rb"+)", filters)]
    if not names:
        return None
    parms = _parameters(_one_value(head, _PARMS, _PARMS_VALUE, "decoding parameters"), filters, len(names))
    picture = bool(_IMAGE.search(head)) and b"/ObjStm" not in head and not _INDIRECT_TYPE.search(head)
    if names[-1] in _PICTURE_FILTERS and _IMAGE.search(head):
        names = names[:-1]  # the picture's own encoding stays; what is around it is decoded and searched
        if not names:
            return None
    unknown = [f for f in names if f not in _DECODED]
    if unknown:
        raise Refused(f"the PDF has a stream encoded with {unknown[0]}, which Ember can't check")
    data = raw
    try:
        for name, parm in zip(names, parms, strict=False):
            kind = _DECODED[name]
            if kind == "flate":
                inflate = zlib.decompressobj()
                data = inflate.decompress(data, room + 1)
                if len(data) > room:
                    return data  # the caller refuses: too much data
                if not inflate.eof:
                    raise ValueError("cut short")
                if parm and not picture:
                    data = _unpredicted(data, parm, predictable)
            elif kind == "a85":
                text = re.sub(rb"\s", b"", data)
                if b"~>" not in text:
                    raise ValueError("cut short")
                data = base64.a85decode(text.split(b"~>")[0], adobe=False)
            else:
                text = re.sub(rb"\s", b"", data)
                if b">" not in text:
                    raise ValueError("cut short")
                text = text.split(b">")[0]
                data = bytes.fromhex((text + b"0" * (len(text) % 2)).decode("ascii"))
    except Refused:
        raise
    except (zlib.error, ValueError):
        raise Refused("a stream of the PDF doesn't decode, so Ember can't check it") from None
    return data


def _one_value(head: bytes, key: re.Pattern[bytes], value: re.Pattern[bytes], what: str) -> bytes | None:
    """0.15.0: the value of a stream's /Filter or /DecodeParms: None when it has none; refused when it is named
    indirectly, in a form Ember doesn't read, or twice with different values."""
    values = set()
    for match in key.finditer(head):
        found = value.match(head, match.end())
        if found is None or _REFERENCE.search(found.group(0)):
            raise Refused(f"the PDF names a stream's {what} in a way Ember can't check")
        values.add(found.group(0))
    if len(values) > 1:
        raise Refused(f"the PDF names a stream's {what} twice, so Ember can't check it")
    return values.pop() if values else None


def _parameters(value: bytes | None, filters: bytes, count: int) -> list[bytes | None]:
    """Each encoding's parameters, as pdfium pairs them: an array of them with an array of encodings, a dictionary
    with a single encoding; anything else is ignored."""
    if value is None or value.startswith(b"null") or filters.startswith(b"[") != value.startswith(b"["):
        return [None] * count
    found = [m.group(0) if m.group(0).startswith(b"<<") else None for m in re.finditer(rb"<<[^<>]*>>|null", value)]
    return (found + [None] * count)[:count]


def _unpredicted(data: bytes, parms: bytes, predictable: list[int]) -> bytes:
    """0.15.0: a stream's data with its PNG (10-15) or TIFF (2) predictor undone, as pdfium does; refused past
    ``predictable[0]`` bytes (Python undoes about a MB a second)."""

    def number(key: bytes, default: int) -> int:
        """A whole number, named once: pdfium reads the last of two, and 1.0 or +11 as numbers too."""
        keys = list(re.finditer(rb"/" + key + _KEY_END + _WS, parms))
        value = re.compile(rb"\d+" + _KEY_END).match(parms, keys[0].end()) if keys else None
        if keys and (len(keys) > 1 or value is None):
            raise Refused("the PDF names a stream's decoding parameters in a way Ember can't check")
        return int(value.group(0)) if value else default

    predictor = number(b"Predictor", 1)
    if predictor < 2 or predictor in range(3, 10):
        return data
    predictable[0] -= len(data)
    if predictable[0] < 0:
        raise Refused(f"the PDF has more than {MAX_PREDICTED_BYTES // (1024 * 1024)} MB of predicted data to check")
    colors, bits, columns = number(b"Colors", 1), number(b"BitsPerComponent", 8), number(b"Columns", 1)
    if not (1 <= colors <= 32 and bits in (1, 2, 4, 8, 16) and 1 <= columns <= 1_000_000):
        raise ValueError("predictor")
    row = (colors * bits * columns + 7) // 8
    step = max(1, colors * bits // 8)
    out = bytearray()
    if predictor == 2:
        if bits != 8:
            raise ValueError("predictor")
        out.extend(data)
        for start in range(0, len(out), row):
            for at in range(start + step, min(start + row, len(out))):
                out[at] = (out[at] + out[at - step]) & 255
        return bytes(out)
    previous = bytearray(row)
    for start in range(0, len(data), row + 1):
        kind, line = data[start], bytearray(data[start + 1 : start + 1 + row])
        for at in range(len(line)):
            left = line[at - step] if at >= step else 0
            up, corner = previous[at], previous[at - step] if at >= step else 0
            if kind == 1:
                line[at] = (line[at] + left) & 255
            elif kind == 2:
                line[at] = (line[at] + up) & 255
            elif kind == 3:
                line[at] = (line[at] + (left + up) // 2) & 255
            elif kind == 4:
                guess = left + up - corner
                near = min((abs(guess - left), 0, left), (abs(guess - up), 1, up), (abs(guess - corner), 2, corner))
                line[at] = (line[at] + near[2]) & 255
        out.extend(line)
        previous = line + bytearray(row - len(line))
    return bytes(out)


def _check_actions(chunk: bytes, whole: bytes) -> None:
    """0.12.0: what the PDF does when it opens is only ever going to a page, and its links are web or mail links (an
    /OpenAction to a web address went unnoticed)."""
    for match in _PDF_NAME.finditer(chunk):
        name = _ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), match.group(1)).decode("latin-1")
        rest = chunk[match.end() : match.end() + 2_000].lstrip()
        if name == "OpenAction" and not _goes_to_a_page(rest, whole):
            raise Refused("the PDF does something when it opens other than showing a page, which Ember never keeps")
        if name == "URI" and not _web_link(rest):
            raise Refused("the PDF has a link that isn't a web or mail link")


def _goes_to_a_page(value: bytes, whole: bytes) -> bool:
    if value.startswith(b"["):  # a destination: a page and how to show it
        return True
    if value.startswith(b"<<"):
        kinds = _ACTION_TYPE.findall(value.split(b">>")[0])
        return all(k == b"GoTo" for k in kinds)
    reference = re.match(rb"(\d+)\s+(\d+)\s+R\b", value)
    if reference is None:
        return False
    target = re.search(rb"(?:^|\s)" + reference.group(1) + rb"\s+" + reference.group(2) + rb"\s+obj\b", whole)
    if target is None:
        return False  # in an object stream, or missing: can't be checked
    return _goes_to_a_page(whole[target.end() : target.end() + 2_000].lstrip(), b"")


def _web_link(value: bytes) -> bool:
    """Whether the string after /URI is a web or mail link (literal or hex). /URI followed by a name is an action's
    type (/S /URI), and by a dictionary the base of the file's links."""
    if value.startswith((b"/", b"<<")):
        return True
    if value.startswith(b"("):
        return bool(_WEB_LINK.match(value[1:].lstrip()))
    if value.startswith(b"<") and not value.startswith(b"<<"):
        text = re.sub(rb"\s", b"", value[1:].split(b">")[0])
        try:
            return bool(_WEB_LINK.match(bytes.fromhex((text + b"0" * (len(text) % 2)).decode("ascii"))))
        except ValueError:
            return False
    return False  # an indirect or odd value: not a plain web link


def unzipped(data: bytes, what: str, max_bytes: int = MAX_UNPACKED_BYTES) -> dict[str, bytes]:
    """0.15.0: the parts of a zip (an Office file), by name. Before anything is unpacked: at most MAX_PARTS parts, each
    named once, ``max_bytes`` in all by the sizes they declare, none far larger than it is packed (a zip bomb), only
    stored or deflated. Each part is then unpacked a piece at a time, never past the size it declares: a reader that
    unpacks a part whole (python-docx, openpyxl) took whatever a lying size let it (a 229 KB .docx took 421 MB), so
    give them ``stored`` of these. Refuses, naming ``what``, whatever doesn't hold."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, EOFError):
        raise Refused(f"{what} isn't a valid Office file") from None
    with archive:
        parts = archive.infolist()
        if len(parts) > MAX_PARTS:
            raise Refused(f"{what} has too many parts")
        if len({p.filename.lower() for p in parts}) < len(parts):  # a reader may take either copy of a part
            raise Refused(f"{what} isn't a valid Office file: it holds a part twice")
        if sum(p.file_size for p in parts) > max_bytes:
            raise Refused(f"{what} unpacks to too much data")
        for part in parts:
            if part.compress_size and part.file_size / part.compress_size > MAX_RATIO and part.file_size > 1_000_000:
                raise Refused(f"{what} unpacks to far more than its size (a zip bomb)")
            if part.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or part.flag_bits & 1:
                raise Refused(f"{what} isn't a valid Office file")
        found: dict[str, bytes] = {}
        try:
            for part in parts:
                pieces = []
                with archive.open(part) as stream:
                    while piece := stream.read(64 * 1024):
                        pieces.append(piece)
                found[part.filename] = b"".join(pieces)
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, RuntimeError, NotImplementedError):
            raise Refused(f"{what} can't be read whole") from None
    return found


def stored(parts: dict[str, bytes]) -> bytes:
    """0.15.0: parts ``unzipped`` read, as a zip again, not compressed: what a reader unpacks from it is what was
    checked."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return out.getvalue()


def _office(data: bytes, suffix: str) -> bytes:
    parts = unzipped(data, f"the {suffix} file")
    for name in parts:
        if _ACTIVE_PART.search(name):
            raise Refused(f"the file holds {name}: macros, ActiveX, embedded objects or links to other files")
    if "[Content_Types].xml" not in parts:
        raise Refused(f"the {suffix} file isn't a valid Office file")
    types = parts["[Content_Types].xml"].decode("utf-8", "replace")
    if "macroEnabled" in types or "vbaProject" in types:
        raise Refused("the file is macro-enabled")
    for name, part in parts.items():
        if name.endswith(".rels"):
            _relationships(part, name)
    for name, part in parts.items():
        if not name.endswith(".xml"):
            continue
        xml = part.decode("utf-8", "replace")
        if suffix == ".docx" and name.startswith("word/"):
            _word_fields(xml, name)
        elif suffix == ".xlsx" and name.startswith("xl/worksheets/"):
            _excel_formulas(xml, name)
        elif suffix == ".xlsx" and name == "xl/workbook.xml":
            _excel_names(xml, name)
        elif suffix == ".pptx" and _PPT_ACTIONS.search(xml):
            raise Refused(f"{name} has an action that starts a program or macro")
    _known_parts(list(parts), _parse(types, "[Content_Types].xml"))
    return data


def _known_parts(names: list[str], types: object) -> None:
    """0.12.0: every part is of a kind known to be safe (by its content type: named, or by its file ending)."""
    named = {
        n.get("PartName", "").lstrip("/").lower(): n.get("ContentType", "") for n in types.iter(f"{_TYPES_NS}Override")
    }  # type: ignore[attr-defined]
    endings = {n.get("Extension", "").lower(): n.get("ContentType", "") for n in types.iter(f"{_TYPES_NS}Default")}  # type: ignore[attr-defined]
    for name in names:
        if name == "[Content_Types].xml" or name.endswith("/"):
            continue
        last = name.rsplit("/", 1)[-1]
        ending = last.rsplit(".", 1)[-1].lower() if "." in last else ""  # ".rels" too
        kind = named.get(name.lower()) or endings.get(ending, "")
        if not kind or not _SAFE_TYPE.match(kind) or _UNSAFE_TYPE.search(kind):
            raise Refused(f"the file holds {name} ({kind or 'of no known kind'}), which Ember doesn't keep")


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
    """Refuse fields that pull in other files or programs. 0.12.0: a field's code is read whole, however its runs
    split it ("INCLUDE" and "TEXT" in two runs), with what nested fields put into it."""
    root = _parse(xml, where)
    codes = [node.get(f"{_WORD_NS}instr", "") for node in root.iter(f"{_WORD_NS}fldSimple")]
    fields: list[list[str]] = []  # the fields being read, outermost first; each collects its code
    reading: list[bool] = []  # whether each is still in its code (before its "separate")
    loose: list[str] = []
    for node in root.iter():
        if node.tag == f"{_WORD_NS}fldChar":
            kind = node.get(f"{_WORD_NS}fldCharType")
            if kind == "begin":
                fields.append([])
                reading.append(True)
            elif kind == "separate" and fields:
                reading[-1] = False
                codes.append("".join(fields[-1]))
            elif kind == "end" and fields:
                code = fields.pop()
                if reading.pop():
                    codes.append("".join(code))
        elif node.tag in (f"{_WORD_NS}instrText", f"{_WORD_NS}t"):
            text = node.text or ""
            for index, open_code in enumerate(reading):
                if open_code:
                    fields[index].append(text)
            if node.tag == f"{_WORD_NS}instrText" and not fields:
                loose.append(text)
    codes += ["".join(loose), *loose]
    for code in codes:
        if _FIELD_CODES.search(code.upper()):
            raise Refused(f"{where} has a field that pulls in other files or programs ({code.strip()[:60]})")


def _excel_formulas(xml: str, where: str) -> None:
    for node in _parse(xml, where).iter(f"{_EXCEL_NS}f"):
        if _EXCEL_ACTIVE.search(node.text or ""):
            raise Refused(f"{where} has a formula that reaches outside the workbook ({(node.text or '')[:60]})")


def _excel_names(xml: str, where: str) -> None:
    """0.12.0: a defined name is a formula too (WEBSERVICE in a name went unnoticed)."""
    for node in _parse(xml, where).iter(f"{_EXCEL_NS}definedName"):
        if _EXCEL_ACTIVE.search(node.text or ""):
            raise Refused(f"{where} has a name that reaches outside the workbook ({(node.text or '')[:60]})")
