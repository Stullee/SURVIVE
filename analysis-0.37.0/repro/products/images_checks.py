"""make_image and resize_image with the real code: sizes, layouts, what is cut or dropped."""

from __future__ import annotations

import io
import sys

sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app.products import images, make  # noqa: E402

LONG_LINE = ("Everything included in this bundle: an editable Word CV, a cover letter, a reference page, "
             "and a PDF guide")  # 104 characters, one item
TEN = "|".join(f"Item number {n:02d}" for n in range(1, 11))  # 10 items, 159 characters


def size_of(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def main() -> None:
    jail = fresh_jail("images")
    # pages to show: a document
    jail.write("d/cv.md", "---\ntheme: modern\n---\n# Jane Doe\n\nA CV.\n\n::: pagebreak\n## Page two\n\nMore.\n")
    make.document(jail, "d/cv.md", "shop/cv.pdf")
    for shape in images.SHAPES:
        made = make.image(jail, f"shop/photo-{shape}.png", "shop/cv.pdf#1, shop/cv.pdf#2, shop/cv-page1.png",
                          "Modern CV Template for Word and Google Docs, A4 and US Letter, Instant Download",
                          "A4 + US Letter · Word & PDF · editable|Cover letter included|Free fonts",
                          "Instant download", shape=shape)
        print(made.report[0], size_of(jail.read_bytes(f"shop/photo-{shape}.png")))
    # text photos: what is drawn of the lines
    for name, subtitle in (("long", LONG_LINE), ("ten", TEN)):
        for shape in ("landscape", "square"):
            out = f"shop/text-{name}-{shape}.png"
            made = make.image(jail, out, "", "What is included", subtitle, "Instant download", shape=shape,
                              layout="text")
            print(made.report[0])
    # posters
    for shape in images.SHAPES:
        made = make.image(jail, f"shop/poster-{shape}.png", "", "Bauhaus Print", "Weimar 1919|Form follows function",
                          shape=shape, layout="poster")
        print(made.report[0], size_of(jail.read_bytes(f"shop/poster-{shape}.png")))
    # resize: A3 at 300 dpi from a landscape photo (cut), and an upscale
    for src, w, h in (("shop/poster-portrait.png", 3508, 4961), ("shop/photo-landscape.png", 3508, 4961),
                      ("shop/cv-page1.png", 3508, 4961), ("shop/poster-square.png", 2480, 3508)):
        out = f"shop/print-{w}x{h}-{src.split('/')[-1]}"
        made = make.resize(jail, src, out, w, h)
        data = jail.read_bytes(out)
        im = Image.open(io.BytesIO(data))
        print(made.report[0], "| file:", im.size, im.info.get("dpi"))


if __name__ == "__main__":
    main()
