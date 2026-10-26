"""The agent's local tools: definitions for the model, validation and handlers.

Every tool is described once (``SPECS``); the JSON schema the model sees and the
validation in code come from the same description, so they can't drift apart.
Handlers never raise: a refused or invalid call comes back to the model as an
error result it can react to. Every call is recorded in ``tool_calls``.

Limits that matter are enforced here, not in the prompt: per-cycle counts,
sizes, the file jail, the number of pending requests (0.19.1: not of open projects), and
which tools may run in the reflect phase. No tool can move money, record
revenue, change the options, reach the network (``research`` is a metered
model call with Anthropic's server-side web tools, not a local fetch) or touch
the constitution. No tool sends anything: the email tools read what Ember's
code fetched into the database, and ``propose_email`` and ``propose_reddit_post``
only create approval requests, which Ember's code (an email) or the owner (a
Reddit post, 0.25.0: a KDP book) carries out once the owner approves them. So do ``propose_pin``
(0.13.0: Ember's code makes the pin on the owner's Pinterest account),
``propose_bluesky_post`` (0.19.0: Ember's code posts it on the account the owner made
for Ember) and the Etsy tools.

The making tools (``make_document``, ``make_spreadsheet``, ``make_image``) turn
the agent's text into PDF, Word, Excel and PNG files with Ember's own code
(app.products; 0.38.0: ``make_image`` also a cover with a small table; 0.52.0: and a
book's front with its lines centred, which ``propose_kdp_book`` checks on every cover),
``resize_image`` (0.17.0) makes print files of its pictures and
``make_cost_statement`` (0.20.0) a Nebenkostenabrechnung with its cover picture;
``look`` shows the model one of its pictures, and (0.40.0) ``propose_kdp_book``'s check
a paperback's pages, drawn fresh (pagecheck.py), which its proposal needs. ``workshop`` (like
``research``, a metered model call) has code written and run in Anthropic's
sandbox; Ember's code checks every file it made before it is kept.

0.42.2 (analysis-0.40.1, 5.6): tools.py was split into modules, one job each. This one runs a call (``run``: checked,
refused or handled, and recorded) and names each tool's handler (``HANDLERS``). The limits are in tools_limits.py,
the tools as the model sees them (``SPECS``, ``definitions``) in tools_specs.py, what every handler shares
(``ToolContext``, ``Outcome``, ``ToolError``) in tools_context.py and the checks of a call's input in tools_checks.py;
the handlers, by area, in tools_files.py, tools_projects.py, tools_requests.py, tools_metered.py, tools_listings.py
and tools_channels.py, with the product line a call works on and the owner's holds in tools_lines.py. Every name the
rest of Ember and the tests read here is still here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..integrations import kdp as kdp
from ..integrations import site_publisher as site_publisher
from ..products import make
from . import netguard, store, ventures, workfiles
from .memory import MemoryError_
from .sandbox import Missing, QuotaError
from .sandbox import SandboxError as SandboxError
from .tools_channels import REPLACED_PIN as REPLACED_PIN
from .tools_channels import (
    _bluesky_posts,
    _pinterest_boards,
    _propose_blog_post,
    _propose_bluesky_post,
    _propose_link_page,
    _propose_pin,
    _site_page,
)
from .tools_channels import _room_in_queue as _room_in_queue
from .tools_checks import TAKEN_OUT as TAKEN_OUT
from .tools_checks import _checked as _checked
from .tools_checks import _clip, _noted
from .tools_checks import _unclosed as _unclosed
from .tools_checks import unleaked as unleaked
from .tools_checks import validate as validate
from .tools_context import BlogAccess as BlogAccess
from .tools_context import BlueskyAccess as BlueskyAccess
from .tools_context import BrainstormFn as BrainstormFn
from .tools_context import CycleTools as CycleTools
from .tools_context import Drafted as Drafted
from .tools_context import DraftFn as DraftFn
from .tools_context import EtsyAccess as EtsyAccess
from .tools_context import KdpAccess as KdpAccess
from .tools_context import MailAccess as MailAccess
from .tools_context import Outcome as Outcome
from .tools_context import PinterestAccess as PinterestAccess
from .tools_context import PrintifyAccess as PrintifyAccess
from .tools_context import ResearchFn as ResearchFn
from .tools_context import ToolContext as ToolContext
from .tools_context import ToolError as ToolError
from .tools_context import WorkshopFn as WorkshopFn
from .tools_context import _unstop
from .tools_context import wrap as wrap
from .tools_files import VERSIONS_KEPT as VERSIONS_KEPT
from .tools_files import VERSIONS_MAX_BYTES as VERSIONS_MAX_BYTES
from .tools_files import (
    _guide,
    _knowledge_search,
    _library_read,
    _look,
    _make_cost_statement,
    _make_document,
    _make_image,
    _make_spreadsheet,
    _memory_read,
    _memory_update,
    _resize_image,
    _workspace_list,
    _workspace_read,
    _workspace_write,
)
from .tools_files import guide_text as guide_text
from .tools_files import image_block as image_block
from .tools_files import keep_version as keep_version
from .tools_files import prune_versions as prune_versions
from .tools_limits import BLUESKY_TOOLS as BLUESKY_TOOLS
from .tools_limits import BUILDING_TOOLS, ELSEWHERE_SHOWN
from .tools_limits import CALL_CHARS as CALL_CHARS
from .tools_limits import CALLING_TOOLS as CALLING_TOOLS
from .tools_limits import CATEGORIES_SHOWN as CATEGORIES_SHOWN
from .tools_limits import CHANNEL_READS as CHANNEL_READS
from .tools_limits import CHECKING_TOOLS as CHECKING_TOOLS
from .tools_limits import DEPARTMENT as DEPARTMENT
from .tools_limits import DRAFT_BYTES as DRAFT_BYTES
from .tools_limits import DRAFT_CHARS as DRAFT_CHARS
from .tools_limits import DRAFT_MAX_TOKENS as DRAFT_MAX_TOKENS
from .tools_limits import ETSY_TOOLS as ETSY_TOOLS
from .tools_limits import FOLDER_SHOWN as FOLDER_SHOWN
from .tools_limits import GUIDES as GUIDES
from .tools_limits import INBOX_SIZE as INBOX_SIZE
from .tools_limits import KDP_NOTE as KDP_NOTE
from .tools_limits import KDP_TOOLS as KDP_TOOLS
from .tools_limits import LIBRARY_TOOLS as LIBRARY_TOOLS
from .tools_limits import LOOK_PIXELS as LOOK_PIXELS
from .tools_limits import MAIL_READS as MAIL_READS
from .tools_limits import MAIL_TOOLS as MAIL_TOOLS
from .tools_limits import MAKERS as MAKERS
from .tools_limits import MARKETING_TOOLS as MARKETING_TOOLS
from .tools_limits import MAX_RESULT_CHARS as MAX_RESULT_CHARS
from .tools_limits import MAX_TOOL_CALLS_PER_TURN as MAX_TOOL_CALLS_PER_TURN
from .tools_limits import MAX_UNREAD_MESSAGES as MAX_UNREAD_MESSAGES
from .tools_limits import ONE_REPLY_CHARS as ONE_REPLY_CHARS
from .tools_limits import ORDINARY_TOOLS as ORDINARY_TOOLS
from .tools_limits import PINTEREST_TOOLS as PINTEREST_TOOLS
from .tools_limits import PRINTIFY_TOOLS as PRINTIFY_TOOLS
from .tools_limits import QA_CHARS as QA_CHARS
from .tools_limits import QA_WORDS as QA_WORDS
from .tools_limits import READ_MAX_CHARS as READ_MAX_CHARS
from .tools_limits import READ_ONLY_TOOLS as READ_ONLY_TOOLS
from .tools_limits import RESEARCH_REPEAT_DAYS as RESEARCH_REPEAT_DAYS
from .tools_limits import RESULT_CHARS as RESULT_CHARS
from .tools_limits import SANDBOX_STRIKES as SANDBOX_STRIKES
from .tools_limits import WHOLE_MAX as WHOLE_MAX
from .tools_limits import WORK_MAX_TOKENS as WORK_MAX_TOKENS
from .tools_limits import WORKSHOP_FOLDER as WORKSHOP_FOLDER
from .tools_limits import WORKSHOP_INPUT_MB as WORKSHOP_INPUT_MB
from .tools_limits import WORKSHOP_INPUTS as WORKSHOP_INPUTS
from .tools_limits import WRITE_CHARS as WRITE_CHARS
from .tools_lines import _held_files, _lock
from .tools_lines import _listing_line as _listing_line
from .tools_lines import held_file as held_file
from .tools_listings import _category as _category
from .tools_listings import (
    _demand_note,
    _etsy_categories,
    _etsy_listing,
    _printify_catalog,
    _propose_etsy_edit,
    _propose_etsy_listing,
    _propose_kdp_book,
    _propose_printify_edit,
    _propose_printify_product,
)
from .tools_listings import _product_line as _product_line
from .tools_metered import _brainstorm, _draft, _research, _workshop
from .tools_metered import _names_an_address as _names_an_address
from .tools_projects import EVIDENCE as EVIDENCE
from .tools_projects import _evidence as _evidence
from .tools_projects import (
    _milestone_update,
    _plan_step,
    _project_create,
    _project_list,
    _project_update,
    _venture_create,
    _venture_update,
)
from .tools_projects import _venture_case as _venture_case
from .tools_projects import update_milestone as update_milestone
from .tools_requests import JOURNAL_DRAFT as JOURNAL_DRAFT
from .tools_requests import QUOTED as QUOTED
from .tools_requests import (
    _email_inbox,
    _email_read,
    _inquiry_done,
    _journal_draft,
    _mark_opt_out,
    _message_owner,
    _obligation_done,
    _propose_email,
    _propose_reddit_post,
    _request_approval,
    _request_upgrade,
    _set_sleep,
    _withdraw_request,
    _write_journal,
)
from .tools_requests import _facts as _facts
from .tools_requests import _room_for_request as _room_for_request
from .tools_specs import ORDINARY_VARIANTS as ORDINARY_VARIANTS
from .tools_specs import ORDINARY_VENTURE_FIELDS as ORDINARY_VENTURE_FIELDS
from .tools_specs import SPECS as SPECS
from .tools_specs import VENTURE_VARIANTS as VENTURE_VARIANTS
from .tools_specs import Field as Field
from .tools_specs import Spec
from .tools_specs import definitions as definitions
from .tools_specs import offered as offered
from .tools_specs import spec_of as spec_of

log = logging.getLogger(__name__)


def run(
    ctx: ToolContext,
    name: str,
    raw_input: Any,
    tool_use_id: str,
    llm_call_id: int,
    phase: str,
    room: int = MAX_TOOL_CALLS_PER_TURN,
) -> Outcome:
    """Validate and run one tool call; always returns an Outcome (never raises). 0.24.0: a call of another tool the
    model wrote inside its journal (``unleaked``) is taken out of it, and what became of it is added to the answer.
    0.39.4: it runs only when it reads (READ_ONLY_TOOLS), as one of the ``room`` calls its reply may still run (the
    answer's ``taken``); any other is refused (TAKEN_OUT). It used to run whatever a text held: a buyer's email saved
    to notes wrote an "Owner rule" into LESSONS, a quote in a message deleted a product file (analysis-0.39.1, 4.1)."""
    leaked: list[tuple[str, dict[str, Any]]] = []
    outcome = _run(ctx, name, raw_input, tool_use_id, llm_call_id, phase, leaked)
    if not leaked:
        return outcome
    taken = 0
    for number, (other, args) in enumerate(leaked, 1):
        use_id = f"{tool_use_id}~{number}"
        if other in READ_ONLY_TOOLS and taken < room:
            more = _run(ctx, other, args, use_id, llm_call_id, phase, None)
            taken += more.ok  # refused (a read in the reflection), it did nothing and leaves the reply its calls
            said = f"ran the {other} call you wrote inside {name} as a call of its own: {more.text}"
        else:
            why = f"at most {MAX_TOOL_CALLS_PER_TURN} tool calls per turn" if other in READ_ONLY_TOOLS else TAKEN_OUT
            skip(ctx, other, args, use_id, llm_call_id, phase, why)
            said = f"didn't run the {other} call you wrote inside {name}: {why}."
        outcome = replace(outcome, text=f"{outcome.text}\nEmber's code {said}")
    return replace(_clip(outcome, RESULT_CHARS.get(str(name), MAX_RESULT_CHARS)), taken=taken)


def _run(
    ctx: ToolContext,
    name: str,
    raw_input: Any,
    tool_use_id: str,
    llm_call_id: int,
    phase: str,
    leaked: list[tuple[str, dict[str, Any]]] | None,
) -> Outcome:
    """One tool call (run): ``leaked`` collects the calls of other tools written inside its journal's texts (None: a
    call taken out of a journal, whose texts are not searched again)."""
    tool_input = raw_input if isinstance(raw_input, dict) else {"_raw": raw_input}
    ctx.state.reply = llm_call_id
    with ctx.db.transaction() as conn:
        call_id = store.start_tool_call(
            conn,
            cycle_id=ctx.cycle_id,
            llm_call_id=llm_call_id,
            phase=phase,
            tool=str(name)[:64],
            tool_use_id=str(tool_use_id),
            tool_input=tool_input,
            now=ctx.now(),
        )
    ctx.workspace.noticed = []  # 0.26.0: the files it writes or deletes, filed under the cycle's focus (_file)
    ctx.state.touched = None  # 0.28.0: the line it works on, which a cycle without one takes once it succeeds (_lock)
    try:
        spec = spec_of(name, ctx.venture)
        if spec is not None and ctx.venture and name in ORDINARY_TOOLS:
            raise ToolError(
                f"{name} is not one of your tools in a venture cycle: making files, the shop, Pinterest, Bluesky, "
                "KDP, email and Reddit belong to ordinary and marketing cycles"
            )
        if spec is not None and ctx.marketing and name in BUILDING_TOOLS:  # 0.28.0
            # 0.45.1: reading an email is a marketing cycle's too (MAIL_READS)
            reads = (
                "; email_read opens an email by its number (MAIL lists the unread ones)" if name in MAIL_TOOLS else ""
            )
            raise ToolError(
                f"{name} is not one of your tools in a marketing cycle: making documents and spreadsheets, the "
                f"workshop, new listings and products, KDP and answering email belong to ordinary cycles{reads}"
            )
        if spec is not None and ctx.marketing_apart and not ctx.marketing and name in MARKETING_TOOLS:  # 0.28.0
            raise ToolError(
                f"{name} belongs to marketing cycles: a marketing step of your plan gets a cycle of its own, which "
                "brings buyers to one line's listings with pins, Bluesky posts and blog posts"
            )
        if spec is None or not offered(
            name,
            mail=ctx.mail is not None,
            workshop=bool(ctx.workshop),
            etsy=ctx.etsy is not None,
            venture=ctx.venture,
            library=ctx.library,
            pinterest=ctx.pinterest is not None,
            printify=ctx.printify is not None,
            site=ctx.site is not None,
            blog=ctx.blog is not None,
            bluesky=ctx.bluesky is not None,
            kdp=ctx.kdp is not None,
            marketing=ctx.marketing,
            marketing_apart=ctx.marketing_apart,
        ):
            raise ToolError(f"there is no tool called {str(name)[:40]!r}")
        if phase == "reflect" and not spec.reflect:
            raise ToolError(
                f"{name} can't be used while reflecting; only journal, memory updates, projects, ventures, your "
                "plan, sleep, messages and upgrade requests (nothing reads a tool's answer after this last reply)"
            )
        if phase == "act" and not spec.act:
            if name != "write_journal":
                raise ToolError(f"{name} is for the reflect phase at the end of the cycle")
            outcome = _journal_draft(ctx, spec, raw_input, leaked)  # 0.24.0
            with ctx.db.transaction() as conn:
                _file(conn, ctx, name)
                store.finish_tool_call(conn, call_id, "ok", outcome.summary, outcome.text, ctx.now())
            return _clip(outcome, RESULT_CHARS.get(name, MAX_RESULT_CHARS))
        limit = per_cycle(ctx, spec)
        if ctx.state.counts.get(name, 0) >= limit:
            more = f" ({ventures.RESEARCH_CALLS} in a venture cycle)" if name == "research" and not ctx.venture else ""
            raise ToolError(f"{name} can be used at most {limit} times per cycle{more}")
        if spec is not SPECS[name] and isinstance(raw_input, dict):  # 0.15.0: say why, not just "unknown field"
            other = sorted(set(raw_input) & set(SPECS[name].fields) - set(spec.fields))
            if other:
                raise ToolError(f"{other[0]} is set in a venture cycle; here {name} takes {', '.join(spec.fields)}")
        cut_notes: list[str] = []
        args = validate(spec, unleaked(spec, raw_input, cut_notes, leaked), cut_notes)
        _held_files(ctx, name, args)  # 0.42.1: a held line's files, and listings
        handler = HANDLERS[name]
        if name in CALLING_TOOLS:  # model calls: network, and no transaction held meanwhile
            outcome = _noted(handler(ctx, args), cut_notes)
        elif name in MAKERS or name in CHECKING_TOOLS:
            with netguard.sealed():
                outcome = _noted(handler(ctx, args), cut_notes)
        else:
            # Tool handlers never need the network, in live mode too (only the model calls do).
            with ctx.db.transaction() as conn, netguard.sealed():
                outcome = _noted(handler(ctx, args, conn), cut_notes)
                if outcome.ok:
                    ctx.state.counts[name] = ctx.state.counts.get(name, 0) + 1
                    _lock(ctx, conn)
                _file(conn, ctx, name)
                store.finish_tool_call(
                    conn, call_id, "ok" if outcome.ok else "error", outcome.summary, outcome.text, ctx.now()
                )
            return _clip(outcome, RESULT_CHARS.get(name, MAX_RESULT_CHARS))
        if (outcome.ok and not outcome.reused) or outcome.paid:
            ctx.state.counts[name] = ctx.state.counts.get(name, 0) + 1
    except (ToolError, make.ProductError) as exc:
        outcome = Outcome(False, f"Error: {_unstop(str(exc))}.", f"refused: {exc}"[:300])
    except (SandboxError, MemoryError_) as exc:
        if isinstance(exc, SandboxError) and not isinstance(exc, QuotaError):  # 0.12.0: a full workspace isn't a strike
            ctx.state.strikes += 1
        outcome = Outcome(False, f"Error: {exc}{_elsewhere(ctx, exc, raw_input)}.", f"refused: {exc}"[:300])
    except Exception as exc:  # noqa: BLE001 - a tool bug must not end the cycle or crash the app
        log.exception("Tool %s failed", name)
        outcome = Outcome(False, f"Error: the tool failed ({type(exc).__name__}).", f"failed: {type(exc).__name__}")
    with ctx.db.transaction() as conn:
        if outcome.ok:
            _lock(ctx, conn)
        _file(conn, ctx, str(name))
        store.finish_tool_call(conn, call_id, "ok" if outcome.ok else "error", outcome.summary, outcome.text, ctx.now())
    return _clip(outcome, RESULT_CHARS.get(str(name), MAX_RESULT_CHARS))


def _elsewhere(ctx: ToolContext, exc: Exception, raw_input: Any) -> str:
    """0.39.2: where the workspace holds a file of the name a call asked for, in another folder: live, the workshop
    kept the book's v14 in workshop/out/, make_document was asked for it in books/, and the refusal said only that it
    didn't exist; the cycle ended there (analysis-0.39.1, 3.3)."""
    if not isinstance(exc, Missing) or not isinstance(raw_input, dict):
        return ""
    missing = str(exc).removesuffix(" doesn't exist")
    asked = next(
        (v for v in raw_input.values() if isinstance(v, str) and (v == missing or v.startswith(f"{missing}/"))), None
    )
    if asked is None:
        return ""
    name = asked.rsplit("/", 1)[-1]
    try:
        files = ctx.workspace.walk(ctx.workspace.limits.max_entries).files
    except SandboxError:
        return ""
    found = [e.path for e in files if e.path != asked and e.path.rsplit("/", 1)[-1] == name][:ELSEWHERE_SHOWN]
    return f"; a file of that name is at {', '.join(found)}" if found else ""


