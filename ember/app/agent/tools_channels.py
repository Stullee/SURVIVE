"""0.42.2 (analysis-0.40.1, 5.6): the tools of the channels that bring buyers, split out of tools.py.

Pinterest (pinterest_boards, propose_pin), Bluesky (bluesky_posts, propose_bluesky_post), the owner's website
(site_page) and their blog (propose_blog_post, propose_link_page). A pin, a post or a blog post is an approval request:
Ember's code publishes it once the owner approves, in the publishing queue's slots (publish_queue.py).
"""

from __future__ import annotations

import contextlib
import re
from typing import Any
from urllib.parse import urlsplit

from ..integrations import (
    bluesky,
    bluesky_publisher,
    etsy,
    etsy_publisher,
    kdp,
    pinterest,
    pinterest_publisher,
    printify_publisher,
    publish_queue,
    qa,
    site_publisher,
)
from ..products import blog, images, site
from . import preconditions, store, ventures, website
from .sandbox import SandboxError
from .store import AgentScope
from .tools_context import (
    BlogAccess,
    BlueskyAccess,
    Outcome,
    PinterestAccess,
    ToolContext,
    ToolError,
    _cut,
    _local,
    _numbers,
)
from .tools_lines import _line, _listing_line, _listing_stopped, _stopped
from .tools_listings import _shop
from .tools_requests import _new_request, _refuse, _unlocked


def _account(ctx: ToolContext) -> PinterestAccess:
    if ctx.pinterest is None:
        raise ToolError("your owner's Pinterest account isn't connected")
    return ctx.pinterest


