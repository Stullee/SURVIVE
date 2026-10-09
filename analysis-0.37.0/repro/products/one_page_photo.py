import sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail
from PIL import Image
from app.products import make
jail = fresh_jail("one_page")
jail.write("d/cv.md", "---\ntheme: modern\n---\n# Jane Doe\n\nA CV.\n")
make.document(jail, "d/cv.md", "shop/cv.pdf")
make.image(jail, "shop/one.png", "shop/cv.pdf#1", "Modern CV", "Word & PDF", "Editable")
im = Image.open(jail.root / "shop" / "one.png").convert("RGB")
# crop the bottom-right corner of the page and its shadow
print(im.size)
im.crop((1900, 1700, 2900, 2250)).save(jail.root / "shop" / "one-corner.png")
# pixel rows below the sheet: is the shadow edge hard?
for y in range(2080, 2140, 4):
    print(y, [im.getpixel((x, y)) for x in (1500, 2000, 2400)])
