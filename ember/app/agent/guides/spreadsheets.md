MAKING SPREADSHEETS (make_spreadsheet)
Write a JSON spec as a .json file in your workspace, then call make_spreadsheet with that source and an output
ending in .xlsx. You get the Excel file (it opens in Excel, Google Sheets, Numbers and LibreOffice) and a picture
of each sheet (name-preview.png, name-sheet2.png, ...) that shows the formulas' results. Look at them before you sell
it. workspace_read shows any Excel file's cells (formulas with their results); make_image shows a sheet by name,
'b.xlsx#Budget' ('#2' counts the tabs, and "How to use" is the first).

{
  "title": "Monthly Budget Planner",
  "theme": {"accent": "#2E7D5B", "font": "sans"},
  "notes": ["Type your planned amounts in column B.", "Enter what you spent in column C."],
  "sheets": [{
    "name": "Budget", "title": "Monthly budget",
    "columns": [
      {"title": "Category", "width": 24, "choices": ["Housing", "Food", "Transport"]},
      {"title": "Planned", "format": "eur"},
      {"title": "Actual", "format": "eur"},
      {"title": "Left", "format": "eur", "formula": "=B{row}-C{row}"}
    ],
    "rows": [["Housing", 950, 950], ["Food", 400, 436.5]],
    "empty_rows": 20,
    "totals": {"Planned": "sum", "Actual": "sum", "Left": "sum"},
    "chart": {"type": "bar", "labels": "Category", "values": "Actual", "title": "Where the money went"}
  }]
}

- notes become a first sheet "How to use" (at most 30 lines). Up to 8 sheets, 26 columns and 2,000 rows each.
- columns: title; width 4-80 (default 14); format: text, number, integer, eur, usd, percent, date (2026-09-28),
  date_de, date_us or general; choices: a dropdown list (no commas or quotes); formula: filled into every row
  where you leave that cell empty, and into the empty rows.
- rows: lists of values (text, numbers, true/false, null), or "rows_csv": "data/rows.csv" for a CSV file you wrote.
  empty_rows: rows left empty for the buyer to fill in (0-1000).
- A text starting with "=" is a formula. {row} is the formula's own row, {first} and {last} the first and last
  data rows, e.g. "=SUM(B{first}:B{last})". Rows are numbered like Excel: the header is row 1, or row 3 under a
  title; data starts on row 2, or row 4 under a title. Formulas may use common functions (SUM, AVERAGE, IF,
  IFERROR, VLOOKUP, XLOOKUP, SUMIF, COUNTIF, ROUND, TODAY, DATE, TEXT and similar) and cells of this workbook
  ('Other sheet'!A1); links to other files or the web are refused.
- totals: a bold row under the data with sum, average, count, min or max per column.
- freeze (the header stays visible), filter (filter buttons) and zebra (striped rows) are on unless set false.
- chart: bar, line or pie of one column (values) by another (labels), placed right of the table.

WHAT SELLS: trackers and planners that save time (budget, debt payoff, savings goals, habit, meal, wedding,
small business bookkeeping, invoices), with dropdowns, automatic totals and charts, clear instructions, and
realistic sample rows the buyer deletes. Say in the notes that it was made with AI help.
