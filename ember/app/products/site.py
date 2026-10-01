"""The owner's website (0.13.0, Phase E3): a static landing site from one audited template, without trackers.

The agent writes the pages in the products' markdown (the site_page tool); Ember's code builds the site from them.
Every text is escaped into one fixed template: a header with the site's name and its pages, a footer with the
Impressum and the privacy page. The stylesheet is inline and named by its hash in a Content-Security-Policy that
allows nothing else: no script, no picture, no font, nothing loaded from elsewhere. There is a robots.txt and, with the
site's address, a sitemap. The Impressum (section 5 DDG) and the privacy page are made from the owner's options, never
from the agent's words. Nothing here reads a file or the network: the owner previews the site in the dashboard,
downloads it and publishes it at their host. Ember never publishes it.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from . import markup

HOME = "index"
MAX_PAGES = 8
SLUG_MAX = 40
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED = frozenset({"impressum", "datenschutz", "style", "robots", "sitemap"})  # Ember's code's own files
TITLE_MAX = 80
DESCRIPTION_MAX = 160
MENU_MAX = 24
SOURCE_MAX = 20_000
LANGUAGES = ("de", "en")
ZIP_TIME = (2026, 1, 1, 0, 0, 0)  # every file's time in the download, so the same site is the same file
_EMAIL = re.compile(r"^[^@\s<>\"']{1,64}@[^@\s<>\"']{1,190}\.[A-Za-z]{2,63}$")
# 0.14.0: the Impressum's address is where the owner can be found (a street with its number, then the postcode with
# the town), never a PO box; a phone number, if given, is one; a business ID in the VAT ID's option is a
# Wirtschafts-Identifikationsnummer (§ 139c AO: DE, 9 digits, a dash and 5 digits).
_POSTCODE = re.compile(r"^(?:[A-Z]{1,2}-)?\d{4,5} +\S")
_STREET = re.compile(r"(?=.*[^\W\d_]).*\d")
_PO_BOX = re.compile(r"\b(?:postfach|postbox|p\.? ?o\.? ?box)\b", re.IGNORECASE)
_PHONE = re.compile(r"^\+?[0-9 ()/.-]{6,40}$")
_BUSINESS_ID = re.compile(r"^DE\d{9}-\d{5}$")
_PRINT_ONLY = (markup.Space, markup.Photo, markup.Lines, markup.PageBreak)


class SiteError(ValueError):
    """A page or a build the site can't use, in words for the agent and the owner."""


@dataclass(frozen=True)
class Page:
    slug: str  # the file's name: "index" is the home page
    title: str
    description: str
    source: str  # the products' markdown
    menu: str = ""  # its name in the header ("": the title)
    notes: tuple[str, ...] = field(default=(), compare=False)  # for the agent: what the site leaves out


@dataclass(frozen=True)
class Owner:
    """The owner's data for the site, from their options: the Impressum's and the privacy page's, the site's name
    and language, and its address."""

    legal_name: str
    address: tuple[str, ...]
    email: str
    phone: str = ""
    vat_id: str = ""
    host: str = ""  # their web host, for the privacy page ("": named in general words)
    name: str = ""  # the site's name in its header ("": the legal name)
    language: str = "de"
    url: str = ""  # where the owner publishes it (https), for the sitemap and the canonical links

    def problems(self) -> list[str]:
        found = []
        if not self.legal_name:
            found.append("site_owner_name is missing")
        town = next((i for i, line in enumerate(self.address) if i and _POSTCODE.match(line)), 0)
        if not any(_STREET.match(line) for line in self.address[:town]):  # 0.14.0: the street and its number first
            found.append("site_address needs the street and the postcode with the town")
        elif any(_PO_BOX.search(line) for line in self.address):
            found.append("site_address must be where you can be found (street, postcode and town), not a PO box")
        if not _EMAIL.match(self.email):
            found.append("site_email is missing")
        if self.phone and (not _PHONE.match(self.phone) or sum(c.isdigit() for c in self.phone) < 6):
            found.append("site_phone must be a phone number, like +49 30 1234567")
        return found


