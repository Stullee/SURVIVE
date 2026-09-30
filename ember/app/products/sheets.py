"""Spreadsheets: a JSON spec the agent writes, made into an Excel file and a picture of each of its tables.

The spec names sheets, columns (title, width, number format, dropdown choices), rows (or a CSV file of rows),
extra empty rows to fill in, totals, a chart and a "How to use" sheet. Values are data; a text starting with "="
is a formula, allowed only with common functions and references to cells of this workbook: a file for strangers
never gets links, other workbooks, DDE ("cmd|...") or functions that reach outside Excel.

0.14.0: any Excel file in the workspace can be read too (``workbook_text``: its cells, sheet by sheet, for
workspace_read) and drawn (``picture``: one sheet, for make_image's 'file.xlsx#2'). The workshop was paid for both.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image, ImageDraw, ImageFont

from . import checks, fonts
from .theme import RGB, hex_rgb, readable_on, rgb_hex, tint

MAX_SHEETS = 8
MAX_COLUMNS = 26
MAX_ROWS = 2_000
MAX_EMPTY_ROWS = 1_000
MAX_CELL_CHARS = 1_000
MAX_FORMULA_CHARS = 400
MAX_NOTES = 30
FORMATS = {
    "text": "@",
    "number": "#,##0.00",
    "integer": "#,##0",
    "eur": '#,##0.00 "€";[Red]-#,##0.00 "€"',
    "usd": '"$"#,##0.00;[Red]-"$"#,##0.00',
    "percent": "0.0%",
    "date": "yyyy-mm-dd",
    "date_de": "dd.mm.yyyy",
    "date_us": "mm/dd/yyyy",
    "general": "General",
}
TOTALS = {"sum": "SUM", "average": "AVERAGE", "count": "COUNTA", "min": "MIN", "max": "MAX"}
_FUNCTIONS = (
    "SUM SUMIF SUMIFS SUMPRODUCT AVERAGE AVERAGEIF AVERAGEIFS COUNT COUNTA COUNTBLANK COUNTIF COUNTIFS MIN MAX "
    "MINIFS MAXIFS MEDIAN LARGE SMALL RANK ROUND ROUNDUP ROUNDDOWN INT ABS MOD SQRT POWER IF IFS IFERROR IFNA AND "
    "OR NOT VLOOKUP HLOOKUP XLOOKUP INDEX MATCH CHOOSE TODAY NOW DATE DATEDIF DAY MONTH YEAR WEEKDAY WEEKNUM EDATE "
    "EOMONTH NETWORKDAYS WORKDAY DAYS TEXT CONCAT CONCATENATE TEXTJOIN LEFT RIGHT MID LEN UPPER LOWER PROPER TRIM "
    "SUBSTITUTE VALUE PMT FV PV RATE NPER ISBLANK ISNUMBER ISTEXT ISERROR N"
)
ALLOWED_FUNCTIONS = frozenset(_FUNCTIONS.split())
_REF = re.compile(
    r"^(?:(?P<sheet>'(?:[^']|'')+'|[A-Za-z0-9_.]+)!)?"
    r"(?:\$?[A-Za-z]{1,3}\$?\d{1,7}(?::\$?[A-Za-z]{1,3}\$?\d{1,7})?"
    r"|\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}|\$?\d{1,7}:\$?\d{1,7})$"
)
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")
_CHOICE_BAD = re.compile(r"[\",]")


class SheetError(ValueError):
    """A spec the agent has to fix; the message says where."""


@dataclass
class Column:
    title: str
    width: float
    format: str
    choices: list[str]
    formula: str = ""  # for every row whose cell is empty, the empty rows included; "{row}" is that row's number


@dataclass
class Sheet:
    name: str
    title: str
    columns: list[Column]
    rows: list[list[Any]]
    empty_rows: int
    totals: dict[int, str]  # column index -> function
    freeze: bool
    filter: bool
    zebra: bool
    chart: dict[str, Any] | None


@dataclass
class Spec:
    title: str
    accent: RGB
    font: str
    sheets: list[Sheet]
    notes: list[str]
    warnings: list[str] = field(default_factory=list)


# --- reading the spec ---


def _text(where: str, value: Any, limit: int, required: bool = True) -> str:
    if value is None or value == "":
        if required:
            raise SheetError(f"{where} is missing")
        return ""
    if not isinstance(value, str):
        raise SheetError(f"{where} must be text")
    if len(value) > limit:
        raise SheetError(f"{where} is longer than {limit:,} characters")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]", value):
        raise SheetError(f"{where} contains control or direction characters")
    return value


def _keys(where: str, data: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SheetError(f"{where} must be an object ({{...}})")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SheetError(f"{where}: unknown key {unknown[0]!r}; use {', '.join(sorted(allowed))}")
    return data


def parse(source: str, read_csv: Any) -> Spec:
    """The checked spec of a JSON text; ``read_csv(path)`` returns a workspace CSV file's text."""
    try:
        data = json.loads(source)
    except ValueError as exc:
        raise SheetError(f"the spec isn't valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})") from None
    top = _keys("the spec", data, {"title", "theme", "sheets", "notes"})
    theme = _keys("theme", top.get("theme") or {}, {"accent", "font"})
    accent = theme.get("accent", "#2C3E50")
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", accent):
        raise SheetError("theme.accent must be a colour like #2C3E50")
    font = str(theme.get("font", "sans")).lower()
    font = {"calibri": "sans", "cambria": "serif", "poppins": "display"}.get(font, font)
    if font not in fonts.FAMILIES:
        raise SheetError("theme.font must be sans (Calibri), serif (Cambria) or display (Poppins)")
    sheets_data = top.get("sheets")
    if not isinstance(sheets_data, list) or not 1 <= len(sheets_data) <= MAX_SHEETS:
        raise SheetError(f"sheets must be a list of 1 to {MAX_SHEETS} sheets")
    notes = top.get("notes") or []
    if not isinstance(notes, list) or len(notes) > MAX_NOTES:
        raise SheetError(f"notes must be a list of at most {MAX_NOTES} lines")
    spec = Spec(
        title=_text("title", top.get("title"), 200, required=False),
        accent=hex_rgb(accent),
        font=font,
        sheets=[],
        notes=[_text(f"notes[{i}]", n, 500) for i, n in enumerate(notes)],
    )
    names: set[str] = {"how to use"} if spec.notes else set()
    for index, sheet_data in enumerate(sheets_data):
        sheet = _sheet(f"sheets[{index}]", sheet_data, read_csv)
        if sheet.name.lower() in names:
            raise SheetError(f"sheets[{index}]: the sheet name {sheet.name!r} is used twice")
        names.add(sheet.name.lower())
        spec.sheets.append(sheet)
    for sheet in spec.sheets:
        first = first_row(bool(sheet.title))
        for r, row in enumerate(sheet.rows):
            for c, value in enumerate(row):
                if isinstance(value, str) and value.startswith("="):
                    check_formula(value, names, f"{sheet.name} row {first + r}, column {c + 1}")
        for c, column in enumerate(sheet.columns):
            if column.formula:
                last = first + len(sheet.rows) + sheet.empty_rows - 1
                check_formula(_placed(column.formula, first, first, last), names, f"{sheet.name} column {c + 1}")
    return spec


