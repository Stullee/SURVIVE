"""The QA registry (0.13.0): what a product must have before it goes out, in one place.

The photo thresholds disagreed: the Etsy guide asked for 5 to 10 photos, a lesson said 3 or more, and 0.11.1 flagged
live listings with fewer than 5, while the owner had to point out one-photo listings three times. Now MIN_PHOTOS is
the one number the guide, the live listings' defects (OBLIGATIONS, ETSY SHOP), the qa_clean metric and the checks
below read. ``CHECKS`` holds each action class's checks (connectors.CLASSES): a listing proposed or changed is checked
when it is proposed, and the owner's request card and the agent's tool result say what falls short.
"""

from __future__ import annotations

from collections.abc import Callable

from .etsy import MAX_PHOTOS, Edit, Listing

MIN_PHOTOS = 5  # a listing's photos at least (Etsy shows up to etsy.MAX_PHOTOS)


def photo_defect(count: int) -> str:
    """What falls short of MIN_PHOTOS ("" when nothing does)."""
    if count >= MIN_PHOTOS:
        return ""
    return f"{count} photo{'s' if count != 1 else ''}, fewer than {MIN_PHOTOS} (Etsy shows up to {MAX_PHOTOS})"


def _listing_photos(listing: Listing) -> str:
    return photo_defect(len(listing.photos))


def _edit_photos(edit: Edit) -> str:
    return photo_defect(len(edit.photos)) if edit.photos is not None else ""


# Each action class's checks: a function of what the request would do, returning what falls short ("" when fine).
CHECKS: dict[str, tuple[Callable[..., str], ...]] = {
    "etsy.create_listing": (_listing_photos,),
    "etsy.edit_listing": (_edit_photos,),
}


def defects(action_class: str, subject: Listing | Edit) -> list[str]:
    """What the request falls short of, by its class's checks."""
    return [found for check in CHECKS.get(action_class, ()) if (found := check(subject))]
