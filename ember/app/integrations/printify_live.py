"""The owner's Printify account, through Printify's API v1: the only module that talks to Printify.

Every request goes to https://api.printify.com (anything else is refused before it leaves, and redirects aren't
followed), with the owner's personal access token (registered for log redaction). Errors come back as ``NotSent``
(Printify refused: nothing changed; ``Gone`` for what isn't there) or ``Unclear`` (a timeout or a lost connection:
something may have changed). Responses are size-limited and never logged.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx2

from ..config import Settings
from ..logging_setup import register_secret
from ..version import app_version
from .printify import (
    API_HOST,
    API_URL,
    SHIP_TO,
    Blueprint,
    Gone,
    Made,
    NotSent,
    OrderLine,
    PrintifyError,
    Product,
    Provider,
    ShopInfo,
    Unclear,
    Variant,
)

log = logging.getLogger(__name__)

TIMEOUT = httpx2.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # the catalog's list of products is long
ORDER_PAGES = 5  # of orders, at most, per sync
EVERYWHERE = "REST_OF_THE_WORLD"  # a shipping profile for the countries no other names


class _Allowlist(httpx2.HTTPTransport):
    """Refuses every request that isn't HTTPS to api.printify.com (raised as a connect error)."""

    def handle_request(self, request: Any) -> Any:
        url = request.url
        if url.scheme != "https" or url.host != API_HOST or url.port not in (None, 443):
            raise httpx2.ConnectError(f"Ember only talks to {API_URL} for Printify, not {url.scheme}://{url.host}")
        return super().handle_request(request)


def _send(client: httpx2.Client, method: str, path: str, *, changes: bool, **kwargs: Any) -> Any:
    try:
        with client.stream(method, path, **kwargs) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise NotSent("Printify's answer was too large")
            status = response.status_code
    except PrintifyError:
        raise
    except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
        raise NotSent(f"Printify couldn't be reached ({type(exc).__name__})") from None
    except httpx2.HTTPError as exc:
        error = f"the connection to Printify broke ({type(exc).__name__})"
        raise (Unclear(error) if changes else NotSent(error)) from None
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        data = {}
    if 200 <= status < 300:
        return data
    detail = str(data.get("message") or data.get("error") or "").strip()[:200] if isinstance(data, dict) else ""
    message = f"HTTP {status}" + (f": {detail}" if detail else "")
    if status >= 500 and changes:
        raise Unclear(message)
    raise (Gone if status == 404 else NotSent)(message)


def _ids(rows: Any) -> list[dict[str, Any]]:
    """The objects of a list answer that have a whole-number id."""
    items = rows if isinstance(rows, list) else []
    return [r for r in items if isinstance(r, dict) and isinstance(r.get("id"), int) and not isinstance(r["id"], bool)]


