"""One wake cycle: plan, act with tools, reflect (or write the last will).

Every model call goes through the budget guard (``MeteredModel``), which can
refuse it; the runner treats a refusal as the end of what it was doing, never
as an error to retry. The conversation of the act phase is kept exactly as the
API returned it, and every ``tool_use`` is answered by a ``tool_result`` in
the next message, in order (the fake model checks this in tests).

In dry run the whole cycle runs with the network and other programs blocked
(``netguard.sealed``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from .. import events
from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, to_iso
from ..economy.costs import micros_to_usd
from ..economy.estimate import Unpriceable
from ..economy.metering import CallFailed, CallRefused, CallResult, MeteredModel
from ..economy.pricing import LAST_WILL, PLANNER_OPENING
from ..economy.service import Economy
from ..version import app_version
from . import context, netguard, news, prompts, store, tools
from .memory import Memory
from .sandbox import Jail
from .store import AgentScope

log = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_TURN = 4
MAX_CONVERSATION_BYTES = 24_000
MAX_EMPTY_NUDGES = 1
RETRY_DELAY_SECONDS = 5.0
NUDGE = "Continue with the plan, or reply with a short report of what you did."
CUT_OFF = "Your reply was cut off at the length limit. Continue in shorter parts, or use a tool."
STEP_GROWTH_BYTES = 20_000  # what one step can add: up to 4 tool results and the model's own reply
PLANNER_SCALES = (1.0, 0.75, 0.5, 0.3)  # the planner's context budgets, until the request fits its profile
NO_STEP = "not enough money left in this cycle for a work step and the reflection"


class Stopping(Exception):
    """The app is shutting down: end the cycle as interrupted."""


class EndCycle(Exception):
    """Something the cycle can't continue after (the guard refused for state or system reasons)."""

    def __init__(self, status: str, note: str) -> None:
        super().__init__(note)
        self.status = status
        self.note = note


@dataclass
class CycleEnd:
    status: str
    note: str | None = None
    rerun: bool = False  # decide again at once (e.g. starvation made the last will due)
    sleep_minutes: int | None = None
    sleep_reason: str | None = None  # the agent's own words for sleep_minutes (set_sleep), if it gave any
    cycle_id: int | None = None
    skipped: bool = False  # the cycle never opened


@dataclass
class Plan:
    assessment: str
    goal: str
    focus_project_id: int | None
    steps: list[str]
    sleep_minutes: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "assessment": self.assessment,
            "goal": self.goal,
            "focus_project_id": self.focus_project_id,
            "steps": self.steps,
            "sleep_minutes": self.sleep_minutes,
        }


@dataclass
class _Act:
    turns: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)  # tool_result blocks not sent yet
    report: str = ""
    end_reason: str = ""
    steps: int = 0  # work calls the model answered


class CycleRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        clock: Clock,
        economy: Economy,
        meter: MeteredModel,
        scope: AgentScope,
        workspace: Jail,
        memory: Memory,
        stop: threading.Event | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.economy = economy
        self.meter = meter
        self.scope = scope
        self.workspace = workspace
        self.memory = memory
        self.stop = stop or threading.Event()
        self.dry_run = scope.mode == "dry_run"

    # --- the cycle ---

    def run(self, trigger: str) -> CycleEnd:
        try:
            cycle_id = self.meter.open_cycle(trigger)
        except CallRefused as exc:
            return CycleEnd("skipped", exc.reason, skipped=True)
        state = tools.CycleTools()
        ctx = tools.ToolContext(
            db=self.db,
            clock=self.clock,
            scope=self.scope,
            cycle_id=cycle_id,
            workspace=self.workspace,
            memory=self.memory,
            min_sleep=self.settings.min_sleep_minutes,
            max_sleep=self.settings.max_sleep_minutes,
            state=state,
            # A fetched PDF has no size limit, so reading pages costs real money only if the owner allows it.
            allow_fetch=self.dry_run or self.settings.web_fetch,
        )
        ctx.research = self._research_fn(ctx)
        end = CycleEnd("failed", "the cycle ended unexpectedly")
        try:
            with netguard.sealed() if self.dry_run else contextlib.nullcontext():
                end = self._last_will(cycle_id) if trigger == "last_will" else self._plan_act_reflect(cycle_id, ctx)
        except Stopping:
            end = CycleEnd("interrupted", "the app was stopping")
        except EndCycle as exc:
            end = CycleEnd(exc.status, exc.note)
        except Exception as exc:  # noqa: BLE001 - a bug ends the cycle, never the app
            log.exception("Wake cycle #%d failed", cycle_id)
            end = CycleEnd("failed", f"internal error ({type(exc).__name__})")
        end.cycle_id = cycle_id
        if end.sleep_minutes is None:
            end.sleep_minutes, end.sleep_reason = state.sleep_minutes, state.sleep_reason or None
        self._close(cycle_id, end, state)
        return end

    def _close(self, cycle_id: int, end: CycleEnd, state: tools.CycleTools) -> None:
        try:
            self._write_report(cycle_id, end)
        except Exception:  # noqa: BLE001 - the cycle must still be closed, or no later cycle could open
            log.exception("Could not write the report of cycle #%d", cycle_id)
        try:
            closed = self.meter.close_cycle(cycle_id, end.status if end.status != "skipped" else "failed", end.note)
        except Exception:  # noqa: BLE001
            log.exception("Could not close cycle #%d", cycle_id)
            return
        try:
            self._announce(cycle_id, end, closed)
        except Exception:  # noqa: BLE001
            log.exception("Could not record the end of cycle #%d", cycle_id)

    def _write_report(self, cycle_id: int, end: CycleEnd) -> None:
        now = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            store.interrupt_open_tool_calls(conn, now, cycle_id)
            if not store.has_journal(conn, cycle_id):
                row = conn.execute("SELECT plan FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
                plan = json.loads(row["plan"]) if row and row["plan"] else {}
                summary = f"Cycle ended {end.status}" + (f": {end.note}" if end.note else "")
                entry = f"Goal: {plan.get('goal', '-')}" if plan else "No plan was made."
                store.write_journal(conn, self.scope, cycle_id, "system", summary, entry, now)
            if end.sleep_minutes is not None:
                store.update_cycle(conn, cycle_id, sleep_minutes=end.sleep_minutes)
            store.update_cycle(conn, cycle_id, phase=None, current_action=None)

    def _announce(self, cycle_id: int, end: CycleEnd, closed: bool) -> None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT status FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        final = row["status"] if row else end.status
        if not closed and final != end.status:
            end.status = final  # the guard already stopped it (an overrun)
        spent, _ = self.economy.books.cycle_spend(cycle_id)
        tail = f"; next sleep {end.sleep_minutes} min" if end.sleep_minutes else ""
        level = "info" if final in ("completed", "idle") else "warning"
        events.record(
            self.db,
            level,
            "agent",
            f"Cycle #{cycle_id} {final}: ${micros_to_usd(spent):.4f}" + (f" ({end.note})" if end.note else "") + tail,
            {"cycle_id": cycle_id},
        )

    def _progress(self, cycle_id: int, **columns: Any) -> None:
        with self.db.transaction() as conn:
            store.update_cycle(conn, cycle_id, **columns)

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Stopping

    def _snapshot(self) -> context.Snapshot:
        status = self.economy.life.evaluate()
        scope = self.economy.life.scope()
        today = self.economy.books.cap_spend_on(scope, self.clock.today())
        local = self.clock.now().astimezone(self.clock.tz).strftime("%A %Y-%m-%d %H:%M %Z")
        with self.db.connection() as conn:
            fresh = news.collect(conn, self.db, self.scope, app_version())
            return context.snapshot(
                conn,
                self.scope,
                status,
                self.memory,
                self.workspace,
                local_time=local,
                version=app_version(),
                agent_name=self.settings.agent_name,
                today_spend=today,
                daily_cap=self.settings.daily_spend_cap_usd,
                cycle_cap=self.settings.cycle_spend_cap_usd,
                news=fresh,
            )

    def _call(self, cycle_id: int, purpose: str, request: dict[str, Any]) -> CallResult:
        """One metered call; a failure that cost nothing (no connection, overloaded) is retried once."""
        self._check_stop()
        try:
            return self.meter.call(cycle_id, purpose, request)
        except CallFailed as exc:
            if exc.result.status != "failed" or exc.result.cost_micros:
                raise
            log.info("Call #%d failed without cost (%s); retrying once", exc.result.call_id, exc.result.error)
            if self.stop.wait(RETRY_DELAY_SECONDS):
                raise Stopping from exc
            return self.meter.call(cycle_id, purpose, request)

    # --- plan, act, reflect ---

    def _plan_act_reflect(self, cycle_id: int, ctx: tools.ToolContext) -> CycleEnd:
        snap = self._snapshot()
        self._progress(cycle_id, phase="plan", current_action="Planning this cycle")
        request = None
        for scale in PLANNER_SCALES:
            planner, planned = context.planner_context(snap, self.dry_run, scale)
            request = prompts.plan_request(self.settings, planner)
            if context.fits(request, PLANNER_OPENING.input_tokens):
                break
        else:
            return CycleEnd("failed", "the planning prompt doesn't fit its budget")
        try:
            result = self._call(cycle_id, "plan", request)
        except CallRefused as exc:
            return CycleEnd("refused", f"planning refused: {exc.reason}", rerun=exc.state == "critical")
        except CallFailed as exc:
            return CycleEnd("failed", f"planning failed: {exc.result.error or exc.result.status}")
        response = result.response or {}
        text = _text_of(response)
        self._save_text(result.call_id, text, response)
        stop = response.get("stop_reason")
        if stop == "refusal":
            return CycleEnd("stopped", "the model refused to plan")
        if stop != "end_turn":
            return CycleEnd("failed", f"the plan was cut off ({stop})")
        plan = self._parse_plan(text)
        if plan is None:
            return CycleEnd("failed", "the plan wasn't valid JSON")
        if planned.changelog:
            news.mark_changelog_seen(self.db, self.scope, snap.news)
        focus = None
        with self.db.connection() as conn:
            if plan.focus_project_id is not None:
                focus = store.project(conn, self.scope, plan.focus_project_id)
                if focus is None or focus["status"] not in store.OPEN_STATUSES:
                    plan.focus_project_id, focus = None, None
        ctx.state.focus_project_id = plan.focus_project_id
        self._progress(
            cycle_id,
            plan=json.dumps(plan.to_json(), ensure_ascii=False),
            project_id=plan.focus_project_id,
            current_action=plan.goal[:300] or None,
        )
        if not plan.steps:
            with self.db.transaction() as conn:
                # No brief follows: what the plan showed in full was all the agent needed to see of it.
                news.mark_seen(conn, cycle_id, planned.items)
                store.write_journal(
                    conn,
                    self.scope,
                    cycle_id,
                    "agent",
                    "Nothing worth doing this cycle",
                    plan.assessment,
                    to_iso(self.clock.now()),
                )
            return CycleEnd("idle", "nothing to do", sleep_minutes=plan.sleep_minutes)

        brief, briefed = context.brief(snap, self.dry_run, plan.to_json(), focus, self.settings.max_tool_steps)
        act = self._act(cycle_id, ctx, brief, planned.listed & briefed.items)
        if act.end_reason == "refusal":
            return CycleEnd("stopped", "the model refused to continue")
        if not act.steps:
            # Nothing ran, so there is nothing to reflect on: the system's journal entry says why (_write_report).
            self._progress(cycle_id, act_end_reason=act.end_reason[:300] or None)
            status = "failed" if act.end_reason.startswith("failed") else "refused"
            return CycleEnd(status, act.end_reason.removeprefix(f"{status}: ") or None)
        reflected = self._reflect(cycle_id, ctx, brief, act)
        status = "completed"
        note = act.end_reason if act.end_reason not in ("", "done") else None
        if act.end_reason.startswith("refused"):
            status = "refused"
        elif act.end_reason.startswith("failed"):
            status = "failed"
        if not reflected and status == "completed":
            note = note or "no money left for reflecting"
        if ctx.state.sleep_minutes:  # set_sleep, the last call winning (the reflection's after the act phase's)
            return CycleEnd(status, note, sleep_minutes=ctx.state.sleep_minutes, sleep_reason=ctx.state.sleep_reason)
        return CycleEnd(status, note, sleep_minutes=plan.sleep_minutes)

    def _parse_plan(self, text: str) -> Plan | None:
        data: Any = None
        for candidate in (text, _first_object(text)):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
                break
            except ValueError:
                continue
        if not isinstance(data, dict):
            return None
        steps = data.get("steps")
        steps = [str(s)[:200] for s in steps if isinstance(s, str) and s.strip()][:6] if isinstance(steps, list) else []
        focus = data.get("focus_project_id")
        sleep = data.get("sleep_minutes")
        return Plan(
            assessment=str(data.get("assessment") or "")[:600],
            goal=str(data.get("goal") or "")[:300],
            focus_project_id=focus if isinstance(focus, int) and not isinstance(focus, bool) else None,
            steps=steps,
            sleep_minutes=self._clamp_sleep(sleep) if isinstance(sleep, int) and not isinstance(sleep, bool) else None,
        )

    def _clamp_sleep(self, minutes: int) -> int:
        return max(self.settings.min_sleep_minutes, min(self.settings.max_sleep_minutes, minutes))

    def _act(self, cycle_id: int, ctx: tools.ToolContext, brief: str, seen: frozenset[news.Item]) -> _Act:
        """The work steps; ``seen`` (the owner's items the plan listed and the brief showed in full) is marked once one
        is answered."""
        act = _Act()
        max_steps = self.settings.max_tool_steps
        self._progress(cycle_id, phase="act", max_steps=max_steps, step=0)
        nudged = 0
        for step in range(1, max_steps + 1):
            self._check_stop()
            if ctx.state.strikes >= tools.SANDBOX_STRIKES:
                act.end_reason = "too many refused file operations"
                break
            turns = self._with_pending(act)
            if _size(turns) > MAX_CONVERSATION_BYTES:
                act.end_reason = "the conversation got too long"
                break
            final = step == max_steps
            request = prompts.work_request(self.settings, brief, turns, final=final)
            if not self._affordable(cycle_id, request, brief, turns):
                act.end_reason = "the budget left in this cycle is kept for reflecting" if act.steps else NO_STEP
                break
            self._progress(cycle_id, step=step, current_action=f"Working (step {step} of {max_steps})")
            try:
                result = self._call(cycle_id, "work", request)
            except CallRefused as exc:
                if exc.category in ("cap", "balance"):
                    act.end_reason = f"refused: {exc.reason}"
                    break
                raise EndCycle("stopped", f"refused: {exc.reason}") from exc
            except CallFailed as exc:
                act.end_reason = f"failed: {exc.result.error or exc.result.status}"
                break
            act.steps += 1
            if act.steps == 1:  # the brief reached the model
                with self.db.transaction() as conn:
                    news.mark_seen(conn, cycle_id, seen)
            act.turns = turns
            act.pending = []
            response = result.response or {}
            content = response.get("content") or []
            stop = response.get("stop_reason")
            if not content:
                # The API rejects an empty assistant turn, so it is never sent back: nudge inside the last
                # user turn instead (once), or end the act phase.
                can_nudge = act.turns and act.turns[-1]["role"] == "user" and step < max_steps
                if nudged < MAX_EMPTY_NUDGES and can_nudge:
                    nudged += 1
                    act.turns[-1] = {
                        "role": "user",
                        "content": [*act.turns[-1]["content"], {"type": "text", "text": NUDGE}],
                    }
                    continue
                act.end_reason = "done"
                break
            act.turns.append({"role": "assistant", "content": content})
            uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            text = _text_of(response)
            if text:
                self._save_text(result.call_id, text, response)
            if stop == "tool_use" and uses:
                act.pending = self._run_tools(ctx, uses, result.call_id, "act")
                if step == max_steps:
                    act.end_reason = "step limit reached"
                continue
            if uses:  # e.g. cut off by max_tokens: never run a tool call that may be incomplete
                act.pending = [
                    self._result_block(
                        u,
                        tools.skip(
                            ctx,
                            u.get("name", "?"),
                            u.get("input"),
                            u.get("id", ""),
                            result.call_id,
                            "act",
                            "reply cut off; write in smaller parts",
                        ),
                    )
                    for u in uses
                ]
            if stop == "refusal":
                act.end_reason = "refusal"
                return act
            if stop == "end_turn":
                if not text.strip() and not act.pending and nudged < MAX_EMPTY_NUDGES and step < max_steps:
                    nudged += 1
                    act.turns.append({"role": "user", "content": [{"type": "text", "text": NUDGE}]})
                    continue
                act.report = text
                act.end_reason = "done"
                break
            if stop == "max_tokens":
                if not act.pending:  # the conversation must end with a user turn (no prefill)
                    act.turns.append({"role": "user", "content": [{"type": "text", "text": CUT_OFF}]})
                continue
            act.end_reason = f"the model stopped ({stop})"
            break
        else:
            act.end_reason = act.end_reason or "step limit reached"
        return act

    def _with_pending(self, act: _Act) -> list[dict[str, Any]]:
        if not act.pending:
            return list(act.turns)
        return [*act.turns, {"role": "user", "content": list(act.pending)}]

    def _affordable(self, cycle_id: int, request: dict[str, Any], brief: str, turns: list[dict[str, Any]]) -> bool:
        """Only take a step if a reflect call still fits after it."""
        grown = [*turns, {"role": "assistant", "content": [{"type": "text", "text": "x" * STEP_GROWTH_BYTES}]}]
        try:
            step_cost = self.meter.quote(request)
            reflect_cost = self.meter.quote(prompts.reflect_request(self.settings, brief, grown, []))
        except Unpriceable:
            return False
        return step_cost + reflect_cost <= self.meter.headroom(cycle_id)

    def _run_tools(
        self, ctx: tools.ToolContext, uses: list[dict[str, Any]], call_id: int, phase: str
    ) -> list[dict[str, Any]]:
        results = []
        for index, use in enumerate(uses):
            name, raw, use_id = use.get("name", "?"), use.get("input"), use.get("id", "")
            self._check_stop()
            if index >= MAX_TOOL_CALLS_PER_TURN:
                outcome = tools.skip(
                    ctx, name, raw, use_id, call_id, phase, f"at most {MAX_TOOL_CALLS_PER_TURN} tool calls per turn"
                )
            else:
                with self.db.transaction() as conn:
                    store.update_cycle(conn, ctx.cycle_id, current_action=f"Using {name}"[:300])
                outcome = tools.run(ctx, name, raw, use_id, call_id, phase)
            results.append(self._result_block(use, outcome))
        return results

    @staticmethod
    def _result_block(use: dict[str, Any], outcome: tools.Outcome) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": use.get("id", ""),
            "content": outcome.text or "-",
        }
        if not outcome.ok:
            block["is_error"] = True
        return block

    def _reflect(self, cycle_id: int, ctx: tools.ToolContext, brief: str, act: _Act) -> bool:
        self._check_stop()
        turns = list(act.turns)
        pending = list(act.pending)
        if turns and turns[-1]["role"] == "user":
            # A trailing user turn (tool results plus a nudge, or a nudge alone): keep its tool results,
            # which answer the tool calls before it, and let the reflect prompt replace the rest.
            kept = [b for b in turns.pop()["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
            pending = [*kept, *pending]
        request = prompts.reflect_request(self.settings, brief, turns, pending)
        try:
            if self.meter.quote(request) > self.meter.headroom(cycle_id, "reflect"):
                return False
        except Unpriceable:
            return False
        self._progress(
            cycle_id, phase="reflect", current_action="Reflecting", act_end_reason=act.end_reason[:300] or None
        )
        try:
            result = self._call(cycle_id, "reflect", request)
        except (CallRefused, CallFailed):
            return False
        response = result.response or {}
        text = _text_of(response)
        if text:
            self._save_text(result.call_id, text, response)
        uses = [b for b in (response.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"]
        if response.get("stop_reason") == "tool_use":
            self._run_tools(ctx, uses, result.call_id, "reflect")
        if not ctx.state.journal_written and text.strip():
            with self.db.transaction() as conn:
                first = text.strip().splitlines()[0][:240]
                ctx.state.journal_written = store.write_journal(
                    conn, self.scope, cycle_id, "agent", first, text.strip()[:2000], to_iso(self.clock.now())
                )
        return True

    # --- research (a metered sub-call with Anthropic's web tools) ---

    def _research_fn(self, ctx: tools.ToolContext) -> tools.ResearchFn:
        def research(question: str, url: str | None, cycle_id: int) -> tools.Outcome:
            request = prompts.research_request(self.settings, question, url)
            try:
                quote = self.meter.quote(request)
            except Unpriceable as exc:
                return tools.Outcome(False, f"Error: research can't be priced ({exc}).", "refused: unpriceable")
            if quote > self.meter.headroom(cycle_id):
                return tools.Outcome(
                    False,
                    f"Error: research could cost up to ${micros_to_usd(quote):.3f}, more than this cycle has left.",
                    "refused: budget",
                )
            try:
                result = self._call(cycle_id, "research", request)
            except CallRefused as exc:
                if exc.category in ("state", "system"):
                    raise EndCycle("stopped", f"refused: {exc.reason}") from exc
                return tools.Outcome(False, f"Error: research refused ({exc.reason}).", "refused")
            except CallFailed as exc:
                return tools.Outcome(
                    False, f"Error: research failed ({exc.result.error or exc.result.status}).", "failed"
                )
            response = result.response or {}
            cost = result.cost_micros
            if response.get("stop_reason") == "pause_turn":
                follow = dict(request)
                follow["messages"] = [
                    *request["messages"],
                    {"role": "assistant", "content": response.get("content") or []},
                ]
                try:
                    more = self._call(cycle_id, "research", follow)
                    response = more.response or response
                    cost += more.cost_micros
                except (CallRefused, CallFailed):
                    pass
            digest = _text_of(response)[:2_000] or "Nothing useful was found."
            sources = _sources(response)
            ctx.state.seen_urls.update(sources)
            self._save_text(result.call_id, digest, response)
            body = tools.wrap(ctx, "research", digest)
            source_text = ("\nSources:\n" + "\n".join(f"- {u}" for u in sources[:5])) if sources else ""
            return tools.Outcome(
                True, f"{body}{source_text}\n(cost ${micros_to_usd(cost):.4f})", f"research: {question[:80]}"
            )

        return research

    # --- the last will ---

    def _last_will(self, cycle_id: int) -> CycleEnd:
        snap = self._snapshot()
        self._progress(cycle_id, phase="last_will", current_action="Writing the last will")
        request = prompts.will_request(self.settings, context.will_context(snap, self.dry_run))
        if not context.fits(request, LAST_WILL.input_tokens):
            request = prompts.will_request(self.settings, context.cut(context.will_context(snap, self.dry_run), 2_000))
        try:
            result = self._call(cycle_id, "last_will", request)
        except CallRefused as exc:
            return CycleEnd("refused", f"last will refused: {exc.reason}")
        except CallFailed as exc:
            return CycleEnd("failed", f"last will failed: {exc.result.error or exc.result.status}")
        response = result.response or {}
        text = _text_of(response).strip()
        if not text:
            return CycleEnd("failed", "the last will was empty")
        cut_off = response.get("stop_reason") == "max_tokens"
        now = to_iso(self.clock.now())
        with self.db.transaction() as conn:
            store.save_call_text(conn, result.call_id, text)
            store.save_last_will(conn, self.scope.life_id, cycle_id, result.call_id, text, cut_off, now)
            self.economy.life.record_last_will(conn, self.scope.life_id, now)
            store.write_journal(conn, self.scope, cycle_id, "agent", "Wrote my last will", text[:2000], now)
            events.record(
                self.db, "warning", "agent", f"{self.settings.agent_name} wrote a last will", {"cycle_id": cycle_id}
            )
        return CycleEnd("completed", "last will written")

    def _save_text(self, call_id: int, text: str, response: dict[str, Any]) -> None:
        details = response.get("stop_details") if isinstance(response.get("stop_details"), dict) else None
        with self.db.transaction() as conn:
            store.save_call_text(conn, call_id, text, details)


def _text_of(response: dict[str, Any]) -> str:
    parts = [
        b.get("text", "")
        for b in response.get("content") or []
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return "\n".join(p for p in parts if p)


def _sources(response: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for block in response.get("content") or []:
        if not isinstance(block, dict) or not str(block.get("type", "")).endswith("_tool_result"):
            continue
        content = block.get("content")
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"] not in urls:
                urls.append(item["url"][:300])
    return urls


def _first_object(text: str) -> str | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else None


def _size(turns: list[dict[str, Any]]) -> int:
    return len(json.dumps(turns, ensure_ascii=False).encode("utf-8"))
