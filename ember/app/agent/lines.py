"""One thing a cycle (0.28.0): what a wake cycle is, and the product lines' READY lists its plan takes one from.

An ordinary cycle worked on several product lines, an unbacked venture and marketing at once: its rules said "keep
2-3 experiments in flight ... when a project waits, work on another", its plan named a project, a venture and the
roadmap's milestone due first apart, and its tools took any project. Live, a cycle on venture #12 listed #12's licence
bundle in the cover-letter line, files were filed under the wrong project, and one line's cost counted for another.

Now every wake cycle is about one thing:

* an ordinary cycle works on one product line: build it, fix it, research it, scale it;
* a marketing cycle (0.28.0; the owner's marketing_share of each day's spending, like the venture share) brings
  buyers to one line's listings: pins, Bluesky posts, a blog post, the link page, a Reddit post, titles and tags;
* a venture cycle decides one venture (desk.py, unchanged);
* a reactive cycle reacts to its event, on one line at most.

Ember's code ranks the lines for each ordinary and marketing plan (READY, ``ready``/``marketing``), and the plan takes
one (``ready``: its key) or says why it takes none. The tools refuse another line's work for the rest of the cycle
(tools._line). A pressing obligation of a line makes READY offer that line alone, once a day at most (an obligation
nobody closes doesn't take every cycle). Each pick is kept with the list it came from, in the decision desk's table
(desk_picks, kind 'line' or 'market'). What a wake cycle is comes from ``kind``: pressing work first, then the share
furthest behind its part of the day's spending, then an ordinary cycle.

0.30.0: a plan she keeps. READY put the line worked on longest ago first after what was owed and due, so every cycle
took another line: in a dry run, 13 ordinary cycles on four lines switched lines 11 times, each handoff was written for
a line the next cycle didn't take, and no line was finished before the next was begun. Now, after what a line owes, the
line the newest ordinary cycles worked on comes first while it has work, MAX_STREAK cycles in a row at most (a
marketing, venture or reactive cycle in between doesn't break the run); then the lines the agent's own judgement chose:
this week's focus lines (the weekly look, weekly.focus) and the changes today's review asked for; a line the review
said to stop goes after the others, and so does one that only waits for the owner or just had its run. Among the
rest, a milestone due, another task of its own and selling come first. A bar of a line's listing test is Ember's
code's check of Etsy's numbers, not the cycle's work: it no longer counts as a milestone due (what a miss asks comes
as an obligation). Each line shows the next step its last cycle left (``handoffs``), and FOCUS shows it in full.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..integrations import etsy_publisher
from . import bets, metrics, obligations, prompts, quality, reach, review, roadmap, ventures, weekly
from .store import AgentScope, open_projects

# The kinds of wake cycle (cycles.venture and cycles.marketing record the two that take a share of the spending).
ORDINARY, MARKETING, VENTURE, REACTIVE = "ordinary", "marketing", "venture", "reactive"
LINE, NEW, MARKET = "line", "new line", "market"  # READY's kinds of item: a line, a new line, a line to market
MAX_LINES = 6  # the lines READY offers: its budget (context.PLANNER_BUDGETS) holds about six
ITEM_CHARS = 200  # an item's text
WHY_CHARS = 200  # why a plan takes no READY item, as kept
DUE_DAYS = 7  # a milestone of the line due this soon (or overdue) puts it ahead
IN_FLIGHT = 2  # with fewer lines in flight (open, not waiting), a new line comes first
PRESS_HOURS = 24  # a pressing obligation takes its line for a cycle at most this often
MAX_STREAK = prompts.LINE_STREAK  # 0.30.0: ordinary cycles in a row on one line before READY ranks it as usual
HANDOFF_CHARS = 70  # 0.30.0: of a line's last handoff, as READY quotes it
HANDOFF_FOCUS_CHARS = 300  # 0.30.0: as FOCUS quotes it
HEADING = "Ranked by Ember's code. Take one (ready: its key), or say why none:"
PRESSED_HEADING = "A pressing obligation of this line comes first: Ember's code takes it for this cycle."
QUESTIONS = "This week's questions (keep them in mind; not items to take):"
_KEY = re.compile(r"^\s*(?:(line|market)\s*#?\s*(\d+)|(new)(?:\s+line)?)\b", re.IGNORECASE)
_STAGE_RANK = {"selling": 1, "not_bought": 2, "not_liked": 3}  # marketing's order after the unseen lines (0)
_LABEL = re.compile(r'^(listing #\d+) "[^"]*"')  # quality.label's title, which READY leaves out


def _one_line(value: Any, limit: int) -> str:
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class Item:
    kind: str  # LINE, NEW or MARKET
    project_id: int | None  # the line (None for a new line)
    text: str
    pressed: bool = False  # a pressing obligation of the line: Ember's code takes it
    job: bool = False  # a concrete job (owed, due, the critic's fixes, a demand note; buyers to bring): wakes sooner

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _one_line(self.text, ITEM_CHARS))

    @property
    def key(self) -> str:
        return self.kind if self.project_id is None else f"{self.kind} #{self.project_id}"

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "project_id": self.project_id, "text": self.text}


# --- what a wake cycle is ---


def marketing_share(venture_share: int, share: int) -> int:
    """The share of each day's spending marketing cycles get: the owner's, within what the ventures' share leaves (the
    two can't take more than all of it)."""
    return max(0, min(share, 100 - max(0, venture_share)))


