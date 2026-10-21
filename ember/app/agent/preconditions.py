"""One precondition per tool, read by the plan too (0.42.0, analysis-0.40.1 codebase, 5.2 and 6.2.1).

A tool that refused for a reason the plan could know before a cycle (a count, a limit, a date, a missing note) refused
by a rule written twice: once as the tool's refusal in tools.py, once as the plan's wait in plan.py, or not at all
there. Where the plan didn't know the rule, a step the tool would refuse looked ready, cut Ember's sleep and paid for a
cycle that ended in the refusal. Each pair was mended on its own: 0.37.6 the requests waiting for the owner, 0.37.8
the channels' queues, 0.37.9 the publish requests and the day's messages, 0.38.1 the sell requests, 0.39.1 a channel's
line, 0.40.2 KDP's week, 0.40.3 the unread messages and a stale demand note.

Now each such rule is written once, here, as a ``Rule``: its check returns None (the call may go on) or the refusal's
text. The tool raises its ToolError with that text where it checked the rule before (tools._refuse), and ``TOOLS``
names each tool's rules in the order it checks them; the plan asks ``refusal`` for the tool a step's work needs
(plan._calls), and the step waits on the rule's ``wait`` (plan.WAITS) with the same words, and cuts no sleep.

What a rule reads is ``Facts``: one connection's scope and clock, and what the call is about (its product line, its
channel, what it links, a book's format, whether a message answers the owner). The tool knows them from its input,
the plan from the step.

Not here: what only the call's input shows (a text too long, a picture, a listing that isn't live), and the platforms'
daily limits (etsy_listings_per_day, pinterest_pins_per_day and the like), by which no tool refuses: an approved
request waits for the next day in Ember's code (integrations/*_publisher.py, executor.py), and proposing it today
costs no refusal. The workshop's runs a day are the workshop's own refusal, and no step's tool is the workshop.
tests/test_preconditions.py fails on a refusal in tools.py that reads one of these limits itself.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from functools import partial

from ..economy.clock import owner_timezone, to_iso
from ..integrations import kdp, publish_queue
from . import demand, obligations, paid, store
from .store import AgentScope


@dataclass(frozen=True)
class Facts:
    """What a precondition reads, from one connection: the scope, the clock and what the call is about."""

    conn: sqlite3.Connection
    scope: AgentScope
    now: datetime
    day_start: str  # when the owner's day began (their clock's day_start): the day's messages count from then
    tz: tzinfo = field(default_factory=owner_timezone)  # the owner's: when KDP's next week begins, in their time
    line: int | None = None  # the product line a request is for
    channel: str | None = None  # a queue channel's request (publish_queue.CHANNELS)
    links: tuple[str, ...] = ()  # what it links (publish_queue.keys: 'listing 123')
    replaced: tuple[int, ...] = ()  # the waiting requests it replaces (a pin of a listing in the same words)
    form: str | None = None  # a KDP book's format; None: not known (the week holds it while every format's is full)
    # a message that answers one of the owner's, or reports a promise they heard nothing of since (``untold``): the
    # day's limit lets it through
    answers: bool = False
    listing: int | None = None  # 0.42.1: the listing a change is for (a paid test's leaves it as it is, ``_paid``)


@dataclass(frozen=True)
class Rule:
    """A precondition: ``check`` returns its refusal's text, or None; ``wait`` is what a step of a tool that refuses
    by it waits on (plan.WAITS); ``name`` names it (tests/test_preconditions.py reads tools.py by it)."""

    name: str
    wait: str
    check: Callable[[Facts], str | None]


def _numbers(ids: list[int]) -> str:
    return ", ".join(f"#{i}" for i in ids)


def _room(kind: str, facts: Facts) -> str | None:
    """0.12.0: a cap per type of request waiting for the owner (store.PENDING_CAPS; one cap of 10 for all let waiting
    listings block an email reply)."""
    cap = store.PENDING_CAPS[kind]
    if store.pending_of_type(facts.conn, facts.scope, kind) < cap:
        return None
    return (
        f"{cap} {kind} requests are already waiting for your owner: withdraw one that is outdated (withdraw_request),"
        " or wait for their decision"
    )


def _slot(facts: Facts) -> str | None:
    """0.39.6: a request for a listing or page (``links``) that has an approved one waiting for its slot on the
    channel: it would only go out publish_queue.LINK_DAYS after that one."""
    if facts.channel not in publish_queue.CHANNELS:
        return None
    held = publish_queue.slotted(facts.conn, facts.scope, facts.channel)
    taken = sorted({held[k] for k in facts.links if k in held and held[k] not in facts.replaced})
    if not taken:
        return None
    one, many = publish_queue.THINGS[facts.channel]
    return (
        f"your approved {one} (request {_numbers(taken)}) for that listing or page waits for its slot, and a listing"
        f" gets one {one} in {publish_queue.LINK_DAYS} days: make the next {one} for a listing with none waiting"
        f" (approved {many} of one listing count once toward the {publish_queue.WAITING} that may wait)"
    )


def _queue(facts: Facts) -> str | None:
    """0.37.8: a channel takes no more of the agent's requests while publish_queue.WAITING of them wait for the owner
    or for their slot (what a day of it holds): a long queue would publish days later what was planned for now."""
    if facts.channel not in publish_queue.CHANNELS:
        return None
    found = [n for n in publish_queue.waiting(facts.conn, facts.scope, facts.channel) if n not in facts.replaced]
    if len(found) < publish_queue.WAITING:
        return None
    one, many = publish_queue.THINGS[facts.channel]
    return (
        f"{len(found)} {many} of yours already wait for your owner or for their slot (request {_numbers(found)}):"
        f" propose the next {one} once one of them is out"
    )


def _unread(facts: Facts) -> str | None:
    """0.40.3 (analysis-0.40.1 codebase, PL3): no message while the owner hasn't read store.MAX_UNREAD_MESSAGES of
    Ember's, a report or an answer neither."""
    if store.unread_messages(facts.conn, facts.scope) < store.MAX_UNREAD_MESSAGES:
        return None
    return f"your owner hasn't read your last {store.MAX_UNREAD_MESSAGES} messages yet"