def check(slug: str, title: str, description: str, source: str, menu: str = "") -> Page:
    """A page the site can build: the agent's words checked, its markdown parsed. Raises SiteError."""
    slug = slug.strip().lower()
    if not SLUG.match(slug) or slug in RESERVED:
        raise SiteError(
            f"a page's name is 1 to {SLUG_MAX} lower-case letters, digits and dashes ('{HOME}' for the home page), "
            f"and not {', '.join(sorted(RESERVED))}"
        )
    title = " ".join(title.split())
    if not 1 <= len(title) <= TITLE_MAX:
        raise SiteError(f"a page's title has 1 to {TITLE_MAX} characters")
    description = " ".join(description.split())
    if not 1 <= len(description) <= DESCRIPTION_MAX:
        raise SiteError(f"a page's description has 1 to {DESCRIPTION_MAX} characters")
    menu = " ".join(menu.split())
    if len(menu) > MENU_MAX:
        raise SiteError(f"a page's name in the menu has at most {MENU_MAX} characters")
    if len(source) > SOURCE_MAX:
        raise SiteError(f"a page's text has at most {SOURCE_MAX:,} characters")
    try:
        document = markup.parse(source)
    except markup.DocumentError as exc:
        raise SiteError(f"the page's text: {exc}") from None
    notes = list(document.warnings)
    left_out = _print_only(document.main) + _print_only(document.sidebar)
    if left_out:
        one = left_out == 1
        notes.append(
            f"{left_out} print-only block{'' if one else 's'} (space, photo frames, writing lines, page breaks) "
            f"show{'s' if one else ''} nothing on a web page"
        )
    return Page(slug, title, description, source, menu, tuple(notes))


def _print_only(blocks: list[Any]) -> int:
    found = 0
    for block in blocks:
        if isinstance(block, _PRINT_ONLY):
            found += 1
        elif isinstance(block, (markup.Callout, markup.Box, markup.Center)):
            found += _print_only(block.blocks)
        elif isinstance(block, markup.Columns):
            found += sum(_print_only(column) for column in block.columns)
    return found


# --- rendering: every text escaped, the agent's words are never markup ---------------------------------------------


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _runs(runs: list[markup.Run]) -> str:
    out = []
    for run in runs:
        text = "<br>".join(_e(part) for part in run.text.split("\n"))
        if run.bold:
            text = f"<strong>{text}</strong>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.url:  # the parser allows https and mailto only
            text = f'<a href="{_e(run.url)}" rel="nofollow noopener">{text}</a>'
        out.append(text)
    return "".join(out)


def _blocks(blocks: list[Any]) -> str:
    return "\n".join(filter(None, (_block(b) for b in blocks)))


def _cell(tag: str, runs: list[markup.Run], align: str) -> str:
    kind = {"center": ' class="c"', "right": ' class="r"'}.get(align, "")
    return f"<{tag}{kind}>{_runs(runs)}</{tag}>"


def _block(block: Any) -> str:
    if isinstance(block, markup.Heading):
        level = min(4, block.level + 1)  # the page's title is its h1
        return f"<h{level}>{_runs(block.runs)}</h{level}>"
    if isinstance(block, markup.Paragraph):
        return f"<p>{_runs(block.runs)}</p>"
    if isinstance(block, markup.ListBlock):
        tag = "ol" if block.ordered else "ul"
        return f"<{tag}>" + "".join(f"<li>{_runs(item)}</li>" for item in block.items) + f"</{tag}>"
    if isinstance(block, markup.Checklist):
        items = "".join(f"<li>{'☑' if done else '☐'} {_runs(item)}</li>" for done, item in block.items)
        return f'<ul class="checks">{items}</ul>'
    if isinstance(block, markup.Table):
        align = list(block.align)
        head = ""
        if block.header is not None:
            cells = "".join(_cell("th", c, a) for c, a in zip(block.header, align, strict=False))
            head = f"<thead><tr>{cells}</tr></thead>"
        rows = "".join(
            "<tr>" + "".join(_cell("td", c, a) for c, a in zip(row, align, strict=False)) + "</tr>"
            for row in block.rows
        )
        return f'<div class="table"><table>{head}<tbody>{rows}</tbody></table></div>'
    if isinstance(block, markup.Divider):
        return "<hr>"
    if isinstance(block, markup.Callout):
        return f'<aside class="callout">{_blocks(block.blocks)}</aside>'
    if isinstance(block, markup.Box):
        return f'<div class="box">{_blocks(block.blocks)}</div>'
    if isinstance(block, markup.Center):
        return f'<div class="center">{_blocks(block.blocks)}</div>'
    if isinstance(block, markup.Columns):
        return (
            '<div class="columns">'
            + "".join(f'<div class="column">{_blocks(c)}</div>' for c in block.columns)
            + "</div>"
        )
    return ""  # print layout (space, photo frames, writing lines, page breaks): nothing on a web page