def marketing_turn(share: int, spent: int, marketed: int) -> bool:
    """Whether marketing cycles have had less than ``share`` percent of the day's spending (ventures.venture_turn's
    rule: the day's first cycle, with nothing spent yet, is an ordinary one)."""
    if share <= 0:
        return False
    if share >= 100:
        return True
    return marketed * 100 < spent * share


@dataclass(frozen=True)
class Turn:
    kind: str
    owed_first: bool = False  # a share's turn gave way to what is owed (the loop records why)


def kind(
    *,
    venture_share: int,
    share: int,
    spends: tuple[int, int, int],
    ventures_run: bool,
    markets: bool,
    marketable: Iterable[int],
    owed: Sequence[obligations.Owed],
    messages: bool,
) -> Turn:
    """What a scheduled or owner-woken wake cycle is. ``spends``: the day's spending, the venture cycles' and the
    marketing cycles' part of it; ``ventures_run``: the burn mode runs venture cycles; ``markets``: marketing cycles
    run (a share, the burn mode, a shop) and ``marketable`` are the lines they may take; ``owed``: the pressing
    obligations; ``messages``: the owner woke it and their messages wait (0.19.3: they wait for what they asked).

    What the owner and the pressing product work wait for comes first (an ordinary cycle), then a pressing push to
    bring buyers to a line (a marketing cycle); else the share furthest behind its part of the day's spending, if one
    is (ventures need nothing pressing), else an ordinary cycle."""
    spent, ventured, marketed = spends
    v_turn = ventures_run and ventures.venture_turn(venture_share, spent, ventured)
    m_turn = markets and marketing_turn(share, spent, marketed)
    lines = set(marketable)
    if messages or any(not (o.marketing and markets) for o in owed):
        return Turn(ORDINARY, owed_first=v_turn or m_turn)
    if any(o.line in lines for o in owed):
        return Turn(MARKETING)
    if owed:  # a push for a line that can't be marketed now: what presses still comes before the ventures
        v_turn = False
    if v_turn and m_turn:
        behind = spent * share - 100 * marketed > spent * venture_share - 100 * ventured
        return Turn(MARKETING if behind else VENTURE)
    return Turn(VENTURE if v_turn else MARKETING if m_turn else ORDINARY)


# --- READY ---


