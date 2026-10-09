"""Review probe: ordinary (harmless) Excel formulas a workshop run might write, through the workshop's file check."""

from __future__ import annotations

import io

from openpyxl import Workbook

from app.products import checks

CASES = {
    "plain sum": "=SUM(A2:A5)",
    "text with a pipe": '="Size | Price"',
    "web link formula": '=HYPERLINK("https://example.org/shop","Shop")',
    "TEXTJOIN with a pipe separator": '=TEXTJOIN(" | ",TRUE,A2:A5)',
}
for label, formula in CASES.items():
    book = Workbook()
    sheet = book.active
    for row in range(2, 6):
        sheet.cell(row=row, column=1, value=row)
    sheet["B1"] = formula
    out = io.BytesIO()
    book.save(out)
    try:
        checks.check("table.xlsx", out.getvalue())
        print(f"kept     {label}: {formula}")
    except checks.Refused as exc:
        print(f"REFUSED  {label}: {formula} -> {exc}")