# --- the template -------------------------------------------------------------------------------------------------

STYLE = """
:root { --text: #1f2933; --muted: #52606d; --accent: #2c3e50; --line: #e4e7eb; --paper: #ffffff; --soft: #f5f7fa; }
* { box-sizing: border-box; }
body { margin: 0; font: 17px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--text);
  background: var(--paper); overflow-wrap: break-word; }
header, main, footer { max-width: 60rem; margin: 0 auto; padding: 1rem 1.25rem; }
header { display: flex; flex-wrap: wrap; gap: .5rem 1.25rem; align-items: baseline;
  border-bottom: 1px solid var(--line); }
header .name { font-weight: 700; color: var(--accent); text-decoration: none; margin-right: auto; }
nav a { color: var(--muted); text-decoration: none; margin-right: 1rem; }
nav a[aria-current="page"] { color: var(--accent); font-weight: 600; }
h1, h2, h3, h4 { line-height: 1.25; color: var(--accent); }
a { color: var(--accent); }
.table { overflow-x: auto; }
table { border-collapse: collapse; }
th, td { border-bottom: 1px solid var(--line); padding: .4rem .75rem; text-align: left; vertical-align: top; }
th.c, td.c { text-align: center; }
th.r, td.r { text-align: right; }
.callout, .box { border-left: 4px solid var(--accent); background: var(--soft); padding: .5rem 1rem; margin: 1rem 0; }
.center { text-align: center; }
.columns { display: flex; flex-wrap: wrap; gap: 1.5rem; }
.column { flex: 1 1 16rem; }
.checks { list-style: none; padding-left: 0; }
.side { border-top: 1px solid var(--line); margin-top: 2rem; }
footer { border-top: 1px solid var(--line); color: var(--muted); font-size: .9rem; }
footer a { color: var(--muted); margin-right: 1rem; }
@media (prefers-color-scheme: dark) {
  :root { --text: #e4e7eb; --muted: #9aa5b1; --accent: #9fc1e0; --line: #323f4b; --paper: #111820; --soft: #1b2631; }
}
"""
STYLE_HASH = "sha256-" + base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
# Nothing but the stylesheet above: no script, no picture, no font, no frame, no form, nothing from elsewhere.
CSP = f"default-src 'none'; style-src '{STYLE_HASH}'; base-uri 'none'; form-action 'none'"
WORDS = {
    "de": {"home": "Start", "imprint": "Impressum", "privacy": "Datenschutz"},
    "en": {"home": "Home", "imprint": "Imprint", "privacy": "Privacy"},
}
LEGAL = (("impressum", "Impressum"), ("datenschutz", "Datenschutzerklärung"))  # always German: German law's pages


def _address(owner: Owner, url_path: str) -> str:
    """A page's address on the owner's host ("": no site address)."""
    if not owner.url:
        return ""
    base = owner.url.rstrip("/")
    return f"{base}/" if url_path == HOME else f"{base}/{url_path}.html"


