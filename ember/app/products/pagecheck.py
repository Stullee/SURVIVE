"""A paperback's pages drawn fresh and measured before it goes to the owner (0.40.0, upgrade request #12).

The owner rejected the Haushaltsbuch's interior again and again, and twice the agent had trusted pictures of an
earlier version or a checklist it had passed days before ("KDP v13 already passed its checklist… resubmit directly,
not re-render", analysis-0.39.6, 3.2). What worked was a workshop script (workshop/scripts/render-page-3-of-71.py,
lesson #c287): one page of the interior drawn fresh with pypdfium2 at 600 pixels wide, then checked against the
owner's own words. Each run cost about $0.19, and nothing made the agent run it. Now propose_kdp_book's check draws the
pages itself, free, every time, and a paperback is proposed only after a check of the same file in the same cycle.

``check`` reads the interior's pages and returns what Ember's code can measure on them:

* the kinds of page (``kinds``): pages with the same ruled lines in the same places are one kind (every week of a
  planner); pages without ruled lines are one kind of their own. One page of each kind is drawn (``shown``), the AI
  line's page and the last page too, so every kind of page is looked at, not the first four;
* the rows to write in (``rows``): blank rows between ruled lines, two or more of one height in a row, as a table's
  empty rows or writing lines are; a row lower than WRITE_LEAST is too low to write in (2.2 mm rows, 0.39.1);
* white space (``blank``): the tallest band with nothing printed between a page's first and last mark (its footer
  included), named when it is more than BLANK_SHARE of the page (note lines alone on a page of their own, 0.39.6);
* the calendar (``calendar``): the dates the pages print, read from their text: a date that doesn't exist, a weekday
  that isn't its date's, a week whose label (KW 1, Week 1) or days aren't that calendar week's, a week missing
  between the first and the last;
* the AI line (``ai_pages``): the pages that say AI made the book, and how many words share its page.

What Ember's code can't judge (whether white space is a title page's or one the owner rejected) is the agent's, on the
pictures: the measures name the pages to look at.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import pypdfium2
from PIL import Image

SCAN_DPI = 100  # each page measured at this (1 pixel: 0.25 mm)
PICTURE_WIDTH = 600  # a page's picture: the workshop script's TARGET_WIDTH (a 6x9 page at 100 dpi)
PAGES_SHOWN = 6  # pictures a check shows at most: about 700 input tokens each
WRITE_LEAST = 5.0  # mm: a blank row to write in at least (a table's empty row is 7 to 8 mm, writing lines 8)
BLANK_SHARE = 0.25  # a blank band taller than this share of its page is named
MM_PER_PIXEL = 25.4 / SCAN_DPI
INK_LEVEL = 245  # darker than this (0 to 255) counts as printed, as KDP's margin check reads it
_BLOCK = 8  # pixels: a ruled line fills every block of this many pixels along it
_BLOCK_FULL = 230  # a block this full of ink is part of a line
_RULE_SHARE = 0.3  # a pixel row whose full blocks span this share of the page is part of a ruled line
_RULE_MOST = 1.2  # mm: a line at most this thick; a thicker band (a shaded row, a filled header) counts by its edges
_ROW_LEAST = 1.0  # mm: lines closer than this are one line drawn twice
_ROW_SAME = 0.6  # mm: rows within this of one another are of one height
_EDGE = 2  # pixels next to a ruled line left out of the row between lines (its anti-aliasing)
_PART_LEAST = 4  # a column of a row inked this much (of 255) and less than _BLOCK_FULL holds part of something printed
_KIND_MM = 2.0  # ruled lines in the same place within this are the same layout
SHOWN = 5  # examples a measure names at most
KINDS_SHOWN = 10  # kinds of page named one by one
ALONE_WORDS = 40  # the AI line's page with fewer words in all holds little else
# What the agent checks on the pictures before it proposes a paperback, and answers in qa (tools: propose_kdp_book).
# The owner's last words on the book come after it.
CHECKLIST = (
    "White space: no page your owner would call empty; notes reach the bottom margin; nothing alone on a page.",
    "Rows to write in: tall enough to write in by hand (7 to 8 mm), as many as your owner asked for.",
    "Calendar: each week its own calendar week, Monday to Sunday, in the book's year; no week missing.",
    "AI line: there, and not alone on the last page.",
)


@dataclass(frozen=True)
class Page:
    number: int
    height: float  # mm
    rules: tuple[float, ...]  # its ruled lines, mm from its top
    rows: tuple[float, int] | None  # its lowest blank rows to write in: height in mm, how many of them
    blank: tuple[float, float]  # its tallest blank band between printed marks: from (mm down), height (mm)
    text: str
    words: int

    @property
    def blank_share(self) -> float:
        return self.blank[1] / self.height if self.height else 0.0


@dataclass(frozen=True)
class Calendar:
    findings: list[str] = field(default_factory=list)  # what is wrong, each with its page
    weeks: list[int] = field(default_factory=list)  # the week labels found, in page order
    dates: int = 0  # dates read
    year: int | None = None  # the book's year (the one it prints most)

    def text(self) -> str:
        if self.findings:
            more = f" (and {len(self.findings) - SHOWN} more)" if len(self.findings) > SHOWN else ""
            return "; ".join(self.findings[:SHOWN]) + more
        if not self.dates:
            return "no dates in its text"
        found = sorted(set(self.weeks))
        weeks = f"{len(found)} week{'s' if len(found) != 1 else ''} ({_numbers(found)}), each its calendar week; "
        weeks = weeks if found else ""
        return f"{weeks}{self.dates} dates, each real, each weekday its date's"


@dataclass(frozen=True)
class Check:
    pages: list[Page]
    kinds: list[list[int]]  # page numbers of each kind, in the order the kinds first appear
    shown: list[int]  # the pages drawn
    pictures: list[bytes]  # PNGs of the pages drawn, PICTURE_WIDTH wide
    ai_pages: list[int]
    calendar: Calendar
    readable: bool  # its pages hold text Ember's code can read

    def findings(self) -> list[str]:
        """What Ember's code is sure of, for the owner's card too: the calendar's mistakes and a missing AI line."""
        found = list(self.calendar.findings[:SHOWN])
        if self.readable and not self.ai_pages:
            found.append("no page says the book was made with AI")
        return found

    def measures(self) -> list[str]:
        """What Ember's code measured, a line a measure, for the agent."""
        lines = [f"Kinds of page (by their ruled lines): {self.kinds_text()}."]
        rows = sorted((p.rows[0], p.number, p.rows[1]) for p in self.pages if p.rows is not None)
        if rows:
            height, number, count = rows[0]
            text = f"Rows to write in: the lowest {_mm(height)} ({count} on page {number})"
            low = [n for h, n, _ in rows if h < WRITE_LEAST]
            if low:
                text += f"; under {_mm(WRITE_LEAST)}, too low to write in, on pages {_numbers(low)}"
            lines.append(text + ".")
        else:
            lines.append("Rows to write in: none (no blank rows between ruled lines).")
        blank = sorted((p for p in self.pages if p.blank_share > BLANK_SHARE), key=lambda p: -p.blank[1])
        if blank:
            named = ", ".join(f"{p.number} ({_mm(p.blank[1])} from {_mm(p.blank[0])} down)" for p in blank[:SHOWN])
            rest = [p.number for p in blank[SHOWN:]]
            more = f"; also {_numbers(rest)}" if rest else ""
            lines.append(f"White space over a quarter of a page between what it prints: pages {named}{more}.")
        else:
            lines.append("White space: no page has a blank band over a quarter of it between what it prints.")
        if not self.readable:
            lines.append("Calendar, AI line: its pages hold no text Ember's code can read; check them on the pictures.")
            return lines
        lines.append(f"Calendar: {self.calendar.text()}.")
        if self.ai_pages:
            first = self.pages[self.ai_pages[0] - 1]
            last = ", the last page," if first.number == len(self.pages) else ""
            alone = ": little else" if first.words < ALONE_WORDS else ""
            lines.append(
                f"AI line: page {_numbers(self.ai_pages)}; page {first.number}{last} has {first.words} words{alone}."
            )
        else:
            lines.append("AI line: no page says the book was made with AI (AI, KI, artificial intelligence).")
        return lines

    def kinds_text(self) -> str:
        parts = []
        for index, numbers in enumerate(self.kinds[:KINDS_SHOWN]):
            rules = self.pages[numbers[0] - 1].rules
            what = f"{len(rules)} lines" if rules else "no ruled lines"
            drawn = "" if any(n in self.shown for n in numbers) else ", not drawn"
            parts.append(f"{chr(ord('A') + index)} {_numbers(numbers, 6)} ({what}{drawn})")
        rest = self.kinds[KINDS_SHOWN:]
        if rest:
            parts.append(f"{len(rest)} more, first pages {_numbers([numbers[0] for numbers in rest], 8)} (not drawn)")
        return "; ".join(parts)


