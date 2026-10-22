"""0.42.2 (analysis-0.40.1, 5.6): the tools that are metered model calls of their own, split out of tools.py.

research (Anthropic's server-side web tools: a question asked again within RESEARCH_REPEAT_DAYS is answered from
before, and no address in a question may carry data out), brainstorm, draft (a long file in one reply; it reads
DRAFT_BYTES through tools, where tests patch it) and workshop. tools.run holds no database transaction while they run
(CALLING_TOOLS, with demand_note and printify_catalog, which stay with their channel in tools_listings.py).
"""

from __future__ import annotations

import contextlib
import json
import re
import unicodedata
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from ..economy.clock import from_iso, to_iso
from ..economy.costs import micros_to_usd
from . import store, ventures
from .sandbox import Jail, SandboxError
from .tools_context import Outcome, ToolContext, ToolError, wrap
from .tools_files import _keep_version, _restorable
from .tools_limits import (
    _DOCUMENT,
    _SITE,
    _WRAPPED,
    DRAFT_SOURCE_CHARS,
    DRAFT_SOURCES,
    ETSY_DOMAINS,
    REPEAT_LOOKBACK,
    RESEARCH_REPEAT_DAYS,
    UNREACHABLE,
    UNREACHABLE_DOMAINS,
)
from .tools_lines import _line_venture
from .tools_projects import _researched_venture


