"""Ember's products: finished files made from what the agent writes in its workspace.

* ``document``: a Markdown file (with settings and ``:::`` layout lines, see markup.py) becomes a PDF, an editable
  Word file next to it and pictures of its first pages: ``shop/cv.pdf``, ``shop/cv.docx``, ``shop/cv-page1.png``.
* ``spreadsheet``: a JSON spec becomes an Excel file and a picture of each table: ``shop/budget.xlsx``,
  ``shop/budget-preview.png`` (0.14.0: and ``shop/budget-sheet2.png`` for the second sheet, and so on).
* ``image``: a listing photo made of pages of Ember's own PDFs, sheets of its Excel files or pictures (0.14.0: or a
  region of one, zoomed in), with a title, a subtitle and a badge; (0.14.0) a text photo, or a poster at print size.

The agent never writes the bytes of these files: Ember's code makes them from the agent's text and writes them
with ``Jail.write_bytes``. Every problem the agent can fix comes back as a ProductError naming what to change.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ..agent.sandbox import Jail
from . import checks, images, markup, pdf, sheets, word

PAGE_PREVIEWS = 4  # pictures of the first pages of a document
PREVIEW_DPI = 100  # an A4 page is 827 x 1169 pixels
MAX_LISTING_PAGES = 3
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
# 0.14.0: 'shop/cv.pdf#2' (a page), 'shop/b.xlsx#2' or 'shop/b.xlsx#Budget' (a sheet), 'shop/p.png', each with an
# optional region to zoom in on: 'shop/cv.pdf#1@top'.
_PAGE_REF = re.compile(
    r"^(?P<path>.+?\.(?P<kind>pdf|xlsx|png|jpg))(?:#(?P<part>[^@#]{1,31}))?(?:@(?P<region>[a-z-]+))?$", re.IGNORECASE
)


class ProductError(ValueError):
    """Something the agent can fix in its source or its call; the message says what."""


@dataclass
class Made:
    """What a make call wrote and what the agent should know about it."""

    files: list[tuple[str, int]] = field(default_factory=list)  # path, bytes
    report: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.report)

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.files]


def _base(output: str, suffix: str, what: str) -> str:
    if not output.lower().endswith(suffix) or len(output) <= len(suffix):
        raise ProductError(f"{what} must be a path ending in {suffix}, e.g. 'shop/planner{suffix}'")
    return output[: -len(suffix)]


def _size(count: int) -> str:
    return f"{count / 1024:.0f} KB" if count < 1024 * 1024 else f"{count / (1024 * 1024):.1f} MB"


def _write(jail: Jail, made: Made, path: str, data: bytes) -> None:
    made.files.append((path, jail.write_bytes(path, data)))


def _names(paths: list[str]) -> str:
    return ", ".join(paths)


# --- documents ---


def document(jail: Jail, source: str, output: str, word_copy: bool = True, previews: bool = True) -> Made:
    """Make ``output`` (a PDF) from the Markdown file ``source``, with a Word copy and page pictures next to it."""
    base = _base(output, ".pdf", "output")
    if not source.lower().endswith((".md", ".txt")):
        raise ProductError("source must be the .md (or .txt) file you wrote the document in")
    text = jail.read(source)
    try:
        parsed = markup.parse(text)
        data, layout = pdf.render(parsed)
        docx = word.render(parsed) if word_copy else None
    except markup.DocumentError as exc:
        raise ProductError(f"{source}: {exc}") from None
    made = Made()
    _write(jail, made, output, data)
    if docx is not None:
        _write(jail, made, f"{base}.docx", docx)
    shown: list[str] = []
    if previews:
        numbers = list(range(1, min(layout.pages, PAGE_PREVIEWS) + 1))
        for number, page in zip(numbers, images.pdf_pages(data, numbers, dpi=PREVIEW_DPI), strict=True):
            path = f"{base}-page{number}.png"
            _write(jail, made, path, images.png(page))
            shown.append(path)
    # Pictures of pages this version no longer has would show an old document.
    first_stale = len(shown) + 1 if previews else 1
    for number in range(first_stale, pdf.MAX_PAGES + 1):
        path = f"{base}-page{number}.png"
        if jail.size_of(path, "product") is None:
            if number > PAGE_PREVIEWS:
                break
            continue
        jail.delete(path)
        made.removed.append(path)

    settings = parsed.settings
    page = f"{settings.page} {'landscape' if settings.landscape else 'portrait'}"
    count = f"{layout.pages} page{'s' if layout.pages != 1 else ''}"
    made.report.append(f"Made {output}: {count} ({page}), {_size(len(data))}.")
    extras = []
    if docx is not None:
        extras.append(f"{base}.docx (Word, editable, {_size(len(docx))})")
    elif jail.size_of(f"{base}.docx", "product") is not None:
        extras.append(f"{base}.docx is from an earlier version: remake it with word true, or delete it")
    if shown:
        more = f" (the first {len(shown)} of {layout.pages})" if len(shown) < layout.pages else ""
        extras.append(f"page pictures {_names(shown)}{more}")
    if extras:
        made.report.append("Also: " + "; ".join(extras) + ".")
    ends_page, used = layout.main_end
    made.report.append(f"Layout: the main text ends on page {ends_page}, {used:.0%} of the way down its text area.")
    if layout.sidebar_pages:
        made.report.append(f"The sidebar fills {layout.sidebar_pages} page{'s' if layout.sidebar_pages != 1 else ''}.")
    if parsed.warnings or layout.warnings:
        made.report.append("Check: " + " ".join(_sentence(w) for w in [*parsed.warnings, *layout.warnings]))
    if made.removed:
        made.report.append(f"Removed old page pictures: {_names(made.removed)}.")
    return made


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


# --- spreadsheets ---


def spreadsheet(jail: Jail, source: str, output: str) -> Made:
    """Make ``output`` (an Excel file) from the JSON spec ``source``, and a picture of each table (0.14.0)."""
    base = _base(output, ".xlsx", "output")
    if not source.lower().endswith(".json"):
        raise ProductError("source must be the .json file you wrote the spreadsheet spec in")
    text = jail.read(source)
    try:
        spec = sheets.parse(text, jail.read)
        data = sheets.build(spec)
        # 0.14.0: a picture of each sheet (the workshop was paid $1.84 to draw three); the first keeps its old name.
        pictures = [sheets.preview(spec, index=index) for index in range(len(spec.sheets))]
    except sheets.SheetError as exc:
        raise ProductError(f"{source}: {exc}") from None
    made = Made()
    _write(jail, made, output, data)
    shown = [f"{base}-preview.png"] + [f"{base}-sheet{n}.png" for n in range(2, len(spec.sheets) + 1)]
    for path, picture in zip(shown, pictures, strict=True):
        _write(jail, made, path, picture)
    for number in range(len(spec.sheets) + 1, sheets.MAX_SHEETS + 1):  # a sheet this version no longer has
        path = f"{base}-sheet{number}.png"
        if number > 1 and jail.size_of(path, "product") is not None:
            jail.delete(path)
            made.removed.append(path)
    parts = [f"{s.name} ({len(s.rows)} row{'s' if len(s.rows) != 1 else ''}" for s in spec.sheets]
    formulas = sum(
        1 for s in spec.sheets for row in s.rows for value in row if isinstance(value, str) and value.startswith("=")
    )
    listed = ", ".join(f"{p})" for p in parts)
    made.report.append(f"Made {output}: {len(spec.sheets)} sheet{'s' if len(spec.sheets) != 1 else ''}: {listed}.")
    extra = f"{formulas} formula{'s' if formulas != 1 else ''}" if formulas else "no formulas"
    notes = f", a 'How to use' sheet with {len(spec.notes)} lines" if spec.notes else ""
    pictures = ", ".join(f"{path} ({sheet.name})" for path, sheet in zip(shown, spec.sheets, strict=True))
    made.report.append(f"{_size(len(data))}; {extra}{notes}. Pictures of its sheets: {pictures}.")
    if spec.warnings:
        made.report.append("Check: " + " ".join(_sentence(w) for w in spec.warnings))
    if made.removed:
        made.report.append(f"Removed old sheet pictures: {_names(made.removed)}.")
    return made


# --- listing photos ---


def _pictures(jail: Jail, pages: str, height: int) -> tuple[list[images.Image.Image], list[str]]:
    """The pages to show, separated by commas: 'shop/cv.pdf#2' (a PDF page; '#1' when left out), 'shop/b.xlsx#2' or
    'shop/b.xlsx#Budget' (0.14.0: a sheet, by number or name; the first when left out) or a PNG or JPEG, each with an
    optional region to zoom in on ('@top', 0.14.0); and what each shows, whatever its name (for the photo's note)."""
    refs = [ref.strip() for ref in pages.split(",") if ref.strip()]
    if not 1 <= len(refs) <= MAX_LISTING_PAGES:
        raise ProductError(f"pages must name 1 to {MAX_LISTING_PAGES} pages, separated by commas")
    shown: list[images.Image.Image] = []
    keys: list[str] = []
    files: dict[str, bytes] = {}
    for ref in refs:
        match = _PAGE_REF.match(ref)
        if match is None:
            raise ProductError(
                f"{ref!r} is not a page: use 'file.pdf#2', 'file.xlsx#2' (a sheet) or a .png or .jpg file in your "
                "workspace, with '@top' or another region to zoom in"
            )
        path, kind, part = match.group("path"), match.group("kind").lower(), match.group("part") or ""
        name = (match.group("region") or "").lower()
        if name and name not in images.REGIONS:
            raise ProductError(f"{ref}: the region must be one of {', '.join(images.REGIONS)}")
        region = images.REGIONS.get(name, images.FULL)
        if part and kind in ("png", "jpg"):
            raise ProductError(f"{ref}: a picture has no pages; name it alone ('{path}')")
        if part and kind == "pdf" and not re.fullmatch(r"[0-9]{1,4}", part):
            raise ProductError(f"{ref}: a PDF's page is a number ('{path}#2')")
        if path not in files:
            files[path] = jail.read_bytes(path)
        number = int(part or 1) if kind == "pdf" else 1
        try:
            if kind == "pdf":
                shown.extend(images.pdf_pages(files[path], [number], height=height, region=region))
            elif kind == "xlsx":
                number, picture = sheets.picture(files[path], part.strip())
                shown.append(images.cropped(picture, region))
            else:
                picture = images.open_png(files[path], longest=2 * height)
                shown.append(images.cropped(picture, region))
        except (images.ImageError, sheets.SheetError, checks.Refused) as exc:
            raise ProductError(f"{ref}: {exc}") from None
        # what it shows, however it is named: the file's content (a copy is the same file), the page or sheet's number
        keys.append(f"{hashlib.sha256(files[path]).hexdigest()}#{number}@{name or 'all'}")
    return shown, keys


def image(
    jail: Jail,
    output: str,
    pages: str,
    title: str,
    subtitle: str = "",
    badge: str = "",
    background: str | None = None,
    accent: str | None = None,
    shape: str = "landscape",
    layout: str = "photo",
) -> Made:
    """Make ``output`` (a PNG listing photo) showing ``pages`` next to a title, a subtitle and a badge; 0.14.0: or,
    with layout 'text', the title and the subtitle's lines (separated by '|') as a list, or with layout 'poster', a
    poster at print size."""
    _base(output, ".png", "output")
    for name, value in (("background", background), ("accent", accent)):
        if value is not None and not _COLOR.fullmatch(value):
            raise ProductError(f"{name} must be a colour like #F4EFE6")
    if shape not in images.SHAPES:
        raise ProductError(f"shape must be one of {', '.join(images.SHAPES)}")
    if layout not in images.LAYOUTS:
        raise ProductError(f"layout must be one of {', '.join(images.LAYOUTS)}")
    lines = [line.strip() for line in subtitle.split("|") if line.strip()]
    if layout != "photo" and pages.strip():
        raise ProductError(f"a {layout} layout shows no pages: leave pages empty (or use layout photo)")
    try:
        if layout == "text":
            data = images.text_photo(title.strip(), lines, badge.strip(), background, accent, shape)
            shows = "text:" + "|".join([title.strip(), *lines])  # its words: the title too
        elif layout == "poster":
            data = images.poster(title.strip(), lines, background, accent, shape)
            shows = "poster:" + "|".join([title.strip(), *lines])
        else:
            if not pages.strip():
                raise ProductError("name the pages to show (or use layout text or poster)")
            pictures, keys = _pictures(jail, pages, images.SHAPES[shape][1])
            data = images.listing(pictures, title.strip(), " ".join(lines), badge.strip(), background, accent, shape)
            shows = "photo:" + ",".join(sorted(keys))
    except images.ImageError as exc:
        raise ProductError(str(exc)) from None
    # 0.14.0: the QA registry counts another title on the same pages as the same photo
    data = images.marked(data, shows)
    made = Made()
    _write(jail, made, output, data)
    if layout == "poster":
        width, height = images.poster_size(shape)
        made.report.append(
            f"Made {output}: a poster, {width} x {height} pixels ({shape}), {_size(len(data))}: at 150 dpi it prints "
            f"up to {width / 150 * 2.54:.0f} x {height / 150 * 2.54:.0f} cm."
        )
        return made
    width, height = images.SHAPES[shape]
    what = "text listing photo" if layout == "text" else "listing photo"
    made.report.append(f"Made {output}: a {shape} {what}, {width} x {height} pixels, {_size(len(data))}.")
    return made
