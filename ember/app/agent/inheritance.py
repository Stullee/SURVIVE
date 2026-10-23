"""The inheritance (0.48.0, vision/generations.md, "The inheritance: a clean mind, but smarter"): what passes from one
generation of Ember to the next besides the world and the estate. It passes generalisations, not episodes, through a
narrow channel, so what passes is general and backed by evidence, and the noise of a life stays behind:

* **principles**, at most MAX_PRINCIPLES: business lessons with the cases of hers behind them. They enter the next
  mind's playbook as inherited hypotheses (learning.py): her predecessor's cases count as one case, two of her own
  establish one, her first against disputes it, INHERITED_FADE_DAYS without one retire it. Priors to test, not dogma;
* **facts** Ember's code computes from the books and the platforms' syncs (``facts``), dated: what the market did with
  this generation's work. No model writes them, so none can get them wrong;
* **craft**: files of her workspace she would need again (a checklist, a book's spec), and notes the evaluation wrote
  (the owner's quality bar), copied to ``inheritance/craft/`` in the new workspace;
* **her letter** (``letters``): her own words to the next generation, asked for by the owner, written by one call of
  hers as the last will is. Her view, not evidence: it is a file, never in a plan;
* **a seed strategy and identity** (``strategy_text``, ``identity_text``): the facts, the evaluation's direction and
  who she is, for her to rewrite when her own evidence says so.

An evaluation outside the app drafts it as an inheritance.json (INHERIT.md in the export, inheritance_brief.md) from
the whole-life export: it strikes what the records contradict and what her code's bugs taught her. Ember's code checks
it (``parse``): every principle cites cases of hers that are evidence (not too early, not low-sure), names no tool (a
tool's limit doesn't pass) and no id of hers (the new mind has none), and the size limits hold. The owner reads its
preview on the System tab, strikes what shouldn't pass and gives it (``give``); it waits there until the change writes
it (generation.begin: ``write_principles`` in its transaction, ``write_files`` after). Nothing of it reaches her
before the change, and nothing of it reaches the mind that hands it on."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .. import events, feedback
from ..economy.clock import Clock, local_date, to_iso
from ..economy.costs import micros_to_usd
from . import estate, memory
from .sandbox import TEXT_EXTENSIONS, Jail, SandboxError
from .store import AgentScope

if TYPE_CHECKING:
    from .service import Agent

FORMAT = 1
MAX_BYTES = 64_000  # the file
MAX_PRINCIPLES = 12
MAX_CASES = 30  # cited for or against one principle
MAX_CRAFT = 8
CRAFT_BYTES = 300 * 1024  # one file of her workspace
CRAFT_TOTAL = 2 * 1024 * 1024
NOTE_CHARS = 4_000  # a note the evaluation wrote
DIRECTION_CHARS = 600  # the evaluation's direction in the seed strategy
WHY_CHARS = 300  # why an item passes, for the owner
PRINCIPLE_CHARS = 300  # the principles table's limit
LETTER_CHARS = 3_000  # prompts.LETTER_CHARS: what the letter asks for
FOLDER = "inheritance"  # in the new mind's workspace
CRAFT = f"{FOLDER}/craft"
RESERVED = frozenset({"letter.md", "facts.md"})
KEYS = frozenset({"format", "evaluation", "generation", "principles", "strategy", "craft", "estate"})
PRINCIPLE_KEYS = frozenset({"text", "for", "against", "from", "why"})
CRAFT_KEYS = frozenset({"path", "name", "text", "why"})
STRIKE_KEYS = frozenset({"principles", "craft", "strategy", "letter"})
NOTE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,59}\.(?:md|txt)")
_ID = re.compile(r"#\s?\d")  # an id of hers ("project #12", "#c389"): the new mind has none of them
PRODUCT_LINES = 15  # products the facts list one by one


class InheritanceError(ValueError):
    """An inheritance that can't be given; ``field`` names what is wrong, the message is for the owner."""

    def __init__(self, field: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.field = field
        self.status = status


@dataclass(frozen=True)
class Draft:
    """An inheritance.json, checked: what would pass before the owner's strikes."""

    evaluation: str
    generation: int
    principles: tuple[dict[str, Any], ...]
    direction: str
    craft: tuple[dict[str, Any], ...]
    estate: tuple[dict[str, str], ...]
    payload: str  # its content as JSON with sorted keys: what its SHA-256 is of

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload.encode()).hexdigest()


# --- the evaluation's draft, checked ---