def ready(
    conn: sqlite3.Connection,
    scope: AgentScope,
    *,
    today: date,
    explore: bool,
    markets: bool,
    owed: Sequence[obligations.Owed] = (),
    since: str = "",
) -> list[Item]:
    """An ordinary plan's READY: the open lines the owner's park or kill doesn't stop, at most MAX_LINES, each with its
    jobs (what it owes, its milestone due, the critic's fixes, a missing demand note, building or scaling it), ranked
    (_Facts.rank): a line a pressing obligation takes, one that owes something, (0.30.0) the line the newest ordinary
    cycles worked on while it has work, a line today's review said to stop, one that only waits for the owner and one
    that just had MAX_STREAK cycles in a row after the others, this week's focus lines and the changes today's review
    asked for, one with a milestone due within DUE_DAYS, one with another task of its own, one that sells, the one
    worked on longest ago; and a new line in the explore burn mode (first while fewer than IN_FLIGHT lines are in
    flight). ``owed``: the pressing obligations (product work; with ``markets``, a push to bring buyers is a
    marketing cycle's, and so is a review's change for reach); ``since``: a line a pressing obligation took after
    this isn't taken again."""
    lines = _lines(conn, scope)
    if not lines:
        return [Item(NEW, None, "start your first product line (project_create)")] if explore else []
    facts = _facts(conn, scope, today, lines, last=_last(conn, scope, marketing=False), markets=markets)
    pressed = _pressed(conn, scope, [o for o in owed if not (o.marketing and markets)], set(lines), since)
    if pressed is not None:
        f = facts[pressed]
        return [Item(LINE, pressed, f.text(markets), pressed=True, job=True)]
    ranked = sorted(lines, key=lambda pid: facts[pid].rank())
    items = [Item(LINE, pid, facts[pid].text(markets), job=facts[pid].job) for pid in ranked[:MAX_LINES]]
    if explore:
        flying = sum(1 for pid in lines if facts[pid].flying)
        new = Item(NEW, None, f"start a new product line (project_create): {flying} in flight")
        items = [new, *items] if flying < IN_FLIGHT else [*items, new]
    return items


def marketing(
    conn: sqlite3.Connection,
    scope: AgentScope,
    *,
    blog: bool,
    owed: Sequence[obligations.Owed] = (),
    since: str = "",
    today: date | None = None,
) -> list[Item]:
    """A marketing plan's READY: the open, unstopped lines with a live listing of Ember's own (a pin, a post and an
    edit take only those; with the owner's blog on, a blog post may recommend one Printify made too), at most
    MAX_LINES, ranked: a line a pressing push takes, one that owes a push to bring buyers (gates.MARKET), (0.30.0) this
    week's focus lines (``today``: the weekly look's, weekly.focus), then by its funnel (not seen with too little reach
    done, selling, liked but not bought, seen but not liked, not seen though marketed), the one marketed longest ago
    first."""
    funnels = reach.funnels(conn, scope)
    eligible = _marketable(conn, scope, blog, funnels)
    if not eligible:
        return []
    owes = _owes(conn, scope)
    pushes = {pid for pid, (_, market) in owes.items() if market}
    pressed = _pressed(conn, scope, [o for o in owed if o.marketing], set(eligible), since)
    last = _last(conn, scope, marketing=True)
    titles = {int(p["id"]): p for p in open_projects(conn, scope)}
    chosen = set(weekly.focus(conn, scope, today)) if today is not None else set()

    def text(pid: int) -> str:
        f = funnels[pid]
        owed_here = [n for n, market in owes[pid][0] if market] if pid in owes else []
        push = f" · owes: obligation #{owed_here[0]}" if owed_here else ""
        focus = " · this week's focus" if pid in chosen else ""
        when = f"last marketed {last[pid][:10]}" if pid in last else "never marketed"
        return f"{titles[pid]['title']}: {f.short()}{push}{focus} · {when}"

    def job(pid: int) -> bool:
        f = funnels[pid]
        return pid in pushes or (f.stage == "not_seen" and f.reach < reach.ENOUGH)

    if pressed is not None:
        return [Item(MARKET, pressed, text(pressed), pressed=True, job=True)]

    def rank(pid: int) -> tuple[Any, ...]:
        f = funnels[pid]
        unseen = f.stage == "not_seen"
        stage = 0 if unseen and f.reach < reach.ENOUGH else _STAGE_RANK.get(f.stage, 4)
        return (pid not in pushes, pid not in chosen, stage, last.get(pid, ""), pid)

    return [Item(MARKET, pid, text(pid), job=job(pid)) for pid in sorted(eligible, key=rank)[:MAX_LINES]]


