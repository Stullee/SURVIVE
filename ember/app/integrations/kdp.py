"""Amazon KDP (0.24.0), phase A: no KDP API, no Amazon credentials.

Amazon offers no API for Kindle Direct Publishing: nothing that creates a book, changes one or reads its sales, and
Amazon's terms forbid robots on its pages. So the agent describes a book in a JSON spec (its words, the manuscript or
interior, its front picture and back text), Ember's code makes its cover and checks the package against KDP's rules,
and after the owner approves it, the owner enters it at kdp.amazon.com from their own account: the approval card holds
every field to copy and the files to download. They mark it done with the book's link at Amazon, and record its
royalties in the ledger. (One tool reads the spec, as propose_blog_post reads its Markdown: the work step's fixed
prompt has no room for a tool with a field for each part.)

What Ember's code checks: the words (title and subtitle together, the description, 7 keywords, 3 categories), the
price within KDP's bands, and the files. A paperback's interior is a PDF whose pages are the trim size (with bleed:
0.125 in wider and 0.25 in higher), within KDP's page counts, with nothing printed in the margins KDP keeps free
(the inside margin grows with the page count); its cover is a PDF of the full wrap (back, spine and front, with
bleed), whose width follows from the page count and the paper. An ebook's manuscript is a Word file (.docx) and its
cover a JPEG in KDP's proportions (1,600 x 2,560 pixels at best). KDP's own previewer checks again when the owner
uploads them.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any

from ..agent.store import AgentScope
from ..economy.clock import to_iso
from .etsy import Upload

BOOKSHELF_URL = "https://kdp.amazon.com/en_US/bookshelf"  # where the owner creates a title (the card's button)
EXECUTOR = "kdp_package"  # an approval the owner carries out: Ember's code prepares it, never sends it
FORMATS = ("ebook", "paperback")
# The words, as KDP takes them
TITLE_CHARS = 199  # the title and the subtitle together: fewer than 200
DESCRIPTION_CHARS = 4_000  # KDP's limit; the tool takes less (one call's texts: tools.CALL_CHARS)
MAX_KEYWORDS = 7
KEYWORD_CHARS = 50
MAX_CATEGORIES = 3
CATEGORY_CHARS = 160  # a path such as "Self-Help > Journal Writing"
LANGUAGES = ("English", "German", "French", "Spanish", "Italian", "Dutch", "Portuguese")
DISCLOSURE = "This book was created with the help of AI and reviewed by the publisher before publication."
# What the owner answers when KDP asks whether AI tools made the book's texts, images or translations: Ember's are
# AI-generated (KDP's word for what an AI tool made, even when edited afterwards), not AI-assisted.
AI_ANSWER = (
    "Yes. Texts: entire work (with minimal or no editing, unless you edited it a lot); images: entire work (Ember "
    "designed the cover); translations: none, unless Ember translated it."
)
# A paperback: trim sizes in inches (width, height), and the most pages KDP prints at each on white, cream and colour
# paper (KDP's help, 2026: the large square and letter sizes take fewer; at least LEAST_PAGES at every one)
_REGULAR = {"white": 828, "cream": 776, "color": 828}
_MOST_PAGES = {
    **dict.fromkeys(("5x8", "5.06x7.81", "5.25x8", "5.5x8.5", "6x9", "6.14x9.21", "6.69x9.61", "7x10"), _REGULAR),
    **dict.fromkeys(("7.44x9.69", "7.5x9.25", "8x10"), _REGULAR),
    **dict.fromkeys(("8.25x6", "8.25x8.25"), {"white": 800, "cream": 750, "color": 800}),
    **dict.fromkeys(("8.5x8.5", "8.5x11"), {"white": 590, "cream": 550, "color": 590}),
    "8.27x11.69": {"white": 780, "cream": 730, "color": 780},  # A4
}
TRIMS: dict[str, tuple[Decimal, Decimal]] = {
    name: (Decimal(name.split("x")[0]), Decimal(name.split("x")[1])) for name in _MOST_PAGES
}
TRIM_NAMES = tuple(TRIMS)
LARGE_WIDTH, LARGE_HEIGHT = Decimal("6.12"), Decimal("9")  # wider or higher than this: a large trim (prints dearer)
# The paper: black ink on white or cream paper, or premium colour (on white). Each page's share of the spine, in inches.
PAPERS = ("white", "cream", "color")
SPINE_PER_PAGE = {"white": Decimal("0.002252"), "cream": Decimal("0.0025"), "color": Decimal("0.002347")}
LEAST_PAGES = 24
BLEED = Decimal("0.125")  # beyond the trim on the top, bottom and outside edge (and around a cover)
OUTSIDE_MARGIN = Decimal("0.25")  # top, bottom and outside, without bleed
OUTSIDE_MARGIN_BLEED = Decimal("0.375")  # with bleed
# The inside (gutter) margin by the most pages it covers
INSIDE_MARGINS = ((150, Decimal("0.375")), (300, Decimal("0.5")), (500, Decimal("0.625")), (700, Decimal("0.75")))
INSIDE_MARGIN_MOST = Decimal("0.875")  # 701 pages and more
SPINE_TEXT_PAGES = 79  # KDP prints text on a spine only for books with more pages than this
SPINE_TEXT_MARGIN = Decimal("0.0625")  # the space between spine text and the spine's folds
BARCODE = (Decimal("2"), Decimal("1.2"))  # the space KDP's barcode takes at the back cover's bottom right, in inches
BARCODE_GAP = Decimal("0.25")  # from the trim's bottom and the spine
SIZE_TOLERANCE = Decimal("0.01")  # inches a PDF's page may differ from the size KDP expects
INK_DPI = 36  # pages drawn this coarsely to find what is printed in their margins (0.25 in is 9 pixels)
INK_LEVEL = 245  # darker than this (0 to 255, any channel) counts as printed
# An ebook's cover: KDP's proportions are 1.6 high to 1 wide; 2,560 x 1,600 is its ideal, 1,000 x 625 its least
EBOOK_COVER = (1_600, 2_560)
EBOOK_COVER_LEAST = (625, 1_000)
EBOOK_COVER_MOST = 10_000  # pixels on either side
EBOOK_RATIO = Decimal("1.6")
EBOOK_RATIO_TOLERANCE = Decimal("0.02")
# The kinds of files, by format: the manuscript (the text) and the cover
MANUSCRIPT_KINDS = {"ebook": ".docx", "paperback": ".pdf"}
COVER_KINDS = {"ebook": ".jpg", "paperback": ".pdf"}
MAX_FILE_BYTES = 15 * 1024 * 1024  # the workspace's limit for one product file; KDP takes far more
# 2026: KDP lets an account create at most 2 new titles of each format a week. Ember's code proposes no more.
WEEKLY_TITLES = 2
# Prices in USD at Amazon.com, KDP's first marketplace (the owner sets the others at KDP, or lets KDP convert them).
# KDP's help is the reference; the card's royalty is an estimate and says so.
CURRENCY = "USD"
EBOOK_MOST = Decimal("200")
# An ebook's least price at 35% by the size of its file: below 3 MB, below 10 MB, from 10 MB on
EBOOK_LEAST = ((3, Decimal("0.99")), (10, Decimal("1.99")))
EBOOK_LEAST_LARGE = Decimal("2.99")
EBOOK_70 = (Decimal("2.99"), Decimal("12.99"))  # where KDP pays 70% less its delivery cost (9.99 until 7 July 2026)
DELIVERY_PER_MB = Decimal("0.15")  # the 70% band's delivery cost per megabyte of the converted file
PAPERBACK_MOST = Decimal("250")
# A paperback's royalty: 60% of its list price from 9.99 USD on (50% below it), less its printing cost
PAPERBACK_SHARE = Decimal("0.6")
PAPERBACK_SHARE_LOW = Decimal("0.5")
PAPERBACK_FULL_SHARE_FROM = Decimal("9.99")
# Amazon.com's printing cost of a paperback (USD): a flat cost for few pages, else a fixed cost and a cost per page,
# by ink and trim (large or not)
FLAT_PAGES = {"white": 110, "cream": 110, "color": 40}
FLAT_COST = {
    ("white", False): Decimal("2.30"),
    ("white", True): Decimal("2.84"),
    ("cream", False): Decimal("2.30"),
    ("cream", True): Decimal("2.84"),
    ("color", False): Decimal("3.60"),
    ("color", True): Decimal("4.20"),
}
FIXED_COST = Decimal("1.00")
PAGE_COST = {
    ("white", False): Decimal("0.012"),
    ("white", True): Decimal("0.017"),
    ("cream", False): Decimal("0.012"),
    ("cream", True): Decimal("0.017"),
    ("color", False): Decimal("0.065"),
    ("color", True): Decimal("0.08"),
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")
_CENT = Decimal("0.01")


class KdpError(ValueError):
    """An invalid package; the message is shown to the agent."""


# --- sizes ---------------------------------------------------------------------------------------------------------


def trim_size(name: str) -> tuple[Decimal, Decimal]:
    """A trim's width and height in inches, from its name ('6x9', '6 x 9 in')."""
    key = re.sub(r"\s+|in(?:ch(?:es)?)?$|\"", "", name.strip().lower()).replace("×", "x")
    if key not in TRIMS:
        raise KdpError(f"trim must be one of {', '.join(TRIM_NAMES)} (inches)")
    return TRIMS[key]


