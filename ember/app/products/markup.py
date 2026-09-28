"""Ember's document source: markdown with a settings block and a few layout lines.

The agent writes a document as a text file in its workspace (in parts, if it is long), then renders it into a PDF,
a Word file and page images. The format is plain markdown, which the model writes fluently, plus:

* a settings block at the top between two ``---`` lines (``key: value``, see ``SETTINGS``);
* layout lines starting with ``:::``: ``sidebar`` and ``main`` (which column the following text goes to),
  containers closed by a line with just ``:::`` (``box``, ``columns`` with ``column`` between its columns,
  ``center``), and single lines (``photo 35x45 Label``, ``lines 6``, ``space 8``, ``pagebreak``).

Parsing never evaluates anything: the result is a tree of plain dataclasses that the renderers draw. Every problem
names its line, so the agent can fix its source; limits keep one document within what a cycle can render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_BLOCKS = 2_000
MAX_DEPTH = 4
MAX_TABLE_COLUMNS = 12
MAX_TABLE_ROWS = 300
MAX_LINES = 60
MAX_SPACE_MM = 120.0
THEMES = ("modern", "classic", "minimal", "bold")
FONT_KEYS = {"sans": "sans", "calibri": "sans", "carlito": "sans", "serif": "serif", "cambria": "serif",
             "caladea": "serif", "display": "display", "poppins": "display"}  # fmt: skip
TABLE_STYLES = ("lines", "grid", "zebra", "plain")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_DIRECTIVE = re.compile(r"^:::\s*(.*?)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_BULLET = re.compile(r"^([-*+])\s+(.*)$")
_NUMBER = re.compile(r"^(\d{1,3})[.)]\s+(.*)$")
_CHECK = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
_ALIGN_CELL = re.compile(r"^\s*(:?)-{3,}(:?)\s*$")
_URL = re.compile(r"^(?:https://|mailto:)[^\s<>\"']{1,500}$")


class DocumentError(ValueError):
    """A source the renderers can't use; the message names the line and is shown to the agent."""


# --- the document tree ---


@dataclass(frozen=True)
class Run:
    """A piece of text in one style; ``url`` makes it a link (https or mailto only)."""

    text: str
    bold: bool = False
    italic: bool = False
    url: str | None = None


@dataclass
class Heading:
    level: int  # 1-3
    runs: list[Run]


@dataclass
class Paragraph:
    runs: list[Run]  # "\n" in a run's text is a line break


@dataclass
class ListBlock:
    items: list[list[Run]]
    ordered: bool = False


@dataclass
class Checklist:
    items: list[tuple[bool, list[Run]]]


@dataclass
class Table:
    header: list[list[Run]] | None  # None: a layout table without a header row
    rows: list[list[list[Run]]]
    align: list[str]  # "left", "center" or "right" per column


@dataclass
class Divider:
    pass


@dataclass
class Space:
    mm: float


@dataclass
class Photo:
    width_mm: float
    height_mm: float
    label: str


@dataclass
class Lines:
    count: int


@dataclass
class PageBreak:
    pass


@dataclass
class Callout:
    blocks: list[Block]


@dataclass
class Box:
    blocks: list[Block]
    background: str | None = None


@dataclass
class Center:
    blocks: list[Block]


@dataclass
class Columns:
    columns: list[list[Block]]
    ratios: list[float]


Block = Heading | Paragraph | ListBlock | Checklist | Table | Divider | Space | Photo | Lines | PageBreak | Callout
Block |= Box | Center | Columns


@dataclass
class Settings:
    title: str = ""
    theme: str = "modern"
    page: str = "A4"
    landscape: bool = False
    font: str | None = None  # None: the theme's
    heading_font: str | None = None
    size: float | None = None
    line_height: float | None = None
    margin: float | None = None
    accent: str | None = None
    text: str | None = None
    muted: str | None = None
    background: str | None = None
    sidebar: str = "none"  # "left", "right" or "none"
    sidebar_width: float = 62.0
    sidebar_background: str | None = None
    sidebar_text: str | None = None
    table: str | None = None
    footer: str = ""


