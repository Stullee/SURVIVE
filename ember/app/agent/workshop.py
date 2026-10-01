"""The workshop (0.7.0): code the agent needs, written and run in Anthropic's code execution sandbox.

The agent describes what it needs (the ``workshop`` tool). A separate model call with Anthropic's code execution
tool writes a Python script and runs it in a container on Anthropic's servers: no internet there, and nothing runs
on the owner's Home Assistant. Ember's code then downloads the files the run made, checks them
(``products.checks``) and keeps them in the workspace, the script in ``workshop/scripts/`` so the agent can run it
again (``script``). Every run is a row in ``workshop_runs``.

A script that proves useful is how Ember grows: the planner points at a script that was run again or whose files
went into a request the owner approved, and the agent asks for it to be built into Ember (``request_upgrade`` with
``workshop_script``), where it then costs nothing to run.

Money: every call goes through the budget guard. A workshop call has its own cap per run and counts toward the
daily cap and the balance, not toward the cycle cap. The Files API is free: inputs are uploaded for one run (and
expire within the hour), and every file is deleted from Anthropic once the run is over.

0.14.0: a run's worst case is a price, not a ceiling: nothing bounds what its code runs print or look at (live, a run
cost $1.84 against $0.35). So a call is refused when its price is above what is left of the cap per run, and it
holds at least that cap, or what recent runs cost if that is more, of the daily cap and the balance
(metering.workshop_reservation). A call can still cost more than all of that; the guard books it, refuses further
workshop calls in the cycle and raises the workshop's estimates.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..config import Settings
from ..db import Database
from ..economy.clock import Clock, to_iso
from ..economy.costs import micros_to_usd
from ..economy.estimate import Unpriceable
from ..economy.metering import WORKSHOP, CallFailed, CallRefused, FilesError, MeteredModel, usd_cap_to_micros
from ..products import checks
from . import netguard, prompts, store
from .sandbox import MAX_DEPTH, NAME, Jail, SandboxError, kind_of
from .store import AgentScope
from .tools import WORKSHOP_INPUT_MB, WORKSHOP_INPUTS

log = logging.getLogger(__name__)

MAX_INPUTS = WORKSHOP_INPUTS  # the workshop tool's description states them (0.12.0: one source)
MAX_INPUT_BYTES = WORKSHOP_INPUT_MB * 1024 * 1024
MAX_OUTPUTS = 12
MAX_LOOKUPS = 3 * MAX_OUTPUTS  # files looked at: a file copied out in several commands comes back each time
MAX_CONTINUATIONS = 2  # a paused run (pause_turn) is continued at most twice, each a metered call
SCRIPTS = "workshop/scripts"
DEFAULT_FOLDER = "workshop/out"
ANSWER_CHARS = 1_500
MIME_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".html": "text/html",
    ".css": "text/css",
    ".xml": "application/xml",
    ".py": "text/x-python",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_WORD = re.compile(r"[a-z0-9]+")
# 0.14.0: the file names a helper's answer mentions (it said it made a poster that never came back).
_NAMED = re.compile(r"[\w./-]+\.(?:png|jpe?g|pdf|docx|xlsx|pptx|csv|txt|md|json|html)\b", re.IGNORECASE)


class WorkshopError(ValueError):
    """A run that can't happen; the message says why, for the agent."""


@dataclass
class Run:
    """What one run did, for the agent and the workshop_runs row."""

    task: str
    script_used: str | None
    inputs: list[str]
    kept: list[tuple[str, int]] = field(default_factory=list)  # path, bytes
    refused: list[tuple[str, str]] = field(default_factory=list)  # name, why
    script_path: str | None = None
    answer: str = ""
    cost: int = 0
    failure: str | None = None
    calls: int = 0  # 0.12.0: metered calls sent (a failed one too): a run that sent one counts toward the limits


