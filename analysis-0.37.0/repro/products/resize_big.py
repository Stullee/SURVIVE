import io, sys
sys.path.insert(0, "../analysis-0.37.0/repro/products")
from helpers import fresh_jail
from PIL import Image, ImageFilter, ImageChops
from app.products import make
jail = fresh_jail("resize_big")
# a photo-like picture: smooth colour fields and fine grain (as a camera or a painterly render makes)
w, h = 2480, 3508
base = Image.effect_noise((w // 40, h // 40), 90).resize((w, h), Image.Resampling.BICUBIC)
r = base; g = base.transpose(Image.Transpose.FLIP_LEFT_RIGHT); b = base.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
photo = Image.merge("RGB", (r, g, b))
grain = Image.merge("RGB", [Image.effect_noise((w, h), s) for s in (6, 6, 6)])
photo = ImageChops.add(photo, grain, scale=1.0, offset=-128)
buf = io.BytesIO(); photo.save(buf, "JPEG", quality=90); jpg = buf.getvalue()
jail.write_bytes("art/photo.jpg", jpg)
print("source JPEG", len(jpg) // 1024, "KB", photo.size)
for size in ((3508, 4961), (2480, 3508)):
    try:
        m = make.resize(jail, "art/photo.jpg", f"shop/print-{size[0]}.png", *size)
        print(m.report[0])
    except Exception as exc:
        print(size, "->", type(exc).__name__, exc)