@dataclass
class Document:
    settings: Settings
    main: list[Block]
    sidebar: list[Block]
    warnings: list[str] = field(default_factory=list)


# --- settings ---


def _number(key: str, raw: str, low: float, high: float) -> float:
    try:
        value = float(raw.removesuffix("mm").removesuffix("pt").strip())
    except ValueError:
        raise DocumentError(f"{key} must be a number, like {low:g}") from None
    if not low <= value <= high:
        raise DocumentError(f"{key} must be between {low:g} and {high:g}")
    return value


def _color(key: str, raw: str) -> str:
    if not _COLOR.match(raw):
        raise DocumentError(f"{key} must be a colour like #2C3E50")
    return raw.upper()


def _choice(key: str, raw: str, choices: tuple[str, ...]) -> str:
    value = raw.lower()
    if value not in choices:
        raise DocumentError(f"{key} must be one of: {', '.join(choices)}")
    return value


def _font(key: str, raw: str) -> str:
    value = FONT_KEYS.get(raw.lower())
    if value is None:
        raise DocumentError(f"{key} must be sans (Calibri), serif (Cambria) or display (Poppins)")
    return value


def _bool(key: str, raw: str) -> bool:
    value = raw.lower()
    if value in ("true", "yes", "on"):
        return True
    if value in ("false", "no", "off"):
        return False
    raise DocumentError(f"{key} must be true or false")


SETTINGS: dict[str, Any] = {
    "title": lambda k, v: v[:200],
    "theme": lambda k, v: _choice(k, v, THEMES),
    "page": lambda k, v: {"a4": "A4", "letter": "Letter"}.get(v.lower()) or _choice(k, v, ("A4", "Letter")),
    "landscape": _bool,
    "font": _font,
    "heading_font": _font,
    "size": lambda k, v: _number(k, v, 7, 16),
    "line_height": lambda k, v: _number(k, v, 1.0, 2.2),
    "margin": lambda k, v: _number(k, v, 5, 35),
    "accent": _color,
    "text": _color,
    "muted": _color,
    "background": _color,
    "sidebar": lambda k, v: _choice(k, v, ("left", "right", "none")),
    "sidebar_width": lambda k, v: _number(k, v, 35, 110),
    "sidebar_background": _color,
    "sidebar_text": _color,
    "table": lambda k, v: _choice(k, v, TABLE_STYLES),
    "footer": lambda k, v: v[:120],
}


def _settings(lines: list[str], start: int) -> Settings:
    settings = Settings()
    seen: set[str] = set()
    for offset, line in enumerate(lines):
        number = start + offset
        text = line.strip()
        if not text or text.startswith("#"):  # no comments after a value: colours start with '#'
            continue
        key, colon, raw = text.partition(":")
        key = key.strip().lower().replace("-", "_")
        if not colon or not key:
            raise DocumentError(f"line {number}: settings are 'key: value' lines, like 'theme: modern'")
        if key not in SETTINGS:
            raise DocumentError(f"line {number}: unknown setting {key!r}; the settings are {', '.join(SETTINGS)}")
        if key in seen:
            raise DocumentError(f"line {number}: {key} is set twice")
        seen.add(key)
        value = raw.strip().strip("\"'").strip()
        try:
            setattr(settings, key, SETTINGS[key](key, value))
        except DocumentError as exc:
            raise DocumentError(f"line {number}: {exc}") from None
    return settings


# --- inline text ---

_INLINE = re.compile(
    r"\\(?P<escaped>[\\`*_{}\[\]()#+\-.!|>~:])"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<link>\[(?P<label>[^\]\n]{1,300})\]\((?P<url>[^)\s]{1,500})\))"
    r"|(?P<mark>\*{1,3}|_{1,3})"
)


