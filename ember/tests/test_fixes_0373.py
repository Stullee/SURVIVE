"""0.37.3: a spreadsheet's pictures show the buyer's file, and its Check line names what is wrong, not what is right.

From the 0.37.0 analysis (sections 3.5, 4.6.1 and 4.6.2) and its reproductions (analysis-0.37.0/repro/products):

- make_spreadsheet drew its pictures with an evaluator of its own that read the spec. A whole column was its data rows
  there, while Excel takes the header and the total row in too: =SUM(Income!C:C) was 3,570.50 € in the picture and
  7,141.00 € in the buyer's file, and shares of =SUM(B:B) added up to 100% in the picture and 50% in the file. A total
  left out the empty rows' formulas (an average of 821.21 € for the file's 307.95 €), 1234567 was "1.23457e+06",
  15.01.2026 was "2026-01-15" and a count total the text "=COUNTA(…)". The pictures are drawn from the file now, worked
  out as make_image's are, in Excel's General format and the columns' date formats.
- The Check line said nothing of a whole column, called four correct quarterly sums wrong, and missed a short total
  on its own sheet, a range of two columns, a COUNTA that counts the header and a template's column formula.
- rows_csv made a CSV's header line a data row (#VALUE! in its formula and the total), and a trailing empty field kept
  the column's formula out.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.products import make, sheets
from tests.test_products import jail

INCOME = {
    "name": "Income",
    "title": "Income",
    "columns": [{"title": "Source"}, {"title": "Month"}, {"title": "Amount", "format": "eur"}],
    "rows": [["Salary", "Jan", 3000], ["Side job", "Jan", 450.5], ["Etsy", "Jan", 120]],
    "empty_rows": 6,
    "totals": {"Amount": "sum"},
}  # data rows 4 to 12, its total in row 13
EXPENSES = {
    "name": "Expenses",
    "columns": [{"title": "Item"}, {"title": "Planned", "format": "eur"}, {"title": "Actual", "format": "eur"}],
    "rows": [["Rent", 950, 950], ["Food", 400, 436.5]],
    "empty_rows": 8,
    "totals": {"Actual": "sum"},
}  # no title: data rows 2 to 11, its total in row 12
WHOLE = [
    ["Income", "=SUM(Income!C:C)"],
    ["Spent", "=SUM(Expenses!C:C)"],
    ["Items", "=COUNTA(Expenses!A:A)"],
    ["Net", "=B4-B5"],
]
SPENDING = {
    "name": "Spending",
    "title": "Where the money goes",
    "columns": [
        {"title": "Category", "width": 20},
        {"title": "Amount", "format": "eur"},
        {"title": "Share", "format": "percent", "formula": "=IFERROR(B{row}/SUM(B:B),0)"},
    ],
    "rows": [["Rent", 950], ["Food", 400], ["Transport", 150]],
    "empty_rows": 5,
    "totals": {"Amount": "sum", "Share": "sum"},
}  # data rows 4 to 11, its total in row 12
BUDGET = {
    "title": "Household budget",
    "notes": ["Made with AI help."],
    "sheets": [
        {
            "name": "Budget",
            "title": "Monthly budget",
            "columns": [
                {"title": "Category", "width": 24},
                {"title": "Planned", "format": "eur"},
                {"title": "Actual", "format": "eur"},
                {"title": "Left", "format": "eur", "formula": "=B{row}-C{row}"},
                {"title": "Note", "format": "general"},
                {"title": "Due", "format": "date_de"},
                {"title": "Paid on", "format": "date_us"},
                {"title": "Status", "choices": ["open", "paid"]},
            ],
            "rows": [
                ["Rent", 950, 950, None, 12345.67, "2026-01-15", "2026-01-03", "paid"],
                ["Food", 400, 436.5, None, 1234567, "2026-01-31", "2026-02-01", "open"],
                ["Savings goal", 2500.125, 0, None, 0.123456789, "2026-12-31", None, "open"],
            ],
            "empty_rows": 5,
            "totals": {"Planned": "sum", "Actual": "sum", "Left": "average", "Status": "count", "Note": "min"},
        }
    ],
}  # the analysis' budget (repro/products/sheets_checks.py)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def summary(rows: list[list[Any]], **more: Any) -> dict[str, Any]:
    columns = [{"title": "What"}, {"title": "Amount", "format": "eur"}]
    return {"name": "Summary", "title": "Summary", "columns": columns, "rows": rows, **more}  # data from row 4


def book(*more: dict[str, Any]) -> str:
    """A budget's spec: Income and Expenses, and the sheets ``more``."""
    return json.dumps({"title": "Budget", "sheets": [copy.deepcopy(INCOME), copy.deepcopy(EXPENSES), *more]})