def _file(conn: Any, ctx: ToolContext, name: str) -> None:
    """0.26.0: the files a call wrote or deleted (a refused call may have written some before it was), filed under
    its cycle's focus: the project or venture the owner's Workspace tab groups them by (agent/workfiles.py)."""
    noticed, ctx.workspace.noticed = ctx.workspace.noticed, None
    if noticed:
        focus = ctx.state.focus_project_id, ctx.state.focus_venture_id
        workfiles.record(conn, ctx.scope, noticed, *focus, ctx.cycle_id, name, ctx.now())


def per_cycle(ctx: ToolContext, spec: Spec) -> int:
    """How often a tool may be used in this cycle: a venture cycle researches more."""
    if spec.name == "research" and ctx.venture:
        return ventures.RESEARCH_CALLS
    return spec.per_cycle


def skip(
    ctx: ToolContext, name: str, raw_input: Any, tool_use_id: str, llm_call_id: int, phase: str, why: str
) -> Outcome:
    """Record a tool call that isn't run (too many in one turn, reply cut off, ...)."""
    with ctx.db.transaction() as conn:
        call_id = store.start_tool_call(
            conn,
            cycle_id=ctx.cycle_id,
            llm_call_id=llm_call_id,
            phase=phase,
            tool=str(name)[:64],
            tool_use_id=str(tool_use_id),
            tool_input=raw_input if isinstance(raw_input, dict) else {"_raw": raw_input},
            now=ctx.now(),
        )
        store.finish_tool_call(conn, call_id, "skipped", why[:300], why, ctx.now())
    return Outcome(False, f"Not executed: {why}.", why)


