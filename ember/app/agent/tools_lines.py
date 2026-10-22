"""0.42.2 (analysis-0.40.1, 5.6): the line a call works on and the holds that stop it, split out of tools.py.

A cycle on one product line works on that line only (``_line``, ``_listing_line``, ``_line_venture``) and, until it
has one, takes the line of its first call that succeeds (``_lock``). No call works on a line the owner holds or a
review's stop holds (``_held``: plan.line_held, plan.review_held), nor writes, makes from or pays a model to build on
such a line's files, or names its listings in a request or an email (``_held_files``, ``held_file``). A project or a
listing of a venture the owner parked or killed takes no new work (``_stopped``, ``_listing_stopped``,
``_open_project``).
"""

from __future__ import annotations

import re
from typing import Any

from . import paid, plan, roadmap, store, ventures, workfiles
from .store import OPEN_STATUSES, AgentScope
from .tools_context import ToolContext, ToolError, _cut
from .tools_limits import WORKSHOP_FOLDER


def _open_project(conn: Any, scope: AgentScope, project_id: int) -> Any:
    """A project a milestone may be linked to: open, and (0.23.2) not one whose venture the owner parked or killed (a
    milestone for a project the owner's park stopped carried its work on, its unlocks included)."""
    row = store.project(conn, scope, project_id)
    if row is None:
        raise ToolError(f"there is no project #{project_id}")
    if row["status"] not in OPEN_STATUSES:
        raise ToolError(f"project #{project_id} is {row['status']}")
    refused = _stopped(conn, scope, project_id)
    if refused:
        raise ToolError(refused)
    return row