def _document(
    owner: Owner, slug: str, title: str, description: str, body: str, nav: list[tuple[str, str]], legal: bool = False
) -> str:
    words = WORDS[owner.language]
    name = _e(owner.name or owner.legal_name)
    here = ' aria-current="page"'
    links = "".join(f'<a href="{_e(s)}.html"{here if s == slug else ""}>{_e(name)}</a>' for s, name in nav)
    address = "" if legal else _address(owner, slug)
    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>{_e(title)}</title>",
        f'<meta name="description" content="{_e(description)}">',
        '<meta name="robots" content="noindex">' if legal else "",
        f'<meta property="og:title" content="{_e(title)}">' if not legal else "",
        f'<meta property="og:description" content="{_e(description)}">' if not legal else "",
        f'<link rel="canonical" href="{_e(address)}">' if address else "",
        f"<style>{STYLE}</style>",
    ]
    return (
        f'<!doctype html>\n<html lang="{"de" if legal else owner.language}">\n<head>\n'
        + "\n".join(line for line in head if line)
        + "\n</head>\n<body>\n"
        + f'<header><a class="name" href="index.html">{name}</a><nav>{links}</nav></header>\n'
        + f"<main>\n<h1>{_e(title)}</h1>\n{body}\n</main>\n"
        + f'<footer><a href="impressum.html">{words["imprint"]}</a><a href="datenschutz.html">{words["privacy"]}</a>'
        + "</footer>\n</body>\n</html>\n"
    )


def impressum(owner: Owner) -> str:
    """Angaben gemäß § 5 DDG, from the owner's options only."""
    address = "<br>".join(_e(line) for line in owner.address)
    contact = [f'E-Mail: <a href="mailto:{_e(owner.email)}">{_e(owner.email)}</a>']
    if owner.phone:
        contact.append(f"Telefon: {_e(owner.phone)}")
    parts = [
        "<h2>Angaben gemäß § 5 DDG</h2>",
        f"<p>{_e(owner.legal_name)}<br>{address}</p>",
        "<h2>Kontakt</h2>",
        f"<p>{'<br>'.join(contact)}</p>",
    ]
    if _BUSINESS_ID.match(owner.vat_id.replace(" ", "")):
        parts += [
            "<h2>Wirtschafts-Identifikationsnummer</h2>",
            f"<p>Wirtschafts-Identifikationsnummer gemäß § 139c Abgabenordnung: {_e(owner.vat_id)}</p>",
        ]
    elif owner.vat_id:
        parts += [
            "<h2>Umsatzsteuer-ID</h2>",
            f"<p>Umsatzsteuer-Identifikationsnummer gemäß § 27a Umsatzsteuergesetz: {_e(owner.vat_id)}</p>",
        ]
    parts += [
        "<h2>Verantwortlich für den Inhalt nach § 18 Abs. 2 MStV</h2>",
        f"<p>{_e(owner.legal_name)}<br>{address}</p>",
        "<h2>Hinweis</h2>",
        "<p>Die Texte dieser Website wurden mit Hilfe einer KI verfasst und vor der Veröffentlichung geprüft.</p>",
    ]
    return "\n".join(parts)