def trim_name(name: str) -> str:
    width, height = trim_size(name)
    return next(key for key, size in TRIMS.items() if size == (width, height))


def large(trim: str) -> bool:
    width, height = trim_size(trim)
    return width > LARGE_WIDTH or height > LARGE_HEIGHT


def check_paper(paper: str) -> str:
    if paper not in PAPERS:
        raise KdpError(f"paper must be one of {', '.join(PAPERS)}")
    return paper


def page_limits(paper: str, trim: str) -> tuple[int, int]:
    """The fewest and the most pages KDP prints on this paper at this trim."""
    return LEAST_PAGES, _MOST_PAGES[trim_name(trim)][check_paper(paper)]


def printed(pages: int) -> int:
    """The pages KDP prints: an odd count is rounded up (a blank page at the end)."""
    return pages + pages % 2


def spine_width(pages: int, paper: str) -> Decimal:
    """The spine's width in inches: each page's share times the pages KDP prints (KDP's formula)."""
    return SPINE_PER_PAGE[check_paper(paper)] * printed(pages)


def inside_margin(pages: int) -> Decimal:
    """The inside (gutter) margin KDP asks for a book of this many pages, in inches."""
    return next((margin for most, margin in INSIDE_MARGINS if pages <= most), INSIDE_MARGIN_MOST)