def _sheet(where: str, data: Any, read_csv: Any) -> Sheet:
    s = _keys(
        where,
        data,
        {"name", "title", "columns", "rows", "rows_csv", "empty_rows", "totals", "freeze", "filter", "zebra", "chart"},
    )
    name = _text(f"{where}.name", s.get("name"), 31)
    if _SHEET_BAD.search(name) or name.startswith("'") or name.endswith("'"):
        raise SheetError(f"{where}.name can't contain [ ] : * ? / \\ or start or end with '")
    columns_data = s.get("columns")
    if not isinstance(columns_data, list) or not 1 <= len(columns_data) <= MAX_COLUMNS:
        raise SheetError(f"{where}.columns must be a list of 1 to {MAX_COLUMNS} columns")
    columns = []
    for i, col in enumerate(columns_data):
        c = _keys(f"{where}.columns[{i}]", col, {"title", "width", "format", "choices", "formula"})
        width = c.get("width", 14)
        if not isinstance(width, int | float) or isinstance(width, bool) or not 4 <= width <= 80:
            raise SheetError(f"{where}.columns[{i}].width must be a number from 4 to 80")
        fmt = c.get("format", "general")
        if fmt not in FORMATS:
            raise SheetError(f"{where}.columns[{i}].format must be one of {', '.join(FORMATS)}")
        choices = c.get("choices") or []
        if not isinstance(choices, list) or len(choices) > 40:
            raise SheetError(f"{where}.columns[{i}].choices must be a list of at most 40 values")
        clean = [_text(f"{where}.columns[{i}].choices", x, 60) for x in choices]
        if any(_CHOICE_BAD.search(x) for x in clean):
            raise SheetError(f"{where}.columns[{i}].choices can't contain commas or quotes")
        if len(",".join(clean)) > 250:
            raise SheetError(f"{where}.columns[{i}].choices are too long together (Excel allows 255 characters)")
        formula = _text(f"{where}.columns[{i}].formula", c.get("formula"), MAX_FORMULA_CHARS, required=False)
        if formula and not formula.startswith("="):
            raise SheetError(f"{where}.columns[{i}].formula must start with '=', e.g. '=B{{row}}-C{{row}}'")
        title = _text(f"{where}.columns[{i}].title", c.get("title"), 60)
        columns.append(Column(title, float(width), fmt, clean, formula))
    titles = [c.title.lower() for c in columns]
    rows = _rows(where, s, read_csv, len(columns))
    empty = s.get("empty_rows", 0)
    if not isinstance(empty, int) or isinstance(empty, bool) or not 0 <= empty <= MAX_EMPTY_ROWS:
        raise SheetError(f"{where}.empty_rows must be a whole number from 0 to {MAX_EMPTY_ROWS}")
    sheet_title = _text(f"{where}.title", s.get("title"), 120, required=False)
    first = first_row(bool(sheet_title))
    last = first + len(rows) + empty - 1
    for r, row in enumerate(rows):
        for c, column in enumerate(columns):
            if row[c] is None and column.formula:
                row[c] = column.formula
            if isinstance(row[c], str) and row[c].startswith("="):
                row[c] = _placed(row[c], first + r, first, last)
    totals: dict[int, str] = {}
    for key, fn in _keys(f"{where}.totals", s.get("totals") or {}, set(s.get("totals") or {})).items():
        if key.lower() not in titles:
            raise SheetError(f"{where}.totals: there is no column {key!r}")
        if fn not in TOTALS:
            raise SheetError(f"{where}.totals[{key!r}] must be one of {', '.join(TOTALS)}")
        totals[titles.index(key.lower())] = TOTALS[fn]
    chart = s.get("chart")
    if chart is not None:
        chart = _keys(f"{where}.chart", chart, {"type", "labels", "values", "title"})
        if chart.get("type") not in ("bar", "line", "pie"):
            raise SheetError(f"{where}.chart.type must be bar, line or pie")
        for key in ("labels", "values"):
            if str(chart.get(key, "")).lower() not in titles:
                raise SheetError(f"{where}.chart.{key} must name one of the columns")
        chart["title"] = _text(f"{where}.chart.title", chart.get("title"), 80, required=False)
    flags = {}
    for flag, default in (("freeze", True), ("filter", True), ("zebra", True)):
        value = s.get(flag, default)
        if not isinstance(value, bool):
            raise SheetError(f"{where}.{flag} must be true or false")
        flags[flag] = value
    return Sheet(
        name=name,
        title=sheet_title,
        columns=columns,
        rows=rows,
        empty_rows=empty,
        totals=totals,
        chart=chart,
        **flags,
    )


