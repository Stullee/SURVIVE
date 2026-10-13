"""Small tables for listing photos (0.38.0, upgrade request #10: make_image's layout table).

The agent's workshop drew its Nebenkostenabrechnung's v9 and v10 covers with a script that proved itself
(workshop/scripts/create-a-single-png-41.py): a title and a subtitle on a band of the accent, and under it a table of
four columns, full bleed, with a header row, rows in turn white and tinted, and grid lines. Each fix of such a cover's
words or numbers was a run of up to $2 against the daily cap, so on a day near the cap a flagged cover waited for the
next day. make_image draws it now, free (``images.table_photo``); this module reads the agent's table: one row a line,
cells split by '|' (a Markdown table works too), the header first.

A row that names itself a total (Summe, Gesamt, Total) is drawn on a deeper tint, and Ember's code adds up the rows
above it, so a cover's sum can't disagree with its rows unnoticed (``mismatches``): the numbers as shown (1.462,50 €,
30%, 210 m²), in German or English notation. Ember's code doesn't know where the numbers come from: the agent copies
them from its product's file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

MAX_ROWS = 8  # rows under the header: more are too small to read in a listing's thumbnail
MAX_COLUMNS = 5
CELL_CHARS = 40
# A row's first cell that names it a total: it sums the rows above it (since the last total).
_TOTAL = re.compile(
    r"^(?:summe\w*|gesamt\w*|insgesamt|zwischensumme|zusammen|sub-?totals?|totals?|sum|grand total)\b", re.IGNORECASE
)
_RULE = re.compile(r"^\|?(?:\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?$")  # a Markdown table's line under its header: |---|--:|
# A number as a cell shows it, with a currency before it or a unit after it: 1.462,50 €, $1,234.56, 30%, 210 m².
_NUMBER = re.compile(
    r"^(?:[€$£] ?)?(?P<sign>[-−+]?)(?:[€$£] ?)?(?P<digits>\d{1,3}(?:[., ']\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?: ?(?:%|‰|€|\$|£|[^\W\d_]{1,5}[²³]?))?$"
)


class TableError(ValueError):
    """Something the agent fixes in its table; the message says what."""


@dataclass(frozen=True)
class Table:
    header: list[str]
    rows: list[list[str]]
    totals: frozenset[int]  # the rows (from 0) that name themselves a total

    @property
    def cells(self) -> list[str]:
        return [*self.header, *(cell for row in self.rows for cell in row)]


def _cells(line: str) -> list[str]:
    if len(line) > 1 and line.startswith("|") and line.endswith("|"):  # a Markdown table's row; 'a | b |' ends empty
        line = line[1:-1]
    cells = []
    for cell in line.split("|"):
        cell = " ".join(cell.split())
        if len(cell) > 4 and cell.startswith("**") and cell.endswith("**"):  # Markdown's bold: no stars on the photo
            cell = cell[2:-2].strip()
        cells.append(cell)
    return cells


def _count(cells: int) -> str:
    return f"{cells} cell{'s' if cells != 1 else ''}"


def parse(text: str) -> Table:
    """The agent's table: one row a line, its cells split by '|', the header first."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not _RULE.match(line)]
    if len(lines) < 2:
        raise TableError("table needs its header line and at least one row under it, one row a line, cells split by |")
    if len(lines) > MAX_ROWS + 1:
        raise TableError(f"table has {len(lines) - 1} rows under its header: at most {MAX_ROWS} read well on a photo")
    header, *rows = (_cells(line) for line in lines)
    if not 2 <= len(header) <= MAX_COLUMNS:
        raise TableError(f"table's header has {_count(len(header))}: give 2 to {MAX_COLUMNS} columns, split by |")
    if not any(header):
        raise TableError("table's header is empty: name its columns")
    for number, row in enumerate(rows, 1):
        if len(row) != len(header):
            raise TableError(
                f"table's row {number} ({' | '.join(row)[:60]}) has {_count(len(row))}, its header {len(header)}: give "
                "each row a cell for each column (an empty one is nothing between two |)"
            )
    for cell in [*header, *(cell for row in rows for cell in row)]:
        if len(cell) > CELL_CHARS:
            raise TableError(f"table's cell {cell[:30]!r}… has {len(cell)} characters: at most {CELL_CHARS}")
    # a total adds up rows above it: the first row is none
    totals = frozenset(index for index, row in enumerate(rows) if index and _TOTAL.match(row[0]))
    return Table(header, rows, totals)


# --- the totals' check ---


