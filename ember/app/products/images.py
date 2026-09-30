"""Pictures of products: page previews of a PDF, listing photos that show pages with a title, text photos and posters.

pypdfium2 draws PDF pages (it loads its native library through ctypes, which a sealed thread may not do, so this
module is imported at startup, before any tool runs; see app.agent.tools). Pillow composes listing photos from
Ember's own pages and previews only: no image the agent didn't make ever reaches Pillow.

0.14.0: one limit for every picture Ember's code reads (MAX_PIXELS, the workshop's check included): at 12 MP a
print-size poster (3510 x 4950 = 17.4 MP for A3 at Printify) couldn't be proposed, looked at or read, and the refusal
hid why. A picture is reduced while it is decoded where it can be (a JPEG at 1/2 to 1/8 of its size), so a large one
doesn't take its full size in memory twice. make_image zooms in on a region of a page (REGIONS), makes text photos
and posters at print size, and notes in each photo what it shows (``marked``). With that and a difference hash of its
pixels (``look``), the QA registry counts distinct photos, not copies.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from collections.abc import Callable, Sequence

import pypdfium2
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import fonts
from .theme import RGB, contrast, hex_rgb, readable_on, tint

PREVIEW_DPI = 150
SHAPES = {"landscape": (3000, 2250), "square": (2400, 2400), "portrait": (2000, 2500), "pin": (2000, 3000)}
SHAPE_NAMES = tuple(SHAPES)
MAX_PIXELS = 40_000_000  # 0.14.0: 12 MP until then
LAYOUTS = ("photo", "text", "poster")
POSTER_SIDE = 6_000  # a poster's longer side: 150 dpi or more on every poster Printify prints (A1, 24 x 36 in)
# A region of a page or picture make_image zooms in on ('shop/cv.pdf#1@top'): left, top, right, bottom, as fractions.
REGIONS = {
    "top": (0.0, 0.0, 1.0, 0.45),
    "middle": (0.0, 0.275, 1.0, 0.725),
    "bottom": (0.0, 0.55, 1.0, 1.0),
    "left": (0.0, 0.0, 0.55, 1.0),
    "right": (0.45, 0.0, 1.0, 1.0),
    "center": (0.2, 0.2, 0.8, 0.8),
    "top-left": (0.0, 0.0, 0.55, 0.55),
    "top-right": (0.45, 0.0, 1.0, 0.55),
    "bottom-left": (0.0, 0.45, 0.55, 1.0),
    "bottom-right": (0.45, 0.45, 1.0, 1.0),
}
FULL = (0.0, 0.0, 1.0, 1.0)
LOOK_SIZE = 16  # a picture's difference hash (look) compares LOOK_SIZE x LOOK_SIZE cells across and down
LOOK_BITS = 2 * LOOK_SIZE * LOOK_SIZE
MARK = "ember-shows"  # a PNG text chunk: a hash of what a photo make_image made shows (its pages or lines)
_LOOKS: dict[str, str] = {}  # looks by the picture's SHA-256 (a request card shows the same photos again and again)
# Pillow's own guard against decompression bombs stays above the limit: it warns above Image.MAX_IMAGE_PIXELS (89 MP)
# and refuses twice that, so it never refuses a picture within MAX_PIXELS (tests/test_fixes_0140_factory.py).


class ImageError(ValueError):
    pass


def page_count(pdf: bytes) -> int:
    document = pypdfium2.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def pdf_pages(
    pdf: bytes,
    numbers: list[int],
    height: int | None = None,
    dpi: int = PREVIEW_DPI,
    region: tuple[float, float, float, float] = FULL,
) -> list[Image.Image]:
    """Pages of a PDF as images, at ``dpi`` or scaled to ``height`` pixels; with a region (0.14.0), only that part of
    each page, drawn at ``height`` pixels itself."""
    left, top, right, bottom = region
    document = pypdfium2.PdfDocument(pdf)
    try:
        images = []
        for number in numbers:
            if not 1 <= number <= len(document):
                raise ImageError(f"the PDF has {len(document)} page(s), not a page {number}")
            page = document[number - 1]
            try:
                width, tall = page.get_width(), page.get_height()
                scale = (height / (tall * (bottom - top))) if height else dpi / 72
                crop = (left * width, (1 - bottom) * tall, (1 - right) * width, top * tall)
                images.append(page.render(scale=scale, crop=crop).to_pil().convert("RGB"))
            finally:
                page.close()
        return images
    finally:
        document.close()


def pdf_active(pdf: bytes) -> list[str]:
    """0.14.0: what pdfium, Chrome's PDF engine, finds in a PDF that acts on its own: JavaScript, embedded files, XFA
    forms. The workshop's check asks it too, after its own search of the file's bytes."""
    document = pypdfium2.PdfDocument(pdf)
    try:
        counts = {
            "JavaScript": pdfium_c.FPDFDoc_GetJavaScriptActionCount(document.raw),
            "embedded files": pdfium_c.FPDFDoc_GetAttachmentCount(document.raw),
            "XFA": pdfium_c.FPDF_GetXFAPacketCount(document.raw),
        }
    finally:
        document.close()
    return [name for name, count in counts.items() if count > 0]


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def open_png(data: bytes, longest: int | None = None) -> Image.Image:
    """One of Ember's own pictures (PNG or JPEG), checked before it is decoded; with ``longest``, no wider or higher
    than that."""
    return _reduced(data, longest) if longest else _decoded(lambda: _checked(data).convert("RGB"))


def png_size(data: bytes) -> tuple[int, int]:
    """The width and height of one of Ember's pictures (PNG or JPEG)."""
    image = _checked(data)
    return image.width, image.height


