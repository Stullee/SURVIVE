import json, sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail, SCRATCH
from sheets_checks import lo_csv
from app.products import make, sheets
jail = fresh_jail("share_of_total")
spec = {"title": "Spending", "sheets": [{"name": "Spending", "title": "Where the money goes",
    "columns": [{"title": "Category", "width": 20}, {"title": "Amount", "format": "eur"},
                {"title": "Share", "format": "percent", "formula": "=IFERROR(B{row}/SUM(B:B),0)"}],
    "rows": [["Rent", 950], ["Food", 400], ["Transport", 150]], "empty_rows": 5,
    "totals": {"Amount": "sum", "Share": "sum"}}]}
jail.write("s.json", json.dumps(spec))
m = make.spreadsheet(jail, "s.json", "shop/share.xlsx")
print("\n".join(m.report))
parsed = sheets.parse(json.dumps(spec), jail.read)
v = sheets._spec_book(parsed)["spending"]
print("make_spreadsheet picture:", [sheets._shown(v.cell(i, 2), "percent") for i in range(3)], "total", sheets._shown(v.total(2), "percent"))
print("LibreOffice:", [r[2] for r in lo_csv(jail.root / "shop" / "share.xlsx", SCRATCH / "out" / "share_lo")["Spending"][3:12]])