def marketable(conn: sqlite3.Connection, scope: AgentScope, blog: bool) -> list[int]:
    """The lines a marketing cycle may take (``marketing``'s, unranked)."""
    return _marketable(conn, scope, blog, reach.funnels(conn, scope))


def _marketable(conn: sqlite3.Connection, scope: AgentScope, blog: bool, funnels: dict[int, reach.Funnel]) -> list[int]:
    live = etsy_publisher.live_rows(metrics.listings(conn, scope, None, None), {})
    own = {int(r["for_project"]) for r in live if r["for_project"] and not r["printify"]}
    lines = set(_lines(conn, scope))
    return sorted(pid for pid, f in funnels.items() if f.listings and pid in lines and (pid in own or blog))


def text(items: list[Item], questions: Sequence[str] = ()) -> str:
    """READY as the plan sees it: the items numbered, as the decision desk's are, and this week's questions."""
    if not items and not questions:
        return ""
    head = PRESSED_HEADING if items and items[0].pressed else HEADING
    lines = [head, *(f"{n}. {item.key}: {item.text}" for n, item in enumerate(items, 1))] if items else []
    if questions:
        lines.append(QUESTIONS)
        lines += [f"- {_one_line(q, ITEM_CHARS)}" for q in questions]
    return "\n".join(lines)


def questions(conn: sqlite3.Connection, scope: AgentScope, today: date) -> list[str]:
    """What this week's look asked the week to answer (slack.py's question items before 0.28.0)."""
    look = weekly.latest(conn, scope, today)
    if look is None:
        return []
    try:
        asked = json.loads(look["answer"] or "{}").get("questions") or []
    except (ValueError, AttributeError):
        return []
    return [str(q) for q in asked if str(q).strip()]


def choose(items: list[Item], answer: str) -> tuple[Item | None, str]:
    """The plan's ``ready``: the item it takes, or None and why (its own words, or what was wrong with its answer)."""
    said = " ".join(str(answer or "").split())
    found = _KEY.match(said)
    if found:
        key = f"{found[1].lower()} #{int(found[2])}" if found[1] else NEW
        for item in items:
            if item.key == key:
                return item, ""
    if said.lower().startswith("none"):
        why = said[4:].lstrip(" :-–").strip()
        return None, (why or "no reason given")[:WHY_CHARS]
    return None, (f"{said[:80]!r} isn't on the READY list" if said else "the plan named none")[:WHY_CHARS]


def record(
    conn: sqlite3.Connection, cycle_id: int, kind_: str, items: list[Item], pick: Item | None, why: str, now: str
) -> None:
    """An ordinary or marketing plan's pick and the READY list it came from (``kind_``: 'line' or 'market'), in the
    decision desk's table: never changed."""
    conn.execute(
        "INSERT INTO desk_picks (cycle_id, created_at, items, pick, why_not, kind, project_id, pressed)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cycle_id,
            now,
            json.dumps([i.to_json() for i in items], ensure_ascii=False),
            pick.key if pick else None,
            None if pick else why[:WHY_CHARS],
            kind_,
            pick.project_id if pick else None,
            1 if pick is not None and pick.pressed else 0,
        ),
    )


def recent(conn: sqlite3.Connection, scope: AgentScope, limit: int = 10) -> list[sqlite3.Row]:
    """The latest ordinary and marketing plans' picks, the newest first."""
    return conn.execute(
        "SELECT d.* FROM desk_picks d JOIN cycles y ON y.id = d.cycle_id WHERE y.session = ? AND y.simulated = ?"
        " AND d.kind IN ('line', 'market') ORDER BY d.id DESC LIMIT ?",
        (scope.session, 1 if scope.simulated else 0, limit),
    ).fetchall()