class Workshop:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        settings: Settings,
        meter: MeteredModel,
        scope: AgentScope,
        jail: Jail,
    ) -> None:
        self.db = db
        self.clock = clock
        self.settings = settings
        self.meter = meter
        self.scope = scope
        self.jail = jail

    @property
    def transport(self) -> Any:
        return self.meter.transport

    # --- one run ---

    def run(
        self, cycle_id: int, task: str, files: list[str], script: str | None, folder: str | None, keep: int = 0
    ) -> Run:
        """Run the task; raises WorkshopError before anything is spent, CallRefused for the budget guard's state
        and system refusals (the cycle ends). Everything else ends up in the returned Run. ``keep``: what the
        cycle's reflection may cost, left for it (0.12.0)."""
        self._check_allowed()
        folder = (folder or DEFAULT_FOLDER).strip().strip("/")
        try:
            levels = self.jail.parts(folder, want_file=False)
        except SandboxError as exc:
            raise WorkshopError(f"folder: {exc}") from None
        if len(levels) >= MAX_DEPTH:  # 0.14.0: its files go inside it, so this is refused before the run is paid for
            raise WorkshopError(f"folder: at most {MAX_DEPTH - 1} folder levels, so that its files fit inside it")
        inputs = self._inputs(files, script)
        run = Run(task=task, script_used=script, inputs=[path for path, _, _ in inputs])
        uploaded: list[str] = []
        made: list[str] = []
        try:
            for _path, name, data in inputs:
                uploaded.append(self.transport.upload_file(name, data, MIME_TYPES[PurePosixPath(name).suffix]))
            request = prompts.workshop_request(self.settings, self._prompt(task, inputs, script), uploaded)
            responses = self._calls(cycle_id, request, run, keep)
            made = _output_ids(responses)
            run.answer = _answer(responses)
            if made:
                self._keep(made, folder, run)
        except FilesError as exc:
            run.failure = str(exc)
        finally:
            self._delete([*uploaded, *made])
        self._record(cycle_id, run)
        return run

    def _check_allowed(self) -> None:
        if not self.settings.workshop or self.settings.workshop_runs_per_day == 0:
            raise WorkshopError("your owner has switched the workshop off")
        since = to_iso(self.clock.day_start(self.clock.today()))
        with self.db.connection() as conn:
            done = store.workshop_runs_since(conn, self.scope, since)
        if done >= self.settings.workshop_runs_per_day:
            raise WorkshopError(f"the workshop runs at most {self.settings.workshop_runs_per_day} times a day")

    def _inputs(self, files: list[str], script: str | None) -> list[tuple[str, str, bytes]]:
        """(workspace path, name in the container, bytes) of every file handed over."""
        paths = [*files, *([script] if script else [])]
        if len(paths) > MAX_INPUTS + 1:
            raise WorkshopError(f"hand over at most {MAX_INPUTS} files")
        if script is not None and not script.endswith(".py"):
            raise WorkshopError("script must be a .py file from an earlier run, e.g. 'workshop/scripts/chart-3.py'")
        out: list[tuple[str, str, bytes]] = []
        total = 0
        names: set[str] = set()
        for path in paths:
            kind = kind_of(path)
            if kind is None:
                raise WorkshopError(f"{path}: only text files and your PDF, Word, Excel, PowerPoint and picture files")
            try:
                data = self.jail.read(path).encode("utf-8") if kind == "text" else self.jail.read_bytes(path)
            except SandboxError as exc:
                raise WorkshopError(str(exc)) from None
            name = PurePosixPath(path).name
            if name in names:
                raise WorkshopError(f"two files are called {name}; hand over only one of them")
            names.add(name)
            total += len(data)
            if total > MAX_INPUT_BYTES:
                raise WorkshopError(f"the files handed over may hold at most {MAX_INPUT_BYTES // (1024 * 1024)} MB")
            out.append((path, name, data))
        return out

    @staticmethod
    def _prompt(task: str, inputs: list[tuple[str, str, bytes]], script: str | None) -> str:
        given = [name for path, name, _ in inputs if path != script]
        lines = [task.strip(), "", f"Files handed over: {', '.join(given)}." if given else "No files handed over."]
        # 0.14.0: a path in the task is a file name in $OUTPUT_DIR (run #7 saved its poster elsewhere: lost)
        lines.append(
            "A path in the task names a file: save it by its file name at the top of $OUTPUT_DIR "
            "(e.g. $OUTPUT_DIR/x.png), or it is lost."
        )
        if script:
            lines.append(
                f"Start from the script {PurePosixPath(script).name} (kept from an earlier run): run it, with the "
                "changes the task asks for."
            )
        return "\n".join(lines)

    def _calls(self, cycle_id: int, request: dict[str, Any], run: Run, keep: int = 0) -> list[dict[str, Any]]:
        """The run's metered calls: the first, and its continuations after pause_turn."""
        responses: list[dict[str, Any]] = []
        first = request
        done: list[Any] = []
        for attempt in range(1 + MAX_CONTINUATIONS):
            try:
                # 0.14.0: the cap is checked against the request as priced; what the call holds of the day can be more
                quote = self.meter.quote(request, WORKSHOP, scaled=False)
                held = self.meter.reservation(request, WORKSHOP)
            except Unpriceable as exc:
                run.failure = f"the run can't be priced ({exc})"
                break
            # The run's cap covers all its calls: a continuation gets what the earlier calls left of it.
            left = usd_cap_to_micros(self.settings.workshop_run_cap_usd) - run.cost
            room = min(self.meter.headroom(cycle_id, WORKSHOP, keep=keep), left)
            going_on = "going on (the run paused)" if attempt else "the run"
            kept = f", after the ${micros_to_usd(keep):.3f} kept for your reflection)" if keep else ")"
            if quote > room:
                run.failure = (
                    f"{going_on} could cost up to ${micros_to_usd(quote):.3f}, but only ${micros_to_usd(room):.3f} "
                    "is left for it (the workshop's cap per run, the daily cap or the balance" + kept
                )
                break
            money = self.meter.rooms(cycle_id, WORKSHOP, keep=keep)[1]
            if held > money:
                run.failure = (
                    f"{going_on} holds ${micros_to_usd(held):.3f} of the day (the workshop's cap per run, or what "
                    f"recent runs cost), but only ${micros_to_usd(money):.3f} is left (the daily cap or the balance"
                    + kept
                )
                break
            try:
                result = self.meter.call(cycle_id, WORKSHOP, request)
            except CallRefused as exc:
                if exc.category in ("state", "system"):
                    raise
                run.failure = f"the budget guard refused the run ({exc.reason})"
                break
            except CallFailed as exc:
                run.calls += 1
                run.cost += exc.result.cost_micros
                run.failure = f"the run failed ({exc.result.error or exc.result.status})"
                break
            run.calls += 1
            run.cost += result.cost_micros
            response = result.response or {}
            responses.append(response)
            container = response.get("container")
            if response.get("stop_reason") != "pause_turn" or attempt == MAX_CONTINUATIONS:
                if response.get("stop_reason") == "pause_turn":
                    run.failure = "the run took too long and was stopped; ask for less at once"
                break
            # Send the paused turn back as it is: the answers so far, as one assistant turn after the task.
            done = [*done, *(response.get("content") or [])]
            request = {**first, "messages": [*first["messages"], {"role": "assistant", "content": done}]}
            if isinstance(container, Mapping) and isinstance(container.get("id"), str):
                request["container"] = container["id"]
        return responses

    def _keep(self, file_ids: list[str], folder: str, run: Run) -> None:
        limit = self.jail.limits.max_product_bytes
        latest: dict[str, tuple[str, int]] = {}  # name -> (file id, bytes): a name copied out again is its last copy
        for file_id in file_ids[:MAX_LOOKUPS]:
            try:
                info = self.transport.file_info(file_id)
            except FilesError as exc:
                run.refused.append(("?", str(exc)))
                continue
            name = _clean_name(str(info.get("filename") or ""))
            if name is None:
                run.refused.append((str(info.get("filename"))[:60], "its name can't be used in your workspace"))
                continue
            latest.pop(name, None)
            latest[name] = (file_id, int(info.get("size_bytes") or 0))
        final = list(latest.items())
        if len(file_ids) > MAX_LOOKUPS or len(final) > MAX_OUTPUTS:
            run.refused.append(("the other files", f"a run keeps at most {MAX_OUTPUTS} files"))
        number = self._next_number()
        for name, (file_id, size) in final[:MAX_OUTPUTS]:
            if size > limit:
                run.refused.append((name, f"it is larger than {limit // (1024 * 1024)} MB"))
                continue
            try:
                data = self.transport.download_file(file_id, limit)
            except FilesError as exc:
                run.refused.append((name, str(exc)))
                continue
            path = self._script_path(run.task, number) if name == "script.py" else f"{folder}/{name}"
            try:
                with netguard.sealed():  # checking a file is decoding untrusted bytes: no network meanwhile
                    kept = checks.check(name, data)
                    size = self.jail.write(path, kept) if isinstance(kept, str) else self.jail.write_bytes(path, kept)
            except (checks.Refused, SandboxError) as exc:
                run.refused.append((name, str(exc)))
                continue
            if name == "script.py":
                run.script_path = path
            run.kept.append((path, size))

    def _script_path(self, task: str, number: int) -> str:
        """workshop/scripts/<the task's first words>-<n>.py, never the name of a script already there."""
        slug = _slug(task)
        while self.jail.size_of(f"{SCRIPTS}/{slug}-{number}.py") is not None:
            number += 1
        return f"{SCRIPTS}/{slug}-{number}.py"

    def _next_number(self) -> int:
        with self.db.connection() as conn:
            where, params = self.scope.where()
            row = conn.execute(f"SELECT COUNT(*) FROM workshop_runs WHERE {where}", params).fetchone()
        return int(row[0]) + 1

    def _delete(self, file_ids: list[str]) -> None:
        for file_id in dict.fromkeys(file_ids):
            try:
                self.transport.delete_file(file_id)
            except FilesError:
                log.warning("Couldn't delete a workshop file from Anthropic's storage; it expires on its own")

    def _record(self, cycle_id: int, run: Run) -> None:
        status = "failed" if run.failure else ("ok" if made(run) else "nothing")
        with self.db.transaction() as conn:
            store.insert_workshop_run(
                conn,
                self.scope,
                cycle_id,
                to_iso(self.clock.now()),
                task=run.task[:3000],
                script_used=run.script_used,
                script_path=run.script_path,
                inputs=run.inputs,
                outputs=[{"path": path, "bytes": size} for path, size in run.kept],
                refused=[{"name": name, "why": why} for name, why in run.refused],
                status=status,
                cost_micros=run.cost,
                summary=(run.failure or run.answer)[:2000],
            )


