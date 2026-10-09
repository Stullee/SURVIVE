import sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail
from app.products import make, images
jail = fresh_jail("docs_more")
# a table row taller than a page
tall = "---\npage: A4\n---\n# Tall\n\nIntro.\n\n| A | B |\n|---|---|\n| " + ("word " * 2600) + "| x |\n\nAfter the table.\n"
jail.write("d/tall.md", tall)
m = make.document(jail, "d/tall.md", "shop/tall.pdf", word_copy=False)
print(m.text())
boxes = images.ink_boxes(jail.read_bytes("shop/tall.pdf"), 36, 245)
print("ink boxes (in):", [tuple(round(v, 2) for v in b) if b else None for b in boxes], "page height 11.69")
# a row a little taller than a page minus its header, on a page that already has text
mid = "---\npage: A4\n---\n# Mid\n\n" + "Filler paragraph.\n\n" * 20 + "| Head | B |\n|---|---|\n| short | x |\n| " + ("word " * 1720) + "| y |\n"
jail.write("d/mid.md", mid)
m = make.document(jail, "d/mid.md", "shop/mid.pdf", word_copy=False)
print(m.text())
boxes = images.ink_boxes(jail.read_bytes("shop/mid.pdf"), 36, 245)
print("ink boxes (in):", [tuple(round(v, 2) for v in b) if b else None for b in boxes])
# make_image odds and ends
for args in (dict(pages="shop/tall-page1.png#@top"), dict(pages="", layout="text", title="Done ✓")):
    try:
        r = make.image(jail, "shop/x.png", args.get("pages", ""), args.get("title", "T"), layout=args.get("layout", "photo"))
        print("ok:", r.report[0])
    except make.ProductError as exc:
        print("refused:", exc)
