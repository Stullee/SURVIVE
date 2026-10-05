NEBENKOSTENABRECHNUNG (make_cost_statement)
A landlord's statement of a building's operating costs, made by Ember's code from your JSON, free. Write the spec as a
.json file in your workspace, then call make_cost_statement with that source and an output ending in .xlsx.

{
  "title": "Nebenkostenabrechnung 2025",
  "period": "01.01.2025 – 31.12.2025",
  "address": "Musterstraße 12, 12345 Musterstadt",
  "theme": {"accent": "#2C5F8A", "font": "sans"},
  "building": {"area": 300, "persons": 8, "units": 4},
  "tenants": [
    {"name": "EG – Müller", "area": 90, "persons": 3, "prepaid": 1500},
    {"name": "OG – Schmidt", "area": 120, "persons": 4, "prepaid": 400, "to": "30.04.2025"},
    {"name": "OG – Yılmaz", "area": 120, "persons": 2, "prepaid": 900, "from": "01.05.2025"}
  ],
  "costs": [
    {"name": "Grundsteuer", "amount": 1200, "key": "area"},
    {"name": "Wasser", "amount": 1600, "key": "persons"},
    {"name": "Hausmeister", "amount": 480, "key": "units"},
    {"name": "Heizung", "amount": 3800, "key": "direct", "parts": {"EG – Müller": 1250.40, "OG – Yılmaz": 1105.75}}
  ],
  "tenant_rows": 10,
  "cost_rows": 20,
  "notes": ["Mit KI-Unterstützung erstellt: bitte prüfen Sie die Zahlen.", "..."]
}

- key: area (by Wohnfläche), persons, units (the same share each) or direct: each tenant's amount as given in parts,
  by name (heating and hot water from the Messdienst's Heizkostenabrechnung; one left out pays none; what the parts
  leave isn't passed on). Euros with at most 2 decimals; prepaid is the year's Vorauszahlungen; area in m².
- from and to (TT.MM.JJJJ, within period, whose two dates period must name): a tenant who moved in or out pays for
  their days only. Two tenants of a flat in turn count as the flat once.
- building: when the building has more than the tenants listed (a vacant flat or days, the owner's own): its whole
  Wohnfläche, Personen, Einheiten. The shares are then of those, as on a real statement, and the cover shows its row
  (90 of 300 m² reads as 30%). Without it the shares are of the tenants listed together, and empty days are theirs.
- tenants: 1 to 20, names different, at most 40 characters, with a letter, without * ? ~. tenant_rows and cost_rows:
  rows the buyer can fill (up to 20 and 40; 10 and 20 if left out). notes become the sheet Anleitung.

WHAT YOU GET
- The Excel file: Mieter (days, shares, costs, Saldo and result of each tenant, what the shares are of, the period),
  Kosten (Umlageschlüssel as a dropdown), Einzelbeträge (the amounts by Direkt), Verteilung (each part to the cent;
  Nicht umgelegt: cents, the building's other units) and Abrechnung (the letter of the tenant chosen in its dropdown).
  Formulas do every sum, so the buyer's own numbers work; the cells to fill in are tinted.
- name-cover.png, 3000 x 2250: the Mieter table as German Excel shows it under your title. Use it as photo 1, or in
  make_image (square or portrait keeps it readable); make_image shows the sheets as 'name.xlsx#Abrechnung'.
- Ember's code works out every formula and keeps nothing unless each number equals its own sums, so the cover shows
  the file's numbers: never type them yourself, and never pay the workshop for a statement or its cover.

SAY IT IN THE NOTES AND THE LISTING
- Heating and hot water (Heizkostenverordnung) and metered water come from the Messdienst's statement as Direkt: the
  file reads no meters.
- Only costs the lease allows may be passed on (§ 2 BetrKV; cable TV fees no longer since 1 July 2024), and the
  statement is due within 12 months of the period's end (§ 556 BGB). It is no legal advice.
- How to fill it in: the period and the tenants on Mieter (Von and Bis for one who moved; the building below), costs
  on Kosten, amounts by Direkt on Einzelbeträge, then choose each tenant on Abrechnung and print.
