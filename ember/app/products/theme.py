"""Themes: the look a document gets from a preset and the settings the agent chose on top of it."""

from __future__ import annotations

from dataclasses import dataclass

from .markup import KDP_PAGES, Settings

RGB = tuple[int, int, int]

PRESETS: dict[str, dict[str, object]] = {
    "modern": {
        "font": "sans", "heading_font": "display", "size": 10.5, "line_height": 1.35, "margin": 16.0,
        "accent": "#2C3E50", "text": "#222222", "muted": "#6B7280", "background": "#FFFFFF",
        "h": (24.0, 12.5, 11.0), "upper": (False, True, False), "rule": True, "band": False, "table": "lines",
    },
    "classic": {
        "font": "serif", "heading_font": "serif", "size": 10.5, "line_height": 1.35, "margin": 18.0,
        "accent": "#1F3A5F", "text": "#1A1A1A", "muted": "#5B6470", "background": "#FFFFFF",
        "h": (22.0, 13.0, 11.0), "upper": (False, False, False), "rule": True, "band": False, "table": "lines",
    },
    "minimal": {
        "font": "sans", "heading_font": "sans", "size": 10.0, "line_height": 1.4, "margin": 20.0,
        "accent": "#111111", "text": "#222222", "muted": "#777777", "background": "#FFFFFF",
        "h": (26.0, 10.5, 10.5), "upper": (False, True, False), "rule": False, "band": False, "table": "plain",
    },
    "bold": {
        "font": "sans", "heading_font": "display", "size": 10.5, "line_height": 1.35, "margin": 15.0,
        "accent": "#E4572E", "text": "#1B1B1B", "muted": "#6B6B6B", "background": "#FFFFFF",
        "h": (28.0, 13.0, 11.0), "upper": (False, False, False), "rule": False, "band": True, "table": "zebra",
    },
}  # fmt: skip

PAGE_SIZES = {"A4": (210.0, 297.0), "Letter": (215.9, 279.4)}  # mm, portrait
# 0.25.0: KDP's paperback trim sizes ('6x9in'), for a book's interior
PAGE_SIZES |= {f"{name}in": (float(name.split("x")[0]) * 25.4, float(name.split("x")[1]) * 25.4) for name in KDP_PAGES}
WHITE: RGB = (255, 255, 255)
DARK: RGB = (34, 34, 34)
MIN_CONTRAST = 3.0  # WCAG's minimum for large text; body text below it is hard to read


@dataclass(frozen=True)
class Theme:
    font: str
    heading_font: str
    size: float  # pt
    line_height: float
    margin: float  # mm
    accent: RGB
    text: RGB
    muted: RGB
    background: RGB
    h_sizes: tuple[float, float, float]
    upper: tuple[bool, bool, bool]
    rule: bool  # a line under level-2 headings
    band: bool  # level-2 headings on an accent band
    table: str
    sidebar: str
    sidebar_width: float
    sidebar_background: RGB
    sidebar_text: RGB
    page_width: float
    page_height: float


def hex_rgb(value: str) -> RGB:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def rgb_hex(color: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*color)


def tint(color: RGB, amount: float) -> RGB:
    """``color`` mixed with white: amount 0 is the colour, 1 is white."""
    r, g, b = (round(c + (255 - c) * amount) for c in color)
    return r, g, b


def luminance(color: RGB) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def readable_on(background: RGB) -> RGB:
    """White or dark text, whichever reads better on ``background``."""
    return WHITE if contrast(WHITE, background) >= contrast(DARK, background) else DARK


def resolve(settings: Settings) -> Theme:
    preset = PRESETS[settings.theme]

    def pick(name: str) -> object:
        value = getattr(settings, name, None)
        return preset[name] if value is None else value

    accent = hex_rgb(str(pick("accent")))
    width, height = PAGE_SIZES[settings.page]
    if settings.landscape:
        width, height = height, width
    sidebar_background = hex_rgb(settings.sidebar_background) if settings.sidebar_background else accent
    sidebar_text = hex_rgb(settings.sidebar_text) if settings.sidebar_text else readable_on(sidebar_background)
    h_sizes = preset["h"]
    upper = preset["upper"]
    assert isinstance(h_sizes, tuple) and isinstance(upper, tuple)  # noqa: S101 - the presets above
    return Theme(
        font=str(pick("font")),
        heading_font=str(pick("heading_font")),
        size=float(pick("size")),  # type: ignore[arg-type]
        line_height=float(pick("line_height")),  # type: ignore[arg-type]
        margin=float(pick("margin")),  # type: ignore[arg-type]
        accent=accent,
        text=hex_rgb(str(pick("text"))),
        muted=hex_rgb(str(pick("muted"))),
        background=hex_rgb(str(pick("background"))),
        h_sizes=h_sizes,  # type: ignore[arg-type]
        upper=upper,  # type: ignore[arg-type]
        rule=bool(preset["rule"]),
        band=bool(preset["band"]),
        table=str(pick("table")),
        sidebar=settings.sidebar,
        sidebar_width=min(settings.sidebar_width, width * 0.5),
        sidebar_background=sidebar_background,
        sidebar_text=sidebar_text,
        page_width=width,
        page_height=height,
    )


def warnings(theme: Theme) -> list[str]:
    """Colour choices that make text hard to read."""
    out = []
    if contrast(theme.text, theme.background) < MIN_CONTRAST:
        out.append(
            f"the text colour {rgb_hex(theme.text)} is hard to read on the background {rgb_hex(theme.background)}"
        )
    if theme.sidebar != "none" and contrast(theme.sidebar_text, theme.sidebar_background) < MIN_CONTRAST:
        out.append(
            f"the sidebar text {rgb_hex(theme.sidebar_text)} is hard to read on {rgb_hex(theme.sidebar_background)}"
        )
    if contrast(theme.accent, theme.background) < 1.8:
        out.append(f"headings in {rgb_hex(theme.accent)} barely show on {rgb_hex(theme.background)}")
    return out