def named(text: str) -> list[int]:
    """The pages a check is asked to draw ('3, 6, 83'), at most PAGES_SHOWN; [] for one of each kind."""
    words = [w for w in re.split(r"[\s,;]+", text or "") if w]
    if not all(w.isdigit() and int(w) >= 1 for w in words):
        raise ValueError("pages names page numbers, like '3, 6, 83'")
    numbers = list(dict.fromkeys(int(w) for w in words))
    if len(numbers) > PAGES_SHOWN:
        raise ValueError(f"pages names at most {PAGES_SHOWN} pages a check: check again for others")
    return numbers


def _mm(value: float) -> str:
    return f"{value:.1f} mm"


def _numbers(numbers: list[int], most: int = 12) -> str:
    """Page numbers with runs joined: 6-11, 13, 15 (the first ``most`` runs)."""
    runs: list[list[int]] = []
    for number in sorted(numbers):
        if runs and number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    text = ", ".join(f"{r[0]}-{r[-1]}" if len(r) > 1 else str(r[0]) for r in runs[:most])
    return text + (" ..." if len(runs) > most else "")


# --- the pages ---


def check(pdf: bytes, asked: list[int] | None = None, language: str = "") -> Check:
    """The interior's pages measured, and pictures of the pages ``asked`` for (or of one page of each kind, the AI
    line's and the last): drawn now from these bytes, never taken from a file made before. ``language`` is the
    book's (an English book's weeks may run from Sunday). Its pages are a KDP trim size (propose_kdp_book checked
    them first), so each is drawn whole at SCAN_DPI."""
    document = pypdfium2.PdfDocument(pdf)
    try:
        pages = [_measure(document, index) for index in range(len(document))]
        kinds = _kinds(pages)
        ai_pages = [p.number for p in pages if _AI.search(p.text)]
        calendar = read_calendar([p.text for p in pages], language)
        readable = any(p.words for p in pages)
        if asked:
            wrong = [n for n in asked if not 1 <= n <= len(pages)]
            if wrong:
                raise ValueError(f"the interior has {len(pages)} pages, no page {_numbers(wrong)}")
            shown = sorted(dict.fromkeys(asked))[:PAGES_SHOWN]
        else:
            shown = _chosen(pages, kinds, ai_pages, calendar)
        pictures = [_picture(document, number) for number in shown]
    finally:
        document.close()
    return Check(pages, kinds, shown, pictures, ai_pages, calendar, readable)


