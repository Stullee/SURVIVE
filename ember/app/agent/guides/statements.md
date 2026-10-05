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
    {"name": "EG links – Müller", "area": 90, "persons": 3, "prepaid": 1500},
    {"name": "1. OG – Schmidt", "area": 120, "persons": 4, "prepaid": 400, "to": "30.04.2025"},
    {"name": "1. OG – Yılmaz", "area": 120, "persons": 2, "prepaid": 900, "from": "01.05.2025"}
  ],
  "costs": [
    {"name": "Grundsteuer", "amount": 1200, "key": "area"},
    {"name": "Wasser und Abwasser", "amount": 1600, "key": "persons"},
    {"name": "Hausmeister", "amount": 480, "key": "units"},
    {"name": "Heizung und Warmwasser", "amount": 3800, "key": "direct",
     "parts": {"EG links – Müller": 1250.40, "1. OG – Schmidt": 520.10, "1. OG – Yılmaz": 1105.75}}
  ],
  "tenant_rows": 10,
  "cost_rows": 20,
  "notes": ["Mit KI-Unterstützung erstellt: bitte prüfen Sie die Zahlen.", "..."]
}

- key: area (by Wohnfläche), persons (by Personen), units (by Einheiten: the same share for each) or direct (each
  tenant's amount as given in parts, by name: heating and hot water from the Messdienst's Heizkostenabrechnung; a
  tenant left out pays none of it, and what the parts leave of the amount isn't passed on). Amounts and prepaid (the
  year's Vorauszahlungen) in euros, at most 2 decimals; area in m².
- from and to: a tenant who moved in or out during the period (TT.MM.JJJJ, both days included, within the period):
  they pay for their days only. They need the period's two dates in period. Two tenants of a flat in turn count as
  the flat once; give building too, or a flat's empty days are paid by the other tenants.
- building: only when the building has more than the tenants you list (a vacant flat, the owner's own, tenants
  without a statement): its whole Wohnfläche, Personen and Einheiten. The shares are then of those, as on a real
  statement, and the cover shows the whole building's row: 90 of 300 m² reads as 30% at a glance. Leave it out and
  the shares are of the tenants listed together (100%).
- tenants: 1 to 20, each name different, at most 40 characters, with a letter and without * ? ~ (Excel looks the
  names up). tenant_rows and cost_rows: the rows the buyer can fill (up to 20 and 40; 10 and 20 if left out); the
  sample's come first.
- notes become the sheet Anleitung. theme as in make_spreadsheet.

WHAT YOU GET
- The Excel file: Mieter (each tenant's days, shares, costs, Saldo, Nachzahlung or Guthaben, what the shares are of
  and the period's dates), Kosten (each with its Umlageschlüssel as a dropdown), Einzelbeträge (each tenant's amount of
  the costs by Direkt, to type in), Verteilung (each tenant's part of each cost, to the cent) and Abrechnung (a
  statement to print for the tenant chosen in its dropdown, with their days). Formulas do every sum, so the buyer's own
  numbers work the same way; the cells to fill in are tinted.
- name-cover.png, 3000 x 2250: the Mieter table as German Excel shows it under your title, address and period. Use it
  as the listing's first photo, or in make_image (shape square or portrait keeps it readable). make_image shows the
  file's sheets as 'name.xlsx#Abrechnung', in German notation too.
- Ember's code works out every formula of the file and keeps nothing unless each number equals its own sums. So the
  cover shows the file's numbers: never type them yourself, and never pay the workshop for a statement or its cover.
- Each tenant's part of each cost is rounded to the cent, as on a real statement; Verteilung shows what is left over
  (Nicht umgelegt: cents, and the building's other units).

SAY IT IN THE NOTES AND THE LISTING
- Costs by consumption (Heizkosten and Warmwasser under the Heizkostenverordnung, water with meters) come from the
  Messdienst's statement, typed in as Direkt: the file reads no meters and doesn't split them itself.
- Only costs the lease allows may be passed on (§ 2 BetrKV; cable TV fees no longer since 1 July 2024), and the
  statement is due within 12 months of the period's end (§ 556 BGB). It is no legal advice.
- How to fill it in: the period's dates and the tenants on Mieter (Von and Bis for one who moved in or out; the whole
  building's numbers below them), costs on Kosten, amounts by Direkt on Einzelbeträge, then choose each tenant on
  Abrechnung and print.
