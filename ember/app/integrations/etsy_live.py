"""The owner's Etsy shop, through Etsy's Open API v3: the only module that talks to Etsy.

Every request goes to https://api.etsy.com (anything else is refused before it leaves, and redirects aren't
followed), with the owner's app key (``x-api-key``: keystring and shared secret) and, for the shop, the OAuth
access token, refreshed when it is about to expire. Errors come back as ``NotSent`` (Etsy refused: nothing changed)
or ``Unclear`` (a timeout or a lost connection: something may have changed). Responses are size-limited and never
logged; tokens and the shared secret are registered for log redaction.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2

from ..config import Settings
from ..economy.clock import Clock, to_iso
from ..logging_setup import register_secret
from .etsy import (
    API_HOST,
    API_URL,
    CATEGORY_JOIN,
    DEAD_ORDERS,
    OAUTH_ENDPOINT,
    QUANTITY,
    REFRESH_EARLY,
    EtsyError,
    Listing,
    NotSent,
    Order,
    RemoteListing,
    ShopInfo,
    TokenFile,
    Tokens,
    Unclear,
    expired,
    listing_url,
    with_disclosure,
)

log = logging.getLogger(__name__)

TIMEOUT = httpx2.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)  # uploads of up to 20 MB
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ACCESS_SECONDS = 3_600  # Etsy's access tokens last an hour
REFRESH_DAYS = 90  # and its refresh tokens 90 days
MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg"}
OFFICE = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
PAGE = 100  # items per page Etsy returns at most
RECEIPT_PAUSE = 1.1  # seconds between pages of orders: Etsy allows about one a second per shop there
_REFRESH_LOCK = threading.Lock()


class _Allowlist(httpx2.HTTPTransport):
    """Refuses every request that isn't HTTPS to api.etsy.com (raised as a connect error)."""

    def handle_request(self, request: Any) -> Any:
        url = request.url
        if url.scheme != "https" or url.host != API_HOST or url.port not in (None, 443):
            raise httpx2.ConnectError(f"Ember only talks to {API_URL} for Etsy, not {url.scheme}://{url.host}")
        return super().handle_request(request)


def _client(transport: Any = None) -> httpx2.Client:
    return httpx2.Client(
        base_url=API_URL,
        transport=transport or _Allowlist(retries=0),
        timeout=TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    )


def _api_key(settings: Settings) -> str:
    secret = settings.etsy_shared_secret.get_secret_value().strip()
    register_secret(secret)
    return f"{settings.etsy_keystring}:{secret}"


def _send(client: httpx2.Client, method: str, path: str, *, changes: bool, **kwargs: Any) -> Any:
    """One request; the JSON answer. ``changes``: whether a failure midway may have changed something."""
    try:
        with client.stream(method, path, **kwargs) as response:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise NotSent("Etsy's answer was too large")
            status = response.status_code
            retry = response.headers.get("retry-after")
    except EtsyError:
        raise
    except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
        raise NotSent(f"Etsy couldn't be reached ({type(exc).__name__})") from None
    except httpx2.HTTPError as exc:
        error = f"the connection to Etsy broke ({type(exc).__name__})"
        raise (Unclear(error) if changes else NotSent(error)) from None
    try:
        data = _json(body) if body else {}
    except ValueError:
        data = {}
    if 200 <= status < 300:
        return data
    detail = (
        str(data.get("error") or data.get("error_description") or "").strip()[:200] if isinstance(data, dict) else ""
    )
    message = f"HTTP {status}" + (f": {detail}" if detail else "")
    if status == 429:
        message += f" (too many requests; retry after {retry or '?'} s)"
    if status >= 500 and changes:
        raise Unclear(message)
    raise NotSent(message)