@dataclass
class _Mark:
    char: str
    length: int
    can_open: bool
    can_close: bool
    role: str = ""  # "open" or "close" once paired; unpaired markers are text
    used: int = 0  # how many of its characters are emphasis (1 italic, 2 bold, 3 both)


def inline(text: str) -> list[Run]:
    """Markdown inline text as styled runs: **bold**, *italic*, ***both***, `code` (plain), [label](url), escapes.

    Emphasis follows markdown's flanking rules closely enough for real text: a marker opens before a non-space
    and closes after one, and underscores inside words (snake_case) stay literal. A closer pairs with the nearest
    open marker of the same character (openers left between them stay text); unmatched markers are text.
    """
    items: list[Any] = []  # str (text), (label, url) or _Mark
    pos = 0
    for match in _INLINE.finditer(text):
        if match.start() > pos:
            items.append(text[pos : match.start()])
        if match["escaped"] is not None:
            items.append(match["escaped"])
        elif match["code"] is not None:
            items.append(match["code"][1:-1])
        elif match["link"] is not None:
            items.append((match["label"], match["url"]))
        else:
            mark = match["mark"]
            before = text[match.start() - 1] if match.start() > 0 else " "
            after = text[match.end()] if match.end() < len(text) else " "
            can_open = not after.isspace()
            can_close = not before.isspace()
            if mark[0] == "_":  # intraword underscores are literal
                can_open = can_open and not before.isalnum()
                can_close = can_close and not after.isalnum()
            items.append(_Mark(mark[0], len(mark), can_open, can_close))
        pos = match.end()
    if pos < len(text):
        items.append(text[pos:])
    openers: list[_Mark] = []
    for item in items:
        if not isinstance(item, _Mark):
            continue
        if item.can_close:
            at = next((k for k in range(len(openers) - 1, -1, -1) if openers[k].char == item.char), None)
            if at is not None:
                opener = openers[at]
                used = min(opener.length, item.length)
                opener.role, opener.used = "open", used
                item.role, item.used = "close", used
                del openers[at:]
                continue
        if item.can_open:
            openers.append(item)
    runs: list[Run] = []
    bold = italic = 0
    for item in items:
        if isinstance(item, _Mark):
            if not item.role:
                _add(runs, item.char * item.length, bold, italic)
                continue
            leftover = item.char * (item.length - item.used)  # stays outside the emphasis
            if item.role == "open":
                _add(runs, leftover, bold, italic)
            delta = 1 if item.role == "open" else -1
            if item.used >= 2:
                bold += delta
            if item.used % 2 == 1:
                italic += delta
            if item.role == "close":
                _add(runs, leftover, bold, italic)
        elif isinstance(item, tuple):
            label, url = item
            if _URL.match(url):
                runs.append(Run(label, bold > 0, italic > 0, url))
            else:
                _add(runs, f"{label} ({url})" if url != label else label, bold, italic)
        else:
            _add(runs, item, bold, italic)
    return runs


def _add(runs: list[Run], text: str, bold: int, italic: int) -> None:
    if not text:
        return
    style = (bold > 0, italic > 0)
    if runs and (runs[-1].bold, runs[-1].italic) == style and runs[-1].url is None:
        runs[-1] = Run(runs[-1].text + text, *style)
    else:
        runs.append(Run(text, *style))


def plain(runs: list[Run]) -> str:
    return "".join(r.text for r in runs)


# --- blocks ---


@dataclass
class _Frame:
    """An open container while parsing: what it collects, and where it started."""

    kind: str  # "top", "box", "columns", "center"
    line: int
    blocks: list[Block] = field(default_factory=list)
    columns: list[list[Block]] = field(default_factory=list)
    background: str | None = None
    ratios: list[float] = field(default_factory=list)


