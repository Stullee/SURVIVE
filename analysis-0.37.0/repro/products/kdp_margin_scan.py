import sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail
from kdp_checks import book, interior_check
from app.products import make, images
from app.integrations import kdp
jail = fresh_jail("kdp_scan")
# plain text only (no writing lines, no rules): does the coarse check still refuse the guide's 13 mm at 151-160 pages?
def plain_book(margin, pages):
    head = f"---\npage: 6x9\nmargin: {margin}\ntheme: minimal\nfooter: {{page}}\n---\n"
    return head + "# A Book\n\n" + "".join(f"Chapter text for page {n}. More words here.\n\n::: pagebreak\n" for n in range(2, pages)) + "The end.\n"
for margin in (13, 13.5, 14):
    for maker, label in ((book, "journal (lines, rules)"), (None, "plain text, minimal theme")):
        src = book("6x9", margin, "Page {page} of {pages}", 160) if maker else plain_book(margin, 160)
        jail.write("b.md", src)
        make.document(jail, "b.md", "b.pdf", word_copy=False, previews=False)
        res = interior_check(jail, "b.pdf")
        data = jail.read_bytes("b.pdf")
        true_left = images.ink_boxes(data, 600, kdp.INK_LEVEL)[0][0]
        print(f"margin {margin} mm, {label}: {res[0]}; true ink from the inside edge {true_left:.3f} in;",
              "REFUSED: " + res[1][:90] if len(res) > 1 else "passes")