def _rows(where: str, s: dict[str, Any], read_csv: Any, width: int) -> list[list[Any]]:
    if "rows" in s and "rows_csv" in s:
        raise SheetError(f"{where}: give rows or rows_csv, not both")
    if "rows_csv" in s:
        path = _text(f"{where}.rows_csv", s["rows_csv"], 200)
        if not path.endswith(".csv"):
            raise SheetError(f"{where}.rows_csv must be a .csv file in your workspace")
        raw = list(csv.reader(io.StringIO(read_csv(path))))
        data: list[Any] = [[_number_or_text(v) for v in row] for row in raw if any(v.strip() for v in row)]
    else:
        data = s.get("rows") or []
    if not isinstance(data, list) or len(data) > MAX_ROWS:
        raise SheetError(f"{where}.rows must be a list of at most {MAX_ROWS:,} rows")
    rows = []
    for r, row in enumerate(data):
        if not isinstance(row, list) or len(row) > width:
            raise SheetError(f"{where}.rows[{r}] must be a list of at most {width} values (one per column)")
        clean = []
        for c, value in enumerate(row):
            if isinstance(value, str):
                clean.append(_text(f"{where}.rows[{r}][{c}]", value, MAX_CELL_CHARS, required=False))
            elif value is None or isinstance(value, bool | int | float):
                clean.append(value)
            else:
                raise SheetError(f"{where}.rows[{r}][{c}] must be text, a number, true/false or null")
        rows.append(clean + [None] * (width - len(clean)))
    return rows


def first_row(titled: bool) -> int:
    """The Excel row of a sheet's first data row: the header is row 1, or row 3 below a title (row 1)."""
    return 4 if titled else 2


def _placed(formula: str, row: int, first: int, last: int) -> str:
    """A formula with its placeholders filled in: {row} (its own row), {first} and {last} (the data rows)."""
    return formula.replace("{row}", str(row)).replace("{first}", str(first)).replace("{last}", str(last))


def _number_or_text(value: str) -> Any:
    text = value.strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def check_formula(formula: str, sheets: set[str], where: str) -> None:
    """Refuse a formula that could reach outside this workbook, or that uses a function not on the list."""
    if len(formula) > MAX_FORMULA_CHARS:
        raise SheetError(f"{where}: the formula is longer than {MAX_FORMULA_CHARS} characters")
    if "|" in formula or "[" in formula:
        raise SheetError(f"{where}: formulas can't refer to other workbooks or programs")
    try:
        items = Tokenizer(formula).items
    except Exception:  # noqa: BLE001 - the tokenizer raises plain errors on bad input
        raise SheetError(f"{where}: the formula {formula[:60]!r} can't be read") from None
    for token in items:
        if token.type == Token.FUNC and token.subtype == Token.OPEN:
            name = token.value[:-1].upper().removeprefix("_XLFN.")
            if name not in ALLOWED_FUNCTIONS:
                raise SheetError(
                    f"{where}: {name} isn't allowed; use common functions such as SUM, IF, SUMIF, VLOOKUP, "
                    "XLOOKUP, INDEX, MATCH, TODAY, TEXT, ROUND"
                )
        elif token.type == Token.OPERAND and token.subtype == Token.RANGE:
            match = _REF.match(token.value)
            if not match:
                raise SheetError(f"{where}: {token.value!r} isn't a cell reference like B2, B2:B20 or Sheet!B2")
            sheet = match["sheet"]
            if sheet:
                name = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
                if name.lower() not in sheets:
                    raise SheetError(f"{where}: there is no sheet {name!r} in this workbook")


