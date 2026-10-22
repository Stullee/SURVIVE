"""0.42.2 (analysis-0.40.1, 5.6): the tools of what Ember sells, split out of tools.py.

Etsy (etsy_categories, propose_etsy_listing, demand_note, etsy_listing, propose_etsy_edit), Printify
(printify_catalog, propose_printify_product, propose_printify_edit) and Amazon KDP (propose_kdp_book, and its check:
a paperback's pages drawn fresh, pagecheck.py). Each proposal is an approval request the owner decides, and Ember's
code (or the owner, for a KDP book) carries it out.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any

from ..integrations import etsy, etsy_publisher, kdp, printify, printify_publisher, qa

# Imported here, at startup: the PDF page renderer loads a native library, which a sealed tool call may not do.
from ..products import images, make, pagecheck
from . import claims, demand, plan, preconditions, store, ventures
from .sandbox import SandboxError
from .store import AgentScope
from .tools_context import Drawn, EtsyAccess, KdpAccess, Outcome, PrintifyAccess, ToolContext, ToolError, _cut, _local
from .tools_files import _mtime
from .tools_limits import (
    CATEGORIES_SHOWN,
    DEPARTMENT,
    DESCRIPTION_MAX,
    DRAWN_ROOM,
    KDP_NOTE,
    MAX_RESULT_CHARS,
    QA_CHARS,
    QA_WORDS,
)
from .tools_lines import _line, _listing_line, _listing_stopped, _stopped
from .tools_requests import _at_once, _new_request, _refuse, _unlocked


def _etsy_categories(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    shop = _shop(ctx)
    if not shop.categories:
        raise ToolError("Etsy's category list isn't loaded yet; it is fetched at the start of the next wake cycle")
    found = etsy.search_categories(shop.categories, args["search"], limit=len(shop.categories))
    if not found:
        return Outcome(True, f"No category holds all of: {args['search']}. Try fewer or broader words.", "none")
    shown = found[:CATEGORIES_SHOWN]
    lines = "\n".join(f"{i}: {path}" + (DEPARTMENT if etsy.department(path) else "") for i, path in shown)
    text = f"Categories (number: path):\n{lines}"
    if len(found) > len(shown):
        text += f"\n...and {len(found) - len(shown)} more with longer paths (more specific): add a word to see them."
    return Outcome(True, text, f"{len(found)} categories")


def _category(shop: EtsyAccess, category_id: int) -> str:
    """The path of the category a listing goes in; numbers that aren't Etsy categories, and whole top-level
    departments (a listing proposed in 'Accessories' went live there, 0.9.0), are refused."""
    path = dict(shop.categories).get(category_id)
    if path is None:
        raise ToolError(f"category {category_id} isn't an Etsy category; find one with etsy_categories")
    if etsy.department(path):
        raise ToolError(
            f"category {category_id} is {path}, a whole department of Etsy's; find the specific category buyers look "
            "in with etsy_categories"
        )
    return path


def _propose_etsy_listing(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    shop = _shop(ctx)
    category = _category(shop, args["category_id"])
    try:
        listing = etsy.Listing(
            title=etsy.check_title(args["title"]),
            description=etsy.check_description(args["description"]),
            price=etsy.check_price(args["price"]),
            currency=shop.currency,
            tags=etsy.check_tags(args["tags"]),
            taxonomy_id=args["category_id"],
            category=category,
            files=_uploads(ctx, args["files"], etsy.FILE_KINDS, etsy.MAX_FILES, "the files buyers download"),
            photos=_uploads(ctx, args["photos"], etsy.PHOTO_KINDS, etsy.MAX_PHOTOS, "the photos"),
        )
    except etsy.EtsyError as exc:
        raise ToolError(str(exc)) from None
    reason = args["reason"].strip()
    # 0.12.0: a listing belongs to a product line (a project), and a product line's first listing needs a demand note.
    project_id = _product_line(ctx, conn, args, "listing")
    _refuse(ctx, conn, preconditions.DEMAND, line=project_id)  # 0.40.3: the plan reads the same rule
    joined = ventures.adopt(conn, ctx.scope, project_id, "etsy", ctx.now())  # 0.15.0
    made = _new_request(
        ctx,
        conn,
        etsy.payload(listing),
        listing.to_action(),
        project_id=project_id,
        type="sell",
        title=_cut(f"Etsy listing: {listing.title}", 120),
        description=reason,
        expected_cost="Etsy's listing fee (USD 0.20), and Etsy's fees on each sale",
        expected_benefit=reason,
        executor="etsy_listing",
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate listing")
    note = _unlocked(ctx)
    then = f" Ember's code creates it in {shop.shop_name} (at most {shop.daily_limit} a day) and you hear the result."
    done = _at_once(conn, made, note, then)
    if done is not None:
        return Outcome(True, done + _joined(project_id, joined), f"#{made} Etsy listing: {_cut(listing.title, 60)}")
    text = (
        f"Approval request #{made} is waiting for your owner, in the category {category} (#{listing.taxonomy_id}). "
        f"Nothing is on Etsy yet. If they approve it, Ember's code creates the listing in {shop.shop_name} (at most "
        f"{shop.daily_limit} a day) and you hear the result."
    )
    looks = images.looks(ctx.workspace.read_bytes, [(u.path, u.sha256) for u in listing.photos])  # 0.15.0
    short = qa.defects("etsy.create_listing", listing, looks)  # 0.13.0: the QA registry; your owner sees it too
    if short:
        text += f" QA (Ember's code): {'; '.join(short)}: make more with make_image and change the request."
    text += _joined(project_id, joined) + note
    return Outcome(True, text, f"#{made} Etsy listing: {_cut(listing.title, 60)}")


def _product_line(ctx: ToolContext, conn: Any, args: dict[str, Any], what: str) -> int:
    """The project (product line) a new listing or product joins: the one named, else the cycle's focus project, but
    (0.19.2) not one of another venture than the cycle's: live, a cycle on venture #12 with a cover-letter project in
    focus listed #12's licence bundle in that project, whose quality checks and listing test then read the bundle."""
    project_id = args.get("project_id")
    if project_id is None:
        project_id = ctx.state.focus_project_id
        if project_id is None:
            raise ToolError(f"name its project (project_id): each {what} belongs to a product line")
        focus = store.project(conn, ctx.scope, project_id)
        venture = ctx.state.focus_venture_id
        if focus is not None and venture is not None and focus["venture_id"] not in (None, venture):
            raise ToolError(
                f"name its project (project_id): your focus project #{project_id} belongs to venture "
                f"#{focus['venture_id']}, and this cycle works on venture #{venture}"
            )
    row = store.project(conn, ctx.scope, project_id)
    if row is None:
        raise ToolError(f"there is no project #{project_id}")
    # 0.23.2: a listing or product for it carried its work on; 0.23.3: a line of no venture in a stopped channel too;
    # 0.28.0: a KDP book sells in no channel of Printify's (a park of print on demand stopped it)
    refused = _stopped(conn, ctx.scope, int(project_id), {"listing": "etsy", "product": "printify"}.get(what))
    if refused:
        raise ToolError(refused)
    _line(ctx, conn, int(project_id), f"new {what}")  # 0.28.0
    return int(project_id)