def _json(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"))


# --- connecting and refreshing -----------------------------------------------------------------------------------


def _token_request(form: dict[str, str], transport: Any) -> dict[str, Any]:
    """The token endpoint takes the app's keystring as client_id and no x-api-key (as clients verified against
    the live API send it)."""
    with _client(transport) as client:
        data = _send(client, "POST", OAUTH_ENDPOINT, changes=False, data=form, headers={"Accept": "application/json"})
    if not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
        raise NotSent("Etsy answered without tokens")
    for value in (data["access_token"], data["refresh_token"]):
        register_secret(str(value))
    return data


def connect(
    settings: Settings, clock: Clock, tokens: TokenFile, code: str, verifier: str, transport: Any = None
) -> ShopInfo:
    """Exchange the authorization code, find the owner's shop and keep the tokens."""
    now = clock.now()
    data = _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": settings.etsy_keystring,
            "redirect_uri": settings.etsy_redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
        transport,
    )
    access = str(data["access_token"])
    user_id = _user_id(access)
    with _client(transport) as client:
        headers = {"x-api-key": _api_key(settings), "Authorization": f"Bearer {access}"}
        me = _send(client, "GET", "/v3/application/users/me", changes=False, headers=headers)
        shop_id = me.get("shop_id") if isinstance(me, dict) else None
        if not isinstance(shop_id, int):
            raise EtsyError("this Etsy account has no shop yet: open one at etsy.com first")
        shop = _send(client, "GET", f"/v3/application/shops/{shop_id}", changes=False, headers=headers)
    info = _shop_info(shop, shop_id)
    tokens.save(
        Tokens(
            access_token=access,
            refresh_token=str(data["refresh_token"]),
            expires_at=to_iso(now + timedelta(seconds=int(data.get("expires_in") or ACCESS_SECONDS))),
            refresh_expires_at=to_iso(now + timedelta(days=REFRESH_DAYS)),
            user_id=user_id or int(me.get("user_id") or 0),
            shop_id=info.shop_id,
            shop_name=info.name,
            currency=info.currency,
            connected_at=to_iso(now),
        )
    )
    return info


def _user_id(access: str) -> int | None:
    """Etsy's access tokens start with the user's id and a dot."""
    head = access.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _shop_info(data: Any, shop_id: int) -> ShopInfo:
    if not isinstance(data, dict):
        raise NotSent("Etsy's answer about the shop wasn't readable")
    name = str(data.get("shop_name") or f"shop {shop_id}")
    currency = str(data.get("currency_code") or "USD")[:3].upper()
    url = str(data.get("url") or f"https://www.etsy.com/shop/{name}")
    return ShopInfo(shop_id=shop_id, name=name, currency=currency, url=url)