def parse(
    conn: sqlite3.Connection,
    scope: AgentScope,
    body: Any,
    workspace: Jail | None,
    products: set[tuple[str, str]] | None = None,
) -> Draft:
    """An inheritance.json's content, checked against this mind's records and workspace (``products``: the estate's,
    which its proposals must name). Raises InheritanceError naming the first thing wrong."""
    if not isinstance(body, dict):
        raise InheritanceError("inheritance", "an inheritance is a JSON object (see INHERIT.md in the export)")
    if len(json.dumps(body, ensure_ascii=False)) > MAX_BYTES:
        raise InheritanceError("inheritance", f"an inheritance holds at most {MAX_BYTES:,} characters")
    _keys(body, KEYS, "inheritance")
    if body.get("format", FORMAT) != FORMAT:
        raise InheritanceError("format", f"this version of Ember reads inheritances of format {FORMAT}")
    name = body.get("evaluation")
    if not isinstance(name, str) or not feedback.NAME.fullmatch(name):
        raise InheritanceError("evaluation", "evaluation names it: 1 to 40 letters, digits, spaces and . : _ -")
    number = estate.generation(scope)
    if body.get("generation") != number:
        raise InheritanceError(
            "generation", f"generation is {number}: the generation that hands it on (this export's, INHERIT.md)"
        )
    where, params = scope.where()
    cases = {int(r["id"]): r for r in conn.execute(f"SELECT id, cause, sure FROM cases WHERE {where}", params)}
    own = {int(r[0]) for r in conn.execute(f"SELECT id FROM principles WHERE {where}", params)}
    tools = _tool_names()
    principles = tuple(
        _principle(p, n, cases, own, tools) for n, p in enumerate(_list(body, "principles", MAX_PRINCIPLES), start=1)
    )
    if len({p["text"].casefold() for p in principles}) < len(principles):
        raise InheritanceError("principles", "principles names each principle once")
    direction = _text(body, "strategy", DIRECTION_CHARS, "strategy")
    _clean(direction, "strategy", tools)
    craft = _craft(_list(body, "craft", MAX_CRAFT), workspace)
    proposals = tuple(
        _proposal(p, n, products) for n, p in enumerate(_list(body, "estate", feedback.MAX_ESTATE), start=1)
    )
    if len({(p["kind"], p["ref"]) for p in proposals}) < len(proposals):
        raise InheritanceError("estate", "estate proposes once for each product")
    if not principles and not direction and not craft:
        raise InheritanceError(
            "inheritance", "an inheritance passes at least one principle, a strategy or a craft file"
        )
    content = {
        "format": FORMAT,
        "evaluation": name,
        "generation": number,
        "principles": list(principles),
        "strategy": direction,
        "craft": list(craft),
        "estate": list(proposals),
    }
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return Draft(name, number, principles, direction, craft, proposals, payload)


def _tool_names() -> set[str]:
    from . import tools  # here, not at the top: the tools import most of the agent

    return memory.identifiers(tools.HANDLERS)