def _told(facts: Facts) -> str | None:
    """0.12.0: at most store.MESSAGES_PER_DAY messages of the owner's day that answer none of theirs (the prompt's
    "once a day" was prose); one that answers them, or reports a promise (``answers``), goes through."""
    if facts.answers or store.unasked_since(facts.conn, facts.scope, facts.day_start) < store.MESSAGES_PER_DAY:
        return None
    return (
        f"you sent your owner {store.MESSAGES_PER_DAY} messages today that answer none of theirs: batch the rest into"
        " tomorrow's, or into your answer when they write"
    )


def _demand(facts: Facts) -> str | None:
    """0.12.0: a product line's first listing needs a demand note from the last demand.DAYS days (0.40.3:
    demand.needs_note)."""
    if facts.line is None or not demand.needs_note(facts.conn, facts.line, to_iso(facts.now)):
        return None
    return (
        f"project #{facts.line} has no listing yet, and a new product line needs a demand note from the last"
        f" {demand.DAYS} days first (demand_note: the keywords buyers search and what shows they buy)"
    )


def _kdp_week(facts: Facts) -> str | None:
    """0.25.0: KDP lets an account create at most kdp.WEEKLY_TITLES new titles of a format a week (0.40.2: KDP's
    week, from Sunday 00:00 UTC). A book of no known format (``form`` None) is held while every format's is full."""
    counted = kdp.weekly(facts.conn, facts.scope, facts.now)
    forms = (facts.form,) if facts.form else kdp.FORMATS
    full = [f for f in forms if counted.get(f, 0) >= kdp.WEEKLY_TITLES]
    if not full or len(full) < len(forms):
        return None
    form = full[0]
    return (
        f"you proposed {counted[form]} {form}s this week (KDP's, from Sunday 00:00 UTC), and KDP lets an account"
        f" create at most {kdp.WEEKLY_TITLES} new {form}s a week: propose the next one from"
        f" {kdp.next_week(facts.now, facts.tz)}"
    )


