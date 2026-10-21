"""The publishing queue (0.37.8): when each approved pin and Bluesky post goes out, and which one goes next.

Until 0.37.8 Ember's code made every approved pin and post at its next round, so a batch of the owner's approvals went
out together: on 10-08 two pins of one listing 5 seconds apart and a third 57 minutes later, on 10-10 a Bluesky post
and two pins within 15 seconds, then two identical pins 2 seconds apart. Scheduling tools agree that Pinterest's spam
filter reads bursts, and one link pinned again and again, as spam (and gives repeated pins less reach); on Bluesky a
follower sees an account's posts one after another, so a second one buries the first. The owner: "there should be
some kind of logic in when and how and how often to post". Now an approval puts a request in its channel's queue, and
Ember's code gives each one a slot (``schedule``):

* when: in the channel's hours, the owner's time (HOURS): pins from 08:00 to 22:00, an English Bluesky post from 14:00
  to 23:00 (Europe's afternoon and evening, America's day), a German one from 08:00 to 22:00;
* how often: GAP after the channel's last pin or post, and a few minutes more (JITTER_MINUTES, the same for a request
  every time its slot is worked out, so the slot holds), and no more in a day than the owner's daily limit;
* what next: the oldest approval that may go, but a listing (or a page) at most once in LINK_DAYS on a channel, so a
  second pin of one listing waits while the next listing's pin goes.

The publishers make only what is due, one at a time (``due``); the Approvals tab, the agent's news, its tools and its
plan say when each one goes out, and the agent proposes no more for a channel while WAITING of its requests there wait
for the owner or for their slot (``waiting``). 0.41.0 (M3): a request whose service asked to wait (retry.py) is in the
queue again, its slot no earlier than that.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..agent.store import AgentScope
from ..economy.clock import Clock, from_iso, to_iso
from . import bluesky, pinterest, retry

CHANNELS = ("pinterest", "bluesky")
EXECUTORS = {"pinterest": "pinterest_pin", "bluesky": "bluesky_post"}
MADE_IN = {"pinterest": "pinterest_pins", "bluesky": "bluesky_posts"}  # the rows of what Ember's code made
# A channel's hours, the owner's time, by the language of what goes out ("" for any other, and for a pin)
HOURS: dict[str, dict[str, tuple[int, int]]] = {
    "pinterest": {"": (8, 22)},
    "bluesky": {"en": (14, 23), "": (8, 22)},
}
GAP = timedelta(hours=3)  # between two pins, and between two posts
JITTER_MINUTES = 30  # a request's slot comes up to this many minutes after its bound (never at the same minute)
LINK_DAYS = 2  # a listing or a page at most once in this many days on a channel
WAITING = 3  # a channel's requests waiting for the owner or for their slot: the agent proposes no more there (0.39.6:
# approved ones of one listing count once)
DAYS_AHEAD = 14  # how far ahead a slot is looked for
APPROVED = ("approved", "approved_with_changes")
THINGS = {"pinterest": ("pin", "pins"), "bluesky": ("post", "posts")}
SERVICES = {"pinterest": "Pinterest", "bluesky": "Bluesky"}
_LISTING = re.compile(r"etsy\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?listing/(\d{1,18})", re.IGNORECASE)


@dataclass(frozen=True)
class Item:
    """An approved request waiting for its slot."""

    approval_id: int
    links: tuple[str, ...]  # the listings and pages it links (``keys``)
    language: str  # a post's ("de" or "en"); "" for a pin
    approved_at: datetime
    retry_at: datetime | None = None  # 0.41.0 (M3): its service asked to wait until then


@dataclass(frozen=True)
class Made:
    """A pin or post Ember's code made (or began: it counts for the daily limit as the publishers count it)."""

    at: datetime
    links: tuple[str, ...]


@dataclass(frozen=True)
class Slot:
    approval_id: int
    at: datetime  # UTC: it goes out at the first round from then on
    why: str  # what set it: 'now', 'gap', 'link', 'hours', 'limit' or (0.41.0) 'retry'
    language: str = ""  # a post's, whose hours it has


def keys(links: Iterable[str | None]) -> tuple[str, ...]:
    """What a pin or post links, as the queue compares it: a listing by its number (whatever the address's form), a
    page by its address without its query, fragment, ".html" or last slash."""
    found = []
    for link in links:
        if not link:
            continue
        listing = _LISTING.search(link)
        if listing:
            found.append(f"listing {listing.group(1)}")
        else:
            page = link.split("#", 1)[0].split("?", 1)[0].strip().lower().rstrip("/")
            found.append(page.removesuffix(".html"))
    return tuple(dict.fromkeys(found))


