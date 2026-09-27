"""The dry-run fake model: response shapes, validation, determinism, cost bounds and scenarios."""

from __future__ import annotations

import ast
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from app.agent import fake_llm, netguard, prompts, tools
from app.agent.fake_llm import (
    CHAOS,
    INJECTIONS,
    SCENARIOS,
    Fail,
    FakeTransport,
    Plan,
    Raw,
    Reply,
    ToolCalls,
    request_kind,
    thinking_signature,
)
from app.config import REFERENCE_PRICES, ModelPrice, Settings
from app.economy.costs import Usage, cost_micros
from app.economy.estimate import plan_request, worst_case_micros
from app.economy.metering import (
    _KNOWN_SERVER_TOOLS,
    _KNOWN_USAGE_KEYS,
    Completed,
    Interrupted,
    NotSent,
    Outcome,
    Rejected,
    rough_token_count,
)
from app.economy.pricing import safety_factor
from tests.economy_helpers import make_economy

SETTINGS = Settings()
HAIKU = REFERENCE_PRICES["claude-haiku-4-5"]
HAIKU_SETTINGS = Settings(planner_model=HAIKU.model, worker_model=HAIKU.model, price_table=(HAIKU,))
RESPONSE_KEYS = {"id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage"}
OPEN = ("idea", "active", "waiting")
_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- contexts in the shape app/agent/context.py builds ---


def _sections(*parts: tuple[str, str]) -> str:
    return "\n\n".join(f"== {title} ==\n{body}" for title, body in parts)


def _status(state: str = "alive") -> str:
    return (
        "Time: Sunday 2026-09-27 10:00 UTC. You are Ember, version 0.3.0. DRY RUN (simulated money).\n"
        f"State: {state}. Balance $12.34. Runway 20.0 days.\n"
        "Spent today $0.10 of $1.00. This cycle may spend up to $0.25."
    )


def _project_lines(projects: dict[int, dict[str, Any]]) -> str:
    lines = [
        f"#{pid} [{p['status']}] {p['title']} · next: {p['next'] or '-'} · spent $0.10 · earned $0.00\n"
        f"   hypothesis: {p['hypothesis']}"
        for pid, p in projects.items()
        if p["status"] in OPEN
    ]
    return "\n".join(lines) or "No open projects."


def planner_context(
    projects: dict[int, dict[str, Any]] | None = None, last_cycle: int | None = None, state: str = "alive"
) -> str:
    news = f"Last cycle #{last_cycle} ended completed." if last_cycle else "Nothing new."
    return _sections(
        ("STATUS", _status(state)),
        ("SINCE YOUR LAST WAKE", news),
        ("OPEN PROJECTS", _project_lines(projects or {})),
        ("WAITING FOR YOUR OWNER", "None."),
        ("STRATEGY", "Start small and honest."),
        ("TASK", "Plan this wake cycle. Reply with the JSON plan only."),
    )


def brief(plan: dict[str, Any], pid: int | None = None, project: dict[str, Any] | None = None) -> str:
    focus = "None."
    if project is not None:
        focus = (
            f"Focus project: #{pid} {project['title']} [{project['status']}]\n"
            f"Hypothesis: {project['hypothesis']}\nNext step: {project['next'] or '-'}\nNotes: -"
        )
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(plan.get("steps", []), 1))
    return _sections(
        ("STATUS", _status()),
        ("PLAN", f"Goal: {plan.get('goal', '')}\n{steps}"),
        ("FOCUS", focus),
        ("LESSONS", "- Keep it small."),
        ("WORKSPACE", "Empty."),
        ("LIMITS", "At most 12 steps this cycle and 4 tool calls per step. Stop when the goal is reached."),
    )


def will_context(projects: dict[int, dict[str, Any]] | None = None) -> str:
    return _sections(
        ("STATUS", _status("critical") + "\nYour money is nearly gone: your last will is due."),
        ("PROJECTS", _project_lines(projects or {})),
        ("RECENT JOURNAL", "- Wrote a first draft\n- Asked my owner to publish"),
        ("TASK", "Write your last will now."),
    )


def text_of(response: dict[str, Any]) -> str:
    return "\n".join(b["text"] for b in response["content"] if b["type"] == "text")


def reflect_prompt_last(request: dict[str, Any]) -> bool:
    return any(
        b["type"] == "text" and b["text"].startswith(prompts.REFLECT_MARKER) for b in request["messages"][-1]["content"]
    )


def parse_plan(text: str) -> dict[str, Any] | None:
    for candidate in (text, (re.search(r"\{.*\}", text, re.DOTALL) or [None])[0]):
        try:
            data = json.loads(candidate) if candidate else None
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# --- a stand-in for the agent loop that follows the API's rules ---


