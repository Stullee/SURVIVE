"""0.40.3: the plan's fixes of the codebase analysis of 0.40.1 (analysis-0.40.1, 4.3 PL1 to PL5, 4.4 PL6, 6.2).

PL1. A launch's marketing on a channel at its floor waits ('floor') as a weekly step there does, and doesn't keep the
     weekly pin of the channel that works from being laid out.
PL2. The owner's day is their clock's, not the UTC date: a decision's due date, the day's waits and changes, the park
     clocks, the critic's retry (clock.local_date; a scan keeps new ``[:10]`` dates out of agent/ and integrations/).
PL3. A report waits ('unread') while message_owner refuses every message: the owner hasn't read Ember's last five.
PL4. A line's first listing waits behind a new demand note once its newest is older than the tool allows.
PL5. plan_step wait refuses a step the owner pinned and a wait that would never lift; the owner lifts a wait.
PL6. Closing a line closes what was owed on it: its promise isn't left open with no step.

Tests from 0.40.3 (formerly test_fixes_0403_plan.py)."""

from __future__ import annotations

import ast
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import demand, desk, obligations, plan, quality, store, tools, ventures, weights  # noqa: E402
from app.agent.service import Agent  # noqa: E402
from app.economy.clock import Clock, local_date, to_iso  # noqa: E402
from tests.agent_helpers import rows  # noqa: E402
from tests.api_helpers import post  # noqa: E402
from tests.etsy_helpers import call, started  # noqa: E402
from tests.plan.test_channel_worth import FLOORED  # noqa: E402
from tests.plan.test_cycle_line import cycle, lined, now, working  # noqa: E402
from tests.plan.test_marketing_order import launched  # noqa: E402
from tests.plan.test_owner_holds import first_marketing_out, waits, weekly  # noqa: E402
from tests.plan.test_plan_tree import ALL, TRACKER, keep, project  # noqa: E402
from tests.plan.test_promises import promise  # noqa: E402
from tests.plan.test_step_brake import asked, closed_without_proposing, decided, noted  # noqa: E402
from tests.plan.test_tree_steers import steered  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
APP = Path(__file__).parents[2] / "app"


def at(agent: Agent, utc: str) -> None:
    """The agent's clock at ``utc`` (an ISO time in UTC, without its Z)."""
    agent.clock.current = datetime.fromisoformat(utc).replace(tzinfo=UTC)  # type: ignore[attr-defined]


def open_steps(agent: Agent, line: int) -> dict[str, int]:
    found = rows(
        agent, f"SELECT id, title FROM plan_nodes WHERE project_id = {line} AND level = 'step' AND status = 'open'"
    )
    return {r["title"]: r["id"] for r in found}


# --- PL1. a launch's marketing on a channel at its floor waits ---


