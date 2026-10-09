import sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail
from app.products import make, images
jail = fresh_jail("docs_window")
for words in (960, 975, 990, 1000):
    src = "---\npage: A4\n---\n# Mid\n\n" + "Filler paragraph.\n\n" * 20 + "| Head | B |\n|---|---|\n| short | x |\n| " + ("word " * words) + "| y |\n"
    jail.write("d/w.md", src)
    m = make.document(jail, "d/w.md", f"shop/w{words}.pdf", word_copy=False, previews=False)
    boxes = images.ink_boxes(jail.read_bytes(f"shop/w{words}.pdf"), 36, 245)
    print(words, m.report[0], [l for l in m.report if l.startswith("Check")], [round(b[3], 2) for b in boxes if b])