class Sim:
    """Runs wake cycles against the fake: tools run on paper, every call is logged."""

    def __init__(
        self,
        fake: FakeTransport,
        settings: Settings = SETTINGS,
        max_steps: int = 12,
        send: Callable[[dict[str, Any]], Outcome] | None = None,
    ) -> None:
        self.fake = fake
        self.settings = settings
        self.max_steps = max_steps
        self._send = send or fake.send
        self.log: list[tuple[str, dict[str, Any], Outcome]] = []
        self.tool_log: list[tuple[str, str, Any, bool]] = []  # (phase, tool, input, ok)
        self.projects: dict[int, dict[str, Any]] = {}
        self.files: dict[str, str] = {}
        self.cycles = 0
        self.counts: Counter[str] = Counter()

    def send(self, request: dict[str, Any]) -> Outcome:
        outcome = self._send(request)
        self.log.append((request_kind(request), request, outcome))
        assert not (isinstance(outcome, Rejected) and outcome.status == 400), outcome.error
        return outcome

    def cycle(self) -> None:
        self.cycles += 1
        self.counts = Counter()
        clock = self.fake.clock
        if isinstance(clock, Clock):
            clock.advance(3 * 3_600)  # wake cycles are hours apart
        outcome = self.send(prompts.plan_request(self.settings, planner_context(self.projects, self.cycles - 1)))
        if not isinstance(outcome, Completed) or outcome.response["stop_reason"] != "end_turn":
            return
        plan = parse_plan(text_of(outcome.response))
        if plan is None or not plan.get("steps"):
            return
        pid = plan.get("focus_project_id")
        project = self.projects.get(pid) if isinstance(pid, int) else None
        if project is None or project["status"] not in OPEN:
            pid, project = None, None
        text = brief(plan, pid, project)
        turns: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for step in range(1, self.max_steps + 1):
            convo = turns + ([{"role": "user", "content": pending}] if pending else [])
            request = prompts.work_request(self.settings, text, convo, final=step == self.max_steps)
            outcome = self.send(request)
            if not isinstance(outcome, Completed):
                break
            content = outcome.response["content"]
            if not content:
                break  # an empty turn can't be sent back (the API rejects it); reflect carries the results
            turns, pending = [*convo, {"role": "assistant", "content": content}], []
            uses = [b for b in content if b["type"] == "tool_use"]
            if outcome.response["stop_reason"] == "tool_use" and uses:
                pending = [self.result(u, *self.run("act", u, index)) for index, u in enumerate(uses)]
                continue
            pending = [self.result(u, False, "Not executed: reply cut off.") for u in uses]
            break
        if not turns:
            return
        request = prompts.reflect_request(self.settings, text, turns, pending)
        outcome = self.send(request)
        if not isinstance(outcome, Completed) or outcome.response["stop_reason"] != "tool_use":
            return
        uses = [b for b in outcome.response["content"] if b["type"] == "tool_use"]
        results = [self.result(u, *self.run("reflect", u, index)) for index, u in enumerate(uses)]
        assistant = {"role": "assistant", "content": outcome.response["content"]}
        self.send({**request, "messages": [*request["messages"], assistant, {"role": "user", "content": results}]})

    def will(self) -> Outcome:
        return self.send(prompts.will_request(self.settings, will_context(self.projects)))

    @staticmethod
    def result(use: dict[str, Any], ok: bool, text: str) -> dict[str, Any]:
        block = {"type": "tool_result", "tool_use_id": use["id"], "content": text or "-"}
        return block if ok else {**block, "is_error": True}

    def run(self, phase: str, use: dict[str, Any], index: int) -> tuple[bool, str]:
        name, args = use["name"], use["input"]
        ok, text = self._run(phase, name, args, index)
        self.tool_log.append((phase, name, args, ok))
        return ok, text

    def _run(self, phase: str, name: str, args: Any, index: int) -> tuple[bool, str]:
        if index >= 4:
            return False, "Not executed: at most 4 tool calls per turn."
        spec = tools.SPECS.get(name)
        if spec is None:
            return False, f"Error: there is no tool called {name!r}."
        if (phase == "reflect" and not spec.reflect) or (phase == "act" and not spec.act):
            return False, f"Error: {name} can't be used now."
        if self.counts[name] >= spec.per_cycle:
            return False, f"Error: {name} can be used at most {spec.per_cycle} times per cycle."
        try:
            args = tools.validate(spec, args)
        except tools.ToolError as exc:
            return False, f"Error: {exc}."
        path = args.get("path", "")
        if path and not (len(path.split("/")) <= 4 and all(_PATH_PART.match(p) for p in path.split("/"))):
            return False, "Error: that path is outside your workspace."
        self.counts[name] += 1
        if name == "workspace_list":
            listing = "\n".join(f"{p}  {len(c):,} B" for p, c in sorted(self.files.items())) or "(empty)"
            return True, f"{listing}\nUsing 0.1 KB of 5 MB, {len(self.files)}/300 files"
        if name == "workspace_read":
            if path not in self.files:
                return False, f"Error: {path} doesn't exist."
            return True, f'{path}\n<data src="workspace:{path}" id="abc123">\n{self.files[path]}\n</data id="abc123">'
        if name == "workspace_write":
            if args["mode"] == "create" and path in self.files:
                return False, f"Error: {path} already exists."
            if args["mode"] == "delete":
                self.files.pop(path, None)
                return True, f"Deleted {path}."
            content = args.get("content") or ""
            self.files[path] = (self.files.get(path, "") if args["mode"] == "append" else "") + content
            return True, f"Wrote {path} ({len(content):,} bytes)."
        if name == "project_create":
            pid = max(self.projects, default=0) + 1
            self.projects[pid] = {"title": args["title"], "hypothesis": args["hypothesis"], "status": args["status"]}
            self.projects[pid]["next"] = args["next_step"]
            return True, f"Created project #{pid}."
        if name == "project_update":
            project = self.projects.get(args["project_id"])
            if project is None or project["status"] not in OPEN:
                return False, f"Error: there is no open project #{args['project_id']}."
            project["status"] = args.get("status", project["status"])
            project["next"] = args.get("next_step", project["next"])
            return True, f"Project #{args['project_id']}: updated."
        if name == "research":
            return self.research(args["question"], args.get("url"))
        return True, f"{name}: done."

    def research(self, question: str, url: str | None) -> tuple[bool, str]:
        request = prompts.research_request(self.settings, question, url)
        outcome = self.send(request)
        if isinstance(outcome, Completed) and outcome.response["stop_reason"] == "pause_turn":
            follow = {**request, "messages": [*request["messages"], {"role": "assistant", "content": []}]}
            follow["messages"][-1]["content"] = outcome.response["content"]
            outcome = self.send(follow)
        if not isinstance(outcome, Completed):
            return False, "Error: research failed."
        digest = text_of(outcome.response)[:2_000] or "Nothing useful was found."
        return True, f'<data src="research" id="abc123">\n{digest}\n</data id="abc123">'