def hours(channel: str, language: str) -> tuple[int, int]:
    table = HOURS[channel]
    return table.get(language, table[""])


def jitter(approval_id: int) -> timedelta:
    """A request's few minutes after its bound: the same every time (its slot holds from round to round)."""
    digest = hashlib.sha256(f"slot {approval_id}".encode()).digest()
    return timedelta(minutes=int.from_bytes(digest[:4], "big") % JITTER_MINUTES)


# --- the schedule: pure, from what waits and what was made ---


def _count(taken: Sequence[tuple[datetime, tuple[str, ...]]], day: date, clock: Clock) -> int:
    return sum(1 for at, _ in taken if at.astimezone(clock.tz).date() == day)


def _earliest(
    item: Item,
    taken: Sequence[tuple[datetime, tuple[str, ...]]],
    now: datetime,
    clock: Clock,
    channel: str,
    per_day: int,
    shifted: bool,
) -> tuple[datetime, str]:
    """The first moment ``item`` may go out after what is made or has its slot (``taken``), and what set it.
    ``shifted``: with the request's own minutes after each bound (``jitter``); without, the queue picks the next."""
    shift = jitter(item.approval_id) if shifted else timedelta(0)
    bound, why = None, "gap"
    last = max((at for at, _ in taken), default=None)
    if last is not None:
        bound = last + GAP
    for at, links in taken:
        if item.links and set(links) & set(item.links):
            again = at + timedelta(days=LINK_DAYS)
            if bound is None or again > bound:
                bound, why = again, "link"
    moment = now
    if bound is not None and bound + shift > now:
        moment = bound + shift
    else:
        why = "now"
    if item.retry_at is not None and item.retry_at > moment:  # 0.41.0 (M3): not before its service's wait ends
        moment, why = item.retry_at, "retry"
    first, end = hours(channel, item.language)
    for _ in range(DAYS_AHEAD):
        day = moment.astimezone(clock.tz).date()
        opens, closes = clock.at(day, first) + shift, clock.at(day, end)
        full = _count(taken, day, clock) >= per_day
        if not full and moment <= closes:
            # the hours set it, unless a listing's last pin or post (or the limit) set the day it opens on
            return (opens, why if why in ("link", "limit") else "hours") if moment < opens else (moment, why)
        why = "limit" if full else why if why == "link" else "hours"
        moment = clock.at(day + timedelta(days=1), first) + shift
    return moment, why


def schedule(
    items: Iterable[Item], made: Iterable[Made], now: datetime, clock: Clock, channel: str, per_day: int
) -> list[Slot]:
    """Each waiting request's slot, the first first: the request that may go out soonest goes next (the oldest approval
    when several may), then the others after it, each GAP and its own minutes later, in the channel's hours, a listing
    or page at most once in LINK_DAYS, at most ``per_day`` a day (counted with what was made that day)."""
    taken: list[tuple[datetime, tuple[str, ...]]] = [(m.at, m.links) for m in made]
    waiting = sorted(items, key=lambda i: (i.approved_at, i.approval_id))
    slots = []
    while waiting:
        chosen = min(waiting, key=lambda i: _earliest(i, taken, now, clock, channel, per_day, False)[0])
        at, why = _earliest(chosen, taken, now, clock, channel, per_day, True)
        slots.append(Slot(chosen.approval_id, at, why, chosen.language))
        taken.append((at, chosen.links))
        waiting.remove(chosen)
    return slots


# --- reading them ---


def _parsed(channel: str, raw: str | None) -> tuple[tuple[str, ...], str]:
    """What an approved request links and its language ("" for a pin); nothing for one that can't be read (it gets a
    slot all the same, and its publisher says what is wrong with it)."""
    try:
        if channel == "pinterest":
            return keys([pinterest.pin_from_action(raw or "").link]), ""
        post = bluesky.post_from_action(raw or "")
        return keys([post.link, post.second_link]), post.language
    except (pinterest.PinterestError, bluesky.BlueskyError, ValueError, TypeError, KeyError):
        return (), ""


def _not_begun(alias: str, channel: str) -> str:
    """SQL: Ember's code hasn't begun request ``alias``, or (0.41.0, M3) its service asked to wait, nothing made."""
    return (
        f"((NOT EXISTS (SELECT 1 FROM action_journal j WHERE j.approval_id = {alias}.id)"
        f" AND NOT EXISTS (SELECT 1 FROM {MADE_IN[channel]} p WHERE p.approval_id = {alias}.id))"
        f" OR {retry.waiting_sql(alias)})"
    )


