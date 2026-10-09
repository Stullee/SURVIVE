"""The plan tree's templates (0.34.0): for each type of product, its stages, what "done" means for each, and the
usual steps that get there.

A product (a project row: a product line) is laid out from its type's template the first time Ember's code sees it
(plan.py). The stages are research, create, release, launch and maintain; each stage's check says what done means,
read from records Ember's code keeps (a demand note, a request to the owner, live listings, pins and posts), so a stage
never closes on words. The steps are the usual way there; research may change them in Release 2b, the checks stay.
Maintain's steps recur (weekly marketing, the critic's fixes): plan.py adds them, the template lays out none.

Templates are versioned: a product keeps the version it was laid out with (``key``), so a changed template never
rewrites a plan that is under way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STAGES = ("research", "create", "release", "launch", "maintain")
# The checks plan.py reads (check_kind), with their parameters (check_spec):
CHECKS = {
    "demand": "a demand note for the product from the last `days` days",
    "built": "a product file made by one of `tools` (at least `count`), without a Check: line",
    "request": "a request to the owner for the product, by `executor`, made (or `status`: done)",
    "live": "at least `count` live listings of the product",
    "photos": "each live listing of the product has at least `count` photos",
    "critic": "the product's newest quality check passes",
    "pin": "at least `count` pins live that link one of the product's listings",
    "post": "at least `count` Bluesky posts live that link one of the product's listings",
    "blog": "at least `count` blog posts online that recommend one of the product's listings",
    "kdp_check": "a propose_kdp_book package check that passed",
    "obligation": "the promise to the owner was kept (its obligation closed)",
    "any": "one of `of` (checks) passes",
    "agent": "Ember says it is done (shown as hers)",
}
OWNER_KIND = "owner"  # a step that is the owner's to take (approve, publish): it waits on them


@dataclass(frozen=True)
class StepT:
    key: str  # unique within its template ('files', 'propose')
    title: str
    kind: str  # weights.KIND, or OWNER_KIND
    check: str  # a key of CHECKS
    spec: dict[str, Any] = field(default_factory=dict)
    channel: str | None = None  # a marketing step: 'pinterest', 'bluesky' or 'blog'
    audiences: tuple[str, ...] = ("en", "de", "both")  # laid out only for these audiences
    waiting: str | None = None  # waits from the start ('upgrade': a tool Ember lacks)
    effort: int = 1


@dataclass(frozen=True)
class StageT:
    stage: str
    done: str  # what done means, in words for the owner
    check: str | None = None  # the stage's own check; None: done once all its steps are
    spec: dict[str, Any] = field(default_factory=dict)
    steps: tuple[StepT, ...] = ()


@dataclass(frozen=True)
class Template:
    name: str
    version: int
    platform: str  # the project it goes under: 'etsy', 'kdp', 'printify', 'website', 'channels', 'other'
    title: str
    could_earn: float  # $ a month when no record says more (the agent's own sub-goal, the business case)
    stages: tuple[StageT, ...]

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def stage(self, name: str) -> StageT:
        return next(s for s in self.stages if s.stage == name)


_DEMAND = StageT(
    "research",
    "done when a demand note from the last 14 days: searches, competitors, prices",
    "demand",
    {"days": 14},
    (StepT("demand", "Write the demand note: searches, competitors, prices", "create", "demand", {"days": 14}),),
)
_PINS = StepT("pins", "Pin it twice, with different pictures", "market", "pin", {"count": 2}, "pinterest")
_POSTS = StepT(
    "posts", "Post it twice on Bluesky, linking the listing", "market", "post", {"count": 2}, "bluesky", ("en", "both")
)
_BLOG = StepT("blog", "A German blog post with its product box", "market", "blog", {"count": 1}, "blog", ("de", "both"))
_CRITIC = StepT("critic", "The critic passes it", "fix", "critic")
_MAINTAIN = StageT("maintain", "it recurs (weekly marketing, the critic's fixes) until you drop the product")

ETSY_DIGITAL = Template(
    "etsy_digital",
    1,
    "etsy",
    "Etsy download",
    5.0,
    (
        _DEMAND,
        StageT(
            "create",
            "done when the files and 5 photos are made and looked at, and the listing proposed",
            "any",
            {"of": [{"check": "request", "executor": "etsy_listing"}, {"check": "live", "count": 1}]},
            (
                StepT(
                    "files",
                    "Make the product files, in the audience's languages, and check them",
                    "create",
                    "built",
                    {"tools": ["make_document", "make_spreadsheet", "make_cost_statement"], "count": 1},
                    effort=2,
                ),
                StepT(
                    "photos", "Make 5 photos and look at each", "create", "built", {"tools": ["make_image"], "count": 5}
                ),
            ),
        ),
        StageT(
            "release",
            "done when you approved the listing and it is live",
            "live",
            {"count": 1},
            (
                StepT("propose", "Propose the listing", "ship", "request", {"executor": "etsy_listing"}),
                StepT("approve", "You approve it, and it goes live", OWNER_KIND, "live", {"count": 1}),
            ),
        ),
        StageT(
            "launch",
            "done when the first week's marketing is out in its channels and the critic passes it",
            None,
            {},
            (_PINS, _POSTS, _BLOG, _CRITIC),
        ),
        _MAINTAIN,
    ),
)

PRINTIFY_POD = Template(
    "printify_pod",
    1,
    "printify",
    "Printify product",
    5.0,
    (
        _DEMAND,
        StageT(
            "create",
            "done when the print file and a room mockup are made, and the product proposed",
            "any",
            {"of": [{"check": "request", "executor": "printify_product"}, {"check": "live", "count": 1}]},
            (
                StepT(
                    "print",
                    "Make the print file at the print area's full resolution",
                    "create",
                    "built",
                    {"tools": ["make_image"], "count": 1},
                ),
                StepT(
                    "mockup",
                    "Make a room mockup for pins and posts",
                    "create",
                    "built",
                    {"tools": ["make_image"], "count": 2},
                ),
            ),
        ),
        StageT(
            "release",
            "done when you approved the product and it is live",
            "live",
            {"count": 1},
            (
                StepT("propose", "Propose the product", "ship", "request", {"executor": "printify_product"}),
                StepT("approve", "You approve it, and it goes live", OWNER_KIND, "live", {"count": 1}),
            ),
        ),
        StageT(
            "launch",
            "done when the first week's marketing is out and the critic passes it (its fixes are yours to make)",
            None,
            {},
            (_PINS, _POSTS, _CRITIC),
        ),
        _MAINTAIN,
    ),
)

KDP_BOOK = Template(
    "kdp_book",
    1,
    "kdp",
    "KDP book",
    10.0,
    (
        StageT(
            "research",
            "done when a demand note from the last 14 days: Amazon searches, competing books",
            "demand",
            {"days": 14},
            (
                StepT(
                    "demand",
                    "Write the demand note: Amazon searches, competing books",
                    "create",
                    "demand",
                    {"days": 14},
                ),
            ),
        ),
        StageT(
            "create",
            "done when the interior PDF and the front picture pass KDP's package check",
            "any",
            {"of": [{"check": "kdp_check"}, {"check": "request", "executor": "kdp_package"}]},
            (
                StepT(
                    "interior",
                    "Interior PDF: 6x9, KDP's margins, an AI page",
                    "create",
                    "built",
                    {"tools": ["make_document"], "count": 1},
                    effort=2,
                ),
                StepT("cover", "Make the front picture", "create", "built", {"tools": ["make_image"], "count": 1}),
                StepT("check", "Write the spec and run the package check", "create", "kdp_check"),
            ),
        ),
        StageT(
            "release",
            "done when you published it and its Amazon link is in",
            "request",
            {"executor": "kdp_package", "status": "done"},
            (
                StepT("propose", "Propose the book", "ship", "request", {"executor": "kdp_package"}),
                StepT(
                    "publish",
                    "You publish it at KDP and add its Amazon link",
                    OWNER_KIND,
                    "request",
                    {"executor": "kdp_package", "status": "done"},
                ),
            ),
        ),
        StageT(
            "launch",
            "done when the first marketing is out (pins, posts and the blog can't link Amazon yet)",
            None,
            {},
            (StepT("blog", "A German blog post naming the book", "market", "agent", {}, "blog", waiting="upgrade"),),
        ),
        _MAINTAIN,
    ),
)

SITE_CONTENT = Template(
    "site_content",
    1,
    "website",
    "Website content",
    2.0,
    (
        StageT(
            "research",
            "done when a demand note from the last 14 days: what people search for",
            "demand",
            {"days": 14},
            (StepT("demand", "Write the demand note: the searches a post answers", "create", "demand", {"days": 14}),),
        ),
        StageT(
            "release",
            "done when a post is online on your site",
            "request",
            {"executor": "site_post", "status": "done"},
            (StepT("post", "Propose the post", "ship", "request", {"executor": "site_post"}),),
        ),
        _MAINTAIN,
    ),
)

CHANNEL = Template(
    "channel",
    1,
    "channels",
    "Channel",
    1.0,
    (
        StageT(
            "create",
            "done when the channel is set up",
            None,
            {},
            (StepT("setup", "Set the channel up with your owner", "create", "agent"),),
        ),
        _MAINTAIN,
    ),
)

GENERIC = Template(
    "generic",
    1,
    "other",
    "Product",
    2.0,
    (
        _DEMAND,
        StageT("create", "done when what it needs is made", None, {}, (StepT("make", "Make it", "create", "agent"),)),
        StageT("release", "done when it is out", None, {}, (StepT("ship", "Put it out", "ship", "agent"),)),
        _MAINTAIN,
    ),
)

TEMPLATES = {t.name: t for t in (ETSY_DIGITAL, PRINTIFY_POD, KDP_BOOK, SITE_CONTENT, CHANNEL, GENERIC)}
PLATFORMS = {
    "etsy": "Etsy",
    "kdp": "KDP",
    "printify": "Printify",
    "website": "Website",
    "channels": "Channels",
    "other": "Other",
    "ventures": "Ventures",  # 0.36.0: the Explore step's project (plan.py)
}


def by_key(key: str | None) -> Template:
    """The template a product was laid out with ('kdp_book@1'); an unknown one reads as the generic template."""
    name, _, version = (key or "").partition("@")
    found = TEMPLATES.get(name)
    if found is None or str(found.version) != version:
        return GENERIC
    return found


def steps_for(stage: StageT, audience: str | None) -> tuple[StepT, ...]:
    """A stage's steps for a product's audience (a marketing step only where its channel reaches that audience)."""
    return tuple(s for s in stage.steps if audience is None or audience in s.audiences)
