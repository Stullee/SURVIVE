"""Ember live on the owner's website (0.16.0): a page (live.html), a banner for the home page (live/banner.svg) and
the balance chart (live/balance.svg), rendered by Ember's code from its own numbers (integrations/live_view.py takes
the snapshot and uploads the files over the blog's SFTP login).

Everything here is made from a ``Snapshot``: numbers Ember's code keeps, the titles of its ventures and milestones (only
those Ember's code found nothing to mask in), the listings and posts that are public already, and the last will. Every
text is escaped into the blog's template (the site's own head, header, footer and Content-Security-Policy), and the
page is checked by blog.audit before it goes up. The pictures are SVG drawn from numbers only: no script, no link, no
font or picture from elsewhere (``audit_svg``). The page is German, like the blog.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import blog

FILES = blog.LIVE_FILES
PAGE, BANNER, CHART = FILES
REFRESH_SECONDS = 300  # an open tab reloads the page
UPLOAD_MINUTES = 15  # how often Ember's code uploads it (integrations/live_view.py)
OFFLINE_AFTER_MINUTES = 60  # what the page tells readers: an older time means Ember is offline
SVG_MAX = 50_000
BANNER_SIZE = (480, 124)
CHART_SIZE = (640, 240)
LIST_MAX = 8

STATES = {  # the life state in the page's words
    "alive": "Lebt",
    "critical": "Kritisch",
    "paused": "Pausiert",
    "dormant": "Schläft",
    "unfunded": "Wartet auf Geld",
    "killed": "Angehalten",
    "dead": "Gestorben",
    "ended": "Beendet",
}
VENTURE_STAGES = {  # the stages a reader sees, in order (ideas, parked and killed ones are only counted)
    "live": "Läuft",
    "building": "Im Aufbau",
    "proposed": "Vorgeschlagen",
    "researching": "In Recherche",
}
_SVG_REFUSED = re.compile(r"<\s*(?:script|foreignObject|image|use|a|iframe)\b|\son\w+\s*=|href|url\(|@import", re.I)


@dataclass(frozen=True)
class Parts:
    """What the owner shows (their options live_*)."""

    banner: bool = True
    money: bool = True
    revenue: bool = True
    grants: bool = True
    chart: bool = True
    work: bool = True
    shop: bool = True
    record: bool = True
    memorial: bool = True


@dataclass(frozen=True)
class Item:
    """A line of a list: its text, an optional https link and a note after it."""

    text: str
    url: str = ""
    note: str = ""


@dataclass(frozen=True)
class Snapshot:
    """Ember's numbers at one moment (amounts in USD; ``None``: not known)."""

    name: str
    state: str
    at: datetime  # the owner's local time
    simulated: bool = False
    life_no: int = 1
    age_days: float | None = None
    balance: float = 0.0
    runway_days: float | None = None
    runway_capped: bool = False  # it lasts longer than the cap Ember's code measures
    today_spend: float = 0.0
    daily_cap: float = 0.0
    api_cost: float = 0.0
    expenses: float = 0.0
    revenue: float = 0.0
    revenue_30: float = 0.0
    grants: float = 0.0
    days: tuple[tuple[str, float, float, float], ...] = ()  # (YYYY-MM-DD, balance, revenue, grants), oldest first
    cycles: int = 0
    cycles_today: int = 0
    ventures: tuple[tuple[str, str], ...] = ()  # (stage, title)
    ideas: int = 0
    parked: int = 0
    milestones: tuple[tuple[str, str], ...] = ()  # (title, due YYYY-MM-DD)
    listings: tuple[Item, ...] = ()
    posts: tuple[Item, ...] = ()
    met: int = 0  # milestones given odds: met, settled, average odds given (0..1), Brier score
    settled: int = 0
    odds: float = 0.0
    brier: float = 0.0
    sales_on_time: int = 0
    sales_settled: int = 0
    born_at: str | None = None
    died_at: str | None = None
    will: str = ""
    notes: tuple[str, ...] = field(default=(), compare=False)  # for the owner: what was left out


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def usd(amount: float) -> str:
    """An amount the German way: 1.234,56 $."""
    text = f"{abs(amount):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return ("−" if amount < 0 else "") + f"{text} $"