# --- the workbook ---


def build(spec: Spec) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title = spec.title
    wb.properties.creator = ""
    wb.properties.lastModifiedBy = ""
    font_name = fonts.FAMILIES[spec.font].word_name
    accent = rgb_hex(spec.accent)[1:]
    header_text = rgb_hex(readable_on(spec.accent))[1:]
    thin = Side(style="thin", color=rgb_hex(tint(spec.accent, 0.75))[1:])
    if spec.notes:
        notes = wb.create_sheet("How to use")
        notes.column_dimensions["A"].width = 100
        notes["A1"] = spec.title or "How to use"
        notes["A1"].font = Font(name=font_name, size=16, bold=True, color=accent)
        for i, line in enumerate(spec.notes, start=3):
            notes.cell(row=i, column=1, value=line).font = Font(name=font_name, size=11)
            notes.cell(row=i, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    for sheet in spec.sheets:
        ws = wb.create_sheet(sheet.name)
        top = first_row(bool(sheet.title)) - 1
        if sheet.title:
            ws.cell(row=1, column=1, value=sheet.title).font = Font(name=font_name, size=16, bold=True, color=accent)
            ws.row_dimensions[1].height = 26
        for c, column in enumerate(sheet.columns, start=1):
            letter = get_column_letter(c)
            ws.column_dimensions[letter].width = column.width
            cell = ws.cell(row=top, column=c, value=column.title)
            cell.font = Font(name=font_name, bold=True, color=header_text)
            cell.fill = PatternFill("solid", fgColor=accent)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[top].height = 22
        first = top + 1
        last = first + len(sheet.rows) + sheet.empty_rows - 1
        zebra = PatternFill("solid", fgColor=rgb_hex(tint(spec.accent, 0.92))[1:])
        for r in range(first, max(last, first - 1) + 1):
            if r - first < len(sheet.rows):
                values = sheet.rows[r - first]
            else:
                values = [_placed(c.formula, r, first, last) if c.formula else None for c in sheet.columns]
            for c, (column, value) in enumerate(zip(sheet.columns, values, strict=True), start=1):
                cell = ws.cell(row=r, column=c, value=_value(value, column.format))
                cell.font = Font(name=font_name)
                cell.number_format = FORMATS[column.format]
                cell.border = Border(bottom=thin)
                if sheet.zebra and (r - first) % 2 == 1:
                    cell.fill = zebra
        if sheet.totals and last >= first:
            total_row = last + 1
            label = ws.cell(row=total_row, column=1, value="Total" if 0 not in sheet.totals else None)
            label.font = Font(name=font_name, bold=True)
            for c, fn in sheet.totals.items():
                letter = get_column_letter(c + 1)
                cell = ws.cell(row=total_row, column=c + 1, value=f"={fn}({letter}{first}:{letter}{last})")
                cell.font = Font(name=font_name, bold=True)
                cell.number_format = FORMATS[sheet.columns[c].format]
                cell.border = Border(top=Side(style="medium", color=accent))
        for c, column in enumerate(sheet.columns, start=1):
            if column.choices and last >= first:
                letter = get_column_letter(c)
                rule = DataValidation(type="list", formula1=f'"{",".join(column.choices)}"', allow_blank=True)
                rule.add(f"{letter}{first}:{letter}{last}")
                ws.add_data_validation(rule)
        if sheet.freeze:
            ws.freeze_panes = ws.cell(row=first, column=1)
        if sheet.filter and last >= first:
            ws.auto_filter.ref = f"A{top}:{get_column_letter(len(sheet.columns))}{last}"
        if sheet.chart and last >= first:
            _chart(ws, sheet, top, first, last)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _value(value: Any, fmt: str) -> Any:
    if isinstance(value, str) and fmt.startswith("date") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _chart(ws: Any, sheet: Sheet, top: int, first: int, last: int) -> None:
    spec = sheet.chart or {}
    titles = [c.title.lower() for c in sheet.columns]
    labels_col = titles.index(str(spec["labels"]).lower()) + 1
    values_col = titles.index(str(spec["values"]).lower()) + 1
    chart = {"bar": BarChart, "line": LineChart, "pie": PieChart}[spec["type"]]()
    chart.title = spec.get("title") or None
    data = Reference(ws, min_col=values_col, min_row=top, max_row=last)
    labels = Reference(ws, min_col=labels_col, min_row=first, max_row=last)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(labels)
    chart.width, chart.height = 16, 9
    ws.add_chart(chart, f"{get_column_letter(len(sheet.columns) + 2)}{top}")


# --- the picture ---


def preview(spec: Spec, max_rows: int = 18, index: int = 0) -> bytes:
    """A PNG of a sheet's table (the first unless ``index`` names another, 0.14.0), for listing photos and the
    dashboard.

    Formulas show their result when the preview can work it out (arithmetic, SUM, AVERAGE, MIN, MAX, COUNT, ROUND
    and ABS over this sheet's cells); any other formula is shown as written, in italics.
    """
    sheet = spec.sheets[index]
    values = _Results(sheet)
    scale = 2
    col_px = [max(60, int(c.width * 7.5)) * scale for c in sheet.columns]
    row_h = 22 * scale
    title_h = 44 * scale if sheet.title else 0
    rows = sheet.rows[:max_rows]
    blank = max(0, min(sheet.empty_rows, max_rows - len(rows), 4))
    count = 1 + len(rows) + blank + (1 if sheet.totals else 0)
    width = sum(col_px) + 2 * 12 * scale
    height = title_h + count * row_h + 2 * 12 * scale
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    body = ImageFont.truetype(str(fonts.path(spec.font, "")), 11 * scale)
    bold = ImageFont.truetype(str(fonts.path(spec.font, "B")), 11 * scale)
    italic = ImageFont.truetype(str(fonts.path(spec.font, "I")), 10 * scale)
    x0 = y = 12 * scale
    if sheet.title:
        big = ImageFont.truetype(str(fonts.path(spec.font, "B")), 20 * scale)
        draw.text((x0, y), sheet.title, font=big, fill=spec.accent)
        y += title_h
    line = tint(spec.accent, 0.75)
    header_text = readable_on(spec.accent)
    draw.rectangle([x0, y, x0 + sum(col_px), y + row_h], fill=spec.accent)
    _row(draw, [c.title for c in sheet.columns], col_px, x0, y, row_h, bold, header_text, scale)
    y += row_h
    for index in range(len(rows) + blank):
        if sheet.zebra and index % 2 == 1:
            draw.rectangle([x0, y, x0 + sum(col_px), y + row_h], fill=tint(spec.accent, 0.92))
        texts, styles = [], []
        for c, column in enumerate(sheet.columns):
            value = values.cell(index, c) if index < len(rows) else None
            unknown = isinstance(value, str) and value.startswith("=")
            texts.append(_shown(value, column.format))
            styles.append(italic if unknown else body)
        _row(draw, texts, col_px, x0, y, row_h, styles, (34, 34, 34), scale)
        draw.line([x0, y + row_h, x0 + sum(col_px), y + row_h], fill=line, width=scale)
        y += row_h
    if sheet.totals:
        texts = ["Total"] + [""] * (len(sheet.columns) - 1)
        for c, fn in sheet.totals.items():
            total = values.total(c)
            texts[c] = _shown(total, sheet.columns[c].format) if total is not None else f"={fn}(…)"
        draw.line([x0, y, x0 + sum(col_px), y], fill=spec.accent, width=2 * scale)
        _row(draw, texts, col_px, x0, y, row_h, bold, (34, 34, 34), scale)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


# --- any Excel file: read and drawn (0.14.0) ---

READ_ROWS = 2_000  # a sheet's rows read (MAX_ROWS of data under a title and a header)
READ_COLUMNS = MAX_COLUMNS
TEXT_CHARS = 200_000  # a workbook's text at most (workspace_read shows it a part at a time)
PICTURE_ROWS = 30
PICTURE_COLUMNS = 12
_FORMAT_KEYS = {code: key for key, code in FORMATS.items()}


def _book(data: bytes) -> Any:
    """An Excel file opened to read: its parts unpacked within bounds first (checks.unzipped)."""
    parts = checks.unzipped(data, "the Excel file", checks.READ_BYTES)
    try:
        return load_workbook(io.BytesIO(checks.stored(parts)), read_only=True, data_only=False)
    except Exception:  # noqa: BLE001 - whatever breaks the reader, the file can't be read
        raise SheetError("the Excel file can't be read") from None


def _cells(sheet: Any, rows: int, columns: int) -> dict[tuple[int, int], Any]:
    """A sheet's cells that hold something, by (row, column), both from 1."""
    found = {}
    for r, row in enumerate(sheet.iter_rows(max_row=rows, max_col=columns), start=1):
        for c, cell in enumerate(row, start=1):
            if cell.value is not None:
                found[(r, c)] = cell
    return found


def workbook_text(data: bytes) -> str:
    """An Excel file's cells as text, sheet by sheet and row by row: values as they are, a formula as written with
    its result when Ember's code can work it out."""
    book = _book(data)
    try:
        out: list[str] = []
        size = 0
        for number, sheet in enumerate(book.worksheets, start=1):
            cells = _cells(sheet, READ_ROWS, READ_COLUMNS)
            grid = _Grid({key: cell.value for key, cell in cells.items()})
            rows = sorted({r for r, _ in cells})
            columns = max((c for _, c in cells), default=0)
            out.append(f"Sheet {number} '{sheet.title}' ({len(rows)} rows with values, {columns} columns):")
            for r in rows:
                shown = []
                for c in range(1, columns + 1):
                    value = cells[(r, c)].value if (r, c) in cells else None
                    text = _plain(value)
                    if isinstance(value, str) and value.startswith("="):
                        result = grid.value(r, c)
                        if not isinstance(result, str):
                            text += f" → {_plain(result)}"
                    shown.append(text)
                out.append(f"{r}: " + " | ".join(shown).rstrip(" |"))
                size += len(out[-1]) + 1
                if size > TEXT_CHARS:
                    out.append("… (the rest is cut)")
                    return "\n".join(out)
            out.append("")
        return "\n".join(out).strip()
    finally:
        book.close()


def picture(data: bytes, which: str = "") -> Image.Image:
    """One sheet of an Excel file drawn as a table (its first rows and columns), for listing photos: the first sheet,
    or the one ``which`` names by number (from 1) or name. Formulas show their result where the preview's can."""
    book = _book(data)
    try:
        names = book.sheetnames
        if not which:
            index = 0
        elif which.isdigit():
            index = int(which) - 1
        else:
            index = next((i for i, n in enumerate(names) if n.lower() == which.lower()), -1)
        if not 0 <= index < len(names):
            listed = ", ".join(f"{i} '{n}'" for i, n in enumerate(names, start=1))
            raise SheetError(f"the workbook has no sheet {which!r}; its sheets are {listed}")
        cells = _cells(book.worksheets[index], READ_ROWS, READ_COLUMNS)
    finally:
        book.close()
    grid = _Grid({key: cell.value for key, cell in cells.items()})
    shown = {key: cell for key, cell in cells.items() if key[0] <= PICTURE_ROWS and key[1] <= PICTURE_COLUMNS}
    last_row = max((r for r, _ in shown), default=1)
    last_column = max((c for _, c in shown), default=1)
    texts = {}
    for (r, c), cell in shown.items():
        value = grid.value(r, c)
        unknown = isinstance(value, str) and value.startswith("=")
        texts[(r, c)] = (_cell_text(value, cell.number_format), unknown)
    scale = 2
    widths = []
    for c in range(1, last_column + 1):
        longest = max((len(texts[(r, c)][0]) for r in range(1, last_row + 1) if (r, c) in texts), default=4)
        widths.append(min(360, max(60, int(longest * 7.5) + 16)) * scale)
    row_h = 22 * scale
    margin = 12 * scale
    image = Image.new("RGB", (sum(widths) + 2 * margin, last_row * row_h + 2 * margin), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    body = ImageFont.truetype(str(fonts.path("sans", "")), 11 * scale)
    bold = ImageFont.truetype(str(fonts.path("sans", "B")), 11 * scale)
    italic = ImageFont.truetype(str(fonts.path("sans", "I")), 10 * scale)
    line = (218, 220, 224)
    y = margin
    for r in range(1, last_row + 1):
        x = margin
        for c, width in enumerate(widths, start=1):
            cell = shown.get((r, c))
            fill = _colour(cell.fill.fgColor) if cell is not None and cell.fill.patternType == "solid" else None
            if fill is not None:
                draw.rectangle([x, y, x + width, y + row_h], fill=fill)
            draw.rectangle([x, y, x + width, y + row_h], outline=line, width=1)
            if cell is not None:
                text, unknown = texts[(r, c)]
                font = italic if unknown else bold if cell.font.b else body
                colour = _colour(cell.font.color) or (readable_on(fill) if fill is not None else (34, 34, 34))
                _row(draw, [text], [width], x, y, row_h, font, colour, scale)
            x += width
        y += row_h
    return image


def _colour(color: Any) -> RGB | None:
    """An Excel colour given as RGB (a theme's colour is left out: the picture doesn't know the theme)."""
    value = getattr(color, "rgb", None) if getattr(color, "type", None) == "rgb" else None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{8}", value):
        return None
    return hex_rgb("#" + value[2:])


def _plain(value: Any) -> str:
    """A value as text to read: a whole number without its ".0", a date without its midnight."""
    if value is None:
        return ""
    if isinstance(value, float) and not isinstance(value, bool):
        return str(int(value)) if value.is_integer() else f"{value:.6f}".rstrip("0")
    if isinstance(value, dt.datetime) and value.time() == dt.time():
        return value.date().isoformat()
    return str(value)


def _cell_text(value: Any, number_format: str) -> str:
    """A cell's value as Excel shows it, near enough: in its number format, or the nearest of FORMATS."""
    if isinstance(value, dt.datetime | dt.date):
        shown = {"dd.mm.yyyy": "%d.%m.%Y", "mm/dd/yyyy": "%m/%d/%Y"}.get(number_format, "%Y-%m-%d")
        return value.strftime(shown)
    return _shown(value, _FORMAT_KEYS.get(number_format) or _format_key(number_format))


def _format_key(number_format: str) -> str:
    """The nearest of FORMATS for a number format Ember didn't write."""
    if "%" in number_format:
        return "percent"
    if "€" in number_format:
        return "eur"
    if "$" in number_format:
        return "usd"
    if number_format in ("0", "#,##0"):
        return "integer"
    return "number" if re.search(r"0\.00", number_format) else "general"


class _Unknown(Exception):
    """A formula the preview can't work out."""


_TOKENS = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<fn>[A-Z]+)\(|"
    r"(?P<range>\$?[A-Z]{1,3}\$?\d{1,7}:\$?[A-Z]{1,3}\$?\d{1,7})|(?P<ref>\$?[A-Z]{1,3}\$?\d{1,7})|(?P<op>[-+*/(),]))"
)
_CELL = re.compile(r"\$?([A-Z]{1,3})\$?(\d{1,7})")
_PREVIEW_FUNCTIONS = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "ROUND", "ABS"}