def interior_size(trim: str, bleed: bool) -> tuple[Decimal, Decimal]:
    """A page of the interior PDF, in inches: the trim, with bleed 0.125 in wider (the outside edge) and 0.25 in
    higher (top and bottom)."""
    width, height = trim_size(trim)
    return (width + BLEED, height + 2 * BLEED) if bleed else (width, height)


def cover_size(trim: str, pages: int, paper: str) -> tuple[Decimal, Decimal]:
    """A paperback's full cover in inches: bleed, back, spine, front and bleed across; bleed, the trim and bleed
    down."""
    width, height = trim_size(trim)
    return 2 * BLEED + 2 * width + spine_width(pages, paper), 2 * BLEED + height


def inches(value: Decimal | float) -> str:
    """A length as the card shows it: '6.125 in' (at most three decimals, no trailing zeros)."""
    text = f"{Decimal(str(value)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP):f}".rstrip("0").rstrip(".")
    return f"{text} in"


# --- the words -----------------------------------------------------------------------------------------------------


def _line(value: Any, what: str, limit: int, required: bool = True) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not text:
        if required:
            raise KdpError(f"the {what} is empty")
        return ""
    if len(text) > limit:
        raise KdpError(f"the {what} has more than {limit} characters")
    if _CONTROL.search(text):
        raise KdpError(f"the {what} contains control or direction characters")
    return text


def check_title(title: Any, subtitle: Any) -> tuple[str, str]:
    """The title and the subtitle; KDP takes at most TITLE_CHARS for both together."""
    main = _line(title, "title", TITLE_CHARS)
    sub = _line(subtitle, "subtitle", TITLE_CHARS, required=False)
    if len(main) + len(sub) > TITLE_CHARS:
        raise KdpError(f"the title and the subtitle together have more than {TITLE_CHARS} characters")
    return main, sub


def check_description(text: Any, limit: int = DESCRIPTION_CHARS) -> str:
    if not isinstance(text, str) or not text.strip():
        raise KdpError("the description is empty")
    text = text.replace("\r\n", "\n").strip()
    if text.endswith(DISCLOSURE):  # Ember's code adds it; a copy the agent wrote isn't doubled
        text = text[: -len(DISCLOSURE)].rstrip()
    if len(text) > limit:
        raise KdpError(f"the description has more than {limit:,} characters")
    if _CONTROL.search(text.replace("\n", "").replace("\t", "")):
        raise KdpError("the description contains control or direction characters")
    return text


def with_disclosure(description: str) -> str:
    return f"{description.rstrip()}\n\n{DISCLOSURE}"


def _items(value: Any, separator: str) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return value.split(separator) if isinstance(value, str) else []


