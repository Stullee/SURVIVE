"""A cost statement with awkward numbers: odd areas, cents, a tenant for 17 days of the leap year 2024.

Independent check: the shares and parts by hand (Fractions, my own code), then
 (a) Ember's file evaluated by LibreOffice (recalculated on load),
 (b) Ember's own engine (sheets.values), and
 (c) the cover's texts (statement.cover_rows), which are what the cover draws.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import SCRATCH, fresh_jail, lo_convert  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from app.products import make, sheets, statement  # noqa: E402

SPEC = {
    "title": "Nebenkostenabrechnung 2024",
    "period": "01.01.2024 – 31.12.2024",
    "address": "Lindenstraße 7, 04109 Leipzig",
    "building": {"area": 287.37, "persons": 11, "units": 5},
    "tenants": [
        {"name": "EG links – Müller", "area": 63.17, "persons": 2, "prepaid": 1234.56},
        {"name": "EG rechts – Kurz", "area": 47.89, "persons": 1, "prepaid": 99.99, "from": "15.12.2024"},
        {"name": "EG rechts – Alt", "area": 47.89, "persons": 3, "prepaid": 1500, "to": "14.12.2024"},
        {"name": "1. OG – Şahin", "area": 85.03, "persons": 3, "prepaid": 2000.01},
        {"name": "DG – Ödön", "area": 41.41, "persons": 1, "prepaid": 777.77, "from": "01.03.2024"},
    ],
    "costs": [
        {"name": "Grundsteuer", "amount": 1180.43, "key": "area"},
        {"name": "Wasser und Abwasser", "amount": 1895.27, "key": "persons"},
        {"name": "Müllabfuhr", "amount": 612.11, "key": "persons"},
        {"name": "Gebäudeversicherung", "amount": 948.77, "key": "area"},
        {"name": "Hausmeister", "amount": 1200.01, "key": "units"},
        {
            "name": "Heizung und Warmwasser",
            "amount": 4321.09,
            "key": "direct",
            "parts": {
                "EG links – Müller": 1003.33,
                "EG rechts – Kurz": 21.07,
                "EG rechts – Alt": 902.18,
                "1. OG – Şahin": 1404.44,
                "DG – Ödön": 600.6,
            },
        },
        {"name": "Allgemeinstrom", "amount": 333.33, "key": "units"},
    ],
    "tenant_rows": 8,
    "cost_rows": 10,
}

START, END = date(2024, 1, 1), date(2024, 12, 31)
PERIOD = (END - START).days + 1


def half_up(value: Fraction) -> Fraction:
    d = Decimal(value.numerator) / Decimal(value.denominator)
    return Fraction(str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))


def by_hand():
    tenants = []
    for t in SPEC["tenants"]:
        begins = date(*reversed([int(x) for x in t["from"].split(".")])) if "from" in t else START
        ends = date(*reversed([int(x) for x in t["to"].split(".")])) if "to" in t else END
        days = (ends - begins).days + 1
        tenants.append((t, days))
    area_l = sum(Fraction(str(t["area"])) * Fraction(d, PERIOD) for t, d in tenants)
    pers_l = sum(Fraction(t["persons"]) * Fraction(d, PERIOD) for t, d in tenants)
    unit_l = sum(Fraction(d, PERIOD) for _, d in tenants)
    b = SPEC["building"]
    base = {
        "area": max(area_l, Fraction(str(b["area"]))),
        "persons": max(pers_l, Fraction(b["persons"])),
        "units": max(unit_l, Fraction(b["units"])),
    }
    out = []
    for t, d in tenants:
        time = Fraction(d, PERIOD)
        shares = {
            "area": Fraction(str(t["area"])) * time / base["area"],
            "persons": t["persons"] * time / base["persons"],
            "units": time / base["units"],
        }
        parts = []
        for c in SPEC["costs"]:
            if c["key"] == "direct":
                parts.append(Fraction(str(c["parts"].get(t["name"], 0))))
            else:
                parts.append(half_up(Fraction(str(c["amount"])) * shares[c["key"]]))
        pays = sum(parts)
        saldo = pays - Fraction(str(t["prepaid"]))
        out.append(dict(name=t["name"], days=d, shares=shares, parts=parts, pays=pays, saldo=saldo))
    return base, (area_l, pers_l, unit_l), out


def de(value: float, places: int = 2) -> str:
    q = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return f"{q:,.{places}f}".translate(str.maketrans({",": ".", ".": ","}))


def main() -> None:
    jail = fresh_jail("stmt_awkward")
    jail.write("nk.json", json.dumps(SPEC, ensure_ascii=False))
    made = make.cost_statement(jail, "nk.json", "shop/nk.xlsx")
    print("\n".join(made.report))
    base, listed, mine = by_hand()
    print("\nBY HAND: bases", {k: float(v) for k, v in base.items()}, "listed", [float(x) for x in listed])
    path = jail.root / "shop" / "nk.xlsx"
    lo = lo_convert(path, "xlsx", SCRATCH / "out" / "stmt_awkward_lo")
    wb = load_workbook(lo, data_only=True)
    ws = wb["Mieter"]
    vt = wb["Verteilung"]
    ember = sheets.values(path.read_bytes())
    problems = 0
    for i, m in enumerate(mine):
        row = 4 + i
        lo_row = {c: ws.cell(row=row, column=c).value for c in range(1, 14)}
        em_row = {c: ember["Mieter"].get((row, c)) for c in range(1, 14)}
        print(f"\n{m['name']} ({m['days']} days)")
        checks = [
            ("Tage", m["days"], lo_row[7], em_row[7], 0),
            ("Anteil Wfl %", float(m["shares"]["area"]) * 100, lo_row[8] * 100, em_row[8] * 100, 2),
            ("Anteil Pers %", float(m["shares"]["persons"]) * 100, lo_row[9] * 100, em_row[9] * 100, 2),
            ("Anteil Einh %", float(m["shares"]["units"]) * 100, lo_row[10] * 100, em_row[10] * 100, 2),
            ("Kostenanteil", float(m["pays"]), lo_row[11], em_row[11], 2),
            ("Saldo", float(m["saldo"]), lo_row[12], em_row[12], 2),
        ]
        for label, hand, lo_v, em_v, places in checks:
            h, l, e = de(hand, places), de(lo_v, places), de(em_v, places)
            flag = "" if h == l == e else "   <-- DIFFERS"
            problems += bool(flag)
            print(f"  {label:14} hand {h:>12}  LibreOffice {l:>12}  Ember-engine {e:>12}{flag}")
        for j, part in enumerate(m["parts"]):
            lo_part = vt.cell(row=4 + j, column=4 + i).value
            if de(float(part)) != de(lo_part):
                problems += 1
                print(f"  part {j}: hand {de(float(part))} LO {de(lo_part)}  <-- DIFFERS")
        print("  result:", lo_row[13])
    print("\nbases LO:", [ws.cell(row=8 + 4 + 2, column=c).value for c in (2, 3, 4)])
    # the cover's texts
    sums = made_sums = statement.work_out(statement.parse(json.dumps(SPEC, ensure_ascii=False)))
    layout = statement.Layout(SPEC["tenant_rows"], SPEC["cost_rows"])
    rows = statement.cover_rows(sums, layout)
    cols = statement.cover_columns(sums)
    print("\nCOVER columns:", [statement.TENANT_COLUMNS[c - 1][0] for c in cols])
    for r in rows:
        print("  ", r.kind, r.texts)
    # compare cover texts with LO's values for the money and shares
    for i, m in enumerate(mine):
        texts = rows[i].texts
        want = [de(float(m["shares"][k]) * 100) + "%" for k in ("area", "persons", "units")]
        want += [de(float(m["pays"])) + " €", de(float(m["saldo"])) + " €"]
        got = texts[-6:-1]
        if want != got:
            problems += 1
            print("COVER DIFFERS", m["name"], want, got)
    # Abrechnung (first tenant) by LO
    ab = wb["Abrechnung"]
    print("\nAbrechnung LO:", [(ab.cell(row=r, column=1).value, ab.cell(row=r, column=2).value) for r in (5, 6, 7, 8, 9, 11, 12, 13)])
    print("Abrechnung line shares LO:", [ab.cell(row=16 + j, column=4).value for j in range(len(SPEC["costs"]))])
    print("\nPROBLEMS:", problems)


if __name__ == "__main__":
    main()