def queued(conn: sqlite3.Connection, scope: AgentScope, channel: str) -> list[Item]:
    """A channel's approved requests that Ember's code hasn't begun to carry out (0.41.0: or whose service asked to
    wait, with when)."""
    where, params = scope.where("a")
    rows = conn.execute(
        f"SELECT a.id, a.action, a.decided_at, a.created_at, {retry.retry_at_sql('a')} AS retry_at FROM approvals a"
        f" WHERE {where} AND a.executor = ? AND a.status IN {APPROVED} AND {_not_begun('a', channel)} ORDER BY a.id",
        (*params, EXECUTORS[channel]),
    ).fetchall()
    found = []
    for r in rows:
        links, language = _parsed(channel, r["action"])
        approved = from_iso(str(r["decided_at"] or r["created_at"]))
        found.append(Item(int(r["id"]), links, language, approved, from_iso(r["retry_at"]) if r["retry_at"] else None))
    return found


def made(conn: sqlite3.Connection, scope: AgentScope, channel: str, since: datetime) -> list[Made]:
    """What Ember's code made on a channel since ``since``, as the daily limits count it (a pin no request reached
    Pinterest for, and a post never sent, count for nothing)."""
    where, params = scope.where()
    if channel == "pinterest":
        rows = conn.execute(
            f"SELECT started_at, link, NULL AS second_link FROM pinterest_pins WHERE {where} AND started_at >= ?"
            " AND NOT (status = 'failed' AND board_id IS NULL)",
            (*params, to_iso(since)),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT started_at, link, second_link FROM bluesky_posts WHERE {where} AND started_at >= ? AND sent = 1",
            (*params, to_iso(since)),
        ).fetchall()
    return [Made(from_iso(str(r["started_at"])), keys([r["link"], r["second_link"]])) for r in rows]


def _history(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, channel: str) -> list[Made]:
    # a link's last LINK_DAYS and today's count, whatever the time zone's day is long
    return made(conn, scope, channel, clock.now() - timedelta(days=LINK_DAYS + 2))


def slots(
    conn: sqlite3.Connection,
    scope: AgentScope,
    clock: Clock,
    channel: str,
    per_day: int,
    extra: Item | None = None,
) -> list[Slot]:
    """A channel's queue now, each request with its slot (``extra``: a request as if approved now)."""
    waiting = queued(conn, scope, channel) + ([extra] if extra is not None else [])
    return schedule(waiting, _history(conn, scope, clock, channel), clock.now(), clock, channel, per_day)


def slot_of(
    conn: sqlite3.Connection, scope: AgentScope, clock: Clock, channel: str, per_day: int, approval_id: int
) -> Slot | None:
    return next((s for s in slots(conn, scope, clock, channel, per_day) if s.approval_id == approval_id), None)


def due(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, channel: str, per_day: int) -> list[int]:
    """The request whose slot has come, if any: one a round, so two never go out together."""
    now = clock.now()
    return [s.approval_id for s in slots(conn, scope, clock, channel, per_day)[:1] if s.at <= now]


