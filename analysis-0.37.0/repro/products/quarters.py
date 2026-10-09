import json, sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail
from app.products import make
jail = fresh_jail("quarters")
months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
spec = {"title": "Yearly savings", "sheets": [
    {"name": "Months", "title": "Savings by month", "columns": [{"title": "Month"}, {"title": "Saved", "format": "eur"}],
     "rows": [[m, None] for m in months], "totals": {"Saved": "sum"}},
    {"name": "Quarters", "title": "Savings by quarter", "columns": [{"title": "Quarter"}, {"title": "Saved", "format": "eur"}],
     "rows": [["Q1", "=SUM(Months!B4:B6)"], ["Q2", "=SUM(Months!B7:B9)"], ["Q3", "=SUM(Months!B10:B12)"],
              ["Q4", "=SUM(Months!B13:B15)"], ["Year", "=SUM(B4:B7)"]]},
]}
jail.write("s.json", json.dumps(spec))
m = make.spreadsheet(jail, "s.json", "shop/q.xlsx")
print("\n".join(m.report))