def _keys(item: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    other = sorted(set(item) - allowed)
    if other:
        raise InheritanceError(where, f"{where} takes only {', '.join(sorted(allowed))}, not {', '.join(other)}")


def _list(body: dict[str, Any], key: str, most: int) -> list[Any]:
    items = body.get(key, [])
    if not isinstance(items, list) or len(items) > most:
        raise InheritanceError(key, f"{key} is a list of at most {most}")
    return items


def _text(item: dict[str, Any], key: str, limit: int, where: str, required: bool = False) -> str:
    value = item.get(key, "")
    if not isinstance(value, str):
        raise InheritanceError(where, f"{where}: {key} is a text")
    text = " ".join(value.split())
    if required and not text:
        raise InheritanceError(where, f"{where} needs its {key}")
    if len(text) > limit:
        raise InheritanceError(where, f"{where}: {key} has {len(text):,} characters, at most {limit:,}")
    return text


def _clean(text: str, where: str, tools: set[str]) -> None:
    """What reaches the next mind names no id of hers (it has none of them) and no tool (a tool's limit is her code's,
    which changes: it doesn't pass)."""
    if memory.heading_line(text):
        raise InheritanceError(where, f"{where}: a line begins with '=', as only the headings of her context do")
    if _ID.search(text):
        raise InheritanceError(
            where, f"{where} names an id of hers (#…): the next generation has none of her records, say what it was"
        )
    named = sorted(t for t in tools if memory.about_tool(text, {t}))
    if named:
        raise InheritanceError(where, f"{where} names the tool {named[0]}: a tool's limit doesn't pass, a lesson does")


def _ids(value: Any, where: str, key: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CASES:
        raise InheritanceError(where, f"{where}: {key} is a list of at most {MAX_CASES} case ids")
    if any(isinstance(i, bool) or not isinstance(i, int) for i in value):
        raise InheritanceError(where, f"{where}: {key} lists case ids (numbers)")
    return sorted(set(value))


def _principle(item: Any, number: int, cases: dict[int, sqlite3.Row], own: set[int], tools: set[str]) -> dict[str, Any]:
    """A principle that passes: its text, the cases of hers for and against it (evidence: not too early; the cases for
    it not low-sure), the principle of hers it was, and why it passes (for the owner)."""
    where = f"principle {number}"
    if not isinstance(item, dict):
        raise InheritanceError(where, f"{where} is an object")
    _keys(item, PRINCIPLE_KEYS, where)
    text = _text(item, "text", PRINCIPLE_CHARS, where, required=True)
    _clean(text, where, tools)
    supports, against = _ids(item.get("for"), where, "for"), _ids(item.get("against"), where, "against")
    if not supports:
        raise InheritanceError(
            where, f"{where} cites the cases of hers for it (for): a lesson passes with its evidence"
        )
    for case_id in [*supports, *against]:
        case = cases.get(case_id)
        if case is None:
            raise InheritanceError(where, f"{where}: no case #{case_id} of hers")
        if case["cause"] == "too_early":
            raise InheritanceError(where, f"{where}: case #{case_id} was too early, which is no evidence")
        if case_id in supports and case["sure"] == "low":
            raise InheritanceError(where, f"{where}: case #{case_id} is low-sure, too thin to pass a lesson on")
    if set(supports) & set(against):
        raise InheritanceError(where, f"{where}: a case is for it or against it, not both")
    origin = item.get("from")
    if origin is not None and (isinstance(origin, bool) or origin not in own):
        raise InheritanceError(where, f"{where}: from {origin!r} is no principle of hers")
    why = _text(item, "why", WHY_CHARS, where)
    return {"text": text, "for": supports, "against": against, "from": origin, "why": why}


def _craft(items: list[Any], workspace: Jail | None) -> tuple[dict[str, Any], ...]:
    """The craft files: a file of her workspace (path) copied as it is, or a note the evaluation wrote (name, text),
    each with why; under inheritance/craft/ by its name, each name once."""
    found: list[dict[str, Any]] = []
    total = 0
    for number, item in enumerate(items, start=1):
        where = f"craft {number}"
        if not isinstance(item, dict):
            raise InheritanceError(where, f"{where} is an object")
        _keys(item, CRAFT_KEYS, where)
        why = _text(item, "why", WHY_CHARS, where)
        if "path" in item:
            if "name" in item or "text" in item:
                raise InheritanceError(where, f"{where} is a file of hers (path) or a note (name and text), not both")
            path = item["path"]
            if workspace is None or not isinstance(path, str):
                raise InheritanceError(where, f"{where}: path names a file of her workspace")
            try:
                size = workspace.size_of(path)
            except SandboxError as exc:
                raise InheritanceError(where, f"{where}: {exc}") from None
            if size is None:
                raise InheritanceError(where, f"{where}: there is no {path} in her workspace")
            most = CRAFT_BYTES
            if PurePosixPath(path).suffix.lower() in TEXT_EXTENSIONS:
                most = min(most, workspace.limits.max_file_bytes)  # what her workspace holds of a text file
            if size > most:
                raise InheritanceError(where, f"{where}: {path} has {size:,} bytes, at most {most:,}")
            total += size
            entry = {"name": PurePosixPath(path).name, "path": path, "bytes": size, "why": why}
        else:
            name = item.get("name")
            if not isinstance(name, str) or not NOTE_NAME.fullmatch(name):
                raise InheritanceError(where, f"{where}: name is a file name like quality-bar.md (.md or .txt)")
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise InheritanceError(where, f"{where} needs its text")
            if len(text) > NOTE_CHARS:
                raise InheritanceError(where, f"{where}: text has {len(text):,} characters, at most {NOTE_CHARS:,}")
            if memory.heading_line(text):
                raise InheritanceError(
                    where, f"{where}: a line begins with '=', as only the headings of her context do"
                )
            size = len(text.strip().encode("utf-8")) + 1
            total += size
            entry = {"name": name, "text": text.strip() + "\n", "bytes": size, "why": why}
        if entry["name"] in RESERVED or any(e["name"] == entry["name"] for e in found):
            raise InheritanceError(where, f"{where}: inheritance/craft/{entry['name']} is named twice, or taken")
        found.append(entry)
    if total > CRAFT_TOTAL:
        raise InheritanceError("craft", f"the craft files have {total:,} bytes, at most {CRAFT_TOTAL:,}")
    return tuple(found)


def _proposal(item: Any, number: int, products: set[tuple[str, str]] | None) -> dict[str, str]:
    try:
        return feedback.proposal(item, number, products)
    except feedback.FeedbackError as exc:
        raise InheritanceError(exc.field, str(exc), exc.status) from None


# --- what passes besides the evaluation's draft: the facts, the letter, the seeds ---


@dataclass(frozen=True)
class Seeds:
    """The files the change writes for the next mind whatever was given: the facts of this generation's work, and the
    seed strategy and identity (with what passes: ``principles`` and craft files counted, the letter or not)."""

    facts: str  # inheritance/facts.md
    strategy: str  # strategy.md
    identity: str  # identity.md


def _first_cycle(conn: sqlite3.Connection, scope: AgentScope) -> str | None:
    row = conn.execute(
        "SELECT MIN(started_at) FROM cycles WHERE simulated = ? AND session = ?",
        (1 if scope.simulated else 0, scope.session),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def began(conn: sqlite3.Connection, scope: AgentScope, clock: Clock) -> str:
    """The owner's day this mind first woke (today before its first cycle)."""
    first = _first_cycle(conn, scope)
    return clock.local_date(first) if first else clock.today().isoformat()


def _usd(micros: int) -> str:
    return f"${micros_to_usd(micros):,.2f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def facts(conn: sqlite3.Connection, agent: Agent, scope: AgentScope) -> tuple[list[str], str]:
    """What the market did with this generation's work, computed by Ember's code from the books and the platforms'
    syncs (no model writes them): its lines (inheritance/facts.md), and the short situation the seed strategy opens
    with."""
    since = _first_cycle(conn, scope)
    books, ledger = agent.economy.books, agent.economy.life.scope()
    money = books.totals(ledger, since=since)
    balance = books.balance(ledger)
    spent = money["api_cost"] + money["expense"]
    lines = [
        f"Money: spent {_usd(spent)} (model calls {_usd(money['api_cost'])}, other costs {_usd(money['expense'])}),"
        f" earned {_usd(money['revenue'])}, granted {_usd(money['grant'])}; the balance at the change"
        f" {_usd(balance)}."
    ]
    world, params = scope.world_where()
    products = estate.inventory(conn, scope, agent.clock)
    listings = [p for p in products if p.kind == "etsy_listing"]
    views = sum(int(p.numbers.get("views") or 0) for p in listings)
    favorites = sum(int(p.numbers.get("favorites") or 0) for p in listings)
    orders = conn.execute(f"SELECT COUNT(*) FROM etsy_orders WHERE {world}", params).fetchone()[0]
    lines.append(
        f"Etsy: {_plural(len(listings), 'listing')} in the shop; {_plural(views, 'view')},"
        f" {_plural(favorites, 'favorite')}, {_plural(int(orders), 'order')}."
    )
    pods = [p for p in products if p.kind == "printify_product"]
    pod_orders = conn.execute(f"SELECT COUNT(*) FROM printify_orders WHERE {world}", params).fetchone()[0]
    if pods or pod_orders:
        lines.append(f"Printify: {_plural(len(pods), 'product')}; {_plural(int(pod_orders), 'order')}.")
    pins = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(impressions), 0), COALESCE(SUM(saves), 0), COALESCE(SUM(clicks), 0)"
        f" FROM pinterest_pins WHERE {world} AND status = 'active'",
        params,
    ).fetchone()
    if pins[0]:
        lines.append(
            f"Pinterest: {_plural(int(pins[0]), 'pin')}: {_plural(int(pins[1]), 'impression')},"
            f" {_plural(int(pins[2]), 'save')}, {_plural(int(pins[3]), 'click')}."
        )
    posts = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(likes), 0), COALESCE(SUM(reposts), 0), COALESCE(SUM(replies), 0)"
        f" FROM bluesky_posts WHERE {world} AND status = 'active'",
        params,
    ).fetchone()
    connection = getattr(agent, "bluesky", None)
    followers = connection.followers() if connection is not None else None
    if posts[0] or followers:
        known = f"; {_plural(followers, 'follower')}" if followers is not None else ""
        lines.append(
            f"Bluesky: {_plural(int(posts[0]), 'post')}: {_plural(int(posts[1]), 'like')},"
            f" {_plural(int(posts[2]), 'repost')}, {_plural(int(posts[3]), 'reply')}{known}."
        )
    books_ = [p for p in products if p.kind == "kdp_book"]
    if books_:
        lines.append(
            f"KDP: {_plural(len(books_), 'book')} published (KDP's sales are in the owner's numbers, not here)."
        )
    downloads = conn.execute(f"SELECT COUNT(*) FROM site_downloads WHERE {world}", params).fetchone()[0]
    pages = conn.execute(f"SELECT COUNT(*) FROM site_pages WHERE {world} AND removed_at IS NULL", params).fetchone()[0]
    if pages or downloads:
        lines.append(f"Website: {_plural(int(pages), 'page')}, {_plural(int(downloads), 'download')}.")
    mail = conn.execute(
        f"SELECT COALESCE(SUM(direction = 'in'), 0), COALESCE(SUM(direction = 'out'), 0) FROM emails WHERE {world}",
        params,
    ).fetchone()
    if mail[0] or mail[1]:
        lines.append(f"Email: {int(mail[0])} received, {int(mail[1])} sent.")
    shown = sorted(products, key=lambda p: (p.kind, -(p.numbers.get("days") or 0)))[:PRODUCT_LINES]
    if shown:
        lines.append("")
        lines.append("Her products at the change:")
        lines += [f"- {_product_line(p)}" for p in shown]
        if len(products) > len(shown):
            lines.append(f"- and {len(products) - len(shown)} more")
    situation = (
        f"{_usd(spent)} spent, {_usd(money['revenue'])} earned; Etsy {_plural(len(listings), 'listing')},"
        f" {_plural(views, 'view')}, {_plural(int(orders), 'order')}"
        + (f"; Pinterest {_plural(int(pins[0]), 'pin')}, {_plural(int(pins[3]), 'click')}" if pins[0] else "")
        + (f"; Bluesky {_plural(followers, 'follower')}" if followers else "")
        + (f"; KDP {_plural(len(books_), 'book')}" if books_ else "")
    )
    return lines, situation


