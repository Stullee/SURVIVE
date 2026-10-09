import copy, json, sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import SCRATCH, fresh_jail
from sheets_checks import lo_csv
from sheets_formula_checks import INCOME, EXPENSES, summary
from app.products import make, sheets
jail = fresh_jail("sheets_wholecol")
spec = {"title": "Budget", "sheets": [copy.deepcopy(INCOME), copy.deepcopy(EXPENSES), summary([
    ["Income", "=SUM(Income!C:C)"],
    ["Spent", "=SUM(Expenses!C:C)"],
    ["Items", "=COUNTA(Expenses!A:A)"],
    ["Net", "=B4-B5"],
])]}
jail.write("s/spec.json", json.dumps(spec))
made = make.spreadsheet(jail, "s/spec.json", "shop/wc.xlsx")
print("\n".join(made.report))
parsed = sheets.parse(json.dumps(spec), jail.read)
vals = sheets._spec_book(parsed)["summary"]
print("picture (make_spreadsheet preview):", [(parsed.sheets[2].rows[i][0], sheets._shown(vals.cell(i, 1), "eur")) for i in range(4)])
_, pic = sheets.picture(jail.read_bytes("shop/wc.xlsx"), "Summary")
g = sheets._grids([(n, sheets._cells(ws, 100, 10)) for n, ws in [(w.title, w) for w in sheets._book(jail.read_bytes("shop/wc.xlsx")).worksheets]])
print("make_image picture engine:", [g["summary"].value(r, 2) for r in range(4, 8)])
lo = lo_csv(jail.root / "shop" / "wc.xlsx", SCRATCH / "out" / "sheets_wholecol_lo")
print("LibreOffice:", lo["Summary"][3:7])