def test_a_launch_post_on_a_channel_at_its_floor_waits_and_the_weekly_pin_comes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the audit, a new product's "Post it twice on Bluesky" and "A German blog post" were ready on channels worth
    0.21 and 0.2, took the launch's urgency, became YOUR STEP and cut the sleep; and while they were open the weekly pin
    of Pinterest, the channel that works, was never laid out."""
    agent, line, _ = launched(data_dir)
    first_marketing_out(agent, line, ("pin",))  # its launch pins are out (Pinterest works: 0.5)
    monkeypatch.setattr(plan, "_channel_factors", lambda conn, scope, now: dict(FLOORED))
    agent.clock.advance(days=1)
    keep(agent)
    launch = {
        r["title"]: r["id"]
        for r in rows(
            agent,
            f"SELECT id, title FROM plan_nodes WHERE project_id = {line} AND stage = 'launch' AND kind = 'market'"
            " AND status = 'open'",
        )
    }
    assert launch  # its Bluesky post and its blog post, as its audience reads them
    waiting = waits(agent, line, ALL)
    assert {waiting[title] for title in launch} == {"floor"}
    assert "recurring/pinterest" in {s["template"] for s in weekly(agent, line)}  # the channel that works
    ranked = {s.id for s, _ in steered(agent).pick.ranked}
    assert not ranked & set(launch.values())  # no YOUR STEP, and no sleep cut, for them
    title, step = next(iter(launch.items()))
    with agent.db.transaction() as conn:  # the owner's pin brings it back
        plan.pin(conn, agent.scope(), step, True, "Owner", now(agent))
    assert waits(agent, line, ALL)[title] is None
    assert steered(agent).step.id == step


# --- PL2. the owner's day ---


def test_a_decision_made_after_the_owners_midnight_is_due_that_day(data_dir: Path) -> None:
    """In the audit, a rejection at 00:20 on 10-26 (CET, after the clocks changed) was due 10-25: overdue at birth,
    urgency 11 instead of a decision's on its day."""
    agent, _ = lined(data_dir, titles=())
    agent.clock.tz = BERLIN
    at(agent, "2026-10-25T10:00:00")
    line = project(agent, *TRACKER)
    keep(agent)
    request = asked(agent, line, "etsy_listing")
    at(agent, "2026-10-25T23:20:00")  # 00:20 on Monday 10-26 in Berlin
    with agent.db.transaction() as conn:
        conn.execute("UPDATE approvals SET status = 'rejected', decided_at = ? WHERE id = ?", (now(agent), request))
    agent._preview_runner()._keep_obligations()  # noqa: SLF001 - as each cycle's keeper does
    [owed] = rows(agent, "SELECT id, due FROM obligations WHERE kind = 'decision'")
    assert agent.clock.today() == date(2026, 10, 26) and owed["due"] == "2026-10-26"
    keep(agent)
    [found] = [c for c in steered(agent).found if c.step.title.startswith(f"Obligation #{owed['id']}")]
    assert found.step.urgency == weights.promise_urgency(0.5)  # on its own day, not overdue


def test_the_days_waits_and_changes_count_from_the_owners_midnight(data_dir: Path) -> None:
    """Waits said at 22:30 counted against the next day's first at 00:30 (the UTC date was still the day before), and
    the Plan tab's "changed today" showed nothing of the owner's day until 02:00."""
    agent, _ = lined(data_dir, titles=())
    agent.clock.tz = BERLIN
    at(agent, "2026-10-24T10:00:00")
    lines = [project(agent, f"{TRACKER[0]} {n}", TRACKER[1]) for n in range(4)]
    keep(agent)
    first = {line: min(open_steps(agent, line).values()) for line in lines}

    def wait(line: int) -> str:
        ctx = working(agent, line)
        args = {"action": "wait", "step_id": first[line], "on": "date", "until": "2026-10-28", "why": "later"}
        out = call(ctx, "plan_step", args)
        return out.text

    at(agent, "2026-10-24T20:30:00")  # 22:30 on Saturday in Berlin
    assert wait(lines[0]).endswith("(1 of 2 waits today).") and wait(lines[1]).endswith("(2 of 2 waits today).")
    at(agent, "2026-10-24T22:30:00")  # 00:30 on Sunday: a new owner's day
    assert wait(lines[2]).endswith("(1 of 2 waits today).")
    at(agent, "2026-10-25T00:30:00")  # 02:30 on Sunday: the same owner's day, a new UTC date
    assert wait(lines[3]).endswith("(2 of 2 waits today).")
    with agent.db.connection() as conn:
        today = agent.clock.today()
        shown = plan.view(conn, agent.scope(), now(agent), today, ALL, day_start=to_iso(agent.clock.day_start(today)))
    changed = {c["id"] for c in shown["changes"] if c["actor"] == "agent" and c["action"] == "wait"}
    assert changed == {first[lines[2]], first[lines[3]]}