class _Results:
    """The values a sheet shows once Excel has calculated it, as far as a preview needs them."""

    def __init__(self, sheet: Sheet) -> None:
        self.sheet = sheet
        self.first = first_row(bool(sheet.title))
        self.last = self.first + len(sheet.rows) + sheet.empty_rows - 1
        self.cache: dict[tuple[int, int], Any] = {}
        self.busy: set[tuple[int, int]] = set()

    def cell(self, index: int, column: int) -> Any:
        """The value of a data cell: a formula's result, or the formula itself when it can't be worked out."""
        value = self.sheet.rows[index][column]
        if not (isinstance(value, str) and value.startswith("=")):
            return value
        try:
            return self._number(index, column)
        except (_Unknown, ZeroDivisionError, OverflowError, RecursionError):
            return value

    def total(self, column: int) -> float | None:
        fn = self.sheet.totals.get(column)
        found: list[float] = []
        for index in range(len(self.sheet.rows)):
            value = self.cell(index, column)
            if isinstance(value, str) and value.startswith("="):
                return None
            if isinstance(value, int | float) and not isinstance(value, bool):
                found.append(float(value))
        return _total(fn, found) if fn and found else None

    def _number(self, index: int, column: int) -> float:
        key = (index, column)
        if key in self.cache:
            return self.cache[key]
        if key in self.busy or len(self.busy) > 50:
            raise _Unknown
        self.busy.add(key)
        try:
            tokens = _tokens(str(self.sheet.rows[index][column])[1:])
            result, rest = self._sum(tokens)
            if rest:
                raise _Unknown
        finally:
            self.busy.discard(key)
        self.cache[key] = result
        return result

    def _ref(self, text: str) -> float:
        value = self._at(text)
        return 0.0 if value is None else value

    def _at(self, text: str) -> float | None:
        """A cell's number, or None when it is empty."""
        match = _CELL.fullmatch(text)
        if match is None:
            raise _Unknown
        column = _column_index(match.group(1))
        row = int(match.group(2))
        if column >= len(self.sheet.columns):
            raise _Unknown
        if row == self.last + 1 and column in self.sheet.totals:
            total = self.total(column)
            if total is None:
                raise _Unknown
            return total
        index = row - self.first
        if not 0 <= index < len(self.sheet.rows):
            if self.first <= row <= self.last:
                return None  # an empty row to fill in
            raise _Unknown
        value = self.sheet.rows[index][column]
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("="):
            return self._number(index, column)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        raise _Unknown

    def _range(self, text: str) -> list[float]:
        start, end = text.split(":")
        a, b = _CELL.fullmatch(start), _CELL.fullmatch(end)
        if a is None or b is None or a.group(1) != b.group(1) or int(b.group(2)) - int(a.group(2)) > MAX_ROWS:
            raise _Unknown  # one column at a time is all a preview needs
        found = [self._at(f"{a.group(1)}{row}") for row in range(int(a.group(2)), int(b.group(2)) + 1)]
        return [value for value in found if value is not None]

    def _sum(self, tokens: list[tuple[str, str]]) -> tuple[float, list[tuple[str, str]]]:
        value, tokens = self._product(tokens)
        while tokens and tokens[0] in (("op", "+"), ("op", "-")):
            op = tokens[0][1]
            right, tokens = self._product(tokens[1:])
            value = value + right if op == "+" else value - right
        return value, tokens

    def _product(self, tokens: list[tuple[str, str]]) -> tuple[float, list[tuple[str, str]]]:
        value, tokens = self._factor(tokens)
        while tokens and tokens[0] in (("op", "*"), ("op", "/")):
            op = tokens[0][1]
            right, tokens = self._factor(tokens[1:])
            value = value * right if op == "*" else value / right
        return value, tokens

    def _factor(self, tokens: list[tuple[str, str]]) -> tuple[float, list[tuple[str, str]]]:
        if not tokens:
            raise _Unknown
        kind, text = tokens[0]
        rest = tokens[1:]
        if (kind, text) == ("op", "-"):
            value, rest = self._factor(rest)
            return -value, rest
        if kind == "num":
            return float(text), rest
        if kind == "ref":
            return self._ref(text), rest
        if (kind, text) == ("op", "("):
            value, rest = self._sum(rest)
            if not rest or rest[0] != ("op", ")"):
                raise _Unknown
            return value, rest[1:]
        if kind == "fn" and text in _PREVIEW_FUNCTIONS:
            args: list[float] = []
            while True:
                if rest and rest[0][0] == "range":
                    args.extend(self._range(rest[0][1]))
                    rest = rest[1:]
                else:
                    value, rest = self._sum(rest)
                    args.append(value)
                if rest and rest[0] == ("op", ","):
                    rest = rest[1:]
                    continue
                if rest and rest[0] == ("op", ")"):
                    return _call(text, args), rest[1:]
                raise _Unknown
        raise _Unknown