def _stopped(conn: Any, scope: AgentScope, project_id: int, channel: str | None = None) -> str:
    """0.23.2: why a project the owner's park or kill stopped takes no new work ("" when it does). 0.23.3: a product
    line of no venture by its channel's venture (ventures.project_stopped)."""
    venture = ventures.project_stopped(conn, scope, project_id, channel)
    if venture is None:
        return ""
    own = conn.execute("SELECT venture_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    how = "belongs to" if own is not None and own["venture_id"] == venture["id"] else "sells in the channel of"
    if venture["stage"] == "killed":
        return f"project #{project_id} {how} venture #{venture['id']}, which your owner killed"
    return (
        f"project #{project_id} {how} venture #{venture['id']}, which your owner parked: its work waits until they "
        "take it up again"
    )


def _listing_stopped(conn: Any, scope: AgentScope, listing_id: int, what: str) -> None:
    """0.23.3: no work on a listing of a product line the owner's park or kill stopped (``what`` is refused)."""
    venture = ventures.listing_stopped(conn, scope, listing_id)
    if venture is not None:
        did = "killed" if venture["stage"] == "killed" else "parked"
        raise ToolError(
            f"#{listing_id} is a listing of venture #{venture['id']}, which your owner {did}: no {what} for it"
            + (" until they take it up again" if did == "parked" else "")
        )


def _line(ctx: ToolContext, conn: Any, project_id: int | None, what: str) -> None:
    """0.28.0: one product line a cycle (lines.py). An ordinary, marketing or reactive cycle works on its line only;
    until it has one, on the line of its first call that works on one (it takes it once the call succeeds, _lock).
    Refuses ``what`` for another line. 0.39.3 (analysis-0.39.1, 4.4): and for a line the owner holds (plan.line_held),
    which no cycle takes: a cycle without a line took the first one it worked on, a held one too, and no tool read
    the owner's hold. 0.39.7 (analysis-0.39.1, 9.2): in every cycle, and before another line's refusal: a venture
    cycle, which isn't one line's, filed a request about a held line, and a cycle on another line heard that the held
    line's work waits for a cycle of its own. 0.42.1: and for a line a review's stop holds (``_held``)."""
    if project_id is None:
        return
    held = _held(conn, ctx.scope, int(project_id), what)
    if held is not None:
        raise ToolError(held)
    if not ctx.state.one_line:
        return
    line = ctx.state.focus_project_id
    if line is not None and int(project_id) != line:
        raise ToolError(
            f"this cycle works on product line #{line}: no {what} for project #{project_id}, whose work waits for a "
            "cycle of its own (say so in your journal's next)"
        )
    if line is None:
        ctx.state.touched = int(project_id)


def _held(conn: Any, scope: AgentScope, project_id: int, what: str) -> str | None:
    """0.39.3 (analysis-0.39.1, 4.4): why ``what`` is refused for product line ``project_id`` while the owner holds it
    (plan.line_held), None while nothing holds it. 0.42.1 (analysis-0.40.1 codebase, 6.2 "Your hold, on files too"):
    and while a review's stop holds it (plan.review_held, 0.40.2), until the owner keeps it or Ember lifts the hold
    with plan_step resume. A hold Ember put on a line herself refuses nothing: she resumes it when its work can't
    wait."""
    held = plan.line_held(conn, scope, project_id)
    if held is not None:
        return (
            f"your owner holds product line #{project_id} ({_cut(held, 80)}): no {what} for it until they resume it, "
            "not even for a promise about it; work on what doesn't need it"
        )
    held = plan.review_held(conn, scope, project_id)
    if held is not None:
        return (
            f"product line #{project_id} is on hold ({_cut(held, 80)}): no {what} for it until your owner keeps it;"
            f" if its work can't wait, lift the hold first (plan_step resume with project_id {project_id} and why)"
        )
    return None


# 0.42.1 (analysis-0.40.1 codebase, 6.2 "Your hold, on files too"): the fields of the tools that write, make or pay a
# model to build on workspace files, or propose what a text names, and what such a call is in a refusal
# (``_held_files``)
HELD_FILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "draft": ("draft", ("path", "sources")),
    "workspace_write": ("change to its files", ("path",)),
    "workshop": ("workshop run", ("files", "script", "task")),  # a task's file in its folder is replaced
    "make_document": ("document", ("source", "output")),
    "make_spreadsheet": ("spreadsheet", ("source", "output")),
    "make_image": ("picture", ("output", "pages")),
    "resize_image": ("print file", ("source", "output")),
    "make_cost_statement": ("cost statement", ("source", "output")),
    "propose_blog_post": ("blog post", ("source",)),
    "request_approval": ("request", ("title", "description", "payload")),
    "propose_email": ("email", ("subject", "body")),
}
HELD_LISTINGS = frozenset({"request_approval", "propose_email"})  # and the listings their texts name (paid.py's way)
_AROUND = "()[]{}<>'\"`.;:!?"  # what stands around a path in a text


def _held_files(ctx: ToolContext, name: str, args: dict[str, Any]) -> None:
    """0.42.1 (analysis-0.40.1 codebase, 6.2 "Your hold, on files too"): no call that writes a held line's workspace
    file, makes one from it or pays a model to build on it, nor a request or an email naming one of its listings
    (``_held``: the owner's hold, or a review's). The hold was kept only where a call names a project or a listing
    (``_line``), so draft, the workshop, the make_ tools, workspace_write, a request without a project, a blog post
    recommending no listing and an email could still spend money on a held line. A file's line is the one it is
    filed under (workfiles.py); one filed under none, or under a line nothing holds, is free, and reading one is too.
    A path in a field is the field (a comma list too; 'shop/cv.pdf#2@top' is shop/cv.pdf), or a word of a text."""
    what, fields = HELD_FILES.get(name, ("", ()))
    texts = [str(args[f]) for f in fields if isinstance(args.get(f), str)]
    if not texts:
        return
    words = {w.split("#")[0].split("@")[0].strip(_AROUND) for t in texts for w in re.split(r"[\s,]+", t)}
    if name == "workshop":  # a file its task names without a folder is made in its folder (books + interior.md)
        folder = str(args.get("folder") or WORKSHOP_FOLDER).strip().strip("/")
        words |= {f"{folder}/{w}" for w in words if w and "/" not in w}
    with ctx.db.connection() as conn:
        filed = workfiles.lines_of(conn, ctx.scope, sorted(w for w in words if "." in w))  # a file has its suffix
        named = [(f"{path} is a file of product line #{line}", line) for path, line in filed.items()]
        if name in HELD_LISTINGS:
            for listing in paid.listings_named(conn, ctx.scope, "\n".join(texts)):
                line = ventures.listing_project(conn, ctx.scope, listing)
                if line is not None:
                    named.append((f"listing #{listing} is product line #{line}'s", line))
        for thing, line in named:
            held = _held(conn, ctx.scope, line, what)
            if held is not None:
                raise ToolError(f"{thing}, and {held}")