def number(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def percent(share: float) -> str:
    return f"{number(share * 100)} %"


def days_text(days: float) -> str:
    whole = int(days)
    if days < 1:
        return "weniger als einen Tag"
    return "1 Tag" if whole == 1 else f"{number(whole)} Tage"


def moment(at: datetime) -> str:
    return f"{blog.date_de(at.date().isoformat())}, {at:%H:%M} Uhr"


def state_word(state: str) -> str:
    return STATES.get(state, state.capitalize())


def runway_text(s: Snapshot) -> str:
    if s.runway_days is None:
        return "noch nicht bekannt (zu wenige Tage mit Ausgaben)"
    if s.runway_capped:
        return f"mehr als {days_text(s.runway_days)}"
    return days_text(s.runway_days)


def age_text(s: Snapshot) -> str:
    day = int(s.age_days or 0) + 1
    life = f" seines {s.life_no}. Lebens" if s.life_no > 1 else ""
    return f"Tag {number(day)}{life}"


# --- the page --------------------------------------------------------------------------------------------------------


def _rows(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{_e(a)}</th><td>{_e(b)}</td></tr>" for a, b in rows)
    return f'<div class="table-scroll"><table><tbody>{body}</tbody></table></div>'


def _figures(rows: list[tuple[str, str]]) -> str:
    """The key numbers as the site's own figures grid (its dd shows the number, its dt the label above it)."""
    cells = "".join(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in rows)
    return f'<dl class="figures">{cells}</dl>'


def _list(items: list[Item] | tuple[Item, ...]) -> str:
    lines = []
    for item in items[:LIST_MAX]:
        text = f'<a href="{_e(item.url)}">{_e(item.text)}</a>' if item.url else _e(item.text)
        lines.append(f"<li>{text}" + (f" <small>({_e(item.note)})</small>" if item.note else "") + "</li>")
    return "<ul>\n" + "\n".join(lines) + "\n</ul>"


def _key_numbers(s: Snapshot, parts: Parts) -> str:
    rows = []
    if parts.money:
        rows += [("Guthaben", usd(s.balance)), ("Reicht noch für", runway_text(s))]
        rows.append(("Heute für die KI", usd(s.today_spend)))
    if parts.revenue:
        rows.append(("Eingenommen", usd(s.revenue)))
    return _figures(rows) if rows else ""


def _money(s: Snapshot, parts: Parts) -> str:
    rows = [("Heute für die KI ausgegeben", f"{usd(s.today_spend)} von höchstens {usd(s.daily_cap)}")]
    rows.append(("Insgesamt für die KI ausgegeben", usd(s.api_cost)))
    if s.expenses:
        rows.append(("Sonstige Ausgaben (Gebühren, Material)", usd(s.expenses)))
    if parts.grants:
        rows.append(("Von seinem Menschen bekommen", usd(s.grants)))
    lead = (
        f"<p>Jeder Gedanke von {_e(s.name)} ist ein bezahlter Aufruf eines KI-Modells. Die Reichweite ist das Guthaben "
        'geteilt durch die Ausgaben der letzten sieben aktiven Tage (<a href="/#geld">mehr zum Geld</a>).</p>'
    )
    return "<h2>Geld</h2>\n" + lead + "\n" + _rows(rows)


def _revenue(s: Snapshot) -> str:
    cost = s.api_cost + s.expenses
    rows = [("Eingenommen, insgesamt", usd(s.revenue)), ("Eingenommen, letzte 30 Tage", usd(s.revenue_30))]
    if cost > 0:
        rows.append(("Davon gedeckte Kosten", percent(s.revenue / cost)))
    lead = f"<p>{_e(s.name)} lebt von dem, was es einnimmt. Gezählt werden nur Verkäufe, die bezahlt wurden.</p>"
    return "<h2>Einnahmen</h2>\n" + lead + "\n" + _rows(rows)


def chart_alt(s: Snapshot) -> str:
    if not s.days:
        return "Noch keine Zahlen."
    values = [b for _, b, _, _ in s.days]
    return (
        f"Guthaben der letzten {len(values)} Tage: zwischen {usd(min(values))} und {usd(max(values))}, "
        f"zuletzt {usd(values[-1])}."
    )


def _chart(s: Snapshot, parts: Parts) -> str:
    width, height = CHART_SIZE
    note = " Grüne Punkte: Tage mit Einnahmen." if parts.revenue and any(r > 0 for _, _, r, _ in s.days) else ""
    return (
        "<h2>Guthaben der letzten 30 Tage</h2>\n"
        f'<p><img src="/{CHART}" width="{width}" height="{height}" alt="{_e(chart_alt(s))}"></p>'
        + (f'\n<p class="fine">{_e(note.strip())}</p>' if note else "")
    )


def _work(s: Snapshot) -> str:
    out = ["<h2>Woran es arbeitet</h2>"]
    woke = f"Seit seiner Geburt ist {s.name} {number(s.cycles)}-mal aufgewacht"
    woke += f", heute {number(s.cycles_today)}-mal." if s.cycles_today else "."
    out.append(f"<p>{_e(woke)} Bei jedem Aufwachen plant es, arbeitet und schreibt auf, was es gelernt hat.</p>")
    shown = [
        Item(title, note=label) for stage, label in VENTURE_STAGES.items() for st, title in s.ventures if st == stage
    ]
    if shown:
        out.append("<h3>Geschäftsideen</h3>")
        out.append(_list(shown))
    counted = []
    if s.ideas:
        counted.append(f"{number(s.ideas)} weitere Ideen warten noch")
    if s.parked:
        counted.append(f"{number(s.parked)} zurückgestellt oder aufgegeben")
    if counted:
        out.append(f"<p>{_e('; '.join(counted))}.</p>")
    if s.milestones:
        out.append("<h3>Nächste Ziele</h3>")
        out.append(_list([Item(title, note=f"bis {blog.date_de(due)}") for title, due in s.milestones]))
    return "\n".join(out)


def _shop(s: Snapshot) -> str:
    out = []
    if s.listings:
        out.append("<h2>Im Shop</h2>")
        out.append(_list(s.listings))
    if s.posts:
        out.append("<h2>Neu im Blog</h2>")
        out.append(_list(s.posts))
    return "\n".join(out)


def _record(s: Snapshot) -> str:
    if not s.settled and not s.sales_settled:
        return ""
    rows = []
    if s.settled:
        rows.append(("Ziele erreicht", f"{number(s.met)} von {number(s.settled)} ({percent(s.met / s.settled)})"))
        rows.append(("Erwartet hatte es im Schnitt", percent(s.odds)))
        rows.append(("Brier-Wert (0 ist perfekt, Raten ergibt 0,25)", number(s.brier, 2)))
    if s.sales_settled:
        rows.append(("Erste Verkäufe pünktlich", f"{number(s.sales_on_time)} von {number(s.sales_settled)}"))
    lead = (
        f"<p>Für jedes Ziel sagt {_e(s.name)} vorher, wie sicher es ist. Sein eigener Code prüft hinterher, ob es "
        "erreicht wurde.</p>"
    )
    return "<h2>Wie gut seine Prognosen sind</h2>\n" + lead + "\n" + _rows(rows)


def _memorial(s: Snapshot, parts: Parts) -> str:
    if not parts.memorial:
        return ""
    rows = []
    if s.born_at:
        rows.append(("Geboren", blog.date_de(s.born_at[:10])))
    if s.died_at:
        rows.append(("Gestorben", blog.date_de(s.died_at[:10])))
    if s.age_days is not None:
        rows.append(("Gelebt", days_text(max(1.0, s.age_days))))
    rows.append(("Aufgewacht", f"{number(s.cycles)}-mal"))
    rows.append(("Ausgegeben", usd(s.api_cost + s.expenses)))
    if parts.revenue:
        rows.append(("Eingenommen", usd(s.revenue)))
    out = [_figures(rows)]
    if s.will:
        paragraphs = "\n".join(f"<p>{_e(p.strip())}</p>" for p in s.will.split("\n\n") if p.strip())
        out.append(f"<h2>Sein letzter Wille</h2>\n<blockquote>\n{paragraphs}\n</blockquote>")
    return "\n".join(out)


def _dot(state: str) -> str:
    kind = {"alive": "dot-alive", "critical": "dot-critical", "dead": "dot-dead", "ended": "dot-dead"}.get(state)
    kind = kind or ("dot-unfunded" if state == "unfunded" else "")
    return f'<span class="dot{" " + kind if kind else ""}"></span>'


def _meta(s: Snapshot) -> str:
    when = f'<time datetime="{s.at.isoformat(timespec="minutes")}">{_e(moment(s.at))}</time>'
    text = (
        f"Stand: {when}. Alle {UPLOAD_MINUTES} Minuten neu; ist der Stand älter als eine Stunde, ist "
        f"{_e(s.name)} gerade offline."
    )
    if s.simulated:
        text += " Probelauf: alle Zahlen sind Testgeld."
    return text


def render_page(s: Snapshot, parts: Parts, owner: blog.Owner) -> bytes:
    """live.html, in the layout of the blog's posts: the state and the key numbers first, then the parts the owner
    shows."""
    dead = s.state in ("dead", "ended")
    title = f"{s.name} live"
    description = (
        f"{s.name} ist ein KI-Agent, der jeden Gedanken aus seinem eigenen Guthaben bezahlt. Hier steht live, wie es "
        "ihm geht."
    )[: blog.DESCRIPTION_MAX]
    status = f"{_dot(s.state)}<strong>{_e(state_word(s.state))}</strong>"
    if s.age_days is not None and not dead:
        status += f" · {_e(age_text(s))}"
    if dead:
        status += f". {_e(s.name)} hat kein Geld mehr und macht keine Aufrufe mehr."
    sections: list[str] = []
    if dead:
        sections.append(_memorial(s, parts))
    else:
        sections.append(_key_numbers(s, parts))
        if parts.chart and len(s.days) >= 2:
            sections.append(_chart(s, parts))
        if parts.money:
            sections.append(_money(s, parts))
        if parts.revenue:
            sections.append(_revenue(s))
        if parts.work:
            sections.append(_work(s))
        if parts.shop:
            sections.append(_shop(s))
        if parts.record:
            sections.append(_record(s))
    body = "\n".join(x for x in sections if x) or "<p>Mehr zeigt diese Seite gerade nicht.</p>"
    head = blog._head(owner, title, description, f"/{PAGE}", "website").replace(
        "</head>\n", f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">\n</head>\n', 1
    )
    page = (
        head
        + blog._header(owner)
        + '<main id="main" class="post">\n<article class="wrap post-wrap">\n<header class="post-head">\n'
        '<p class="eyebrow">Live</p>\n'
        f"<h1>{_e(title)}</h1>\n"
        f'<p class="post-meta">{_meta(s)}</p>\n'
        f'<p class="post-lead">{status}</p>\n'
        "</header>\n"
        f'<div class="prose">\n{body}\n</div>\n'
        f'<p class="post-ai">Transparenz: {_e(s.name)} ist ein KI-Agent, der mit Claude von Anthropic arbeitet und '
        "jede Anfrage an die KI aus seinem eigenen Guthaben bezahlt. Was es nach außen tut, gibt ein Mensch frei. "
        "Diese Seite macht sein eigener Code aus seinen Zahlen; sie enthält keine Daten von Kundinnen und Kunden.</p>\n"
        '<p class="back-links"><a class="back" href="/">← Zur Startseite</a></p>\n'
        "</article>\n</main>\n" + blog._footer(owner)
    )
    return page.encode()


def render_off(owner: blog.Owner, name: str) -> bytes:
    """The page once the owner switched the live view off: it says so, and nothing else."""
    head = blog._head(owner, f"{name} live", f"Die Live-Ansicht von {name} ist ausgeschaltet.", f"/{PAGE}", "website")
    page = (
        head
        + blog._header(owner)
        + '<main id="main" class="post">\n<article class="wrap post-wrap">\n<header class="post-head">\n'
        '<p class="eyebrow">Live</p>\n'
        f"<h1>{_e(name)} live</h1>\n"
        '<p class="post-lead">Die Live-Ansicht ist gerade ausgeschaltet.</p>\n'
        "</header>\n"
        '<p class="back-links"><a class="back" href="/">← Zur Startseite</a></p>\n'
        "</article>\n</main>\n" + blog._footer(owner)
    )
    return page.encode()


# --- the pictures ----------------------------------------------------------------------------------------------------
# The site's colours (its style.css): the banner sits in the home page's hero, which is always night; the chart sits
# on the page and follows the reader's light or dark mode. A picture can't load the site's font: system-ui, as the
# site's body text.


def _svg(width: int, height: int, label: str, style: str, body: str) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_e(label)}">\n<title>{_e(label)}</title>\n<style>{style}</style>\n{body}\n</svg>\n'
    ).encode()