def _paid(facts: Facts) -> str | None:
    """0.42.1 (analysis-0.40.1 codebase, 6.2 "A paid test in the plan"): no change of a listing in the owner's paid test
    while it runs (paid.running): what the ads bring is judged on the listing as it was. STATUS asked Ember to leave
    it alone and no tool refused a change, so the plan made the critic's fixes for it ready: live, a cycle spent one of
    Ember's two waits a day to keep the coach bundle as it was. Renewing or deactivating it isn't a change."""
    if facts.listing is None:
        return None
    test = paid.running(facts.conn, facts.scope, facts.now.astimezone(facts.tz).date()).get(facts.listing)
    if test is None:
        return None
    ends = date.fromisoformat(test["ends_on"])
    return (
        f"listing #{facts.listing} is in your owner's paid test (request #{test['approval_id']}): leave it as it is"
        f" until the test ends on {ends:%m-%d}, then judge it and change it"
    )


def untold(conn: sqlite3.Connection, scope: AgentScope, obligation_id: int) -> bool:
    """0.15.0: whether ``obligation_id`` is an open promise the owner heard nothing of since the message that made it:
    a message reporting on it goes through the day's limit (``answers``), so obligation_done can close it (live: the
    limit refused the very message obligation_done asked for). message_owner reads it of the promises a message names,
    the plan of a step keeping one."""
    where, params = scope.where()
    row = conn.execute(
        f"SELECT message_id FROM obligations WHERE id = ? AND {where} AND kind = 'promise' AND status = 'open'",
        (obligation_id, *params),
    ).fetchone()
    return row is not None and not obligations.told_since(conn, scope, row["message_id"])


# the plan's waits for a request type's cap: the publishing queue's (0.37.9) and the sell requests' (0.38.1)
_ROOM_WAITS = {"publish": "queue", "sell": "sales"}
ROOM = {
    kind: Rule(f"ROOM[{kind}]", _ROOM_WAITS.get(kind, "blocked"), partial(_room, kind)) for kind in store.PENDING_CAPS
}
PUBLISH, SELL = ROOM["publish"], ROOM["sell"]
SLOT = Rule("SLOT", "queue", _slot)
QUEUE = Rule("QUEUE", "queue", _queue)
UNREAD = Rule("UNREAD", "unread", _unread)
MESSAGES = Rule("MESSAGES", "messages", _told)
DEMAND = Rule("DEMAND", "blocked", _demand)
KDP_WEEK = Rule("KDP_WEEK", "blocked", _kdp_week)
PAID = Rule("PAID", "paid", _paid)  # 0.42.1

# Each tool's preconditions, in the order it checks them (a request type's cap last: tools._new_request checks it)
TOOLS: dict[str, tuple[Rule, ...]] = {
    "propose_pin": (SLOT, QUEUE, PUBLISH),
    "propose_bluesky_post": (SLOT, QUEUE, PUBLISH),
    "propose_blog_post": (PUBLISH,),
    "propose_link_page": (PUBLISH,),
    "propose_reddit_post": (PUBLISH,),
    "propose_etsy_listing": (DEMAND, SELL),
    "propose_printify_product": (DEMAND, SELL),
    "propose_etsy_edit": (PAID, SELL),
    "propose_printify_edit": (PAID, SELL),
    "propose_kdp_book": (KDP_WEEK, SELL),
    "propose_email": (ROOM["contact"],),
    "message_owner": (UNREAD, MESSAGES),
}


def refusal(name: str, facts: Facts) -> tuple[Rule, str] | None:
    """What tool ``name`` would refuse now by a rule the plan can know, with the rule; None while none refuses (the call
    may still be refused for its input)."""
    for rule in TOOLS.get(name, ()):
        said = rule.check(facts)
        if said:
            return rule, said
    return None


def blocked(name: str, facts: Facts) -> str | None:
    """The words of tool ``name``'s refusal by a rule the plan can know (``refusal``), None for none."""
    found = refusal(name, facts)
    return found[1] if found else None