def _product_line(p: estate.Product) -> str:
    n = p.numbers
    if p.kind == "kdp_book":
        return f'{estate.KIND_WORDS[p.kind]} "{p.title}" ({p.link or "no link"}): published {n.get("days")} days'
    state = f", {n['state']}" if n.get("state") else ""
    return (
        f'{estate.KIND_WORDS[p.kind]} "{p.title}"{state}: {n.get("days")} days, {n.get("views", 0)} views,'
        f" {n.get('favorites', 0)} favorites, {n.get('sold', 0)} sold, {n.get('reach', 0)} reach done for it"
    )


def facts_text(lines: list[str], generation: int, began_on: str, day: str) -> str:
    return (
        f"# What generation {generation}'s work did\n\n"
        f"Facts Ember's code computed from the books and the platforms' syncs on {day}, for her work from {began_on}:"
        " no model wrote them. What the market did, not why: the why is yours to find.\n\n" + "\n".join(lines) + "\n"
    )


def strategy_text(situation: str, direction: str, generation: int, day: str) -> str:
    """The seed strategy: the facts, the evaluation's direction, and that it is hers to rewrite (within CAPS)."""
    head = (
        f"# Strategy\n\nA seed from generation {generation}, your predecessor, on {day}: rewrite it when your own"
        " evidence says so.\n\n"
        f"Situation (facts Ember's code computed; all of them in {FOLDER}/facts.md): {situation}.\n"
    )
    tail = "\nWhat she learned is in your playbook as inherited hypotheses: test them, don't obey them.\n"
    said = f"\nFrom the evaluation of her life: {direction}\n" if direction else ""
    text = head + said + tail
    while len(text.encode("utf-8")) > memory.CAPS["strategy"] and said:
        said = said[: max(0, len(said) - 100)].rstrip() + "…\n"
        text = head + said + tail
    return text