def thumbnail(data: bytes, longest: int) -> tuple[bytes, int, int]:
    """A PNG no wider or higher than ``longest`` pixels (for the model to look at) of a PNG or JPEG, and its
    size."""
    image = _reduced(data, longest)
    return png(image), image.width, image.height


def cropped(image: Image.Image, where: tuple[float, float, float, float]) -> Image.Image:
    """0.14.0: the part of a picture a region names (REGIONS)."""
    left, top, right, bottom = where
    w, h = image.size
    return image.crop((round(left * w), round(top * h), max(round(right * w), 1), max(round(bottom * h), 1)))


def marked(data: bytes, shows: str) -> bytes:
    """0.14.0: a PNG with a note of what it shows (a hash of ``shows``, no file names) after its header: two photos of
    the same page with other words on them are the same photo to a buyer."""
    chunk = b"tEXt" + MARK.encode() + b"\0" + hashlib.sha256(shows.encode()).hexdigest()[:16].encode()
    return data[:33] + struct.pack(">I", len(chunk) - 4) + chunk + struct.pack(">I", zlib.crc32(chunk)) + data[33:]


def look(data: bytes) -> str:
    """0.14.0: what a picture looks like, as 'mark.hash': what make_image noted it shows (``marked``; empty if none),
    and its difference hash in hex: for each of LOOK_SIZE x LOOK_SIZE cells of it in grey, whether it is brighter than
    the next one to its right, and than the next one below it. A copy (resized, re-encoded, another colour or badge)
    has nearly the same bits; the same layout with other pages or words doesn't."""
    image = _checked(data)
    mark = str(image.info.get(MARK, "")) if image.format == "PNG" else ""
    grey = _reduced(data, 512).convert("L")
    across = grey.resize((LOOK_SIZE + 1, LOOK_SIZE), Image.Resampling.BOX).tobytes()
    down = grey.resize((LOOK_SIZE, LOOK_SIZE + 1), Image.Resampling.BOX).tobytes()
    bits = 0
    for row in range(LOOK_SIZE):
        for column in range(LOOK_SIZE):
            at = row * (LOOK_SIZE + 1) + column
            bits = bits << 1 | (across[at] > across[at + 1])
    for at in range(LOOK_SIZE * LOOK_SIZE):
        bits = bits << 1 | (down[at] > down[at + LOOK_SIZE])
    return f"{mark}.{bits:0{LOOK_BITS // 4}x}"


def looks(read: Callable[[str], bytes], photos: Sequence[tuple[str, str]]) -> list[str]:
    """0.14.0: the look of each (path, SHA-256) photo, for the QA registry; "" where the file is gone, has changed or
    can't be read."""
    found = []
    for path, sha256 in photos:
        if sha256 not in _LOOKS:
            try:
                data = read(path)
            except (OSError, ValueError):  # the workspace's own errors are ValueErrors
                found.append("")
                continue
            if hashlib.sha256(data).hexdigest() != sha256:
                found.append("")
                continue
            try:
                _LOOKS[sha256] = look(data)
            except ImageError:
                _LOOKS[sha256] = ""
            if len(_LOOKS) > 1_000:
                _LOOKS.pop(next(iter(_LOOKS)))
        found.append(_LOOKS[sha256])
    return found