def _pinterest_boards(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    account = _account(ctx)
    made = pinterest_publisher.boards(conn, ctx.scope)
    head = f"Your owner's Pinterest account: {account.username} (at most {account.daily_limit} pins a day)."
    queue = publish_queue.text(conn, ctx.scope, ctx.clock, "pinterest", account.daily_limit)  # 0.37.8
    text = f"{head}\n{pinterest_publisher.text(conn, ctx.scope, ctx.clock, 12)}" + (f"\n{queue}" if queue else "")
    return Outcome(True, text, f"{len(made)} boards")


def _propose_pin(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.13.0 (Phase E2): a pin on the owner's Pinterest account, linking to one of Ember's live Etsy listings."""
    account = _account(ctx)
    _shop(ctx)
    listing_id = args["listing_id"]
    try:
        listing = etsy_publisher.current_listing(conn, ctx.scope, listing_id)
    except etsy.EtsyError as exc:
        raise ToolError(f"Ember's record of #{listing_id} isn't readable ({exc})") from None
    row = etsy_publisher.listing_row(conn, ctx.scope, listing_id)
    if listing is None or row is None:  # 0.32.0: or one Printify made of a product of Ember's
        row = printify_publisher.live_listing(conn, ctx.scope, listing_id)
    if row is None:
        raise ToolError(f"#{listing_id} isn't one of your live listings; etsy_listing without a number lists them")
    if etsy_publisher.etsy_state(row) != etsy.LIVE_STATE:
        raise ToolError(f"#{listing_id} isn't live at Etsy ({etsy_publisher.state_text(row)}): pin a live listing")
    _listing_stopped(conn, ctx.scope, listing_id, "pin")  # 0.23.3
    _listing_line(ctx, conn, listing_id, "pin")  # 0.28.0
    board_id = str(args.get("board_id") or "").strip() or None
    board_name = pinterest.one_line(args.get("board_name") or "") or None
    if (board_id is None) == (board_name is None):
        raise ToolError("give board_id (one of your boards) or board_name (a new board), one of them")
    made = pinterest_publisher.boards(conn, ctx.scope)
    if board_id is not None:
        found = next((b for b in made if b["board_id"] == board_id), None)
        if found is None:
            raise ToolError(f"{board_id} isn't one of your boards; pinterest_boards lists them")
        board = f"{found['name']} ({board_id})"
    else:
        same = next((b for b in made if str(b["name"]).lower() == str(board_name).lower()), None)
        if same is not None:
            raise ToolError(f"you have a board named {same['name']!r}: give its board_id, {same['board_id']}")
        board = f"{board_name} (a new board: Ember's code makes it first)"
    path = args["image"].strip()
    try:
        data = ctx.workspace.read_bytes(path)
    except SandboxError as exc:
        raise ToolError(str(exc)) from None
    try:
        upload = pinterest.image(path, data)
        width, height = images.png_size(data)
    except pinterest.PinterestError as exc:
        raise ToolError(str(exc)) from None
    except images.ImageError as exc:  # 0.15.0: what is wrong with it (a print-size poster was "not a picture")
        raise ToolError(f"{path} can't be used: {exc}") from None
    title = pinterest.one_line(args["title"])
    if not title:
        raise ToolError("title is empty")
    pin = pinterest.Pin(
        title=title,
        description=args["description"].strip(),
        link=etsy.listing_url(listing_id),
        alt_text=pinterest.one_line(args.get("alt_text") or ""),
        image=upload,
        width=width,
        height=height,
        board_id=board_id,
        board_name=board_name,
    )
    reason = args["reason"].strip()
    payload = pinterest.payload(pin, board)
    replaced: list[int] = []
    if store.pending_approval_by_payload(conn, ctx.scope, store.sha256(payload)) is None:  # that one: as before
        replaced = _repeated_pin(conn, ctx.scope, pin)  # 0.37.8
        _room_in_queue(ctx, conn, "pinterest", replaced, publish_queue.keys([pin.link]))
        for old in replaced:
            store.withdraw_request(conn, ctx.scope, old, REPLACED_PIN, ctx.cycle_id, ctx.now())
    made_id = _new_request(
        ctx,
        conn,
        payload,
        pin.to_action(),
        type="publish",
        title=_cut(f"Pin: {pin.title}", 120),
        description=reason,
        expected_cost="none: Pinterest charges nothing for a pin",
        expected_benefit=reason,
        executor="pinterest_pin",
    )
    if isinstance(made_id, str):
        return Outcome(True, made_id, "duplicate pin")
    text = (
        f"Approval request #{made_id} is waiting for your owner. Nothing is on Pinterest yet. If they approve it, "
        f"Ember's code makes the pin on {account.username}'s account"
        + (f" and the board {board_name!r} first" if board_name else "")
        + f" in its slot: {_slot_words(ctx, conn, 'pinterest', account.daily_limit, made_id)}. You hear the result."
    )
    if replaced:
        text += f" It replaces request {_numbers(replaced)} (withdrawn): a pin of this listing in the same words."
    short = qa.defects("pinterest.create_pin", pin)  # the QA registry: your owner sees it too
    if short:
        # 0.37.8: "propose it again" made a second pin of the first (#89 and #90 went out 2 seconds apart, live)
        text += (
            f" QA (Ember's code): {'; '.join(short)}: make one with make_image (shape pin) and propose the pin again,"
            f" in the same words: it replaces #{made_id}."
        )
    text += _unlocked(ctx)
    return Outcome(True, text, f"#{made_id} pin: {_cut(pin.title, 60)}")


REPLACED_PIN = "replaced by a new request for a pin of the same listing in the same words"


def _slot_words(ctx: ToolContext, conn: Any, channel: str, per_day: int, approval_id: int) -> str:
    """0.37.8: when a request just made would go out if the owner approved it now, after the channel's queue, with
    the queue's rules."""
    slot = publish_queue.if_approved_now(conn, ctx.scope, ctx.clock, channel, per_day, approval_id)
    rules = publish_queue.rules(channel, per_day)
    if slot is None:
        return rules
    at = publish_queue.when(ctx.clock, slot.at)
    then = "it would go out at once" if at == "now" else f"it would go out {at}"
    cause = publish_queue.why(channel, slot.why, per_day, slot.language)
    return f"{rules}; approved now, {then}" + (f" ({cause})" if slot.why not in ("now", "gap") else "")


def _room_in_queue(ctx: ToolContext, conn: Any, channel: str, replaced: list[int], links: tuple[str, ...] = ()) -> None:
    """0.37.8: a channel takes no more of the agent's requests while publish_queue.WAITING of them wait for the owner or
    for their slot (preconditions.QUEUE). 0.39.6: approved ones of one listing count once there, and a request for a
    listing (``links``, the queue's keys) that has an approved one waiting for its slot is refused
    (preconditions.SLOT)."""
    call = {"channel": channel, "links": tuple(links), "replaced": tuple(replaced)}
    _refuse(ctx, conn, preconditions.SLOT, **call)
    _refuse(ctx, conn, preconditions.QUEUE, **call)


def _same_pin(pin: pinterest.Pin, other: pinterest.Pin) -> str:
    """0.37.8: what ``pin`` repeats of another pin of its listing ("" when nothing): its picture or its words."""
    if pin.image.sha256 == other.image.sha256:
        return "picture"
    if bluesky.same_words(pin.title, other.title):
        return "title"
    return "description" if bluesky.same_words(pin.description, other.description) else ""


def _repeated_pin(conn: Any, scope: AgentScope, pin: pinterest.Pin) -> list[int]:
    """0.37.8: the requests ``pin`` repeats, for the same listing (its picture, its title or its description): one that
    waits for the owner it replaces (returned, to withdraw); one approved, being made or live refuses it. Live on 10-10,
    propose_pin's QA note said "propose it again" after it had queued #89, and #90 went out 2 seconds after it: the
    same words for the same listing, which Pinterest reads as spam and shows less."""
    where, params = scope.where("a")
    rows = conn.execute(
        f"SELECT a.id, a.status, a.action, p.status AS made FROM approvals a LEFT JOIN pinterest_pins p ON"
        f" p.approval_id = a.id WHERE {where} AND a.executor = 'pinterest_pin' AND (a.status IN ('pending', 'approved',"
        " 'approved_with_changes') OR p.status IN ('running', 'active', 'unclear')) ORDER BY a.id DESC",
        params,
    ).fetchall()
    replaced = []
    for row in rows:
        try:
            other = pinterest.pin_from_action(row["action"])
        except pinterest.PinterestError:
            continue
        what = _same_pin(pin, other) if other.link == pin.link else ""
        if not what:
            continue
        if row["status"] == "pending" and row["made"] is None:
            replaced.append(int(row["id"]))
            continue
        state = "is on Pinterest" if row["made"] else "waits for its slot in the queue"
        raise ToolError(
            f"it repeats the {what} of request #{row['id']}'s pin of this listing, which {state}: a listing's next pin"
            " needs a new picture and new words (or none)"
        )
    return replaced


_ETSY_LISTING = re.compile(
    r"^https://(?:www\.)?etsy\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?listing/(\d{1,18})(?:[/?#][^\s]*)?$", re.IGNORECASE
)


def _bluesky(ctx: ToolContext) -> BlueskyAccess:
    if ctx.bluesky is None:
        raise ToolError("Ember's Bluesky account isn't set up")
    return ctx.bluesky


def _bluesky_posts(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.37.7: with the followers, the posts made today and the reactions per post, each post by its request's number,
    and what Bluesky doesn't count (views, clicks): what the channel is judged by."""
    account = _bluesky(ctx)
    made = bluesky_publisher.posts(conn, ctx.scope, 12)
    n = account.followers
    followers = "" if n is None else f"{n} follower{'' if n == 1 else 's'}; "
    today = bluesky_publisher.created_today(conn, ctx.clock, ctx.scope)
    head = (
        f"Ember's Bluesky account: @{account.handle} ({followers}{today} posted today, at most {account.daily_limit}"
        " a day)"
    )
    if not made:
        queue = publish_queue.text(conn, ctx.scope, ctx.clock, "bluesky", account.daily_limit)  # 0.37.8
        return Outcome(True, f"{head}.\nNo post of yours yet." + (f"\n{queue}" if queue else ""), "0 posts")
    synced = max((str(r["synced_at"]) for r in made if r["synced_at"]), default=None)
    read = f"Numbers as Ember's code read them at {_local(ctx, synced)}" if synced else "Numbers not read yet"
    listed = bluesky_publisher.text(conn, ctx.scope, ctx.clock, 12)
    text = (
        f"{head}; {bluesky_publisher.summary(conn, ctx.scope)}.\n{listed}\n"
        f"{read}. Bluesky counts no views or clicks: the listings a post links show their views (etsy_listing)."
    )
    queue = publish_queue.text(conn, ctx.scope, ctx.clock, "bluesky", account.daily_limit)  # 0.37.8
    return Outcome(True, text + (f"\n{queue}" if queue else ""), f"{len(made)} posts")


def _post_link(ctx: ToolContext, conn: Any, raw: str, what: str = "link") -> tuple[str, str, str, etsy.Upload | None]:
    """0.19.0: a post's link, checked: one of Ember's live Etsy listings (its card: the listing's title and main
    photo) or a page of the owner's website (a blog post of Ember's: its title and description on the card). Returns
    the link, the card's title and description ("": the link shows in the words) and its photo. 0.25.1: a second link
    is checked the same way (``what``: "the second link"; its address only: it shows in the words). 0.54.0: or one of
    her KDP books at Amazon (``_book_link``)."""
    link = raw.strip()
    parts = urlsplit(link)
    if parts.scheme != "https" or not parts.netloc or any(ch.isspace() for ch in link):
        raise ToolError(f"{what} must be an https address")
    listing = _ETSY_LISTING.match(link)
    if listing is not None:
        listing_id = int(listing.group(1))
        try:
            current = etsy_publisher.current_listing(conn, ctx.scope, listing_id)
        except etsy.EtsyError as exc:
            raise ToolError(f"Ember's record of #{listing_id} isn't readable ({exc})") from None
        row = etsy_publisher.listing_row(conn, ctx.scope, listing_id)
        made = None
        if current is None or row is None:  # 0.32.0: or one Printify made of a product of Ember's
            row = made = printify_publisher.live_listing(conn, ctx.scope, listing_id)
        if row is None:
            raise ToolError(f"#{listing_id} isn't one of your live listings; etsy_listing without a number lists them")
        if etsy_publisher.etsy_state(row) != etsy.LIVE_STATE:
            raise ToolError(f"#{listing_id} isn't live at Etsy ({etsy_publisher.state_text(row)}): link a live listing")
        _listing_stopped(conn, ctx.scope, listing_id, "post")  # 0.23.3
        _listing_line(ctx, conn, listing_id, "post")  # 0.28.0
        if made is not None:  # its card: the product's title (Printify keeps its photos, which aren't Ember's files)
            return etsy.listing_url(listing_id), bluesky.one_line(made["title"])[: bluesky.CARD_TITLE_MAX], "", None
        photo = current.photos[0] if current.photos else None
        return etsy.listing_url(listing_id), bluesky.one_line(current.title)[: bluesky.CARD_TITLE_MAX], "", photo
    if kdp.at_amazon(link):
        return _book_link(ctx, conn, link)
    site = _bluesky(ctx).site_url
    if site and urlsplit(site).netloc.lower() == parts.netloc.lower():
        # 0.19.2: only a page Ember's code knows is there (live, two posts linked missing pages of the blog)
        page = site_publisher.known_page(conn, ctx.db, ctx.scope, site, link, site_pages=ctx.site is not None)
        if page is None:
            pages = ", ".join(site_publisher.known_pages(conn, ctx.db, ctx.scope, site))
            raise ToolError(
                f"{link} isn't a page of your owner's website that Ember's code knows is there: link one of {pages} "
                "(a blog post's address ends in .html, as BLOG gives it)"
            )
        address, post = page
        if post is not None:
            title, about = bluesky.one_line(post["title"]), bluesky.one_line(post["description"])
            return address, title[: bluesky.CARD_TITLE_MAX], about[: bluesky.CARD_TITLE_MAX], None
        return address, "", "", None
    where = f" ({site})" if site else " (your owner hasn't set its address, site_url)"
    raise ToolError(
        f"{what} goes to one of your live Etsy listings (https://www.etsy.com/listing/...), one of your KDP books at "
        f"Amazon or a page of your owner's website{where}: other sites aren't yours to promote"
    )


def _book_link(ctx: ToolContext, conn: Any, link: str) -> tuple[str, str, str, None]:
    """0.54.0: a post's link to one of her KDP books at Amazon: a book whose link the owner recorded on its request
    (kdp.linked), by that link or by its ASIN in any Amazon store (a German post links amazon.de, though the owner
    recorded amazon.com), posted as the store's plain address. Its card: the book's title and subtitle. Live, the
    Kassenbuch's and the Household Budget Book's addresses at amazon.de were refused ("other sites aren't yours to
    promote"), and the plan's KDP section said a book with its link could be linked."""
    books = kdp.linked(conn, ctx.scope)
    number = kdp.asin(link)
    book = next((b for b in books if link == b.link or (number and number == kdp.asin(b.link))), None)
    if book is None and not books:
        raise ToolError(
            "none of your KDP books has its link at Amazon in Ember's records: your owner adds it on the book's "
            "request (Add its Amazon link), and until then no post can link it (a link in a message isn't recorded)"
        )
    if book is None:
        known = "; ".join(f"#{b.request_id} {b.title}: {b.link}" for b in books)
        raise ToolError(
            f"{link} isn't one of your KDP books at Amazon that Ember's code knows: link one of {known} (by its "
            "address, or as https://www.amazon.de/dp/ and its ASIN, in any Amazon store)"
        )
    if book.project_id is not None:
        stopped = _stopped(conn, ctx.scope, book.project_id)
        if stopped:
            raise ToolError(f"{stopped}: no post for its book #{book.request_id}")
    _line(ctx, conn, book.project_id, "post")
    store = kdp.amazon_store(link)
    address = kdp.store_link(store, number) if store and number else book.link
    title = bluesky.one_line(book.title)[: bluesky.CARD_TITLE_MAX]
    return address, title, bluesky.one_line(book.subtitle)[: bluesky.CARD_TITLE_MAX], None


def _propose_bluesky_post(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.19.0: a post on the Bluesky account the owner made for Ember."""
    account = _bluesky(ctx)
    text = bluesky.words(args["text"])
    try:
        bluesky.check_words(text)
    except bluesky.BlueskyError as exc:
        raise ToolError(str(exc)) from None
    link = title = description = second = None
    photo = None
    addresses = (args.get("link") or "").split()  # 0.25.1: a second one after a space, shown in the words
    if len(addresses) > 2:
        raise ToolError("link takes one address, or two separated by a space")
    if addresses:
        link, title, description, photo = _post_link(ctx, conn, addresses[0])
    if len(addresses) == 2:
        second = _post_link(ctx, conn, addresses[1], "the second link")[0]
        if second == link:
            raise ToolError(f"link names {link} twice: give two different addresses, or one")
    upload = None
    width = height = 0
    alt = bluesky.one_line(args.get("alt_text") or "")
    path = (args.get("image") or "").strip()
    if path:
        try:
            data = ctx.workspace.read_bytes(path)
        except SandboxError as exc:
            raise ToolError(str(exc)) from None
        try:
            upload = bluesky.image(path, data)
            width, height = images.png_size(data)
        except bluesky.BlueskyError as exc:
            raise ToolError(str(exc)) from None
        except images.ImageError as exc:
            raise ToolError(f"{path} can't be used: {exc}") from None
        if not alt:
            raise ToolError("alt_text is missing: say what the picture shows, for people who can't see it")
    elif alt:
        raise ToolError("alt_text describes a picture: give image too, or leave alt_text out")
    post = bluesky.Post(
        text=text,
        language=args["language"],
        link=link,
        link_title=title or "",
        link_description=description or "",
        image=upload,
        width=width,
        height=height,
        alt_text=alt,
        card_photo=photo if upload is None and title else None,
        tags=tuple(tag for _, _, tag in bluesky.hashtags(text)),
        second_link=second,
    )
    left = bluesky.room(post)
    if left < 0:
        # 0.32.0: the links the words show, if any (a card's isn't in them): "with the link" was said of none too
        links = "the links and " if second else "the link and " if link and not post.card() else ""
        raise ToolError(
            f"the post would be {bluesky.TEXT_MAX - left} characters with {links}the AI line Ember's code adds;"
            f" Bluesky takes {bluesky.TEXT_MAX}: shorten your words by {-left}"
        )
    payload = bluesky.payload(post, account.handle)
    if store.pending_approval_by_payload(conn, ctx.scope, store.sha256(payload)) is None:  # that one: as before
        repeated = _repeated_post(conn, ctx.scope, text)
        if repeated:
            raise ToolError(f"it says what {repeated} says: never the same post twice; write a new one (or none)")
        _room_in_queue(ctx, conn, "bluesky", [], publish_queue.keys([post.link, post.second_link]))  # 0.37.8
    reason = args["reason"].strip()
    made_id = _new_request(
        ctx,
        conn,
        payload,
        post.to_action(),
        type="publish",
        title=_cut(f"Bluesky: {' '.join(text.split())}", 120),
        description=reason,
        expected_cost="none: Bluesky charges nothing for a post",
        expected_benefit=reason,
        executor="bluesky_post",
    )
    if isinstance(made_id, str):
        return Outcome(True, made_id, "duplicate post")
    answer = (
        f"Approval request #{made_id} is waiting for your owner. Nothing is on Bluesky yet. If they approve it, "
        f"Ember's code posts it on @{account.handle} in its slot: "
        f"{_slot_words(ctx, conn, 'bluesky', account.daily_limit, made_id)}. You hear the result. It has {left}"
        " characters to spare."
    )
    short = qa.defects("bluesky.create_post", post)  # the QA registry: your owner sees it too
    if short:
        answer += f" QA (Ember's code): {'; '.join(short)}."
    answer += _unlocked(ctx)
    return Outcome(True, answer, f"#{made_id} post: {_cut(' '.join(text.split()), 60)}")


def _repeated_post(conn: Any, scope: AgentScope, text: str) -> str | None:
    """0.24.0: the live post or the waiting request that says what ``text`` says (bluesky.same_words), or None. Before,
    only a request waiting with exactly the same post was caught: one repeating a live post reached the owner."""
    where, params = scope.where("a")
    rows = conn.execute(
        f"SELECT a.id, a.status, a.action, p.text, p.status AS posted FROM approvals a LEFT JOIN bluesky_posts"
        f" p ON p.approval_id = a.id WHERE {where} AND a.executor = 'bluesky_post' AND (a.status IN ('pending',"
        " 'approved', 'approved_with_changes') OR p.status IN ('running', 'active', 'unclear')) ORDER BY a.id DESC",
        params,
    ).fetchall()
    for row in rows:
        said = [str(row["text"] or "")]
        with contextlib.suppress(bluesky.BlueskyError, ValueError, TypeError, KeyError):
            said.append(bluesky.post_from_action(row["action"]).text)
        if any(bluesky.same_words(text, words) for words in said if words):
            if row["posted"] in ("running", "active", "unclear"):
                return f"your post of request #{row['id']} on Bluesky"  # 0.37.7: as bluesky_posts names it
            return f"request #{row['id']}, which waits for your owner"
    return None


def _site_page(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.13.0 (Phase E3): write a page of the owner's website, or take one off. Ember's code builds the site; the
    owner publishes it."""
    if ctx.site is None:
        raise ToolError("your owner hasn't switched their website on")
    slug = args["name"].strip().lower()
    if args.get("remove"):
        if not website.remove(conn, ctx.scope, slug, ctx.now()):
            raise ToolError(f"the site has no page {slug!r}")
        text = f"Page {slug!r} is off the site: your owner's next download of it leaves it out."
        return Outcome(True, text, f"took page {slug} off the site")
    missing = [name for name in ("source", "title", "description") if not (args.get(name) or "").strip()]
    if missing:
        raise ToolError(f"a page needs {', '.join(missing)} (or remove, to take it off)")
    path = args["source"].strip()
    if not path.lower().endswith((".md", ".txt")):
        raise ToolError("source must be the .md (or .txt) file you wrote the page in")
    try:
        source = ctx.workspace.read(path)
    except SandboxError as exc:
        raise ToolError(str(exc)) from None
    try:
        page = site.check(slug, args["title"], args["description"], source, args.get("menu") or "")
        new = website.save(conn, ctx.scope, page, ctx.cycle_id, ctx.now())
    except site.SiteError as exc:
        raise ToolError(str(exc)) from None
    now = website.state(conn, ctx.scope, ctx.site)
    lines = [
        f"Page {page.slug!r} is {'on' if new else 'rewritten on'} the site ({len(now['pages'])} of {site.MAX_PAGES} "
        "pages). Your owner previews it, downloads it and publishes it themselves."
    ]
    lines += [f"Note: {note}." for note in page.notes]
    if now["problem"]:
        lines.append(f"The site can't be built yet: {now['problem']}.")
    return Outcome(True, "\n".join(lines), f"{'wrote' if new else 'rewrote'} page {page.slug}")


def _blog(ctx: ToolContext) -> BlogAccess:
    if ctx.blog is None:
        raise ToolError("your owner hasn't switched their blog on")
    if ctx.blog.problems:
        raise ToolError(f"your owner's blog can't be published yet: {'; '.join(ctx.blog.problems)}. Tell them")
    return ctx.blog


def _withdraw_older(ctx: ToolContext, conn: Any, executor: str, slug: str | None, what: str) -> list[int]:
    """0.14.0: a waiting request for the same page is replaced by the new one (an approved one is about to go up)."""
    replaced = []
    for r in site_publisher.waiting_for(conn, ctx.scope, executor, slug):
        if r["status"] != "pending":
            raise ToolError(
                f"request #{r['id']} for {what} is approved and about to be uploaded: propose a change after you heard "
                "it is online"
            )
        why = f"replaced by a new version of {what}"
        store.withdraw_request(conn, ctx.scope, int(r["id"]), why, ctx.cycle_id, ctx.now())
        replaced.append(int(r["id"]))
    return replaced


def _propose_blog_post(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.14.0: a post for the owner's blog, rendered by Ember's code now (the owner previews exactly this page) and
    uploaded with the blog's list once they approve it."""
    access = _blog(ctx)
    path = args["source"].strip()
    if not path.lower().endswith((".md", ".txt")):
        raise ToolError("source must be the .md (or .txt) file you wrote the post in")
    try:
        source = ctx.workspace.read(path)
    except SandboxError as exc:
        raise ToolError(str(exc)) from None
    try:
        post = blog.read_post(source, access.owner)
    except blog.BlogError as exc:
        raise ToolError(f"{path}: {exc}") from None
    recommended = ventures.LISTING_LINK.search(post.product_url) if post.product_url else None
    if recommended is not None:  # 0.23.3: no page recommends a listing whose work the owner's park stopped
        _listing_stopped(conn, ctx.scope, int(recommended[1]), "blog post recommending it")
        _listing_line(ctx, conn, int(recommended[1]), "blog post recommending it")  # 0.28.0
    earlier = site_publisher.known(conn, ctx.scope, post.slug)
    day = str(earlier["day"]) if earlier is not None else ctx.clock.today().isoformat()
    page = blog.render_post(post, day, access.owner)
    found = blog.audit(page, access.owner)
    if found:  # never: the page is escaped text in a fixed template
        raise ToolError(f"the page didn't pass Ember's check ({'; '.join(found)})")
    url = f"{access.owner.url}/{post.path}"
    lines = [
        f"{'Update of the' if earlier is not None else 'New'} blog post {url}"
        + (f" (online since {earlier['day']}; its date stays)" if earlier is not None else ""),
        f"Title: {post.title}",
        f"Description (search results): {post.description}",
        f"Lead: {post.lead}",
    ]
    if post.product_url:
        lines.append(f"Recommends: {post.product_name} ({post.product_url})")
    lines += ["", "Ember's code uploads exactly the page your preview shows, and adds it to the blog's list.", ""]
    head = "\n".join(lines)
    room = 7_900 - len(head)
    body = post.body if len(post.body) <= room else post.body[: room - 60].rstrip() + "\n\n[... the preview shows all]"
    payload = head + body
    action = {
        "path": post.path,
        "slug": post.slug,
        "title": post.title,
        "description": post.description,
        "date": day,
        "sha256": blog.sha256(page),
    }
    if store.pending_approval_by_payload(conn, ctx.scope, store.sha256(payload)) is None:
        replaced = _withdraw_older(ctx, conn, site_publisher.POST, post.slug, post.path)
    else:
        replaced = []
    reason = args["reason"].strip()
    made = _new_request(
        ctx,
        conn,
        payload,
        action,
        public=[post.body],  # 0.45.0 (C1): the article itself, which its action names by its hash
        type="publish",
        title=_cut(f"Blog post: {post.title}", 120),
        description=reason,
        expected_cost="none: the upload to your web host is free",
        expected_benefit=reason,
        executor=site_publisher.POST,
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate blog post")
    site_publisher.propose(conn, ctx.scope, made, post.path, page)
    text = (
        f"Approval request #{made} is waiting for your owner: they preview the page and approve it or not. Nothing "
        f"is online yet. If they approve it, Ember's code uploads {post.path} and adds it to the blog's list "
        f"({access.owner.url}/blog/), and you hear the result."
    )
    if earlier is not None:
        text += f" It replaces the post online since {earlier['day']} (its date stays)."
    if replaced:
        text += f" It replaces request {', '.join(f'#{n}' for n in replaced)} (withdrawn)."
    text += "".join(f" Note: {note}." for note in post.notes)
    text += _unlocked(ctx)
    return Outcome(True, text, f"#{made} blog post: {post.slug}")


def _propose_link_page(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.14.0: the owner's link page as a whole, rendered now and uploaded once they approve it."""
    access = _blog(ctx)
    try:
        bio, links = blog.read_links(args["bio"], args["links"], access.owner)
    except blog.BlogError as exc:
        raise ToolError(str(exc)) from None
    page = blog.render_links(bio, links, access.owner)
    found = blog.audit(page, access.owner)
    if found:
        raise ToolError(f"the page didn't pass Ember's check ({'; '.join(found)})")
    url = f"{access.owner.url}/{blog.LINKS}"
    payload = "\n".join(
        [
            f"The link page {url}, as a whole:",
            f"Bio: {bio}",
            *(
                f"{i}. {link.label}" + (f" ({link.note})" if link.note else "") + f" -> {link.url}"
                for i, link in enumerate(links, 1)
            ),
        ]
    )
    action = {
        "path": blog.LINKS,
        "bio": bio,
        "links": [{"label": x.label, "note": x.note, "url": x.url} for x in links],
        "sha256": blog.sha256(page),
    }
    if store.pending_approval_by_payload(conn, ctx.scope, store.sha256(payload)) is None:
        replaced = _withdraw_older(ctx, conn, site_publisher.LINKS, None, "the link page")
    else:
        replaced = []
    reason = args["reason"].strip()
    made = _new_request(
        ctx,
        conn,
        payload,
        action,
        type="publish",
        title=_cut(f"Link page: {len(links)} links", 120),
        description=reason,
        expected_cost="none: the upload to your web host is free",
        expected_benefit=reason,
        executor=site_publisher.LINKS,
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate link page")
    site_publisher.propose(conn, ctx.scope, made, blog.LINKS, page)
    text = (
        f"Approval request #{made} is waiting for your owner: they preview the page and approve it or not. If they "
        f"approve it, Ember's code uploads it as {url} and you hear the result."
    )
    if replaced:
        text += f" It replaces request {', '.join(f'#{n}' for n in replaced)} (withdrawn)."
    text += _unlocked(ctx)
    return Outcome(True, text, f"#{made} link page")
