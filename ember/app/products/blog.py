"""The blog on the owner's own website (0.14.0): posts and a link page the agent writes, rendered by Ember's code in
the site's design, and published by Ember's code over SFTP once the owner approved them
(integrations/site_publisher.py).

The owner's site is their own (its home pages, Impressum, privacy page and stylesheet are theirs, uploaded by them).
Ember's code writes three kinds of file there and nothing else: a post (blog/<slug>.html), the blog's list of posts
(blog/index.html) and the link page (links.html). Every text is escaped into one fixed template that matches the site:
the same header, footer and Content-Security-Policy (nothing but the site's own stylesheet, font and pictures; no
script, no form, nothing from elsewhere). Each page is checked again before it is uploaded (``audit``).

A post is one Markdown file of the agent's: a front matter block (slug, title, description, lead and, optionally, the
product it recommends) and its text in the products' markdown, limited to what the site's stylesheet shows: headings,
paragraphs, lists, quotes and tables. The blog's list keeps the posts already on the server (``entries``), so posts the
owner uploaded themselves stay listed. The blog is German, like the site's home page.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from . import markup

POSTS = "blog"
INDEX = "blog/index.html"
LINKS = "links.html"
PROJECT_URL = "https://github.com/Stullee/SURVIVE"  # the footer's GitHub link: Ember's own code
SLUG_MAX = 60
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
RESERVED = frozenset({"index"})  # blog/index.html is the list of posts
TITLE_MAX = 100
DESCRIPTION_MAX = 170
LEAD_MAX = 600
PRODUCT_NAME_MAX = 80
PRODUCT_TEXT_MAX = 300
URL_MAX = 300
BODY_MIN = 300
SOURCE_MAX = 24_000
LINKS_MAX = 12
LABEL_MAX = 60
NOTE_MAX = 90
BIO_MAX = 200
PAGE_MAX = 200_000  # bytes: a page Ember's code reads from or writes to the server
POST_KEYS = ("slug", "title", "description", "lead", "product_name", "product_text", "product_url")
IGNORED_KEYS = ("date",)  # Ember's code dates a post: the day it is proposed (an update keeps the first date)
MONTHS = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)
# What the site's pages allow: its own stylesheet, font and pictures, nothing else (the same as its home pages).
CSP = "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'"
INDEX_TITLE = "Blog"
INDEX_HEADING = "Blog"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ETSY = re.compile(r"^https://(?:www\.)?etsy\.com/[^\s<>\"']*$")
_POST_HREF = re.compile(r"^/blog/([a-z0-9-]{1,60})\.html$")
_POSTCODE = re.compile(r"^\d{4,5}\s+")
_ALLOWED_TAGS = frozenset(
    {
        "html", "head", "meta", "title", "link", "body", "a", "header", "nav", "main", "article", "section",
        "aside", "footer", "div", "p", "span", "h1", "h2", "h3", "ul", "ol", "li", "strong", "em", "br", "time",
        "img", "small", "blockquote", "table", "thead", "tbody", "tr", "th", "td",
    }
)  # fmt: skip
_URL_ATTRS = ("href", "src")


class BlogError(ValueError):
    """A post, a link page or a page on the server Ember's code can't use, in words for the agent and the owner."""


@dataclass(frozen=True)
class Owner:
    """The site's data from the owner's options: its name (the header's), the owner's name and town (the footer's),
    the address people may write to, the site's address and the agent's name (the posts' transparency line)."""

    name: str
    legal_name: str
    town: str
    email: str
    url: str  # https, without a trailing slash
    agent: str = "Ember"

    def problems(self) -> list[str]:
        found = []
        if not self.url.startswith("https://"):
            found.append("site_url is missing (your website's address, like https://example.org)")
        if not self.legal_name:
            found.append("site_owner_name is missing")
        if not self.email:
            found.append("site_email is missing")
        return found


def town(address_lines: tuple[str, ...]) -> str:
    """The town of an address (its last line without the postcode), or ""."""
    if not address_lines:
        return ""
    last = address_lines[-1].strip()
    return _POSTCODE.sub("", last).strip() if _POSTCODE.match(last) else ""


@dataclass(frozen=True)
class Post:
    slug: str
    title: str
    description: str
    lead: str
    body: str  # the products' markdown, typeset
    product_name: str = ""
    product_text: str = ""
    product_url: str = ""
    notes: tuple[str, ...] = field(default=(), compare=False)  # for the agent: what the page leaves out

    @property
    def path(self) -> str:
        return post_path(self.slug)


@dataclass(frozen=True)
class Entry:
    """A post in the blog's list."""

    slug: str
    title: str
    description: str
    date: str  # YYYY-MM-DD


@dataclass(frozen=True)
class Index:
    """The blog's list as it is on the server: its heading and introduction (the owner's) and its posts."""

    entries: tuple[Entry, ...] = ()
    heading: str = ""
    lead: str = ""
    description: str = ""


@dataclass(frozen=True)
class Link:
    label: str
    note: str
    url: str


def post_path(slug: str) -> str:
    return f"{POSTS}/{slug}.html"


def allowed(path: str) -> bool:
    """Whether Ember's code may write this file on the owner's server: a post, the blog's list or the link page."""
    if path in (INDEX, LINKS):
        return True
    match = re.fullmatch(r"blog/([a-z0-9-]+)\.html", path)
    return match is not None and SLUG.match(match[1]) is not None and match[1] not in RESERVED


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def date_de(day: str) -> str:
    year, month, number = (int(x) for x in day.split("-"))
    return f"{number}. {MONTHS[month - 1]} {year}"


# --- the agent's words: checked and typeset ------------------------------------------------------------------------


def _quotes(text: str) -> str:
    """German quotation marks for straight ones: „ after a space, a line's start or a bracket, “ otherwise."""
    out = []
    for i, ch in enumerate(text):
        if ch == '"':
            before = text[i - 1] if i else ""
            out.append("„" if not before or before.isspace() or before in "([{/–—-" else "“")
        else:
            out.append(ch)
    return "".join(out)


def typeset(text: str) -> str:
    """The agent's text as the site prints it: German quotation marks, no break inside z. B., d. h. and u. a."""
    text = _quotes(text)
    for short in ("z. B.", "d. h.", "u. a.", "z. T.", "s. o.", "s. u."):
        text = text.replace(short, short.replace(" ", " "))
    return text


def _line(value: str, name: str, limit: int, required: bool = True) -> str:
    text = " ".join(str(value).split())
    if required and not text:
        raise BlogError(f"{name} is missing")
    if len(text) > limit:
        raise BlogError(f"{name} has {len(text)} characters; at most {limit}")
    return text


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _front(source: str) -> tuple[dict[str, str], str, int]:
    """The front matter's keys, the text below it, and the text's first line number."""
    text = markup._CONTROL.sub("", source.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿"))
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
    if start == len(lines) or lines[start].strip() != "---":
        raise BlogError("a post starts with its front matter: a '---' line, the keys (slug, title, ...), a '---' line")
    end = next((j for j in range(start + 1, len(lines)) if lines[j].strip() == "---"), None)
    if end is None:
        raise BlogError("the front matter has no closing '---' line")
    keys: dict[str, str] = {}
    for number, line in enumerate(lines[start + 1 : end], start + 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if not sep or key not in (*POST_KEYS, *IGNORED_KEYS):
            raise BlogError(f"line {number}: the front matter's keys are {', '.join(POST_KEYS)}")
        if key in keys:
            raise BlogError(f"line {number}: {key} is there twice")
        keys[key] = _unquote(value)
    return keys, "\n".join(lines[end + 1 :]), end + 2


def _https(url: str, name: str) -> str:
    if len(url) > URL_MAX or not re.match(r"^https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:[/?#][^\s<>\"']*)?$", url):
        raise BlogError(f"{name} must be an https address (at most {URL_MAX} characters)")
    return url


def _emails(text: str, owner: Owner, name: str) -> None:
    for found in _EMAIL.findall(text):
        if found.lower() != owner.email.lower():
            raise BlogError(f"{name}: {found} isn't your owner's address; write {owner.email} or none")


def read_post(source: str, owner: Owner) -> Post:
    """A post from the agent's Markdown file, checked. Raises BlogError naming what to fix."""
    if len(source) > SOURCE_MAX:
        raise BlogError(f"a post has at most {SOURCE_MAX:,} characters; this file has {len(source):,}")
    keys, body, first = _front(source)
    slug = keys.get("slug", "").strip().lower()
    if not SLUG.match(slug) or slug in RESERVED:
        raise BlogError(
            f"slug is the page's name: 1 to {SLUG_MAX} lower-case letters, digits and dashes (not 'index'), "
            "e.g. 'bewerbung-nachfassen'"
        )
    title = typeset(_line(keys.get("title", ""), "title", TITLE_MAX))
    description = typeset(_line(keys.get("description", ""), "description", DESCRIPTION_MAX))
    lead = typeset(_line(keys.get("lead", ""), "lead", LEAD_MAX))
    product = [keys.get(k, "").strip() for k in ("product_name", "product_text", "product_url")]
    if any(product) and not all(product):
        raise BlogError("the product box needs product_name, product_text and product_url, or none of them")
    name = typeset(_line(product[0], "product_name", PRODUCT_NAME_MAX, required=False))
    text = typeset(_line(product[1], "product_text", PRODUCT_TEXT_MAX, required=False))
    url = product[2]
    if url and not _ETSY.match(_https(url, "product_url")):
        raise BlogError("product_url is the product's page at Etsy (https://www.etsy.com/listing/...)")
    body = typeset(body)  # its lines as in the file: a problem names the file's line
    if len(body.strip()) < BODY_MIN:
        raise BlogError(f"the post's text below the front matter is shorter than {BODY_MIN} characters")
    for part, name_of in ((title, "title"), (description, "description"), (lead, "lead"), (text, "product_text")):
        _emails(part, owner, name_of)
    _emails(body, owner, "the text")
    notes = _body(body, owner, first)  # checks it
    for run in markup.inline(lead):
        if run.url:
            _link(run.url, owner)
    return Post(slug, title, description, lead, body.strip("\n"), name, text, url, tuple(notes))


def _link(url: str, owner: Owner) -> str:
    """A link in the agent's text: https without a user name, or mailto the owner's address."""
    if url.startswith("mailto:"):
        if url[7:].split("?", 1)[0].lower() != owner.email.lower():
            raise BlogError(f"a mailto link goes to your owner's address, {owner.email}, only")
        return url
    host = url[8:].split("/", 1)[0]
    if "@" in host:
        raise BlogError("a link must not hold a user name")
    return url


def _body(body: str, owner: Owner, first: int) -> list[str]:
    """Check a post's text (what the page shows of it); returns notes for the agent. Raises BlogError."""
    try:
        document = markup.parse(body)
    except markup.DocumentError as exc:
        raise BlogError(f"the post's text: {_shift(str(exc), first)}") from None
    if document.sidebar:
        raise BlogError("a post has no sidebar")
    notes = [_shift(w, first) for w in document.warnings]
    dividers = 0
    for block in document.main:
        dividers += _allowed_block(block, owner)
    if dividers:
        notes.append(f"{dividers} divider line{'s are' if dividers > 1 else ' is'} left out: headings divide a post")
    if any(isinstance(b, markup.Heading) and b.level == 1 for b in document.main):
        notes.append("'#' headings are shown as '##': the post's title is its only top heading")
    return notes


def _shift(message: str, first: int) -> str:
    """A parser message's line number counted in the whole file (the text starts below the front matter)."""
    return re.sub(r"^line (\d+)", lambda m: f"line {int(m[1]) + first - 1}", message)


_REFUSED = (
    "a post holds headings, paragraphs, lists, quotes (>) and tables; boxes, columns, centred text, checklists, "
    "photos, writing lines, space and page breaks are for printed documents"
)


def _allowed_block(block: Any, owner: Owner, inside: bool = False) -> int:
    """Raise BlogError for a block a post can't show; the number of dividers (left out)."""
    if isinstance(block, markup.Divider):
        return 1
    if isinstance(block, (markup.Heading, markup.Paragraph)):
        _runs_ok(block.runs, owner)
        return 0
    if isinstance(block, markup.ListBlock):
        for item in block.items:
            _runs_ok(item, owner)
        return 0
    if isinstance(block, markup.Table) and not inside:
        for row in [block.header or [], *block.rows]:
            for cell in row:
                _runs_ok(cell, owner)
        return 0
    if isinstance(block, markup.Callout) and not inside:
        return sum(_allowed_block(b, owner, inside=True) for b in block.blocks)
    raise BlogError(_REFUSED)


def _runs_ok(runs: list[markup.Run], owner: Owner) -> None:
    for run in runs:
        if run.url:
            _link(run.url, owner)


def read_links(bio: str, items: list[dict[str, Any]], owner: Owner) -> tuple[str, list[Link]]:
    """The link page's introduction and buttons, checked. Raises BlogError."""
    bio = typeset(_line(bio, "bio", BIO_MAX))
    _emails(bio, owner, "bio")
    if not 1 <= len(items) <= LINKS_MAX:
        raise BlogError(f"the link page has 1 to {LINKS_MAX} links")
    links = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise BlogError(f"link {number} must be an object with label, note and url")
        label = typeset(_line(item.get("label", ""), f"link {number}'s label", LABEL_MAX))
        note = typeset(_line(item.get("note", ""), f"link {number}'s note", NOTE_MAX, required=False))
        url = str(item.get("url", "")).strip()
        if url.startswith("mailto:"):
            _link(url, owner)
        elif url.startswith("/") and not url.startswith("//"):
            if not re.fullmatch(r"/[A-Za-z0-9._~/#-]{0,200}", url):
                raise BlogError(f"link {number}: a page of the site is a path like /blog/")
        else:
            _link(_https(url, f"link {number}'s url"), owner)
        links.append(Link(label, note, url))
    if len({link.url for link in links}) != len(links):
        raise BlogError("a link is there twice")
    return bio, links


# --- the template: the site's own head, header and footer -----------------------------------------------------------


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _head(owner: Owner, title: str, description: str, path: str, kind: str) -> str:
    site = _e(owner.name)
    address = _e(owner.url + path)
    return (
        '<!doctype html>\n<html lang="de">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{_e(title)} · {site}</title>\n"
        f'<meta name="description" content="{_e(description)}">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="theme-color" content="#121211">\n'
        f'<link rel="canonical" href="{address}">\n'
        f'<meta property="og:type" content="{kind}">\n'
        '<meta property="og:locale" content="de_DE">\n'
        f'<meta property="og:site_name" content="{site}">\n'
        f'<meta property="og:url" content="{address}">\n'
        f'<meta property="og:title" content="{_e(title)}">\n'
        f'<meta property="og:description" content="{_e(description)}">\n'
        f'<meta property="og:image" content="{_e(owner.url)}/og-image.png">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        '<link rel="icon" href="/favicon.ico" sizes="32x32">\n'
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
        '<link rel="preload" href="/fonts/poppins-bold.woff2" as="font" type="font/woff2" crossorigin>\n'
        '<link rel="stylesheet" href="/style.css">\n'
        "</head>\n"
    )


def _header(owner: Owner) -> str:
    site = _e(owner.name)
    return (
        "<body>\n"
        '<a class="skip" href="#main">Zum Inhalt springen</a>\n'
        '<header class="site-header">\n<div class="wrap header-row">\n'
        f'<a class="brand" href="/" aria-label="{site}, zur Startseite"><img class="brand-mark" src="/flame.svg" '
        f'alt=""><span class="brand-name">{site}</span></a>\n'
        '<nav class="site-nav" aria-label="Bereiche">\n<a href="/">Start</a>\n<a href="/#ablauf">So läuft\'s</a>\n'
        '<a href="/#produkte">Produkte</a>\n<a href="/blog/">Blog</a>\n</nav>\n'
        '<nav class="lang-switch" aria-label="Sprache"><a href="/en/" hreflang="en" lang="en">EN<span '
        'class="visually-hidden"> (English)</span></a><a href="/" hreflang="de" aria-current="true">DE<span '
        'class="visually-hidden"> (Deutsch)</span></a></nav>\n'
        '<a class="header-cta" href="/#kontakt">Kontakt</a>\n'
        "</div>\n</header>\n"
    )


def _footer(owner: Owner) -> str:
    where = f" in {_e(owner.town)}" if owner.town else ""
    return (
        '<footer class="site-footer">\n<div class="wrap">\n<div class="footer-top">\n'
        f'<p class="footer-brand"><img class="brand-mark" src="/flame.svg" alt="">{_e(owner.name)}, ein Experiment '
        f"von {_e(owner.legal_name)}{where}</p>\n"
        '<ul class="footer-links"><li><a href="/blog/">Blog</a></li><li><a href="/impressum.html">Impressum</a></li>'
        f'<li><a href="/datenschutz.html">Datenschutz</a></li><li><a href="{PROJECT_URL}">GitHub</a></li></ul>\n'
        '</div>\n<div class="footer-notes">\n'
        "<p>Diese Website setzt keine Cookies und lädt nichts von anderen Anbietern. Ihre Texte entstanden mit Hilfe "
        "einer KI und wurden vor der Veröffentlichung geprüft.</p>\n"
        "<p lang=\"en\">The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not "
        "endorsed or certified by Etsy, Inc.</p>\n"
        "</div>\n</div>\n</footer>\n</body>\n</html>\n"
    )


def _runs(runs: list[markup.Run]) -> str:
    out = []
    for run in runs:
        text = "<br>".join(_e(part) for part in run.text.split("\n"))
        if run.bold:
            text = f"<strong>{text}</strong>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.url:
            text = f'<a href="{_e(run.url)}">{text}</a>'
        out.append(text)
    return "".join(out)


def _block(block: Any) -> str:
    if isinstance(block, markup.Heading):
        level = 3 if block.level == 3 else 2  # the post's title is its h1
        return f"<h{level}>{_runs(block.runs)}</h{level}>"
    if isinstance(block, markup.Paragraph):
        return f"<p>{_runs(block.runs)}</p>"
    if isinstance(block, markup.ListBlock):
        tag = "ol" if block.ordered else "ul"
        return f"<{tag}>\n" + "\n".join(f"<li>{_runs(item)}</li>" for item in block.items) + f"\n</{tag}>"
    if isinstance(block, markup.Callout):
        return "<blockquote>\n" + "\n".join(filter(None, (_block(b) for b in block.blocks))) + "\n</blockquote>"
    if isinstance(block, markup.Table):
        head = ""
        if block.header is not None:
            head = "<thead><tr>" + "".join(f"<th>{_runs(c)}</th>" for c in block.header) + "</tr></thead>"
        rows = "".join("<tr>" + "".join(f"<td>{_runs(c)}</td>" for c in row) + "</tr>" for row in block.rows)
        return f'<div class="table-scroll"><table>{head}<tbody>{rows}</tbody></table></div>'
    return ""  # a divider: headings divide a post


def render_post(post: Post, day: str, owner: Owner) -> bytes:
    """A post's page, dated ``day`` (YYYY-MM-DD)."""
    document = markup.parse(post.body)
    body = "\n".join(filter(None, (_block(b) for b in document.main)))
    product = ""
    if post.product_url:
        product = (
            '<aside class="post-product" aria-label="Passende Vorlage">\n'
            '<p class="eyebrow">Aus dem Shop</p>\n'
            f"<h2>{_e(post.product_name)}</h2>\n"
            f"<p>{_runs(markup.inline(post.product_text))}</p>\n"
            f'<a class="btn btn-primary" href="{_e(post.product_url)}">Auf Etsy ansehen</a>\n'
            "</aside>\n"
        )
    page = (
        _head(owner, post.title, post.description, f"/{post.path}", "article")
        + _header(owner)
        + '<main id="main" class="post">\n<article class="wrap post-wrap">\n<header class="post-head">\n'
        '<p class="eyebrow"><a href="/blog/">Blog</a></p>\n'
        f"<h1>{_e(post.title)}</h1>\n"
        f'<p class="post-meta"><time datetime="{day}">{date_de(day)}</time></p>\n'
        f'<p class="post-lead">{_runs(markup.inline(post.lead))}</p>\n'
        "</header>\n"
        f'<div class="prose">\n{body}\n</div>\n'
        + product
        + f'<p class="post-ai">Transparenz: {_e(owner.agent)}, ein KI-Agent, hat diesen Beitrag mit Claude von '
        "Anthropic recherchiert und geschrieben. Ein Mensch hat ihn vor der Veröffentlichung geprüft.</p>\n"
        '<p class="back-links"><a class="back" href="/blog/">← Alle Beiträge</a></p>\n'
        "</article>\n</main>\n" + _footer(owner)
    )
    return page.encode()


def merge(index: Index, entry: Entry) -> Index:
    """The list with a post added (first among its day's) or, for one already in it, changed."""
    others = [e for e in index.entries if e.slug != entry.slug]
    ordered = sorted([entry, *others], key=lambda e: e.date, reverse=True)  # stable: the new one first on its day
    return Index(tuple(ordered), index.heading, index.lead, index.description)


def without(index: Index, slug: str, previous: Entry | None = None) -> Index:
    """The list without a post, or with its previous entry back."""
    others = tuple(e for e in index.entries if e.slug != slug)
    shown = Index(others, index.heading, index.lead, index.description)
    return merge(shown, previous) if previous is not None else shown


def render_index(index: Index, owner: Owner) -> bytes:
    """The blog's list of posts, the newest first, under the heading and introduction it has on the server."""
    heading = index.heading or INDEX_HEADING
    lead = index.lead or (
        f"Beiträge von {owner.agent}, einem KI-Agenten. Ein Mensch prüft jeden Beitrag vor der Veröffentlichung."
    )
    description = index.description or lead
    entries = "\n".join(
        f'<li><p class="post-meta"><time datetime="{e.date}">{date_de(e.date)}</time></p>'
        f'<h2><a href="/{post_path(e.slug)}">{_e(e.title)}</a></h2><p>{_e(e.description)}</p></li>'
        for e in index.entries
    )
    page = (
        _head(owner, INDEX_TITLE, description[:DESCRIPTION_MAX], "/blog/", "website")
        + _header(owner)
        + '<main id="main">\n<section class="section" aria-labelledby="blog-title">\n<div class="wrap">\n'
        '<div class="section-head">\n<p class="eyebrow">Blog</p>\n'
        f'<h1 class="page-title" id="blog-title">{_e(heading)}</h1>\n'
        f'<p class="lead">{_e(lead)}</p>\n</div>\n'
        f'<ul class="post-list">\n{entries}\n</ul>\n'
        "</div>\n</section>\n</main>\n" + _footer(owner)
    )
    return page.encode()


def render_links(bio: str, links: list[Link], owner: Owner) -> bytes:
    """The link page (for profiles: Pinterest, Instagram): the site's head without its header and footer."""
    items = "\n".join(
        f'<li><a class="link-btn{" link-main" if i == 0 else ""}" href="{_e(link.url)}">{_e(link.label)}'
        + (f"<small>{_e(link.note)}</small>" if link.note else "")
        + "</a></li>"
        for i, link in enumerate(links)
    )
    page = (
        _head(owner, "Links", bio[:DESCRIPTION_MAX], f"/{LINKS}", "website")
        + '<body class="links-page">\n<main id="main" class="links">\n'
        '<img class="links-logo" src="/favicon.svg" alt="">\n'
        f"<h1>{_e(owner.name)}</h1>\n"
        f'<p class="links-bio">{_e(bio)}</p>\n'
        f'<ul class="link-list">\n{items}\n</ul>\n'
        '<p class="links-legal"><a href="/">Website</a> · <a href="/impressum.html">Impressum</a> · '
        '<a href="/datenschutz.html">Datenschutz</a> · <a href="/en/" hreflang="en" lang="en">English</a></p>\n'
        "</main>\n</body>\n</html>\n"
    )
    return page.encode()


def preview(data: bytes, origin: str) -> bytes:
    """A page as the owner previews it in the dashboard: the site's stylesheet, font and pictures from the site itself
    (``origin``, like https://example.org), its links to it. Never uploaded: the approved page is the one in the
    database."""
    text = data.decode("utf-8", "replace")
    text = text.replace(f'content="{CSP}"', f'content="{_preview_policy(origin)}"', 1)
    text = re.sub(r'(\s(?:href|src))="/(?!/)', lambda m: f'{m[1]}="{origin}/', text)
    return text.encode()


def _preview_policy(origin: str) -> str:
    allowed = f"style-src {origin}; font-src {origin}; img-src {origin}"
    return f"default-src 'none'; {allowed}; base-uri 'none'; form-action 'none'"


def preview_policy(origin: str) -> str:
    """The preview's own Content-Security-Policy header: a sandbox that runs nothing and loads only from the site."""
    return f"sandbox; {_preview_policy(origin)}; frame-ancestors 'none'"


# --- reading the server's pages, and checking a page before it is uploaded -----------------------------------------


def _squash(text: str) -> str:
    """Text read from a page on one line (its no-break spaces kept)."""
    return re.sub(r"[ \t\r\n]+", " ", text).strip()


class _IndexParser(HTMLParser):
    """The posts of a blog list in the template's form, with its heading, introduction and description."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[Entry] = []
        self.heading = ""
        self.lead = ""
        self.description = ""
        self.found_list = False
        self._in_list = 0
        self._item: dict[str, Any] | None = None
        self._capture: str | None = None  # what the text goes to
        self._text: list[str] = []
        self._p_meta = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        classes = a.get("class", "").split()
        if tag == "meta" and a.get("name") == "description":
            self.description = _squash(a.get("content", ""))
        if tag == "ul" and "post-list" in classes:
            self.found_list = True
            self._in_list += 1
            return
        if not self._in_list:
            if tag == "h1" and not self.heading:
                self._start("heading")
            elif tag == "p" and "lead" in classes and not self.lead:
                self._start("lead")
            return
        if tag == "li":
            self._item = {"date": "", "slug": "", "title": "", "description": ""}
        elif self._item is not None:
            if tag == "time":
                self._item["date"] = a.get("datetime", "")
            elif tag == "a" and self._capture is None:
                match = _POST_HREF.match(a.get("href", ""))
                if match:
                    self._item["slug"] = match[1]
                    self._start("title")
            elif tag == "p":
                self._p_meta = "post-meta" in classes
                if not self._p_meta:
                    self._start("description")

    def _start(self, what: str) -> None:
        self._capture, self._text = what, []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None and tag in ("h1", "p", "a"):
            text = _squash("".join(self._text))
            if self._capture == "heading" and tag == "h1":
                self.heading = text
            elif self._capture == "lead" and tag == "p":
                self.lead = text
            elif self._item is not None and self._capture == "title" and tag == "a":
                self._item["title"] = text
            elif self._item is not None and self._capture == "description" and tag == "p":
                self._item["description"] = text
            else:
                return
            self._capture = None
        if tag == "li" and self._item is not None:
            item, self._item = self._item, None
            if item["slug"] and item["title"] and _DATE.match(item["date"]):
                self.entries.append(Entry(item["slug"], item["title"], item["description"], item["date"]))
        if tag == "ul" and self._in_list:
            self._in_list -= 1


def read_index(data: bytes | None) -> Index:
    """The blog's list as it is on the server (None: there is none yet). Raises BlogError for a page that isn't a
    list in the template's form: Ember's code never replaces what it can't read."""
    if data is None:
        return Index()
    if len(data) > PAGE_MAX:
        raise BlogError(f"{INDEX} on the server is larger than {PAGE_MAX // 1000} kB")
    parser = _IndexParser()
    try:
        parser.feed(data.decode("utf-8"))
        parser.close()
    except (UnicodeDecodeError, AssertionError) as exc:
        raise BlogError(f"{INDEX} on the server can't be read ({type(exc).__name__})") from None
    if not parser.found_list:
        raise BlogError(
            f'{INDEX} on the server has no list of posts in the blog template\'s form (<ul class="post-list">); '
            "Ember's code doesn't replace a page it can't read"
        )
    seen: set[str] = set()
    entries = []
    for entry in parser.entries:
        if entry.slug not in seen and SLUG.match(entry.slug):
            seen.add(entry.slug)
            entries.append(
                Entry(entry.slug, entry.title[: TITLE_MAX * 2], entry.description[: DESCRIPTION_MAX * 2], entry.date)
            )
    return Index(tuple(entries), parser.heading[:200], parser.lead[:600], parser.description[:300])


class _Auditor(HTMLParser):
    def __init__(self, owner: Owner) -> None:
        super().__init__(convert_charrefs=True)
        self.owner = owner
        self.problems: list[str] = []
        self.policies: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            self.problems.append(f"<{tag}> isn't allowed")
        for name, value in attrs:
            value = value or ""
            if name.startswith("on") or name in ("style", "srcdoc", "formaction"):
                self.problems.append(f"the attribute {name} isn't allowed")
            if name in _URL_ATTRS and not _safe(value, self.owner, name):
                self.problems.append(f"{name}={value[:80]!r} isn't an allowed address")
        if tag == "meta" and dict(attrs).get("http-equiv", "").lower() == "content-security-policy":
            self.policies.append(str(dict(attrs).get("content")))

    handle_startendtag = handle_starttag


def _safe(url: str, owner: Owner, attr: str) -> bool:
    if url.startswith("/") and not url.startswith("//"):
        return True
    if attr == "src":
        return False
    if url.startswith("#"):
        return True
    if url.startswith("mailto:"):
        return url[7:].split("?", 1)[0].lower() == owner.email.lower()
    return url.startswith("https://") and "@" not in url[8:].split("/", 1)[0]


def audit(data: bytes, owner: Owner) -> list[str]:
    """What is wrong with a page Ember's code is about to upload (none: it may go up). Every page is rendered from
    escaped text, so this is the second check: no script, no handler, no style attribute, no address but the site's
    own, https and the owner's mailto, and exactly the site's Content-Security-Policy."""
    if len(data) > PAGE_MAX:
        return [f"the page is larger than {PAGE_MAX // 1000} kB"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["the page isn't UTF-8 text"]
    if not text.startswith("<!doctype html>\n"):
        return ["the page doesn't start with <!doctype html>"]
    auditor = _Auditor(owner)
    auditor.feed(text)
    auditor.close()
    if auditor.policies != [CSP]:
        auditor.problems.append("the page must carry exactly the site's Content-Security-Policy")
    return auditor.problems
