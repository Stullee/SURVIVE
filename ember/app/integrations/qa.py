"""The QA registry (0.13.0): what a product must have before it goes out, in one place.

The photo thresholds disagreed: the Etsy guide asked for 5 to 10 photos, a lesson said 3 or more, and 0.11.1 flagged
live listings with fewer than 5, while the owner had to point out one-photo listings three times. Now MIN_PHOTOS is
the one number the guide, the live listings' defects (OBLIGATIONS, ETSY SHOP), the qa_clean metric and the checks
below read. ``CHECKS`` holds each action class's checks (connectors.CLASSES): a listing proposed or changed is checked
when it is proposed, and the owner's request card and the agent's tool result say what falls short. 0.13.0 (Phase
E1): an answer to someone who wrote keeps their thread's subject and is short (REPLY_WORDS); (Phase E2) a pin's image
is portrait, about 2:3 (PIN_RATIO).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from .etsy import MAX_PHOTOS, Edit, Listing

MIN_PHOTOS = 5  # a listing's photos at least (Etsy shows up to etsy.MAX_PHOTOS)
REPLY_WORDS = 200  # an email answer's words at most (the footer Ember adds not counted)
PIN_RATIO = (1.3, 1.7)  # a pin's image, height to width: portrait, about 2:3 (1000 x 1500) shows best
_THREAD = re.compile(r"^\s*(?:re|aw|antw|sv|rif)\s*(?:\[\d+\])?\s*:", re.IGNORECASE)


def photo_defect(count: int) -> str:
    """What falls short of MIN_PHOTOS ("" when nothing does)."""
    if count >= MIN_PHOTOS:
        return ""
    return f"{count} photo{'s' if count != 1 else ''}, fewer than {MIN_PHOTOS} (Etsy shows up to {MAX_PHOTOS})"


def _listing_photos(listing: Listing) -> str:
    return photo_defect(len(listing.photos))


def _edit_photos(edit: Edit) -> str:
    return photo_defect(len(edit.photos)) if edit.photos is not None else ""


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


# Each action class's checks: a function of what the request would do, returning what falls short ("" when fine).
CHECKS: dict[str, tuple[Callable[..., str], ...]] = {
    "etsy.create_listing": (_listing_photos,),
    "etsy.edit_listing": (_edit_photos,),
    "email.reply": (_reply_subject, _reply_length),  # the email's action (to, subject, body)
    "pinterest.create_pin": (_pin_shape,),  # the pin (pinterest.Pin)
}


def defects(action_class: str, subject: Any) -> list[str]:
    """What the request falls short of, by its class's checks."""
    return [found for check in CHECKS.get(action_class, ()) if (found := check(subject))]