def check_keywords(value: Any) -> tuple[str, ...]:
    """KDP's keywords: at most MAX_KEYWORDS phrases (separated by commas), each at most KEYWORD_CHARS."""
    clean: list[str] = []
    for item in _items(value, ","):
        phrase = " ".join(item.split())
        if not phrase:
            continue
        if len(phrase) > KEYWORD_CHARS:
            raise KdpError(f"the keyword '{phrase[:30]}...' has more than {KEYWORD_CHARS} characters")
        if _CONTROL.search(phrase) or '"' in phrase:
            raise KdpError(f"the keyword '{phrase[:30]}' contains quotes or control characters")
        if phrase.lower() not in (k.lower() for k in clean):
            clean.append(phrase)
    if not clean:
        raise KdpError("give at least one keyword: what readers type into Amazon's search")
    if len(clean) > MAX_KEYWORDS:
        raise KdpError(f"KDP takes at most {MAX_KEYWORDS} keywords")
    return tuple(clean)


def check_categories(value: Any) -> tuple[str, ...]:
    """Up to MAX_CATEGORIES of Amazon's categories, separated by ';' ('Self-Help > Journal Writing'). The owner picks
    them in KDP's list: these say which."""
    clean: list[str] = []
    for item in _items(value, ";"):
        path = " > ".join(part.strip() for part in " ".join(item.split()).split(">") if part.strip())
        if not path:
            continue
        if len(path) > CATEGORY_CHARS:
            raise KdpError(f"the category '{path[:30]}...' has more than {CATEGORY_CHARS} characters")
        if _CONTROL.search(path):
            raise KdpError("a category contains control or direction characters")
        if path.lower() not in (c.lower() for c in clean):
            clean.append(path)
    if not clean:
        raise KdpError("name at least one category, like 'Self-Help > Journal Writing'")
    if len(clean) > MAX_CATEGORIES:
        raise KdpError(f"KDP takes at most {MAX_CATEGORIES} categories (separate them with ';')")
    return tuple(clean)


def check_language(value: Any) -> str:
    text = str(value or "").strip().capitalize()
    if text not in LANGUAGES:
        raise KdpError(f"language must be one of {', '.join(LANGUAGES)}")
    return text


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value).strip().replace(",", ".").removeprefix("$"))
    except InvalidOperation:
        raise KdpError("the price must be a number like 4.99 (USD)") from None
    if not amount.is_finite():
        raise KdpError("the price must be a number like 4.99 (USD)")
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def printing_cost(pages: int, paper: str, trim: str) -> Decimal:
    """What Amazon.com charges to print one paperback of this many pages, in USD."""
    key = (check_paper(paper), large(trim))
    count = printed(pages)
    if count <= FLAT_PAGES[paper]:
        return FLAT_COST[key]
    return (FIXED_COST + PAGE_COST[key] * count).quantize(_CENT, rounding=ROUND_HALF_UP)


def paperback_share(price: Decimal) -> Decimal:
    """The share of a paperback's list price KDP pays at Amazon.com: 60% from 9.99 USD on, 50% below."""
    return PAPERBACK_SHARE if price >= PAPERBACK_FULL_SHARE_FROM else PAPERBACK_SHARE_LOW


def least_price(pages: int, paper: str, trim: str) -> Decimal:
    """The lowest list price KDP takes for a paperback: its share of it pays the printing."""
    cost = printing_cost(pages, paper, trim)
    low = (cost / PAPERBACK_SHARE_LOW).quantize(_CENT, rounding=ROUND_CEILING)
    if low < PAPERBACK_FULL_SHARE_FROM:
        return low
    return max(PAPERBACK_FULL_SHARE_FROM, (cost / PAPERBACK_SHARE).quantize(_CENT, rounding=ROUND_CEILING))


def ebook_least(file_bytes: int) -> Decimal:
    """An ebook's lowest list price by the size of its file (KDP measures the converted file: this is close)."""
    megabytes = file_bytes / (1024 * 1024)
    return next((low for most, low in EBOOK_LEAST if megabytes < most), EBOOK_LEAST_LARGE)


def check_price(value: Any, form: str, *, file_bytes: int = 0, pages: int = 0, paper: str = "", trim: str = "") -> str:
    """A list price in USD at Amazon.com, within KDP's band for the format."""
    amount = _money(value)
    if form == "ebook":
        low = ebook_least(file_bytes)
        if not low <= amount <= EBOOK_MOST:
            raise KdpError(f"this ebook's price must be {low} to {EBOOK_MOST} USD")
    else:
        low = least_price(pages, paper, trim)
        if not low <= amount <= PAPERBACK_MOST:
            raise KdpError(
                f"this paperback's price must be {low} to {PAPERBACK_MOST} USD: printing it costs "
                f"{printing_cost(pages, paper, trim)} USD at Amazon.com, and KDP pays 50% of a price below "
                f"{PAPERBACK_FULL_SHARE_FROM} USD (60% from it on) before the printing"
            )
    return str(amount)