def _brainstorm(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    if ctx.brainstorm is None:
        raise ToolError("brainstorming isn't available right now")
    return ctx.brainstorm(" ".join((args.get("theme") or "").split()), args.get("venture_id"))


def _draft(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    """0.12.0: a long text file written by a call of its own and saved in the workspace; everything that can be
    refused is checked before the call is paid for."""
    from .tools import DRAFT_BYTES  # 0.42.2: read through tools, where tests patch it

    if ctx.draft is None:
        raise ToolError("drafting isn't available right now")
    path, mode = args["path"].strip(), args.get("mode") or "create"
    ctx.workspace.parts(path)  # a text file's path, or refused
    exists = ctx.workspace.exists(path)
    if mode == "create" and exists:
        raise ToolError(f"{path} already exists: overwrite it, append to it, or name a new file")
    if mode == "append" and not exists:
        raise ToolError(f"{path} doesn't exist yet: create it first")
    limit = ctx.workspace.limits.max_file_bytes
    size = (ctx.workspace.size_of(path) or 0) if mode == "append" else 0
    if limit - size < DRAFT_BYTES:  # 0.15.0: refused before it is paid for, not after
        raise ToolError(
            f"{path} holds {size // 1024} KB and a draft may add {DRAFT_BYTES // 1024} KB, more than the "
            f"{limit // 1024} KB a file holds: draft the rest into a new file, with {path} as a source"
        )
    brief = args["brief"].strip()
    if not brief:
        raise ToolError("the brief is empty")
    names = [n.strip() for n in (args.get("sources") or "").split(",") if n.strip()]
    if len(names) > DRAFT_SOURCES:
        raise ToolError(f"a draft builds on at most {DRAFT_SOURCES} files")
    sources, left = [], DRAFT_SOURCE_CHARS
    for name in dict.fromkeys(names):
        text = ctx.workspace.read(name)  # a text file of the workspace, or refused
        if len(text) > left:
            more = f"\n[... the rest of {name} left out: a draft's sources hold {DRAFT_SOURCE_CHARS:,} characters]"
            text = text[: max(0, left)] + more
        left -= len(text)
        sources.append(wrap(ctx, name, text))
    drafted = ctx.draft(brief, "\n\n".join(sources))
    if isinstance(drafted, Outcome):
        return drafted
    cost = f"(cost ${micros_to_usd(drafted.cost_micros):.4f})"
    moved = ""
    if mode == "append" and size + len(drafted.text.encode("utf-8")) > limit:
        # 0.15.0: a paid draft longer than the file's room is kept in a file of its own, not thrown away
        path, mode, moved = _free_name(ctx.workspace, path), "create", f" It didn't fit in {path} ({limit // 1024} KB)."
    try:
        # 0.40.3 (analysis-0.40.1 codebase, T2): the text an overwrite replaces is kept, as workspace_write's is
        with ctx.db.transaction() as conn:
            kept = mode == "overwrite" and _keep_version(ctx, conn, path, "overwrite")
            size = ctx.workspace.write(path, drafted.text, append=mode == "append", create_only=mode == "create")
    except SandboxError as exc:
        return Outcome(False, f"Error: the draft was written but can't be saved ({exc}). {cost}", "refused", paid=True)
    go_on = "with mode append and" if limit - size >= DRAFT_BYTES else "into a new file, with"
    cut = (
        f" It was cut off at its length limit: read its end with workspace_read, then draft the rest {go_on} "
        f"{path} as a source."
        if drafted.cut_off
        else ""
    )
    return Outcome(
        True,
        f"{'Appended to' if mode == 'append' else 'Wrote'} {path}: {len(drafted.text):,} characters, now {size:,} "
        f"bytes.{moved}{_restorable(kept)}{cut} Read it with workspace_read before you use it. {cost}",
        f"draft {path}",
    )


def _free_name(workspace: Jail, path: str) -> str:
    """0.15.0: the first of <name>-2.md, <name>-3.md, ... next to ``path`` that can be written and doesn't exist yet
    (``path`` itself if none of the first 98 is: then the write is refused as before)."""
    stem, _, suffix = path.rpartition(".")  # a text file's name always has its suffix
    for number in range(2, 100):
        candidate = f"{stem}-{number}.{suffix}"
        try:
            workspace.parts(candidate)
        except SandboxError:
            return path  # the name is as long as a name can be
        if not workspace.exists(candidate):
            return candidate
    return path


def _research(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    url = args.get("url")
    site = args.get("site")
    if site is not None:
        site = site.strip().lower()
        if not _SITE.match(site):
            raise ToolError("site must be a bare domain like etsy.com (no https://, no path)")
        if _on(site, UNREACHABLE_DOMAINS):
            raise ToolError(UNREACHABLE)
    if url is not None:
        if not ctx.allow_fetch:
            raise ToolError("reading whole pages is switched off by your owner; search instead")
        if not url.startswith("https://") or any(c.isspace() for c in url):
            raise ToolError("the url must start with https:// and contain no spaces")
        if _on_etsy(url):
            raise ToolError(
                "Etsy's pages can't be read by a program (Etsy's API terms forbid it); search instead, for example"
                " with site 'etsy.com'"
            )
        if _on(_host(url), UNREACHABLE_DOMAINS):
            raise ToolError(UNREACHABLE)
        if url not in ctx.state.seen_urls:
            # Otherwise a URL could carry data out of the container (in its path or query) without approval.
            raise ToolError("you can only read pages that appeared in your research results this cycle")
        if _DOCUMENT.search(urlsplit(url).path):
            # The page reader's size limit doesn't apply to PDFs and other documents: one can cost dollars.
            raise ToolError("documents such as PDFs can't be read (they can cost dollars each); look for a web page")
    question = args["question"].strip()
    if not question:
        raise ToolError("the question is empty")
    if url is not None and _names_an_address(question, url):
        # 0.21.0: the page reader may read an address in the request, the question's too: one could carry data out
        raise ToolError(
            "when you read a page, the question names no web address and nothing on the page's site: only what to find"
            " on the page"
        )
    # 0.12.0: research counts for a venture: the one it names or, in a venture cycle, the focus venture (a venture
    # cycle's research is always a venture's). A question asked again is answered from before, free; a new call for a
    # venture that isn't backed needs what is left of its research budget. 0.15.0: in any other cycle too, research
    # counts for a focus venture that has a research budget (it escaped the budget, while its cost was the venture's);
    # 0.28.0: for the venture of the cycle's line.
    venture_id = args.get("venture_id")
    if venture_id is None and ctx.venture:
        if ctx.state.focus_venture_id is None:
            raise ToolError("name the venture it researches (venture_id): a venture cycle's research is a venture's")
        venture_id = ctx.state.focus_venture_id
    with ctx.db.connection() as conn:
        if venture_id is None and ctx.state.focus_venture_id is not None:
            focus = ventures.get(conn, ctx.scope, ctx.state.focus_venture_id)
            if focus is not None and focus["stage"] in ventures.BUDGETED:
                venture_id = ctx.state.focus_venture_id
        if venture_id is None and not ctx.venture and ctx.state.focus_project_id is not None:
            # 0.28.0: a cycle on one line has no focus venture: its research counts for the line's own venture while
            # that isn't backed (its research budget)
            line = store.project(conn, ctx.scope, ctx.state.focus_project_id)
            own = ventures.get(conn, ctx.scope, line["venture_id"]) if line and line["venture_id"] else None
            if own is not None and own["stage"] in ventures.EXPLORING:
                venture_id = int(own["id"])
        venture = _researched_venture(conn, ctx.scope, venture_id, "research") if venture_id is not None else None
        _line_venture(ctx, conn, venture_id, "research")  # 0.28.0
        earlier = _asked_before(conn, ctx, question, url, None if url else site)
        if earlier is not None:
            return earlier
        refusal = ventures.research_refusal(venture) if venture is not None else ""
        if refusal:
            raise ToolError(refusal)
    if ctx.research is None:
        raise ToolError("research isn't available right now")
    return ctx.research(question, url, ctx.cycle_id, None if url else site, venture_id)


def _asked_before(conn: Any, ctx: ToolContext, question: str, url: str | None, site: str | None) -> Outcome | None:
    """The answer to the same question (its words, whatever their case and punctuation; the same page or site) asked
    within RESEARCH_REPEAT_DAYS that found web pages, or None. It is free, doesn't count as research for a venture (no
    call was made) and names what it repeats; its pages may be read again, like this cycle's results."""
    since = to_iso(from_iso(ctx.now()) - timedelta(days=RESEARCH_REPEAT_DAYS))
    rows = conn.execute(
        "SELECT t.id, t.cycle_id, t.input, t.result, t.started_at FROM tool_calls t JOIN cycles c ON c.id = t.cycle_id"
        " WHERE c.session = ? AND c.simulated = ? AND t.tool = 'research' AND t.status = 'ok' AND t.parent_id IS NULL"
        " AND t.started_at >= ? AND t.summary NOT LIKE 'research (repeated)%' ORDER BY t.id DESC LIMIT ?",
        (ctx.scope.session, 1 if ctx.scope.simulated else 0, since, REPEAT_LOOKBACK),
    ).fetchall()
    key = (_words(question), url, site)
    for r in rows:
        try:
            before = json.loads(r["input"])
        except ValueError:
            continue
        if not isinstance(before, dict) or not isinstance(before.get("question"), str):
            continue
        site_before = before.get("site") if not before.get("url") else None
        if (_words(before["question"]), before.get("url"), (site_before or "").strip().lower() or None) != key:
            continue
        found = _WRAPPED.search(str(r["result"] or ""))
        pages = re.findall(r"^- (https?://\S+)$", str(r["result"])[found.end() :], re.MULTILINE) if found else []
        if not pages:  # it found nothing then: a repeat is a new try
            continue
        ctx.state.seen_urls.update(pages)
        return Outcome(
            True,
            f"Not researched again: you asked this on {str(r['started_at'])[:10]} (cycle #{r['cycle_id']}), so this "
            "is that answer (free; a repeat doesn't count as research for a venture: ask what it left open).\n"
            + wrap(ctx, "research", found[2])  # as data again, with this cycle's nonce
            + "\nSources:\n"
            + "\n".join(f"- {u}" for u in pages),
            f"research (repeated) of call #{r['id']}: {question[:60]}",
            reused=True,
        )
    return None


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def _on_etsy(url: str) -> bool:
    """Whether ``url`` is a page of Etsy's website or one of its short links (see ETSY_DOMAINS)."""
    return _on(_host(url), ETSY_DOMAINS)


# 0.21.0: in a page read's question, a web address with its scheme, or a host name (shop.example.org, bücher.de)
_SCHEME = re.compile(r"\b[a-z][a-z0-9+.-]*:/+")
# 0.42.2 (analysis-0.40.1 audit): from a name's start only: tried at each of 20,000 '-', it took 4.7 s
_HOST_NAME = re.compile(r"(?<![\w-])[\w-]+(?:\.[\w-]+)+")
_DOTS = re.compile("[\u3002\uff0e\uff61]")  # dots IDNA reads as dots
_UNSEEN = re.compile("[\u00ad\u034f\u180b-\u180e\u200b-\u200f\u2060-\u2064\ufe00-\ufe0f\ufeff]")


def _names_an_address(question: str, url: str) -> bool:
    """0.21.0: whether a page read's question holds a web address (with its scheme), or a host name on the page's site
    (its host, a subdomain of it, or its domain without www), in any form a browser maps to it (NFKC, case, the dots
    IDNA reads as dots, invisible characters, punycode): the page reader reaches only that site (prompts:
    allowed_domains), so another host can't be read, and a file name or a product name like Node.js is no address."""
    text = _DOTS.sub(".", _UNSEEN.sub("", unicodedata.normalize("NFKC", question))).casefold()
    if _SCHEME.search(text):
        return True
    sites = {form.removeprefix("www.") for form in _host_forms(_host(url))} - {""}
    for match in _HOST_NAME.finditer(text):
        for name in _host_forms(match.group(0).strip(".-_")):
            if any(name == site or name.endswith(f".{site}") for site in sites):
                return True
    return False


def _host_forms(host: str) -> set[str]:
    """A host name as written and in punycode (bücher.de, xn--bcher-kva.de)."""
    forms = {host}
    with contextlib.suppress(UnicodeError, ValueError):
        forms.add(host.encode("idna").decode("ascii").lower())
    with contextlib.suppress(UnicodeError, ValueError):
        forms.add(host.encode("ascii").decode("idna").casefold())
    return forms


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:  # not an address anyone could read: refused as not found in the research results
        return ""


def _on(host: str, domains: tuple[str, ...]) -> bool:
    """Whether ``host`` is one of ``domains`` or a subdomain of one."""
    return bool(host) and any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _workshop(ctx: ToolContext, args: dict[str, Any]) -> Outcome:
    if ctx.workshop is None:
        raise ToolError("the workshop isn't available")
    files = [path.strip() for path in (args.get("files") or "").split(",") if path.strip()]
    return ctx.workshop(args["task"].strip(), files, args.get("script"), args.get("folder"))
