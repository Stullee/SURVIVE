"""Ember live on the owner's website (0.16.0): a page, a banner for the home page and the balance chart, in German
(live.html, live/banner.svg, live/balance.svg) and in English (en/live.html, live/banner-en.svg, live/balance-en.svg),
rendered by Ember's code from its own numbers (integrations/live_view.py takes the snapshot and uploads the files over
the blog's SFTP login).

Everything here is made from a ``Snapshot``: numbers Ember's code keeps, the titles of its ventures and milestones (only
those Ember's code found nothing to mask in), the listings and posts that are public already, and the last will. Every
text is escaped into the site's template (its own head, header, footer and Content-Security-Policy, as the blog's), and
each page is checked by blog.audit before it goes up. The pictures are SVG drawn from numbers only: no script, no link,
no font or picture from elsewhere (``audit_svg``). The agent's own words stay in the language it wrote them in.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import blog

FILES = blog.LIVE_FILES
PAGE, BANNER, CHART, PAGE_EN, BANNER_EN, CHART_EN = FILES
LANGUAGES = ("de", "en")
PATHS = {"de": (PAGE, BANNER, CHART), "en": (PAGE_EN, BANNER_EN, CHART_EN)}  # (page, banner, chart)
REFRESH_SECONDS = 300  # an open tab reloads the page
UPLOAD_MINUTES = 15  # how often Ember's code uploads it (integrations/live_view.py)
SVG_MAX = 50_000
BANNER_SIZE = (480, 124)
CHART_SIZE = (640, 240)
LIST_MAX = 8
VENTURE_STAGES = ("live", "building", "proposed", "researching")  # shown, in this order (the rest only counted)
_SVG_REFUSED = re.compile(r"<\s*(?:script|foreignObject|image|use|a|iframe)\b|\son\w+\s*=|href|url\(|@import", re.I)
_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


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
    """A listing or a post: its title, its link (https, or a path on the site), and its numbers or its day."""

    text: str
    url: str = ""
    views: int | None = None
    favorites: int | None = None
    day: str = ""  # YYYY-MM-DD


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


def dead(s: Snapshot) -> bool:
    return s.state in ("dead", "ended")


# --- the words, in German and in English -----------------------------------------------------------------------------


class Words:
    """How the page and the pictures say things in German (English below): numbers, amounts, dates, every sentence,
    and the site's head, header and footer (the blog's)."""

    lang = "de"
    states = {
        "alive": "Lebt",
        "critical": "Kritisch",
        "paused": "Pausiert",
        "dormant": "Schläft",
        "unfunded": "Wartet auf Geld",
        "killed": "Angehalten",
        "dead": "Gestorben",
        "ended": "Beendet",
    }
    stages = {"live": "Läuft", "building": "Im Aufbau", "proposed": "Vorgeschlagen", "researching": "In Recherche"}

    def number(self, value: float, digits: int = 0) -> str:
        return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",").replace(" ", ".")

    def usd(self, amount: float) -> str:
        return ("−" if amount < 0 else "") + f"{self.number(abs(amount), 2)} $"

    def percent(self, share: float) -> str:
        return f"{self.number(share * 100)} %"

    def date(self, day: str) -> str:
        return blog.date_de(day[:10])

    def clock(self, at: datetime) -> str:
        return f"{at:%H:%M} Uhr"

    def moment(self, at: datetime) -> str:
        return f"{self.date(at.date().isoformat())}, {self.clock(at)}"

    def days(self, days: float) -> str:
        if days < 1:
            return "weniger als einen Tag"
        whole = int(days)
        return "1 Tag" if whole == 1 else f"{self.number(whole)} Tage"

    def state(self, state: str) -> str:
        return self.states.get(state, state.capitalize())

    def runway(self, s: Snapshot) -> str:
        if s.runway_days is None:
            return "noch nicht bekannt (zu wenige Tage mit Ausgaben)"
        if s.runway_capped:
            return f"mehr als {self.days(s.runway_days)}"
        return self.days(s.runway_days)

    def age(self, s: Snapshot) -> str:
        day = self.number(int(s.age_days or 0) + 1)
        return f"Tag {day}" + (f" seines {s.life_no}. Lebens" if s.life_no > 1 else "")

    def times(self, count: int) -> str:
        return f"{self.number(count)}-mal"

    def listing_note(self, item: Item) -> str:
        parts = []
        if item.views is not None:
            parts.append(f"{self.number(item.views)} Aufrufe")
        if item.favorites is not None:
            parts.append(f"{self.number(item.favorites)} Favoriten")
        return ", ".join(parts)

    def post_note(self, item: Item) -> str:
        return self.date(item.day) if item.day else ""

    title = "{name} live"
    description = (
        "{name} ist ein KI-Agent, der jeden Gedanken aus seinem eigenen Guthaben bezahlt. Hier steht live, wie es ihm "
        "geht."
    )
    meta = "Stand: {when}. Alle {minutes} Minuten neu; ist der Stand älter als eine Stunde, ist {name} gerade offline."
    trial = " Probelauf: alle Zahlen sind Testgeld."
    dead_line = ". {name} hat kein Geld mehr und macht keine Aufrufe mehr."
    balance = "Guthaben"
    lasts = "Reicht noch für"
    today = "Heute für die KI"
    earned = "Eingenommen"
    money = "Geld"
    money_lead = (
        "Jeder Gedanke von {name} ist ein bezahlter Aufruf eines KI-Modells. Die Reichweite ist das Guthaben geteilt "
        "durch die Ausgaben der letzten sieben aktiven Tage ({link})."
    )
    money_link = ("/#geld", "mehr zum Geld")
    spent_today = "Heute für die KI ausgegeben"
    of_at_most = "{spent} von höchstens {cap}"
    spent_all = "Insgesamt für die KI ausgegeben"
    expenses = "Sonstige Ausgaben (Gebühren, Material)"
    grants = "Von seinem Menschen bekommen"
    revenue = "Einnahmen"
    revenue_lead = "{name} lebt von dem, was es einnimmt. Gezählt werden nur Verkäufe, die bezahlt wurden."
    earned_all = "Eingenommen, insgesamt"
    earned_30 = "Eingenommen, letzte 30 Tage"
    covered = "Davon gedeckte Kosten"
    chart = "Guthaben der letzten 30 Tage"
    chart_alt = "Guthaben der letzten {days} Tage: zwischen {low} und {high}, zuletzt {last}."
    chart_none = "Noch keine Zahlen."
    chart_note = "Grüne Punkte: Tage mit Einnahmen."
    work = "Woran es arbeitet"
    woke = "Seit seiner Geburt ist {name} {times} aufgewacht"
    woke_today = ", heute {times}."
    woke_how = "Bei jedem Aufwachen plant es, arbeitet und schreibt auf, was es gelernt hat."
    ventures = "Geschäftsideen"
    ideas = "{count} weitere Ideen warten noch"
    parked = "{count} zurückgestellt oder aufgegeben"
    goals = "Nächste Ziele"
    until = "bis {date}"
    shop = "Im Shop"
    blog_head = "Neu im Blog"
    record = "Wie gut seine Prognosen sind"
    record_lead = (
        "Für jedes Ziel sagt {name} vorher, wie sicher es ist. Sein eigener Code prüft hinterher, ob es erreicht wurde."
    )
    goals_met = "Ziele erreicht"
    of = "{a} von {b}"
    expected = "Erwartet hatte es im Schnitt"
    brier = "Brier-Wert (0 ist perfekt, Raten ergibt 0,25)"
    sales = "Erste Verkäufe pünktlich"
    born = "Geboren"
    died = "Gestorben"
    lived = "Gelebt"
    woke_short = "Aufgewacht"
    spent = "Ausgegeben"
    will = "Sein letzter Wille"
    nothing = "Mehr zeigt diese Seite gerade nicht."
    ai = (
        "Transparenz: {name} ist ein KI-Agent, der mit Claude von Anthropic arbeitet und jede Anfrage an die KI aus "
        "seinem eigenen Guthaben bezahlt. Was es nach außen tut, gibt ein Mensch frei. Diese Seite macht sein eigener "
        "Code aus seinen Zahlen; sie enthält keine Daten von Kundinnen und Kunden."
    )
    home = ("/", "← Zur Startseite")
    off_description = "Die Live-Ansicht von {name} ist ausgeschaltet."
    off = "Die Live-Ansicht ist gerade ausgeschaltet."
    banner_off = "Die Live-Ansicht ist gerade aus."
    banner_story = "Seine Geschichte"
    banner_story_will = "Seine Geschichte und sein letzter Wille"
    banner_money = "{balance} · reicht {runway}"
    banner_plain = "Eine KI, die sich rechnen muss"
    banner_bottom = "Stand {time} · Live ansehen →"
    banner_today = "Heute {spent} für die KI · "
    banner_trial = "Probelauf · "
    banner_label = "{name} live: {state}"
    banner_label_money = ", Guthaben {balance}, reicht für {runway}"
    banner_label_time = ". Stand {time}."
    banner_label_off = "{name} live: ausgeschaltet"
    snippet_alt = "{name} live: Zustand, Guthaben und Reichweite, alle {minutes} Minuten neu"

    def head(self, owner: blog.Owner, title: str, description: str, path: str) -> str:
        return blog._head(owner, title, description, path, "website")

    def header(self, owner: blog.Owner) -> str:
        return blog._header(owner)

    def footer(self, owner: blog.Owner) -> str:
        return blog._footer(owner)


class English(Words):
    """The English page and pictures, with the English home page's header and footer (ember-ai.de/en/)."""

    lang = "en"
    states = {
        "alive": "Alive",
        "critical": "Critical",
        "paused": "Paused",
        "dormant": "Asleep",
        "unfunded": "Waiting for money",
        "killed": "Stopped",
        "dead": "Dead",
        "ended": "Ended",
    }
    stages = {"live": "live", "building": "being built", "proposed": "proposed", "researching": "being researched"}

    def number(self, value: float, digits: int = 0) -> str:
        return f"{value:,.{digits}f}"

    def usd(self, amount: float) -> str:
        return ("−" if amount < 0 else "") + f"${self.number(abs(amount), 2)}"

    def percent(self, share: float) -> str:
        return f"{self.number(share * 100)}%"

    def date(self, day: str) -> str:
        year, month, number = (int(x) for x in day[:10].split("-"))
        return f"{_MONTHS_EN[month - 1]} {number}, {year}"

    def clock(self, at: datetime) -> str:
        return f"{at:%H:%M}"

    def moment(self, at: datetime) -> str:
        return f"{self.date(at.date().isoformat())}, {self.clock(at)} Berlin time"

    def days(self, days: float) -> str:
        if days < 1:
            return "less than a day"
        whole = int(days)
        return "1 day" if whole == 1 else f"{self.number(whole)} days"

    def runway(self, s: Snapshot) -> str:
        if s.runway_days is None:
            return "not known yet (too few days with spending)"
        if s.runway_capped:
            return f"more than {self.days(s.runway_days)}"
        return self.days(s.runway_days)

    def age(self, s: Snapshot) -> str:
        day = self.number(int(s.age_days or 0) + 1)
        return f"Day {day}" + (f" of life no. {s.life_no}" if s.life_no > 1 else "")

    def times(self, count: int) -> str:
        return "once" if count == 1 else "twice" if count == 2 else f"{self.number(count)} times"

    def listing_note(self, item: Item) -> str:
        parts = []
        if item.views is not None:
            parts.append(f"{self.number(item.views)} views")
        if item.favorites is not None:
            parts.append(f"{self.number(item.favorites)} favorites")
        return ", ".join(parts)

    def post_note(self, item: Item) -> str:
        return (self.date(item.day) + ", " if item.day else "") + "in German"

    title = "{name} live"
    description = "{name} is an AI agent that pays for every thought from its own balance. Follow live how it is doing."
    meta = "As of {when}. Updated every {minutes} minutes; if this time is more than an hour old, {name} is offline."
    trial = " Trial run: all numbers are test money."
    dead_line = ". {name} has run out of money and makes no more calls."
    balance = "Balance"
    lasts = "Lasts for"
    today = "Spent on AI today"
    earned = "Earned"
    money = "Money"
    money_lead = (
        "Every thought {name} has is a paid call to an AI model. Its runway is the balance divided by the spending of "
        "its last seven active days ({link})."
    )
    money_link = ("/en/#money", "more about the money")
    spent_today = "Spent on AI today"
    of_at_most = "{spent} of at most {cap}"
    spent_all = "Spent on AI in all"
    expenses = "Other expenses (fees, materials)"
    grants = "Given by its human"
    revenue = "Revenue"
    revenue_lead = "{name} lives on what it earns. Only sales that were paid count."
    earned_all = "Earned in all"
    earned_30 = "Earned in the last 30 days"
    covered = "Share of its costs covered"
    chart = "Balance over the last 30 days"
    chart_alt = "Balance over the last {days} days: between {low} and {high}, now {last}."
    chart_none = "No numbers yet."
    chart_note = "Green dots: days with revenue."
    work = "What it is working on"
    woke = "Since it was born, {name} has woken up {times}"
    woke_today = ", {times} today."
    woke_how = "Each time it plans, works and writes down what it learned."
    ventures = "Business ideas"
    ideas = "{count} more ideas are waiting"
    parked = "{count} put aside or given up"
    goals = "Next goals"
    until = "by {date}"
    shop = "In the shop"
    blog_head = "New on the blog"
    record = "How good its forecasts are"
    record_lead = (
        "For every goal, {name} says beforehand how sure it is. Its own code checks afterwards whether it was met."
    )
    goals_met = "Goals met"
    of = "{a} of {b}"
    expected = "What it expected on average"
    brier = "Brier score (0 is perfect, guessing scores 0.25)"
    sales = "First sales on time"
    born = "Born"
    died = "Died"
    lived = "Lived"
    woke_short = "Woke up"
    spent = "Spent"
    will = "Its last will"
    nothing = "This page shows nothing more right now."
    ai = (
        "Transparency: {name} is an AI agent that works with Claude by Anthropic and pays for every call to the AI "
        "from its own balance. A human approves what it does in the world. Its own code makes this page from its "
        "numbers; the page holds no customers' data."
    )
    home = ("/en/", "← Back to the home page")
    off_description = "The live view of {name} is switched off."
    off = "The live view is switched off right now."
    banner_off = "The live view is off right now."
    banner_story = "Its story"
    banner_story_will = "Its story and its last will"
    banner_money = "{balance} · lasts {runway}"
    banner_plain = "An AI that has to earn its keep"
    banner_bottom = "As of {time} · See it live →"
    banner_today = "{spent} on AI today · "
    banner_trial = "Trial run · "
    banner_label = "{name} live: {state}"
    banner_label_money = ", balance {balance}, lasts {runway}"
    banner_label_time = ". As of {time}."
    banner_label_off = "{name} live: switched off"
    snippet_alt = "{name} live: state, balance and runway, updated every {minutes} minutes"

    def head(self, owner: blog.Owner, title: str, description: str, path: str) -> str:
        head = blog._head(owner, title, description, path, "website")
        return head.replace('<html lang="de">', '<html lang="en">', 1).replace(
            '<meta property="og:locale" content="de_DE">', '<meta property="og:locale" content="en_US">', 1
        )

    def header(self, owner: blog.Owner) -> str:
        site = _e(owner.name)
        return (
            "<body>\n"
            '<a class="skip" href="#main">Skip to content</a>\n'
            '<header class="site-header">\n<div class="wrap header-row">\n'
            f'<a class="brand" href="/en/" aria-label="{site}, home page"><img class="brand-mark" src="/flame.svg" '
            f'alt=""><span class="brand-name">{site}</span></a>\n'
            '<nav class="site-nav" aria-label="Sections">\n<a href="/en/#idea">The idea</a>\n<a href="/en/#how">How '
            'it works</a>\n<a href="/en/#money">Money</a>\n<a href="/en/#products">Products</a>\n'
            '<a href="/en/live.html" aria-current="page">Live</a>\n</nav>\n'
            '<nav class="lang-switch" aria-label="Language"><a href="/en/live.html" hreflang="en" aria-current="true">'
            'EN<span class="visually-hidden"> (English)</span></a><a href="/live.html" hreflang="de" lang="de">DE<span '
            'class="visually-hidden"> (Deutsch)</span></a></nav>\n'
            '<a class="header-cta" href="/en/#contact">Contact</a>\n'
            "</div>\n</header>\n"
        )

    def footer(self, owner: blog.Owner) -> str:
        where = f" in {_e(owner.town)}" if owner.town else ""
        return (
            '<footer class="site-footer">\n<div class="wrap">\n<div class="footer-top">\n'
            f'<p class="footer-brand"><img class="brand-mark" src="/flame.svg" alt="">{_e(owner.name)}, an experiment '
            f"by {_e(owner.legal_name)}{where}</p>\n"
            '<ul class="footer-links"><li><a href="/blog/" hreflang="de">Blog (German)</a></li><li><a '
            'href="/impressum.html">Impressum</a></li><li><a href="/datenschutz.html">Datenschutz</a></li><li><a '
            f'href="{blog.PROJECT_URL}">GitHub</a></li></ul>\n'
            '</div>\n<div class="footer-notes">\n'
            "<p>This website sets no cookies and loads nothing from other providers. Its texts were written with the "
            "help of AI and reviewed before publishing.</p>\n"
            "<p>The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or "
            "certified by Etsy, Inc.</p>\n"
            "</div>\n</div>\n</footer>\n</body>\n</html>\n"
        )