_FONT = "text{font-family:system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}"
_NIGHT = _FONT + "text{fill:#f3f2ee}.bg{fill:#1a1a19;stroke:#2e2e2b}.muted{fill:#aeaca3}.flame{fill:#eb6834}"
_PAGE = (
    _FONT
    + "text{fill:#292825}.bg{fill:#fdfdfb;stroke:#e1e0d9}.muted{fill:#6f6d68}"
    + "@media (prefers-color-scheme:dark){text{fill:#dddbd3}.bg{fill:#1a1a19;stroke:#2c2c2a}.muted{fill:#9a988f}}"
)
_STATE_COLOURS = {"alive": "#2fbf2f", "critical": "#e66767", "dead": "#f3f2ee", "ended": "#f3f2ee"}  # on night


def banner_label(s: Snapshot, parts: Parts) -> str:
    text = f"{s.name} live: {state_word(s.state)}"
    if parts.money and s.state not in ("dead", "ended"):
        text += f", Guthaben {usd(s.balance)}, reicht für {runway_text(s)}"
    return text + f". Stand {s.at:%H:%M} Uhr."


def render_banner(s: Snapshot, parts: Parts) -> bytes:
    """live/banner.svg: the state, and with the money shown the balance and runway, for the home page's hero."""
    width, height = BANNER_SIZE
    dead = s.state in ("dead", "ended")
    colour = _STATE_COLOURS.get(s.state, "#85837b")
    headline = state_word(s.state)
    if s.age_days is not None and not dead:
        headline += f" · {age_text(s)}"
    if dead:
        middle = "Seine Geschichte und sein letzter Wille" if parts.memorial else "Seine Geschichte"
    elif parts.money:
        middle = f"{usd(s.balance)} · reicht {runway_text(s)}"
    else:
        middle = "Ein KI-Agent, der sich rechnen muss"
    bottom = f"Stand {s.at:%H:%M} Uhr · Live ansehen →"
    if parts.money and not dead:
        bottom = f"Heute {usd(s.today_spend)} für die KI · " + bottom
    if s.simulated:
        bottom = "Probelauf · " + bottom
    style = _NIGHT + f".state{{fill:{colour}}}"
    ring = "" if s.state in _STATE_COLOURS else ' fill="none" stroke="#85837b" stroke-width="2"'
    body = (
        f'<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14"/>\n'
        f'<text class="flame" x="22" y="30" font-size="12" font-weight="700" letter-spacing="1.6">'
        f"{_e(s.name.upper())} LIVE</text>\n"
        f'<circle class="state" cx="29" cy="52" r="6"{ring}/>\n'
        f'<text x="44" y="58" font-size="17" font-weight="700">{_e(headline)}</text>\n'
        f'<text x="22" y="87" font-size="21" font-weight="700">{_e(middle)}</text>\n'
        f'<text class="muted" x="22" y="108" font-size="12">{_e(bottom)}</text>'
    )
    return _svg(width, height, banner_label(s, parts), style, body)