def royalty(book: Book) -> str:
    """What one sale at Amazon.com earns, in words (an estimate: before tax withholding; KDP's figure counts)."""
    price = Decimal(book.price)
    if book.format == "ebook":
        low, high = EBOOK_70
        if low <= price <= high:
            megabytes = Decimal(book.manuscript.bytes) / (1024 * 1024)
            delivery = max(_CENT, (DELIVERY_PER_MB * megabytes).quantize(_CENT, rounding=ROUND_HALF_UP))
            earned = (price * Decimal("0.7") - delivery).quantize(_CENT, rounding=ROUND_HALF_UP)
            return f"about {earned} USD a sale at the 70% royalty, less a delivery cost of about {delivery} USD"
        earned = (price * Decimal("0.35")).quantize(_CENT, rounding=ROUND_HALF_UP)
        return f"about {earned} USD a sale at the 35% royalty (KDP pays 70% from {low} to {high} USD)"
    cost = printing_cost(book.pages, book.paper, book.trim)
    share = paperback_share(price)
    earned = (price * share - cost).quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"about {earned} USD a sale: {share:.0%} of {price} USD less the printing cost of about {cost} USD"


# --- the files -----------------------------------------------------------------------------------------------------


def upload(path: str, data: bytes, kind: str, what: str) -> Upload:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix != kind:
        raise KdpError(f"{path}: {what} must be a {kind} file")
    if not data:
        raise KdpError(f"{path} is empty")
    if len(data) > MAX_FILE_BYTES:
        raise KdpError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    return Upload(path, hashlib.sha256(data).hexdigest(), len(data))


def check_docx(data: bytes, path: str) -> None:
    """A Word file KDP can convert: a .docx package with its document part (never a macro-enabled one)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = set(package.namelist())
    except (zipfile.BadZipFile, ValueError):
        raise KdpError(f"{path} isn't a Word file (.docx) Ember's code can read") from None
    if "word/document.xml" not in names or "[Content_Types].xml" not in names:
        raise KdpError(f"{path} isn't a Word document (.docx)")
    if any(name.lower().endswith("vbaproject.bin") for name in names):
        raise KdpError(f"{path} holds macros, which KDP refuses")


def check_ebook_cover(data: bytes, width: int, height: int, path: str) -> list[str]:
    """An ebook cover: a JPEG (KDP takes JPEG or TIFF), refused below KDP's least or above its most pixels, or out of
    its proportions; a note below its ideal."""
    if not data.startswith(b"\xff\xd8\xff"):
        raise KdpError(f"{path} isn't a JPEG picture: KDP takes JPEG covers (give cover.front to have one made)")
    if max(width, height) > EBOOK_COVER_MOST:
        raise KdpError(f"{path} is {width} x {height} pixels: KDP takes at most {EBOOK_COVER_MOST:,} a side")
    least_w, least_h = EBOOK_COVER_LEAST
    if width < least_w or height < least_h:
        raise KdpError(f"{path} is {width} x {height} pixels: KDP needs at least {least_w} x {least_h}")
    ratio = Decimal(height) / Decimal(width)
    if abs(ratio - EBOOK_RATIO) > EBOOK_RATIO_TOLERANCE:
        best_w, best_h = EBOOK_COVER
        raise KdpError(
            f"{path} is {width} x {height} pixels ({ratio:.2f} high to 1 wide): KDP's covers are 1.6 high to 1 wide, "
            f"{best_w} x {best_h} at best (give cover.front for Ember's code to make one)"
        )
    best_w, best_h = EBOOK_COVER
    if width < best_w:
        return [f"the cover has {width} x {height} pixels, fewer than KDP's ideal {best_w} x {best_h}"]
    return []


@dataclass(frozen=True)
class Interior:
    """What Ember's code found in a paperback's interior PDF."""

    pages: int
    bleed: bool
    findings: tuple[str, ...]  # what KDP would refuse; empty when it passes


def _matches(size: tuple[float, float], want: tuple[Decimal, Decimal]) -> bool:
    width, height = (Decimal(str(round(v / 72, 4))) for v in size)
    return abs(width - want[0]) <= SIZE_TOLERANCE and abs(height - want[1]) <= SIZE_TOLERANCE


Ink = list[tuple[float, float, float, float] | None]


def check_interior(sizes: list[tuple[float, float]], ink: Callable[[], Ink], trim: str, paper: str) -> Interior:
    """A paperback's interior: ``sizes`` are its pages' sizes in points, ``ink`` reads each page's printed part (left,
    top, right and bottom, in inches from the page's top left corner; None for an empty page), only once its pages
    are the right size. Its pages must all be the trim, or all the trim with bleed; their count within KDP's for the
    paper and trim; and without bleed, nothing may be printed within the margins (the inside one on the left of a
    right-hand page, page 1 being one). With bleed, KDP's previewer checks the margins: art may reach the edge."""
    low, high = page_limits(paper, trim)
    pages = len(sizes)
    if not sizes:
        raise KdpError("the interior has no pages")
    plain, bled = interior_size(trim, False), interior_size(trim, True)
    bleed = _matches(sizes[0], bled) and not _matches(sizes[0], plain)
    want = bled if bleed else plain
    findings: list[str] = []
    if not low <= pages <= high:
        findings.append(f"it has {pages} pages; KDP prints {low} to {high} on {paper} paper")
    wrong = [n for n, size in enumerate(sizes, 1) if not _matches(size, want)]
    if wrong:
        width, height = (Decimal(str(round(v / 72, 3))) for v in sizes[wrong[0] - 1])
        findings.append(
            f"page {wrong[0]} is {inches(width)} x {inches(height)}, not {inches(want[0])} x {inches(want[1])} "
            f"(the {trim} trim{' with bleed' if bleed else ''}; {len(wrong)} such page{'s' if len(wrong) != 1 else ''}"
            f"); without bleed every page is {inches(plain[0])} x {inches(plain[1])}, with bleed "
            f"{inches(bled[0])} x {inches(bled[1])}"
        )
    if not wrong and not bleed:
        findings += _margins(ink(), plain, inside_margin(pages))
    return Interior(pages, bleed, tuple(findings))