HANDLERS: dict[str, Callable[..., Outcome]] = {
    "workspace_list": _workspace_list,
    "workspace_read": _workspace_read,
    "workspace_write": _workspace_write,
    "memory_update": _memory_update,
    "memory_read": _memory_read,
    "project_create": _project_create,
    "project_update": _project_update,
    "project_list": _project_list,
    "plan_step": _plan_step,
    "venture_create": _venture_create,
    "venture_update": _venture_update,
    "brainstorm": _brainstorm,
    "milestone_update": _milestone_update,
    "request_approval": _request_approval,
    "withdraw_request": _withdraw_request,
    "message_owner": _message_owner,
    "obligation_done": _obligation_done,
    "request_upgrade": _request_upgrade,
    "set_sleep": _set_sleep,
    "write_journal": _write_journal,
    "research": _research,
    "evidence": _evidence,
    "venture_case": _venture_case,
    "demand_note": _demand_note,
    "draft": _draft,
    "knowledge_search": _knowledge_search,
    "library_read": _library_read,
    "workshop": _workshop,
    "etsy_categories": _etsy_categories,
    "propose_etsy_listing": _propose_etsy_listing,
    "etsy_listing": _etsy_listing,
    "propose_etsy_edit": _propose_etsy_edit,
    "make_document": _make_document,
    "make_spreadsheet": _make_spreadsheet,
    "make_image": _make_image,
    "resize_image": _resize_image,
    "make_cost_statement": _make_cost_statement,
    "look": _look,
    "guide": _guide,
    "email_inbox": _email_inbox,
    "email_read": _email_read,
    "mark_opt_out": _mark_opt_out,
    "inquiry_done": _inquiry_done,
    "propose_email": _propose_email,
    "propose_reddit_post": _propose_reddit_post,
    "propose_kdp_book": _propose_kdp_book,
    "pinterest_boards": _pinterest_boards,
    "propose_pin": _propose_pin,
    "bluesky_posts": _bluesky_posts,
    "propose_bluesky_post": _propose_bluesky_post,
    "printify_catalog": _printify_catalog,
    "propose_printify_product": _propose_printify_product,
    "propose_printify_edit": _propose_printify_edit,
    "site_page": _site_page,
    "propose_blog_post": _propose_blog_post,
    "propose_link_page": _propose_link_page,
}