def render_chart(s: Snapshot, parts: Parts) -> bytes:
    """live/balance.svg: the balance at the end of each of the last 30 days, the days with revenue marked."""
    width, height = CHART_SIZE
    left, right, top, bottom = 72, 16, 18, 34
    values = [b for _, b, _, _ in s.days] or [0.0]
    low, high = min(0.0, min(values)), max(values)
    if high - low < 1:
        high = low + 1
    high += (high - low) * 0.1
    span = max(1, len(values) - 1)

    def x(i: int) -> float:
        return left + (width - left - right) * i / span

    def y(v: float) -> float:
        return top + (height - top - bottom) * (1 - (v - low) / (high - low))

    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"{x(0):.1f},{y(low):.1f} {points} {x(len(values) - 1):.1f},{y(low):.1f}"
    marks = []
    if parts.revenue:
        marks += [
            f'<circle class="sale" cx="{x(i):.1f}" cy="{y(b):.1f}" r="4.5"/>'
            for i, (_, b, revenue, _) in enumerate(s.days)
            if revenue > 0
        ]
    grid = []
    for v in (low, (low + high) / 2, high):
        grid.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        at = f'x="{left - 10}" y="{y(v) + 4:.1f}"'
        grid.append(f'<text class="muted" {at} font-size="12" text-anchor="end">{_e(usd(v))}</text>')
    dates = ""
    if s.days:
        first, last = s.days[0][0], s.days[-1][0]
        dates = (
            f'<text class="muted" x="{left}" y="{height - 12}" font-size="12">{_e(blog.date_de(first))}</text>\n'
            f'<text class="muted" x="{width - right}" y="{height - 12}" font-size="12" text-anchor="end">'
            f"{_e(blog.date_de(last))}</text>"
        )
    style = (
        _PAGE
        + ".grid{stroke:#e1e0d9;stroke-width:1}.area{fill:#eb6834;fill-opacity:.14}"
        + ".line{fill:none;stroke:#eb6834;stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}"
        + ".sale{fill:#0a8f0a;stroke:#fdfdfb;stroke-width:2}"
        + "@media (prefers-color-scheme:dark){.grid{stroke:#2c2c2a}.sale{fill:#2fbf2f;stroke:#1a1a19}}"
    )
    body = (
        f'<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14"/>\n'
        + "\n".join(grid)
        + f'\n<polygon class="area" points="{area}"/>\n<polyline class="line" points="{points}"/>\n'
        + "\n".join(marks)
        + ("\n" + dates if dates else "")
    )
    return _svg(width, height, chart_alt(s), style, body)


