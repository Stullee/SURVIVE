"""0.13.0 (Phase E4, the owner's request): Printify. The agent looks through Printify's catalog and proposes a product
made on order (one of its pictures, variants of one print area's shape with their prices, the listing's words); the
owner approves it (the first one is a new kind of business for them: never automatic); Ember's code creates it at
Printify, publishes it to the Etsy shop only if every price keeps its margin after what making and shipping cost, and
never twice; the owner's Undo deletes it; the sync reads the Etsy listing and the orders' costs. The live client is
tested against a mocked Printify; nothing reaches Printify in a dry run."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

httpx2 = pytest.importorskip("httpx2")

from app.agent import econ, metrics, never, stages, tools  # noqa: E402
from app.agent.fake_llm import FakeTransport, request_kind  # noqa: E402
from app.config import Settings  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import etsy, etsy_publisher, printify, printify_publisher, qa  # noqa: E402
from app.integrations.printify import (  # noqa: E402
    DISCLOSURE,
    Gone,
    NotSent,
    PrintifyError,
    Product,
    ShopInfo,
    Unclear,
    Upload,
)
from app.integrations.printify_live import LiveAccount, _Allowlist  # noqa: E402
from app.logging_setup import redact  # noqa: E402
from app.products import images  # noqa: E402
from tests.economy_helpers import owner as owner_entry  # noqa: E402
from tests.test_agent import ROOMY, rows  # noqa: E402
from tests.test_etsy import Etsy, call, shop_context, views_approval  # noqa: E402
from tests.test_loop_shapes import run  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402

PRINTING = ROOMY.model_copy(update={"printify_enabled": True})
LIVE = Settings(printify_enabled=True, printify_api_token="pr1ntify-t0ken-value-xyz")
POSTER, SENSARIA = 282, 2
SMALL, LARGE, SQUAREISH = 43135, 43141, 43150  # 12x18 in and 24x36 in (both 2:3), 11x14 in


def picture(width: int = 2000, height: int = 3000) -> bytes:
    return images.png(Image.new("RGB", (width, height), (250, 245, 235)))


def a_product(**changes: Any) -> Product:
    fields: dict[str, Any] = {
        "title": "Minimalist mountain poster",
        "description": "A calm mountain line drawing, printed on matte paper.",
        "tags": ("mountain poster", "wall art"),
        "blueprint_id": POSTER,
        "provider_id": SENSARIA,
        "prices": ((SMALL, 2490),),
        "shipping": ((SMALL, 450),),
        "image": Upload("shop/poster.png", "a" * 64, 1234),
        "width": 2000,
        "height": 3000,
        "area_width": 3600,
        "area_height": 5400,
        "currency": "EUR",
    }
    fields.update(changes)
    return Product(**fields)


# --- what a product may be ---------------------------------------------------------------------------------------


def test_prices_margins_and_the_picture_are_checked() -> None:
    assert printify.parse_prices(f"{SMALL}: 24.90, #{LARGE} = 39,9") == ((SMALL, 2490), (LARGE, 3990))
    for text, message in (
        ("24.90", "its number, a colon and its price"),
        (f"{SMALL}: 0.50", "between 1.00 and 1000.00"),
        (f"{SMALL}: 20, {SMALL}: 22", "named twice"),
        (", ".join(f"{i}: 20" for i in range(21)), "at most 20"),
        ("", "at least one variant"),
    ):
        with pytest.raises(PrintifyError, match=message):
            printify.parse_prices(text)
    # A price keeps 15% of itself after Etsy's fees (0.15.0: econ's, with VAT), making and shipping (with VAT).
    fees = Decimal(str(econ.fees("etsy_physical", 24.90, econ.DEFAULT_USD_PER_EUR))) * 100
    assert printify.kept(2490, 790, 450) == round(2490 - fees - Decimal(1240) * Decimal("1.19"))
    assert printify.keeps(2490, 790, 450) and not printify.keeps(1290, 790, 450)
    least = printify.least_price(790, 450)
    assert least % 10 == 0 and printify.keeps(least, 790, 450) and not printify.keeps(least - 10, 790, 450)
    # The picture fills the print area's width unless it is relatively taller; how sharp it prints follows.
    product = a_product()
    assert (product.scale(), product.dpi()) == (1.0, 166)
    wide_area = a_product(area_width=3300, area_height=4200)  # 11x14: less tall than the 2:3 picture
    assert wide_area.scale() == round((4200 / 3300) / 1.5, 4) and wide_area.dpi() == int(
        300 * 2000 / (wide_area.scale() * 3300)
    )
    assert qa.defects("printify.create_product", product) == []
    big = a_product(area_width=7200, area_height=10800)
    assert qa.defects("printify.create_product", big) == [
        "it prints at about 83 dpi on the print area (7200 x 10800 pixels at 300 dpi): blurry below 150; use a bigger "
        "picture or a smaller size"
    ]
    assert qa.defects("printify.create_product", a_product(area_width=3300, area_height=4200)) == [
        "the picture (2000 x 3000) and the print area (3300 x 4200) differ in shape: part of the area stays blank"
    ]
    assert printify.product_from_action(json.dumps(product.to_action())) == product
    assert product.full_description().endswith(f"\n\n{DISCLOSURE}")
    with pytest.raises(PrintifyError, match="isn't readable"):
        printify.product_from_action('{"title": "t"}')
    for path, data, message in (
        ("shop/p.pdf", b"x", ".png or .jpg"),
        ("shop/p.png", b"", "is empty"),
        ("shop/p.png", b"x" * (printify.IMAGE_MAX_BYTES + 1), "larger than 15 MB"),
    ):
        with pytest.raises(PrintifyError, match=message):
            printify.image(path, data)


def test_the_shop_is_the_one_connected_to_etsy() -> None:
    shops = [ShopInfo(1, "Mine", "custom_integration"), ShopInfo(2, "Etsy one", "etsy")]
    assert printify.etsy_shop(shops, 0).shop_id == 2
    assert printify.etsy_shop(shops, 1).shop_id == 1  # the owner named one
    with pytest.raises(PrintifyError, match="no shop 9"):
        printify.etsy_shop(shops, 9)
    with pytest.raises(PrintifyError, match="none is connected to Etsy"):
        printify.etsy_shop(shops[:1], 0)
    with pytest.raises(PrintifyError, match="2 are connected to Etsy"):
        printify.etsy_shop([*shops, ShopInfo(3, "Other", "etsy")], 0)
    assert printify.config_problems(Settings()) == ["printify_api_token is missing"]
    assert printify.config_problems(LIVE) == []


# --- the live client, against a mocked Printify ------------------------------------------------------------------


def live(answers: dict[tuple[str, str], Any]) -> tuple[LiveAccount, Etsy]:
    server = Etsy(answers)  # answers requests like an API would, and records them
    return LiveAccount(LIVE, httpx2.MockTransport(server)), server


def test_the_catalog_is_read_with_the_print_areas_and_german_shipping() -> None:
    base = f"/v1/catalog/blueprints/{POSTER}/print_providers/{SENSARIA}"
    account, server = live(
        {
            ("GET", "/v1/shops.json"): [{"id": 77, "title": "PlannerShop", "sales_channel": "etsy"}],
            ("GET", "/v1/catalog/blueprints.json"): [{"id": POSTER, "title": "Matte Vertical Posters", "brand": "X"}],
            ("GET", f"/v1/catalog/blueprints/{POSTER}/print_providers.json"): [{"id": SENSARIA, "title": "Sensaria"}],
            ("GET", f"{base}/variants.json"): {
                "id": SENSARIA,
                "variants": [
                    {
                        "id": SMALL,
                        "title": "12x18 in",
                        "placeholders": [{"position": "front", "width": 3600, "height": 5400}],
                    },
                    {
                        "id": LARGE,
                        "title": "24x36 in",
                        "placeholders": [{"position": "front", "width": 7200, "height": 10800}],
                    },
                    {"id": 999, "title": "No front", "placeholders": [{"position": "back", "width": 1, "height": 1}]},
                    {
                        "id": 998,
                        "title": "Not shipped here",
                        "placeholders": [{"position": "front", "width": 1, "height": 1}],
                    },
                ],
            },
            ("GET", f"{base}/shipping.json"): {
                "profiles": [
                    {"variant_ids": [SMALL], "first_item": {"cost": 450, "currency": "EUR"}, "countries": ["DE", "AT"]},
                    {"variant_ids": [SMALL, LARGE], "first_item": {"cost": 990}, "countries": ["REST_OF_THE_WORLD"]},
                    {"variant_ids": [998], "first_item": {"cost": 100}, "countries": ["US"]},
                ]
            },
        }
    )
    assert account.shops() == [ShopInfo(77, "PlannerShop", "etsy")]
    assert account.blueprints() == [printify.Blueprint(POSTER, "Matte Vertical Posters", "X")]
    assert account.providers(POSTER) == [printify.Provider(SENSARIA, "Sensaria")]
    assert account.variants(POSTER, SENSARIA) == [
        printify.Variant(SMALL, "12x18 in", 3600, 5400, 450, "EUR"),  # Germany's own profile wins
        printify.Variant(LARGE, "24x36 in", 7200, 10800, 990),  # the rest of the world's (0.15.0: no currency stated)
    ]
    first = server.requests[0]
    assert first.headers["authorization"] == "Bearer pr1ntify-t0ken-value-xyz"
    assert first.headers["user-agent"].startswith("Ember/") and first.url.host == "api.printify.com"
    assert redact("token pr1ntify-t0ken-value-xyz") == "token ***"


def test_a_product_is_uploaded_created_published_read_and_deleted() -> None:
    made = {
        "id": "5d39b411749d0a000f30e0f4",
        "visible": False,
        "variants": [
            {"id": SMALL, "cost": 790, "price": 2490, "is_enabled": True},
            {"id": 1, "cost": 5, "is_enabled": False},
        ],
    }
    account, server = live(
        {
            ("POST", "/v1/uploads/images.json"): {"id": "5941187eb8e7e37b3f0e62e5", "file_name": "poster.png"},
            ("POST", "/v1/shops/77/products.json"): made,
            ("POST", f"/v1/shops/77/products/{made['id']}/publish.json"): {},
            ("GET", f"/v1/shops/77/products/{made['id']}.json"): {
                **made,
                "visible": True,
                "external": {"id": "1234567890", "handle": "https://www.etsy.com/listing/1234567890"},
            },
            ("DELETE", f"/v1/shops/77/products/{made['id']}.json"): {},
        }
    )
    data = picture(20, 30)
    image_id = account.upload("poster.png", data)
    assert json.loads(server.requests[0].content) == {
        "file_name": "poster.png",
        "contents": base64.b64encode(data).decode(),
    }
    product = a_product(area_width=3300, area_height=4200)
    created = account.create(77, product, image_id)
    assert created == printify.Made(made["id"], {SMALL: 790})
    body = json.loads(server.requests[1].content)
    assert body["description"] == product.full_description() and body["tags"] == list(product.tags)
    assert body["variants"] == [{"id": SMALL, "price": 2490, "is_enabled": True}]
    assert body["print_areas"] == [
        {
            "variant_ids": [SMALL],
            "placeholders": [
                {
                    "position": "front",
                    "images": [{"id": image_id, "x": 0.5, "y": 0.5, "scale": product.scale(), "angle": 0}],
                }
            ],
        }
    ]
    account.publish(77, made["id"])
    assert json.loads(server.requests[2].content) == {
        "title": True,
        "description": True,
        "images": True,
        "variants": True,
        "tags": True,
    }
    assert account.product(77, made["id"]) == printify.Made(made["id"], {SMALL: 790}, 1234567890, True)
    account.delete(77, made["id"])


def test_orders_are_read_page_by_page() -> None:
    order = {
        "id": "o1",
        "status": "fulfilled",
        "created_at": "2026-09-30 10:00:00+00:00",
        "line_items": [{"product_id": "p1", "quantity": 2, "cost": 1580, "shipping_cost": 450, "status": "fulfilled"}],
    }

    def page(request: Any) -> Any:
        number = int(request.url.params["page"])
        return {
            "data": [order] if number == 1 else [{**order, "id": "o2"}],
            "next_page_url": "?page=2" if number == 1 else None,
        }

    account, server = live({("GET", "/v1/shops/77/orders.json"): page})
    lines = account.orders(77)
    assert [(o.order_id, o.product_id, o.quantity, o.cost_cents, o.shipping_cents) for o in lines] == [
        ("o1", "p1", 2, 1580, 450),
        ("o2", "p1", 2, 1580, 450),
    ]
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx2.Response(400, json={"message": "Validation failed."}), NotSent),
        (httpx2.Response(404, json={"message": "Not found"}), Gone),
        (httpx2.Response(502, json={}), Unclear),
        (httpx2.ReadTimeout("slow"), Unclear),
        (httpx2.ConnectError("down"), NotSent),
    ],
)
def test_errors_say_whether_printify_may_have_made_it(answer: Any, error: type) -> None:
    account, _ = live({("POST", "/v1/shops/77/products.json"): answer})
    with pytest.raises(error):
        account.create(77, a_product(), "img")


def test_only_printify_can_be_reached_and_only_with_a_token() -> None:
    allow = _Allowlist(retries=0)
    for url in ("https://example.com/v1/shops.json", "http://api.printify.com/v1/", "https://api.printify.com:8443/"):
        with pytest.raises(httpx2.ConnectError, match="only talks to"):
            allow.handle_request(httpx2.Request("GET", url))
    with pytest.raises(NotSent, match="token isn't set"):
        LiveAccount(Settings(printify_enabled=True)).shops()


# --- the agent's tools, with the dry run's fake account ------------------------------------------------------------


def listed(data_dir: Path) -> tuple[Any, FakeTransport]:
    """A dry-run agent with Printify on (the fake account) and one live listing in the fake shop."""
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=4, settings=PRINTING, brake=False)  # the fourth cycle's listing (test_etsy)
    request = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")[0]["id"]
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    return agent, fake


def pod_context(agent: Any) -> tools.ToolContext:
    ctx = shop_context(agent)
    ctx.printify = tools.PrintifyAccess("EmberTestShop", "EUR", 2)
    catalog = printify_publisher.Catalog(agent.db, agent.clock, agent.scope().mode, agent.printify.account)
    ctx.catalog = lambda search, blueprint, provider: catalog.answer(search, blueprint, provider, "EUR")
    # the product line of the shop's listing (a new line would need a demand note first)
    ctx.state.focus_project_id = rows(
        agent,
        "SELECT COALESCE(a.project_id, y.project_id) AS id FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id"
        " WHERE a.executor = 'etsy_listing'",
    )[0]["id"]
    return ctx


def read_catalog(ctx: tools.ToolContext) -> None:
    for args in ({"search": "poster"}, {"blueprint_id": POSTER}, {"blueprint_id": POSTER, "provider_id": SENSARIA}):
        assert call(ctx, "printify_catalog", args).ok


def a_proposal(agent: Any, ctx: tools.ToolContext, write: bool = True, **args: Any) -> tools.Outcome:
    image = str(args.pop("image", "shop/poster.png"))
    if write and agent.roots()[0].size_of(image) is None:
        agent.roots()[0].write_bytes(image, picture())
    fields = {
        "blueprint_id": POSTER,
        "provider_id": SENSARIA,
        "prices": f"{SMALL}: 24.90",
        "image": image,
        "title": "Minimalist mountain poster, matte print",
        "description": "A calm mountain line drawing, printed on matte paper and made on order.",
        "tags": "mountain poster, minimalist art, wall art",
        "reason": "Posters of this style sell; it reuses my line drawing.",
        **args,
    }
    return call(ctx, "propose_printify_product", {k: v for k, v in fields.items() if v is not None})


def proposed(data_dir: Path, **args: Any) -> tuple[Any, FakeTransport, int]:
    agent, fake = listed(data_dir)
    ctx = pod_context(agent)
    read_catalog(ctx)
    made = a_proposal(agent, ctx, **args)
    assert made.ok, made.text
    return agent, fake, rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'printify_product'")[0]["id"]


def made_rows(agent: Any) -> list[dict[str, Any]]:
    return rows(agent, "SELECT approval_id, status, product_id, listing_id, prices, error FROM printify_products")


def test_the_catalog_and_a_proposal_are_checked(data_dir: Path) -> None:
    agent, _ = listed(data_dir)
    ctx = pod_context(agent)
    found = call(ctx, "printify_catalog", {"search": "poster"})
    assert found.ok and f"#{POSTER}: Matte Vertical Posters (Generic brand)" in found.text
    refused = a_proposal(agent, ctx)
    assert not refused.ok and "with printify_catalog first" in refused.text
    assert "Sensaria" in call(ctx, "printify_catalog", {"blueprint_id": POSTER}).text
    shown = call(ctx, "printify_catalog", {"blueprint_id": POSTER, "provider_id": SENSARIA})
    assert f"#{SMALL}: 12x18 in: print area 3600 x 5400 pixels, shipping to Germany 4.50 EUR" in shown.text
    assert not call(ctx, "printify_catalog", {"provider_id": SENSARIA}).ok
    agent.roots()[0].write_bytes("shop/notes.pdf", b"%PDF-1.7 x")
    for args, message in (
        ({"prices": "12345: 20"}, "variant 12345 isn't one provider #2 makes"),
        ({"prices": f"{SMALL}: 24.90, {SQUAREISH}: 22.90"}, "different shapes"),
        ({"image": "shop/notes.pdf"}, "a product's picture is a .png or .jpg file"),
        ({"image": "shop/missing.png", "write": False}, "missing.png"),
        ({"tags": ",".join(f"tag{i}" for i in range(14))}, "at most 13"),
        ({"project_id": 9999}, "there is no project #9999"),
    ):
        answer = a_proposal(agent, ctx, **args)
        assert not answer.ok and message in answer.text, (args, answer.text)
    made = a_proposal(agent, ctx, prices=f"{SMALL}: 24.90, {LARGE}: 39.90")
    assert made.ok, made.text
    assert "Nothing is at Printify yet" in made.text and "keeps 15%" in made.text
    assert "QA (Ember's code): it prints at about 83 dpi" in made.text  # the 24x36 in print area
    [request] = rows(agent, "SELECT * FROM approvals WHERE executor = 'printify_product'")
    assert (request["type"], request["title"]) == ("sell", "Printify product: Minimalist mountain poster, matte print")
    assert request["payload"].startswith(
        "Product: Matte Vertical Posters (#282), made by Sensaria (#2)\n"
        "Picture: shop/poster.png (2000 x 3000 pixels, about 83 dpi printed)\n"
        "Variants and prices (shipping to Germany, from Printify's catalog):\n"
        "- 12x18 in (#43135): 24.90 EUR (shipping 4.50 EUR)\n"
        "- 24x36 in (#43141): 39.90 EUR (shipping 6.50 EUR)\n"
    )
    assert request["payload"].endswith(DISCLOSURE)
    with agent.db.connection() as conn:
        assert never.reasons(conn, request) == ["first_publication"]  # physical goods: the owner's decision
    assert views_approval(agent, request["id"])["qa"]


def test_an_approved_product_is_published_once_with_its_margins(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "active")]
    account = agent.printify.account()
    [product_id] = list(account.state["products"])
    [made] = made_rows(agent)
    assert (made["status"], made["product_id"]) == ("active", product_id) and made["listing_id"] > 800_000_000
    assert json.loads(made["prices"]) == [[SMALL, 2490, 790, 450, printify.kept(2490, 790, 450)]]
    closed = rows(agent, f"SELECT status, closed_by, result_note, result_link FROM approvals WHERE id = {request}")[0]
    assert (closed["status"], closed["closed_by"]) == ("done", "Ember")
    assert (
        closed["result_link"] == etsy.listing_url(made["listing_id"])
        and "fake Printify account" in closed["result_note"]
    )
    [entry] = rows(agent, f"SELECT class, status, subject, undo FROM action_journal WHERE approval_id = {request}")
    assert (entry["class"], entry["status"], entry["subject"]) == ("printify.create_product", "simulated", product_id)
    assert json.loads(entry["undo"]) == {"action": "delete_product", "product_id": product_id}
    assert agent.execute_approved() == []  # never twice
    assert views_approval(agent, request)["execution"]["status"] == "active"
    # The next product of the line isn't a first publication any more, and needs no demand note.
    ctx = pod_context(agent)
    assert a_proposal(agent, ctx, title="Minimalist lake poster", image="shop/lake.png").ok
    [second] = rows(agent, f"SELECT * FROM approvals WHERE executor = 'printify_product' AND id > {request}")
    with agent.db.connection() as conn:
        assert never.reasons(conn, second) == []
        assert printify_publisher.listing_ids(conn, agent.scope()) == [made["listing_id"]]


def test_prices_that_keep_too_little_are_never_published(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir, prices=f"{SMALL}: 12.90")
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "failed")]
    assert agent.printify.account().state["products"] == {}  # the unpublished product was deleted
    [made] = made_rows(agent)
    assert made["status"] == "failed" and made["error"] == "prices below the margin"
    note = rows(agent, f"SELECT result_note FROM approvals WHERE id = {request}")[0]["result_note"]
    least = printify.money(printify.least_price(790, 450), "EUR")
    assert f"variant {SMALL} at 12.90 EUR keeps" in note and f"at least {least}" in note
    assert "The unpublished product was deleted at Printify" in note


def test_the_owner_s_undo_deletes_the_product(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "printify.create_product")
    assert entry["undo"] == {"label": "Delete the product", "why_not": None, "request": None}
    reply = owner(agent).undo(entry["id"], "Stefan")
    assert reply.status == 200, reply.body
    undo = int(reply.body["approval_id"])
    made = rows(agent, f"SELECT executor, status, decided_by FROM approvals WHERE id = {undo}")[0]
    assert made == {"executor": "printify_delete", "status": "approved", "decided_by": "Stefan"}
    assert agent.execute_approved() == [(undo, "done")]
    assert agent.printify.account().state["products"] == {} and made_rows(agent)[0]["status"] == "deleted"
    assert views_approval(agent, undo)["execution"]["status"] == "deleted"
    entry = next(e for e in agent.dashboard()["audit"]["feed"] if e["class"] == "printify.create_product")
    assert entry["undo"]["why_not"] == "it is undone"


def test_the_owner_approves_as_it_is_or_cancels_and_a_crash_is_unclear(data_dir: Path) -> None:
    agent, _, request = proposed(data_dir)
    who = owner(agent)
    changed = who.decide(request, {"decision": "approve_with_changes", "final_payload": "Title: mine"}, "Owner")
    assert changed.status == 422 and "approve a product as it is" in changed.body["error"]
    assert who.decide(request, {"decision": "approve"}, "Owner").status == 200
    agent.settings = agent.pod.settings = agent.settings.model_copy(update={"printify_products_per_day": 0})
    assert agent.execute_approved() == [(request, "waiting_limit")]
    done = who.close(request, {"outcome": "done"}, "Owner")
    assert done.status == 422 and "carries approved products out itself" in done.body["error"]
    scope = agent.scope()
    with agent.db.transaction() as conn:  # a crash while creating it
        conn.execute(
            "INSERT INTO printify_products (mode, session, approval_id, title, currency, status, started_at)"
            " VALUES (?, ?, ?, 'Poster', 'EUR', 'running', '2026-09-01T10:00:00Z')",
            (scope.mode, scope.session, request),
        )
    assert who.close(request, {"outcome": "failed"}, "Owner").status == 409  # it is under way
    assert agent.pod.recover() == 1
    assert made_rows(agent)[0]["status"] == "unclear"
    assert rows(agent, f"SELECT status FROM approvals WHERE id = {request}")[0]["status"] == "failed"
    assert agent.execute_approved() == []


def test_the_sync_reads_orders_and_a_product_deleted_at_printify(data_dir: Path) -> None:
    agent, fake, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    account = agent.printify.account()
    [product_id] = list(account.state["products"])
    account.sell(product_id, 2)
    assert agent.pod.sync(force=True) is None
    [order] = agent.integrations()["printify"]["orders"]
    assert (order["quantity"], order["cost_cents"], order["currency"], order["recorded"]) == (
        2,
        2 * 790 + 450,
        "EUR",
        False,
    )
    assert order["key"] == printify_publisher.order_key(order["order_id"])
    now = to_iso(agent.clock.now())
    with agent.db.connection() as conn:
        for name, value in (("pod_products_live", 1), ("pod_orders", 1)):
            row = {"metric": name, "project_id": None, "venture_id": None, "created_at": now}
            assert metrics.read(conn, agent.scope(), row, None, now).value == value  # type: ignore[arg-type]
            assert metrics.CATALOGUE[name].source == "printify"
        # A sale of the listing Printify made counts for the product's project.
        listing_id = printify_publisher.listing_ids(conn, agent.scope())[0]
        project = rows(agent, f"SELECT project_id FROM approvals WHERE id = {request}")[0]["project_id"]
        assert etsy_publisher.order_project(conn, agent.scope(), [{"listing_id": listing_id}])[0] == project
    # The plan shows the products, what each price keeps, and the orders.
    agent.clock.advance(hours=2)
    before = len(fake.sent)
    agent.run_cycle("schedule")
    plan = next(r for r in list(fake.sent)[before:] if request_kind(r) == "plan")
    text = plan["messages"][0]["content"][0]["text"]
    assert "\n== PRINTIFY ==\nPrintify shop: EmberTestShop (prices in EUR; at most 2 products a day).\n" in text
    assert f"variant {SMALL}: 24.90 EUR, making 7.90 EUR + shipping 4.50 EUR, keeps" in text
    assert "Orders of your products at Printify: 1 (1 products in the shop)." in text
    work = next(r for r in list(fake.sent)[before:] if request_kind(r) == "work")
    assert {"printify_catalog", "propose_printify_product"} <= {t["name"] for t in work["tools"]}
    # Deleted at Printify by hand: noted at the next sync.
    account.delete(4242, product_id)
    assert agent.pod.sync(force=True) is None
    assert made_rows(agent)[0]["status"] == "deleted"


def test_printify_s_own_time_format_is_read_and_its_bill_booked(data_dir: Path) -> None:
    """0.21.0: Printify writes an order's time as "2026-09-30 10:00:00+00:00". It was stored as it came, the ledger's
    day couldn't be read from it, and record_costs skipped every order, every sync, without a word: each poster sale
    booked its revenue but not Printify's bill. The fake account wrote Ember's own format, so no test saw it."""
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    account = agent.printify.account()
    [product_id] = list(account.state["products"])
    account.sell(product_id)
    assert account.state["orders"][0]["created_at"] == printify.live_time(agent.clock.now())  # Printify's format
    assert agent.pod.sync(force=True) is None
    stored = rows(agent, "SELECT order_id, created_at FROM printify_orders")
    assert [r["created_at"] for r in stored] == [to_iso(agent.clock.now())]
    on = agent.settings.model_copy(update={"etsy_auto_record_revenue": True, "etsy_usd_per_eur": 1.10})
    scope = agent.scope()
    assert len(printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on)) == 1
    # An order stored before 0.21.0 in Printify's format is read, and the next sync stores it as Ember writes times.
    second = account.sell(product_id)
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO printify_orders (mode, session, order_id, product_id, quantity, cost_cents, shipping_cents,"
            " currency, status, created_at, synced_at) VALUES (?, ?, ?, ?, 1, 790, 450, 'EUR', 'fulfilled', ?, ?)",
            (scope.mode, scope.session, second, product_id, "2026-09-30 10:00:00+00:00", to_iso(agent.clock.now())),
        )
    assert len(printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on)) == 1
    assert agent.pod.sync(force=True) is None
    assert rows(agent, f"SELECT created_at FROM printify_orders WHERE order_id = '{second}'") == [
        {"created_at": to_iso(agent.clock.now())}  # Printify's, as Ember writes times
    ]
    # A time Printify sends that nobody can read is stored as the sync's: booked.
    account.sell(product_id)
    account.state["orders"][-1]["created_at"] = "yesterday"
    assert agent.pod.sync(force=True) is None
    assert len(printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on)) == 1
    # One stored so is never skipped silently: the owner hears of it once.
    fourth = account.sell(product_id)
    with agent.db.transaction() as conn:
        conn.execute(
            "INSERT INTO printify_orders (mode, session, order_id, product_id, quantity, cost_cents, shipping_cents,"
            " currency, status, created_at, synced_at) VALUES (?, ?, ?, ?, 1, 790, 450, 'EUR', 'fulfilled', 'soon',"
            " ?)",
            (scope.mode, scope.session, fourth, product_id, to_iso(agent.clock.now())),
        )
    for _ in range(2):
        assert printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on) == []
    warned = [r for r in rows(agent, "SELECT message FROM events") if "can't read when" in r["message"]]
    assert len(warned) == 1 and fourth in warned[0]["message"] and "'soon'" in warned[0]["message"]


def test_the_cost_of_an_order_cancelled_after_it_was_booked_is_taken_back(data_dir: Path) -> None:
    # 0.23.0: it stayed booked, so the P&L and the runway kept a bill Printify never charged.
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    agent.execute_approved()
    account = agent.printify.account()
    [product_id] = list(account.state["products"])
    first, second = account.sell(product_id), account.sell(product_id)
    assert agent.pod.sync(force=True) is None
    scope = agent.scope()
    on = agent.settings.model_copy(update={"etsy_auto_record_revenue": True, "etsy_usd_per_eur": 1.10})
    key = printify_publisher.order_key(second)  # the owner booked the second one's: it stays theirs to correct
    owner_entry(agent.economy, "expense", "15.00", idempotency_key=key, source=f"Printify order {second}")
    assert len(printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on)) == 1
    for order in account.state["orders"]:
        order["status"] = "canceled"
    assert agent.pod.sync(force=True) is None
    [taken] = printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on)
    assert printify_publisher.record_costs(agent.db, agent.clock, agent.economy, scope, on) == []  # once
    [booked] = rows(agent, f"SELECT * FROM ledger WHERE source = 'Printify order {first}'")
    [back] = rows(agent, f"SELECT * FROM ledger WHERE id = {taken}")
    assert (back["type"], back["corrects_id"], back["amount_micros"]) == ("expense", booked["id"], -13_640_000)
    assert (back["project_id"], back["venture_id"]) == (booked["project_id"], booked["venture_id"])
    assert back["note"] == f"Printify order {first} was cancelled: its cost is taken back"
    assert rows(agent, f"SELECT COUNT(*) AS n FROM ledger WHERE note LIKE '%{second} was cancelled%'")[0]["n"] == 0


def test_the_print_on_demand_venture_s_first_test_is_a_first_order(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=PRINTING)
    with agent.db.transaction() as conn:
        venture = conn.execute("SELECT * FROM ventures WHERE channel = 'printify'").fetchone()
        assert venture is not None and venture["title"] == "Print on demand in the Etsy shop"
        milestone = stages.first_test(conn, agent.scope(), venture, agent.clock.today(), to_iso(agent.clock.now()))
        row = conn.execute("SELECT metric, target FROM milestones WHERE id = ?", (milestone,)).fetchone()
    assert (row["metric"], row["target"]) == ("pod_orders", 1)


def test_the_printify_tools_come_with_the_account_and_a_shop() -> None:
    assert {"printify_catalog", "propose_printify_product"} == tools.PRINTIFY_TOOLS
    assert {d["name"] for d in tools.definitions(etsy=True, printify=True)} >= tools.PRINTIFY_TOOLS
    for kinds in ({"etsy": True}, {"printify": True}, {"etsy": True, "printify": True, "venture": True}):
        assert not {d["name"] for d in tools.definitions(**kinds)} & tools.PRINTIFY_TOOLS
    guide = tools.guide_text("printify")
    assert "below 150 dpi" in guide and "keep 15% of" in guide and "{" not in guide


def test_the_dashboard_shows_nothing_while_printify_is_off(ingress_client: TestClient) -> None:
    shown = ingress_client.get("api/dashboard").json()["integrations"]["printify"]
    assert (shown["status"], shown["products"], shown["orders"]) == ("disabled", [], [])


def test_nothing_is_offered_while_printify_is_off(data_dir: Path) -> None:
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=1)
    assert agent.printify.account() is None and agent.pod.run() == []
    work = next(r for r in fake.sent if request_kind(r) == "work")
    assert not {t["name"] for t in work["tools"]} & tools.PRINTIFY_TOOLS
