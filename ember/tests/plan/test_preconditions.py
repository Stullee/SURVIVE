"""One precondition per tool, read by the plan too (0.42.0, analysis-0.40.1 codebase, 5.2 and 6.2.1).

A tool that refuses by a rule the plan can know before a cycle (a count, a limit, a date, a missing note) refuses by
a precondition of preconditions.py, and a step of that tool waits on it with the tool's words, cutting no sleep. Until
0.42.0 each rule was written twice, or only in the tool: 0.37.6, 0.37.8, 0.37.9, 0.38.1, 0.39.1, 0.40.2 and 0.40.3
each mended one pair after a step the tool refused had cut Ember's sleep and paid for a cycle that ended in the
refusal.

The class: no refusal in tools.py reads such a limit itself, each tool checks exactly the rules preconditions.TOOLS
names for it, the plan knows the tool of every request a step's check counts, and a step of each tool waits on each of
its rules with the rule's words. The cases: a pin while 3 publish requests wait, a KDP proposal in a full KDP week, a
report at the unread cap (the rule message_owner checks first), a first listing without a fresh demand note and at the
sell cap; and a listing at the day's Etsy limit, which no tool refuses (the approved listing waits for the next day in
Ember's code), stays ready.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app.agent import plan, preconditions, store, tools  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import etsy, etsy_publisher  # noqa: E402
from tests.etsy_helpers import call  # noqa: E402
from tests.plan.test_cycle_line import cycle, lined, now, working  # noqa: E402
from tests.plan.test_limit_waits import full_queue, launched, refused_by  # noqa: E402
from tests.plan.test_marketing_order import PINS  # noqa: E402
from tests.plan.test_plan_tree import BOOK, TRACKER, keep, project, request  # noqa: E402
from tests.plan.test_promises import promise  # noqa: E402
from tests.plan.test_sell_request_waits import PROPOSE, ready_to_list, selling  # noqa: E402
from tests.plan.test_step_brake import candidate, closed_without_proposing, decided, live, noted  # noqa: E402
from tests.plan.test_tree_steers import steered  # noqa: E402
from tests.products.test_kdp import QA, next_reply, paperback, publishing, spec  # noqa: E402

# 0.42.2 (analysis-0.40.1, 5.6): tools.py and the modules split out of it (tools_*.py)
SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in sorted(Path(tools.__file__).parent.glob("tools*.py")))
# what a rule the plan can know reads: preconditions.py reads it, a refusal in tools.py doesn't
LIMITS = {
    "store.PENDING_CAPS",
    "store.pending_of_type",
    "store.MESSAGES_PER_DAY",
    "store.unasked_since",
    "store.MAX_UNREAD_MESSAGES",
    "store.unread_messages",
    "publish_queue.WAITING",
    "publish_queue.waiting",
    "publish_queue.slotted",
    "kdp.WEEKLY_TITLES",
    "kdp.weekly",
    "kdp.week_start",
    "demand.needs_note",
    "demand.recent",
    "demand.listed",
    "MESSAGES_PER_DAY",
    "MAX_UNREAD_MESSAGES",
}
DAILY = ("daily_limit", "_per_day", "created_today", "sent_today")  # the platforms' daily limits
LISTING = {
    "title": "Job Application Tracker", "description": "A tracker for applications.", "price": "4.50",
    "tags": "tracker", "category_id": 1, "files": "shop/tracker.pdf", "photos": "shop/tracker.png",
    "reason": "Applicants search for it",
}  # fmt: skip


def _reads(expr: ast.AST, assigned: dict[str, list[ast.expr]], seen: set[str]) -> set[str]:
    """The names an expression reads (``store.PENDING_CAPS`` and ``PENDING_CAPS``), through the local variables it
    uses (``assigned``: what each was assigned in its function)."""
    found: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
            if isinstance(node.value, ast.Name):
                found.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Name):
            found.add(node.id)
            if node.id in assigned and node.id not in seen:
                seen.add(node.id)
                for value in assigned[node.id]:
                    found |= _reads(value, assigned, seen)
    return found


def _refuses(statement: ast.stmt) -> bool:
    exc = statement.exc if isinstance(statement, ast.Raise) else None
    return isinstance(exc, ast.Call) and getattr(exc.func, "id", "") == "ToolError"


def limits_read_by_refusals(source: str) -> list[str]:
    """Each ``if ...: raise ToolError`` of ``source`` whose condition reads a limit the plan could know (LIMITS,
    DAILY), with what it reads."""
    said = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        assigned: dict[str, list[ast.expr]] = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                for name in (n for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)):
                    assigned.setdefault(name.id, []).append(node.value)
        for node in ast.walk(function):
            if not isinstance(node, ast.If) or not any(_refuses(s) for s in node.body):
                continue
            read = {r for r in _reads(node.test, assigned, set()) if r in LIMITS or r.endswith(DAILY)}
            if read:
                said.append(f"{function.name} (line {node.lineno}): {sorted(read)}")
    return said


def checked(name: str) -> set[str]:
    """The preconditions tool ``name``'s handler checks (their Rule.name): those it or a function of tools.py it calls
    names (preconditions.UNREAD), and the cap of each type of request it makes (_new_request's type: ROOM[sell])."""
    functions = {f.name: f for f in ast.walk(ast.parse(SOURCE)) if isinstance(f, ast.FunctionDef)}
    found: set[str] = set()
    todo, seen = [tools.HANDLERS[name].__name__], set()
    while todo:
        function = todo.pop()
        if function in seen or function not in functions:
            continue
        seen.add(function)
        for node in ast.walk(functions[function]):
            if isinstance(node, ast.Attribute) and getattr(node.value, "id", "") == "preconditions":
                rule = getattr(preconditions, node.attr)
                if isinstance(rule, preconditions.Rule):
                    found.add(rule.name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "_new_request":
                    kind = next(k.value for k in node.keywords if k.arg == "type")
                    assert isinstance(kind, ast.Constant), ast.unparse(kind)
                    found.add(preconditions.ROOM[kind.value].name)
                todo.append(node.func.id)
    return found


def the_step(agent: Agent, title: str, line: int | None = None) -> plan.Candidate:
    return candidate(steered(agent), title, line)


def at_once(agent: Agent) -> str:
    """YOUR STEP, as an ordinary cycle's plan sees it now."""
    with agent.db.connection() as conn:
        return plan.step_text(conn, agent.scope(), steered(agent), explore=False)


def listing(agent: Agent, line: int) -> tools.Outcome:
    """propose_etsy_listing of a listing of line ``line``, in a cycle on it with the shop (its files made first)."""
    ctx = working(agent, line)
    ctx.etsy = tools.EtsyAccess("EmberTestShop", "EUR", daily_limit=3, categories=etsy.FAKE_CATEGORIES)
    agent.roots()[0].write_bytes("shop/tracker.pdf", b"%PDF-1.7 tracker")
    agent.roots()[0].write_bytes("shop/tracker.png", b"\x89PNG photo")
    return call(ctx, "propose_etsy_listing", {**LISTING, "project_id": line})


# --- the class ---


def test_no_refusal_in_the_tools_reads_a_limit_the_plan_could_know() -> None:
    """A refusal that reads a count, a limit or a date the plan can know before a cycle is a precondition: the plan
    can't know a rule written inline in a tool, and a step the tool refuses would cut the sleep for a cycle that ends in
    the refusal. 0.40.3's tools.py had eight such refusals of seven rules (the request caps, a channel's queue and its
    slots, the day's messages, the unread ones, a demand note's age in two tools, KDP's week); none read a platform's
    daily limit."""
    assert limits_read_by_refusals(SOURCE) == []
    inline = "def t(x):\n    n = kdp.WEEKLY_TITLES\n    if x >= n:\n        raise ToolError('')\n"  # through a name
    assert limits_read_by_refusals(inline) == ["t (line 3): ['kdp.WEEKLY_TITLES']"]
    assert limits_read_by_refusals("def t(shop, n):\n    if n >= shop.daily_limit:\n        raise ToolError('')\n")


def test_each_tool_checks_the_preconditions_the_plan_reads_for_it() -> None:
    """preconditions.TOOLS names each tool's rules (the plan waits on them): the handler checks exactly those, and a
    tool that checks a precondition is in it."""
    for name in tools.HANDLERS:
        found = checked(name)
        assert found == {r.name for r in preconditions.TOOLS.get(name, ())}, name
    assert set(preconditions.TOOLS) <= set(tools.HANDLERS)
    assert checked("message_owner") == {"UNREAD", "MESSAGES"}
    assert checked("propose_kdp_book") == {"KDP_WEEK", "ROOM[sell]"}
    assert checked("request_approval") == set()  # its type is the call's: no step of the plan is one


def test_the_plan_knows_the_tool_of_every_request_a_step_counts() -> None:
    assert set(plan.CALLED) == set(store.EXECUTOR_TYPES)  # each kind of request, the tool that makes it
    assert set(preconditions.TOOLS) == {*plan.CALLED.values(), "message_owner"}
    assert set(plan.PUBLISHES) == set(plan.CHANNELS)
    assert {r.wait for rules in preconditions.TOOLS.values() for r in rules} <= plan.REFUSED <= set(plan.WAITS)


def test_a_step_of_each_tool_waits_on_each_of_its_rules_with_its_words(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) for every pair: a step whose work is a request the tool makes (or a report) waits on the rule's wait, with
    the words the tool would refuse it with."""
    agent, _ = lined(data_dir, titles=())
    blank = {"id": 1, "title": "A step", "project_id": None, "channel": None, "obligation_id": None}
    steps = {
        tool: {**blank, "kind": "ship", "check_kind": "request", "check_spec": json.dumps({"executor": executor})}
        for executor, tool in plan.CALLED.items()
    }
    steps["message_owner"] = {**blank, "kind": "report", "check_kind": "agent", "check_spec": None}
    assert set(steps) == set(preconditions.TOOLS)
    for tool, row in steps.items():
        with agent.db.connection() as conn:  # nothing refuses: nothing waits
            found = plan.limits(conn, agent.scope(), now(agent), now(agent))
            assert plan._limited(plan.Facts(conn, agent.scope(), now(agent)), row, found) is None, tool
        for rule in preconditions.TOOLS[tool]:
            said = f"{tool} refuses by {rule.name}"
            refuses = preconditions.Rule(rule.name, rule.wait, lambda _, s=said: s)
            with monkeypatch.context() as patched, agent.db.connection() as conn:
                patched.setitem(preconditions.TOOLS, tool, (refuses,))
                found = plan.limits(conn, agent.scope(), now(agent), now(agent))
                facts = plan.Facts(conn, agent.scope(), now(agent))
                assert plan._limited(facts, row, found) == rule.wait, (tool, rule.name)
                assert plan._words(facts, row, found, rule.wait) == said


# --- the cases ---


def test_a_pin_waits_while_three_publish_requests_wait_with_the_tools_words(data_dir: Path) -> None:
    """0.37.9's case: three publish requests wait for the owner, and the next is refused (tools._new_request checks
    preconditions.PUBLISH): "Pin it twice" waits on the queue in those words and cuts no sleep."""
    agent, _, tracker, letters = launched(data_dir)
    assert the_step(agent, PINS, tracker).waiting is None
    full_queue(agent, tracker, letters)
    pins = the_step(agent, PINS, tracker)
    with agent.db.connection() as conn:
        said = refused_by(tools._room_for_request, working(agent, tracker), conn, "publish")
        assert not plan.busy(conn, agent.scope(), steered(agent), now(agent))
    assert said == "3 publish requests are already waiting for your owner: withdraw one that is outdated" + (
        " (withdraw_request), or wait for their decision"
    )
    assert (pins.waiting, pins.refused) == ("queue", said)
    assert f"on {plan.WAITS['queue']}" in at_once(agent)


def test_a_kdp_proposal_waits_in_a_full_kdp_week_with_the_tools_words(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.40.2's case, which the plan didn't know: two paperbacks proposed in KDP's week, and "Propose the book" of a
    third (its check passed: a paperback) looked ready, cut the sleep, and propose_kdp_book refused it. Now it waits
    ('blocked') in the tool's words, until KDP's next week begins."""
    monkeypatch.delenv("TZ", raising=False)  # the owner's time zone: the tool's clock's and the plan's, UTC both
    agent, ctx = publishing(data_dir)
    line = project(agent, *BOOK)
    keep(agent)
    ctx.cycle_id = cycle(agent, line, status="running")  # a cycle on the book
    path = spec(ctx.workspace, paperback(ctx.workspace), project_id=line)
    checked_book = call(ctx, "propose_kdp_book", {"spec": path, "check": True})
    assert checked_book.ok, checked_book.text
    keep(agent)
    assert the_step(agent, "Propose the book", line).waiting is None
    with agent.db.transaction() as conn:  # two paperbacks of other books proposed this week
        for n in range(2):
            store.insert_approval(
                conn, agent.scope(), 1, now(agent), project_id=None, type="sell", title=f"KDP paperback: Book {n}",
                description="a book", payload=f"book {n}", expected_cost="none", expected_benefit="royalties",
                executor="kdp_package", action=json.dumps({"format": "paperback"}),
            )  # fmt: skip
    book = the_step(agent, "Propose the book", line)
    next_reply(ctx)
    refused = call(ctx, "propose_kdp_book", {"spec": path, "qa": QA})
    said = (
        "you proposed 2 paperbacks this week (KDP's, from Sunday 00:00 UTC), and KDP lets an account create at most 2"
        " new paperbacks a week: propose the next one from Sunday 09-06 at 00:00"
    )
    assert not refused.ok and said in refused.text
    assert (book.waiting, book.refused) == ("blocked", said)
    with agent.db.connection() as conn:
        assert not plan.busy(conn, agent.scope(), steered(agent), now(agent))
    agent.clock.current = datetime.fromisoformat("2026-09-06T00:01:00+00:00")  # type: ignore[attr-defined]
    keep(agent)
    assert the_step(agent, "Propose the book", line).waiting is None  # KDP's next week


def test_a_report_at_the_unread_cap_waits_on_the_rule_message_owner_checks_first(data_dir: Path) -> None:
    """0.40.3's case (PL3): while the owner hasn't read Ember's last 5 messages, message_owner refuses every message.
    With 5 sent today, the day's limit holds too: the plan waits on the rule the tool refuses by first, in its words,
    and then on the day's limit once the owner reads them."""
    agent, _ = lined(data_dir, titles=())
    owed = promise(agent, "Report the coach bundle's ads numbers", days=1)
    agent.clock.advance(days=1)
    with agent.db.transaction() as conn:
        for n in range(store.MAX_UNREAD_MESSAGES):
            store.insert_message(conn, agent.scope(), 1, f"Update {n}", now(agent))
    keep(agent)
    title = f"Keep promise #{owed}"
    report = the_step(agent, title)
    refused = call(working(agent, None), "message_owner", {"text": f"Promise #{owed}: the numbers.", "answers": ""})
    assert not refused.ok and "your owner hasn't read your last 5 messages yet" in refused.text
    assert (report.waiting, report.refused) == ("unread", "your owner hasn't read your last 5 messages yet")
    with agent.db.transaction() as conn:  # the owner reads them
        conn.execute("UPDATE messages SET read_at = ? WHERE sender = 'agent'", (now(agent),))
    report = the_step(agent, title)
    refused = call(working(agent, None), "message_owner", {"text": f"Promise #{owed}: the numbers.", "answers": ""})
    said = (
        "you sent your owner 2 messages today that answer none of theirs: batch the rest into tomorrow's, or into your"
        " answer when they write"
    )
    assert not refused.ok and said in refused.text
    assert (report.waiting, report.refused) == ("messages", said)
    agent.clock.advance(days=1)
    keep(agent)
    assert the_step(agent, title).waiting is None


def test_a_first_listing_waits_for_a_demand_note_and_then_for_room_in_the_tools_words(data_dir: Path) -> None:
    """propose_etsy_listing refuses a line's first listing without a demand note from the last 14 days (0.40.3: the
    plan puts a new note's step in front of a proposal whose note aged). A proposal the plan has no note step for
    (its line never had a note: the research stage closed with the create stage) waited on nothing, and the tool
    refused it in every cycle that took it. Now it waits ('blocked') in the tool's words; with the note, on the sell
    requests ('sales') while six wait, in those words."""
    agent, _ = lined(data_dir, titles=())
    line = project(agent, *TRACKER)
    keep(agent)
    first = request(agent, line, "etsy_listing")
    keep(agent)
    decided(agent, first, "rejected")
    keep(agent)
    closed_without_proposing(agent)
    keep(agent)
    again = f"Propose the listing again: request #{first} rejected"
    refused = listing(agent, line)
    said = (
        f"project #{line} has no listing yet, and a new product line needs a demand note from the last 14 days first"
        " (demand_note: the keywords buyers search and what shows they buy)"
    )
    assert not refused.ok and said in refused.text
    step = the_step(agent, again, line)
    assert (step.waiting, step.refused) == ("blocked", said)
    assert f"1 on {plan.WAITS['blocked']} ({said})" in at_once(agent)  # no step of the line is ready
    noted(agent, line)
    assert the_step(agent, again, line).waiting is None
    selling(agent, store.PENDING_CAPS["sell"])
    refused = listing(agent, line)
    assert not refused.ok and "6 sell requests are already waiting for your owner" in refused.text
    step = the_step(agent, again, line)
    assert step.waiting == "sales" and step.refused is not None and step.refused in refused.text


def test_a_listing_at_the_days_etsy_limit_stays_ready_as_no_tool_refuses_it(data_dir: Path) -> None:
    """etsy_listings_per_day is no precondition: propose_etsy_listing takes the request, and an approved listing over
    the day's limit waits for the next day in Ember's code (etsy_publisher), where the owner's approval tonight makes
    it go out first thing. So "Propose the listing" stays ready at the limit, as it was (the platforms' other daily
    limits too: no tool refuses by them, test_no_refusal_in_the_tools_reads_a_limit_the_plan_could_know)."""
    agent, _ = lined(data_dir, titles=())
    line = project(agent, *TRACKER)
    others = [project(agent, f"Planner {n}") for n in range(agent.settings.etsy_listings_per_day)]
    keep(agent)
    for other in others:
        live(agent, other)  # a listing of each created today
    with agent.db.connection() as conn:
        assert etsy_publisher.created_today(conn, agent.clock, agent.scope()) == agent.settings.etsy_listings_per_day
    ready_to_list(agent, line)
    keep(agent)
    assert the_step(agent, PROPOSE, line).waiting is None
    made = listing(agent, line)
    assert made.ok, made.text


def test_the_tools_preconditions_read_one_clock_and_the_calls_facts(data_dir: Path) -> None:
    """A tool's facts are its clock's (the owner's day_start, their time zone) and what its call is about."""
    agent, _ = lined(data_dir, titles=())
    ctx = working(agent)
    with agent.db.connection() as conn:
        facts = tools._facts(ctx, conn, line=3, form="ebook")
    assert (facts.now, facts.day_start, facts.tz) == (
        agent.clock.now(),
        to_iso(agent.clock.day_start(agent.clock.today())),
        agent.clock.tz,
    )
    assert (facts.line, facts.form, facts.channel, facts.answers) == (3, "ebook", None, False)
    assert preconditions.blocked("message_owner", facts) is None
    assert preconditions.blocked("no_such_tool", facts) is None