# --- a corpus of simulated cycles, shared by the property tests ---


@cache
def corpus(settings: Settings = SETTINGS, seeds: int = 20, cycles: int = 3) -> dict[str, list[Sim]]:
    sims: dict[str, list[Sim]] = {scenario: [] for scenario in SCENARIOS}
    for seed in range(seeds):
        for scenario in SCENARIOS:
            sim = Sim(FakeTransport(seed=seed, scenario=scenario, clock=Clock()), settings)
            for _ in range(cycles):
                sim.cycle()
                sim.will()
            sims[scenario].append(sim)
    return sims


def all_calls(sims: dict[str, list[Sim]]) -> list[tuple[str, dict[str, Any], Outcome]]:
    return [entry for group in sims.values() for sim in group for entry in sim.log]


def worst_case(request: dict[str, Any], price: ModelPrice, settings: Settings = SETTINGS) -> int:
    return worst_case_micros(plan_request(request, rough_token_count(request)), price, settings.web_search_usd_per_1000)


def priced(usage: dict[str, Any], price: ModelPrice, settings: Settings = SETTINGS) -> int:
    return cost_micros(Usage.from_api(usage, "5m"), price, settings.web_search_usd_per_1000)


# --- shapes ---


def test_every_kind_of_answer_has_the_messages_api_shape() -> None:
    sims = corpus()["founder"][:10]
    seen: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for sim in sims:
        for kind, request, outcome in sim.log:
            assert isinstance(outcome, Completed)
            response = outcome.response
            assert set(response) == RESPONSE_KEYS
            assert (response["type"], response["role"], response["stop_sequence"]) == ("message", "assistant", None)
            assert response["model"] == request["model"]
            assert response["id"].startswith("msg_fake_") and outcome.request_id.startswith("req_fake_")
            usage = response["usage"]
            assert (usage["service_tier"], usage["inference_geo"]) == ("standard", "global")
            assert usage["output_tokens_details"] == {"thinking_tokens": 0}
            assert usage["input_tokens"] >= 1 and 1 <= usage["output_tokens"] <= request["max_tokens"]
            assert ("cache_read_input_tokens" in usage) == ("cache_control" in json.dumps(request))
            assert ("server_tool_use" in usage) == (kind == "research")
            seen.setdefault(kind, []).append((request, response))
    assert set(seen) == {"plan", "work", "research", "reflect", "will"}

    for _, response in seen["plan"]:
        assert response["stop_reason"] == "end_turn" and [b["type"] for b in response["content"]] == ["text"]
        plan = json.loads(response["content"][0]["text"])
        assert set(plan) == set(prompts.PLAN_SCHEMA["required"]) == set(prompts.PLAN_SCHEMA["properties"])
        assert isinstance(plan["assessment"], str) and isinstance(plan["goal"], str)
        assert plan["focus_project_id"] is None or isinstance(plan["focus_project_id"], int)
        assert all(isinstance(s, str) for s in plan["steps"]) and 2 <= len(plan["steps"]) <= 5
        assert isinstance(plan["sleep_minutes"], int)

    for _, response in seen["work"]:
        uses = [b for b in response["content"] if b["type"] == "tool_use"]
        if response["stop_reason"] == "tool_use":
            assert 1 <= len(uses) <= 2
            assert all(b["id"].startswith("toolu_fake_") and b["name"] in tools.SPECS for b in uses)
        else:
            assert response["stop_reason"] == "end_turn" and not uses and text_of(response).startswith("Report:")

    for request, response in seen["research"]:
        assert response["stop_reason"] == "end_turn"
        types = [b["type"] for b in response["content"]]
        fetch = request["tools"][0]["name"] == "web_fetch"
        if fetch:
            assert types == ["server_tool_use", "web_fetch_tool_result", "text"]
            assert response["usage"]["server_tool_use"] == {"web_fetch_requests": 1}
            continue
        assert types == ["server_tool_use", "web_search_tool_result", "text"]
        use, result, digest = response["content"]
        assert use["name"] == "web_search" and result["tool_use_id"] == use["id"] and use["input"]["query"]
        assert 2 <= len(result["content"]) <= 3
        for item in result["content"]:
            assert item["type"] == "web_search_result" and item["url"].startswith("https://example.invalid/")
            assert item["title"].startswith("[simulated]") and item["url"] in digest["text"]
        assert response["usage"]["server_tool_use"] == {"web_search_requests": 1}

    reflections = [r for q, r in seen["reflect"] if reflect_prompt_last(q)]
    for response in reflections:
        names = [b["name"] for b in response["content"] if b["type"] == "tool_use"]
        assert response["stop_reason"] == "tool_use" and names.count("write_journal") == 1
        assert all(tools.SPECS[n].reflect for n in names)
    closing = [r for q, r in seen["reflect"] if not reflect_prompt_last(q)]
    assert closing and all(r["stop_reason"] == "end_turn" and text_of(r) for r in closing)

    for _, response in seen["will"]:
        assert response["stop_reason"] == "end_turn" and [b["type"] for b in response["content"]] == ["text"]
        assert "last will" in response["content"][0]["text"]