class LiveShop:
    simulated = False

    def __init__(self, settings: Settings, clock: Clock, tokens: TokenFile, transport: Any = None) -> None:
        self.settings = settings
        self.clock = clock
        self.tokens = tokens
        self._transport = transport

    # --- the token ---

    def _access(self) -> Tokens:
        tokens = self.tokens.load()
        if tokens is None:
            raise NotSent("the shop isn't connected")
        now = self.clock.now()
        if not expired(tokens.expires_at, now, REFRESH_EARLY):
            return tokens
        # A refresh returns a new refresh token and ends the old one at once: one refresh at a time, saved whole.
        with _REFRESH_LOCK:
            tokens = self.tokens.load()
            if tokens is None:
                raise NotSent("the shop isn't connected")
            if not expired(tokens.expires_at, now, REFRESH_EARLY):
                return tokens  # another thread refreshed it meanwhile
            if expired(tokens.refresh_expires_at, now):
                raise NotSent("the connection to Etsy expired: connect the shop again (System, Etsy)")
            try:
                data = _token_request(
                    {
                        "grant_type": "refresh_token",
                        "client_id": self.settings.etsy_keystring,
                        "refresh_token": tokens.refresh_token,
                    },
                    self._transport,
                )
            except NotSent as exc:
                raise NotSent(f"Etsy didn't renew the connection ({exc}): connect the shop again") from None
            tokens.access_token = str(data["access_token"])
            tokens.refresh_token = str(data["refresh_token"])
            tokens.expires_at = to_iso(now + timedelta(seconds=int(data.get("expires_in") or ACCESS_SECONDS)))
            tokens.refresh_expires_at = to_iso(now + timedelta(days=REFRESH_DAYS))
            self.tokens.save(tokens)
            return tokens

    def _call(self, method: str, path: str, *, changes: bool = False, auth: bool = True, **kwargs: Any) -> Any:
        headers = {"x-api-key": _api_key(self.settings), "Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._access().access_token}"
        with _client(self._transport) as client:
            return _send(client, method, path, changes=changes, headers=headers, **kwargs)

    def _shop_id(self) -> int:
        tokens = self.tokens.load()
        if tokens is None:
            raise NotSent("the shop isn't connected")
        return tokens.shop_id

    # --- the Shop protocol ---

    def info(self) -> ShopInfo:
        shop_id = self._shop_id()
        return _shop_info(self._call("GET", f"/v3/application/shops/{shop_id}"), shop_id)

    def taxonomy(self) -> list[tuple[int, str]]:
        data = self._call("GET", "/v3/application/seller-taxonomy/nodes", auth=False)
        found: list[tuple[int, str]] = []

        def walk(nodes: Any, path: list[str]) -> None:
            for node in nodes if isinstance(nodes, list) else []:
                if not isinstance(node, dict) or not isinstance(node.get("id"), int):
                    continue
                here = [*path, str(node.get("name") or "?")]
                found.append((node["id"], CATEGORY_JOIN.join(here)))
                walk(node.get("children"), here)

        walk(data.get("results") if isinstance(data, dict) else None, [])
        return found

    def create_draft(self, listing: Listing) -> int:
        form: dict[str, Any] = {
            "quantity": str(QUANTITY),
            "title": listing.title,
            "description": with_disclosure(listing.description),
            "price": listing.price,
            "who_made": "i_did",
            "when_made": "made_to_order",
            "taxonomy_id": str(listing.taxonomy_id),
            "type": "download",
            "is_supply": "false",
            "tags": ",".join(listing.tags),
            "should_auto_renew": "false",
        }
        data = self._call("POST", f"/v3/application/shops/{self._shop_id()}/listings", changes=True, data=form)
        listing_id = data.get("listing_id") if isinstance(data, dict) else None
        if not isinstance(listing_id, int):
            raise Unclear("Etsy's answer had no listing number")
        return listing_id

    def upload_photo(self, listing_id: int, name: str, data: bytes, rank: int) -> None:
        mime = MIME.get(_suffix(name), "application/octet-stream")
        self._call(
            "POST",
            f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}/images",
            changes=True,
            files={"image": (name, data, mime)},
            data={"rank": str(rank)},
        )

    def upload_file(self, listing_id: int, name: str, data: bytes, rank: int) -> None:
        mime = MIME.get(_suffix(name)) or OFFICE.get(_suffix(name), "application/octet-stream")
        self._call(
            "POST",
            f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}/files",
            changes=True,
            files={"file": (name, data, mime)},
            data={"name": name, "rank": str(rank)},
        )

    def activate(self, listing_id: int) -> str:
        data = self._call(
            "PATCH",
            f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}",
            changes=True,
            data={"state": "active"},
        )
        return str(data.get("state") or "active") if isinstance(data, dict) else "active"

    def listings(self, listing_ids: list[int]) -> list[RemoteListing]:
        found: list[RemoteListing] = []
        for start in range(0, len(listing_ids), PAGE):
            batch = listing_ids[start : start + PAGE]
            data = self._call(
                "GET", "/v3/application/listings/batch", params={"listing_ids": ",".join(str(i) for i in batch)}
            )
            for item in data.get("results", []) if isinstance(data, dict) else []:
                if not isinstance(item, dict) or not isinstance(item.get("listing_id"), int):
                    continue
                found.append(
                    RemoteListing(
                        listing_id=item["listing_id"],
                        state=str(item.get("state") or "?"),
                        title=str(item.get("title") or ""),
                        url=str(item.get("url") or listing_url(item["listing_id"])),
                        views=_int(item.get("views")),
                        favorites=_int(item.get("num_favorers")),
                        ends_at=_moment(item.get("ending_timestamp")),
                        auto_renew=item["should_auto_renew"]
                        if isinstance(item.get("should_auto_renew"), bool)
                        else None,
                    )
                )
        return found

    def orders(self, since: datetime) -> list[Order]:
        found: list[Order] = []
        offset = 0
        while offset < 500:  # at most five pages a sync
            data = self._call(
                "GET",
                f"/v3/application/shops/{self._shop_id()}/receipts",
                # By change, not by creation (0.12.0): a refund or cancellation changes a receipt, and its stored
                # order has to learn it.
                params={
                    "min_last_modified": str(int(since.timestamp())),
                    "limit": str(PAGE),
                    "offset": str(offset),
                },
            )
            results = data.get("results", []) if isinstance(data, dict) else []
            for receipt in results:
                order = _order(receipt)
                if order is not None:
                    found.append(order)
            if len(results) < PAGE:
                break
            offset += PAGE
            time.sleep(RECEIPT_PAUSE)
        return found

    # --- changing a live listing (0.9.0) ---

    def update_listing(self, listing_id: int, fields: dict[str, str]) -> None:
        self._call("PATCH", f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}", changes=True, data=fields)

    def set_price(self, listing_id: int, price: str) -> None:
        """A listing's price lives in its inventory: a digital listing has one product with one offering, which gets
        the new price (a listing with variations is left to the owner, at Etsy)."""
        path = f"/v3/application/listings/{listing_id}/inventory"
        data = self._call("GET", path)
        products = data.get("products") if isinstance(data, dict) else None
        product = products[0] if isinstance(products, list) and len(products) == 1 else None
        offerings = product.get("offerings") if isinstance(product, dict) else None
        if (
            not isinstance(product, dict)
            or product.get("property_values")
            or not isinstance(offerings, list)
            or len(offerings) != 1
            or not isinstance(offerings[0], dict)
        ):
            raise NotSent("the listing has variations: change its price at Etsy")
        old = offerings[0]
        offering: dict[str, Any] = {
            "price": float(Decimal(price)),
            "quantity": _int(old.get("quantity")) or QUANTITY,
            "is_enabled": old.get("is_enabled") is not False,
        }
        if _int(old.get("readiness_state_id")) is not None:
            offering["readiness_state_id"] = old["readiness_state_id"]
        body = {
            "products": [{"sku": str(product.get("sku") or ""), "property_values": [], "offerings": [offering]}],
            "price_on_property": [],
            "quantity_on_property": [],
            "sku_on_property": [],
        }
        self._call("PUT", path, changes=True, json=body)

    def photo_ids(self, listing_id: int) -> list[int]:
        return _ranked(self._call("GET", f"/v3/application/listings/{listing_id}/images"), "listing_image_id")

    def delete_photo(self, listing_id: int, photo_id: int) -> None:
        path = f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}/images/{photo_id}"
        self._call("DELETE", path, changes=True)

    def file_ids(self, listing_id: int) -> list[int]:
        path = f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}/files"
        return _ranked(self._call("GET", path), "listing_file_id")

    def delete_file(self, listing_id: int, file_id: int) -> None:
        path = f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}/files/{file_id}"
        self._call("DELETE", path, changes=True)

    # --- an order's fees (0.12.0) ---

    def payment_fees(self, receipt_id: int) -> int | None:
        """The processing fee of the order's payment in cents, after any refund's adjustment; None without a payment."""
        data = self._call("GET", f"/v3/application/shops/{self._shop_id()}/receipts/{receipt_id}/payments")
        results = data.get("results") if isinstance(data, dict) else None
        payments = [p for p in results if isinstance(p, dict)] if isinstance(results, list) else []
        if not payments:
            return None
        total = 0
        for payment in payments:
            adjusted = _cents(payment.get("adjusted_fees"))
            total += adjusted if adjusted is not None else (_cents(payment.get("amount_fees")) or 0)
        return total

    # --- a listing's state and renewal (0.12.0) ---

    def set_state(self, listing_id: int, state: str) -> str:
        """'active' puts a listing live again (Etsy renews an expired or sold-out one, for its listing fee);
        'inactive' takes it off the shop. Returns the state Etsy answers with."""
        data = self._call(
            "PATCH",
            f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}",
            changes=True,
            data={"state": state},
        )
        return str(data.get("state") or state) if isinstance(data, dict) else state

    def set_auto_renew(self, listing_id: int, on: bool) -> None:
        self._call(
            "PATCH",
            f"/v3/application/shops/{self._shop_id()}/listings/{listing_id}",
            changes=True,
            data={"should_auto_renew": "true" if on else "false"},
        )