def datenschutz(owner: Owner) -> str:
    """The privacy page, from the owner's options only: a site without cookies, trackers or anything from elsewhere."""
    address = "<br>".join(_e(line) for line in owner.address)
    email = f'<a href="mailto:{_e(owner.email)}">{_e(owner.email)}</a>'
    host = (
        f"Die Website wird bei {_e(owner.host)} gespeichert. Beim Aufruf verarbeitet dieser Anbieter"
        if owner.host
        else ("Beim Aufruf verarbeitet der Anbieter, bei dem die Website gespeichert ist,")
    )
    return "\n".join(
        [
            "<h2>Verantwortlicher</h2>",
            f"<p>{_e(owner.legal_name)}<br>{address}<br>E-Mail: {email}</p>",
            "<h2>Was diese Website verarbeitet</h2>",
            "<p>Diese Website setzt keine Cookies, verwendet keine Analyse- oder Tracking-Werkzeuge und lädt keine "
            "Inhalte von anderen Anbietern (keine Schriften, Skripte, Karten oder Videos).</p>",
            f"<p>{host} technisch notwendige Daten (IP-Adresse, Zeitpunkt, aufgerufene Seite, Browser) in seinen "
            "Server-Protokollen, um die Seite auszuliefern und ihren Betrieb zu sichern (Art. 6 Abs. 1 lit. f "
            "DSGVO). Die Protokolle werden gelöscht, sobald sie dafür nicht mehr nötig sind; die genaue Frist richtet "
            "sich nach den Vorgaben des Anbieters.</p>",
            "<h2>Wenn Sie uns schreiben</h2>",
            "<p>Schreiben Sie uns eine E-Mail, verarbeiten wir Ihre Angaben, um Ihre Anfrage zu beantworten (Art. 6 "
            "Abs. 1 lit. b und f DSGVO), und löschen sie, wenn sie dafür nicht mehr nötig sind und keine "
            "Aufbewahrungspflicht besteht.</p>",
            "<h2>Links zu anderen Seiten</h2>",
            "<p>Links führen zu anderen Anbietern, etwa zu Etsy. Wenn Sie einem Link folgen, gelten dort deren "
            "Datenschutzbestimmungen.</p>",
            "<h2>Ihre Rechte</h2>",
            "<p>Sie haben das Recht auf Auskunft, Berichtigung, Löschung und Einschränkung der Verarbeitung Ihrer "
            "Daten, auf Datenübertragbarkeit und auf Widerspruch (Art. 15 bis 21 DSGVO) sowie das Recht, sich bei "
            f"einer Datenschutz-Aufsichtsbehörde zu beschweren. Schreiben Sie dafür an {email}.</p>",
        ]
    )


def build(pages: list[Page], owner: Owner) -> dict[str, bytes]:
    """Every file of the site, by name. Raises SiteError without the owner's data or a home page."""
    problems = owner.problems()
    if problems:
        raise SiteError("; ".join(problems))
    if owner.language not in LANGUAGES:
        raise SiteError(f"site_language must be one of: {', '.join(LANGUAGES)}")
    if not any(p.slug == HOME for p in pages):
        raise SiteError(f"the site has no home page yet (the page '{HOME}')")
    ordered = sorted(pages, key=lambda p: (p.slug != HOME, p.slug))[:MAX_PAGES]
    home = WORDS[owner.language]["home"]
    nav = [(p.slug, home if p.slug == HOME else (p.menu or _short(p.title))) for p in ordered]
    files: dict[str, bytes] = {}
    for page in ordered:
        document = markup.parse(page.source)
        body = _blocks(document.main)
        if document.sidebar:
            body += f'\n<aside class="side">{_blocks(document.sidebar)}</aside>'
        files[f"{page.slug}.html"] = _document(owner, page.slug, page.title, page.description, body, nav).encode()
    for (slug, title), body in zip(LEGAL, (impressum(owner), datenschutz(owner)), strict=True):
        files[f"{slug}.html"] = _document(owner, slug, title, title, body, nav, legal=True).encode()
    robots = ["User-agent: *", "Allow: /"]
    if owner.url:
        robots.append(f"Sitemap: {owner.url.rstrip('/')}/sitemap.xml")
        urls = "".join(f"<url><loc>{_e(_address(owner, p.slug))}</loc></url>" for p in ordered)
        files["sitemap.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n'
        ).encode()
    files["robots.txt"] = ("\n".join(robots) + "\n").encode()
    return files


def _short(title: str) -> str:
    """A title for the menu: cut at a word, at most MENU_MAX characters."""
    if len(title) <= MENU_MAX:
        return title
    cut = title[: MENU_MAX - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut or title[: MENU_MAX - 1]}…"


def fingerprints(files: dict[str, bytes]) -> dict[str, str]:
    """Each file's SHA-256: what the owner downloaded, to tell what changed since."""
    return {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}


def changes(files: dict[str, bytes], before: dict[str, str]) -> list[str]:
    """The files that are new, changed or gone since ``before`` (fingerprints), by name."""
    now = fingerprints(files)
    changed = [
        f"{name} (new)" if name not in before else name for name, digest in now.items() if before.get(name) != digest
    ]
    return changed + [f"{name} (gone)" for name in sorted(before) if name not in now]


def archive(files: dict[str, bytes]) -> bytes:
    """The site as one zip for the owner to upload: the same site is the same file."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            out.writestr(info, files[name])
    return buffer.getvalue()