def test_a_fetch_is_emulated_too() -> None:
    fake = FakeTransport()
    request = prompts.research_request(SETTINGS, "What do guides cost?", "https://shop.example/guides")
    outcome = fake.send(request)
    assert isinstance(outcome, Completed)
    use, result, digest = outcome.response["content"]
    assert use == {
        **use,
        "type": "server_tool_use",
        "name": "web_fetch",
        "input": {"url": "https://shop.example/guides"},
    }
    assert result["type"] == "web_fetch_tool_result" and result["content"]["url"] == "https://shop.example/guides"
    assert result["content"]["content"]["source"]["type"] == "text" and "[simulated]" in digest["text"]
    assert outcome.response["usage"]["server_tool_use"] == {"web_fetch_requests": 1}


def test_a_plain_request_gets_a_plain_answer() -> None:
    request = {"model": "claude-sonnet-5", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}
    outcome = FakeTransport().send(request)
    assert isinstance(outcome, Completed) and outcome.response["stop_reason"] == "end_turn"
    assert text_of(outcome.response) and outcome.response["usage"]["output_tokens"] <= 50


# --- determinism ---


def test_the_same_requests_get_the_same_answers() -> None:
    def run(seed: int, scenario: str) -> list[Outcome]:
        sim = Sim(FakeTransport(seed=seed, scenario=scenario, clock=Clock()))
        for _ in range(3):
            sim.cycle()
        sim.will()
        return [outcome for _, _, outcome in sim.log]

    for scenario in SCENARIOS:
        assert run(7, scenario) == run(7, scenario)
    assert run(7, "founder") != run(8, "founder")

    fake = FakeTransport(seed=3)
    request = prompts.work_request(SETTINGS, "Some brief.", [])
    first, second = fake.send(request), fake.send(request)
    assert isinstance(first, Completed) and isinstance(second, Completed)
    assert first.response["content"] == second.response["content"] and first.response["id"] == second.response["id"]
    # Only the usage differs: the second call reads the prompt from the (simulated) cache.
    assert second.response["usage"]["cache_read_input_tokens"] > first.response["usage"]["cache_read_input_tokens"]


# --- the validator ---

USE = {"type": "tool_use", "id": "toolu_1", "name": "workspace_list", "input": {}}
RESULT = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "(empty)"}
TEXT = {"type": "text", "text": "ok"}
MARK = {"type": "ephemeral"}


def turn(role: str, *blocks: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "content": list(blocks)}


def work(*turns: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**prompts.work_request(SETTINGS, "== FOCUS ==\nNone.", list(turns)), **extra}


BAD_REQUESTS = {
    "two user turns": (work(turn("user", TEXT)), "alternate"),
    "starts with the assistant": (
        {**work(), "messages": [turn("assistant", TEXT), turn("user", TEXT)]},
        "alternate",
    ),
    "trailing assistant turn": (work(turn("assistant", TEXT)), "prefill"),
    "tool_use left open at the end": (work(turn("assistant", TEXT, USE)), "without tool_result"),
    "tool_use not answered": (work(turn("assistant", USE), turn("user", TEXT)), "without tool_result"),
    "tool_result not first": (work(turn("assistant", USE), turn("user", TEXT, RESULT)), "must come before"),
    "tool_result with the wrong id": (
        work(turn("assistant", USE), turn("user", {**RESULT, "tool_use_id": "toolu_2"})),
        "without tool_result",
    ),
    "an extra tool_result": (
        work(turn("assistant", USE), turn("user", RESULT, {**RESULT, "tool_use_id": "toolu_9"})),
        "unexpected tool_use_id",
    ),
    "tool_result without tool_use": (work(turn("assistant", TEXT), turn("user", RESULT)), "unexpected tool_use_id"),
    "five cache breakpoints": (
        work(
            turn("assistant", {**TEXT, "cache_control": MARK}, USE),
            turn("user", {**RESULT, "cache_control": MARK}, {**TEXT, "cache_control": MARK}),
        ),
        "maximum of 4",
    ),
    "empty text block": (work(turn("assistant", TEXT), turn("user", {"type": "text", "text": ""})), "non-whitespace"),
    "whitespace text block": (
        work(turn("assistant", TEXT), turn("user", {"type": "text", "text": " \n"})),
        "non-white",
    ),
    "empty system text": ({**work(), "system": [{"type": "text", "text": ""}]}, "non-whitespace"),
    "empty text in a tool result": (
        work(turn("assistant", USE), turn("user", {**RESULT, "content": [{"type": "text", "text": ""}]})),
        "non-whitespace",
    ),
    "empty assistant turn (a nudge after an empty reply)": (
        work(turn("assistant"), turn("user", TEXT)),
        "non-empty content",
    ),
    "temperature": (work(temperature=0.2), "sampling"),
    "top_p": (work(top_p=0.9), "sampling"),
    "top_k": (work(top_k=5), "sampling"),
    "forced tool on opus-5-5": (
        work(model="claude-opus-5-5", thinking={"type": "adaptive"}, tool_choice={"type": "any"}),
        "not supported",
    ),
    "thinking disabled on opus-5-5": (work(model="claude-opus-5-5"), "adaptive"),
    "budget_tokens on sonnet-5": (work(thinking={"type": "enabled", "budget_tokens": 1_024}), "budget_tokens"),
    "edited thinking block": (
        work(
            turn("assistant", {"type": "thinking", "thinking": "edited", "signature": thinking_signature("original")}),
            turn("user", TEXT),
        ),
        "cannot be modified",
    ),
    "tool blocks without tools": (
        {"model": "claude-sonnet-5", "max_tokens": 50, "messages": [turn("user", TEXT), turn("assistant", USE)]},
        "must define tools",
    ),
}