def test_the_park_clocks_the_critics_retry_and_the_rules_day_are_the_owners(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A park clock started after the owner's midnight parked a day early; a critic's check that failed then was tried
    again the same night; a rule set then was "set yesterday"."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    began = "2026-10-25T23:30:00Z"  # 00:30 on Monday 10-26 in Berlin
    assert Clock(tz=BERLIN).local_date(began) == local_date(began) == "2026-10-26"
    venture = {"stage": "researching", "research_from": began, "stage_at": began, "created_at": began}
    park = date(2026, 10, 26) + timedelta(days=ventures.RESEARCH_DAYS)
    assert ventures.rule_clock(venture) == date(2026, 10, 26) and desk._park_date(venture) == park  # noqa: SLF001
    assert ventures.stage_rule(venture) == f"researched since 2026-10-26: no business case by {park} parks it"
    assert store._set_on(began, date(2026, 10, 26)) == "set 2026-10-26, today"  # noqa: SLF001
    agent, line = started(data_dir)
    agent.clock.tz = BERLIN
    at(agent, "2026-10-26T01:00:00")  # 02:00 on 10-26 in Berlin
    [listing] = [r["listing_id"] for r in rows(agent, "SELECT listing_id FROM etsy_listings")]
    with agent.db.connection() as conn:
        assert quality.due(conn, agent.scope(), agent.clock.today()) == (line, listing)
    with agent.db.transaction() as conn:
        quality.save(conn, agent.scope(), line, None, None, "the model failed", began, listing)
    with agent.db.connection() as conn:
        assert quality.due(conn, agent.scope(), agent.clock.today()) is None  # failed today: tomorrow


# The ``[:10]`` slices left in agent/ and integrations/, each meaning no owner's day: a date shown beside a record
# (what the agent reads of it, in UTC, as stored), a date of Etsy's own, or a slice of a list or of words. A date that
# counts the owner's day (a due date, today, a park clock) takes clock.local_date (or a count from day_start).
ALLOWED = {
    ("app/agent/context.py", "r['decided_at'] or r['created_at']"),  # shown: a request's day
    ("app/agent/context.py", "store.expires_at(r)"),  # shown: when a request expires
    ("app/agent/loop.py", "other"),  # a list
    ("app/agent/metrics.py", "str(row['checked_at'])"),  # shown: when a milestone was read
    ("app/agent/obligations.py", "str(r['created_at'])"),  # shown: an obligation's day
    ("app/agent/plan.py", "str(spec['since'])"),  # shown: a check's words
    ("app/agent/quality.py", "str(r['created_at'])"),  # shown: a check's day
    ("app/agent/quality.py", "str(row['created_at'])"),  # shown: a check's day
    ("app/agent/reach.py", "str(note['created_at'])"),  # shown: a demand note's day
    ("app/agent/review.py", "str(item.get('new_due') or '').strip()"),  # the model's words, a date
    ("app/agent/review.py", "p['updated_at']"),  # shown
    ("app/agent/review.py", "cycles[2]"),  # shown: the last cycle's day
    ("app/agent/review.py", "r['started_at']"),  # shown: a listing's first day
    ("app/agent/roadmap.py", "str(row['created_at'])"),  # a pace's start, a day at most of weeks; rows built by hand
    ("app/agent/roadmap.py", "str(replaced['closed_at'] or '')"),  # shown
    ("app/agent/tools_metered.py", "str(r['started_at'])"),  # shown: when a question was researched
    ("app/agent/tools_projects.py", "str(p['updated_at'])"),  # shown: a project's last change
    (
        "app/agent/tools_requests.py",
        "list(dict.fromkeys((int(n) for n in re.findall('\\\\d{1,9}', args['numbers']))))",
    ),
    ("app/agent/ventures.py", "str(row['created_at'])"),  # shown
    ("app/agent/ventures.py", "str(row['owner_at'])"),  # shown: the owner's word's day
    ("app/agent/website.py", "p['updated_at']"),  # shown
    ("app/agent/website.py", "now['downloaded_at']"),  # shown
    ("app/integrations/bluesky_publisher.py", "str(r['finished_at'] or r['started_at'])"),  # shown
    ("app/integrations/bluesky_publisher.py", "row['ends_at']"),  # Etsy's own date
    ("app/integrations/etsy_publisher.py", "row['ends_at'] or ''"),  # Etsy's own date
    ("app/integrations/etsy_publisher.py", "str(row['ends_at'])"),  # Etsy's own date
    ("app/integrations/etsy_publisher.py", "item.ends_at"),  # Etsy's own date
    ("app/integrations/etsy_publisher.py", "r['started_at']"),  # shown
    ("app/integrations/kdp.py", "str((r['closed_at'] if r['status'] == 'failed' else r['decided_at']) or '')"),
    ("app/integrations/live_view.py", "status.ended_at"),  # shown
    ("app/integrations/pinterest_publisher.py", "row['ends_at']"),  # Etsy's own date
    ("app/integrations/site_publisher.py", "shown[0]['seen_at']"),  # shown: when the blog was read
}


def test_no_new_utc_date_stands_for_the_owners_day() -> None:
    """analysis-0.40.1, 5.3: about ten places took ``now[:10]``, the UTC date, for the owner's day, wrong every night
    from their midnight to 01:00 or 02:00."""
    found: set[tuple[str, str]] = set()
    sql: list[str] = []
    for folder in ("agent", "integrations"):
        for path in sorted((APP / folder).glob("*.py")):
            source = path.read_text(encoding="utf-8")
            name = path.relative_to(APP.parent).as_posix()
            for node in ast.walk(ast.parse(source)):
                cut = node.slice if isinstance(node, ast.Subscript) else None
                if (
                    isinstance(cut, ast.Slice)
                    and cut.lower is None
                    and cut.step is None
                    and isinstance(cut.upper, ast.Constant)
                    and cut.upper.value == 10
                ):
                    found.add((name, ast.unparse(node.value)))  # type: ignore[attr-defined]
            sql += [f"{name}: {m}" for m in re.findall(r"substr\([^)]*,\s*1,\s*10\)", source, re.IGNORECASE)]
    new = sorted(found - ALLOWED)
    assert not new, f"a [:10] date: use clock.local_date for the owner's day, or add it to ALLOWED with why: {new}"
    assert not sql, f"a UTC date in SQL: count from the owner's day_start instead: {sql}"
    assert not ALLOWED - found, f"gone, take them out of ALLOWED: {sorted(ALLOWED - found)}"


# --- PL3. a report waits while the owner hasn't read Ember's last messages ---


def test_a_report_waits_while_the_owner_hasnt_read_embers_last_messages(data_dir: Path) -> None:
    """message_owner refuses every message, a report too, while the owner hasn't read MAX_UNREAD_MESSAGES of Ember's;
    the plan didn't know, so a promise to report stayed ready, urgent and YOUR STEP: a cycle a day, refused."""
    assert tools.MAX_UNREAD_MESSAGES == store.MAX_UNREAD_MESSAGES == 5  # one rule, the tool's and the plan's
    agent, _ = lined(data_dir, titles=())
    owed = promise(agent, "Report the coach bundle's ads numbers", days=1)
    agent.clock.advance(days=-1)  # sent yesterday: today's limit of messages is untouched
    with agent.db.transaction() as conn:
        for n in range(store.MAX_UNREAD_MESSAGES):
            store.insert_message(conn, agent.scope(), 1, f"Update {n}", now(agent))
    agent.clock.advance(days=1)
    keep(agent)
    s = steered(agent)
    [report] = [c for c in s.found if c.step.title.startswith(f"Keep promise #{owed}")]
    assert report.waiting == "unread" and (s.step is None or s.step.id != report.step.id)
    with agent.db.connection() as conn:
        assert not plan.busy(conn, agent.scope(), s, now(agent))
        assert "all 5 of Ember's last messages are unread" in plan.limits(conn, agent.scope(), now(agent)).text()
    refused = call(working(agent, None), "message_owner", {"text": f"Promise #{owed}: the numbers.", "answers": ""})
    assert not refused.ok and "hasn't read your last 5 messages" in refused.text
    with agent.db.transaction() as conn:  # the owner reads them
        conn.execute("UPDATE messages SET read_at = ? WHERE sender = 'agent'", (now(agent),))
    [report] = [c for c in steered(agent).found if c.step.title.startswith(f"Keep promise #{owed}")]
    assert report.waiting is None
    assert "unread" in plan.WAITS


# --- PL4. a first listing waits behind a new demand note ---


def test_a_first_listing_proposed_again_after_its_note_aged_waits_behind_a_new_note(data_dir: Path) -> None:
    """A line's first listing needs a demand note from the last 14 days when it is proposed. The owner rejected one 16
    days after its note: "Propose the listing again" was urgent (0.40.1: until it is proposed), and the tool refused it
    at once, a cycle a day."""
    agent, _ = lined(data_dir, titles=())
    line = project(agent, *TRACKER)
    keep(agent)
    noted(agent, line)
    keep(agent)
    request = asked(agent, line, "etsy_listing")
    keep(agent)
    agent.clock.advance(days=demand.DAYS + 2)
    decided(agent, request, "rejected")
    keep(agent)
    keep(agent)
    with agent.db.connection() as conn:
        assert demand.needs_note(conn, line, now(agent))  # the tool would refuse it
    again = f"Propose the listing again: request #{request} rejected"
    steps = open_steps(agent, line)
    [note] = [title for title in steps if title.startswith("Write a new demand note")]
    assert note == (
        "Write a new demand note: a first listing needs one from the last 14 days (the newest is of 2026-09-01)"
    )
    waiting = waits(agent, line, ALL)
    assert waiting[again] == "step" and waiting[note] is None
    ranked = {s.id: parts for s, parts in steered(agent).pick.ranked}
    assert steps[again] not in ranked and ranked[steps[note]].carried_from == steps[again]  # it lends its weight
    closed_without_proposing(agent)  # the owner's decision noted: the note is YOUR STEP
    keep(agent)
    assert steered(agent).step.id == steps[note]
    noted(agent, line)  # a new note
    keep(agent)
    assert note not in open_steps(agent, line) and waits(agent, line, ALL)[again] is None
    assert steered(agent).step.id == steps[again]


# --- PL5. waits that void a pin or never lift; the owner's unwait ---


def test_a_wait_refuses_a_pinned_step_and_one_that_would_never_lift(data_dir: Path) -> None:
    """hold and skip refused a step the owner pinned; wait voided the pin silently. And a step could wait on a step
    that waits on it: only its closing lifts a wait, so "Propose the listing" on "You approve it" waited for good."""
    agent, _ = lined(data_dir, titles=())
    line = project(agent, *TRACKER)
    keep(agent)
    ctx = working(agent, line)
    step = steered(agent).step.id
    with agent.db.transaction() as conn:
        plan.pin(conn, agent.scope(), step, True, "Owner", now(agent))
    day = (agent.clock.today() + timedelta(days=3)).isoformat()
    pinned = call(ctx, "plan_step", {"action": "wait", "step_id": step, "on": "date", "until": day, "why": "later"})
    assert not pinned.ok and f"your owner pinned step #{step}" in pinned.text
    added = call(ctx, "plan_step", {"action": "add", "stage": "research", "why": "x", "steps": [
        {"title": "Read the competitors' reviews", "kind": "create"},
        {"title": "Sum up what buyers miss", "kind": "create"},
    ]})  # fmt: skip
    assert added.ok, added.text
    mine = open_steps(agent, line)
    a, b = mine["Read the competitors' reviews"], mine["Sum up what buyers miss"]
    first = call(ctx, "plan_step", {"action": "wait", "step_id": a, "on": "step", "ref": b, "why": "after it"})
    assert first.ok, first.text
    back = call(ctx, "plan_step", {"action": "wait", "step_id": b, "on": "step", "ref": a, "why": "after it"})
    assert not back.ok and f"step #{a} waits on step #{b}, so the wait would never lift" in back.text
    release = open_steps(agent, line)
    propose, approve = release["Propose the listing"], release["You approve it, and it goes live"]
    owners = call(ctx, "plan_step", {"action": "wait", "step_id": propose, "on": "step", "ref": approve, "why": "x"})
    assert not owners.ok and f"step #{approve} is your owner's and waits for the request step #{propose}" in owners.text
    later = call(ctx, "plan_step", {"action": "wait", "step_id": b, "on": "step", "ref": propose, "why": "x"})
    assert not later.ok and "comes in the release stage, which waits until the research stage is done" in later.text


def test_the_owner_lifts_a_wait_ember_said(ingress_client: TestClient) -> None:
    """Only Ember's code lifted a wait, once its block was gone: one said by mistake held its step until then."""
    agent = ingress_client.app.state.ember.agent  # type: ignore[attr-defined]
    cycle(agent)
    line = project(agent, *TRACKER)
    keep(agent)
    step = min(open_steps(agent, line).values())
    day = (agent.clock.today() + timedelta(days=5)).isoformat()
    with agent.db.transaction() as conn:
        plan.wait_step(
            conn, agent.scope(), line, step, "date", None, day, "later", None, now(agent), agent.clock.today()
        )

    def shown() -> dict[str, object]:
        products = [q for p in ingress_client.get("api/plan").json()["projects"] for q in p["products"]]
        return next(s for q in products for st in q["stages"] for s in st["steps"] if s["id"] == step)

    assert shown()["unwait"] is True and shown()["waiting"] == "date"
    assert post(ingress_client, f"api/plan/steps/{step}/unwait", headers={}).status_code == 403
    lifted = post(ingress_client, f"api/plan/steps/{step}/unwait")
    assert lifted.status_code == 200 and lifted.json() == {"id": step, "line": line, "waiting": None}
    assert shown()["unwait"] is False and shown()["waiting"] is None
    assert post(ingress_client, f"api/plan/steps/{step}/unwait").status_code == 409  # no wait now
    assert post(ingress_client, "api/plan/steps/9999/unwait").status_code == 404
    assert rows(agent, f"SELECT actor, action FROM plan_changes WHERE node_id = {step} ORDER BY id")[-2:] == [
        {"actor": "agent", "action": "wait"},
        {"actor": "owner", "action": "unwait"},
    ]
    with agent.db.connection() as conn:
        said = plan.owner_words(conn, agent.scope(), "2000-01-01T00:00:00Z")
    assert any(w.startswith(f"lifted the wait of #{step} ") for w in said), said
    script = (APP / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "planUnwaitButton(st)," in script and '"/unwait"' in script


# --- PL6. a closed line's promise ---


def test_closing_a_line_closes_its_promise(data_dir: Path) -> None:
    """Closing a line dropped its promise's step, but the obligation stayed open and no plan laid it out again."""
    agent, _ = lined(data_dir, titles=())
    line = project(agent, *TRACKER)
    other = project(agent, "Nebenkosten-Vorlage", "Eine Vorlage für Vermieter")
    keep(agent)
    owed = promise(agent, "Tell you the tracker's numbers and what I do next", days=3, line=line)
    kept = promise(agent, "Tell you what the landlords say", days=3, line=other)
    keep(agent)
    [product] = rows(agent, f"SELECT id FROM plan_nodes WHERE level = 'product' AND project_id = {line}")
    with agent.db.transaction() as conn:
        plan.end_product(conn, agent.scope(), product["id"], False, "not worth it", "Owner", now(agent))
    assert rows(agent, f"SELECT status, closed_by, result FROM obligations WHERE id = {owed}") == [
        {"status": "closed", "closed_by": "code", "result": "line closed by your owner"}
    ]
    with agent.db.transaction() as conn:  # a line closed otherwise (a venture killed): Ember's code closes it too
        conn.execute("UPDATE projects SET status = 'abandoned' WHERE id = ?", (other,))
    said = keep(agent)
    assert any(f"What was owed on it closed with it: obligation #{kept}." in s for s in said), said
    assert rows(agent, f"SELECT status, result FROM obligations WHERE id = {kept}") == [
        {"status": "closed", "result": "its line closed (abandoned)"}
    ]
    with agent.db.connection() as conn:
        assert obligations.close_one(conn, kept, "again", "code", None, now(agent)) is False  # closed is final