class _Grid(_Results):
    """0.14.0: the values of any Excel file's sheet, by (row, column): formulas worked out as the preview does,
    over whole ranges (text in them is left out, as Excel does)."""

    def __init__(self, cells: dict[tuple[int, int], Any]) -> None:
        self.cells = cells
        self.cache: dict[tuple[int, int], Any] = {}
        self.busy: set[tuple[int, int]] = set()

    def value(self, row: int, column: int) -> Any:
        value = self.cells.get((row, column))
        if not (isinstance(value, str) and value.startswith("=")):
            return value
        try:
            return self._formula((row, column))
        except (_Unknown, ZeroDivisionError, OverflowError, RecursionError):
            return value

    def _formula(self, key: tuple[int, int]) -> float:
        if key in self.cache:
            return self.cache[key]
        if key in self.busy or len(self.busy) > 50:
            raise _Unknown
        self.busy.add(key)
        try:
            result, rest = self._sum(_tokens(str(self.cells[key])[1:]))
            if rest:
                raise _Unknown
        finally:
            self.busy.discard(key)
        self.cache[key] = result
        return result

    def _at(self, text: str) -> float | None:
        match = _CELL.fullmatch(text)
        if match is None:
            raise _Unknown
        key = (int(match.group(2)), _column_index(match.group(1)) + 1)
        value = self.cells.get(key)
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("="):
            return self._formula(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        raise _Unknown

    def _range(self, text: str) -> list[float]:
        start, end = (_CELL.fullmatch(part) for part in text.split(":"))
        if start is None or end is None:
            raise _Unknown
        rows = range(int(start.group(2)), int(end.group(2)) + 1)
        columns = range(_column_index(start.group(1)), _column_index(end.group(1)) + 1)
        if len(rows) * len(columns) > READ_ROWS * 4:
            raise _Unknown
        found = []
        for row in rows:
            for column in columns:
                value = self.cells.get((row, column + 1))
                if isinstance(value, str) and value.startswith("="):
                    found.append(self._formula((row, column + 1)))
                elif isinstance(value, int | float) and not isinstance(value, bool):
                    found.append(float(value))
        return found


def _tokens(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    position = 0
    text = text.upper()
    while position < len(text.rstrip()):
        match = _TOKENS.match(text, position)
        if match is None:
            raise _Unknown
        kind = match.lastgroup or ""
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


def _column_index(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number - 1


def _call(name: str, args: list[float]) -> float:
    if name == "ROUND":
        if len(args) != 2:
            raise _Unknown
        return round(args[0], int(args[1]))
    if name == "ABS":
        if len(args) != 1:
            raise _Unknown
        return abs(args[0])
    if not args:
        raise _Unknown
    if name == "COUNT":
        return float(len(args))
    return _total(name, args)


def _row(
    draw: Any, texts: list[str], widths: list[int], x: int, y: int, h: int, font: Any, color: RGB, scale: int
) -> None:
    for i, (text, width) in enumerate(zip(texts, widths, strict=True)):
        f = font[i] if isinstance(font, list) else font
        shown = text
        while shown and draw.textlength(shown, font=f) > width - 12 * scale:
            shown = shown[:-2] + "…" if len(shown) > 2 else ""
        box = draw.textbbox((0, 0), "Ag", font=f)
        draw.text((x + 6 * scale, y + (h - (box[3] - box[1])) / 2 - box[1]), shown, font=f, fill=color)
        x += width


def _total(fn: str, values: list[float]) -> float:
    if fn == "SUM":
        return sum(values)
    if fn == "AVERAGE":
        return sum(values) / len(values)
    if fn == "COUNTA":
        return len(values)
    return min(values) if fn == "MIN" else max(values)


def _shown(value: Any, fmt: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        if fmt == "eur":
            return f"{value:,.2f} €"
        if fmt == "usd":
            return f"${value:,.2f}"
        if fmt == "percent":
            return f"{value * 100:.1f}%"
        if fmt == "integer":
            return f"{value:,.0f}"
        if fmt == "number":
            return f"{value:,.2f}"
        return f"{value:g}"
    return str(value)
