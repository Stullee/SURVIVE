"""The QA registry (0.13.0): what a product must have before it goes out, in one place.

The photo thresholds disagreed: the Etsy guide asked for 5 to 10 photos, a lesson said 3 or more, and 0.11.1 flagged
live listings with fewer than 5, while the owner had to point out one-photo listings three times. Now MIN_PHOTOS is
the one number the guide, the live listings' defects (OBLIGATIONS, ETSY SHOP), the qa_clean metric and the checks
below read. ``CHECKS`` holds each action class's checks (connectors.CLASSES): a listing proposed or changed is checked
when it is proposed, and the owner's request card and the agent's tool result say what falls short. 0.13.0 (Phase
E1): an answer to someone who wrote keeps their thread's subject and is short (REPLY_WORDS); (Phase E2) a pin's image
is portrait, about 2:3 (PIN_RATIO); (Phase E4) a Printify product's picture prints sharp (SHARP_DPI) and fills its
print area (SHAPE_SHARE).

0.14.0: photos count as distinct pictures. The agent met MIN_PHOTOS with near-copies of one page, and the owner had to
say so twice. A copy of an earlier photo adds no photo, and the check names it: the same file, or with ``looks``
(images.look), a photo make_image made of the same pages or lines, or one whose pixels look the same.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .etsy import MAX_PHOTOS, Edit, Listing, Upload

MIN_PHOTOS = 5  # a listing's photos at least (Etsy shows up to etsy.MAX_PHOTOS)
REPLY_WORDS = 200  # an email answer's words at most (the footer Ember adds not counted)
PIN_RATIO = (1.3, 1.7)  # a pin's image, height to width: portrait, about 2:3 (1000 x 1500) shows best
SHARP_DPI = 150  # a printed picture below this looks blurry
SHAPE_SHARE = 0.1  # a picture whose shape differs more from its print area's leaves part of it blank
ALIKE_BITS = 10  # 0.14.0: pictures whose difference hashes differ in fewer of their 512 bits look the same (a copy
# resized, re-encoded or in another colour differs in about 4; another page under the same title in 16 or more)
_THREAD = re.compile(r"^\s*(?:re|aw|antw|sv|rif)\s*(?:\[\d+\])?\s*:", re.IGNORECASE)


def photo_defect(count: int, repeated: Sequence[str] = ()) -> str:
    """What falls short of MIN_PHOTOS ("" when nothing does); 0.14.0: ``count`` distinct photos, and the ones that
    repeat another (repeats) named."""
    copies = f"{', '.join(repeated)}: a copy adds no photo" if repeated else ""
    if count >= MIN_PHOTOS:
        return copies
    few = "distinct photo" if repeated else "photo"
    short = f"{count} {few}{'s' if count != 1 else ''}, fewer than {MIN_PHOTOS} (Etsy shows up to {MAX_PHOTOS})"
    return f"{short}; {copies}" if copies else short


def repeats(photos: Sequence[Upload], looks: Sequence[str] | None = None) -> list[str]:
    """0.14.0: the photos that repeat an earlier one, as 'b.png repeats a.png': the same file (its SHA-256) or, with
    ``looks`` (images.look, in the same order, "" where unknown), one that shows or looks the same."""
    marks = list(looks or ())
    found = []
    for index, photo in enumerate(photos):
        mark = marks[index] if index < len(marks) else ""
        for earlier in range(index):
            other = marks[earlier] if earlier < len(marks) else ""
            if photo.sha256 == photos[earlier].sha256 or (mark and other and _alike(mark, other)):
                found.append(f"{photo.path} repeats {photos[earlier].path}")
                break
    return found


def distinct(photos: Sequence[Upload], looks: Sequence[str] | None = None) -> int:
    """0.14.0: how many of the photos are not copies of an earlier one (repeats)."""
    return len(photos) - len(repeats(photos, looks))


def _alike(one: str, other: str) -> bool:
    """Two looks ('mark.hash'): the same things shown, or nearly the same pixels. Two text photos or posters
    make_image noted as showing different words differ, however alike their pixels (titles alone on the same colours).
    0.14.0: other photos are compared by their pixels too (a page under another name or title is no new photo)."""
    one_mark, _, one_bits = one.rpartition(".")
    other_mark, _, other_bits = other.rpartition(".")
    if one_mark and one_mark == other_mark:
        return True
    if one_mark and other_mark and not one_mark.startswith("photo-") and not other_mark.startswith("photo-"):
        return False
    try:
        return len(one_bits) == len(other_bits) and (int(one_bits, 16) ^ int(other_bits, 16)).bit_count() < ALIKE_BITS
    except ValueError:
        return False


def _photos(photos: Sequence[Upload], looks: Sequence[str] | None) -> str:
    repeated = repeats(photos, looks)
    return photo_defect(len(photos) - len(repeated), repeated)


def _listing_photos(listing: Listing, looks: Sequence[str] | None = None) -> str:
    return _photos(listing.photos, looks)


def _edit_photos(edit: Edit, looks: Sequence[str] | None = None) -> str:
    return _photos(edit.photos, looks) if edit.photos is not None else ""


def _reply_subject(action: Mapping[str, Any]) -> str:
    return "" if _THREAD.match(str(action.get("subject") or "")) else "its subject doesn't keep the thread (Re: ...)"


def _reply_length(action: Mapping[str, Any]) -> str:
    words = len(str(action.get("body") or "").split())
    return "" if words <= REPLY_WORDS else f"{words} words, more than {REPLY_WORDS}: an answer is short"


def _pin_shape(pin: Any) -> str:
    width, height = int(getattr(pin, "width", 0) or 0), int(getattr(pin, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return "its image size is unknown"
    if PIN_RATIO[0] <= height / width <= PIN_RATIO[1]:
        return ""
    return f"{width} x {height} pixels: a pin shows best portrait, about 2:3 (1000 x 1500)"


def _print_sharpness(product: Any) -> str:
    dpi = int(product.dpi()) if callable(getattr(product, "dpi", None)) else 0
    if dpi <= 0:
        return "its picture's size is unknown"
    if dpi >= SHARP_DPI:
        return ""
    return (
        f"it prints at about {dpi} dpi on the print area ({product.area_width} x {product.area_height} pixels at "
        f"300 dpi): blurry below {SHARP_DPI}; use a bigger picture or a smaller size"
    )


def _print_shape(product: Any) -> str:
    width, height = int(getattr(product, "width", 0) or 0), int(getattr(product, "height", 0) or 0)
    area_width, area_height = int(getattr(product, "area_width", 0) or 0), int(getattr(product, "area_height", 0) or 0)
    if not (width and height and area_width and area_height):
        return ""
    picture, area = height / width, area_height / area_width
    if abs(picture - area) / area <= SHAPE_SHARE:
        return ""
    return (
        f"the picture ({width} x {height}) and the print area ({area_width} x {area_height}) differ in shape: part of "
        "the area stays blank"
    )


# Each action class's checks: a function of what the request would do, returning what falls short ("" when fine).
CHECKS: dict[str, tuple[Callable[..., str], ...]] = {
    "etsy.create_listing": (_listing_photos,),
    "etsy.edit_listing": (_edit_photos,),
    "email.reply": (_reply_subject, _reply_length),  # the email's action (to, subject, body)
    "pinterest.create_pin": (_pin_shape,),  # the pin (pinterest.Pin)
    "printify.create_product": (_print_sharpness, _print_shape),  # the product (printify.Product)
}


_LOOKING = frozenset({_listing_photos, _edit_photos})  # the checks that see the photos' looks (0.14.0)


def defects(action_class: str, subject: Any, looks: Sequence[str] | None = None) -> list[str]:
    """What the request falls short of, by its class's checks (``looks``: its photos' difference hashes, if known)."""
    return [
        found
        for check in CHECKS.get(action_class, ())
        if (found := check(subject, looks) if check in _LOOKING else check(subject))
    ]