def if_approved_now(
    conn: sqlite3.Connection, scope: AgentScope, clock: Clock, channel: str, per_day: int, approval_id: int
) -> Slot | None:
    """The slot a request waiting for the owner would get if they approved it now (after the queue as it is)."""
    row = conn.execute("SELECT action FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        return None
    links, language = _parsed(channel, row["action"])
    extra = Item(approval_id, links, language, clock.now())
    found = slots(conn, scope, clock, channel, per_day, extra)
    return next((s for s in found if s.approval_id == approval_id), None)


def waiting(conn: sqlite3.Connection, scope: AgentScope, channel: str) -> list[int]:
    """A channel's requests that wait for the owner's decision or, approved, for their slot. 0.39.6: approved ones of
    one listing or page count once (the oldest): LINK_DAYS holds each of them back, not the others' listings. Live on
    10-15 three approved pins of the Haushaltsbuch listing had their slots on 10-16, 10-18 and 10-20, and as WAITING
    they kept every other listing's pin out of the queue until 10-20."""
    where, params = scope.where("a")
    rows = conn.execute(
        f"SELECT a.id, a.status, a.action FROM approvals a WHERE {where} AND a.executor = ? AND (a.status = 'pending'"
        f" OR (a.status IN {APPROVED} AND {_not_begun('a', channel)})) ORDER BY a.id",
        (*params, EXECUTORS[channel]),
    ).fetchall()
    found, linked = [], set()
    for r in rows:
        if r["status"] != "pending":
            links = set(_parsed(channel, r["action"])[0])
            if links & linked:
                continue
            linked |= links
        found.append(int(r["id"]))
    return found


def slotted(conn: sqlite3.Connection, scope: AgentScope, channel: str) -> dict[str, int]:
    """0.39.6: what a channel's approved requests waiting for their slot link (as ``keys`` says), each with the oldest
    request that links it: a listing with one gets no other request on the channel until it is out (its pins count
    once toward WAITING, ``waiting``, and LINK_DAYS would only push another days back)."""
    found: dict[str, int] = {}
    for item in queued(conn, scope, channel):
        for link in item.links:
            found.setdefault(link, item.approval_id)
    return found


# --- in words ---


def rules(channel: str, per_day: int) -> str:
    """The queue's rules for a channel, for the tools, the plan and the guides."""
    many = THINGS[channel][1]
    if channel == "bluesky":
        when = "an English one from {}:00 to {}:00, a German one from {}:00 to {}:00 your owner's time".format(
            *hours(channel, "en"), *hours(channel, "de")
        )
    else:
        when = "from {}:00 to {}:00 your owner's time".format(*hours(channel, ""))
    return (
        f"approved {many} go out one at a time, in their slots: {when}, at least {GAP.seconds // 3600} hours apart,"
        f" a listing or page at most once in {LINK_DAYS} days, at most {per_day} a day"
    )


def why(channel: str, cause: str, per_day: int, language: str = "") -> str:
    """What set a slot, in words."""
    one, many = THINGS[channel]
    first, end = hours(channel, language)
    return {
        "now": "it goes out at the next round",
        "gap": f"{many} go out at least {GAP.seconds // 3600} hours apart",
        "link": f"a listing or page gets at most one {one} in {LINK_DAYS} days",
        "hours": f"{many} go out from {first}:00 to {end}:00",
        "limit": f"at most {per_day} {many} a day",
        "retry": f"{SERVICES[channel]} asked to wait",  # 0.41.0 (M3)
    }.get(cause, "")


def scheduled(slot: Slot, channel: str, per_day: int) -> dict[str, Any]:
    """An approved request while it waits for its slot, for the Approvals tab: when it goes out and why then (one the
    daily limit holds waits for it, as before the queue)."""
    return {
        "status": "waiting_limit" if slot.why == "limit" else "scheduled",
        "due_at": to_iso(slot.at),
        "why": slot.why,
        "started_at": None,
        "finished_at": None,
        "result": why(channel, slot.why, per_day, slot.language),
        "error": None,
        "url": None,
    }


def when(clock: Clock, at: datetime) -> str:
    """A slot in the owner's time: "today at 16:10", "tomorrow at 08:17", "Tue 10-14 at 08:17" (now: "now")."""
    now = clock.now()
    if at <= now:
        return "now"
    local, today = at.astimezone(clock.tz), clock.today()
    clock_time = local.strftime("%H:%M")
    if local.date() == today:
        return f"today at {clock_time}"
    if local.date() == today + timedelta(days=1):
        return f"tomorrow at {clock_time}"
    return f"{local.strftime('%a %m-%d')} at {clock_time}"


def slot_words(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, per_day: dict[str, int]) -> dict[int, str]:
    """Each approved pin's and post's slot in words, by its request (the agent's news of an approval): "today at 16:10
    (pins go out at least 3 hours apart)", or "at the next round"."""
    found = {}
    for channel in CHANNELS:
        for s in slots(conn, scope, clock, channel, per_day[channel]):
            at = when(clock, s.at)
            cause = why(channel, s.why, per_day[channel], s.language)
            found[s.approval_id] = "at the next round" if at == "now" else f"{at} ({cause})"
    return found


def text(conn: sqlite3.Connection, scope: AgentScope, clock: Clock, channel: str, per_day: int) -> str:
    """The channel's queue for the plan and the tools: each approved request with its slot ("" when none waits)."""
    found = slots(conn, scope, clock, channel, per_day)
    if not found:
        return ""
    titles = {
        int(r["id"]): str(r["title"])
        for r in conn.execute(
            f"SELECT id, title FROM approvals WHERE id IN ({', '.join('?' for _ in found)})",
            tuple(s.approval_id for s in found),
        )
    }
    lines = [f"Queue ({rules(channel, per_day)}):"]
    for s in found:
        cause = f" ({why(channel, s.why, per_day, s.language)})" if s.why not in ("now", "gap") else ""
        lines.append(f"- request #{s.approval_id} {titles.get(s.approval_id, '')[:70]}: {when(clock, s.at)}{cause}")
    return "\n".join(lines)
