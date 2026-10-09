import sys
from app.products import images
from app.integrations import kdp
root = "/tmp/ember-repro/products/out/kdp/books/"
for name in ("b160.pdf", "b151.pdf", "m10-f22.pdf", "m12-f22.pdf"):
    data = open(root + name, "rb").read()
    boxes = images.ink_boxes(data, kdp.INK_DPI, kdp.INK_LEVEL)
    print(name, len(boxes))
    for i, b in enumerate(boxes[:3], 1):
        print("  page", i, tuple(round(v, 4) for v in b), "right room", round(6 - b[2], 4), "bottom room", round(9 - b[3], 4))
    # high resolution: the true ink box of page 1
    hi = images.ink_boxes(data, 600, kdp.INK_LEVEL)
    print("  600 dpi page 1", tuple(round(v, 4) for v in hi[0]), "page 2", tuple(round(v, 4) for v in hi[1]))