def _measure(document: pypdfium2.PdfDocument, index: int) -> Page:
    page = document[index]
    try:
        picture = page.render(scale=SCAN_DPI / 72, grayscale=True).to_pil().convert("L")
        textpage = page.get_textpage()
        try:
            text = textpage.get_text_range()
        finally:
            textpage.close()
    finally:
        page.close()
    ink = picture.point(lambda v: 255 if v < INK_LEVEL else 0)
    width, height = ink.size
    rules = _rules(ink)
    return Page(
        number=index + 1,
        height=height * MM_PER_PIXEL,
        rules=tuple(rules),
        rows=_rows(ink, rules),
        blank=_blank(ink),
        text=text.replace("\r\n", "\n"),
        words=len(text.split()),
    )


def _rules(ink: Image.Image) -> list[float]:
    """The page's ruled lines, mm from its top: pixel rows whose ink fills blocks along RULE_SHARE of the page (text
    leaves gaps between its letters and words); a thicker band counts by its top and bottom edges."""
    width, height = ink.size
    blocks = ink.resize((max(1, width // _BLOCK), height), Image.Resampling.BOX)
    full = blocks.point(lambda v: 255 if v >= _BLOCK_FULL else 0).resize((1, height), Image.Resampling.BOX)
    lined = [full.getpixel((0, y)) >= _RULE_SHARE * 255 for y in range(height)]
    rules: list[float] = []
    y = 0
    while y < height:
        if not lined[y]:
            y += 1
            continue
        start = y
        while y < height and lined[y]:
            y += 1
        if (y - start) * MM_PER_PIXEL <= _RULE_MOST:
            rules.append((start + y) / 2 * MM_PER_PIXEL)
        else:
            rules += [start * MM_PER_PIXEL, y * MM_PER_PIXEL]
    return rules


def _rows(ink: Image.Image, rules: list[float]) -> tuple[float, int] | None:
    """The lowest blank rows on the page that come two or more of one height in a row (a table's empty rows, writing
    lines): their height in mm and how many. A single blank gap (under a heading's line) is no row to write in."""
    gaps = []
    for top, bottom in zip(rules, rules[1:], strict=False):
        if bottom - top < _ROW_LEAST:
            continue
        gaps.append((bottom - top) if _empty(ink, top, bottom) else None)
    found: list[tuple[float, int]] = []
    run: list[float] = []
    for gap in [*gaps, None]:
        if gap is not None and (not run or abs(gap - run[0]) <= _ROW_SAME):
            run.append(gap)
            continue
        if len(run) >= 2:
            found.append((sum(run) / len(run), len(run)))
        run = [gap] if gap is not None else []
    return min(found) if found else None


def _empty(ink: Image.Image, top: float, bottom: float) -> bool:
    """Whether nothing is printed between two ruled lines but lines across the whole row (a table's borders): each
    column of the band is inked from top to bottom or not at all. A letter inks part of its column."""
    first, last = int(top / MM_PER_PIXEL) + _EDGE, int(bottom / MM_PER_PIXEL) - _EDGE
    if last - first < 2:
        return False
    columns = ink.crop((0, first, ink.width, last)).resize((ink.width, 1), Image.Resampling.BOX).histogram()
    return not any(columns[_PART_LEAST:_BLOCK_FULL])


def _blank(ink: Image.Image) -> tuple[float, float]:
    """The tallest band with nothing printed between the page's first and last printed pixel rows: from, height (mm).
    The footer's page number is printed, so the white space above it counts."""
    _, rows = ink.getprojection()
    inked = [y for y, on in enumerate(rows) if on]
    best = (0, 0)
    for above, below in zip(inked, inked[1:], strict=False):
        if below - above > best[1]:
            best = (above + 1, below - above - 1)
    return best[0] * MM_PER_PIXEL, best[1] * MM_PER_PIXEL


def _kinds(pages: list[Page]) -> list[list[int]]:
    """Pages of one layout: the same number of ruled lines in the same places (within _KIND_MM); pages without ruled
    lines are one kind."""
    kinds: dict[tuple[int, ...], list[int]] = {}
    for page in pages:
        kinds.setdefault(tuple(round(r / _KIND_MM) for r in page.rules), []).append(page.number)
    return list(kinds.values())


def _chosen(pages: list[Page], kinds: list[list[int]], ai_pages: list[int], calendar: Calendar) -> list[int]:
    """The pages a check draws: the first of each kind, the AI line's page and the last page; past PAGES_SHOWN, those
    with something measured first (white space, low rows, a calendar mistake)."""
    wanted = list(dict.fromkeys([numbers[0] for numbers in kinds] + ai_pages[:1] + [len(pages)]))
    if len(wanted) <= PAGES_SHOWN:
        return sorted(wanted)
    marked = {p.number for p in pages if p.blank_share > BLANK_SHARE or (p.rows and p.rows[0] < WRITE_LEAST)}
    marked |= {int(m[1]) for f in calendar.findings if (m := re.search(r"page (\d+)", f))}
    for numbers in kinds:  # a kind with a marked page shows that page
        hit = next((n for n in numbers if n in marked), None)
        if hit is not None and numbers[0] in wanted:
            wanted[wanted.index(numbers[0])] = hit
    ordered = [n for n in wanted if n in marked] + [n for n in wanted if n not in marked]
    return sorted(list(dict.fromkeys(ordered))[:PAGES_SHOWN])


def _picture(document: pypdfium2.PdfDocument, number: int) -> bytes:
    """Page ``number`` as a PNG, PICTURE_WIDTH pixels wide, on white (the workshop script's rendering)."""
    page = document[number - 1]
    try:
        picture = page.render(scale=PICTURE_WIDTH / page.get_width()).to_pil().convert("RGB")
    finally:
        page.close()
    buffer = io.BytesIO()
    picture.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


# --- the AI line ---

# "AI" and "KI" as capitals only (a word like "ki" isn't the note); the long names in any case
_AI = re.compile(
    r"\b(?:AI|KI)\b|\bA\.I\.|(?i:\b(?:artificial intelligence|künstliche[rn]? intelligenz|intelligence"
    r" artificielle|inteligencia artificial|intelligenza artificiale|inteligência artificial|kunstmatige"
    r" intelligentie)\b)"
)


# --- the calendar ---

_MONTHS = {
    **{"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7},
    **{"august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12},
    **{"jan": 1, "feb": 2, "mär": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10},
    **{"nov": 11, "dez": 12},
    **{"january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7, "october": 10, "december": 12},
    **{"mar": 3, "oct": 10, "dec": 12},
}
_WEEKDAYS = {
    **{"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonnabend": 5},
    **{"sonntag": 6, "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6},
    **{"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6},
    **{"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6},
    **{"tu": 1, "we": 2, "th": 3, "su": 6},
}
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_YEAR = r"(?:\s?(?P<year>\d{4})(?!\d)|(?P<short>\d{2})(?![\d.,]))"
_DATES = (
    # 04.01. / 4.1.2027 / 04.01.27: a day and a month that end in a point (a price, 1.462,50, or 0.39.8 is none)
    re.compile(rf"(?<![\d.,])(?P<day>\d{{1,2}})\.\s?(?P<month>\d{{1,2}})\.{_YEAR}?"),
    re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?!\d)"),  # 2027-01-04
    re.compile(
        rf"(?i)(?<![\d.])(?P<day>\d{{1,2}})(?:\.|st|nd|rd|th)?\s+(?P<name>{_MONTH})\b\.?(?:,?\s+(?P<year>\d{{4}}))?"
    ),
    re.compile(rf"(?i)\b(?P<name>{_MONTH})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{{4}}))?"),
)
# the weekday right before a date (Mo 04.01., Montag, 4. Januar, Fri Jan 8); not the end of a span of days (Mo–So)
_WEEKDAY_BEFORE = re.compile(rf"(?i)(?<![-–—/])\b(?P<weekday>{_WEEKDAY})\.?,?\s*$")
# what joins the two dates of a range: a dash, 'bis' or 'to', and the second one's weekday (Mon 4 Jan - Sun 10 Jan)
_RANGE = re.compile(rf"(?i)^\s*(?:[-–—]|bis|to|until)\s*(?:(?:{_WEEKDAY})\.?,?\s*)?$")
_WEEK = re.compile(r"(?i)\b(?P<label>KW|Kalenderwoche|Woche|Week|Wk)\.?\s*(?P<number>\d{1,2})(?!\d)")
_YEARS = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")
WEEKS_LEAST = 4  # week labels from which a missing week is named


@dataclass(frozen=True)
class _Date:
    raw: str
    day: int
    month: int
    year: int | None  # None: the page doesn't print it
    start: int
    end: int
    weekday: int | None  # the weekday printed before it

    def candidates(self, book_year: int | None) -> list[date]:
        """The real dates it can be: its own year's, or the book's; a yearless date in the last days of December or
        the first week of January may be the year before or after (a calendar week across the new year)."""
        if self.year is not None:
            years = [self.year]
        elif book_year is None:
            return []
        else:
            years = [book_year]
            if self.month == 12 and self.day >= 25:
                years.append(book_year - 1)
            if self.month == 1 and self.day <= 7:
                years.append(book_year + 1)
        found = []
        for year in years:
            try:
                found.append(date(year, self.month, self.day))
            except ValueError:
                continue
        return found


def _dates(line: str) -> list[_Date]:
    found: list[_Date] = []
    for pattern in _DATES:
        for match in pattern.finditer(line):
            if any(match.start() < d.end and d.start < match.end() for d in found):
                continue  # read by an earlier pattern
            parts = match.groupdict()
            month = _MONTHS[parts["name"].lower()] if parts.get("name") else int(parts["month"])
            day = int(parts["day"])
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            year = (
                int(parts["year"])
                if parts.get("year")
                else (2000 + int(parts["short"]) if parts.get("short") else None)
            )
            before = _WEEKDAY_BEFORE.search(line[max(0, match.start() - 16) : match.start()])
            weekday = _WEEKDAYS[before["weekday"].lower()] if before else None
            found.append(_Date(match.group(0).strip(), day, month, year, match.start(), match.end(), weekday))
    return sorted(found, key=lambda d: d.start)


def _iso_week(day: date) -> int:
    return day.isocalendar().week


def _us_week(day: date) -> int:
    """A week from Sunday to Saturday, week 1 the one holding 1 January (a US calendar's)."""
    saturday = day + timedelta(days=(5 - day.weekday()) % 7)
    first = date(saturday.year, 1, 1)
    return (saturday - (first + timedelta(days=(5 - first.weekday()) % 7))).days // 7 + 1


def _sunday_week(day: date) -> int:
    """A week from Sunday to Saturday, week 1 the one starting on the year's first Sunday (as many planners count)."""
    return int(day.strftime("%U"))


def _systems(english: bool) -> tuple[Callable[[date], int], ...]:
    """The ways a week is numbered: ISO's (Monday to Sunday) in every book, a US calendar's too in an English one."""
    return (_iso_week, _us_week, _sunday_week) if english else (_iso_week,)


def read_calendar(texts: list[str], language: str = "") -> Calendar:
    """The dates the pages print, checked against the calendar: each exists, the weekday printed before it is its own,
    a week's label (KW 3, Week 3) is the calendar week of the days it names (ISO's, Monday to Sunday; for an English
    book a US week from Sunday counts too), a range of 7 days is a calendar week, and no week is missing between the
    first and the last. A date without its year is read in the year the book prints most."""
    english = language.lower() == "english"
    years = Counter(int(y) for text in texts for y in _YEARS.findall(text))
    book_year = years.most_common(1)[0][0] if years else None
    findings: list[str] = []
    labels: list[int] = []
    count = 0
    for number, text in enumerate(texts, 1):
        lines = text.replace("\r\n", "\n").split("\n")
        page_dates = [(line, _dates(line)) for line in lines]
        count += sum(len(found) for _, found in page_dates)
        for _, found in page_dates:
            findings += _day_findings(number, found, book_year)
        findings += _week_findings(number, page_dates, book_year, english, labels)
    if len(labels) >= WEEKS_LEAST:
        seen = set(labels)
        missing = [n for n in range(min(seen), max(seen) + 1) if n not in seen]
        if missing:
            findings.append(f"no page for week {_numbers(missing)} (weeks {min(seen)} to {max(seen)} are there)")
    return Calendar(findings, labels, count, book_year)


def _day_findings(number: int, found: list[_Date], book_year: int | None) -> list[str]:
    findings = []
    for d in found:
        options = d.candidates(book_year)
        if d.year is not None and not options:
            findings.append(f"page {number}: {d.raw} doesn't exist")
            continue
        if not options and book_year is None:
            continue
        if not options:
            findings.append(f"page {number}: {d.raw} doesn't exist in {book_year}")
            continue
        if d.weekday is not None and all(day.weekday() != d.weekday for day in options):
            findings.append(
                f"page {number}: {d.raw} is a {DAY_NAMES[options[0].weekday()]}, not a {DAY_NAMES[d.weekday]}"
            )
    return findings


def _week_findings(
    number: int, page: list[tuple[str, list[_Date]]], book_year: int | None, english: bool, labels: list[int]
) -> list[str]:
    """A page's weeks: each label with the days it names (a range after it on its line, or the days that follow it one
    by one), and each range of 7 days."""
    findings = []
    flat = [(index, d) for index, (_, found) in enumerate(page) for d in found]
    for index, (line, found) in enumerate(page):
        ranges = _ranges(line, found)
        for match in _WEEK.finditer(line):
            week = int(match["number"])
            if not 1 <= week <= 53:
                continue
            after = [r for r in ranges if r[0].start >= match.end()]
            if after:
                days = list(after[0])
            else:  # its days one by one on the lines below, until the next label: consecutive days
                below = [d for i, d in flat if i > index or (i == index and d.start >= match.end())]
                days = _consecutive(below, book_year)
            if not days:
                continue
            labels.append(week)
            name = f"{match['label']} {week}"
            wrong = _week_wrong(days, week, book_year, english)
            if wrong:
                findings.append(f"page {number}: {name} ({_shown(days)}) {wrong}")
        for first, last in ranges:
            if any(m.end() <= first.start for m in _WEEK.finditer(line)):
                continue  # a label's range: checked with its label
            spans = _spans(first, last, book_year)
            if (
                spans
                and all((b - a).days == 6 for a, b in spans)
                and all(a.weekday() not in ((0, 6) if english else (0,)) for a, _ in spans)
            ):
                a, b = spans[0]
                findings.append(
                    f"page {number}: the week {first.raw}–{last.raw} runs {DAY_NAMES[a.weekday()]} to "
                    f"{DAY_NAMES[b.weekday()]}"
                )
    return findings


def _ranges(line: str, found: list[_Date]) -> list[tuple[_Date, _Date]]:
    """Two dates joined by a dash, 'bis' or 'to': 04.01.–10.01."""
    return [(a, b) for a, b in zip(found, found[1:], strict=False) if _RANGE.match(line[a.end : b.start])]


def _spans(first: _Date, last: _Date, book_year: int | None) -> list[tuple[date, date]]:
    """The real (start, end) pairs a range can be, its end not before its start and within a year of it."""
    return [
        (a, b)
        for a in first.candidates(book_year)
        for b in last.candidates(book_year)
        if timedelta(0) <= b - a < timedelta(days=366)
    ]


def _consecutive(dates: list[_Date], book_year: int | None) -> list[_Date]:
    """The first dates of a list that follow one another day by day (a week's days, Mo 04.01. Di 05.01. ...), 2 to 7;
    [] when the first two don't."""
    run: list[_Date] = []
    for d in dates[:7]:
        if run and not any((b - a).days == 1 for a in run[-1].candidates(book_year) for b in d.candidates(book_year)):
            break
        run.append(d)
    return run if len(run) >= 2 else []


def _week_wrong(days: list[_Date], week: int, book_year: int | None, english: bool) -> str:
    """What is wrong with week ``week``'s days ("" when they are that calendar week's)."""
    first, last = days[0], days[-1]
    spans = _spans(first, last, book_year)
    if not spans:
        return ""  # a day that doesn't exist, or no year to read it in: named with the day
    if all((b - a).days > 6 for a, b in spans):
        return f"spans {(spans[0][1] - spans[0][0]).days + 1} days"
    if any(number(a) == week == number(b) for a, b in spans for number in _systems(english)):
        return ""
    year = book_year or spans[0][0].year
    weeks = date(year, 12, 28).isocalendar().week  # 52, or 53 in a long year
    if week > weeks:
        return f"isn't a week: {year} has {weeks}"
    monday = date.fromisocalendar(year, week, 1)
    return f"isn't week {week}: week {week} of {year} is Mon {monday.isoformat()} to Sun {monday + timedelta(days=6)}"


def _shown(days: list[_Date]) -> str:
    return days[0].raw if len(days) == 1 else f"{days[0].raw}–{days[-1].raw}"
