from app.products import markup, pdf
for words in (960, 975, 990, 1000):
    src = "---\npage: A4\n---\n# Mid\n\n" + "Filler paragraph.\n\n" * 20 + "| Head | B |\n|---|---|\n| short | x |\n| " + ("word " * words) + "| y |\n"
    doc = markup.parse(src)
    r = pdf.Renderer(doc)
    t = r.theme
    table = doc.main[-1]
    from app.products.pdf import Flow, Look
    import math
    # measure each row the way table() does
    left, width = r.canvas.main_band()
    rows = [table.header] + table.rows
    hl = r._header_look(Look(t.text, t.accent, t.accent), t.table)
    minimum = [2 * 1.8 + 4.0] * 2; natural = list(minimum)
    for ri, row in enumerate(rows):
        for c, cell in enumerate(row):
            ws = r.words(cell, t.font, t.size, t.text, bold=ri == 0)
            if ws:
                minimum[c] = max(minimum[c], max(w.width for w in ws) + 3.6)
                natural[c] = max(natural[c], sum(w.width + w.space for w in ws) + 3.6)
    widths = pdf._share(width, minimum, natural)
    heights = [max(len(r.break_lines(r.words(cell, t.font, t.size, t.text, bold=ri == 0), widths[c] - 3.6)) for c, cell in enumerate(row)) * r.body_lh + 2.2 for ri, row in enumerate(rows)]
    area = t.page_height - 2 * t.margin
    print(words, "row heights (mm):", [round(h, 1) for h in heights], "text area", round(area, 1), "area - header", round(area - heights[0], 1),
          "=> fits a fresh page:", heights[2] <= area)
    r.canvas.close()