def _joined(project_id: int, venture_id: int | None) -> str:
    """0.15.0: what the agent hears when a product line joined a venture (ventures.adopt)."""
    return f" Project #{project_id} is part of venture #{venture_id} now: its sales count there." if venture_id else ""


def _demand_note(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    """0.12.0: a demand note for a project; with the owner's market probe on, Etsy's numbers for its keywords first
    (a network call: no transaction is held meanwhile)."""
    _shop(ctx)
    project_id = args["project_id"]
    keywords = " ".join(args["keywords"].split())
    said = " ".join(str(args.get("demand") or "").split()) or None
    source = str(args.get("source") or "").strip() or None
    with ctx.db.connection() as conn:
        if store.project(conn, ctx.scope, project_id) is None:
            raise ToolError(f"there is no project #{project_id}")
        _line(ctx, conn, project_id, "demand note")  # 0.28.0: before the market probe
        problem = demand.source_problem(conn, ctx.scope, source, project_id, said) if source is not None else ""
    if problem:
        raise ToolError(problem)
    if ctx.market is None and (said is None or source is None):
        raise ToolError("give demand and its source: your owner's Etsy market probe is off")
    found = None
    if ctx.market is not None:
        try:
            found = ctx.market(keywords)
        except etsy.EtsyError as exc:
            if said is None or source is None:
                raise ToolError(f"Etsy's market probe failed ({exc}): give demand and its source instead") from None
    with ctx.db.transaction() as conn:
        number = demand.add(conn, ctx.scope, ctx.cycle_id, project_id, keywords, said, source, found, ctx.now())
    probe = f" Etsy's market probe for '{keywords}': {found.text()}." if found is not None else ""
    return Outcome(
        True,
        f"Saved demand note #{number} for project #{project_id}: its first listing may be proposed within "
        f"{demand.DAYS} days.{probe}",
        f"demand note #{number} for project #{project_id}",
        project_id=project_id,
    )


def _etsy_listing(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    _shop(ctx)
    listing_id = args.get("listing_id")
    # 0.19.2: with Etsy's numbers, which Ember's code reads every hour (live, the work steps never saw them, so the
    # agent asked its owner for its listings' views and promised to report them once they were sent)
    numbers, read = etsy_publisher.listing_numbers(conn, ctx.scope)
    as_read = f"Etsy's numbers as Ember's code read them at {_local(ctx, read)}" if read else "not read from Etsy yet"
    try:
        if listing_id is None:
            found = etsy_publisher.live_listings(conn, ctx.scope)
            idle = etsy_publisher.idle_listings(conn, ctx.scope)  # 0.12.0: they were listed as live
            gone = "; ".join(f"#{r['listing_id']} {etsy_publisher.state_text(r)}" for r in idle)
            pod = _printify_listings(conn, ctx.scope)
            if not found:
                text = "You have no live listings." + (f" Not live at Etsy: {gone}." if gone else "") + pod
                return Outcome(True, text, "none")
            lines = "\n".join(
                etsy.listing_line(i, listing) + (f" · {numbers.get(i, 'not read from Etsy yet')}" if read else "")
                for i, listing in found
            )
            text = f"Your live listings (newest first; {as_read}):\n{lines}"
            text += (f"\nNot live at Etsy: {gone}." if gone else "") + pod
            return Outcome(True, text, f"{len(found)} listings")
        listing = etsy_publisher.current_listing(conn, ctx.scope, listing_id)
    except etsy.EtsyError as exc:
        raise ToolError(f"Ember's record of #{listing_id} isn't readable ({exc})") from None
    row = etsy_publisher.listing_row(conn, ctx.scope, listing_id)
    if listing is None or row is None:
        made = _printify_listings(conn, ctx.scope, listing_id)
        if made:  # 0.19.2: live, a cycle took its posters made through Printify for listings that weren't live
            found = printify_publisher.current_product(conn, ctx.scope, listing_id)
            if found is not None:  # 0.39.0: in full, as a change of it starts from
                made += "\n" + printify_publisher.product_text(found[1])
            critic = _critic(conn, ctx.scope, listing_id)  # 0.40.1
            made += f"\n{critic}" if critic else ""
            waiting = printify_publisher.open_edit(conn, ctx.scope, listing_id)
            if waiting is not None:
                made += f"\n\nRequest #{waiting} changes it and hasn't been made yet."
            return Outcome(True, made.strip(), f"read #{listing_id}")
        raise ToolError(f"#{listing_id} isn't one of your live listings; etsy_listing without a number lists them")
    said = f"{as_read}: {numbers.get(listing_id, 'not read yet')}." if read else "Etsy's numbers: not read yet."
    critic = _critic(conn, ctx.scope, listing_id)  # 0.40.1: before its description, which may be long
    said += f"\n{critic}" if critic else ""
    text = etsy.listing_text(listing_id, listing, etsy_publisher.state_text(row), said)
    waiting = etsy_publisher.open_edit(conn, ctx.scope, listing_id)
    if waiting is not None:
        text += f"\n\nRequest #{waiting} changes it and hasn't been made yet."
    return Outcome(True, text, f"read #{listing_id}")


def _critic(conn: Any, scope: AgentScope, listing_id: int) -> str:
    """0.40.1 (analysis-0.40.0, 3.5): the quality critic's newest check of a listing (quality.listing_text)."""
    from . import quality  # here, not at the top: quality imports prompts, which imports tools, then this

    return quality.listing_text(conn, scope, listing_id)


def _printify_listings(conn: Any, scope: AgentScope, listing_id: int | None = None) -> str:
    """0.19.2: the Etsy listings of Ember's Printify products, with Etsy's numbers (etsy_listing showed only those Ember
    listed itself: live, the agent took its two posters for listings that weren't live). "" when there are none."""
    rows = [
        r
        for r in printify_publisher.products(conn, scope, 50)
        if r["status"] == "active" and r["listing_id"] and listing_id in (None, int(r["listing_id"]))
    ]
    if not rows:
        return ""
    lines = [
        f"- #{r['listing_id']} {' '.join(str(r['title']).split())[:70]} · "
        + (etsy_publisher.numbers_text(r["views"], r["favorites"]) if r["synced_at"] else "not read from Etsy yet")
        for r in rows
    ]
    return (  # 0.39.0: propose_printify_edit changes them (propose_etsy_edit doesn't: Printify keeps them)
        "\nMade through Printify (live at Etsy; propose_printify_edit changes them, propose_etsy_edit doesn't, and a "
        "pin or a post may link them; PRINTIFY in your plan has their prices):\n" + "\n".join(lines)
    )


def _propose_etsy_edit(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    shop = _shop(ctx)
    listing_id = args["listing_id"]
    try:
        now = etsy_publisher.current_listing(conn, ctx.scope, listing_id)
    except etsy.EtsyError as exc:
        raise ToolError(f"Ember's record of #{listing_id} isn't readable ({exc})") from None
    row = etsy_publisher.listing_row(conn, ctx.scope, listing_id)
    if now is None or row is None:
        if printify_publisher.live_listing(conn, ctx.scope, listing_id) is not None:  # 0.39.0
            raise ToolError(f"#{listing_id} is a listing Printify made of a product of yours: propose_printify_edit")
        raise ToolError(f"#{listing_id} isn't one of your live listings; etsy_listing without a number lists them")
    state, action, stands = etsy_publisher.etsy_state(row), args.get("state"), etsy_publisher.state_text(row)
    if action != "deactivate":  # 0.23.3: taking it out of the shop stays possible (0.28.0: from any line's cycle)
        _listing_stopped(conn, ctx.scope, listing_id, "change or renewal (deactivate it, if it shouldn't sell)")
        _listing_line(ctx, conn, listing_id, "change")
    if action == "renew" and state not in etsy.RENEWABLE:
        raise ToolError(f"#{listing_id} is {stands} at Etsy: only an expired, sold-out or deactivated one is renewed")
    if action != "renew" and state != etsy.LIVE_STATE:  # 0.12.0: Etsy's state counts, not Ember's record
        raise ToolError(f"#{listing_id} isn't live at Etsy ({stands}): renew it (state renew), with changes or without")
    waiting = etsy_publisher.open_edit(conn, ctx.scope, listing_id)
    if waiting is not None:
        raise ToolError(f"request #{waiting} already changes #{listing_id}; you hear its result first")
    changes: dict[str, Any] = {}
    try:
        if args.get("title") is not None:
            changes["title"] = etsy.check_title(args["title"])
        if args.get("description") is not None:
            changes["description"] = etsy.check_description(args["description"])
        if args.get("price") is not None:
            changes["price"] = etsy.check_price(args["price"])
        if args.get("tags") is not None:
            changes["tags"] = etsy.check_tags(args["tags"])
        if args.get("photos") is not None:
            changes["photos"] = _uploads(ctx, args["photos"], etsy.PHOTO_KINDS, etsy.MAX_PHOTOS, "the photos")
        if args.get("files") is not None:
            changes["files"] = _uploads(
                ctx, args["files"], etsy.FILE_KINDS, etsy.MAX_FILES, "the files buyers download"
            )
    except etsy.EtsyError as exc:
        raise ToolError(str(exc)) from None
    if args.get("category_id") is not None and args["category_id"] != now.taxonomy_id:
        changes["taxonomy_id"], changes["category"] = args["category_id"], _category(shop, args["category_id"])
    for name in [n for n in ("title", "description", "price", "tags", "photos", "files") if n in changes]:
        if changes[name] == getattr(now, name):
            del changes[name]  # the same as now: no change
    frozen = plan.frozen(conn, ctx.scope, ctx.clock.today())  # 0.36.0: the owner's freeze, kept by Ember's code
    if frozen and ("title" in changes or "tags" in changes):
        raise ToolError(
            f"your owner froze the titles and tags of your listings until {frozen}: propose the other changes without"
            " title and tags"
        )
    if action == "deactivate" and changes:
        raise ToolError("deactivate on its own: a change to a listing that leaves the shop helps nobody")
    if not changes and action is None:
        raise ToolError("nothing changes: give a part that differs from the listing now (etsy_listing shows it)")
    if changes:  # 0.42.1: the owner's paid test leaves it as it is (renewing or deactivating it stays possible)
        _refuse(ctx, conn, preconditions.PAID, listing=listing_id)
    edit = etsy.Edit(listing_id=listing_id, currency=now.currency, state=action, **changes)
    reason = args["reason"].strip()
    verb = {"renew": "Renew", "deactivate": "Deactivate"}.get(action or "", "Change")
    cost = "none: Etsy charges nothing for changing a listing"
    if action == "renew":
        cost = f"Etsy's listing fee for the renewal ({etsy.RENEWAL_FEE} at most), and its fees on each sale"
    made = _new_request(
        ctx,
        conn,
        etsy.edit_payload(edit, now, stands),
        edit.to_action(),
        type="sell",
        title=_cut(f"{verb} Etsy listing: {now.title}", 120),
        description=reason,
        expected_cost=cost,
        expected_benefit=reason,
        executor="etsy_edit",
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate change")
    parts = ", ".join(p for p in edit.parts() if p not in etsy.STATES)
    does = {"renew": "renews", "deactivate": "deactivates"}.get(action or "", "changes")
    what = f"{does} #{listing_id}" + (f" and changes its {parts}" if action and parts else "")
    if action is None:
        what = f"changes the {parts} of #{listing_id}"
    note = _unlocked(ctx)
    waits = (
        f"Approval request #{made} is waiting for your owner: it {what}. Nothing has changed at Etsy yet. If they "
        f"approve it, Ember's code makes the change and you hear the result.{note}"
    )
    text = _at_once(conn, made, note, f" Ember's code {what} at Etsy and you hear the result.") or waits
    if action is None:
        return Outcome(True, text, f"#{made} change of #{listing_id}: {parts}")
    return Outcome(True, text, f"#{made} {verb.lower()} #{listing_id}" + (f": {parts}" if parts else ""))


def _shop(ctx: ToolContext) -> EtsyAccess:
    if ctx.etsy is None:
        raise ToolError("there is no Etsy shop")
    return ctx.etsy


def _uploads(ctx: ToolContext, paths: str, kinds: frozenset[str], limit: int, what: str) -> tuple[etsy.Upload, ...]:
    """The workspace files a listing uses, each with its SHA-256: Ember's code uploads exactly these."""
    names = [p.strip() for p in paths.split(",") if p.strip()]
    if not names:
        raise ToolError(f"name {what}")
    if len(names) > limit:
        raise ToolError(f"at most {limit} for {what}")
    if len(set(names)) != len(names):
        raise ToolError(f"{what} name the same file twice")
    found = []
    for name in names:
        try:
            data = ctx.workspace.read_bytes(name)
        except SandboxError as exc:
            raise ToolError(str(exc)) from None
        try:
            found.append(etsy.upload(name, data, kinds, what))
        except etsy.EtsyError as exc:
            raise ToolError(str(exc)) from None
    return tuple(found)


def _printify(ctx: ToolContext) -> PrintifyAccess:
    if ctx.printify is None:
        raise ToolError("your owner's Printify account isn't set up")
    return ctx.printify


def _printify_catalog(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    """0.13.0 (Phase E4): Printify's catalog (a network call when Ember's copy is old: no transaction is held)."""
    _printify(ctx)
    if ctx.catalog is None:
        raise ToolError("Printify's catalog can't be read now")
    search = " ".join(str(args.get("search") or "").split()) or None
    blueprint_id, provider_id = args.get("blueprint_id"), args.get("provider_id")
    if provider_id is not None and blueprint_id is None:
        raise ToolError("give the product's blueprint_id with its provider_id")
    try:
        answer = ctx.catalog(search, blueprint_id, provider_id)
    except printify.PrintifyError as exc:
        raise ToolError(f"Printify's catalog: {exc}") from None
    what = (
        f"variants of #{blueprint_id}" if provider_id else f"providers of #{blueprint_id}" if blueprint_id else search
    )
    return Outcome(True, answer, f"catalog: {_cut(str(what), 60)}")


def _propose_printify_product(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.13.0 (Phase E4): a product made on order by Printify, sold in the owner's Etsy shop."""
    access = _printify(ctx)
    _shop(ctx)
    blueprint_id, provider_id = args["blueprint_id"], args["provider_id"]
    try:
        prices = printify.parse_prices(args["prices"])
    except printify.PrintifyError as exc:
        raise ToolError(str(exc)) from None
    known = printify_publisher.variants(conn, ctx.scope.mode, blueprint_id, provider_id)
    if known is None:
        raise ToolError(
            f"read the variants of #{blueprint_id} by provider #{provider_id} with printify_catalog first "
            "(blueprint_id and provider_id)"
        )
    by_id = {v.variant_id: v for v in known}
    unknown = next((v for v, _ in prices if v not in by_id), None)
    if unknown is not None:
        raise ToolError(f"variant {unknown} isn't one provider #{provider_id} makes of #{blueprint_id}")
    chosen = [by_id[v] for v, _ in prices]
    # 0.15.0: Printify's currency was never read, nor compared with the shop's: a price's margin is checked in one,
    # with what Printify states in another converted at the owner's rate
    if _shop(ctx).currency != access.currency:
        raise ToolError(
            f"the Etsy shop sells in {_shop(ctx).currency}, but printify_currency is {access.currency}: no product can "
            "be proposed until your owner makes them one"
        )
    billed = {v.currency for v in chosen}
    if len(billed) > 1:
        raise ToolError(f"Printify states these variants in {' and '.join(sorted(billed))}: one product each")
    billed_in, rate = billed.pop(), ctx.usd_per_eur
    if access.currency not in ("EUR", "USD"):  # 0.15.0: Etsy's USD listing fee converts only to EUR
        raise ToolError(f"Ember's code checks a margin in EUR or USD only, not {access.currency}")
    sale = printify.Terms(access.currency, rate, access.buyer_ships, access.bill_vat)
    try:
        shipping = {v.variant_id: printify.convert(v.shipping_cents, billed_in, access.currency, rate) for v in chosen}
    except printify.PrintifyError as exc:
        raise ToolError(f"its shipping: {exc}") from None
    shape = chosen[0].height / chosen[0].width
    other = next((v for v in chosen if abs(v.height / v.width - shape) / shape > printify.SHAPE_TOLERANCE), None)
    if other is not None:
        raise ToolError(
            f"{chosen[0].title} and {other.title} have print areas of different shapes: one product each (or variants "
            "of one shape)"
        )
    area = max(chosen, key=lambda v: v.width * v.height)
    path = args["image"].strip()
    try:
        data = ctx.workspace.read_bytes(path)
    except SandboxError as exc:
        raise ToolError(str(exc)) from None
    try:
        upload = printify.image(path, data)
        width, height = images.png_size(data)
    except printify.PrintifyError as exc:
        raise ToolError(str(exc)) from None
    except images.ImageError as exc:  # 0.15.0: what is wrong with it (a print-size poster was "not a picture")
        raise ToolError(f"{path} can't be used: {exc}") from None
    try:
        title = etsy.check_title(args["title"])
        description = etsy.check_description(_without_ai_line(args["description"]))  # added, once
        tags = etsy.check_tags(args["tags"])
    except etsy.EtsyError as exc:
        raise ToolError(str(exc)) from None
    project_id = _product_line(ctx, conn, args, "product")
    _refuse(ctx, conn, preconditions.DEMAND, line=project_id)  # 0.40.3: the plan reads the same rule
    # 0.15.0: what making costs, when printify_catalog read it: a price that keeps too little costs no approval
    costs = printify_publisher.costs_of(conn, ctx.scope.mode, blueprint_id, provider_id, ctx.clock.now())
    low, currency = [], access.currency
    for v, price in prices:
        cost = printify.convert(costs[v], billed_in, currency, rate) if v in costs else None
        if cost is not None and not printify.keeps(price, cost, shipping[v], sale):
            least = printify.least_price(cost, shipping[v], sale)
            low.append(f"variant {v} at {printify.money(price, currency)}: at least {printify.money(least, currency)}")
    if low:
        raise ToolError(
            f"{'; '.join(low)}: below that a price keeps less than {printify.MIN_MARGIN * 100:.0f}% after Etsy's "
            f"fees, making and shipping ({sale.said()})"
        )
    joined = ventures.adopt(conn, ctx.scope, project_id, "printify", ctx.now())
    product = printify.Product(
        title=title,
        description=description,
        tags=tags,
        blueprint_id=blueprint_id,
        provider_id=provider_id,
        prices=prices,
        shipping=tuple((v.variant_id, shipping[v.variant_id]) for v in chosen),
        image=upload,
        width=width,
        height=height,
        area_width=area.width,
        area_height=area.height,
        currency=access.currency,
        billed_in=billed_in,
    )
    blueprint, provider = printify_publisher.names(conn, ctx.scope.mode, blueprint_id, provider_id)
    reason = args["reason"].strip()
    made = _new_request(
        ctx,
        conn,
        printify.payload(product, blueprint, provider, {v.variant_id: v.title for v in chosen}, sale),
        product.to_action(),
        project_id=project_id,
        type="sell",
        title=_cut(f"Printify product: {product.title}", 120),
        description=reason,
        expected_cost=(
            "Printify charges your owner for making and shipping each order; Etsy's listing fee (USD 0.20) and its "
            "fees on each sale"
        ),
        expected_benefit=reason,
        executor="printify_product",
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate product")
    text = (
        f"Approval request #{made} is waiting for your owner. Nothing is at Printify yet. If they approve it, Ember's "
        f"code creates the product at Printify, publishes it to {access.shop_title} only if each price keeps "
        f"{printify.MIN_MARGIN * 100:.0f}% after Etsy's fees, making and shipping (at most {access.daily_limit} "
        "products a day), and you hear the result."
    )
    short = qa.defects("printify.create_product", product)  # the QA registry: your owner sees it too
    if short:
        text += f" QA (Ember's code): {'; '.join(short)}."
    text += _joined(project_id, joined) + _unlocked(ctx)
    return Outcome(True, text, f"#{made} Printify product: {_cut(product.title, 60)}", project_id=project_id)


def _propose_printify_edit(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.39.0 (the agent's request): a change of one of its live Printify products: a new design, title, description
    or tags. propose_etsy_edit leaves the listings Printify made alone, so a flaw the critic found in one (a photo, a
    claim the product no longer keeps) could only be fixed by a new product: a second listing fee, and a listing
    without the first one's views and favorites."""
    _printify(ctx)
    _shop(ctx)
    listing_id = args["listing_id"]
    found = printify_publisher.current_product(conn, ctx.scope, listing_id)
    if found is None:
        raise ToolError(
            f"#{listing_id} isn't one of your live Printify products; etsy_listing without a number lists them"
        )
    row, now = found
    if (row["state"] or etsy.LIVE_STATE) != etsy.LIVE_STATE:  # as the Etsy sync last read it
        raise ToolError(f"#{listing_id} isn't live at Etsy ({row['state']}): a change of it reaches no buyer")
    _listing_stopped(conn, ctx.scope, listing_id, "change")
    _listing_line(ctx, conn, listing_id, "change")
    _refuse(ctx, conn, preconditions.PAID, listing=listing_id)  # 0.42.1: the owner's paid test leaves it as it is
    waiting = printify_publisher.open_edit(conn, ctx.scope, listing_id)
    if waiting is not None:
        raise ToolError(f"request #{waiting} already changes #{listing_id}; you hear its result first")
    changes: dict[str, Any] = {}
    try:
        if args.get("title") is not None:
            changes["title"] = etsy.check_title(args["title"])
        if args.get("description") is not None:  # the AI line is added, once
            changes["description"] = etsy.check_description(_without_ai_line(args["description"]))
        if args.get("tags") is not None:
            changes["tags"] = etsy.check_tags(args["tags"])
    except etsy.EtsyError as exc:
        raise ToolError(str(exc)) from None
    if args.get("image") is not None:
        path = args["image"].strip()
        try:
            data = ctx.workspace.read_bytes(path)
        except SandboxError as exc:
            raise ToolError(str(exc)) from None
        try:
            upload = printify.image(path, data)
            width, height = images.png_size(data)
        except printify.PrintifyError as exc:
            raise ToolError(str(exc)) from None
        except images.ImageError as exc:
            raise ToolError(f"{path} can't be used: {exc}") from None
        if upload.sha256 != now.image.sha256:
            changes.update(image=upload, width=width, height=height)
    for name in [n for n in ("title", "description", "tags") if n in changes]:
        if changes[name] == getattr(now, name):
            del changes[name]  # the same as now: no change
    frozen = plan.frozen(conn, ctx.scope, ctx.clock.today())  # the owner's freeze holds for these listings too
    if frozen and ("title" in changes or "tags" in changes):
        raise ToolError(
            f"your owner froze the titles and tags of your listings until {frozen}: propose the other changes without"
            " title and tags"
        )
    if not changes:
        raise ToolError("nothing changes: give a part that differs from the product now (etsy_listing shows it)")
    edit = printify.Edit(listing_id=listing_id, product_id=str(row["product_id"]), **changes)
    reason = args["reason"].strip()
    made = _new_request(
        ctx,
        conn,
        printify.edit_payload(edit, now),
        edit.to_action(),
        type="sell",
        title=_cut(f"Change Printify product: {now.title}", 120),
        description=reason,
        expected_cost="none: Printify and Etsy charge nothing for changing a listing",
        expected_benefit=reason,
        executor="printify_edit",
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate change")
    parts = ", ".join(edit.parts())
    text = (
        f"Approval request #{made} is waiting for your owner: it changes the {parts} of #{listing_id}. Nothing has "
        "changed at Printify yet. If they approve it, Ember's code changes the product at Printify, which publishes "
        "it to the listing, and you hear the result."
    )
    if edit.image is not None:
        short = qa.defects("printify.edit_product", edit.applied(now))  # the QA registry: your owner sees it too
        if short:
            text += f" QA (Ember's code): {'; '.join(short)}."
    text += _unlocked(ctx)
    return Outcome(True, text, f"#{made} change of #{listing_id}: {parts}")


def _kdp(ctx: ToolContext) -> KdpAccess:
    if ctx.kdp is None:
        raise ToolError("Amazon KDP is off: your owner hasn't switched it on")
    return ctx.kdp


def _without_ai_line(description: str) -> str:
    """A Printify product's description without the AI line Ember's code adds (0.45.0: or the one it added before)."""
    for line in (printify.DISCLOSURE, *printify.EARLIER):
        description = description.replace(line, "")
    return description


def _read_file(ctx: ToolContext, path: str) -> bytes:
    try:
        return ctx.workspace.read_bytes(path)
    except SandboxError as exc:
        raise ToolError(str(exc)) from None


def _book_cover(ctx: ToolContext, spec: dict[str, Any], path: str) -> tuple[str, make.Made | None]:
    """0.25.0: a spec's cover: the file it names, or the one Ember's code makes now from its cover object (front, back,
    spine, background), so a paperback's spine always fits its interior's pages."""
    cover = spec["cover"]
    if isinstance(cover, str):
        return cover, None
    out = kdp.cover_path(path, spec["format"])
    paperback = spec["format"] == "paperback"
    made = make.kdp_cover(
        ctx.workspace,
        out,
        cover["front"],
        spec["manuscript"] if paperback else "",
        spec.get("paper", "") if paperback else "",
        html.unescape(cover.get("back", "")),
        html.unescape(cover.get("spine", "")),
        cover.get("background") or None,
    )
    return out, made


def _book(ctx: ToolContext, spec: dict[str, Any], cover_path: str) -> tuple[kdp.Book, list[str]]:
    """0.25.0: a KDP book's package, checked against KDP's rules (a paperback's interior drawn to find what it prints
    in its margins), and what its card notes; a package KDP would refuse is a ToolError that says why."""
    access = _kdp(ctx)
    form = spec["format"]
    paper = spec.get("paper", "")
    notes: list[str] = []
    try:
        title, subtitle = kdp.check_title(spec["title"], spec.get("subtitle"))
        words = {
            "title": title,
            "subtitle": subtitle,
            "author": access.author,
            "description": kdp.check_description(spec["description"]),
            "keywords": kdp.check_keywords(spec["keywords"]),
            "categories": kdp.check_categories(spec["categories"]),
            "language": kdp.check_language(spec["language"]),
        }
        text_data = _read_file(ctx, spec["manuscript"])
        what = "the manuscript" if form == "ebook" else "the interior"
        text = kdp.upload(spec["manuscript"], text_data, kdp.MANUSCRIPT_KINDS[form], what)
        cover_data = _read_file(ctx, cover_path)
        cover = kdp.upload(cover_path, cover_data, kdp.COVER_KINDS[form], "the cover")
        if form == "ebook":
            kdp.check_docx(text_data, text.path)
            try:
                width, height = images.png_size(cover_data)
            except images.ImageError as exc:
                raise ToolError(f"{cover.path}: {exc}") from None
            notes += kdp.check_ebook_cover(cover_data, width, height, cover.path)
            price = kdp.check_price(spec["price"], form, file_bytes=text.bytes)
            return kdp.Book(format=form, **words, price=price, manuscript=text, cover=cover), notes
        try:
            sizes = images.page_sizes(text_data)
            found = kdp.trim_of(sizes[0]) if sizes else None
            if found is None:
                shown = " x ".join(kdp.inches(v / 72) for v in sizes[0]) if sizes else "missing"
                raise ToolError(
                    f"{text.path}'s pages are {shown}: no KDP trim size (make_document's page setting takes them: "
                    f"{', '.join(kdp.TRIM_NAMES)})"
                )
            trim = found[0]
            interior = kdp.check_interior(
                sizes, lambda: images.ink_boxes(text_data, kdp.INK_DPI, kdp.INK_LEVEL), trim, paper
            )
            if interior.findings:
                raise ToolError(f"{text.path} isn't ready for KDP: {'; '.join(interior.findings)}")
            cover_sizes = images.page_sizes(cover_data)
        except images.ImageError as exc:
            raise ToolError(f"{text.path} or {cover.path}: {exc}") from None
        if len(cover_sizes) != 1:
            raise ToolError(f"{cover.path} must be one page, the full cover (give cover.front to have it made)")
        wrong = kdp.check_cover(cover_sizes[0], interior.pages, trim, paper)
        if wrong:
            raise ToolError("; ".join(wrong))
        notes += _front(spec, cover.path, cover_data, trim, interior.pages, paper)
        price = kdp.check_price(spec["price"], form, pages=interior.pages, paper=paper, trim=trim)
        if interior.bleed:
            keep = kdp.inches(kdp.OUTSIDE_MARGIN_BLEED)
            notes.append(f"the interior has bleed: KDP's previewer checks that its words keep {keep} from its edges")
        book = kdp.Book(
            format=form,
            **words,
            price=price,
            manuscript=text,
            cover=cover,
            trim=trim,
            paper=paper,
            bleed=interior.bleed,
            pages=interior.pages,
            low_content=bool(spec.get("low_content")),
        )
        return book, notes
    except kdp.KdpError as exc:
        raise ToolError(str(exc)) from None


def _front(spec: dict[str, Any], path: str, data: bytes, trim: str, pages: int, paper: str) -> list[str]:
    """0.45.0 (evaluation 2026-10-22, C6): a paperback cover's front keeps its words kdp.SAFE_ZONE inside the trim, or
    the book is refused with what to change; a front whose art reaches its edges is a note (Ember's code can't tell its
    words from its picture). Live, the Haushaltsbuch's title printed 0.048 in inside the trim (a pin's 2:3 front
    picture on an 8.5x11 front, 13% of it cut off) and the tool said KDP would take it."""
    fold = float(kdp.BLEED + kdp.trim_size(trim)[0] + kdp.spine_width(pages, paper))
    try:
        plain, box = images.front_ink(data, fold, kdp.FRONT_DPI, kdp.FRONT_CONTRAST)
    except images.ImageError as exc:
        raise ToolError(f"{path}: {exc}") from None
    if not plain:
        return [
            f"the front's picture reaches its edges, so Ember's code can't tell its words from it: look at the cover's "
            f"preview, its words keep {kdp.inches(kdp.SAFE_ZONE)} inside the trim"
        ]
    close = kdp.check_front(box, trim, pages, paper)
    if not close:
        return []
    cover = spec["cover"]
    if isinstance(cover, dict):
        shape = make.front_shape(trim)
        fix = (
            f"make {cover['front']} again in make_image's shape {shape} (the nearest to a front at {trim}: the least "
            "of it is cut off) with its words 0.25 in from its edges, then propose the book again"
        )
    else:
        room, edge = kdp.inches(kdp.SAFE_ZONE), kdp.inches(kdp.BLEED + kdp.SAFE_ZONE)
        fix = f"move its words {room} inside the trim ({edge} from the cover's edges)"
    raise ToolError(f"{path}: {close[0]}: {fix}")


def _propose_kdp_book(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    """0.25.0: a book for Amazon KDP from the agent's .json spec: Ember's code makes its cover, checks the package and
    (without check) asks the owner, who publishes it at kdp.amazon.com from their own account (Amazon has no API for
    KDP). Its files are made, read and drawn outside the database transaction."""
    _kdp(ctx)
    path = args["spec"]
    if not path.lower().endswith(".json"):
        raise ToolError("spec must be your book's .json file in your workspace (guide 'kdp')")
    try:
        spec = kdp.read_spec(ctx.workspace.read(path), path)
    except SandboxError as exc:
        raise ToolError(str(exc)) from None
    except kdp.KdpError as exc:
        raise ToolError(str(exc)) from None
    # 0.45.0 (evaluation 2026-10-22, C1): its words and its cover's, before a cover is made or a page drawn
    words = [spec.get(key) for key in ("title", "subtitle", "description", "keywords")]
    said = claims.refusal(claims.texts([*words, spec["cover"] if isinstance(spec["cover"], dict) else ""]))
    if said:
        raise ToolError(said)
    cover_path, made = _book_cover(ctx, spec, path)
    book, notes = _book(ctx, spec, cover_path)
    cover = f"{made.text()} " if made is not None else ""
    noted = "".join(f" Note: {note}." for note in notes)
    if args.get("check"):
        checked = (
            f"{cover}Checked {path}: KDP would take this {book.format} as it is. Price {book.price} {kdp.CURRENCY}: "
            f"{kdp.royalty(book)}.{noted}"
        )
        summary = f"checked KDP {book.format}: {_cut(book.title, 60)}"
        if book.format != "paperback":
            return Outcome(True, f"{checked} Nothing went to your owner: propose it with check false.", summary)
        drawn, pictures = _draw_pages(ctx, spec, book, args.get("pages") or "")
        return Outcome(True, f"{checked}\n{drawn}", summary, pictures=pictures)
    reason = " ".join(str(spec.get("reason") or "").split())
    if not reason:
        raise ToolError(f"{path} needs reason: why this book, for your owner")
    page_check = _page_check(ctx, book, args.get("qa"), notes) if book.format == "paperback" else ""
    noted = "".join(f" Note: {note}." for note in notes)  # 0.42.0: with a qa cut to its length
    with ctx.db.transaction() as conn:
        project_id = _product_line(ctx, conn, spec, "book")
        _refuse(ctx, conn, preconditions.KDP_WEEK, form=book.format)  # 0.25.0; 0.42.0: the plan waits on it too
        made_request = _new_request(
            ctx,
            conn,
            kdp.payload(book),
            book.to_action(),
            project_id=project_id,
            type="sell",
            title=_cut(f"KDP {book.format}: {book.title}", 120),
            description=_with_page_check(reason, page_check),
            expected_cost="none: publishing at KDP is free (Amazon keeps its share of each sale)",
            expected_benefit=reason,
            executor=kdp.EXECUTOR,
        )
    if isinstance(made_request, str):
        return Outcome(True, made_request, "duplicate book")
    text = (
        f"{cover}Approval request #{made_request} is waiting for your owner. Nothing is at Amazon yet. If they approve "
        "it, they publish it at KDP from their own account and mark it done with the book's link; its royalties count "
        f"once they record them. Price {book.price} {kdp.CURRENCY}: {kdp.royalty(book)}.{noted}"
    )
    text += _unlocked(ctx)
    return Outcome(True, text, f"#{made_request} KDP {book.format}: {_cut(book.title, 60)}", project_id)


def _draw_pages(ctx: ToolContext, spec: dict[str, Any], book: kdp.Book, pages: str) -> tuple[str, tuple[bytes, ...]]:
    """0.40.0 (upgrade request #12): a paperback's check draws its interior's pages fresh, free, as the workshop's
    render-page-3-of-71.py did for a run of about $0.19 (one page with pypdfium2, 600 pixels wide): one page of each
    kind (or the pages named), with what Ember's code measured on all of them, the checklist and the owner's last words
    on the book. A proposal of the same file in a later reply of the cycle passes (_page_check). Live, two
    resubmissions trusted pictures of an earlier version or a checklist passed days before (analysis-0.39.6, 3.2)."""
    path = book.manuscript.path
    data = _read_file(ctx, path)
    try:
        checked = pagecheck.check(data, pagecheck.named(pages), book.language)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    sha256 = hashlib.sha256(data).hexdigest()
    before = ctx.state.drawn.get(path)
    seen = [*before.pages, *checked.shown] if before is not None and before.sha256 == sha256 else checked.shown
    ctx.state.drawn[path] = Drawn(
        sha256,
        ctx.state.reply,
        tuple(sorted(set(seen))),
        len(checked.pages),
        tuple(checked.findings()),
        tuple(checked.placeholders),
    )
    with ctx.db.connection() as conn:
        words = kdp.last_words(conn, ctx.scope, book.title, spec.get("project_id") or ctx.state.focus_project_id)
    owner = (
        f"Your owner's last words on it, {words}: each point done?"
        if words
        else "What your owner asked of it in their messages: each point done?"
    )
    listed = " ".join(f"{n}. {item}" for n, item in enumerate([*pagecheck.CHECKLIST, owner], 1))
    shown = ", ".join(map(str, checked.shown))
    head = f"Its pages, drawn now from {path} as it is ({len(checked.pages)} pages): {shown}, shown below."
    made = _made_from(ctx, path, hashlib.sha256(data).hexdigest())
    head += f"\n{made}" if made else ""
    tail = (
        f"Check on these pictures (pages: '45, 47' draws others): {listed}\n"
        "Nothing went to your owner. Propose it with check false in a later reply of this cycle, with qa: what you "
        f"saw, point by point (your owner reads it; {QA_CHARS:,} characters are kept). Once the file changes, check it "
        "again."
    )
    # what is measured gives way first: the check's own text and the cover's come before it in the result
    room = MAX_RESULT_CHARS - DRAWN_ROOM - len(head) - len(tail)
    return "\n".join([head, _cut("\n".join(checked.measures()), room), tail]), tuple(checked.pictures)


def _made_from(ctx: ToolContext, path: str, digest: str) -> str:
    """0.40.1 (analysis-0.40.0, 3.2): what a paperback's interior (its bytes' SHA-256 ``digest``) was made from
    (made_pdfs) and whether that file changed since, and a newer PDF of the same book in its folder: made from the same
    file, or (made before 0.40.1) named the same but for its version. Live, the spec named v15.pdf, the book the owner
    rejected, while v19 to v22 had been made from v18.md, and each cycle on the book spent its first steps finding out
    which file the book was."""
    said = []
    made = _source_of(ctx, digest)
    if made is not None:
        source, digest = made
        try:
            now = ctx.workspace.read(source)
        except SandboxError:
            now = None
        if now is None:
            state = "it isn't there any more"
        elif hashlib.sha256(now.encode()).hexdigest() == digest:
            state = "unchanged since"
        else:
            state = "changed since: make_document it again and check the new PDF, or this isn't the book as it is"
        said.append(f"Made with make_document from {source} ({state}).")
    newer = _newer_interior(ctx, path, made)
    if newer is not None:
        when = _mtime(ctx, ctx.workspace.modified(newer) or 0.0)
        said.append(f"{newer} ({when}) is newer than {path}: if it is the book, set the spec's manuscript to it.")
    return " ".join(said)


NEWER_READ = 10  # 0.40.1: the newer PDFs of an interior's folder whose source a check looks up at most


def _newer_interior(ctx: ToolContext, path: str, made: tuple[str, str] | None) -> str | None:
    """The newest PDF in ``path``'s folder written after it, of the same book: made from the same file, or named the
    same but for a version at its end (v15, v22)."""
    folder, _, _ = path.rpartition("/")
    written = ctx.workspace.modified(path)
    if written is None:
        return None
    try:
        found = ctx.workspace.listing(folder)
    except SandboxError:
        return None
    later = sorted(
        (e for e in found if not e.is_dir and e.path.lower().endswith(".pdf") and e.modified > written),
        key=lambda e: -e.modified,
    )
    for entry in later[:NEWER_READ]:
        try:
            other = _source_of(ctx, hashlib.sha256(ctx.workspace.read_bytes(entry.path)).hexdigest())
        except SandboxError:  # gone since the listing: the agent's own files only change in its cycle
            continue
        if (made is not None and other is not None and other[0] == made[0]) or _book_stem(entry.path) == _book_stem(
            path
        ):
            return entry.path
    return None


def _source_of(ctx: ToolContext, digest: str) -> tuple[str, str] | None:
    """The file make_document made the PDF of these bytes from, and that file's SHA-256 then; None for a PDF made
    otherwise (or before 0.40.1)."""
    where, params = ctx.scope.where()
    with ctx.db.connection() as conn:
        row = conn.execute(
            f"SELECT source, source_sha256 FROM made_pdfs WHERE {where} AND sha256 = ? ORDER BY id DESC LIMIT 1",
            (*params, digest),
        ).fetchone()
    return (str(row["source"]), str(row["source_sha256"])) if row is not None else None


def _book_stem(path: str) -> str:
    """A file's name without its folder, its extension and a version at its end: 'interior' of 'b/interior-v15.pdf'."""
    name = path.rpartition("/")[2].rsplit(".", 1)[0]
    # 0.42.2 (analysis-0.40.1 audit): digits from the first of their run: tried from each, they took quadratic time
    return re.sub(r"[-_.]?v?(?<!\d)\d+$", "", name, flags=re.IGNORECASE)


def _page_check(ctx: ToolContext, book: kdp.Book, qa: Any, notes: list[str]) -> str:
    """0.40.0 (upgrade request #12): a paperback goes to the owner only after a check drew its interior's pages, of
    the same file, in an earlier reply of this cycle (one in the same reply hadn't reached the model when it wrote the
    proposal), with qa: what the agent saw on them. The owner reads qa on the request, with what Ember's code found
    (0.42.0: a longer qa cut to QA_CHARS, said in ``notes``)."""
    path = book.manuscript.path
    drawn = ctx.state.drawn.get(path)
    if drawn is None:
        raise ToolError(
            f"check its pages first: propose_kdp_book with check true draws {path}'s pages fresh, as the file is now, "
            "with the checklist; then propose it in a later reply with qa, what you saw on them. Pictures, notes or a "
            "check from before are not this file's pages"
        )
    if drawn.sha256 != book.manuscript.sha256:
        raise ToolError(f"{path} changed since this cycle's check drew its pages: check it again, then propose it")
    if drawn.reply == ctx.state.reply:
        raise ToolError(
            f"you checked {path} in this same reply, before its pages reached you: look at them, then propose it in "
            "your next reply with qa"
        )
    if drawn.placeholders:  # 0.40.2 (analysis-0.40.1, 3.2): live, "Gestaltet von [Verlag/Herausgeber]" on page 67
        raise ToolError(
            f"{path} prints a placeholder ({', '.join(drawn.placeholders[:3])}): write the real words in its source, or"
            " take the sentence out, make it again and check it; a book never prints [words in brackets]"
        )
    said = " ".join(str(qa or "").split())
    # 0.42.0 (analysis-0.40.1 codebase, 5.5): cut, not refused. Live, qa of 1,605 and 909 characters were refused: each
    # cost a step of the cycle whose check it needs, and three cycles that day ended at their last step. It is the
    # agent's own account for its owner, who checks the files before approving, beside what Ember's code found.
    if len(said) > QA_CHARS:
        notes.append(f"qa was cut to {QA_CHARS:,} of its {len(said):,} characters on your owner's request")
        said = _cut(said, QA_CHARS)
    if len(said.split()) < QA_WORDS:
        raise ToolError(
            f"qa says what you saw on its fresh pages, point by point of the check's list (at least {QA_WORDS} "
            "words): your owner reads it on the request"
        )
    found = f" Ember's code found: {'; '.join(drawn.findings)}." if drawn.findings else ""
    pages = ", ".join(map(str, drawn.pages))
    return f"Page check (pages {pages} of {drawn.count}, drawn fresh in cycle #{ctx.cycle_id}): {said}{found}"


def _with_page_check(reason: str, page_check: str) -> str:
    """A KDP request's description: the agent's reason, its page check (0.40.0) and what the owner does; the page
    check cut to the room the request's description has."""
    if not page_check:
        return f"{reason}\n\n{KDP_NOTE}"
    room = DESCRIPTION_MAX - len(reason) - len(KDP_NOTE) - 4
    return f"{reason}\n\n{_cut(page_check, room)}\n\n{KDP_NOTE}"