def warnings(*more: dict[str, Any]) -> list[str]:
    return sheets.parse(book(*more), lambda path: "").warnings


def shown(text: str, data: bytes | None = None, rows_csv: str = "") -> dict[str, list[list[str]]]:
    """What make_spreadsheet's picture of each sheet shows, row by row: its header, its rows and its total row."""
    spec = sheets.parse(text, lambda path: rows_csv)
    tables = sheets._tables(spec, data if data is not None else sheets.build(spec))
    found = {}
    for sheet, table in zip(spec.sheets, tables, strict=True):
        rows = [[cell for cell, _ in row] for row in [*table.rows, *([table.total] if table.total else [])]]
        found[sheet.name] = [table.header, *rows]
    return found


# --- the pictures: the buyer's file -----------------------------------------------------------------------------------


def test_a_whole_columns_sum_shows_what_the_file_does_and_the_check_line_names_it(tmp_path: Path) -> None:
    workspace = jail(tmp_path)
    workspace.write("s/budget.json", book(summary(WHOLE)))
    made = make.spreadsheet(workspace, "s/budget.json", "shop/budget.xlsx")
    assert "shop/budget-sheet3.png" in made.paths
    data = workspace.read_bytes("shop/budget.xlsx")
    # Excel adds Income's total row (13) to its data, and COUNTA counts Expenses' header and "Total" too: in the
    # analysis LibreOffice showed these, and the picture 3,570.50 €, 1,386.50 €, 2 and 2,184.00 €
    assert shown(book(summary(WHOLE)), data)["Summary"] == [
        ["What", "Amount"],
        ["Income", "7,141.00 €"],
        ["Spent", "2,773.00 €"],
        ["Items", "4.00 €"],
        ["Net", "4,368.00 €"],
    ]
    assert sheets.values(data)["Summary"][(4, 2)] == 7141.0  # make_image's sheet pictures, of the same file
    assert (
        "Check: Summary row 4: Income!C:C, the whole column, counts the total row (row 13) of Income besides its "
        "data: Income!C4:C12 takes the data alone. Summary row 5: Expenses!C:C, the whole column, counts the total "
        "row (row 12) of Expenses besides its data: Expenses!C2:C11 takes the data alone. Summary row 6: Expenses!A:A, "
        "the whole column, counts the header row (row 1) and the total row (row 12) of Expenses besides its data: "
        "Expenses!A2:A11 takes the data alone."
    ) in made.text()


def test_shares_of_a_whole_column_show_the_files_half_and_are_named() -> None:
    text = json.dumps({"sheets": [SPENDING]})
    rows = shown(text)["Spending"]
    assert [row[2] for row in rows[1:4]] == ["31.7%", "13.3%", "5.0%"]  # the picture showed 63.3%, 26.7%, 10.0%
    assert rows[-1] == ["Total", "1,500.00 €", "50.0%"]
    assert sheets.parse(text, lambda path: "").warnings == [
        "Spending column C (Share): B:B, the whole column, counts the total row (row 12) of Spending besides its "
        "data: B4:B11 takes the data alone"
    ]
    fixed = json.loads(text)
    fixed["sheets"][0]["columns"][2]["formula"] = "=IFERROR(B{row}/SUM(B{first}:B{last}),0)"
    assert sheets.parse(json.dumps(fixed), lambda path: "").warnings == []
    assert shown(json.dumps(fixed))["Spending"][-1] == ["Total", "1,500.00 €", "100.0%"]


def test_the_pictures_numbers_dates_and_totals_are_the_files() -> None:
    # every text as LibreOffice showed the file in the analysis (repro/products/sheets_checks.py)
    assert shown(json.dumps(BUDGET))["Budget"] == [
        ["Category", "Planned", "Actual", "Left", "Note", "Due", "Paid on", "Status"],
        ["Rent", "950.00 €", "950.00 €", "0.00 €", "12345.67", "15.01.2026", "01/03/2026", "paid"],
        ["Food", "400.00 €", "436.50 €", "-36.50 €", "1234567", "31.01.2026", "02/01/2026", "open"],
        ["Savings goal", "2,500.13 €", "0.00 €", "2,500.13 €", "0.123456789", "31.12.2026", "", "open"],
        *[["", "", "", "0.00 €", "", "", "", ""]] * 4,  # 4 of the 5 empty rows, their Left formula worked out
        ["Total", "3,850.13 €", "1,386.50 €", "307.95 €", "0.123456789", "", "", "3"],
    ]