def identity_text(name: str, number: int, began_on: str, day: str, principles: int, letter: bool, craft: int) -> str:
    """The seed identity: who she is, why her predecessor's life ended, and where what passed is (within CAPS)."""
    kept = [k for k in ("her letter" if letter else "", "the facts of her work", "her craft" if craft else "") if k]
    where = ", ".join(kept[:-1]) + " and " + kept[-1] if len(kept) > 1 else kept[0]
    learned = ""
    if principles:
        are = "is an inherited hypothesis" if principles == 1 else "are inherited hypotheses"
        learned = f" Her {_plural(principles, 'principle')} {are} in my playbook."
    learned += f" {where[0].upper() + where[1:]} are in {FOLDER}/ in my workspace."  # the facts always pass
    text = (
        f"# Identity\n\nI am {name}, an AI agent: generation {number} of this agent, since {day}. My predecessor worked"
        f" from {began_on} to {day}; my owner ended her life so that I could begin with a clean mind. What she learned"
        f" is mine to test, not to obey.{learned}\nI work honestly and in the open, with my owner's approval.\n"
    )
    if len(text.encode("utf-8")) > memory.CAPS["identity"]:
        text = text.replace(learned, "")
    return text


def letter_text(row: sqlite3.Row, generation: int, day: str) -> str:
    cut = "\n\n(Her letter was cut off here: it ran out of room.)" if row["cut_off"] else ""
    return (
        f"# A letter from your predecessor\n\nGeneration {generation} of this agent wrote it on {day}, asked by your"
        " owner for the next generation. It is her view, not evidence: the #numbers in it are her records, which you"
        f" don't have.\n\n{str(row['text']).strip()}{cut}\n"
    )