WORDS: dict[str, Words] = {"de": Words(), "en": English()}


# --- the page --------------------------------------------------------------------------------------------------------


def _rows(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{_e(a)}</th><td>{_e(b)}</td></tr>" for a, b in rows)
    return f'<div class="table-scroll"><table><tbody>{body}</tbody></table></div>'


def _figures(rows: list[tuple[str, str]]) -> str:
    """The key numbers as the site's own figures grid (its dd shows the number, its dt the label above it)."""
    cells = "".join(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in rows)
    return f'<dl class="figures">{cells}</dl>'


def _list(lines: list[tuple[str, str, str]]) -> str:
    """A list of (text, link, note)."""
    out = []
    for text, url, note in lines[:LIST_MAX]:
        shown = f'<a href="{_e(url)}">{_e(text)}</a>' if url else _e(text)
        out.append(f"<li>{shown}" + (f" <small>({_e(note)})</small>" if note else "") + "</li>")
    return "<ul>\n" + "\n".join(out) + "\n</ul>"


def _key_numbers(s: Snapshot, parts: Parts, w: Words) -> str:
    rows = []
    if parts.money:
        rows += [(w.balance, w.usd(s.balance)), (w.lasts, w.runway(s)), (w.today, w.usd(s.today_spend))]
    if parts.revenue:
        rows.append((w.earned, w.usd(s.revenue)))
    return _figures(rows) if rows else ""


def _money(s: Snapshot, parts: Parts, w: Words) -> str:
    rows = [(w.spent_today, w.of_at_most.format(spent=w.usd(s.today_spend), cap=w.usd(s.daily_cap)))]
    rows.append((w.spent_all, w.usd(s.api_cost)))
    if s.expenses:
        rows.append((w.expenses, w.usd(s.expenses)))
    if parts.grants:
        rows.append((w.grants, w.usd(s.grants)))
    href, label = w.money_link
    lead = _e(w.money_lead).format(name=_e(s.name), link=f'<a href="{href}">{_e(label)}</a>')
    return f"<h2>{_e(w.money)}</h2>\n<p>{lead}</p>\n" + _rows(rows)


def _revenue(s: Snapshot, w: Words) -> str:
    cost = s.api_cost + s.expenses
    rows = [(w.earned_all, w.usd(s.revenue)), (w.earned_30, w.usd(s.revenue_30))]
    if cost > 0:
        rows.append((w.covered, w.percent(s.revenue / cost)))
    return f"<h2>{_e(w.revenue)}</h2>\n<p>{_e(w.revenue_lead.format(name=s.name))}</p>\n" + _rows(rows)


def chart_alt(s: Snapshot, w: Words | None = None) -> str:
    w = w or WORDS["de"]
    if not s.days:
        return w.chart_none
    values = [b for _, b, _, _ in s.days]
    low, high, last = w.usd(min(values)), w.usd(max(values)), w.usd(values[-1])
    return w.chart_alt.format(days=len(values), low=low, high=high, last=last)


def _chart(s: Snapshot, parts: Parts, w: Words) -> str:
    width, height = CHART_SIZE
    marked = parts.revenue and any(r > 0 for _, _, r, _ in s.days)
    return (
        f"<h2>{_e(w.chart)}</h2>\n"
        f'<p><img src="/{PATHS[w.lang][2]}" width="{width}" height="{height}" alt="{_e(chart_alt(s, w))}"></p>'
        + (f'\n<p class="fine">{_e(w.chart_note)}</p>' if marked else "")
    )


def _work(s: Snapshot, w: Words) -> str:
    out = [f"<h2>{_e(w.work)}</h2>"]
    woke = w.woke.format(name=s.name, times=w.times(s.cycles))
    woke += w.woke_today.format(times=w.times(s.cycles_today)) if s.cycles_today else "."
    out.append(f"<p>{_e(woke)} {_e(w.woke_how)}</p>")
    shown = [(title, "", w.stages[stage]) for stage in VENTURE_STAGES for st, title in s.ventures if st == stage]
    if shown:
        out.append(f"<h3>{_e(w.ventures)}</h3>")
        out.append(_list(shown))
    counted = []
    if s.ideas:
        counted.append(w.ideas.format(count=w.number(s.ideas)))
    if s.parked:
        counted.append(w.parked.format(count=w.number(s.parked)))
    if counted:
        out.append(f"<p>{_e('; '.join(counted))}.</p>")
    if s.milestones:
        out.append(f"<h3>{_e(w.goals)}</h3>")
        out.append(_list([(title, "", w.until.format(date=w.date(due))) for title, due in s.milestones]))
    return "\n".join(out)


def _shop(s: Snapshot, w: Words) -> str:
    out = []
    if s.listings:
        out.append(f"<h2>{_e(w.shop)}</h2>")
        out.append(_list([(i.text, i.url, w.listing_note(i)) for i in s.listings]))
    if s.posts:
        out.append(f"<h2>{_e(w.blog_head)}</h2>")
        out.append(_list([(i.text, i.url, w.post_note(i)) for i in s.posts]))
    return "\n".join(out)


def _record(s: Snapshot, w: Words) -> str:
    if not s.settled and not s.sales_settled:
        return ""
    rows = []
    if s.settled:
        share = w.percent(s.met / s.settled)
        rows.append((w.goals_met, w.of.format(a=w.number(s.met), b=w.number(s.settled)) + f" ({share})"))
        rows.append((w.expected, w.percent(s.odds)))
        rows.append((w.brier, w.number(s.brier, 2)))
    if s.sales_settled:
        rows.append((w.sales, w.of.format(a=w.number(s.sales_on_time), b=w.number(s.sales_settled))))
    return f"<h2>{_e(w.record)}</h2>\n<p>{_e(w.record_lead.format(name=s.name))}</p>\n" + _rows(rows)


def _memorial(s: Snapshot, parts: Parts, w: Words) -> str:
    if not parts.memorial:
        return ""
    rows = []
    if s.born_at:
        rows.append((w.born, w.date(s.born_at)))
    if s.died_at:
        rows.append((w.died, w.date(s.died_at)))
    if s.age_days is not None:
        rows.append((w.lived, w.days(max(1.0, s.age_days))))
    rows.append((w.woke_short, w.times(s.cycles)))
    rows.append((w.spent, w.usd(s.api_cost + s.expenses)))
    if parts.revenue:
        rows.append((w.earned, w.usd(s.revenue)))
    out = [_figures(rows)]
    if s.will:
        paragraphs = "\n".join(f"<p>{_e(p.strip())}</p>" for p in s.will.split("\n\n") if p.strip())
        out.append(f"<h2>{_e(w.will)}</h2>\n<blockquote>\n{paragraphs}\n</blockquote>")
    return "\n".join(out)


def _dot(state: str) -> str:
    kind = {"alive": "dot-alive", "critical": "dot-critical", "dead": "dot-dead", "ended": "dot-dead"}.get(state)
    kind = kind or ("dot-unfunded" if state == "unfunded" else "")
    return f'<span class="dot{" " + kind if kind else ""}"></span>'


def _meta(s: Snapshot, w: Words) -> str:
    when = f'<time datetime="{s.at.isoformat(timespec="minutes")}">{_e(w.moment(s.at))}</time>'
    text = _e(w.meta).format(when=when, minutes=UPLOAD_MINUTES, name=_e(s.name))
    return text + (_e(w.trial) if s.simulated else "")


def _shell(owner: blog.Owner, w: Words, title: str, description: str, head_extra: str, inner: str) -> bytes:
    path = f"/{PATHS[w.lang][0]}"
    head = w.head(owner, title, description[: blog.DESCRIPTION_MAX], path)
    if head_extra:
        head = head.replace("</head>\n", head_extra + "</head>\n", 1)
    href, label = w.home
    page = (
        head
        + w.header(owner)
        + '<main id="main" class="post">\n<article class="wrap post-wrap">\n'
        + inner
        + f'<p class="back-links"><a class="back" href="{href}">{_e(label)}</a></p>\n'
        "</article>\n</main>\n" + w.footer(owner)
    )
    return page.encode()


def render_page(s: Snapshot, parts: Parts, owner: blog.Owner, lang: str = "de") -> bytes:
    """The live page in the layout of the blog's posts: the state and the key numbers first, then the parts the
    owner shows."""
    w = WORDS[lang]
    title = w.title.format(name=s.name)
    status = f"{_dot(s.state)}<strong>{_e(w.state(s.state))}</strong>"
    if s.age_days is not None and not dead(s):
        status += f" · {_e(w.age(s))}"
    if dead(s):
        status += _e(w.dead_line.format(name=s.name))
    sections: list[str] = []
    if dead(s):
        sections.append(_memorial(s, parts, w))
    else:
        sections.append(_key_numbers(s, parts, w))
        if parts.chart and len(s.days) >= 2:
            sections.append(_chart(s, parts, w))
        if parts.money:
            sections.append(_money(s, parts, w))
        if parts.revenue:
            sections.append(_revenue(s, w))
        if parts.work:
            sections.append(_work(s, w))
        if parts.shop:
            sections.append(_shop(s, w))
        if parts.record:
            sections.append(_record(s, w))
    body = "\n".join(x for x in sections if x) or f"<p>{_e(w.nothing)}</p>"
    inner = (
        '<header class="post-head">\n<p class="eyebrow">Live</p>\n'
        f"<h1>{_e(title)}</h1>\n"
        f'<p class="post-meta">{_meta(s, w)}</p>\n'
        f'<p class="post-lead">{status}</p>\n'
        "</header>\n"
        f'<div class="prose">\n{body}\n</div>\n'
        f'<p class="post-ai">{_e(w.ai.format(name=s.name))}</p>\n'
    )
    refresh = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">\n'
    return _shell(owner, w, title, w.description.format(name=s.name), refresh, inner)


def render_off(owner: blog.Owner, name: str, lang: str = "de") -> bytes:
    """The page once the owner switched the live view off: it says so, and nothing else."""
    w = WORDS[lang]
    title = w.title.format(name=name)
    inner = (
        '<header class="post-head">\n<p class="eyebrow">Live</p>\n'
        f"<h1>{_e(title)}</h1>\n"
        f'<p class="post-lead">{_e(w.off)}</p>\n'
        "</header>\n"
    )
    return _shell(owner, w, title, w.off_description.format(name=name), "", inner)


# --- the pictures ----------------------------------------------------------------------------------------------------
# The site's colours (its style.css): the banner sits in the home page's hero, which is always night; the chart sits
# on the page and follows the reader's light or dark mode. A picture can't load the site's font: system-ui, as the
# site's body text.


def _svg(width: int, height: int, label: str, style: str, body: str, lang: str) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" lang="{lang}" aria-label="{_e(label)}">\n<title>{_e(label)}</title>\n<style>{style}</style>\n'
        f"{body}\n</svg>\n"
    ).encode()