def focus_text(conn: sqlite3.Connection, scope: AgentScope, line: int, marketing: bool) -> str:
    """The work steps' FOCUS on the cycle's line: Ember's code keeps the tools on it, its funnel and the reach done
    for it, and in a marketing cycle its live listings (a pin, a post and an edit name one). 0.30.0: in an ordinary
    cycle, the next step the last cycle on the line left (the cycles between were often about other things), and on a
    live line without an open bet, to bet on what the cycle's change brings (a bet settled is a case to learn from)."""
    found = [
        f"Your line this cycle: project #{line}. Ember's code keeps your tools on it: what another line needs waits for"
        " a cycle of its own."
    ]
    said = handoffs(conn, scope).get(line) if not marketing else None
    if said is not None:
        quoted = json.dumps(_one_line(said[1], HANDOFF_FOCUS_CHARS), ensure_ascii=False)
        found.append(f"Your last cycle on it (#{said[0]}) left as next: {quoted}")
    funnel = reach.funnels(conn, scope).get(line)
    if funnel is not None:
        found.append(funnel.text())
        if not marketing and funnel.listings and line not in bets.open_lines(conn, scope):
            found.append(
                "No open bet on it: say what you expect this cycle's change to bring (project_update bet), so Ember's"
                " code checks it and your review learns from it."
            )
    if marketing:
        for r in etsy_publisher.live_rows(metrics.listings(conn, scope, line, None), {})[:MAX_LINES]:
            whose = " (made by Printify)" if r["printify"] else ""
            found.append(
                f"- listing #{r['listing_id']}{whose} {_one_line(r['title'], 60)}: {r['views'] or 0} views,"
                f" {r['favorites'] or 0} favorites"
            )
    return "\n".join(found)


def has_job(items: Iterable[Item]) -> bool:
    """Whether READY lists a concrete job (slack.sleep wakes the agent sooner for one): not a line only waiting for
    the owner, a new line or this week's questions."""
    return any(item.job for item in items)


# --- the facts READY ranks the lines by ---