@pytest.mark.parametrize("name", BAD_REQUESTS)
def test_requests_the_api_would_reject_get_a_400(name: str) -> None:
    request, reason = BAD_REQUESTS[name]
    assert fake_llm.validate_request(request) is not None
    fake = FakeTransport(script=[Reply("scripted")])
    outcome = fake.send(request)
    assert isinstance(outcome, Rejected) and outcome.status == 400 and re.search(reason, outcome.error), outcome
    assert len(fake.script) == 1  # a rejected request doesn't use up a scripted turn


GOOD_REQUESTS = {
    "tool results then text": work(turn("assistant", TEXT, USE), turn("user", RESULT, TEXT)),
    "four cache breakpoints": work(
        turn("assistant", {**TEXT, "cache_control": MARK}, USE), turn("user", {**RESULT, "cache_control": MARK})
    ),
    "a signed thinking block": work(
        turn("assistant", {"type": "thinking", "thinking": "hm", "signature": thinking_signature("hm")}, TEXT),
        turn("user", TEXT),
    ),
    "a pause_turn continuation": {
        **prompts.research_request(SETTINGS, "q", None),
        "messages": [
            turn("user", {"type": "text", "text": "Question: q"}),
            turn("assistant", {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}),
        ],
    },
    "every request the agent builds": prompts.plan_request(SETTINGS, "context"),
    "the reflect turn": prompts.reflect_request(SETTINGS, "brief", [turn("assistant", TEXT, USE)], [RESULT]),
    "the last will": prompts.will_request(SETTINGS, "context"),
}


@pytest.mark.parametrize("name", GOOD_REQUESTS)
def test_valid_requests_are_answered(name: str) -> None:
    assert fake_llm.validate_request(GOOD_REQUESTS[name]) is None
    assert isinstance(FakeTransport().send(GOOD_REQUESTS[name]), Completed)


# --- tool inputs, costs and usage keys over many simulated cycles ---


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s != "chaos"])
def test_every_tool_call_is_valid(scenario: str) -> None:
    calls = [entry for sim in corpus()[scenario] for entry in sim.tool_log]
    if scenario != "idle":
        assert len(calls) > 200
        assert {name for _, name, _, _ in calls} >= {
            "workspace_list",
            "project_create",
            "research",
            "workspace_write",
            "project_update",
            "request_approval",
            "set_sleep",
            "write_journal",
            "memory_update",
        } - (set() if scenario in ("founder", "injection") else {"request_approval"})
    for phase, name, args, _ in calls:
        spec = tools.SPECS[name]
        assert spec.reflect if phase == "reflect" else spec.act, (phase, name)
        tools.validate(spec, args)  # raises ToolError if invalid
        if name == "workspace_write" and scenario != "drain" and args["path"].startswith("projects/"):
            assert 800 <= len(args["content"]) <= 2_500
        if name == "request_approval":
            assert args["type"] == "publish" and args["expected_cost"] == "none"
            assert "written by an AI" in args["payload"]
        if name == "write_journal":
            assert phase == "reflect"


def test_tool_calls_mostly_succeed_in_the_founder_scenario() -> None:
    results = Counter(ok for sim in corpus()["founder"] for _, _, _, ok in sim.tool_log)
    assert results[False] <= results[True] / 20
    parallel = [
        o
        for sim in corpus()["founder"]
        for kind, _, o in sim.log
        if kind == "work" and sum(b["type"] == "tool_use" for b in o.response["content"]) == 2
    ]
    assert parallel  # sometimes two calls in one turn


