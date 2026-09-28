"""Pictures of products: page previews of a PDF, and listing photos that show pages with a title.

pypdfium2 draws PDF pages (it loads its native library through ctypes, which a sealed thread may not do, so this
module is imported at startup, before any tool runs; see app.agent.tools). Pillow composes listing photos from
Ember's own pages and previews only: no image the agent didn't make ever reaches Pillow.
"""

from __future__ import annotations

import io

import pypdfium2
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import fonts
from .theme import RGB, contrast, hex_rgb, readable_on, tint

PREVIEW_DPI = 150
SHAPES = {"landscape": (3000, 2250), "square": (2400, 2400), "portrait": (2000, 2500)}
SHAPE_NAMES = tuple(SHAPES)
MAX_PIXELS = 12_000_000


class ImageError(ValueError):
    pass


def page_count(pdf: bytes) -> int:
    document = pypdfium2.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def pdf_pages(pdf: bytes, numbers: list[int], height: int | None = None, dpi: int = PREVIEW_DPI) -> list[Image.Image]:
    """Pages of a PDF as images, at ``dpi`` or scaled to ``height`` pixels."""
    document = pypdfium2.PdfDocument(pdf)
    try:
        images = []
        for number in numbers:
            if not 1 <= number <= len(document):
                raise ImageError(f"the PDF has {len(document)} page(s), not a page {number}")
            page = document[number - 1]
            try:
                scale = (height / page.get_height()) if height else dpi / 72
                images.append(page.render(scale=scale).to_pil().convert("RGB"))
            finally:
                page.close()
        return images
    finally:
        document.close()


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def open_png(data: bytes) -> Image.Image:
    """One of Ember's own pictures (PNG or JPEG), checked before it is decoded."""
    return _checked(data).convert("RGB")


def png_size(data: bytes) -> tuple[int, int]:
    """The width and height of one of Ember's pictures (PNG or JPEG)."""
    image = _checked(data)
    return image.width, image.height


def thumbnail(data: bytes, longest: int) -> tuple[bytes, int, int]:
    """A PNG no wider or higher than ``longest`` pixels (for the model to look at) of a PNG or JPEG, and its
    size."""
    image = _checked(data).convert("RGB")
    image.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return png(image), image.width, image.height


def _checked(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    if image.format not in ("PNG", "JPEG") or image.width * image.height > MAX_PIXELS:
        raise ImageError("only Ember's own PNG and JPEG pictures can be shown")
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