def test_a_total_below_row_2000_is_in_the_picture() -> None:
    log = {"name": "Log", "title": "Log", "columns": [{"title": "Hours", "format": "integer"}], "rows": [[1]] * 1990}
    rows = shown(json.dumps({"sheets": [{**log, "empty_rows": 20, "totals": {"Hours": "sum"}}]}))["Log"]
    assert rows[-1] == ["1,990"]  # the total in row 2,014: the file was read to row 2,000 only


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (1234567, "1234567"),  # %g: 1.23457e+06
        (12345.67, "12345.67"),  # %g: 12345.7
        (-1234567.891, "-1234567.891"),
        (1 / 3, "0.333333333"),
        (math.pi, "3.141592654"),
        (1234.5678901234, "1234.56789"),
        (0.1 + 0.2, "0.3"),
        (-0.000123456789, "-0.000123457"),
        (0.00001, "0.00001"),
        (0.0000123456, "1.23456E-05"),
        (12345678901, "12345678901"),
        (123456789012, "1.23457E+11"),
        (1e15, "1E+15"),
    ],
)
def test_a_general_number_is_shown_as_excels_general_format_shows_it(value: float, text: str) -> None:
    assert sheets.cell_text(value, "General") == text  # make_image's sheet pictures too


def test_a_date_formulas_day_is_shown_as_its_date() -> None:
    days = [sheets.cell_text(day, "dd.mm.yyyy") for day in (46037, 46037.75, 61, 60, 30, 0, -7)]
    assert days == ["15.01.2026", "15.01.2026", "01.03.1900", "29.02.1900", "30.01.1900", "00.01.1900", "########"]
    bills = {
        "name": "Bills",
        "columns": [
            {"title": "Bill"},
            {"title": "Due", "format": "date_de"},
            {"title": "Remind", "format": "date_us", "formula": "=B{row}-7"},
        ],
        "rows": [["Rent", "2026-01-15"]],
        "empty_rows": 1,
    }
    # an empty Due less 7 is a day before Excel's first: Excel fills the cell with #
    assert shown(json.dumps({"sheets": [bills]}))["Bills"][1:] == [
        ["Rent", "15.01.2026", "01/08/2026"],
        ["", "", "########"],
    ]


# --- the Check line: whole columns ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("formula", "named"),
    [
        ("=SUM(Income!C:C)", "the total row (row 13) of Income"),
        ("=AVERAGE(Income!$C:$C)", "the total row (row 13) of Income"),
        ("=MAX(Expenses!C:C)", "the total row (row 12) of Expenses"),
        ("=COUNTA(Expenses!A:A)", "the header row (row 1) and the total row (row 12) of Expenses"),
        ('=COUNTIF(Expenses!C:C,">0")', "the total row (row 12) of Expenses"),  # a positive total meets it
        ("=SUM(Expenses!B:C)", "the total row (row 12) of Expenses"),
        ('=SUMIF(Expenses!A:A,"Rent",Expenses!C:C)', ""),  # "Total" isn't "Rent"
        ('=SUMIFS(Expenses!C:C,Expenses!A:A,"Food",Expenses!C:C,">0")', ""),
        ("=SUM(Expenses!B:B)", ""),  # its total row is empty, and the header's text is no number
        ('=VLOOKUP("Rent",Expenses!A:C,3,FALSE)', ""),
        ('=INDEX(Expenses!C:C,MATCH("Food",Expenses!A:A,0))', ""),
    ],
)
def test_a_whole_column_is_named_where_its_function_counts_what_is_not_data(formula: str, named: str) -> None:
    found = warnings(summary([["Value", formula]]))
    if not named:
        assert found == []
    else:
        [warning] = found
        assert f" counts {named} besides its data: " in warning


# --- the Check line: what is right is not named, what is wrong is -----------------------------------------------------


