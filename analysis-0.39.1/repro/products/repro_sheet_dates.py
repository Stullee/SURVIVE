"""make_spreadsheet's pictures (0.37.5: 'every number as Excel shows it'): functions over a date column."""

from __future__ import annotations

import json

from app.products import sheets

spec = {
    "title": "Bill tracker",
    "sheets": [
        {
            "name": "Bills",
            "columns": [
                {"title": "Due", "format": "date"},
                {"title": "Bill", "format": "text"},
                {"title": "Amount", "format": "usd"},
            ],
            "rows": [
                ["2026-01-05", "Rent", 1200],
                ["2026-01-12", "Power", 80.5],
                ["2026-01-20", "Phone", 35],
            ],
            "totals": {"Amount": "sum"},
        },
        {
            "name": "Summary",
            "columns": [{"title": "What", "format": "text"}, {"title": "Value", "format": "general"},
                        {"title": "As a date", "format": "date"}],
            "rows": [
                ["Bills entered", "=COUNT(Bills!A2:A4)", None],
                ["Last due date", "=MAX(Bills!A2:A4)", "=MAX(Bills!A2:A4)"],
                ["First due date", "=MIN(Bills!A2:A4)", "=MIN(Bills!A2:A4)"],
                ["Bills due from 10 Jan", '=COUNTIF(Bills!A2:A4,">="&Bills!A3)', None],
            ],
        },
    ],
}
parsed = sheets.parse(json.dumps(spec), lambda path: "")
data = sheets.build(parsed)
for table in sheets._tables(parsed, data):
    print(table.header)
    for row in table.rows:
        print("   ", [text for text, _ in row])
print("Excel: COUNT = 3, MAX = 2026-01-20 (46042), MIN = 2026-01-05 (46027), COUNTIF = 2")