def _mark(digits: str) -> str | None:
    """The decimal mark a number shows unmistakably ('1.462,50' and '0,5': ','; '1,234,567': '.'), or None."""
    marks = [c for c in digits if c in ".,"]
    if not marks:
        return None
    if len(set(marks)) == 2:
        return marks[-1]
    if len(marks) > 1:  # one kind, several times: they group thousands
        return "," if marks[0] == "." else "."
    before, _, after = digits.replace(" ", "").replace("'", "").partition(marks[0])
    return marks[0] if len(after) != 3 or len(before) > 3 else None


def _value(digits: str, mark: str) -> tuple[Decimal, int] | None:
    """The number ``digits`` shows with ``mark`` as its decimal mark, and its decimals; None if it can't be read so."""
    other = "." if mark == "," else ","
    whole, _, decimals = digits.partition(mark)
    if mark in decimals or other in decimals:
        return None
    groups = re.split(rf"[{re.escape(other)} ']", whole)
    if len(groups) > 1 and (len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:])):
        return None
    return Decimal("".join(groups) + "." + (decimals or "0")), len(decimals)


def _notation(table: Table) -> list[str]:
    """The decimal marks to read the table's numbers with: its own when its numbers show it, else both."""
    found = set()
    for cell in table.cells:
        match = _NUMBER.match(cell)
        if match and (mark := _mark(match.group("digits"))):
            found.add(mark)
    return list(found) if len(found) == 1 else [",", "."]


def _number(cell: str, mark: str) -> tuple[Decimal, int] | None:
    match = _NUMBER.match(cell)
    if match is None:
        return None
    read = _value(match.group("digits"), mark)
    if read is None:
        return None
    value, decimals = read
    return (-value if match.group("sign") in ("-", "−") else value), decimals


def _shown(value: Decimal, decimals: int, mark: str, like: str) -> str:
    """``value`` as the cell ``like`` shows its number: the same decimal mark, grouping, currency and unit."""
    match = _NUMBER.match(like)
    assert match is not None  # noqa: S101 - only cells that were read as numbers
    digits = match.group("digits")
    group = next((c for c in digits.partition(mark)[0] if not c.isdigit()), "")
    text = f"{abs(value):,.{decimals}f}".replace(",", "\0").replace(".", mark).replace("\0", group)
    before = like[: match.start("digits")].replace("-", "").replace("−", "").replace("+", "")
    return ("-" if value < 0 else "") + before + text + like[match.end("digits") :]


def _rate(cell: str) -> bool:
    return cell.endswith(("%", "‰"))


def _wrong(cells: list[str], total: str, marks: list[str]) -> str | None:
    """The sum of ``cells`` as ``total`` would show it, if it isn't ``total`` in any of the notations ``marks``; ""
    when it is, None when they aren't all numbers. A percentage between the cells' least and greatest is a rate of
    them all (a month's savings rate 19% each, 19% in all), not their sum."""
    wrong = None
    for mark in marks:
        read = [_number(cell, mark) for cell in [*cells, total]]
        if any(r is None for r in read):
            continue
        values = [r[0] for r in read if r is not None]
        added = sum(values[:-1], Decimal(0))
        if added == values[-1] or (_rate(total) and min(values[:-1]) <= values[-1] <= max(values[:-1])):
            return ""
        decimals = max(r[1] for r in read if r is not None)
        wrong = wrong or _shown(added, decimals, mark, total)
    return wrong


def mismatches(table: Table) -> list[str]:
    """Each total row's number that is neither the sum of the numbers shown in the rows since the last total above it,
    nor (a grand total under subtotals) of every row above it that is no total. A column whose rows aren't all numbers
    (or empty) isn't added up; read in either notation when the table's numbers don't show which, a sum is named only
    when it is wrong in both."""
    marks = _notation(table)
    found = []
    last = -1
    for index in sorted(table.totals):
        parts = [("its rows above", table.rows[last + 1 : index])]
        if last >= 0:  # under a subtotal: the rows since it, or (a grand total) all rows above that are no total
            every = [row for number, row in enumerate(table.rows[:index]) if number not in table.totals]
            parts = [(f"the rows since {table.rows[last][0]}", parts[0][1]), ("all rows above", every)]
        last = index
        total_row = table.rows[index]
        for column in range(1, len(table.header)):
            total = total_row[column]
            if not total:
                continue
            checked = [(what, _wrong(cells, total, marks)) for what, rows in parts if (cells := _column(rows, column))]
            wrong = [(what, added) for what, added in checked if added]
            if not wrong or any(added == "" for _, added in checked):
                continue
            name = table.header[column] or f"column {column + 1}"
            (what, added), *others = wrong
            sums = "".join([f"{what} add up to {added}", *(f", {what} to {added}" for what, added in others)])
            found.append(f"{total_row[0]} shows {total} under {name}, but {sums}")
    return found


def _column(rows: list[list[str]], column: int) -> list[str]:
    return [row[column] for row in rows if row[column]]