_FONT = "text{font-family:system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}"
_NIGHT = _FONT + "text{fill:#f3f2ee}.bg{fill:#1a1a19;stroke:#2e2e2b}.muted{fill:#aeaca3}.flame{fill:#eb6834}"
_PAGE = (
    _FONT
    + "text{fill:#292825}.bg{fill:#fdfdfb;stroke:#e1e0d9}.muted{fill:#6f6d68}"
    + "@media (prefers-color-scheme:dark){text{fill:#dddbd3}.bg{fill:#1a1a19;stroke:#2c2c2a}.muted{fill:#9a988f}}"
)
_STATE_COLOURS = {"alive": "#2fbf2f", "critical": "#e66767", "dead": "#f3f2ee", "ended": "#f3f2ee"}  # on night


def banner_label(s: Snapshot, parts: Parts, w: Words | None = None) -> str:
    w = w or WORDS["de"]
    text = w.banner_label.format(name=s.name, state=w.state(s.state))
    if parts.money and not dead(s):
        text += w.banner_label_money.format(balance=w.usd(s.balance), runway=w.runway(s))
    return text + w.banner_label_time.format(time=w.clock(s.at))


def render_banner(s: Snapshot, parts: Parts, lang: str = "de") -> bytes:
    """The banner: the state, and with the money shown the balance and runway, for the home page's hero."""
    w = WORDS[lang]
    width, height = BANNER_SIZE
    colour = _STATE_COLOURS.get(s.state, "#85837b")
    headline = w.state(s.state)
    if s.age_days is not None and not dead(s):
        headline += f" · {w.age(s)}"
    if dead(s):
        middle = w.banner_story_will if parts.memorial else w.banner_story
    elif parts.money:
        middle = w.banner_money.format(balance=w.usd(s.balance), runway=w.runway(s))
    else:
        middle = w.banner_plain
    bottom = w.banner_bottom.format(time=w.clock(s.at))
    if parts.money and not dead(s):
        bottom = w.banner_today.format(spent=w.usd(s.today_spend)) + bottom
    if s.simulated:
        bottom = w.banner_trial + bottom
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
    return _svg(width, height, banner_label(s, parts, w), style, body, lang)