def _margins(ink: Ink, size: tuple[Decimal, Decimal], inside: Decimal) -> list[str]:
    """The pages that print within KDP's margins (without bleed): the first few, and how many in all."""
    width, height = (float(v) for v in size)
    outside, gutter = float(OUTSIDE_MARGIN), float(inside)
    found: list[str] = []
    count = 0
    for number, box in enumerate(ink, 1):
        if box is None:
            continue
        left, top, right, bottom = box
        # page 1 is a right-hand page: its inside edge is on the left
        inner, outer = (left, width - right) if number % 2 else (width - right, left)
        sides = [
            name
            for name, room, least in (
                ("inside", inner, gutter),
                ("outside", outer, outside),
                ("top", top, outside),
                ("bottom", height - bottom, outside),
            )
            if room < least - 0.02  # the drawing's coarseness
        ]
        if sides:
            count += 1
            if len(found) < 3:
                named = ", ".join(sides[:-1]) + " and " + sides[-1] if len(sides) > 1 else sides[0]
                found.append(f"page {number} prints within its {named} margin{'s' if len(sides) > 1 else ''}")
    if not count:
        return []
    more = f" ({count} pages in all)" if count > len(found) else ""
    return [
        "; ".join(found) + more + f": without bleed KDP keeps {inches(outside)} free at the top, bottom and outside "
        f"edge and {inches(gutter)} at the inside edge for this many pages (make_document: a margin of at least "
        f"{float(inside) * 25.4 + 0.5:.0f} mm, and no sidebar or coloured page that reaches the edge)"
    ]


def check_cover(size: tuple[float, float], pages: int, trim: str, paper: str) -> list[str]:
    """A paperback's cover PDF (one page): its size must be the full wrap for this many pages on this paper."""
    want = cover_size(trim, pages, paper)
    if _matches(size, want):
        return []
    width, height = (Decimal(str(round(v / 72, 3))) for v in size)
    return [
        f"the cover is {inches(width)} x {inches(height)}; for {pages} pages on {paper} paper at {trim} it must be "
        f"{inches(want[0])} x {inches(want[1])} (the spine is {inches(spine_width(pages, paper))}): give cover.front "
        "for Ember's code to make it from this interior"
    ]


# --- the spec: the agent's .json file ------------------------------------------------------------------------------

SPEC_KEYS = (
    "format", "title", "subtitle", "description", "keywords", "categories", "language", "price", "manuscript", "paper",
    "low_content", "cover", "project_id", "reason",
)  # fmt: skip
REQUIRED_KEYS = ("format", "title", "description", "keywords", "categories", "language", "price", "manuscript", "cover")
COVER_KEYS = ("front", "back", "spine", "background")
REASON_CHARS = 300
_TEXT_KEYS = ("title", "subtitle", "description", "language", "manuscript", "paper", "reason")


