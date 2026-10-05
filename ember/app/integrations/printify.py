"""Printify (0.13.0, Phase E4, the owner's request): physical products with Ember's designs, made on order by a print
provider and sold in the owner's Etsy shop through Printify's Etsy connection.

The owner connects their Etsy shop to Printify (a Printify shop whose sales channel is Etsy), makes a personal access
token for Ember (Printify: My profile, Connections) and sets it in the options with Printify switched on. Then:

* the agent looks through Printify's catalog (``printify_catalog``: products, their print providers, their variants
  with the print area's size and the shipping to Germany), which Ember's code keeps (printify_publisher.py) for the
  proposal's checks;
* it proposes a product (``propose_printify_product``): one of its pictures, the product and provider, the variants
  (of one print area's shape) with their prices, and the listing's title, description and tags (Etsy's rules);
* the owner approves it (Ember's first product is a new kind of business for them, physical goods with duties of their
  own: never automatic);
* Ember's code uploads the picture, creates the product at Printify (not published), reads what each variant costs to
  make, and publishes it to the Etsy shop only if every price keeps MIN_MARGIN after Etsy's fees, making and
  shipping; otherwise it deletes the unpublished product and says what each price would need;
* the sync reads the Etsy listing Printify made and the Printify orders of Ember's products: what they cost to make
  and ship, which the owner pays at Printify.

0.15.0: the margin is checked with econ's fee model (the one the whole app uses: the listing fee, 6.5%, payment
processing and VAT on Etsy's fees), and Printify's bill (making and shipping) carries VAT too, as for a seller without
a VAT ID (the owner's option printify_bill_vat). Who pays the shipping is the owner's option too
(printify_buyer_pays_shipping, as their Etsy shipping profile charges it): by default the check assumes the price alone
pays it (as if the listing shipped free); when the buyer pays it, it is revenue with Etsy's fees on it, and the margin
is a share of price and shipping. Amounts carry the currency Printify states where it states one; shipping in another
currency is converted at the owner's exchange rate, or refused without one. A margin is checked in EUR or USD only.

In dry run (with Printify switched on) a fake account stands in, with a small catalog, its state kept in the database
per dry-run session: nothing reaches Printify.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import PurePosixPath
from typing import Any, Protocol

from ..agent import econ
from ..config import Settings
from ..economy.clock import Clock, to_iso
from .etsy import Upload

API_HOST = "api.printify.com"
API_URL = f"https://{API_HOST}/v1"
IMAGE_KINDS = frozenset({".png", ".jpg"})  # the workspace's pictures
IMAGE_MAX_BYTES = 15 * 1024 * 1024
MAX_VARIANTS = 20  # a product's variants, in one proposal
SHAPE_TOLERANCE = 0.03  # a product's variants share one print area's shape (height to width) within this
PRINT_DPI = 300  # a print area's pixels are for this resolution (below qa.SHARP_DPI a print looks blurry)
# A price must keep this share of itself after Etsy's fees (0.15.0: econ.fees, the app's one fee model), what Printify
# charges to make the variant and its shipping to Germany, with VAT on that bill (BILL_VAT).
MIN_MARGIN = Decimal("0.15")
BILL_VAT = Decimal(str(econ.FEE_VAT))  # 0.15.0: Printify bills VAT to a seller without a VAT ID, as Etsy does
SHIP_TO = "DE"
# Added to every product's description, as to every Etsy listing's (the design is the AI's part; the product is made by
# the print provider).
DISCLOSURE = "This design was made with the help of AI and reviewed by the seller before listing."


class PrintifyError(Exception):
    """Something about a product or the account, in words for the owner and the agent."""


class NotSent(PrintifyError):
    """Printify refused it: nothing changed there."""


class Gone(NotSent):
    """Printify has no such product (deleted there)."""


class Unclear(PrintifyError):
    """A timeout or a lost connection: something may have changed there."""


@dataclass(frozen=True)
class Blueprint:
    blueprint_id: int
    title: str
    brand: str


@dataclass(frozen=True)
class Provider:
    provider_id: int
    title: str


@dataclass(frozen=True)
class Variant:
    variant_id: int
    title: str
    width: int  # the front print area's pixels (at PRINT_DPI)
    height: int
    shipping_cents: int  # the first item's shipping to SHIP_TO, as Printify charges it
    currency: str = ""  # 0.15.0: the currency Printify states for it ("": none stated)


@dataclass(frozen=True)
class Product:
    """A checked product: exactly what Ember's code creates at Printify and publishes to the Etsy shop."""

    title: str
    description: str
    tags: tuple[str, ...]
    blueprint_id: int
    provider_id: int
    prices: tuple[tuple[int, int], ...]  # (variant id, price in cents): the variants it sells
    shipping: tuple[tuple[int, int], ...]  # (variant id, shipping to SHIP_TO in cents), from the catalog
    image: Upload
    width: int  # the picture's pixels
    height: int
    area_width: int  # the variants' print area (the largest), for the placement and the QA registry
    area_height: int
    currency: str
    position: str = "front"
    billed_in: str = ""  # 0.15.0: the currency Printify states for the variants ("": printify_currency's)

    def to_action(self) -> dict[str, Any]:
        data = asdict(self)
        data["image"] = asdict(self.image)
        data["tags"] = list(self.tags)
        data["prices"] = [list(p) for p in self.prices]
        data["shipping"] = [list(s) for s in self.shipping]
        return data

    def full_description(self) -> str:
        """The description as the Etsy listing shows it: with the AI line."""
        return f"{self.description.rstrip()}\n\n{DISCLOSURE}"

    def scale(self) -> float:
        """The picture's width on the print area (1: as wide as it), so that all of it fits: a picture taller than
        the area's shape is made narrower."""
        if not (self.width and self.height and self.area_width and self.area_height):
            return 1.0
        return round(min(1.0, (self.area_height / self.area_width) / (self.height / self.width)), 4)

    def dpi(self) -> int:
        """How sharp the picture prints at the print area's size (dots per inch)."""
        if not (self.width and self.area_width):
            return 0
        return int(PRINT_DPI * self.width / (self.scale() * self.area_width))