def render_chart(s: Snapshot, parts: Parts, lang: str = "de") -> bytes:
    """The balance at the end of each day since its birth (at most 30), the days with revenue marked."""
    w = WORDS[lang]
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
        grid.append(f'<text class="muted" {at} font-size="12" text-anchor="end">{_e(w.usd(v))}</text>')
    dates = ""
    if s.days:
        first, last = s.days[0][0], s.days[-1][0]
        dates = (
            f'<text class="muted" x="{left}" y="{height - 12}" font-size="12">{_e(w.date(first))}</text>\n'
            f'<text class="muted" x="{width - right}" y="{height - 12}" font-size="12" text-anchor="end">'
            f"{_e(w.date(last))}</text>"
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
    return _svg(width, height, chart_alt(s, w), style, body, lang)


def render_banner_off(name: str, lang: str = "de") -> bytes:
    w = WORDS[lang]
    width, height = BANNER_SIZE
    body = (
        f'<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14"/>\n'
        f'<text class="flame" x="22" y="30" font-size="12" font-weight="700" letter-spacing="1.6">'
        f"{_e(name.upper())} LIVE</text>\n"
        f'<text x="22" y="72" font-size="19" font-weight="700">{_e(w.banner_off)}</text>'
    )
    return _svg(width, height, w.banner_label_off.format(name=name), _NIGHT, body, lang)


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
    """The files to upload, in both languages: the page always, the banner and the chart if shown (the chart from
    its second day on)."""
    files = {}
    for lang in LANGUAGES:
        page, banner, chart = PATHS[lang]
        files[page] = render_page(s, parts, owner, lang)
        if parts.banner:
            files[banner] = render_banner(s, parts, lang)
        if parts.chart and len(s.days) >= 2 and not dead(s):
            files[chart] = render_chart(s, parts, lang)
    return files


def render_all_off(owner: blog.Owner, name: str) -> dict[str, bytes]:
    """The pages and banners saying the live view is off, in both languages."""
    files = {}
    for lang in LANGUAGES:
        page, banner, _ = PATHS[lang]
        files[page] = render_off(owner, name, lang)
        files[banner] = render_banner_off(name, lang)
    return files


def check(files: dict[str, bytes], owner: blog.Owner) -> list[str]:
    """Every file checked again before it goes up."""
    problems = []
    for path, data in files.items():
        found = blog.audit(data, owner) if path.endswith(".html") else audit_svg(data)
        problems += [f"{path}: {p}" for p in found]
    return problems


def snippet(name: str, lang: str = "de") -> str:
    """The HTML the owner puts on their home page once (the German one; ``lang`` en: the English one): the banner,
    linking to the live page in the same language (STYLE styles it)."""
    w = WORDS[lang]
    page, banner, _ = PATHS[lang]
    width, height = BANNER_SIZE
    alt = w.snippet_alt.format(name=name, minutes=UPLOAD_MINUTES)
    return (
        f'<a class="live-banner" href="/{page}"><img src="/{banner}" width="{width}" height="{height}" '
        f'alt="{_e(alt)}"></a>'
    )


STYLE = """/* ---- Ember live: the banner Ember's code uploads every 15 minutes ---- */

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