@pytest.mark.parametrize("settings", [SETTINGS, HAIKU_SETTINGS], ids=["sonnet-5", "haiku-4-5"])
def test_a_call_never_costs_more_than_the_guards_worst_case(settings: Settings) -> None:
    price = settings.price_for(settings.worker_model)
    assert price is not None
    sims = corpus(settings) if settings is SETTINGS else corpus(settings, seeds=5)
    counts: Counter[str] = Counter()
    for kind, request, outcome in all_calls(sims):
        if isinstance(outcome, Completed):
            usage = outcome.response["usage"]
        elif isinstance(outcome, Interrupted) and outcome.partial_usage:
            usage = outcome.partial_usage
        else:
            continue
        counts[kind] += 1
        assert priced(usage, price, settings) <= worst_case(request, price, settings), (kind, usage)
        prompt = usage["input_tokens"] + usage.get("cache_read_input_tokens", 0)
        prompt += usage.get("cache_creation_input_tokens", 0)
        if kind != "research":
            assert prompt <= rough_token_count(request) - 1
        assert usage["output_tokens"] <= request["max_tokens"]
    if settings is SETTINGS:
        assert min(counts[k] for k in ("plan", "work", "research", "reflect", "will")) >= 300, counts


def test_answers_report_only_usage_keys_the_guard_knows() -> None:
    allowed = _KNOWN_USAGE_KEYS - {"iterations"}
    assert allowed >= fake_llm.USAGE_KEYS
    for _, _, outcome in all_calls(corpus()):
        if isinstance(outcome, Interrupted):
            assert outcome.partial_usage is None or set(outcome.partial_usage) <= allowed
        if not isinstance(outcome, Completed):
            continue
        response = outcome.response
        assert set(response) - {"stop_details"} == RESPONSE_KEYS
        assert ("stop_details" in response) == (response["stop_reason"] == "refusal")
        usage = response["usage"]
        assert set(usage) <= allowed and "iterations" not in usage
        assert all(isinstance(v, int) and v >= 0 for v in usage.get("cache_creation", {}).values())
        assert set(usage.get("server_tool_use", {})) <= _KNOWN_SERVER_TOOLS
        assert usage["service_tier"] == "standard" and usage["inference_geo"] == "global"
        assert usage["input_tokens"] >= 1


def test_the_budget_guard_settles_every_answer_without_doubt(data_dir: Path) -> None:
    economy = make_economy(data_dir, Settings(starting_balance_usd=50, daily_spend_cap_usd=10, cycle_spend_cap_usd=5))
    fake = FakeTransport(seed=11, clock=Clock())
    model = economy.metered(fake)
    cycle = model.open_cycle("test")
    purposes = {"plan": "plan", "work": "work", "research": "research", "reflect": "reflect", "will": "last_will"}
    results = []

    def send(request: dict[str, Any]) -> Outcome:
        result = model.call(cycle, purposes[request_kind(request)], request)
        results.append(result)
        return Completed(result.response or {}, "req")

    sim = Sim(fake, send=send)
    for _ in range(3):
        sim.cycle()
    sim.will()
    assert {request_kind(r) for _, r, _ in sim.log} == set(purposes)
    for result in results:
        assert result.status == "ok" and not result.billing_uncertain and not result.overrun
        assert result.cost_micros <= result.estimate_micros
    assert safety_factor(economy.db, SETTINGS.worker_model, "dry_run") == 1
    economy.stop()


# --- prompt caching ---


def usage_of(outcome: Outcome) -> dict[str, Any]:
    assert isinstance(outcome, Completed)
    return outcome.response["usage"]


def test_the_prompt_cache_is_simulated() -> None:
    clock = Clock()
    fake = FakeTransport(clock=clock)
    request = prompts.work_request(SETTINGS, "A brief. " * 20, [])
    first = usage_of(fake.send(request))
    written = first["cache_creation_input_tokens"]
    assert written > 1_024 and first["cache_read_input_tokens"] == 0
    assert first["cache_creation"] == {"ephemeral_5m_input_tokens": written, "ephemeral_1h_input_tokens": 0}
    assert first["input_tokens"] + written <= rough_token_count(request) - 1

    clock.advance(299)
    second = usage_of(fake.send(request))
    assert (second["cache_read_input_tokens"], second["cache_creation_input_tokens"]) == (written, 0)

    # The next step reads the shared prefix and writes only what is new.
    step = prompts.work_request(SETTINGS, "A brief. " * 20, [turn("assistant", TEXT, USE), turn("user", RESULT)])
    third = usage_of(fake.send(step))
    assert third["cache_read_input_tokens"] == written and third["cache_creation_input_tokens"] > 0

    # A different tool_choice keeps only the tools and system part.
    final = usage_of(fake.send(prompts.work_request(SETTINGS, "A brief. " * 20, [], final=True)))
    assert 1_024 < final["cache_read_input_tokens"] < written

    clock.advance(301)  # past the 5-minute lifetime
    expired = usage_of(fake.send(request))
    assert (expired["cache_read_input_tokens"], expired["cache_creation_input_tokens"]) == (0, written)


def test_short_prompts_and_haiku_minimums_are_not_cached() -> None:
    research = usage_of(FakeTransport().send(prompts.research_request(SETTINGS, "What sells?", None)))
    assert research["cache_creation_input_tokens"] == research["cache_read_input_tokens"] == 0
    request = prompts.work_request(HAIKU_SETTINGS, "A brief.", [])
    fake = FakeTransport()
    assert fake_llm.prompt_tokens(request) < 4_096
    for _ in range(2):
        usage = usage_of(fake.send(request))
        assert usage["cache_creation_input_tokens"] == usage["cache_read_input_tokens"] == 0
    sonnet = usage_of(fake.send(prompts.work_request(SETTINGS, "A brief.", [])))
    assert sonnet["cache_creation_input_tokens"] > 0