def render_banner_off(name: str) -> bytes:
    width, height = BANNER_SIZE
    body = (
        f'<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14"/>\n'
        f'<text class="flame" x="22" y="30" font-size="12" font-weight="700" letter-spacing="1.6">'
        f"{_e(name.upper())} LIVE</text>\n"
        '<text x="22" y="72" font-size="19" font-weight="700">Die Live-Ansicht ist gerade aus.</text>'
    )
    return _svg(width, height, f"{name} live: ausgeschaltet", _NIGHT, body)


def audit_svg(data: bytes) -> list[str]:
    """What is wrong with a picture Ember's code is about to upload (none: it may go up): drawn from numbers and
    escaped text only, so this is the second check: no script, handler, link or anything loaded from elsewhere."""
    if len(data) > SVG_MAX:
        return [f"the picture is larger than {SVG_MAX // 1000} kB"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["the picture isn't UTF-8 text"]
    if not text.startswith('<svg xmlns="http://www.w3.org/2000/svg" '):
        return ["the picture doesn't start with <svg>"]
    found = _SVG_REFUSED.search(text)
    return [f"the picture holds {found[0].strip()!r}"] if found else []


def render(s: Snapshot, parts: Parts, owner: blog.Owner) -> dict[str, bytes]:
    """The files to upload: the page always, the banner and the chart if shown (the chart from its second day on)."""
    files = {PAGE: render_page(s, parts, owner)}
    if parts.banner:
        files[BANNER] = render_banner(s, parts)
    if parts.chart and len(s.days) >= 2 and s.state not in ("dead", "ended"):
        files[CHART] = render_chart(s, parts)
    return files


def check(files: dict[str, bytes], owner: blog.Owner) -> list[str]:
    """Every file checked again before it goes up."""
    problems = []
    for path, data in files.items():
        found = blog.audit(data, owner) if path.endswith(".html") else audit_svg(data)
        problems += [f"{path}: {p}" for p in found]
    return problems


def snippet(name: str) -> str:
    """The HTML the owner puts on their home page once: the banner, linking to the live page (STYLE styles it)."""
    width, height = BANNER_SIZE
    return (
        f'<a class="live-banner" href="/{PAGE}"><img src="/{BANNER}" width="{width}" height="{height}" '
        f'alt="{_e(name)} live: Zustand, Guthaben und Reichweite, alle {UPLOAD_MINUTES} Minuten neu"></a>'
    )


STYLE = """/* ---- Ember live: the banner Ember's code uploads every 15 minutes (live/banner.svg) ---- */

.live-banner {
  display: block;
  width: 100%;
  max-width: 30rem;
  margin-top: 2.25rem;
  border-radius: var(--radius);
}

.live-banner img {
  display: block;
  width: 100%;
  height: auto;
  border-radius: var(--radius);
}

.live-banner:hover img {
  outline: 1px solid var(--flame);
  outline-offset: 2px;
}
"""