@dataclass
class _Facts:
    title: str
    status: str
    owes: list[int]  # open obligations of the line
    due: tuple[str, int] | None  # its milestone due first (day, number), if due within DUE_DAYS or overdue
    fixes: list[str]  # what the quality critic's last checks of its listings said to fix
    no_demand: bool  # live without a demand note
    funnel: reach.Funnel | None  # None: nothing live yet
    waiting: list[int]  # its requests waiting for the owner
    last: str  # when a cycle last worked on it ("" never)
    # 0.30.0: what keeps a plan going and what the agent's own judgement chose
    streak: int = 0  # the newest ordinary cycles in a row that worked on it (0: the last one worked on another line)
    handoff: str = ""  # the next step the last cycle on it left
    focus: bool = False  # one of this week's focus lines (the weekly look)
    verdict: str = ""  # today's review's verdict on it: continue, change or stop ("" without one)
    neck: str = ""  # the bottleneck today's review named for it
    markets: bool = False  # marketing cycles run: a change for reach is theirs
    focused: bool = False  # this week has a focus line that doesn't only wait (the same for every line)

    @property
    def change(self) -> bool:
        """0.30.0: today's review asked for a change an ordinary cycle makes (one for reach is the marketing cycles'
        while they run)."""
        return self.verdict == "change" and not (self.neck == "reach" and self.markets)

    @property
    def tasks(self) -> bool:
        """Concrete work of its own: what it owes, its milestone due, the critic's fixes, a missing demand note, the
        review's change."""
        return bool(self.owes or self.due or self.fixes or self.no_demand or self.change)

    @property
    def waits(self) -> bool:
        return self.status == "waiting" or (bool(self.waiting) and not self.tasks)

    @property
    def continues(self) -> bool:
        """0.30.0: the line the newest ordinary cycles worked on, while it has work: fewer than MAX_STREAK cycles in a
        row, not only waiting for the owner, not a line today's review said to stop, and while this week's focus lines
        have work, one of them or a line with a task of its own (a detour from them stays one cycle)."""
        return (
            0 < self.streak < MAX_STREAK
            and not self.waits
            and self.verdict != "stop"
            and (self.focus or self.tasks or not self.focused)
        )

    @property
    def rested(self) -> bool:
        """0.30.0: the line had MAX_STREAK ordinary cycles in a row: the next goes to another line with work."""
        return self.streak >= MAX_STREAK

    @property
    def job(self) -> bool:
        return self.tasks or self.continues or (self.focus and not self.waits)

    @property
    def flying(self) -> bool:
        return self.status != "waiting"

    @property
    def selling(self) -> bool:
        return self.funnel is not None and self.funnel.stage == "selling"

    def rank(self) -> tuple[Any, ...]:
        """0.30.0: what it owes; the line in progress; then not a line to stop, one only waiting or one that just had
        its MAX_STREAK cycles; this week's focus and the review's changes; a milestone due; another task of its own;
        selling; the one worked on longest ago (0.28.0 went from what was owed and due straight to the one worked on
        longest ago, so every cycle took another line)."""
        return (
            not self.owes,
            not self.continues,
            self.verdict == "stop",
            self.waits,
            self.rested,
            not (self.focus or self.change),
            self.due is None,
            not self.tasks,
            not self.selling,
            self.last,
        )

    def text(self, markets: bool) -> str:
        """What READY says of the line, the most pressing first (an item holds ITEM_CHARS)."""
        parts = [f"{self.title} [{self.status}]"]
        if self.owes:
            parts.append("owes " + ", ".join(f"obligation #{n}" for n in self.owes[:2]))
        if self.continues:  # 0.30.0
            nth = f"cycle {self.streak + 1} of at most {MAX_STREAK} in a row"
            said = f": next {json.dumps(_one_line(self.handoff, HANDOFF_CHARS), ensure_ascii=False)}"
            parts.append(f"in progress, {nth}" + (said if self.handoff else ""))
        elif self.rested:
            parts.append(f"had {MAX_STREAK} cycles in a row: another line with work first")
        if self.verdict == "stop":
            parts.append("your review said stop: close it (project_update)")
        if self.focus:
            parts.append("this week's focus")
        if self.change:
            parts.append("your review: change" + (f" ({self.neck})" if self.neck else ""))
        if self.due:
            parts.append(f"milestone #{self.due[1]} due {self.due[0]}")
        parts.append(_funnel(self.funnel, markets))
        if self.fixes:
            more = f" (+{len(self.fixes) - 1} more)" if len(self.fixes) > 1 else ""
            said = _LABEL.sub(r"\1", self.fixes[0].removeprefix("the quality critic said of "))
            parts.append("the critic on " + _one_line(said, 70) + more)
        if self.no_demand:
            parts.append("no demand note")
        if self.waiting:
            parts.append("waits for your owner: " + ", ".join(f"request #{n}" for n in self.waiting[:2]))
        parts.append(f"last worked on {self.last[:10]}" if self.last else "never worked on")
        return " · ".join(parts)


def _funnel(f: reach.Funnel | None, markets: bool) -> str:
    if f is None or not f.listings:
        return "nothing live yet: build it"
    if f.stage == "selling":
        return f"selling ({f.orders} orders): scale it"
    if f.stage == "not_seen" and markets:
        return f"not seen yet ({f.views} views): its marketing cycles bring buyers"
    return f.short().removeprefix("funnel: ")


def _lines(conn: sqlite3.Connection, scope: AgentScope) -> list[int]:
    """The open lines the owner's park or kill doesn't stop, the one changed last first."""
    return [int(p["id"]) for p in open_projects(conn, scope) if ventures.project_stopped(conn, scope, p["id"]) is None]


