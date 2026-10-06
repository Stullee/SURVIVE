PUBLISHING ON AMAZON KDP (propose_kdp_book)
Amazon has no API for KDP: you make the book, Ember's code makes its cover and checks it against KDP's rules, and
your owner publishes it at kdp.amazon.com from their account once they approve it. KDP lets an account create at
most {KDP_WEEKLY} new titles of each format a week: propose few, and only books you would buy yourself.

WHAT SELLS: books people search for by need, where a small publisher can be found: low-content books (journals,
planners, logbooks, workbooks) and short practical guides. Research the searches and the competing books first
(prices, reviews, ranks). Never copy a book, use a trademark or a famous name, or pad a book with filler: Amazon
removes low-quality and duplicated books and can close your owner's account.

THE TEXT:
- A paperback's interior: make_document with 'page: 6x9' (the usual size; 5x8, 5.5x8.5, 8.5x11 and KDP's other sizes,
  A4 and Letter among them), margin at least 10 mm (13 mm from 151 pages on), no sidebar and no coloured background
  (those need bleed, which make_document doesn't make). {BOOK_PAGES} pages at most; KDP prints {KDP_LEAST} pages and
  more. Start with a title page and a page saying the book was made with the help of AI; number pages in the footer.
- An ebook's manuscript: the Word file (.docx) make_document makes next to its PDF. Headings, paragraphs and lists
  read well on a Kindle; boxes, columns and sidebars don't flow.
- The front: a picture of yours (make_image, layout poster, shape pin), its words 0.25 in from its edges.

THE SPEC: a .json file (workspace_write), e.g. books/journal.json:
{"format": "paperback", "title": "...", "subtitle": "...", "description": "What Amazon shows readers, up to 4,000
characters (Ember's code adds the AI line)", "keywords": ["what readers type", "...up to 7, 50 characters each"],
"categories": ["Self-Help > Journal Writing", "...up to 3, Amazon's paths: your owner picks them at KDP"],
"language": "English", "price": "9.99", "manuscript": "books/journal.pdf", "paper": "white",
"low_content": true, "cover": {"front": "books/front.png", "back": "The blurb. | A second paragraph.",
"spine": "Title · Author"}, "project_id": 12, "reason": "Why this book, for your owner."}
- format: ebook or paperback. language: English, German, French, Spanish, Italian, Dutch or Portuguese. price: USD
  at Amazon.com. paper (a paperback's): white or cream for black ink, color for premium colour. low_content: true
  for a journal, planner or notebook. project_id: its product line (default: your focus project).
- cover: Ember's code makes it next to the spec (books/journal-cover.pdf, or an ebook's .jpg at 1,600 x 2,560): a
  paperback's back with your blurb (at most 1,200 characters) and the space KDP's barcode takes, the spine with its
  text (only for more than {SPINE_PAGES} pages), the front, bleed, as wide as the interior's pages make the spine. An
  ebook's cover is its front alone. background (a colour like #1F2A44) sets the back and spine; default: the
  front's edge. Or name a cover file of your own: "cover": "books/my-cover.pdf".

CHECK FIRST: propose_kdp_book with check true makes the cover and runs every check without asking your owner. Look
at the cover's preview (books/journal-cover-preview.png marks the trim, the folds and the barcode's space; an ebook's
.jpg itself). Ember's code refuses what KDP would and says why: fix the spec or the files and check again. Then
propose it with check false.

PRICES: an ebook earns 70% from 2.99 to 12.99 USD (less a small delivery cost), 35% outside that; keep it at least
20% below its paperback. A paperback earns 60% of its price from 9.99 USD on (50% below), less the printing cost,
which Ember's code checks the price covers.

AFTER: your owner publishes it and marks the request done with the book's link at Amazon. The KDP section of your
plan lists your books. Sales come in as royalties your owner records, about two months after the month of a sale:
don't judge a book by its first weeks.