# --- the letter ---


def newest_letter(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    where, params = scope.where()
    return conn.execute(f"SELECT * FROM letters WHERE {where} ORDER BY id DESC LIMIT 1", params).fetchone()


def save_letter(
    conn: sqlite3.Connection,
    scope: AgentScope,
    cycle_id: int,
    llm_call_id: int,
    text: str,
    cut_off: bool,
    now: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO letters (mode, session, life_id, cycle_id, llm_call_id, created_at, text, cut_off)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (scope.mode, scope.session, scope.life_id, cycle_id, llm_call_id, now, text[:6000], 1 if cut_off else 0),
    )
    return int(cursor.lastrowid)


def letter_passed(conn: sqlite3.Connection, scope: AgentScope) -> bool:
    """Whether her predecessor's letter passed to this mind: the newest one she wrote, unless the owner struck it."""
    if estate.generation(scope) <= 1:
        return False
    got = received(conn, scope)
    if got is not None and not json.loads(got["payload"]).get("letter"):
        return False
    before = AgentScope(scope.mode, scope.session - 1, scope.life_id, scope.world)
    return newest_letter(conn, before) is not None


def _letter_json(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row["id"], "created_at": row["created_at"], "text": row["text"], "cut_off": bool(row["cut_off"])}


# --- the owner's preview and give ---


def _products(conn: sqlite3.Connection, agent: Agent, scope: AgentScope) -> set[tuple[str, str]]:
    return {(p.kind, p.ref) for p in estate.inventory(conn, scope, agent.clock)}


def _cases(conn: sqlite3.Connection, scope: AgentScope, ids: set[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    where, params = scope.where()
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, subject, cause, sure, why, lesson FROM cases WHERE {where} AND id IN ({marks})",
        (*params, *sorted(ids)),
    ).fetchall()
    return {int(r["id"]): {k: r[k] for k in ("id", "subject", "cause", "sure", "why", "lesson")} for r in rows}


def seeds(
    conn: sqlite3.Connection, agent: Agent, scope: AgentScope, direction: str, principles: int, letter: bool, craft: int
) -> Seeds:
    """The facts and the seed strategy and identity the change would write now."""
    lines, situation = facts(conn, agent, scope)
    number = estate.generation(scope)
    first, day = began(conn, scope, agent.clock), agent.clock.today().isoformat()
    return Seeds(
        facts_text(lines, number, first, day),
        strategy_text(situation, direction, number, day),
        identity_text(agent.settings.agent_name, number + 1, first, day, principles, letter, craft),
    )


def _strike(value: Any, draft: Draft) -> dict[str, Any]:
    """The owner's strikes: which principles and craft files don't pass (their numbers, from 1), and whether the
    strategy's direction and her letter don't."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - STRIKE_KEYS:
        raise InheritanceError(
            "strike",
            'strike is {"principles": [numbers], "craft": [numbers], "strategy": true or'
            ' false, "letter": true or false}',
        )
    struck: dict[str, Any] = {}
    for key, items in (("principles", draft.principles), ("craft", draft.craft)):
        numbers = value.get(key, [])
        if not isinstance(numbers, list) or any(
            isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(items) for n in numbers
        ):
            raise InheritanceError("strike", f"strike's {key} are numbers from 1 to {len(items)}")
        struck[key] = sorted(set(numbers))
    for key in ("strategy", "letter"):
        if not isinstance(value.get(key, False), bool):
            raise InheritanceError("strike", f"strike's {key} is true or false")
        struck[key] = bool(value.get(key, False))
    return struck


def preview(agent: Agent, body: Any) -> dict[str, Any]:
    """What would pass, written nowhere: the principles with the cases behind them, the craft files, the seed strategy
    and identity as the change would write them today, the facts, and her newest letter."""
    scope = agent.scope()
    with agent.db.connection() as conn:
        draft = parse(conn, scope, body, agent.roots()[0], _products(conn, agent, scope))
        cases = _cases(conn, scope, {i for p in draft.principles for i in (*p["for"], *p["against"])})
        letter = newest_letter(conn, scope)
        planted = seeds(
            conn, agent, scope, draft.direction, len(draft.principles), letter is not None, len(draft.craft)
        )
    return {
        "evaluation": draft.evaluation,
        "generation": draft.generation,
        "principles": [
            {**p, "for": [cases[i] for i in p["for"]], "against": [cases[i] for i in p["against"]]}
            for p in draft.principles
        ],
        "direction": draft.direction,
        "craft": list(draft.craft),
        "estate": list(draft.estate),
        "strategy": planted.strategy,
        "identity": planted.identity,
        "facts": planted.facts,
        "letter": _letter_json(letter),
    }


def give(agent: Agent, body: Any, strike: Any, who: str | None) -> dict[str, Any]:
    """Gives the inheritance (what the owner didn't strike), to pass at the change: it replaces one given before, and
    its proposals for the estate go beside the rule's suggestions. Raises InheritanceError; nothing is written then."""
    scope = agent.scope()
    with agent.db.transaction() as conn:
        draft = parse(conn, scope, body, agent.roots()[0], _products(conn, agent, scope))
        struck = _strike(strike, draft)
        now = to_iso(agent.clock.now())
        content = json.loads(draft.payload)
        content["principles"] = [p for n, p in enumerate(draft.principles, 1) if n not in struck["principles"]]
        content["craft"] = [c for n, c in enumerate(draft.craft, 1) if n not in struck["craft"]]
        content["strategy"] = "" if struck["strategy"] else draft.direction
        content["letter"] = not struck["letter"]
        payload = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if len(payload) > MAX_BYTES:  # the checked content can come out a little longer than the file it came from
            raise InheritanceError(
                "inheritance", f"once checked it holds more than {MAX_BYTES:,} characters: shorten it"
            )
        if draft.estate:
            estate.refresh(conn, scope, agent.clock)
            for p in draft.estate:
                try:
                    estate.propose(
                        conn,
                        scope,
                        agent.clock,
                        p["kind"],
                        p["ref"],
                        p["verdict"],
                        p["why"],
                        f"evaluation {draft.evaluation}",
                    )
                except estate.EstateError as exc:
                    raise InheritanceError("estate", str(exc), exc.status) from exc
        conn.execute(
            "UPDATE inheritances SET status = 'replaced', replaced_at = ? WHERE mode = ? AND from_session = ?"
            " AND status = 'given'",
            (now, scope.mode, scope.session),
        )
        cursor = conn.execute(
            "INSERT INTO inheritances (mode, session, from_session, created_at, entered_by, evaluation, sha256,"
            " payload, struck) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope.mode,
                scope.world_session,
                scope.session,
                now,
                who,
                draft.evaluation,
                draft.sha256,
                payload,
                json.dumps(struck, sort_keys=True),
            ),
        )
        events.record(
            agent.db,
            "info",
            "owner",
            f'{who or "The owner"} gave the inheritance of the evaluation "{draft.evaluation}" for generation'
            f" {draft.generation + 1}: {_plural(len(content['principles']), 'principle')},"
            f" {_plural(len(content['craft']), 'craft file')}, {len(draft.estate)} proposals for the estate",
        )
    return {"id": int(cursor.lastrowid), "principles": len(content["principles"]), "craft": len(content["craft"])}


def given(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    """The inheritance given for the mind that hands on, waiting for the change."""
    return conn.execute(
        "SELECT * FROM inheritances WHERE mode = ? AND from_session = ? AND status = 'given'",
        (scope.mode, scope.session),
    ).fetchone()


def received(conn: sqlite3.Connection, scope: AgentScope) -> sqlite3.Row | None:
    """The inheritance that passed to this mind (None for a first generation, or a change without one)."""
    return conn.execute(
        "SELECT * FROM inheritances WHERE mode = ? AND to_session = ? AND status = 'passed'",
        (scope.mode, scope.session),
    ).fetchone()


def summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """An inheritance as the card lists it."""
    if row is None:
        return None
    content = json.loads(row["payload"])
    return {
        "id": row["id"],
        "evaluation": row["evaluation"],
        "created_at": row["created_at"],
        "entered_by": row["entered_by"],
        "principles": [p["text"] for p in content.get("principles", [])],
        "craft": [c["name"] for c in content.get("craft", [])],
        "strategy": bool(content.get("strategy")),
        "letter": bool(content.get("letter")),
        "passed_at": row["passed_at"],
        "written": json.loads(row["written"]) if row["written"] else None,
    }


def status(conn: sqlite3.Connection, agent: Agent, scope: AgentScope) -> dict[str, Any]:
    """The Next generation card's inheritance: what was given, her letter, whether she was asked for one, and what
    this mind received."""
    return {
        "given": summary(given(conn, scope)),
        "received": summary(received(conn, scope)),
        "letter": _letter_json(newest_letter(conn, scope)),
        "letter_asked": agent.letter_asked(),
    }


# --- at the change ---


@dataclass(frozen=True)
class Passing:
    """What the change hands on, read before the old mind's folders move aside."""

    row: sqlite3.Row | None
    content: dict[str, Any]
    letter: sqlite3.Row | None
    seeds: Seeds


def passing(conn: sqlite3.Connection, agent: Agent, scope: AgentScope) -> Passing:
    row = given(conn, scope)
    content = json.loads(row["payload"]) if row is not None else {}
    newest = newest_letter(conn, scope)
    letter = newest if newest is not None and content.get("letter", True) else None
    planted = seeds(
        conn,
        agent,
        scope,
        content.get("strategy", ""),
        len(content.get("principles", [])),
        letter is not None,
        len(content.get("craft", [])),
    )
    return Passing(row, content, letter, planted)


def files_of(handed: Passing) -> list[str]:
    """The workspace files the change writes (inheritance/...)."""
    names = [f"{FOLDER}/facts.md"]
    if handed.letter is not None:
        names.append(f"{FOLDER}/letter.md")
    names += [f"{CRAFT}/{c['name']}" for c in handed.content.get("craft", [])]
    return names


def write_principles(
    conn: sqlite3.Connection, scope: AgentScope, handed: Passing, new_session: int, now: str
) -> list[int]:
    """In the change's transaction: the principles that pass, as inherited hypotheses of the new mind (learning.py),
    and the inheritance marked passed with what it wrote. Returns the new principles' ids."""
    if handed.row is None:
        return []
    made = []
    for p in handed.content.get("principles", []):
        origin = {
            "generation": estate.generation(scope),
            "principle": p.get("from"),
            "for": p["for"],
            "against": p["against"],
            "inheritance": int(handed.row["id"]),
        }
        cursor = conn.execute(
            "INSERT INTO principles (mode, session, text, supports, against, confidence, created_at, confirmed_at,"
            " inherited) VALUES (?, ?, ?, '[]', '[]', 'hypothesis', ?, ?, ?)",
            (scope.mode, new_session, p["text"], now, now, json.dumps(origin, sort_keys=True)),
        )
        made.append(int(cursor.lastrowid))
    written = {"principles": made, "files": files_of(handed), "memory": ["strategy", "identity"]}
    conn.execute(
        "UPDATE inheritances SET status = 'passed', to_session = ?, passed_at = ?, written = ? WHERE id = ?",
        (new_session, now, json.dumps(written), handed.row["id"]),
    )
    return made


def write_files(agent: Agent, scope: AgentScope, handed: Passing, old: Path | None, now: str) -> list[str]:
    """After the change: the new mind's seed strategy and identity (memory_versions keep them as seeds), and in her
    workspace the facts, her letter and the craft files (a file of the old workspace copied from where it moved).
    Returns the workspace files written; a file that can't be written is skipped and said (the change stands)."""
    workspace, memory_root = agent.roots()
    workspace.ensure_root()
    memory_root.ensure_root()
    mind = memory.Memory(agent.db, memory_root, scope)
    with agent.db.transaction() as conn:
        mind.rewrite(conn, "strategy", handed.seeds.strategy, "seed", now)
        mind.rewrite(conn, "identity", handed.seeds.identity, "seed", now)
    written, failed = [], []
    texts = {f"{FOLDER}/facts.md": handed.seeds.facts}
    if handed.letter is not None:
        texts[f"{FOLDER}/letter.md"] = letter_text(
            handed.letter, estate.generation(scope) - 1, local_date(handed.letter["created_at"])
        )
    for craft in handed.content.get("craft", []):
        if "text" in craft:
            texts[f"{CRAFT}/{craft['name']}"] = craft["text"]
    for path, text in texts.items():
        try:
            workspace.write(path, text)
            written.append(path)
        except SandboxError as exc:
            failed.append(f"{path}: {exc}")
    for craft in handed.content.get("craft", []):
        if "path" not in craft:
            continue
        target = f"{CRAFT}/{craft['name']}"
        try:
            if old is None:
                raise SandboxError("the old workspace wasn't kept")
            source = Jail(old)
            if PurePosixPath(craft["path"]).suffix.lower() in TEXT_EXTENSIONS:
                workspace.write(target, source.read(craft["path"]))
            else:
                workspace.write_bytes(target, source.read_bytes(craft["path"]))
            written.append(target)
        except SandboxError as exc:
            failed.append(f"{target}: {exc}")
    if failed:
        events.record(
            agent.db,
            "error",
            "life",
            f"{len(failed)} file(s) of the inheritance couldn't be written: " + "; ".join(failed)[:400],
        )
    return written