def _reduced(data: bytes, longest: int) -> Image.Image:
    """0.14.0: a picture no wider or higher than ``longest``, in RGB. A JPEG is decoded at 1/2 to 1/8 of its size
    where that is enough, and a picture is reduced before it is converted."""
    image = _checked(data)

    def reduce() -> Image.Image:
        image.draft("RGB", (longest, longest))
        shown = image if image.mode in ("RGB", "L", "RGBA", "LA") else image.convert("RGB")  # (a palette: nearest)
        shown.thumbnail((longest, longest), Image.Resampling.LANCZOS, reducing_gap=3.0)
        return shown.convert("RGB")

    return _decoded(reduce)


def _decoded(decode: Callable[[], Image.Image]) -> Image.Image:
    """What ``decode`` makes of a picture; a picture that breaks the decoder (cut short, damaged) is an ImageError."""
    try:
        return decode()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, ImageError):
            raise
        raise ImageError("it can't be read whole (damaged or cut short)") from None


def too_large(width: int, height: int) -> str:
    """0.14.0: why a picture this size isn't read, in numbers ("" when it is read)."""
    if width * height <= MAX_PIXELS:
        return ""
    return f"{width} x {height} = {width * height / 1_000_000:.1f} MP, more than {MAX_PIXELS // 1_000_000} MP"


def _checked(data: bytes) -> Image.Image:
    """A PNG or JPEG, opened (not decoded yet). 0.14.0: a picture too large says its size (it said it wasn't one of
    Ember's pictures)."""
    try:
        image = Image.open(io.BytesIO(data), formats=("PNG", "JPEG"))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError(f"it has far more than {MAX_PIXELS // 1_000_000} MP (Pillow refused to read it)") from None
    except OSError:  # 0.13.0: not a picture at all (a pin's image is checked too)
        raise ImageError("it isn't a PNG or JPEG picture Ember's code can read") from None
    if too_large(image.width, image.height):
        raise ImageError(f"it is {too_large(image.width, image.height)}")
    return image


def _font(family: str, style: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(fonts.path(family, style)), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str, family: str, style: str, start: int, width: int, max_lines: int):  # noqa: ANN202
    """The largest font (from ``start`` down) at which ``text`` wraps into ``max_lines`` lines of ``width``."""
    size = start
    while size > 24:
        font = _font(family, style, size)
        lines = _wrap(draw, text, font, width)
        if len(lines) <= max_lines and all(draw.textlength(line, font=font) <= width for line in lines):
            return font, lines
        size = int(size * 0.92)
    font = _font(family, style, size)
    return font, _wrap(draw, text, font, width)[:max_lines]