def product_from_action(raw: str | dict[str, Any]) -> Product:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise PrintifyError("the product isn't readable")
    try:
        image = data["image"]
        return Product(
            title=str(data["title"]),
            description=str(data["description"]),
            tags=tuple(str(t) for t in data["tags"]),
            blueprint_id=int(data["blueprint_id"]),
            provider_id=int(data["provider_id"]),
            prices=tuple((int(v), int(p)) for v, p in data["prices"]),
            shipping=tuple((int(v), int(c)) for v, c in data["shipping"]),
            image=Upload(str(image["path"]), str(image["sha256"]), int(image["bytes"])),
            width=int(data["width"]),
            height=int(data["height"]),
            area_width=int(data["area_width"]),
            area_height=int(data["area_height"]),
            currency=str(data["currency"]),
            position=str(data.get("position") or "front"),
            billed_in=str(data.get("billed_in") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PrintifyError(f"the product isn't readable ({type(exc).__name__})") from None


def image(path: str, data: bytes) -> Upload:
    """A workspace picture for a product, with its SHA-256: Ember's code uploads exactly this file."""
    if PurePosixPath(path).suffix.lower() not in IMAGE_KINDS:
        raise PrintifyError(f"{path}: a product's picture is a .png or .jpg file")
    if not data:
        raise PrintifyError(f"{path} is empty")
    if len(data) > IMAGE_MAX_BYTES:
        raise PrintifyError(f"{path} is larger than {IMAGE_MAX_BYTES // (1024 * 1024)} MB")
    return Upload(path, hashlib.sha256(data).hexdigest(), len(data))


_PRICE_ITEM = re.compile(r"^\s*#?(\d{1,12})\s*[:=]\s*(\d{1,4}(?:[.,]\d{1,2})?)\s*$")
_NEXT_ITEM = re.compile(r",\s*(?=#?\d{1,12}\s*[:=])")  # a comma before the next variant (a price may have one)


def parse_prices(text: str) -> tuple[tuple[int, int], ...]:
    """'43135: 24.90, 43141: 29,90' -> ((43135, 2490), (43141, 2990)). Raises PrintifyError."""
    found: list[tuple[int, int]] = []
    for part in [p for p in _NEXT_ITEM.split(text.strip().strip(",")) if p.strip()]:
        match = _PRICE_ITEM.match(part)
        if match is None:
            raise PrintifyError(f"{part.strip()!r}: give each variant as its number, a colon and its price")
        cents = int((Decimal(match.group(2).replace(",", ".")) * 100).to_integral_value(rounding=ROUND_HALF_UP))
        if not 100 <= cents <= 100_000:
            raise PrintifyError(f"{part.strip()!r}: a price is between 1.00 and 1000.00")
        found.append((int(match.group(1)), cents))
    if not found:
        raise PrintifyError("name at least one variant with its price")
    if len(found) > MAX_VARIANTS:
        raise PrintifyError(f"at most {MAX_VARIANTS} variants in one product")
    if len({v for v, _ in found}) != len(found):
        raise PrintifyError("a variant is named twice")
    return tuple(found)


def money(cents: int, currency: str) -> str:
    return f"{cents / 100:.2f} {currency}"


def convert(cents: int, currency: str, to: str, usd_per_eur: float = 0.0) -> int:
    """0.15.0: an amount Printify states in ``currency`` ("": none stated), in ``to``: between USD and EUR at the
    owner's rate (etsy_usd_per_eur), rounded up (it is a cost). It was taken to be in printify_currency's currency.
    Raises PrintifyError when it can't be converted."""
    if not currency or currency == to:
        return cents
    pair = {currency, to} == {"USD", "EUR"}
    if not pair or usd_per_eur <= 0:
        why = "your owner set no exchange rate (etsy_usd_per_eur)" if pair else "Ember's code can't convert it"
        raise PrintifyError(f"Printify states it in {currency}, not {to} (printify_currency), and {why}")
    rate = Decimal(str(usd_per_eur))
    value = Decimal(cents) / rate if currency == "USD" else Decimal(cents) * rate
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def fees(price_cents: int, currency: str = "EUR", usd_per_eur: float = 0.0) -> Decimal:
    """0.15.0: Etsy's fees on a sale at this price (cents), by econ.fees: the listing fee (USD 0.20, at the owner's
    rate or econ's assumed one), 6.5%, payment processing (4% and 0.30) and VAT on Etsy's fees. It was about 10.5% and
    0.50, less than the fee model the rest of the app uses. Raises PrintifyError for a currency other than EUR or USD
    (the listing fee can't be converted to it)."""
    if currency not in ("EUR", "USD"):
        raise PrintifyError(f"Ember's code can't check a margin in {currency} (only EUR or USD)")
    rate = 1.0 if currency == "USD" else usd_per_eur if usd_per_eur > 0 else econ.DEFAULT_USD_PER_EUR
    return Decimal(str(econ.fees("etsy_physical", price_cents / 100, rate))) * 100


@dataclass(frozen=True)
class Terms:
    """0.15.0: how a sale is reckoned, from the owner's options: the currency, the exchange rate, whether the buyer pays
    the shipping on top of the price, and whether Printify's bill carries VAT."""

    currency: str = "EUR"
    usd_per_eur: float = 0.0
    buyer_ships: bool = False
    bill_vat: bool = True

    def said(self) -> str:
        """What the check assumes, for the approval card and the agent (no word the NEVER list reads as legal)."""
        pays = "the buyer pays the shipping" if self.buyer_ships else "the price alone pays the shipping"
        return f"{pays}; Printify's bill {f'plus {BILL_VAT * 100:.0f}%' if self.bill_vat else 'as billed'}"


DEFAULT_TERMS = Terms()
# 0.15.0: Etsy's fees the check counts, for the approval card (econ's fee table); Offsite Ads aren't counted
FEES_SAID = (
    f"Etsy's fees counted: the {econ.LISTING_FEE_USD:.2f} USD listing fee, {econ.TRANSACTION_SHARE * 100:g}%, "
    f"{econ.PROCESSING_SHARE * 100:g}% + {econ.PROCESSING_EUR:.2f} for payments, and {econ.FEE_VAT * 100:g}% on the "
    "first two. Not counted: Offsite Ads (12-15% of a sale they bring)."
)


def terms(settings: Settings) -> Terms:
    return Terms(
        settings.printify_currency,
        settings.etsy_usd_per_eur,
        settings.printify_buyer_pays_shipping,
        settings.printify_bill_vat,
    )


def kept(price_cents: int, cost_cents: int, shipping_cents: int, sale: Terms = DEFAULT_TERMS) -> int:
    """What a sale at this price keeps after Etsy's fees, making and shipping, with VAT on Printify's bill if it
    carries VAT (cents). When the buyer pays the shipping, it is revenue and Etsy's fees apply to it."""
    bill = Decimal(cost_cents + shipping_cents) * (1 + (BILL_VAT if sale.bill_vat else 0))
    paid = price_cents + (shipping_cents if sale.buyer_ships else 0)
    left = Decimal(paid) - fees(paid, sale.currency, sale.usd_per_eur) - bill
    return int(left.to_integral_value(rounding=ROUND_HALF_UP))


def keeps(price_cents: int, cost_cents: int, shipping_cents: int, sale: Terms = DEFAULT_TERMS) -> bool:
    """Whether a sale keeps MIN_MARGIN of what the buyer pays (the price, and the shipping when the buyer pays it) after
    Etsy's fees, making and shipping."""
    paid = price_cents + (shipping_cents if sale.buyer_ships else 0)
    return kept(price_cents, cost_cents, shipping_cents, sale) >= Decimal(paid) * MIN_MARGIN


def least_price(cost_cents: int, shipping_cents: int, sale: Terms = DEFAULT_TERMS) -> int:
    """The lowest price (cents, a multiple of 10) that keeps MIN_MARGIN. Raises PrintifyError (fees)."""
    price = max(100, (cost_cents + (0 if sale.buyer_ships else shipping_cents)) // 10 * 10)
    while not keeps(price, cost_cents, shipping_cents, sale):
        price += 10  # counted up from what it costs, so it holds whatever econ's fee table is
    return price


def payload(
    product: Product, blueprint: str, provider: str, variants: dict[int, str], sale: Terms = DEFAULT_TERMS
) -> str:
    """The request as the owner reads and approves it."""
    shipping = dict(product.shipping)
    lines = [
        f"Product: {blueprint} (#{product.blueprint_id}), made by {provider} (#{product.provider_id})",
        f"Picture: {product.image.path} ({product.width} x {product.height} pixels, about {product.dpi()} dpi printed)",
        "Variants and prices (shipping to Germany, from Printify's catalog):",
        *(
            f"- {variants.get(v, f'#{v}')} (#{v}): {money(c, product.currency)}"
            f" (shipping {money(shipping.get(v, 0), product.currency)})"
            for v, c in product.prices
        ),
        # 0.15.0: what the check assumes (no word the NEVER list reads as legal: the fee model is in the docs)
        f"Published only if each price keeps {MIN_MARGIN * 100:.0f}% after Etsy's fees, making and shipping "
        f"({sale.said()}).",
        FEES_SAID,
        f"Title: {product.title}",
        f"Tags: {', '.join(product.tags)}",
        "",
        product.full_description(),
    ]
    return "\n".join(lines)


# --- the account ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShopInfo:
    shop_id: int
    title: str
    channel: str  # "etsy" for a shop connected to Etsy


@dataclass(frozen=True)
class Made:
    """A product as Printify has it: its number, what each variant costs to make, and its Etsy listing once
    published."""

    product_id: str
    costs: dict[int, int]  # variant id -> cents
    listing_id: int | None = None
    visible: bool = False


@dataclass(frozen=True)
class OrderLine:
    order_id: str
    product_id: str
    quantity: int
    cost_cents: int  # making, for the whole line
    shipping_cents: int
    status: str
    created_at: str
    tax_cents: int = 0  # 0.15.0: the line's share of the tax Printify bills on the order
    currency: str = ""  # 0.15.0: the order's currency, if Printify states one


class Account(Protocol):
    simulated: bool

    def shops(self) -> list[ShopInfo]: ...

    def blueprints(self) -> list[Blueprint]: ...

    def providers(self, blueprint_id: int) -> list[Provider]: ...

    def variants(self, blueprint_id: int, provider_id: int) -> list[Variant]: ...

    def upload(self, name: str, data: bytes) -> str: ...  # the picture's id at Printify

    def create(self, shop_id: int, product: Product, image_id: str) -> Made: ...

    def publish(self, shop_id: int, product_id: str) -> None: ...

    def product(self, shop_id: int, product_id: str) -> Made: ...

    def delete(self, shop_id: int, product_id: str) -> None: ...

    def orders(self, shop_id: int) -> list[OrderLine]: ...


def config_problems(settings: Settings) -> list[str]:
    return [] if settings.printify_api_token.get_secret_value().strip() else ["printify_api_token is missing"]


def etsy_shop(shops: list[ShopInfo], shop_id: int) -> ShopInfo:
    """The Printify shop Ember sells through: the one the options name, or the only one connected to Etsy."""
    if shop_id:
        found = next((s for s in shops if s.shop_id == shop_id), None)
        if found is None:
            raise PrintifyError(f"Printify has no shop {shop_id} for this token: check printify_shop_id")
        return found
    etsy = [s for s in shops if s.channel.lower() == "etsy"]
    if len(etsy) != 1:
        found = "none is" if not etsy else f"{len(etsy)} are"
        raise PrintifyError(f"{found} connected to Etsy at Printify: connect one, or set printify_shop_id")
    return etsy[0]


# --- the dry run's fake account ---------------------------------------------------------------------------------

FAKE_CATALOG: dict[str, Any] = {
    "blueprints": [
        [282, "Matte Vertical Posters", "Generic brand"],
        [68, "Mug 11oz", "Generic brand"],
        [485, "Hardcover Journal Matte", "Generic brand"],
    ],
    "providers": {"282": [[2, "Sensaria"]], "68": [[1, "SPOKE Custom Products"]], "485": [[28, "Print Clever"]]},
    # variant id, title, print area width and height, cost to make (cents), shipping to DE (cents)
    "variants": {
        "282:2": [
            [43135, "12x18 in", 3600, 5400, 790, 450],
            [43141, "24x36 in", 7200, 10800, 1490, 650],
            [43150, "11x14 in", 3300, 4200, 690, 450],
        ],
        "68:1": [[33719, "11oz / White", 2475, 1155, 480, 590]],
        "485:28": [[62155, "5.75 x 8 in / Lined", 2750, 3825, 890, 390]],
    },
}


class FakePrintify:
    """The dry run's account: FAKE_CATALOG, one shop connected to Etsy, products kept in the database
    (``on_change``). A published product gets a fake Etsy listing number at once. Nothing reaches Printify."""

    simulated = True

    def __init__(self, clock: Clock, state: dict[str, Any] | None, on_change: Callable[[dict[str, Any]], None]) -> None:
        self.clock = clock
        self.state: dict[str, Any] = state or {"products": {}, "images": {}, "next": 1, "orders": []}
        self._on_change = on_change

    def _next(self) -> int:
        number = int(self.state["next"])
        self.state["next"] = number + 1
        return number

    def shops(self) -> list[ShopInfo]:
        return [ShopInfo(4242, "EmberTestShop", "etsy")]

    def blueprints(self) -> list[Blueprint]:
        return [Blueprint(int(i), str(t), str(b)) for i, t, b in FAKE_CATALOG["blueprints"]]

    def providers(self, blueprint_id: int) -> list[Provider]:
        found = FAKE_CATALOG["providers"].get(str(blueprint_id))
        if found is None:
            raise Gone("no such product in the catalog")
        return [Provider(int(i), str(t)) for i, t in found]

    def variants(self, blueprint_id: int, provider_id: int) -> list[Variant]:
        found = FAKE_CATALOG["variants"].get(f"{blueprint_id}:{provider_id}")
        if found is None:
            raise Gone("this provider doesn't make it")
        return [Variant(int(v), str(t), int(w), int(h), int(s), "EUR") for v, t, w, h, _cost, s in found]

    def _cost(self, blueprint_id: int, provider_id: int, variant_id: int) -> int:
        for v, _title, _w, _h, cost, _ship in FAKE_CATALOG["variants"].get(f"{blueprint_id}:{provider_id}", []):
            if v == variant_id:
                return int(cost)
        raise NotSent(f"variant {variant_id} isn't made by this provider")

    def upload(self, name: str, data: bytes) -> str:
        image_id = f"{self._next():024x}"
        self.state["images"][image_id] = {"name": name, "bytes": len(data)}
        self._on_change(self.state)
        return image_id

    def create(self, shop_id: int, product: Product, image_id: str) -> Made:
        if image_id not in self.state["images"]:
            raise NotSent("no such picture")
        costs = {v: self._cost(product.blueprint_id, product.provider_id, v) for v, _ in product.prices}
        product_id = f"{self._next():024x}"
        self.state["products"][product_id] = {
            "shop_id": shop_id,
            "title": product.title,
            "costs": {str(v): c for v, c in costs.items()},
            "listing_id": None,
            "visible": False,
            "created_at": to_iso(self.clock.now()),
        }
        self._on_change(self.state)
        return Made(product_id, costs)

    def publish(self, shop_id: int, product_id: str) -> None:
        item = self.state["products"].get(product_id)
        if item is None:
            raise Gone("no such product")
        item["listing_id"], item["visible"] = 800_000_000 + self._next(), True
        self._on_change(self.state)

    def product(self, shop_id: int, product_id: str) -> Made:
        item = self.state["products"].get(product_id)
        if item is None:
            raise Gone("no such product")
        costs = {int(v): int(c) for v, c in item["costs"].items()}
        return Made(product_id, costs, item["listing_id"], bool(item["visible"]))

    def delete(self, shop_id: int, product_id: str) -> None:
        if self.state["products"].pop(product_id, None) is None:
            raise Gone("no such product")
        self._on_change(self.state)

    def orders(self, shop_id: int) -> list[OrderLine]:
        return [OrderLine(**o) for o in self.state["orders"]]

    def sell(self, product_id: str, quantity: int = 1) -> str:
        """A buyer's order (for tests and the dry run's demo): what Printify then charges to make and ship it."""
        item = self.state["products"][product_id]
        order_id = f"{self._next():024x}"
        cost = min(int(c) for c in item["costs"].values())
        self.state["orders"].append(
            {
                "order_id": order_id,
                "product_id": product_id,
                "quantity": quantity,
                "cost_cents": cost * quantity,
                "shipping_cents": 450,
                "status": "fulfilled",
                "created_at": live_time(self.clock.now()),  # 0.21.0: as Printify writes it, not as Ember does
            }
        )
        self._on_change(self.state)
        return order_id


def live_time(moment: datetime) -> str:
    """0.21.0: a time as Printify's API writes it (``2026-09-30 10:00:00+00:00``)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00:00")