def _order(receipt: Any) -> Order | None:
    """An order with its status: paid ones count; cancelled and refunded ones update what was stored (0.12.0: they
    were dropped, so an order refunded after the sync stayed counted)."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("receipt_id"), int):
        return None
    status = str(receipt.get("status") or "").lower()[:30]
    if receipt.get("is_paid") is False and status not in DEAD_ORDERS:
        status = "unpaid"
    total = receipt.get("grandtotal") if isinstance(receipt.get("grandtotal"), dict) else {}
    created = receipt.get("created_timestamp") or receipt.get("create_timestamp") or 0
    items = []
    for t in receipt.get("transactions") or []:
        if isinstance(t, dict) and isinstance(t.get("listing_id"), int):
            items.append(
                {
                    "listing_id": t["listing_id"],
                    "title": str(t.get("title") or "")[:140],
                    "quantity": _int(t.get("quantity")) or 1,
                    "price_cents": _cents(t.get("price")) or 0,
                }
            )
    refunds = [r for r in receipt.get("refunds") or [] if isinstance(r, dict)]
    return Order(
        receipt_id=receipt["receipt_id"],
        ordered_at=to_iso(datetime.fromtimestamp(int(created), tz=UTC)),
        total_cents=_cents(total) or 0,
        currency=str(total.get("currency_code") or "USD")[:3].upper(),
        items=items,
        status=status or "paid",
        items_cents=_cents(receipt.get("total_price")) or sum(i["price_cents"] * i["quantity"] for i in items),
        discount_cents=_cents(receipt.get("discount_amt")) or 0,
        refunded_cents=sum(_cents(r.get("amount")) or 0 for r in refunds),
    )


def _cents(money: Any) -> int | None:
    """Etsy's money ({"amount": 490, "divisor": 100, "currency_code": "EUR"}) in cents."""
    if not isinstance(money, dict):
        return None
    amount, divisor = _int(money.get("amount")), _int(money.get("divisor")) or 100
    if amount is None:
        return None
    return round(amount * 100 / divisor) if divisor else amount


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _moment(seconds: Any) -> str | None:
    """A Unix time Etsy gives (seconds), as Ember stores times; None when there is none."""
    value = _int(seconds)
    return to_iso(datetime.fromtimestamp(value, tz=UTC)) if value else None


def _ranked(data: Any, key: str) -> list[int]:
    """The numbers (``key``) of a listing's photos or files, in their order."""
    results = data.get("results") if isinstance(data, dict) else None
    items = (
        [r for r in results if isinstance(r, dict) and _int(r.get(key)) is not None]
        if isinstance(results, list)
        else []
    )
    return [r[key] for r in sorted(items, key=lambda r: _int(r.get("rank")) or 0)]


def _suffix(name: str) -> str:
    return name[name.rfind(".") :].lower() if "." in name else ""
