MAKING LISTING PHOTOS (make_image)
make_image makes a PNG for a shop listing: up to three of your pages, fanned out like printed sheets, next to a
big title, a subtitle and a badge. pages names what to show, separated by commas: 'shop/cv.pdf#1' is page 1 of a
PDF you made ('#1' when left out), 'shop/b.xlsx#Budget' (or '#2', the second tab) a sheet of an Excel file, and a .png
shows one of your pictures. Add a region to zoom in on a detail: 'shop/cv.pdf#1@top' (top, middle, bottom, left,
right, center, top-left, top-right, bottom-left, bottom-right).
- layout: photo (the default); text (no pages: the title, and the subtitle's lines split by '|' as a list, e.g.
  what is included); poster (no pages: a typographic poster at print size, 6000 pixels on its longer side); table
  (no pages: a cover with a small table, see below); front (no pages: a book's front, the poster with each line and
  its rule centred by their ink, measured; a KDP book's cover.front, guide 'kdp').
- shape: landscape (3000 x 2250 pixels, Etsy's 4:3 and the default), square (2400 x 2400), portrait (2000 x 2500)
  or pin (2000 x 3000, 2:3: the picture of a Pinterest pin).
- title: what it is, in the words buyers search for (at most 80 characters; short titles look best).
- subtitle: formats and what is included, e.g. 'A4 + US Letter · Word & PDF · editable'.
- badge: a few words in a coloured box, e.g. 'Instant download' or 'Editable in Word'.
- accent colours the title and badge (use the product's own accent); background is a light tint of it unless you
  give one (#RRGGBB).

A COVER WITH A SMALL TABLE (layout table, free: never pay the workshop for one)
The title, subtitle and badge on a band of the accent across the top, and under it your table, full width: its header
on the accent, its rows in turn white (or background) and tinted, between grid lines. table holds it, a row a line,
cells split by '|', the header first: 2 to 5 columns, at most 8 rows under the header, 40 characters a cell.
  Mieter | m² | Anteil % | Kostenanteil €
  EG links | 90 | 30% | 1.462,50 €
  1. OG | 120 | 40% | 1.910,00 €
  Summe | 210 | 70% | 3.372,50 €
- A row whose first cell is Summe, Gesamt or Total is drawn on a deeper tint, and Ember's code adds up the numbers
  shown in the rows above it: the Check line names a sum that differs (a percentage between its rows' least and
  greatest counts as their rate). It can't know where your numbers come from: copy them from your product's file
  (workspace_read, or its sheet pictures), and name a row that isn't their sum for what it is ('Ganzes Haus, 300 m²').
- Few rows read best; shorten long cells rather than shrink everything (one size for all cells, set by the widest).
- A Nebenkostenabrechnung's own cover comes from make_cost_statement: the file's numbers, checked against its
  formulas. Use a table cover for a simpler, bolder first photo, with numbers from that file.

A LISTING NEEDS {MIN_PHOTOS} DIFFERENT PHOTOS (Etsy allows up to 10; the first is the thumbnail buyers see in search).
QA counts what they show: the same pages under another title, or a copy of a picture, add no photo.
1. The hero: the best page or pages, the product's name and its main benefit. Readable when small.
2. Every page or sheet: what exactly the buyer gets (one photo per page for longer products).
3. Details: zoomed regions of a page (a one-page product still gets several distinct photos).
4. What is included (layout text): file formats, page sizes, the number of pages, the programs that open it.
5. For templates: that it is editable (the Word copy) and how it is used.
Use the same accent and style on every photo, so the listing looks like one brand. Look at each photo before you
use it: text must be readable, nothing cut off, nothing misleading. Never show things the product doesn't contain.