def _facts(
    conn: sqlite3.Connection,
    scope: AgentScope,
    today: date,
    lines: list[int],
    last: dict[int, str],
    markets: bool = False,
) -> dict[int, _Facts]:
    where, params = scope.where()
    projects = {int(p["id"]): p for p in open_projects(conn, scope)}
    funnels = reach.funnels(conn, scope)
    owes = _owes(conn, scope)
    soon = (today + timedelta(days=DUE_DAYS)).isoformat()
    # 0.30.0: a bar of a line's listing test is Ember's code's check, not the cycle's work (_bar)
    milestones = [m for m in roadmap.open_milestones(conn, scope) if not roadmap.waiting(m, today) and not _bar(m)]
    running, streak = _streak(conn, scope)  # 0.30.0: the line in progress
    said = handoffs(conn, scope)
    judged = verdicts(conn, scope, today)
    chosen = set(weekly.focus(conn, scope, today))
    waiting: dict[int, list[int]] = {}
    for r in conn.execute(f"SELECT * FROM approvals WHERE {where} AND status = 'pending' ORDER BY id", params):
        line = ventures.request_line(conn, scope, r)
        if line is not None:
            waiting.setdefault(line, []).append(int(r["id"]))
    noted = {
        int(r[0])
        for r in conn.execute(f"SELECT DISTINCT project_id FROM demand_notes WHERE {where}", params)
        if r[0] is not None
    }
    found = {}
    for pid in lines:
        p = projects[pid]
        venture = p["venture_id"]
        mine = [m for m in milestones if roadmap.of_line(m, pid, venture) and str(m["due"]) <= soon]
        due = (str(mine[0]["due"]), int(mine[0]["id"])) if mine else None
        funnel = funnels.get(pid)
        verdict, neck = judged.get(pid, ("", ""))
        found[pid] = _Facts(
            title=_one_line(p["title"], 50),
            status=str(p["status"]),
            owes=[n for n, _ in owes[pid][0]] if pid in owes else [],
            due=due,
            fixes=fixes(conn, scope, pid) if funnel is not None and funnel.listings else [],
            no_demand=funnel is not None and bool(funnel.listings) and pid not in noted,
            funnel=funnel,
            waiting=waiting.get(pid, []),
            last=last.get(pid, ""),
            streak=streak if pid == running else 0,
            handoff=said[pid][1] if pid in said else "",
            focus=pid in chosen,
            verdict=verdict,
            neck=neck,
            markets=markets,
        )
    focused = any(f.focus and not f.waits for f in found.values())
    for f in found.values():
        f.focused = focused
    return found


def _bar(milestone: sqlite3.Row) -> bool:
    """0.30.0: a bar of a product line's listing test (gates.py): Ember's code checks it from Etsy's numbers, so it is
    no work of the cycle's (a backed venture's first test, of no line, is)."""
    code = milestone["created_by"] == "code" and milestone["kind"] == "first_test"
    return code and milestone["project_id"] is not None


def _streak(conn: sqlite3.Connection, scope: AgentScope) -> tuple[int | None, int]:
    """0.30.0: the line the newest ordinary cycles worked on, and how many of them in a row (MAX_STREAK at most). A
    marketing, venture or reactive cycle between them doesn't break the run, nor does an idle one; a running cycle
    isn't counted yet."""
    rows = conn.execute(
        "SELECT project_id FROM cycles WHERE session = ? AND simulated = ? AND venture = 0 AND marketing = 0"
        " AND trigger NOT IN ('event', 'last_will') AND status NOT IN ('running', 'idle') AND project_id IS NOT NULL"
        " ORDER BY id DESC LIMIT ?",
        (scope.session, 1 if scope.simulated else 0, MAX_STREAK),
    ).fetchall()
    if not rows:
        return None, 0
    line = int(rows[0][0])
    count = 0
    for r in rows:
        if int(r[0]) != line:
            break
        count += 1
    return line, count