def held_file(conn: Any, scope: AgentScope, path: str, what: str) -> str | None:
    """0.42.1 (analysis-0.40.1 codebase, 6.2 "Your hold, on files too"): why ``what`` may not write ``path``, a file of
    a held line (``_held``), or None: the workshop's check of a file its run made, which its task didn't name."""
    for filed, line in workfiles.lines_of(conn, scope, [path]).items():
        held = _held(conn, scope, line, what)
        if held is not None:
            return f"{filed} is a file of product line #{line}, and {held}"
    return None


def _listing_line(ctx: ToolContext, conn: Any, listing_id: int, what: str) -> None:
    """0.28.0: ``_line`` for a call about one of Ember's listings (an edit, a pin, a post, a blog post): its line.
    0.39.8: a refusal for another line's listing says how a listing that is this line's moves (live, three posts for
    the coach bundle were refused in cycle #318 with nothing to say why it counted for line #4)."""
    project_id = ventures.listing_project(conn, ctx.scope, listing_id)
    try:
        _line(ctx, conn, project_id, what)
    except ToolError as exc:
        line = ctx.state.focus_project_id
        if line is None or project_id is None or "waits for a cycle of its own" not in str(exc):
            raise
        raise ToolError(
            f"{exc}. Listing #{listing_id} counts for line #{project_id}, the line of the request that made it; if "
            f"it is line #{line}'s, move it: project_update with project_id {line}, listing_id {listing_id} and why in "
            "note"
        ) from None


def _line_venture(ctx: ToolContext, conn: Any, venture_id: int | None, what: str) -> None:
    """0.28.0: in a cycle on one product line, research and updates of a venture are for its line's own venture
    only: another venture's (an idea, one being researched) are a venture cycle's work."""
    if not ctx.state.one_line or venture_id is None:
        return
    line = ctx.state.focus_project_id
    project = store.project(conn, ctx.scope, line) if line is not None else None
    if project is None or project["venture_id"] != venture_id:
        on = f": this cycle works on product line #{line}" if line is not None else ""
        raise ToolError(f"venture #{venture_id}'s {what} is a venture cycle's work{on}")


def _lock(ctx: ToolContext, conn: Any) -> None:
    """0.28.0: a cycle without a line yet takes the line a call that succeeded worked on (_line): the cycle counts
    for it from then on (its cost, its requests, the files it wrote so far), aimed at its milestone due first unless
    the plan's is one the line may serve."""
    line, ctx.state.touched = ctx.state.touched, None
    if line is None or ctx.state.focus_project_id is not None:
        return
    ctx.state.focus_project_id = line
    cycle = conn.execute("SELECT project_id, milestone_id FROM cycles WHERE id = ?", (ctx.cycle_id,)).fetchone()
    if cycle is None:
        return
    changes: dict[str, Any] = {}
    if cycle["project_id"] is None:
        changes["project_id"] = line
    project = store.project(conn, ctx.scope, line)
    venture = project["venture_id"] if project is not None else None
    aimed = roadmap.get(conn, ctx.scope, cycle["milestone_id"]) if cycle["milestone_id"] else None
    if aimed is None or not roadmap.serves_line(aimed, line, venture):
        changes["milestone_id"] = roadmap.line_milestone(conn, ctx.scope, line, ctx.clock.today())
    if changes:
        store.update_cycle(conn, ctx.cycle_id, **changes)
    workfiles.adopt(conn, ctx.scope, ctx.cycle_id, line)
