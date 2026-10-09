import json, sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail, SCRATCH
from sheets_checks import lo_csv
from app.products import make
jail = fresh_jail("csv_header")
jail.write("s/rows.csv", "Category,Planned,Actual\nRent,950,950\nFood,400,436.5\n")
spec = {"title": "CSV", "sheets": [{"name": "Budget", "columns": [{"title": "Category"}, {"title": "Planned", "format": "eur"},
        {"title": "Actual", "format": "eur"}, {"title": "Left", "format": "eur", "formula": "=B{row}-C{row}"}],
        "rows_csv": "s/rows.csv", "totals": {"Left": "sum"}}]}
jail.write("s/spec.json", json.dumps(spec))
m = make.spreadsheet(jail, "s/spec.json", "shop/csv.xlsx")
print(m.text())
print(lo_csv(jail.root / "shop" / "csv.xlsx", SCRATCH / "out" / "csv_header_lo")["Budget"])