class _Parser:
    def __init__(self, lines: list[str], first: int, settings: Settings) -> None:
        self.lines = lines
        self.first = first  # the file's line number of lines[0]
        self.settings = settings
        self.main: list[Block] = []
        self.sidebar: list[Block] = []
        self.target = self.main
        self.stack: list[_Frame] = []
        self.count = 0
        self.warnings: list[str] = []

    # where parsed blocks go
    def _into(self) -> list[Block]:
        if not self.stack:
            return self.target
        frame = self.stack[-1]
        if frame.kind == "columns":
            if not frame.columns:
                frame.columns.append([])
            return frame.columns[-1]
        return frame.blocks

    def _emit(self, block: Block, number: int) -> None:
        self.count += 1
        if self.count > MAX_BLOCKS:
            raise DocumentError(f"line {number}: more than {MAX_BLOCKS:,} blocks; split the document")
        self._into().append(block)

    def parse(self) -> Document:
        i = 0
        while i < len(self.lines):
            line = self.lines[i].rstrip()
            number = self.first + i
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            directive = _DIRECTIVE.match(stripped)
            if directive:
                self._directive(directive[1], number)
                i += 1
            elif heading := _HEADING.match(stripped):
                level = len(heading[1])
                if level > 3:
                    self.warnings.append(f"line {number}: headings go down to ###; '{heading[1]}' is shown as ###")
                self._emit(Heading(min(level, 3), inline(heading[2])), number)
                i += 1
            elif _RULE.match(stripped):
                self._emit(Divider(), number)
                i += 1
            elif stripped.startswith("|"):
                i = self._table(i)
            elif stripped.startswith(">"):
                i = self._callout(i)
            elif _CHECK.match(stripped) or _BULLET.match(stripped) or _NUMBER.match(stripped):
                i = self._list(i)
            else:
                i = self._paragraph(i)
        if self.stack:
            frame = self.stack[-1]
            raise DocumentError(f"line {frame.line}: '::: {frame.kind}' is never closed; end it with a ':::' line")
        if self.sidebar and self.settings.sidebar == "none":
            raise DocumentError("there is a '::: sidebar' but no sidebar: set 'sidebar: left' or 'sidebar: right'")
        return Document(self.settings, self.main, self.sidebar, self.warnings)

    def _directive(self, text: str, number: int) -> None:
        name, _, rest = text.partition(" ")
        name = name.lower()
        rest = rest.strip()
        if not name:  # ":::" closes the innermost container
            if not self.stack:
                raise DocumentError(f"line {number}: ':::' closes nothing (no box, columns or center is open)")
            frame = self.stack.pop()
            if frame.kind == "box":
                block: Block = Box(frame.blocks, frame.background)
            elif frame.kind == "center":
                block = Center(frame.blocks)
            else:
                columns = frame.columns or [[]]
                ratios = frame.ratios or [1.0] * len(columns)
                if len(ratios) != len(columns):
                    raise DocumentError(
                        f"line {frame.line}: '::: columns {':'.join(f'{r:g}' for r in ratios)}' names "
                        f"{len(ratios)} columns, but {len(columns)} follow"
                    )
                block = Columns(columns, ratios)
            self._emit(block, number)
            return
        if name in ("sidebar", "main"):
            if self.stack:
                raise DocumentError(f"line {number}: '::: {name}' can't be inside a box, columns or center")
            self.target = self.sidebar if name == "sidebar" else self.main
            return
        if name in ("box", "columns", "center"):
            if len(self.stack) >= MAX_DEPTH:
                raise DocumentError(f"line {number}: containers can be nested at most {MAX_DEPTH} deep")
            frame = _Frame(name, number)
            if name == "box" and rest:
                frame.background = _at(number, lambda: _color("box", rest))
            if name == "columns" and rest:
                frame.ratios = _ratios(rest, number)
            self.stack.append(frame)
            return
        if name == "column":
            if not self.stack or self.stack[-1].kind != "columns":
                raise DocumentError(f"line {number}: '::: column' belongs inside '::: columns'")
            frame = self.stack[-1]
            frame.columns.append([])  # the first '::: column' starts column 1, unless text already did
            if len(frame.columns) > 4:
                raise DocumentError(f"line {number}: at most 4 columns")
            return
        if name == "photo":
            size, _, label = rest.partition(" ")
            match = re.fullmatch(r"(\d{1,3}(?:\.\d)?)[xX](\d{1,3}(?:\.\d)?)", size)
            if not match:
                raise DocumentError(f"line {number}: write '::: photo 35x45' (width x height in mm), then a label")
            width, height = float(match[1]), float(match[2])
            if not (10 <= width <= 180 and 10 <= height <= 250):
                raise DocumentError(f"line {number}: a photo box is 10-180 mm wide and 10-250 mm high")
            self._emit(Photo(width, height, label.strip()[:60]), number)
            return
        if name == "lines":
            count = _at(number, lambda: int(_number("lines", rest or "5", 1, MAX_LINES)))
            self._emit(Lines(count), number)
            return
        if name == "space":
            self._emit(Space(_at(number, lambda: _number("space", rest or "5", 0, MAX_SPACE_MM))), number)
            return
        if name == "pagebreak":
            self._emit(PageBreak(), number)
            return
        raise DocumentError(
            f"line {number}: unknown layout line '::: {name}'; use sidebar, main, box, columns, column, center, "
            "photo, lines, space or pagebreak"
        )

    def _paragraph(self, i: int) -> int:
        start = i
        parts: list[str] = []
        while i < len(self.lines):
            line = self.lines[i]
            stripped = line.strip()
            if not stripped or _is_block_start(stripped):
                break
            hard = line.endswith(("  ", "\\"))
            text = stripped.removesuffix("\\").rstrip()
            parts.append(text + ("\n" if hard else " "))
            i += 1
        text = "".join(parts).rstrip()
        self._emit(Paragraph(_runs_with_breaks(text)), self.first + start)
        return i

    def _list(self, i: int) -> int:
        start = i
        first = self.lines[i].strip()
        kind = "check" if _CHECK.match(first) else "number" if _NUMBER.match(first) else "bullet"
        items: list[list[str]] = []
        checks: list[bool] = []
        while i < len(self.lines):
            line = self.lines[i]
            stripped = line.strip()
            if not stripped:
                # A blank line ends the list unless the next line continues it.
                if i + 1 < len(self.lines) and _same_list(self.lines[i + 1].strip(), kind):
                    i += 1
                    continue
                break
            if _same_list(stripped, kind):
                match = _CHECK.match(stripped) if kind == "check" else None
                if match:
                    checks.append(match[1].lower() == "x")
                    items.append([match[2]])
                else:
                    pattern = _NUMBER if kind == "number" else _BULLET
                    item = pattern.match(stripped)
                    items.append([item[2] if item else stripped])
                i += 1
            elif line.startswith((" ", "\t")) and items and not _is_block_start(stripped):
                items[-1].append(stripped)  # a continuation line of the item
                i += 1
            else:
                break
        runs = [inline(" ".join(parts)) for parts in items]
        if kind == "check":
            self._emit(Checklist(list(zip(checks, runs, strict=True))), self.first + start)
        else:
            self._emit(ListBlock(runs, ordered=kind == "number"), self.first + start)
        return i

    def _callout(self, i: int) -> int:
        start = i
        texts: list[str] = []
        while i < len(self.lines) and self.lines[i].strip().startswith(">"):
            texts.append(self.lines[i].strip()[1:].strip())
            i += 1
        paragraphs: list[Block] = []
        chunk: list[str] = []
        for text in [*texts, ""]:
            if text:
                chunk.append(text)
            elif chunk:
                paragraphs.append(Paragraph(_runs_with_breaks(" ".join(chunk))))
                chunk = []
        self._emit(Callout(paragraphs), self.first + start)
        return i

    def _table(self, i: int) -> int:
        start = i
        rows: list[list[str]] = []
        align: list[str] | None = None
        while i < len(self.lines) and self.lines[i].strip().startswith("|"):
            cells = _cells(self.lines[i].strip())
            if len(rows) == 1 and align is None and all(_ALIGN_CELL.match(c) for c in cells):
                align = [_alignment(c) for c in cells]
            else:
                rows.append(cells)
            i += 1
        number = self.first + start
        if align is None:
            raise DocumentError(
                f"line {number}: a table needs a line like |---|---| after its first row (leave the first row's "
                "cells empty for a table without a header)"
            )
        width = len(align)
        if width > MAX_TABLE_COLUMNS:
            raise DocumentError(f"line {number}: a table has at most {MAX_TABLE_COLUMNS} columns")
        if len(rows) - 1 > MAX_TABLE_ROWS:
            raise DocumentError(f"line {number}: a table has at most {MAX_TABLE_ROWS} rows; split it")
        grid = []
        for row in rows:
            if len(row) > width:
                self.warnings.append(f"line {number}: a table row has more cells than the header; extras dropped")
            grid.append([inline(c.strip()) for c in (row + [""] * width)[:width]])
        header = grid[0] if any(plain(c).strip() for c in grid[0]) else None
        self._emit(Table(header, grid[1:], align), number)
        return i