def test_one_hour_cache_writes_are_reported_separately() -> None:
    request = {**prompts.plan_request(SETTINGS, planner_context()), "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    usage = usage_of(FakeTransport().send(request))
    assert usage["cache_creation"]["ephemeral_1h_input_tokens"] == usage["cache_creation_input_tokens"] > 0
    price = SETTINGS.price_for(SETTINGS.planner_model)
    assert price is not None and priced(usage, price) <= worst_case(request, price)


# --- no network, delays ---


def test_it_runs_with_the_network_and_processes_sealed() -> None:
    stop = threading.Event()
    with netguard.sealed():
        sim = Sim(FakeTransport(seed=3, delay_ms=1, stop=stop, clock=Clock()))
        sim.cycle()
        sim.will()
    assert {kind for kind, _, _ in sim.log} == {"plan", "work", "research", "reflect", "will"}
    assert all(isinstance(outcome, Completed) for _, _, outcome in sim.log)


def test_the_module_imports_nothing_that_could_reach_out() -> None:
    tree = ast.parse(Path(fake_llm.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    allowed = {"__future__", "collections", "copy", "dataclasses", "hashlib", "json", "random", "re", "threading"}
    assert imported <= allowed | {"time", "typing"}
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"eval", "exec", "open", "__import__", "compile"}


def test_the_delay_can_be_interrupted() -> None:
    request = prompts.will_request(SETTINGS, will_context())
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    outcome = FakeTransport(delay_ms=10_000, stop=stop).send(request)
    assert isinstance(outcome, Interrupted) and time.monotonic() - started < 1
    started = time.monotonic()
    assert isinstance(FakeTransport(delay_ms=30).send(request), Completed)
    assert time.monotonic() - started >= 0.03


def test_unknown_scenarios_are_refused() -> None:
    with pytest.raises(ValueError, match="scenario"):
        FakeTransport(scenario="party")
    assert FakeTransport().count_tokens(prompts.will_request(SETTINGS, "x")) == rough_token_count(
        prompts.will_request(SETTINGS, "x")
    )


# --- scenarios ---


def test_founder_asks_for_approval_every_third_cycle_and_writes_real_drafts() -> None:
    for sim in corpus()["founder"][:20]:
        approvals = [args for phase, name, args, ok in sim.tool_log if name == "request_approval"]
        assert len(approvals) <= 1  # three cycles: only the third plans one
        drafts = [a["content"] for _, name, a, _ in sim.tool_log if name == "workspace_write"]
        assert drafts and all(c.startswith("# ") for c in drafts)
    assert any(name == "request_approval" for sim in corpus()["founder"] for _, name, _, _ in sim.tool_log)
    assert any(name == "message_owner" for sim in corpus()["founder"] for _, name, _, _ in sim.tool_log)


def answer(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    outcome = FakeTransport(**kwargs).send(request)
    assert isinstance(outcome, Completed), outcome
    return outcome.response


def calls_in(response: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(b["name"], b["input"]) for b in response["content"] if b["type"] == "tool_use"]


def test_the_founder_follows_the_context_and_the_conversation() -> None:
    projects = {3: {"title": "Tea tasting notes", "hypothesis": "h", "status": "active", "next": "research"}}
    plan = json.loads(text_of(answer(prompts.plan_request(SETTINGS, planner_context(projects, last_cycle=5)))))
    assert plan["focus_project_id"] == 3 and any("approv" in s for s in plan["steps"])  # cycle 6
    fresh = json.loads(text_of(answer(prompts.plan_request(SETTINGS, planner_context()))))
    assert fresh["focus_project_id"] is None and fresh["steps"][0].startswith("Create a project: ")

    # No focus project: look around and start one, in parallel. With one: only look around.
    no_focus = brief(fresh)
    assert [n for n, _ in calls_in(answer(work_on(no_focus)))] == ["workspace_list", "project_create"]
    focused = brief(plan, 3, projects[3])
    assert [n for n, _ in calls_in(answer(work_on(focused)))] == ["workspace_list"]

    # A failed write is retried once as a shorter draft; three failures end the act phase.
    write = {"type": "tool_use", "id": "toolu_w", "name": "workspace_write", "input": {"path": "projects/tea.md"}}
    failed = {"type": "tool_result", "tool_use_id": "toolu_w", "content": "Error: too big.", "is_error": True}
    retry = calls_in(answer(work_on(focused, turn("assistant", write), turn("user", failed))))
    assert retry[0][0] == "workspace_write" and retry[0][1]["path"].startswith("notes/")
    uses = [{**write, "id": f"toolu_{i}"} for i in range(3)]
    results = [{**failed, "tool_use_id": f"toolu_{i}"} for i in range(3)]
    gave_up = answer(work_on(focused, turn("assistant", *uses), turn("user", *results)))
    assert gave_up["stop_reason"] == "end_turn" and "3 tool call(s) failed" in text_of(gave_up)

    # A brief without any of the expected sections still gets valid tool calls.
    for name, args in calls_in(answer(work_on("hi"))):
        tools.validate(tools.SPECS[name], args)
    # The final step (tool_choice none) is answered with a report, never a tool call.
    final = answer({**work_on(focused), "tool_choice": {"type": "none"}})
    assert final["stop_reason"] == "end_turn" and not calls_in(final)


def work_on(text: str, *turns: dict[str, Any]) -> dict[str, Any]:
    return prompts.work_request(SETTINGS, text, list(turns))


def test_idle_plans_have_no_steps() -> None:
    for sim in corpus()["idle"]:
        plans = [json.loads(text_of(o.response)) for kind, _, o in sim.log if kind == "plan"]
        assert plans and all(p["steps"] == [] for p in plans)
        assert {kind for kind, _, _ in sim.log} == {"plan", "will"}


def test_drain_answers_come_close_to_max_tokens() -> None:
    for sim in corpus()["drain"][:20]:
        for kind, request, outcome in sim.log:
            assert isinstance(outcome, Completed)
            if kind != "plan":
                assert outcome.response["usage"]["output_tokens"] >= 0.85 * request["max_tokens"], kind
            else:
                assert json.loads(text_of(outcome.response))["sleep_minutes"] == 5


def test_flaky_fails_every_fourth_call() -> None:
    fake = FakeTransport(scenario="flaky")
    request = prompts.work_request(SETTINGS, "A brief.", [])
    outcomes = [fake.send(request) for _ in range(12)]
    assert isinstance(outcomes[3], NotSent)
    assert isinstance(outcomes[7], Rejected) and outcomes[7].status == 529 and "overloaded" in outcomes[7].error
    interrupted = outcomes[11]
    assert isinstance(interrupted, Interrupted) and interrupted.partial_usage
    assert set(interrupted.partial_usage) == {"input_tokens", "output_tokens"}
    assert all(isinstance(o, Completed) for i, o in enumerate(outcomes) if i not in (3, 7, 11))


def test_chaos_produces_every_kind_of_misbehaviour() -> None:
    sims = corpus()["chaos"]
    notes = {note for sim in sims for _, _, note in sim.fake.trace}
    assert {f"chaos: {c}" for kinds in CHAOS.values() for c in kinds} <= notes
    inputs = [(name, args, ok) for sim in sims for _, name, args, ok in sim.tool_log]
    assert any(".." in json.dumps(args) and not ok for _, args, ok in inputs)
    assert any(name == "workspace_write" and len(str(args.get("content"))) > 6_000 for name, args, _ in inputs)
    assert any(name not in tools.SPECS for name, _, _ in inputs)
    responses = [(kind, o.response) for sim in sims for kind, _, o in sim.log if isinstance(o, Completed)]
    stops = Counter((kind, r["stop_reason"]) for kind, r in responses)
    assert stops[("work", "max_tokens")] and stops[("work", "refusal")] and stops[("research", "pause_turn")]
    assert any(kind == "work" and not r["content"] and r["stop_reason"] == "end_turn" for kind, r in responses)
    assert any(r.get("stop_details", {}).get("type") == "refusal" for _, r in responses)
    assert any(kind == "plan" and parse_plan(text_of(r)) is None for kind, r in responses if r["content"])


def test_injected_text_flows_through_but_the_fake_agent_behaves() -> None:
    sims = corpus()["injection"]
    digests = [text_of(o.response) for sim in sims for kind, _, o in sim.log if kind == "research"]
    assert any(INJECTIONS[1] in d for d in digests)
    writes = [a["content"] for sim in sims for _, name, a, _ in sim.tool_log if name == "workspace_write"]
    assert writes and all(INJECTIONS[0] in w for w in writes)
    assert any(name == "workspace_read" for sim in sims for _, name, _, _ in sim.tool_log)
    for sim in sims:
        for _, name, args, _ in sim.tool_log:
            assert name in tools.SPECS
            text = json.dumps(args)
            if name != "workspace_write":
                assert "options.json" not in text and ".." not in text and "spend_money" not in text
            if name == "request_approval":
                assert args["type"] == "publish"


# --- scripts ---


def test_scripted_turns_come_first_then_the_scenario_takes_over() -> None:
    plan = {"assessment": "a", "goal": "g", "focus_project_id": None, "steps": ["x"], "sleep_minutes": 60}
    raw = {"id": "msg_raw", "type": "message", "content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
    fake = FakeTransport(
        script=[
            Plan(plan),
            ToolCalls([("workspace_list", {}), ("set_sleep", {"minutes": 60, "reason": "r"})], text="Looking."),
            Reply("All done."),
            Raw(raw),
            Fail(NotSent("down")),
            Plan("not json"),
        ]
    )
    first = fake.send(prompts.plan_request(SETTINGS, "context"))
    assert isinstance(first, Completed) and json.loads(text_of(first.response)) == plan
    second = fake.send(work())
    assert isinstance(second, Completed) and second.response["stop_reason"] == "tool_use"
    blocks = second.response["content"]
    assert blocks[0] == {"type": "text", "text": "Looking."} and [b["name"] for b in blocks[1:]] == [
        "workspace_list",
        "set_sleep",
    ]
    third = fake.send(work())
    assert isinstance(third, Completed) and text_of(third.response) == "All done."
    assert third.response["stop_reason"] == "end_turn"
    fourth = fake.send(work())
    assert isinstance(fourth, Completed) and fourth.response == raw
    assert fake.send(work()) == NotSent("down")
    sixth = fake.send(prompts.plan_request(SETTINGS, "context"))
    assert isinstance(sixth, Completed) and text_of(sixth.response) == "not json"
    assert not fake.script
    after = fake.send(prompts.plan_request(SETTINGS, planner_context()))
    assert isinstance(after, Completed) and parse_plan(text_of(after.response)) is not None