def read_spec(text: str, path: str) -> dict[str, Any]:
    """A book's spec as the agent wrote it: one JSON object with SPEC_KEYS; ``cover`` is a cover file of its own, or
    an object with COVER_KEYS for Ember's code to make it. Its values are checked further as the package is made."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        where = f", line {exc.lineno}" if isinstance(exc, json.JSONDecodeError) else ""
        raise KdpError(f"{path} isn't valid JSON{where}") from None
    if not isinstance(data, dict):
        raise KdpError(f"{path} must hold one JSON object (guide 'kdp')")
    unknown = sorted(set(data) - set(SPEC_KEYS))
    if unknown:
        raise KdpError(f"{path}: unknown key {unknown[0]!r}; a spec has {', '.join(SPEC_KEYS)}")
    missing = [key for key in REQUIRED_KEYS if data.get(key) in (None, "")]
    if missing:
        raise KdpError(f"{path} needs {', '.join(missing)} (guide 'kdp')")
    if data["format"] not in FORMATS:
        raise KdpError(f"format must be {' or '.join(FORMATS)}")
    for key in _TEXT_KEYS:
        if key in data and not isinstance(data[key], str):
            raise KdpError(f"{key} must be text")
    if not isinstance(data.get("price"), str | int | float) or isinstance(data["price"], bool):
        raise KdpError("price must be a number like 9.99")
    if "low_content" in data and not isinstance(data["low_content"], bool):
        raise KdpError("low_content must be true or false")
    if "project_id" in data and (not isinstance(data["project_id"], int) or isinstance(data["project_id"], bool)):
        raise KdpError("project_id must be a project's number")
    if data["format"] == "ebook" and (data.get("paper") or data.get("low_content")):
        raise KdpError("paper and low_content are a paperback's: leave them out for an ebook")
    if data["format"] == "paperback" and not data.get("paper"):
        raise KdpError(f"a paperback needs paper: {', '.join(PAPERS)}")
    if len(str(data.get("reason") or "")) > REASON_CHARS:
        raise KdpError(f"reason has more than {REASON_CHARS} characters")
    cover = data["cover"]
    if isinstance(cover, dict):
        unknown = sorted(set(cover) - set(COVER_KEYS))
        if unknown:
            raise KdpError(f"cover: unknown key {unknown[0]!r}; it has {', '.join(COVER_KEYS)}")
        if not isinstance(cover.get("front"), str) or not cover["front"].strip():
            raise KdpError("cover needs front: your front picture, a .png or .jpg")
        if any(not isinstance(cover.get(key, ""), str) for key in COVER_KEYS):
            raise KdpError("cover's front, back, spine and background are text")
    elif not isinstance(cover, str):
        raise KdpError("cover is your cover file, or {\"front\": ...} for Ember's code to make it (guide 'kdp')")
    return data


def cover_path(spec: str, form: str) -> str:
    """Where Ember's code makes a spec's cover: next to it, an ebook's .jpg or a paperback's .pdf."""
    return f"{spec.removesuffix('.json')}-cover{COVER_KINDS[form]}"


# --- the package ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Book:
    """A checked package: exactly what the owner enters at KDP (the description without its AI line)."""

    format: str  # ebook or paperback
    title: str
    subtitle: str
    author: str  # the owner's kdp_author ("" when they choose it at KDP)
    description: str
    keywords: tuple[str, ...]
    categories: tuple[str, ...]
    language: str
    price: str  # USD at Amazon.com
    manuscript: Upload  # an ebook's .docx, a paperback's interior .pdf
    cover: Upload  # an ebook's .jpg, a paperback's full-wrap .pdf
    trim: str = ""  # a paperback's
    paper: str = ""
    bleed: bool = False
    pages: int = 0
    low_content: bool = False  # a paperback with little text (a journal, a planner): no ISBN needed at KDP

    def to_action(self) -> dict[str, Any]:
        data = asdict(self)
        data["keywords"] = list(self.keywords)
        data["categories"] = list(self.categories)
        return data


def book_from_action(raw: str | dict[str, Any]) -> Book:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise KdpError("the package isn't readable")
    try:
        files = {
            key: Upload(str(data[key]["path"]), str(data[key]["sha256"]), int(data[key]["bytes"]))
            for key in ("manuscript", "cover")
        }
        return Book(
            format=str(data["format"]),
            title=str(data["title"]),
            subtitle=str(data.get("subtitle") or ""),
            author=str(data.get("author") or ""),
            description=str(data["description"]),
            keywords=tuple(str(k) for k in data["keywords"]),
            categories=tuple(str(c) for c in data["categories"]),
            language=str(data["language"]),
            price=str(data["price"]),
            manuscript=files["manuscript"],
            cover=files["cover"],
            trim=str(data.get("trim") or ""),
            paper=str(data.get("paper") or ""),
            bleed=bool(data.get("bleed")),
            pages=int(data.get("pages") or 0),
            low_content=bool(data.get("low_content")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KdpError(f"the package isn't readable ({type(exc).__name__})") from None


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def print_line(book: Book) -> str:
    """A paperback's print settings, as KDP asks for them."""
    ink = "Premium color ink, white paper" if book.paper == "color" else f"Black & white ink, {book.paper} paper"
    width, height = trim_size(book.trim)
    return (
        f"{ink}; trim {inches(width)} x {inches(height)}; {'bleed' if book.bleed else 'no bleed'}; matte or glossy "
        f"cover (your choice); {book.pages} pages"
    )


