"""Ember's products: finished files made from what the agent writes in its workspace.

* ``document``: a Markdown file (with settings and ``:::`` layout lines, see markup.py) becomes a PDF, an editable
  Word file next to it and pictures of its first pages: ``shop/cv.pdf``, ``shop/cv.docx``, ``shop/cv-page1.png``.
* ``spreadsheet``: a JSON spec becomes an Excel file and a picture of its first table: ``shop/budget.xlsx``,
  ``shop/budget-preview.png``.
* ``image``: a listing photo made of pages of Ember's own PDFs or pictures, with a title, a subtitle and a badge.

The agent never writes the bytes of these files: Ember's code makes them from the agent's text and writes them
with ``Jail.write_bytes``. Every problem the agent can fix comes back as a ProductError naming what to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..agent.sandbox import Jail
from . import images, markup, pdf, sheets, word

PAGE_PREVIEWS = 4  # pictures of the first pages of a document
PREVIEW_DPI = 100  # an A4 page is 827 x 1169 pixels
MAX_LISTING_PAGES = 3
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_PAGE_REF = re.compile(r"^(?P<path>.+?\.pdf)(?:#(?P<page>\d{1,3}))?$", re.IGNORECASE)


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
    """Make ``output`` (an Excel file) from the JSON spec ``source``, and a picture of its first table."""
    base = _base(output, ".xlsx", "output")
    if not source.lower().endswith(".json"):
        raise ProductError("source must be the .json file you wrote the spreadsheet spec in")
    text = jail.read(source)
    try:
        spec = sheets.parse(text, jail.read)
        data = sheets.build(spec)
        picture = sheets.preview(spec)
    except sheets.SheetError as exc:
        raise ProductError(f"{source}: {exc}") from None
    made = Made()
    _write(jail, made, output, data)
    preview = f"{base}-preview.png"
    _write(jail, made, preview, picture)
    parts = [f"{s.name} ({len(s.rows)} row{'s' if len(s.rows) != 1 else ''}" for s in spec.sheets]
    formulas = sum(
        1 for s in spec.sheets for row in s.rows for value in row if isinstance(value, str) and value.startswith("=")
    )
    listed = ", ".join(f"{p})" for p in parts)
    made.report.append(f"Made {output}: {len(spec.sheets)} sheet{'s' if len(spec.sheets) != 1 else ''}: {listed}.")
    extra = f"{formulas} formula{'s' if formulas != 1 else ''}" if formulas else "no formulas"
    notes = f", a 'How to use' sheet with {len(spec.notes)} lines" if spec.notes else ""
    made.report.append(f"{_size(len(data))}; {extra}{notes}. Picture of the first sheet: {preview}.")
    if spec.warnings:
        made.report.append("Check: " + " ".join(_sentence(w) for w in spec.warnings))
    return made


# --- listing photos ---


def _pictures(jail: Jail, pages: str, height: int) -> list[images.Image.Image]:
    """The pages to show: 'shop/cv.pdf#2' (a PDF page; '#1' when left out) or a PNG, separated by commas."""
    refs = [ref.strip() for ref in pages.split(",") if ref.strip()]
    if not 1 <= len(refs) <= MAX_LISTING_PAGES:
        raise ProductError(f"pages must name 1 to {MAX_LISTING_PAGES} pages, separated by commas")
    shown: list[images.Image.Image] = []
    documents: dict[str, bytes] = {}
    for ref in refs:
        match = _PAGE_REF.match(ref)
        try:
            if match:
                path = match.group("path")
                if path not in documents:
                    documents[path] = jail.read_bytes(path)
                number = int(match.group("page") or 1)
                shown.extend(images.pdf_pages(documents[path], [number], height=height))
            elif ref.lower().endswith((".png", ".jpg")):
                shown.append(images.open_png(jail.read_bytes(ref)))
            else:
                raise ProductError(f"{ref!r} is not a page: use 'file.pdf#2' or a .png or .jpg file in your workspace")
        except images.ImageError as exc:
            raise ProductError(f"{ref}: {exc}") from None
    return shown


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
) -> Made:
    """Make ``output`` (a PNG listing photo) showing ``pages`` next to a title, a subtitle and a badge."""
    _base(output, ".png", "output")
    for name, value in (("background", background), ("accent", accent)):
        if value is not None and not _COLOR.fullmatch(value):
            raise ProductError(f"{name} must be a colour like #F4EFE6")
    if shape not in images.SHAPES:
        raise ProductError(f"shape must be one of {', '.join(images.SHAPES)}")
    height = images.SHAPES[shape][1]
    pictures = _pictures(jail, pages, height)
    try:
        data = images.listing(pictures, title.strip(), subtitle.strip(), badge.strip(), background, accent, shape)
    except images.ImageError as exc:
        raise ProductError(str(exc)) from None
    made = Made()
    _write(jail, made, output, data)
    width, height = images.SHAPES[shape]
    made.report.append(f"Made {output}: a {shape} listing photo, {width} x {height} pixels, {_size(len(data))}.")
    return made
