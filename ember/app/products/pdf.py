"""Documents as PDF files: a small layout engine drawn with fpdf2's primitives.

Blocks are laid out in flows: the main column, the sidebar and each column of a ``::: columns``, each a band of
the page with its own cursor. The engine breaks lines itself, so it knows every height before it draws: a box gets
its background first, a heading moves to the next page with the lines it heads, a table row never splits and a
table's header repeats on the next page. The same code measures (a dry flow draws nothing and never breaks pages)
and draws. Page backgrounds and the sidebar are painted as soon as a page exists, so text always lies on top.

Everything happens in memory; the caller writes the bytes through the workspace jail.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any

from fpdf import FPDF

from . import fonts
from .markup import (
    Block,
    Box,
    Callout,
    Center,
    Checklist,
    Columns,
    Divider,
    Document,
    DocumentError,
    Heading,
    Lines,
    ListBlock,
    PageBreak,
    Paragraph,
    Photo,
    Run,
    Space,
    Table,
    plain,
)
from .theme import MIN_CONTRAST, RGB, Theme, contrast, readable_on, resolve, tint
from .theme import warnings as theme_warnings

MM_PER_PT = 25.4 / 72
MAX_PAGES = 40
SIDEBAR_PADDING = 7.0
COLUMN_GAP = 6.0
BOX_PADDING = 4.0
LIST_INDENT = 5.0
WRITING_LINE = 8.0  # mm between writing lines
FOOTER_SPACE = 8.0
_TOKENS = re.compile(r"\n|[^\S\n]+|\S+")


class TooLong(DocumentError):
    pass


@dataclass
class Report:
    """What the agent learns about its document without seeing it."""

    pages: int = 0
    main_end: tuple[int, float] = (1, 0.0)  # page, share of its text area used
    sidebar_pages: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, text: str) -> None:
        if text not in self.warnings and len(self.warnings) < 12:
            self.warnings.append(text)


@dataclass
class Seg:
    text: str
    family: str
    style: str
    size: float
    width: float
    color: RGB
    url: str | None = None


@dataclass
class Word:
    segs: list[Seg]
    width: float
    space: float = 0.0  # the space after it
    hard_break: bool = False


@dataclass
class Line:
    words: list[Word]
    width: float
    last: bool  # never justified
    size: float


@dataclass(frozen=True)
class Look:
    """How text is drawn in a flow."""

    text: RGB
    heading: RGB
    rule: RGB
    align: str = "left"


class Canvas:
    """The PDF and its pages; a new page gets its background, sidebar and footer before anything else."""

    def __init__(self, theme: Theme, title: str, footer: str) -> None:
        self.theme = theme
        self.footer = footer
        pdf = FPDF(unit="mm", format=(theme.page_width, theme.page_height))
        pdf.set_auto_page_break(False)
        pdf.set_margins(0, 0, 0)
        pdf.set_title(title[:200])
        pdf.set_creator("Ember")
        for family in fonts.FAMILIES:
            for style in fonts.STYLES:
                pdf.add_font(family, style, str(fonts.path(family, style)))
        self.pdf = pdf
        self._widths: dict[tuple[str, str, float, str], float] = {}

    @property
    def pages(self) -> int:
        return self.pdf.pages_count

    def close(self) -> None:
        """Close the font files fpdf2 keeps open (it reads them lazily, until the PDF is written)."""
        for font in self.pdf.fonts.values():
            ttfont = getattr(font, "ttfont", None)
            if ttfont is not None:
                ttfont.close()

    def goto(self, number: int) -> None:
        if number > MAX_PAGES:
            raise TooLong(f"the document would be longer than {MAX_PAGES} pages; split it into several files")
        while self.pdf.pages_count < number:
            if self.pdf.pages_count:
                self.pdf.page = self.pdf.pages_count  # add_page() on an earlier page would step, not append
            self.pdf.add_page()
            self._paint_page()
        self._on(number)

    def _on(self, number: int) -> None:
        """Draw on page ``number`` from now on.

        fpdf2 writes the font, the colours and the line width into a page only when they change, so a page it comes
        back to gets the current ones written again; otherwise its text and lines would use whatever that page had
        last (a footer in the heading's font, say).
        """
        pdf = self.pdf
        if pdf.page == number:
            return
        pdf.page = number
        pdf.current_font_is_set_on_page = False
        # fpdf2 has no public way to restate them.
        pdf._out(f"{pdf.line_width * pdf.k:.2f} w")  # noqa: SLF001
        pdf._out(pdf.draw_color.serialize().upper())  # noqa: SLF001
        pdf._out(pdf.fill_color.serialize().lower())  # noqa: SLF001

    def _paint_page(self) -> None:
        t = self.theme
        if t.background != (255, 255, 255):
            self.fill(t.background)
            self.pdf.rect(0, 0, t.page_width, t.page_height, "F")
        if t.sidebar != "none":
            x = 0.0 if t.sidebar == "left" else t.page_width - t.sidebar_width
            self.fill(t.sidebar_background)
            self.pdf.rect(x, 0, t.sidebar_width, t.page_height, "F")

    def footers(self) -> None:
        """Each page's footer, once the page count is known ({page} and {pages} in the text)."""
        if not self.footer:
            return
        t = self.theme
        size = 8.0
        left, width = self.main_band()
        for number in range(1, self.pages + 1):
            self._on(number)
            text = self.footer.replace("{pages}", str(self.pages)).replace("{page}", str(number))
            parts, _ = fonts.pieces(text, t.font, "")
            total = sum(self.width(p, fam, "", size) for p, fam in parts)
            x = left + (width - total) / 2
            for piece, family in parts:
                self.pdf.set_font(family, "", size)
                self.pdf.set_text_color(*t.muted)
                self.pdf.text(x, t.page_height - t.margin / 2 - 1, piece)
                x += self.width(piece, family, "", size)

    def main_band(self) -> tuple[float, float]:
        t = self.theme
        if t.sidebar == "left":
            return t.sidebar_width + t.margin, t.page_width - t.sidebar_width - 2 * t.margin
        if t.sidebar == "right":
            return t.margin, t.page_width - t.sidebar_width - 2 * t.margin
        return t.margin, t.page_width - 2 * t.margin

    def width(self, text: str, family: str, style: str, size: float) -> float:
        key = (family, style, size, text)
        if key not in self._widths:
            self.pdf.set_font(family, style, size)
            self._widths[key] = self.pdf.get_string_width(text)
        return self._widths[key]

    def fill(self, color: RGB) -> None:
        self.pdf.set_fill_color(*color)

    def draw(self, color: RGB, width: float) -> None:
        self.pdf.set_draw_color(*color)
        self.pdf.set_line_width(width)


class Flow:
    """A band of the page with a cursor; ``dry`` flows only measure (and never break pages)."""

    def __init__(
        self, canvas: Canvas, x: float, width: float, top: float, bottom: float, page: int, y: float, dry: bool
    ) -> None:
        self.canvas = canvas
        self.x = x
        self.width = width
        self.top = top
        self.bottom = bottom
        self.page = page
        self.y = y
        self.dry = dry

    @property
    def at_top(self) -> bool:
        return self.y <= self.top + 0.01

    def fits(self, height: float) -> bool:
        return self.dry or self.y + height <= self.bottom + 0.01

    def need(self, height: float) -> None:
        """Start a new page unless ``height`` fits below the cursor (or the page is still empty)."""
        if not self.fits(height) and not self.at_top:
            self.next_page()

    def next_page(self) -> None:
        if self.dry:
            return
        self.page += 1
        self.canvas.goto(self.page)
        self.y = self.top

    def sub(self, x: float, width: float, y: float | None = None) -> Flow:
        return Flow(self.canvas, x, width, self.top, self.bottom, self.page, self.y if y is None else y, self.dry)

    def enter(self) -> None:
        if not self.dry:
            self.canvas.goto(self.page)


class Renderer:
    def __init__(self, document: Document) -> None:
        self.document = document
        self.theme = resolve(document.settings)
        title = document.settings.title or next(
            (plain(b.runs) for b in document.main if isinstance(b, Heading)), "Document"
        )
        self.canvas = Canvas(self.theme, title, document.settings.footer)
        self.report = Report(warnings=[*document.warnings, *theme_warnings(self.theme)])
        t = self.theme
        self.body_lh = t.size * t.line_height * MM_PER_PT
        self.gap = t.size * 0.62 * MM_PER_PT

    # --- the document ---

    def render(self) -> tuple[bytes, Report]:
        t = self.theme
        canvas = self.canvas
        top, bottom = t.margin, t.page_height - t.margin - (FOOTER_SPACE if self.document.settings.footer else 0)
        canvas.goto(1)
        left, width = canvas.main_band()
        main = Flow(canvas, left, width, top, bottom, 1, top, dry=False)
        self.blocks(main, self.document.main, Look(t.text, t.accent, t.accent))
        page_count = canvas.pages
        area = bottom - top
        self.report.main_end = (main.page, max(0.0, min(1.0, (main.y - top) / area)))
        if t.sidebar != "none":
            x = 0.0 if t.sidebar == "left" else t.page_width - t.sidebar_width
            side = Flow(canvas, x + SIDEBAR_PADDING, t.sidebar_width - 2 * SIDEBAR_PADDING, top, bottom, 1, top, False)
            side.enter()
            look = Look(t.sidebar_text, t.sidebar_text, t.sidebar_text)
            self.blocks(side, self.document.sidebar, look)
            self.report.sidebar_pages = side.page
            if side.page > page_count:
                self.report.warn(f"the sidebar runs on to page {side.page}, past the main text: shorten the sidebar")
        self.report.pages = canvas.pages
        canvas.footers()
        page, used = self.report.main_end
        if page > 1 and used < 0.15 and self.report.sidebar_pages < page:
            self.report.warn(
                f"page {page} holds only a few lines: shorten the text a little so it fits on {page - 1} page(s), "
                "or fill the page"
            )
        return bytes(canvas.pdf.output()), self.report

    # --- blocks ---

    def blocks(self, flow: Flow, blocks: list[Block], look: Look) -> None:
        for block in blocks:
            self.block(flow, block, look)

    def block(self, flow: Flow, block: Block, look: Look) -> None:  # noqa: C901 - one dispatch
        if isinstance(block, Heading):
            self.heading(flow, block, look)
        elif isinstance(block, Paragraph):
            self.paragraph(flow, block.runs, look)
        elif isinstance(block, ListBlock):
            self.listing(flow, block, look)
        elif isinstance(block, Checklist):
            self.checklist(flow, block, look)
        elif isinstance(block, Table):
            self.table(flow, block, look)
        elif isinstance(block, Divider):
            flow.need(4)
            if not flow.dry:
                self.canvas.draw(tint(look.rule, 0.45), 0.3)
                self.canvas.pdf.line(flow.x, flow.y + 2, flow.x + flow.width, flow.y + 2)
            flow.y += 4
        elif isinstance(block, Space):
            flow.y += block.mm
            if not flow.fits(0):
                flow.next_page()
        elif isinstance(block, Photo):
            self.photo(flow, block, look)
        elif isinstance(block, Lines):
            self.writing_lines(flow, block, look)
        elif isinstance(block, PageBreak):
            if not flow.at_top:
                flow.next_page()
        elif isinstance(block, Callout):
            self.panel(flow, block.blocks, look, tint(look.heading, 0.92), bar=True)
        elif isinstance(block, Box):
            background = _rgb(block.background) if block.background else tint(look.heading, 0.9)
            self.panel(flow, block.blocks, look, background, bar=False)
        elif isinstance(block, Center):
            self.blocks(flow, block.blocks, replace(look, align="center"))
        elif isinstance(block, Columns):
            self.columns(flow, block, look)

    def heading(self, flow: Flow, block: Heading, look: Look) -> None:
        t = self.theme
        level = block.level
        size = t.h_sizes[level - 1]
        upper = t.upper[level - 1]
        runs = [Run(r.text.upper() if upper else r.text, True, r.italic, r.url) for r in block.runs]
        band = level == 2 and t.band
        color = readable_on(look.heading) if band else look.heading
        pad = 2.0 if band else 0.0
        words = self.words(runs, t.heading_font, size, color)
        lines = self.break_lines(words, flow.width - 2 * pad)
        lh = size * 1.18 * MM_PER_PT
        before = 0.0 if flow.at_top else {1: 0.0, 2: 4.5, 3: 3.0}[level]
        after = {1: 2.0, 2: 2.4, 3: 1.2}[level]
        rule = level == 2 and t.rule and not band
        height = len(lines) * lh + (1.2 if rule else 0.0) + 2 * pad * 0.5
        flow.need(before + height + after + 2 * self.body_lh)  # never alone at the bottom of a page
        flow.y += 0.0 if flow.at_top else before
        if band and not flow.dry:
            self.canvas.fill(look.heading)
            self.canvas.pdf.rect(flow.x, flow.y, flow.width, len(lines) * lh + pad, "F")
        y = flow.y + pad * 0.5
        for line in lines:
            self.draw_line(flow, line, flow.x + pad, y, flow.width - 2 * pad, lh, look.align)
            y += lh
        flow.y = y + pad * 0.5
        if rule:
            if not flow.dry:
                self.canvas.draw(look.rule, 0.35)
                self.canvas.pdf.line(flow.x, flow.y + 0.6, flow.x + flow.width, flow.y + 0.6)
            flow.y += 1.2
        flow.y += after

    def paragraph(self, flow: Flow, runs: list[Run], look: Look, gap: bool = True) -> None:
        t = self.theme
        lines = self.break_lines(self.words(runs, t.font, t.size, look.text), flow.width)
        for index, line in enumerate(lines):
            flow.need(self.body_lh * (2 if index == 0 and len(lines) > 1 else 1))  # no single line at a page end
            self.draw_line(flow, line, flow.x, flow.y, flow.width, self.body_lh, look.align)
            flow.y += self.body_lh
        if gap:
            flow.y += self.gap

    def listing(self, flow: Flow, block: ListBlock, look: Look) -> None:
        t = self.theme
        marker_width = LIST_INDENT
        if block.ordered:
            widest = self.canvas.width(f"{len(block.items)}.", t.font, "", t.size)
            marker_width = max(LIST_INDENT, widest + 2)
        for number, item in enumerate(block.items, 1):
            lines = self.break_lines(self.words(item, t.font, t.size, look.text), flow.width - marker_width)
            for index, line in enumerate(lines):
                flow.need(self.body_lh)
                if index == 0 and not flow.dry:
                    marker = f"{number}." if block.ordered else "•"
                    style = "B" if block.ordered else ""
                    self.text(marker, flow.x, flow.y, self.body_lh, t.font, style, t.size, look.heading)
                self.draw_line(
                    flow, line, flow.x + marker_width, flow.y, flow.width - marker_width, self.body_lh, "left"
                )
                flow.y += self.body_lh
            flow.y += 0.8
        flow.y += self.gap - 0.8

    def checklist(self, flow: Flow, block: Checklist, look: Look) -> None:
        t = self.theme
        box = t.size * MM_PER_PT * 0.85
        indent = box + 2.5
        for checked, item in block.items:
            lines = self.break_lines(self.words(item, t.font, t.size, look.text), flow.width - indent)
            for index, line in enumerate(lines):
                flow.need(self.body_lh)
                if index == 0 and not flow.dry:
                    top = flow.y + (self.body_lh - box) / 2
                    self.canvas.draw(look.heading, 0.3)
                    self.canvas.pdf.rect(flow.x, top, box, box, "D")
                    if checked:
                        self.canvas.draw(look.heading, 0.45)
                        pdf = self.canvas.pdf
                        pdf.line(flow.x + box * 0.2, top + box * 0.55, flow.x + box * 0.42, top + box * 0.78)
                        pdf.line(flow.x + box * 0.42, top + box * 0.78, flow.x + box * 0.82, top + box * 0.2)
                self.draw_line(flow, line, flow.x + indent, flow.y, flow.width - indent, self.body_lh, "left")
                flow.y += self.body_lh
            flow.y += 0.8
        flow.y += self.gap - 0.8

    def photo(self, flow: Flow, block: Photo, look: Look) -> None:
        width = min(block.width_mm, flow.width)
        height = block.height_mm * width / block.width_mm
        flow.need(height + self.gap)
        if not flow.dry:
            x = flow.x + (flow.width - width) / 2
            pdf = self.canvas.pdf
            # The text colour, faint: a placeholder that shows on a white page and on a dark sidebar alike.
            with pdf.local_context(fill_opacity=0.12, stroke_opacity=0.55):
                self.canvas.fill(look.text)
                pdf.rect(x, flow.y, width, height, "F")
                self.canvas.draw(look.text, 0.3)
                pdf.set_dash_pattern(dash=1.2, gap=1.0)
                pdf.rect(x, flow.y, width, height, "D")
                pdf.set_dash_pattern()
            if block.label:
                size = 8.5
                w = self.canvas.width(block.label, self.theme.font, "", size)
                self.text(
                    block.label, x + (width - w) / 2, flow.y + height / 2 - 2, 4, self.theme.font, "", size, look.text
                )
        flow.y += height + self.gap

    def writing_lines(self, flow: Flow, block: Lines, look: Look) -> None:
        for _ in range(block.count):
            flow.need(WRITING_LINE)
            if not flow.dry:
                self.canvas.draw(tint(look.text, 0.6), 0.2)
                y = flow.y + WRITING_LINE - 1
                self.canvas.pdf.line(flow.x, y, flow.x + flow.width, y)
            flow.y += WRITING_LINE
        flow.y += self.gap

    def panel(self, flow: Flow, blocks: list[Block], look: Look, background: RGB, bar: bool) -> None:
        """A box or a callout: its background, then its blocks; never split across pages."""
        pad = BOX_PADDING if not bar else 3.0
        left = pad + (1.5 if bar else 0.0)
        inner = flow.width - left - pad
        text = look.text
        if contrast(text, background) < MIN_CONTRAST:
            text = readable_on(background)
        inside = replace(look, text=text, heading=look.heading if contrast(look.heading, background) >= 2 else text)
        height = self.measure(blocks, inner, inside) - self.gap + 2 * pad
        if not flow.fits(height):
            if height <= flow.bottom - flow.top:
                flow.next_page()
            else:
                self.report.warn("a box is taller than a page, so it is drawn without its background: shorten it")
                self.blocks(flow, blocks, look)
                return
        if not flow.dry:
            self.canvas.fill(background)
            self.canvas.pdf.rect(flow.x, flow.y, flow.width, height, "F", round_corners=not bar, corner_radius=1.8)
            if bar:
                self.canvas.fill(look.heading)
                self.canvas.pdf.rect(flow.x, flow.y, 1.2, height, "F")
        sub = flow.sub(flow.x + left, inner, flow.y + pad)
        sub.bottom = math.inf  # it fits: never break inside
        self.blocks(sub, blocks, inside)
        flow.y += height + self.gap

    def columns(self, flow: Flow, block: Columns, look: Look) -> None:
        total = sum(block.ratios)
        usable = flow.width - COLUMN_GAP * (len(block.columns) - 1)
        widths = [usable * r / total for r in block.ratios]
        if flow.dry:
            flow.y += max(self.measure(col, w, look) for col, w in zip(block.columns, widths, strict=True))
            return
        ends: list[tuple[int, float]] = []
        x = flow.x
        for column, width in zip(block.columns, widths, strict=True):
            sub = flow.sub(x, width)
            sub.enter()
            self.blocks(sub, column, look)
            ends.append((sub.page, sub.y))
            x += width + COLUMN_GAP
        flow.page, flow.y = max(ends)
        flow.enter()

    def table(self, flow: Flow, block: Table, look: Look) -> None:
        t = self.theme
        style = t.table
        n = len(block.align)
        pad_x, pad_y = 1.8, 1.1
        rows = ([block.header] if block.header else []) + block.rows
        header_look = self._header_look(look, style)
        # Column widths: at least the longest word, then by how much text each column holds.
        minimum = [2 * pad_x + 4.0] * n
        natural = [2 * pad_x + 4.0] * n
        for r, row in enumerate(rows):
            bold = r == 0 and block.header is not None
            for c, cell in enumerate(row):
                words = self.words(cell, t.font, t.size, header_look.text if bold else look.text, bold=bold)
                if words:
                    minimum[c] = max(minimum[c], max(w.width for w in words) + 2 * pad_x)
                    natural[c] = max(natural[c], sum(w.width + w.space for w in words) + 2 * pad_x)
        widths = _share(flow.width, minimum, natural)
        if sum(minimum) > flow.width + 0.01:
            self.report.warn("a table is too wide for its column: words in it were split; use fewer columns")
        heights: list[float] = []
        cell_lines: list[list[list[Line]]] = []
        for r, row in enumerate(rows):
            bold = r == 0 and block.header is not None
            # 0.21.0: the header's words in the header's colour (they were the body's, #222 on #2C3E50: 1.45 to 1)
            colour = header_look.text if bold else look.text
            lines = [
                self.break_lines(self.words(cell, t.font, t.size, colour, bold=bold), widths[c] - 2 * pad_x)
                for c, cell in enumerate(row)
            ]
            cell_lines.append(lines)
            heights.append(max(len(cl) for cl in lines) * self.body_lh + 2 * pad_y)
        header_height = heights[0] if block.header else 0.0
        if flow.dry:
            flow.y += sum(heights) + self.gap
            return
        for r in range(len(rows)):
            is_header = r == 0 and block.header is not None
            height = heights[r]
            if not flow.fits(height):
                if height > flow.bottom - flow.top - header_height:
                    self.report.warn("a table row is taller than a page: shorten its cells")
                else:
                    flow.next_page()
                    if block.header and not is_header:
                        self._row(flow, cell_lines[0], widths, block.align, heights[0], header_look, True, style, 0)
            self._row(flow, cell_lines[r], widths, block.align, height, header_look if is_header else look, is_header,
                      style, r)  # fmt: skip
        flow.y += self.gap

    def _header_look(self, look: Look, style: str) -> Look:
        if style == "plain":
            return look
        return replace(look, text=readable_on(look.heading))

    def _row(
        self,
        flow: Flow,
        lines: list[list[Line]],
        widths: list[float],
        align: list[str],
        height: float,
        look: Look,
        header: bool,
        style: str,
        index: int,
    ) -> None:
        pdf = self.canvas.pdf
        x = flow.x
        if header and style != "plain":
            self.canvas.fill(look.heading if look.text != look.heading else self.theme.accent)
            pdf.rect(flow.x, flow.y, sum(widths), height, "F")
        elif style == "zebra" and index % 2 == 0:
            self.canvas.fill(tint(self.theme.accent, 0.93))
            pdf.rect(flow.x, flow.y, sum(widths), height, "F")
        for c, cell in enumerate(lines):
            y = flow.y + 1.1
            for line in cell:
                self.draw_line(flow, line, x + 1.8, y, widths[c] - 3.6, self.body_lh, align[c])
                y += self.body_lh
            if style == "grid":
                self.canvas.draw(tint(self.theme.muted, 0.55), 0.2)
                pdf.rect(x, flow.y, widths[c], height, "D")
            x += widths[c]
        if style in ("lines", "zebra") or (style == "plain" and header):
            self.canvas.draw(tint(self.theme.muted, 0.6 if not header else 0.3), 0.25)
            pdf.line(flow.x, flow.y + height, flow.x + sum(widths), flow.y + height)
        flow.y += height

    # --- measuring ---

    def measure(self, blocks: list[Block], width: float, look: Look) -> float:
        dry = Flow(self.canvas, 0.0, width, 0.0, math.inf, 1, 0.0, dry=True)
        self.blocks(dry, blocks, look)
        return dry.y

    # --- text ---

    def words(self, runs: list[Run], family: str, size: float, color: RGB, bold: bool = False) -> list[Word]:
        words: list[Word] = []
        current: list[Seg] = []

        def finish(space: float = 0.0, hard: bool = False) -> None:
            nonlocal current
            if current or hard:
                words.append(Word(current, sum(s.width for s in current), space, hard))
            current = []

        for run in runs:
            style = fonts.style_of(run.bold or bold, run.italic)
            for piece in _TOKENS.findall(run.text):
                if piece == "\n":
                    finish(hard=True)
                elif piece.isspace():
                    if current:
                        finish(space=self.canvas.width(" ", family, style, size))
                else:
                    parts, missing = fonts.pieces(piece, family, style)
                    if missing:
                        self.report.warn(
                            f"no font has {''.join(sorted(missing))!r}: shown as '?'; use other characters"
                        )
                    for text, fam in parts:
                        width = self.canvas.width(text, fam, style, size)
                        current.append(Seg(text, fam, style, size, width, color, run.url))
        finish()
        return words

    def break_lines(self, words: list[Word], width: float) -> list[Line]:
        lines: list[Line] = []
        current: list[Word] = []
        used = 0.0
        size = words[0].segs[0].size if words and words[0].segs else self.theme.size
        for word in words:
            if word.width > width and word.segs:
                for piece in self._split(word, width):
                    if current:
                        lines.append(Line(current, used, False, size))
                    current, used = [piece], piece.width
                if word.hard_break:
                    lines.append(Line(current, used, True, size))
                    current, used = [], 0.0
                continue
            gap = current[-1].space if current else 0.0
            if current and used + gap + word.width > width + 0.01:
                lines.append(Line(current, used, False, size))
                current, used, gap = [], 0.0, 0.0
            current.append(word)
            used += gap + word.width
            if word.hard_break:
                lines.append(Line(current, used, True, size))
                current, used = [], 0.0
        if current:
            lines.append(Line(current, used, True, size))
        return lines

    def _split(self, word: Word, width: float) -> list[Word]:
        """A word wider than its line, split into pieces that fit (reported: it reads badly)."""
        self.report.warn(
            f"a word was too wide for its column and was split ({''.join(s.text for s in word.segs)[:30]!r})"
        )
        pieces: list[Word] = []
        current: list[Seg] = []
        used = 0.0
        for seg in word.segs:
            for char in seg.text:
                w = self.canvas.width(char, seg.family, seg.style, seg.size)
                if current and used + w > width:
                    pieces.append(Word(current, used))
                    current, used = [], 0.0
                if current and current[-1].family == seg.family and current[-1].style == seg.style:
                    last = current[-1]
                    current[-1] = replace(last, text=last.text + char, width=last.width + w)
                else:
                    current.append(replace(seg, text=char, width=w))
                used += w
        if current:
            pieces.append(Word(current, used, word.space, False))
        return pieces

    def draw_line(self, flow: Flow, line: Line, x: float, y: float, width: float, lh: float, align: str) -> None:
        if flow.dry or not line.words:
            return
        free = max(0.0, width - line.width)
        gaps = len(line.words) - 1
        extra = 0.0
        if align == "center":
            x += free / 2
        elif align == "right":
            x += free
        elif align == "justify" and not line.last and gaps > 0:
            extra = free / gaps
        pdf = self.canvas.pdf
        size_mm = line.size * MM_PER_PT
        ascent, _ = fonts.metrics(line.words[0].segs[0].family if line.words[0].segs else self.theme.font)
        baseline = y + (lh - size_mm) / 2 + ascent * size_mm * 0.92
        for index, word in enumerate(line.words):
            for seg in word.segs:
                pdf.set_font(seg.family, seg.style, seg.size)
                pdf.set_text_color(*seg.color)
                pdf.text(x, baseline, seg.text)
                if seg.url:
                    pdf.link(x, y, seg.width, lh, seg.url)
                    self.canvas.draw(seg.color, 0.2)
                    pdf.line(x, baseline + 0.6, x + seg.width, baseline + 0.6)
                x += seg.width
            if index < gaps:
                x += word.space + extra

    def text(self, text: str, x: float, y: float, lh: float, family: str, style: str, size: float, color: RGB) -> None:
        size_mm = size * MM_PER_PT
        ascent, _ = fonts.metrics(family)
        pdf = self.canvas.pdf
        pdf.set_font(family, style, size)
        pdf.set_text_color(*color)
        pdf.text(x, y + (lh - size_mm) / 2 + ascent * size_mm * 0.92, text)


def _share(total: float, minimum: list[float], natural: list[float]) -> list[float]:
    """Column widths that fill ``total``: at least each minimum, the rest by how much text a column holds."""
    low = sum(minimum)
    if low >= total:
        return [m * total / low for m in minimum]
    wanted = [max(0.0, n - m) for n, m in zip(natural, minimum, strict=True)]
    spare = total - low
    if sum(wanted) <= spare:  # everything fits unwrapped: widen by content
        extra = spare - sum(wanted)
        weights = natural if sum(natural) else [1.0] * len(natural)
        return [m + w + extra * n / sum(weights) for m, w, n in zip(minimum, wanted, weights, strict=True)]
    return [m + spare * w / sum(wanted) for m, w in zip(minimum, wanted, strict=True)]


def _rgb(value: str) -> RGB:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def render(document: Document) -> tuple[bytes, Report]:
    """The PDF of a parsed document and what the agent should know about its layout."""
    renderer = Renderer(document)
    try:
        return renderer.render()
    finally:
        renderer.canvas.close()


def describe(report: Report) -> dict[str, Any]:
    return {
        "pages": report.pages,
        "main_ends": {"page": report.main_end[0], "used": round(report.main_end[1], 2)},
        "sidebar_pages": report.sidebar_pages,
        "warnings": list(report.warnings),
    }