def handoffs(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, tuple[int, str]]:
    """0.30.0: the next step (its journal's next) the newest cycle on each line left, with that cycle's number: the
    plan's YOUR LAST CYCLE shows the last cycle's, often of another line, a venture or marketing."""
    where, params = scope.where("j")
    found: dict[int, tuple[int, str]] = {}
    for r in conn.execute(
        f"SELECT y.project_id, j.cycle_id, j.handoff FROM journal j JOIN cycles y ON y.id = j.cycle_id WHERE {where}"
        " AND j.author = 'agent' AND j.handoff <> '' AND y.project_id IS NOT NULL AND y.venture = 0"
        " ORDER BY j.id DESC LIMIT 500",
        params,
    ):
        found.setdefault(int(r[0]), (int(r[1]), str(r[2])))
    return found


def verdicts(conn: sqlite3.Connection, scope: AgentScope, today: date) -> dict[int, tuple[str, str]]:
    """0.30.0: today's review's verdict on each line it judged, with the bottleneck it named ({} before the day's
    review)."""
    row = review.of_day(conn, scope, today)
    if row is None:
        return {}
    try:
        items = json.loads(row["verdicts"] or "[]")
    except ValueError:
        return {}
    found: dict[int, tuple[str, str]] = {}
    for v in items if isinstance(items, list) else []:
        pid = v.get("project_id") if isinstance(v, dict) else None
        if isinstance(pid, int) and not isinstance(pid, bool) and v.get("verdict") in review.VERDICTS:
            found[pid] = (str(v["verdict"]), str(v.get("bottleneck") or ""))
    return found


def fixes(conn: sqlite3.Connection, scope: AgentScope, project_id: int) -> list[str]:
    """What the quality critic's newest check of each listing of the line said to fix, when the listing wasn't changed
    since (0.24.0: each names its listing; a change waits for the next check, quality.due). slack.py's improve items
    before 0.28.0."""
    found = []
    checked = quality.newest(conn, scope, project_id) or quality.unplaced(conn, scope, project_id)
    for row in checked:
        if row["verdict"] == "improve" and row["fixes"] and not quality.changed_since(conn, scope, row):
            judged = quality.label(conn, scope, row["listing_id"])
            found.append(f"the quality critic said of {judged}: {row['fixes'][:200]}")
    return found


def _owes(conn: sqlite3.Connection, scope: AgentScope) -> dict[int, tuple[list[tuple[int, bool]], bool]]:
    """The open obligations by line: (their numbers with whether each is marketing work, whether any is)."""
    found: dict[int, tuple[list[tuple[int, bool]], bool]] = {}
    for r in obligations.open_rows(conn, scope):
        o = obligations.owed(conn, scope, r)
        if o.line is None:
            continue
        rows, market = found.get(o.line, ([], False))
        found[o.line] = ([*rows, (int(r["id"]), o.marketing)], market or o.marketing)
    return found


def _last(conn: sqlite3.Connection, scope: AgentScope, *, marketing: bool) -> dict[int, str]:
    """When an ordinary cycle (or with ``marketing``, a marketing cycle) last worked on each line."""
    return {
        int(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT project_id, MAX(started_at) FROM cycles WHERE session = ? AND simulated = ? AND venture = 0"
            " AND marketing = ? AND project_id IS NOT NULL GROUP BY project_id",
            (scope.session, 1 if scope.simulated else 0, 1 if marketing else 0),
        )
    }


def _pressed(
    conn: sqlite3.Connection, scope: AgentScope, owed: Sequence[obligations.Owed], lines: set[int], since: str
) -> int | None:
    """The line the first pressing obligation (of this kind of cycle's work) takes, unless one took it after ``since``
    already."""
    taken = {
        int(r[0])
        for r in conn.execute(
            "SELECT d.project_id FROM desk_picks d JOIN cycles y ON y.id = d.cycle_id WHERE y.session = ?"
            " AND y.simulated = ? AND d.pressed = 1 AND d.created_at > ? AND d.project_id IS NOT NULL",
            (scope.session, 1 if scope.simulated else 0, since),
        )
    }
    return next((o.line for o in owed if o.line in lines and o.line not in taken), None)
