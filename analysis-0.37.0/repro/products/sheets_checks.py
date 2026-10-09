"""make_spreadsheet with the real code: pictures vs what the file shows (LibreOffice recalculated), and the formula
checks (0.19.2 / 0.32.0) on specs that should and shouldn't be flagged."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys

sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import SCRATCH, _profile, fresh_jail  # noqa: E402

from app.products import make, sheets  # noqa: E402


def lo_csv(path, outdir) -> dict[str, list[list[str]]]:
    """Every sheet as LibreOffice shows it (formatted text), recalculated."""
    outdir.mkdir(parents=True, exist_ok=True)
    flt = "csv:Text - txt - csv (StarCalc):44,34,76,1,,1033,false,true,true,false,false,-1"
    cmd = ["soffice", f"-env:UserInstallation={_profile()}", "--headless", "--norestore", "--convert-to", flt,
           "--outdir", str(outdir), str(path)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=True)
    out = {}
    for f in sorted(outdir.glob(f"{path.stem}-*.csv")):
        out[f.stem[len(path.stem) + 1:]] = list(csv.reader(io.StringIO(f.read_text(encoding="utf-8"))))
    return out


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
}


def main() -> None:
    jail = fresh_jail("sheets")
    jail.write("s/budget.json", json.dumps(BUDGET))
    made = make.spreadsheet(jail, "s/budget.json", "shop/budget.xlsx")
    print("\n".join(made.report))
    lo = lo_csv(jail.root / "shop" / "budget.xlsx", SCRATCH / "out" / "sheets_lo")
    print("\nLibreOffice shows the Budget sheet (rows 3-13):")
    for row in lo["Budget"][2:13]:
        print("   ", row)
    # what the picture shows: the same texts the preview draws
    spec = sheets.parse(json.dumps(BUDGET), jail.read)
    values = sheets._spec_book(spec)["budget"]
    sheet = spec.sheets[0]
    print("\nmake_spreadsheet's picture draws:")
    for index in range(len(sheet.rows)):
        print("   ", [sheets._shown(values.cell(index, c), col.format) for c, col in enumerate(sheet.columns)])
    texts = ["Total"] + [""] * (len(sheet.columns) - 1)
    for c, fn in sheet.totals.items():
        total = values.total(c)
        texts[c] = sheets._shown(total, sheet.columns[c].format) if total is not None else f"={fn}(…)"
    print("    total row:", texts)
    # make_image's 'file.xlsx#Budget' path: sheets.picture's texts
    number, picture = sheets.picture(jail.read_bytes("shop/budget.xlsx"), "Budget")
    picture.save(SCRATCH / "out" / "sheets" / "budget-image-picture.png")


if __name__ == "__main__":
    main()