def _at(number: int, fn: Any) -> Any:
    try:
        return fn()
    except DocumentError as exc:
        raise DocumentError(f"line {number}: {exc}") from None


def _ratios(text: str, number: int) -> list[float]:
    parts = text.split(":")
    try:
        ratios = [float(p) for p in parts]
    except ValueError:
        raise DocumentError(f"line {number}: write column widths as ratios, like '::: columns 1:2'") from None
    if not 2 <= len(ratios) <= 4 or any(not 0.2 <= r <= 10 for r in ratios):
        raise DocumentError(f"line {number}: 2 to 4 columns, each ratio between 0.2 and 10")
    return ratios


def _cells(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    cells, current, escaped = [], [], False
    for char in body:
        if escaped:
            current.append("\\" + char if char != "|" else "|")
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
    cells.append("".join(current))
    return cells


def _alignment(cell: str) -> str:
    match = _ALIGN_CELL.match(cell)
    left, right = (match[1], match[2]) if match else ("", "")
    return "center" if left and right else "right" if right else "left"


def _is_block_start(stripped: str) -> bool:
    return bool(
        _DIRECTIVE.match(stripped)
        or _HEADING.match(stripped)
        or _RULE.match(stripped)
        or stripped.startswith(("|", ">"))
        or _CHECK.match(stripped)
        or _BULLET.match(stripped)
        or _NUMBER.match(stripped)
    )


def _same_list(stripped: str, kind: str) -> bool:
    if kind == "check":
        return bool(_CHECK.match(stripped))
    if kind == "number":
        return bool(_NUMBER.match(stripped))
    return bool(_BULLET.match(stripped)) and not _CHECK.match(stripped)


def _runs_with_breaks(text: str) -> list[Run]:
    runs: list[Run] = []
    for index, part in enumerate(text.split("\n")):
        if index:
            runs.append(Run("\n"))
        runs.extend(inline(part.strip()))
    return runs


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def parse(source: str) -> Document:
    """The document tree of a source text, or a DocumentError naming the line to fix."""
    text = _CONTROL.sub("", source.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff"))
    lines = text.split("\n")
    settings = Settings()
    first = 1
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start].strip() == "---":
        end = next((j for j in range(start + 1, len(lines)) if lines[j].strip() == "---"), None)
        if end is None:
            raise DocumentError(f"line {start + 1}: the settings block has no closing '---' line")
        settings = _settings(lines[start + 1 : end], start + 2)
        lines = lines[end + 1 :]
        first = end + 2
    document = _Parser(lines, first, settings).parse()
    if not document.main and not document.sidebar:
        raise DocumentError("the document is empty: write its text below the settings")
    return document