class LiveAccount:
    simulated = False

    def __init__(self, settings: Settings, transport: Any = None) -> None:
        self.settings = settings
        self._transport = transport  # tests only

    def _call(self, method: str, path: str, *, changes: bool = False, **kwargs: Any) -> Any:
        token = self.settings.printify_api_token.get_secret_value().strip()
        if not token:
            raise NotSent("the Printify token isn't set")
        register_secret(token)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"Ember/{app_version()} (Home Assistant app)",
        }
        with httpx2.Client(
            base_url=API_URL,
            transport=self._transport or _Allowlist(retries=0),
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            return _send(client, method, path, changes=changes, headers=headers, **kwargs)

    def shops(self) -> list[ShopInfo]:
        return [
            ShopInfo(int(s["id"]), str(s.get("title") or "")[:100], str(s.get("sales_channel") or "")[:40])
            for s in _ids(self._call("GET", "/shops.json"))
        ]

    def blueprints(self) -> list[Blueprint]:
        return [
            Blueprint(int(b["id"]), str(b.get("title") or "")[:200], str(b.get("brand") or "")[:100])
            for b in _ids(self._call("GET", "/catalog/blueprints.json"))
        ]

    def providers(self, blueprint_id: int) -> list[Provider]:
        rows = self._call("GET", f"/catalog/blueprints/{blueprint_id}/print_providers.json")
        return [Provider(int(p["id"]), str(p.get("title") or "")[:200]) for p in _ids(rows)]

    def variants(self, blueprint_id: int, provider_id: int) -> list[Variant]:
        """The variants with a front print area that ship to Germany (their first item's shipping, in the currency
        Printify states for it: 0.15.0, it was taken to be the printify_currency option's)."""
        base = f"/catalog/blueprints/{blueprint_id}/print_providers/{provider_id}"
        data = self._call("GET", f"{base}/variants.json")
        shipping = self._call("GET", f"{base}/shipping.json")
        costs: dict[int, tuple[int, str]] = {}
        profiles = shipping.get("profiles") if isinstance(shipping, dict) else None
        for country in (SHIP_TO, EVERYWHERE):  # a profile naming Germany wins over the rest of the world's
            for profile in profiles if isinstance(profiles, list) else []:
                if not isinstance(profile, dict) or country not in (profile.get("countries") or []):
                    continue
                first = profile.get("first_item") if isinstance(profile.get("first_item"), dict) else {}
                cost, currency = first.get("cost"), str(first.get("currency") or "")[:3].upper()
                for variant_id in profile.get("variant_ids") or []:
                    if isinstance(variant_id, int) and isinstance(cost, int):
                        costs.setdefault(variant_id, (cost, currency))
        found = []
        for v in _ids(data.get("variants") if isinstance(data, dict) else None):
            front = next(
                (p for p in v.get("placeholders") or [] if isinstance(p, dict) and p.get("position") == "front"),
                None,
            )
            if front is None or v["id"] not in costs:
                continue  # nothing to print on its front, or it isn't shipped to Germany
            try:
                width, height = int(front["width"]), int(front["height"])
            except (KeyError, TypeError, ValueError):
                continue
            cost, currency = costs[v["id"]]
            found.append(Variant(int(v["id"]), str(v.get("title") or "")[:100], width, height, cost, currency))
        return found

    def upload(self, name: str, data: bytes) -> str:
        body = {"file_name": name, "contents": base64.b64encode(data).decode("ascii")}
        answer = self._call("POST", "/uploads/images.json", changes=True, json=body)
        if not isinstance(answer, dict) or not answer.get("id"):
            raise Unclear("Printify's answer about the picture wasn't readable")
        return str(answer["id"])[:40]

    def create(self, shop_id: int, product: Product, image_id: str) -> Made:
        body = {
            "title": product.title,
            "description": product.full_description(),
            "tags": list(product.tags),
            "blueprint_id": product.blueprint_id,
            "print_provider_id": product.provider_id,
            "variants": [{"id": v, "price": c, "is_enabled": True} for v, c in product.prices],
            "print_areas": [
                {
                    "variant_ids": [v for v, _ in product.prices],
                    "placeholders": [
                        {
                            "position": product.position,
                            "images": [{"id": image_id, "x": 0.5, "y": 0.5, "scale": product.scale(), "angle": 0}],
                        }
                    ],
                }
            ],
        }
        return _made(self._call("POST", f"/shops/{shop_id}/products.json", changes=True, json=body))

    def publish(self, shop_id: int, product_id: str) -> None:
        body = {"title": True, "description": True, "images": True, "variants": True, "tags": True}
        self._call("POST", f"/shops/{shop_id}/products/{product_id}/publish.json", changes=True, json=body)

    def product(self, shop_id: int, product_id: str) -> Made:
        return _made(self._call("GET", f"/shops/{shop_id}/products/{product_id}.json"))

    def delete(self, shop_id: int, product_id: str) -> None:
        self._call("DELETE", f"/shops/{shop_id}/products/{product_id}.json", changes=True)

    def orders(self, shop_id: int) -> list[OrderLine]:
        found: list[OrderLine] = []
        for page in range(1, ORDER_PAGES + 1):
            data = self._call("GET", f"/shops/{shop_id}/orders.json", params={"page": page, "limit": 50})
            rows = data.get("data") if isinstance(data, dict) else None
            for order in rows if isinstance(rows, list) else []:
                if not isinstance(order, dict):
                    continue
                items = [i for i in order.get("line_items") or [] if isinstance(i, dict) and i.get("product_id")]
                bill = sum(_number(i.get("cost")) + _number(i.get("shipping_cost")) for i in items)
                for line in items:
                    found.append(_line(order, line, bill))
            if not isinstance(data, dict) or not data.get("next_page_url"):
                break
        return found


def _number(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _line(order: dict[str, Any], line: dict[str, Any], bill: int) -> OrderLine:
    """A line of an order, with its share of the order's tax (0.15.0: by what it costs of the order's ``bill``)."""
    cost, shipping = _number(line.get("cost")), _number(line.get("shipping_cost"))
    tax = _number(order.get("total_tax"))
    return OrderLine(
        order_id=str(order.get("id") or "")[:40],
        product_id=str(line["product_id"])[:40],
        quantity=_number(line.get("quantity")),
        cost_cents=cost,
        shipping_cents=shipping,
        status=str(line.get("status") or order.get("status") or "")[:40],
        created_at=str(order.get("created_at") or "")[:40],
        tax_cents=round(tax * (cost + shipping) / bill) if bill else 0,
        currency=str(order.get("currency") or "")[:3].upper(),
    )


def _made(data: Any) -> Made:
    if not isinstance(data, dict) or not data.get("id"):
        raise Unclear("Printify's answer about the product wasn't readable")
    costs = {int(v["id"]): _number(v.get("cost")) for v in _ids(data.get("variants")) if v.get("is_enabled")}
    external = data.get("external") if isinstance(data.get("external"), dict) else {}
    listing = str(external.get("id") or "")
    return Made(str(data["id"])[:40], costs, int(listing) if listing.isdigit() else None, bool(data.get("visible")))