def payload(book: Book) -> str:
    """The request as the owner reads and approves it."""
    lines = [
        f"Amazon KDP {book.format}: {book.title}",
        *([f"Subtitle: {book.subtitle}"] if book.subtitle else []),
        f"Author: {book.author or 'yours to enter at KDP (set kdp_author to have it filled in)'}",
        f"Language: {book.language}",
        f"Keywords: {'; '.join(book.keywords)}",
        f"Categories: {'; '.join(book.categories)}",
        f"Price: {book.price} {CURRENCY} at Amazon.com ({royalty(book)})",
    ]
    if book.format == "paperback":
        lines.append(f"Print: {print_line(book)}" + ("; low-content book" if book.low_content else ""))
        lines.append(f"Interior: {book.manuscript.path} ({_size(book.manuscript.bytes)})")
    else:
        lines.append(f"Manuscript: {book.manuscript.path} ({_size(book.manuscript.bytes)})")
    lines += [
        f"Cover: {book.cover.path} ({_size(book.cover.bytes)})",
        f"KDP's AI question: {AI_ANSWER}",
        "",
        with_disclosure(book.description),
    ]
    return "\n".join(lines)


def trim_of(size: tuple[float, float]) -> tuple[str, bool] | None:
    """The trim a page of ``size`` points is, and whether with bleed (None: no KDP trim)."""
    for name in TRIMS:
        if _matches(size, interior_size(name, False)):
            return name, False
        if _matches(size, interior_size(name, True)):
            return name, True
    return None


# --- the plan's KDP section ----------------------------------------------------------------------------------------

SHOWN = 8  # the newest books the plan lists
_STATES = {
    "pending": "waiting for your owner",
    "approved": "approved: your owner publishes it at KDP",
    "approved_with_changes": "approved: your owner publishes it at KDP",
    "done": "published",
    "failed": "not published",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
    "expired": "expired undecided",
}


def _quoted(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return json.dumps(flat if len(flat) <= limit else flat[: limit - 1] + "…", ensure_ascii=False)


def text(conn: sqlite3.Connection, scope: AgentScope, now: datetime, author: str) -> str:
    """The plan's KDP section: how the owner publishes, this week's room under KDP's limit, and the newest books."""
    where, params = scope.where()
    rows = conn.execute(
        f"SELECT id, status, action, created_at, decision_comment, result_note, result_link FROM approvals"
        f" WHERE {where} AND executor = ? ORDER BY id DESC",
        (*params, EXECUTOR),
    ).fetchall()
    since = to_iso(now - timedelta(days=7))
    counted = {form: 0 for form in FORMATS}
    lines = []
    for r in rows:
        try:
            book = book_from_action(r["action"])
        except KdpError:
            continue
        if r["created_at"] >= since and r["status"] not in ("rejected", "withdrawn", "expired", "failed"):
            counted[book.format] = counted.get(book.format, 0) + 1
        if len(lines) >= SHOWN:
            continue
        what = f"{book.trim}, {book.pages} pages, " if book.format == "paperback" else ""
        line = f"#{r['id']} {book.format} {_quoted(book.title, 80)} ({what}{book.price} {CURRENCY}): "
        line += _STATES.get(str(r["status"]), str(r["status"]))
        if r["status"] == "done" and r["result_link"]:
            line += f" at {r['result_link']}"
        why = r["decision_comment"] if r["status"] == "rejected" else r["result_note"]
        if why and r["status"] in ("rejected", "done", "failed"):
            line += f" ({_quoted(why, 120)})"
        lines.append(line)
    room = ", ".join(f"{counted.get(form, 0)} {form}{'s' if counted.get(form, 0) != 1 else ''}" for form in FORMATS)
    head = (
        "Your owner publishes your books at KDP by hand (Amazon has no API): propose one with propose_kdp_book from "
        f"its .json spec (guide 'kdp'). KDP lets them create at most {WEEKLY_TITLES} new titles of each format a "
        f"week; proposed in the last 7 days: {room}. Royalties count once your owner records them. Author name: "
        f"{'set by your owner' if author else 'your owner enters it at KDP'}."
    )
    return "\n".join([head, *(["Your books, newest first:", *lines] if lines else ["No books yet."])])