def test_parts_of_the_data_running_sums_and_subtotals_are_not_named() -> None:
    months = {
        "name": "Months",
        "title": "Savings by month",
        "columns": [{"title": "Month"}, {"title": "Saved", "format": "eur"}],
        "rows": [[month, None] for month in MONTHS],
        "totals": {"Saved": "sum"},
    }  # data rows 4 to 15
    quarters = {
        "name": "Quarters",
        "title": "Savings by quarter",
        "columns": [{"title": "Quarter"}, {"title": "Saved", "format": "eur"}],
        "rows": [
            ["Q1", "=SUM(Months!B4:B6)"],
            ["Q2", "=SUM(Months!B7:B9)"],
            ["Q3", "=SUM(Months!B10:B12)"],
            ["Q4", "=SUM(Months!B13:B15)"],
            ["Year", "=SUM(B4:B7)"],
        ],
    }  # each was named as leaving out the rest of the year (repro/products/quarters.py)
    assert sheets.parse(json.dumps({"sheets": [months, quarters]}), lambda path: "").warnings == []
    running = copy.deepcopy(months)
    running["columns"].append({"title": "So far", "format": "eur", "formula": "=SUM(B$4:B{row})"})
    running["columns"].append({"title": "Still to come", "format": "eur", "formula": "=SUM(B{row}:B$15)"})
    running["columns"].append({"title": "Share", "format": "percent", "formula": "=B{row}/SUM(B{first}:B{last})"})
    assert sheets.parse(json.dumps({"sheets": [running]}), lambda path: "").warnings == []
    below = copy.deepcopy(months)  # the quarters under the months, on their sheet
    below["rows"] += [["Q1", "=SUM(B4:B6)"], ["Q2", "=SUM(B7:B9)"], ["Q3", "=SUM(B10:B12)"], ["Q4", "=SUM(B13:B15)"]]
    below["rows"] += [["Half", "=SUM(B4:B9)"], ["Year", "=SUM(B16:B19)"]]
    assert sheets.parse(json.dumps({"sheets": [below]}), lambda path: "").warnings == []
    sections = summary([["Rent", 950], ["Food", 400], ["Housing", "=SUM(B4:B5)"], ["Fun", 80], ["Other", "=B7"]])
    assert warnings(sections) == []


@pytest.mark.parametrize(
    ("rows", "named"),
    [
        (
            [["Rent", 950], ["Food", 400], ["Fun", 80], ["Total", "=SUM(B4:B5)"]],
            "Summary row 7: B4:B5 leaves out row 6 above it: B4:B6 takes them all",
        ),
        (
            [["Both", "=SUM(Expenses!B2:C5)"]],
            "Summary row 4: Expenses!B2:C5 leaves out some of the data of Expenses (rows 2 to 11; its total is in row "
            "12): Expenses!B2:C11 takes them all",
        ),
        (
            [["Count", "=COUNTA(Expenses!A1:A11)"]],
            "Summary row 4: Expenses!A1:A11 counts the header row (row 1) of Expenses besides its data: "
            "Expenses!A2:A11 takes the data alone",
        ),
        (
            [["Income", "=SUM(Income!C4:C9)"]],  # in row 4 as Income's data start: no running sum
            "Summary row 4: Income!C4:C9 leaves out some of the data of Income (rows 4 to 12; its total is in row 13): "
            "Income!C4:C12 takes them all",
        ),
    ],
)
def test_a_short_range_on_its_own_sheet_or_of_two_columns_and_a_counted_header_are_named(
    rows: list[list[Any]], named: str
) -> None:
    assert warnings(summary(rows)) == [named]  # repro/products/sheets_formula_checks.py, F, H and I


def test_a_templates_column_formula_is_checked_once_with_rows_or_without() -> None:
    tracker = {
        "name": "Tracker",
        "title": "Tracker",
        "columns": [
            {"title": "Date", "format": "date"},
            {"title": "Hours", "format": "number"},
            {"title": "Pay", "format": "eur", "formula": "=B{row}*Income!C3"},
        ],
        "rows": [],
        "empty_rows": 30,
    }
    named = [
        "Tracker column C (Pay): Income!C3 is the header row of Income, whose data are rows 4 to 12 (its total row 13)"
    ]
    assert warnings(tracker) == named  # rows only were read: a template's column formula never was
    tracker["rows"] = [["2026-01-05", 3], ["2026-01-06", 4], ["2026-01-07", 5]]
    assert warnings(tracker) == named  # once, not once a row


def test_the_check_line_names_ten_formulas_and_counts_the_rest() -> None:
    found = warnings(summary([[f"Item {n}", "=Income!C3"] for n in range(12)]))
    assert len(found) == 11 and found[-1] == "and 2 more like these"


# --- rows_csv ---------------------------------------------------------------------------------------------------------


