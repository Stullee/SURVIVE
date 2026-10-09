"""KDP interiors and covers, made with the real code and checked as propose_kdp_book checks them."""

from __future__ import annotations

import sys
from decimal import Decimal

sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail  # noqa: E402
from PIL import Image  # noqa: E402

from app.integrations import kdp  # noqa: E402
from app.products import images, make  # noqa: E402


def book(page: str, margin: float | None, footer: str, pages: int) -> str:
    head = ["---", f"page: {page}"]
    if margin is not None:
        head.append(f"margin: {margin}")
    if footer:
        head.append(f"footer: {footer}")
    head.append("---")
    body = "# A Journal\n\nThis book was made with the help of AI.\n\n"
    body += "".join(f"## Day {n}\n\nWhat went well today?\n\n::: lines 6\n::: pagebreak\n" for n in range(2, pages))
    body += "## The end\n\nThank you.\n"
    return "\n".join(head) + "\n" + body


def interior_check(jail, path: str, paper: str = "white") -> list[str]:
    data = jail.read_bytes(path)
    sizes = images.page_sizes(data)
    found = kdp.trim_of(sizes[0])
    if found is None:
        return [f"no trim: {sizes[0]}"]
    trim = found[0]
    interior = kdp.check_interior(sizes, lambda: images.ink_boxes(data, kdp.INK_DPI, kdp.INK_LEVEL), trim, paper)
    return [f"trim {trim}, {interior.pages} pages, bleed {interior.bleed}", *interior.findings]


def main() -> None:
    jail = fresh_jail("kdp")
    print("=== 1. the guide's interior: page 6x9, margin 10 mm, page numbers in the footer ===")
    for margin, footer in ((10, "Page {page} of {pages}"), (10, "{page}"), (11, "{page}"), (12, "Page {page} of {pages}"),
                           (13, "Page {page} of {pages}"), (10, "")):
        name = f"m{margin}-{'f' if footer else 'nf'}{len(footer)}"
        jail.write(f"books/{name}.md", book("6x9", margin, footer, 30))
        made = make.document(jail, f"books/{name}.md", f"books/{name}.pdf", word_copy=False, previews=False)
        result = interior_check(jail, f"books/{name}.pdf")
        print(f"margin {margin} mm, footer {footer!r}: {made.report[0]}")
        for line in result:
            print("   ", line)

    print("\n=== 2. A4 and Letter are KDP sizes: a 45-page book ===")
    for page in ("A4", "8.27x11.69", "Letter", "8.5x11"):
        jail.write(f"books/big-{page}.md", book(page, 15, "{page}", 45))
        try:
            made = make.document(jail, f"books/big-{page}.md", f"books/big-{page}.pdf", word_copy=False, previews=False)
            print(f"page {page}: {made.report[0]}  ->", interior_check(jail, f"books/big-{page}.pdf")[0])
        except Exception as exc:  # noqa: BLE001
            print(f"page {page}: REFUSED: {type(exc).__name__}: {exc}")
    jail.write("books/a4-30.md", book("A4", 15, "{page}", 30))
    make.document(jail, "books/a4-30.md", "books/a4-30.pdf", word_copy=False, previews=False)
    print("an A4 document of 30 pages is KDP trim:", interior_check(jail, "books/a4-30.pdf")[0])

    print("\n=== 3. the full-wrap cover: width = 2 x trim + spine + 2 x bleed ===")
    jail.write("books/front.md", "x")
    made = make.image(jail, "books/front.png", "", "A Journal", "For calm days | by Someone", layout="poster", shape="pin")
    print(made.report[0])
    for pages, paper in ((30, "white"), (151, "cream"), (160, "color")):
        name = f"b{pages}"
        jail.write(f"books/{name}.md", book("6x9", 13, "{page}", pages))
        doc = make.document(jail, f"books/{name}.md", f"books/{name}.pdf", word_copy=False, previews=False)
        spine_text = "A Journal · Someone" if pages > 79 else ""
        made = make.kdp_cover(jail, f"books/{name}-cover.pdf", "books/front.png", f"books/{name}.pdf", paper,
                              "The blurb. | A second paragraph.", spine_text)
        data = jail.read_bytes(f"books/{name}-cover.pdf")
        size = images.page_sizes(data)[0]
        pages_real = images.page_count(jail.read_bytes(f"books/{name}.pdf"))
        by_hand = 2 * Decimal(6) + Decimal(pages_real + pages_real % 2) * kdp.SPINE_PER_PAGE[paper] + 2 * Decimal("0.125")
        print(f"{doc.report[0]}")
        print(f"  cover {size[0] / 72:.4f} x {size[1] / 72:.4f} in; by hand {by_hand:.4f} x 9.25 in;"
              f" check_cover -> {kdp.check_cover(size, pages_real, '6x9', paper) or 'ok'}")
        print("  ", made.report[0])
        print("  ", interior_check(jail, f"books/{name}.pdf", paper))
        pic = Image.open(jail.root / "books" / f"{name}-cover-preview.png")
        print("   preview", pic.size)

    print("\n=== 4. the ebook cover ===")
    made = make.kdp_cover(jail, "books/e-cover.jpg", "books/front.png")
    data = jail.read_bytes("books/e-cover.jpg")
    w, h = images.png_size(data)
    print(made.report, (w, h), kdp.check_ebook_cover(data, w, h, "books/e-cover.jpg") or "ok")


if __name__ == "__main__":
    main()
