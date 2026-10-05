"""The fonts Ember's products use: three bundled families (see fonts/FONTS.md), and which glyphs they have.

``sans`` and ``serif`` are metric-compatible with Calibri and Cambria, so a Word file can name the Office font and
look like its PDF; ``display`` is Poppins, for headings. Text is split into pieces a font can draw: a character the
chosen family lacks falls back to Carlito (the largest), and one no bundled font has is replaced by "?" (and
reported), so nothing silently disappears from a product.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from fontTools.ttLib import TTFont

FONT_DIR = Path(__file__).with_name("fonts")
STYLES = ("", "B", "I", "BI")
FALLBACK = "sans"
MISSING = "?"


@dataclass(frozen=True)
class Family:
    key: str
    label: str  # how the agent and the owner know it
    word_name: str  # the font a Word file names
    files: dict[str, str]  # style -> file name


FAMILIES: dict[str, Family] = {
    "sans": Family(
        "sans",
        "Calibri",
        "Calibri",
        {"": "Carlito-Regular.ttf", "B": "Carlito-Bold.ttf", "I": "Carlito-Italic.ttf", "BI": "Carlito-BoldItalic.ttf"},
    ),
    "serif": Family(
        "serif",
        "Cambria",
        "Cambria",
        {"": "Caladea-Regular.ttf", "B": "Caladea-Bold.ttf", "I": "Caladea-Italic.ttf", "BI": "Caladea-BoldItalic.ttf"},
    ),
    "display": Family(
        "display",
        "Poppins",
        "Poppins",
        {"": "Poppins-Regular.ttf", "B": "Poppins-Bold.ttf", "I": "Poppins-Italic.ttf", "BI": "Poppins-BoldItalic.ttf"},
    ),
}


def style_of(bold: bool, italic: bool) -> str:
    return ("B" if bold else "") + ("I" if italic else "")


def path(family: str, style: str) -> Path:
    return FONT_DIR / FAMILIES[family].files[style]


@cache
def codepoints(family: str, style: str) -> frozenset[int]:
    with TTFont(path(family, style), lazy=True) as font:
        return frozenset(font.getBestCmap())


def undrawable(text: str, family: str, *styles: str) -> list[str]:
    """0.23.0: the characters of ``text`` (spaces aside) that the family's ``styles`` (its regular one when none is
    named) don't all have: Pillow draws a box for each. The statement's cover and make_image's pictures share it."""
    have = frozenset.intersection(*(codepoints(family, style) for style in set(styles or ("",))))
    return sorted({char for char in text if ord(char) not in have and not char.isspace()})


@cache
def metrics(family: str) -> tuple[float, float]:
    """(ascender, descender) as fractions of the font size, from the regular face's hhea table."""
    with TTFont(path(family, ""), lazy=True) as font:
        units = font["head"].unitsPerEm
        return font["hhea"].ascent / units, -font["hhea"].descent / units


def pieces(text: str, family: str, style: str) -> tuple[list[tuple[str, str]], set[str]]:
    """``text`` as (text, family) pieces that can be drawn, and the characters no bundled font has (as "?")."""
    own = codepoints(family, style)
    spare = codepoints(FALLBACK, style)
    out: list[tuple[str, str]] = []
    missing: set[str] = set()
    for char in text:
        code = ord(char)
        if code in own or char in "\n\t":
            use = family
        elif code in spare:
            use = FALLBACK
        else:
            missing.add(char)
            char, use = MISSING, family
        if out and out[-1][1] == use:
            out[-1] = (out[-1][0] + char, use)
        else:
            out.append((char, use))
    return out, missing