def _sheet(page: Image.Image, height: int, width: int, angle: float) -> tuple[Image.Image, Image.Image]:
    """A page as a sheet of paper: scaled to fit ``width`` x ``height``, with a hairline edge, rotated; and its soft
    shadow."""
    scale = min(height / page.height, width / page.width)
    size = (max(1, int(page.width * scale)), max(1, int(page.height * scale)))
    sheet = page.resize(size, Image.Resampling.LANCZOS)
    ImageDraw.Draw(sheet).rectangle([0, 0, sheet.width - 1, sheet.height - 1], outline=(220, 220, 220), width=2)
    rgba = sheet.convert("RGBA").rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    shadow = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    alpha = rgba.getchannel("A").point(lambda a: 70 if a else 0)
    shadow.putalpha(alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    return rgba, shadow


def _fan(pages: list[Image.Image], box_w: int, box_h: int) -> list[tuple[Image.Image, Image.Image, float, float]]:
    """The sheets fanned out from the middle of a box, shrunk until the whole fan fits: (sheet, shadow, x, y) with
    x and y relative to the box's centre."""
    count = len(pages)
    angles = {1: [0.0], 2: [-5.0, 4.0], 3: [-6.0, 0.0, 6.0]}[count]
    height = box_h * (0.92 if count == 1 else 0.8)
    width = box_w * (0.92 if count == 1 else 0.6)
    step = box_w * (0.22 if count == 3 else 0.3)
    for _ in range(3):
        sheets = [_sheet(page, int(height), int(width), angle) for page, angle in zip(pages, angles, strict=True)]
        offsets = [(index - (count - 1) / 2) * step for index in range(count)]
        left = min(o - s.width / 2 for o, (s, _) in zip(offsets, sheets, strict=True))
        right = max(o + s.width / 2 for o, (s, _) in zip(offsets, sheets, strict=True))
        tall = max(s.height + abs(o) * 0.16 for o, (s, _) in zip(offsets, sheets, strict=True))
        factor = min(1.0, box_w / (right - left), box_h / tall)
        if factor >= 0.99:
            break
        height, width, step = height * factor * 0.98, width * factor * 0.98, step * factor * 0.98
    middle = (left + right) / 2
    return [
        (sheet, shadow, o - middle - sheet.width / 2, abs(o) * 0.08 - sheet.height / 2)
        for o, (sheet, shadow) in zip(offsets, sheets, strict=True)
    ]


def listing(
    pages: list[Image.Image],
    title: str,
    subtitle: str = "",
    badge: str = "",
    background: str | None = None,
    accent: str | None = None,
    shape: str = "landscape",
) -> bytes:
    """A listing photo: up to three pages fanned out next to (or under) a title, a subtitle and a badge."""
    if shape not in SHAPES:
        raise ImageError(f"shape must be one of {', '.join(SHAPES)}")
    if not 1 <= len(pages) <= 3:
        raise ImageError("show one to three pages")
    width, height = SHAPES[shape]
    accent_rgb: RGB = hex_rgb(accent) if accent else (44, 62, 80)
    bg: RGB = hex_rgb(background) if background else tint(accent_rgb, 0.88)
    ink = readable_on(bg)
    canvas = Image.new("RGBA", (width, height), (*bg, 255))
    draw = ImageDraw.Draw(canvas)
    margin = int(width * 0.06)
    if shape == "landscape":
        text_box = (margin, margin, int(width * 0.42), height - margin)
        page_box = (int(width * 0.44), margin, width - margin // 2, height - margin)
    elif shape == "square":
        text_box = (margin, margin, width - margin, int(height * 0.34))
        page_box = (margin, int(height * 0.36), width - margin, height - margin)
    else:
        text_box = (margin, margin, width - margin, int(height * 0.3))
        page_box = (margin, int(height * 0.32), width - margin, height - margin)
    # Pages: fanned out from the middle of their box.
    box_w, box_h = page_box[2] - page_box[0], page_box[3] - page_box[1]
    centre_x = page_box[0] + box_w / 2
    centre_y = page_box[1] + box_h / 2
    for sheet, shadow, dx, dy in _fan(pages, box_w, box_h):
        x, y = int(centre_x + dx), int(centre_y + dy)
        canvas.alpha_composite(shadow, (x + 18, y + 28))
        canvas.alpha_composite(sheet, (x, y))
    # Text: title, subtitle, badge, top to bottom in their box.
    tw = text_box[2] - text_box[0]
    th = text_box[3] - text_box[1]
    start = int(th * (0.16 if shape == "landscape" else 0.3))
    while True:  # the largest title at which title, subtitle and badge fit their box together
        title_font, title_lines = _fit(draw, title, "display", "B", start, tw, 4)
        sub_font, sub_lines = (None, [])
        if subtitle:
            sub_font, sub_lines = _fit(draw, subtitle, "sans", "", int(title_font.size * 0.42), tw, 4)
        badge_font = _font("sans", "B", max(28, int(title_font.size * 0.33))) if badge else None
        heights = [title_font.size * 1.12 * len(title_lines)]
        if sub_lines and sub_font is not None:
            heights.append(sub_font.size * 0.6 + sub_font.size * 1.3 * len(sub_lines))
        if badge_font is not None:
            heights.append(badge_font.size * 0.9 + badge_font.size * 2.1)
        if sum(heights) <= th or start <= 40:
            break
        start = int(start * 0.9)
    y = text_box[1] + max(0, (th - sum(heights)) / (2 if shape == "landscape" else 3))
    for line in title_lines:
        draw.text((text_box[0], y), line, font=title_font, fill=accent_rgb if _visible(accent_rgb, bg) else ink)
        y += title_font.size * 1.12
    if sub_lines and sub_font is not None:
        y += sub_font.size * 0.6
        for line in sub_lines:
            draw.text((text_box[0], y), line, font=sub_font, fill=ink)
            y += sub_font.size * 1.3
    if badge and badge_font is not None:
        y += badge_font.size * 0.9
        pad_x, pad_y = int(badge_font.size * 0.8), int(badge_font.size * 0.45)
        text_w = draw.textlength(badge, font=badge_font)
        box = (text_box[0], int(y), int(text_box[0] + text_w + 2 * pad_x), int(y + badge_font.size + 2 * pad_y))
        draw.rounded_rectangle(box, radius=int(badge_font.size * 0.6), fill=accent_rgb)
        draw.text((box[0] + pad_x, box[1] + pad_y - badge_font.size * 0.08), badge, font=badge_font,
                  fill=readable_on(accent_rgb))  # fmt: skip
    return png(canvas.convert("RGB"))


def _visible(color: RGB, background: RGB) -> bool:
    return contrast(color, background) >= 2.5


def _colours(background: str | None, accent: str | None) -> tuple[RGB, RGB, RGB]:
    """Accent, background and ink: the accent's light tint when no background is given, and ink readable on it."""
    accent_rgb: RGB = hex_rgb(accent) if accent else (44, 62, 80)
    bg: RGB = hex_rgb(background) if background else tint(accent_rgb, 0.88)
    return accent_rgb, bg, readable_on(bg)


def text_photo(
    title: str,
    lines: list[str],
    badge: str = "",
    background: str | None = None,
    accent: str | None = None,
    shape: str = "landscape",
) -> bytes:
    """0.14.0: a listing photo of words alone (what is included, the features): a title, its lines as a list, and a
    badge."""
    if shape not in SHAPES:
        raise ImageError(f"shape must be one of {', '.join(SHAPES)}")
    width, height = SHAPES[shape]
    accent_rgb, bg, ink = _colours(background, accent)
    canvas = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(canvas)
    margin = int(min(width, height) * 0.08)
    box_w = width - 2 * margin
    title_font, title_lines = _fit(draw, title, "display", "B", int(height * 0.1), box_w, 3)
    y = margin
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill=accent_rgb if _visible(accent_rgb, bg) else ink)
        y += int(title_font.size * 1.12)
    y += int(title_font.size * 0.3)
    draw.rectangle([margin, y, margin + int(box_w * 0.18), y + max(6, height // 180)], fill=accent_rgb)
    y += int(title_font.size * 0.6)
    room = height - margin - y - (int(height * 0.12) if badge else 0)
    size = int(min(height * 0.06, room / max(1, len(lines)) / 1.5))
    font = _font("sans", "", max(24, size))
    bullet = max(8, font.size // 3)
    for line in lines:
        shown = _wrap(draw, line, font, box_w - 2 * bullet)[:2]
        top = y + font.size * 0.35
        draw.rectangle([margin, top, margin + bullet, top + bullet], fill=accent_rgb)
        for part in shown:
            draw.text((margin + 2 * bullet, y), part, font=font, fill=ink)
            y += int(font.size * 1.25)
        y += int(font.size * 0.25)
    if badge:
        badge_font = _font("sans", "B", max(28, int(height * 0.035)))
        pad_x, pad_y = int(badge_font.size * 0.8), int(badge_font.size * 0.45)
        text_w = draw.textlength(badge, font=badge_font)
        box = (margin, height - margin - badge_font.size - 2 * pad_y, int(margin + text_w + 2 * pad_x), height - margin)
        draw.rounded_rectangle(box, radius=int(badge_font.size * 0.6), fill=accent_rgb)
        draw.text((box[0] + pad_x, box[1] + pad_y - badge_font.size * 0.08), badge, font=badge_font,
                  fill=readable_on(accent_rgb))  # fmt: skip
    return png(canvas)


def poster_size(shape: str) -> tuple[int, int]:
    """0.14.0: a poster's pixels: the shape's proportions, POSTER_SIDE on the longer side."""
    width, height = SHAPES[shape]
    scale = POSTER_SIDE / max(width, height)
    return round(width * scale), round(height * scale)


def poster(
    title: str,
    lines: list[str],
    background: str | None = None,
    accent: str | None = None,
    shape: str = "portrait",
) -> bytes:
    """0.14.0: a typographic poster at print size: a large title, an accent rule and lines of text, in the shape's
    proportions (drawn by Ember's code: a simple poster needs no workshop run)."""
    if shape not in SHAPES:
        raise ImageError(f"shape must be one of {', '.join(SHAPES)}")
    width, height = poster_size(shape)
    accent_rgb, bg, ink = _colours(background, accent)
    canvas = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(canvas)
    margin = int(min(width, height) * 0.09)
    box_w = width - 2 * margin
    title_font, title_lines = _fit(draw, title, "display", "B", int(height * 0.16), box_w, 5)
    y = margin
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill=accent_rgb if _visible(accent_rgb, bg) else ink)
        y += int(title_font.size * 1.08)
    y += int(title_font.size * 0.25)
    draw.rectangle([margin, y, margin + int(box_w * 0.3), y + max(8, height // 120)], fill=accent_rgb)
    if lines:
        font = _font("sans", "", max(24, int(height * 0.028)))
        shown = [part for line in lines for part in _wrap(draw, line, font, box_w)][:12]
        y = height - margin - int(font.size * 1.35) * len(shown)
        for part in shown:
            draw.text((margin, y), part, font=font, fill=ink)
            y += int(font.size * 1.35)
    out = io.BytesIO()
    canvas.save(out, "PNG", compress_level=6)  # optimize would take seconds at this size
    return out.getvalue()