def test_a_csvs_header_line_is_left_out_and_an_empty_field_gets_the_columns_formula() -> None:
    columns = [
        {"title": "Category"},
        {"title": "Planned", "format": "eur"},
        {"title": "Actual", "format": "eur"},
        {"title": "Left", "format": "eur", "formula": "=B{row}-C{row}"},
    ]
    text = json.dumps(
        {"sheets": [{"name": "Budget", "columns": columns, "rows_csv": "s/rows.csv", "totals": {"Left": "sum"}}]}
    )
    header = "Category,Planned,Actual\nRent,950,950\nFood,400,436.5\n"  # repro/products/csv_header.py
    parsed = sheets.parse(text, lambda path: header)
    assert parsed.warnings == [] and parsed.sheets[0].rows == [
        ["Rent", 950, 950, "=B2-C2"],
        ["Food", 400, 436.5, "=B3-C3"],
    ]
    assert shown(text, rows_csv=header)["Budget"][
        1:
    ] == [  # LibreOffice showed #VALUE! in a "Category" row and the total
        ["Rent", "950.00 €", "950.00 €", "0.00 €"],
        ["Food", "400.00 €", "436.50 €", "-36.50 €"],
        ["Total", "", "", "-36.50 €"],
    ]
    trailing = sheets.parse(text, lambda path: "Rent,950,950,\nFood,400,436.5, \nFun,80,95\n")
    assert [row[3] for row in trailing.sheets[0].rows] == ["=B2-C2", "=B3-C3", "=B4-C4"]  # the first two had none
    other = sheets.parse(text, lambda path: "Item,Budget,Spent\nRent,950,950\n")
    assert other.warnings == [
        "Budget row 2: the first line of s/rows.csv (Item, Budget, Spent) looks like a header, not data: the columns "
        "give the titles, so take it out of s/rows.csv"
    ]
    rows = json.loads(text)
    del rows["sheets"][0]["rows_csv"]
    rows["sheets"][0]["rows"] = [["Rent", 950, 950, ""], ["Food", 400, 436.5, " "]]
    assert [row[3] for row in sheets.parse(json.dumps(rows), lambda path: "").sheets[0].rows] == ["=B2-C2", "=B3-C3"]


# --- LibreOffice works the files out as the pictures do ---------------------------------------------------------------


def recalculated(soffice: str, path: Path, profile: Path) -> dict[str, list[list[str]]]:
    """Every sheet of an Excel file as LibreOffice shows it once it has calculated every formula, as text."""
    settings = profile / "user" / "registrymodifications.xcu"
    settings.parent.mkdir(parents=True, exist_ok=True)
    recalc = (
        '<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="{}" oor:op="fuse">'
        "<value>0</value></prop></item>"
    )
    settings.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + recalc.format("OOXMLRecalcMode")
        + recalc.format("ODFRecalcMode")
        + "</oor:items>\n"
    )
    out = path.parent / "csv"
    as_shown = "csv:Text - txt - csv (StarCalc):44,34,76,1,,1033,false,true,true,false,false,-1"  # every sheet
    command = [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", "--convert-to"]
    command += [as_shown, "--outdir", str(out), str(path)]
    subprocess.run(command, capture_output=True, timeout=240, check=True)  # noqa: S603
    found = {}
    for sheet in out.glob(f"{path.stem}-*.csv"):
        found[sheet.stem[len(path.stem) + 1 :]] = list(csv.reader(io.StringIO(sheet.read_text(encoding="utf-8"))))
    return found


def test_libreoffice_shows_the_numbers_the_pictures_show(tmp_path: Path) -> None:
    soffice = shutil.which("soffice")
    if soffice is None:
        pytest.skip("LibreOffice isn't installed")
    for name, text in (
        ("whole", book(summary(WHOLE))),
        ("share", json.dumps({"sheets": [SPENDING]})),
        ("budget", json.dumps(BUDGET)),
    ):
        spec = sheets.parse(text, lambda path: "")
        path = tmp_path / name / f"{name}.xlsx"
        path.parent.mkdir()
        path.write_bytes(sheets.build(spec))
        theirs = recalculated(soffice, path, tmp_path / "profile")
        for sheet, table in zip(spec.sheets, sheets._tables(spec, path.read_bytes()), strict=True):
            first, last = sheets.data_rows(sheet)
            rows = {first - 1: table.header} | {
                first + i: [cell for cell, _ in row] for i, row in enumerate(table.rows)
            }
            if table.total:
                rows[last + 1] = [cell for cell, _ in table.total]
            for number, row in rows.items():
                assert theirs[sheet.name][number - 1][: len(row)] == row, (name, sheet.name, number)