def made(run: Run) -> list[tuple[str, int]]:
    """0.14.0: the files the run kept besides its script (live: run #7 was "ok" with only its script back, $0.89)."""
    return [(path, size) for path, size in run.kept if path != run.script_path]


def report(run: Run, wrap: Any) -> tuple[bool, str, str]:
    """(ok, the agent's tool result, a one-line summary). ``wrap`` marks the helper's answer as data."""
    cost = f"${micros_to_usd(run.cost):.4f}"
    lines = [f"Workshop run (cost {cost})."]
    if run.answer:
        lines.append(wrap("workshop", run.answer[:ANSWER_CHARS]))
    if run.kept:
        kept = "; ".join(f"{path} ({size / 1024:,.0f} KB)" for path, size in run.kept)
        lines.append(f"Kept: {kept}.")
    if run.script_path:
        lines.append(f"The script is {run.script_path}: run it again with script, instead of writing it anew.")
    if run.refused:
        lines.append("Not kept: " + "; ".join(f"{name}: {why}" for name, why in run.refused) + ".")
    if run.failure:
        lines.append(f"Problem: {run.failure}.")
    elif run.kept and not made(run) and not run.refused:
        # A refused file came back: the "Not kept" line says why, so no advice to run it again.
        known = {PurePosixPath(n).name for n in [*(path for path, _ in run.kept), *run.inputs]}
        named = dict.fromkeys(PurePosixPath(n).name for n in _NAMED.findall(run.answer))
        lost = [n for n in named if n not in known][:5]
        lines.append(
            "Only the script came back"
            + (f" ({', '.join(lost)} didn't)" if lost else "")
            + ": every file must be saved into $OUTPUT_DIR. Run the script again with script."
        )
    elif not run.kept:
        lines.append("Nothing was kept: name the files you need in the task (e.g. 'chart.png, 1200 x 800 pixels').")
    ok = bool(made(run)) and not run.failure
    files = ", ".join(path for path, _ in run.kept[:3]) if made(run) else ""
    summary = f"workshop {cost}: " + (files or run.failure or ("only its script" if run.kept else "nothing kept"))
    return ok, "\n".join(lines), summary[:300]


def _output_ids(responses: list[dict[str, Any]]) -> list[str]:
    """The ids of the files the run's commands left in $OUTPUT_DIR, oldest first, each once."""
    found: list[str] = []
    for response in responses:
        for block in response.get("content") or []:
            if not isinstance(block, Mapping) or not str(block.get("type", "")).endswith("_tool_result"):
                continue
            result = block.get("content")
            items = result.get("content") if isinstance(result, Mapping) else None
            for item in items if isinstance(items, list) else []:
                if isinstance(item, Mapping) and isinstance(item.get("file_id"), str) and item["file_id"] not in found:
                    found.append(item["file_id"])
    return found


def _answer(responses: list[dict[str, Any]]) -> str:
    """The helper's final words: the text after its last code run."""
    for response in reversed(responses):
        texts: list[str] = []
        for block in response.get("content") or []:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "server_tool_use":
                texts = []  # only what comes after the last run
        text = "\n".join(t for t in texts if t.strip()).strip()
        if text:
            return text
    return ""


def _clean_name(name: str) -> str | None:
    """A file name the jail accepts (letters, digits, '.', '_', '-'), or None."""
    base = PurePosixPath(name.replace("\\", "/")).name
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        return None
    stem = _UNSAFE_NAME.sub("-", stem).strip("-._")[:50] or "file"
    suffix = "jpg" if suffix.lower() == "jpeg" else suffix.lower()
    clean = f"{stem}.{suffix}"
    return clean if NAME.fullmatch(clean) else None


def _slug(task: str) -> str:
    words = _WORD.findall(task.lower())[:4]
    return "-".join(words)[:40].strip("-") or "script"
