"""0.29.0: everything on the roadmap leads to the goal at its root. A milestone a test plans without a parent leads to
that goal, as the agent's would have to; the refusal itself is tested in test_owner_goal.py."""

from __future__ import annotations

from typing import Any

from app.agent import roadmap, tools


def led_to_goal(ctx: tools.ToolContext, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """``args`` of a milestone_plan call with the goal as the parent of each milestone that names none."""
    if tool != "milestone_plan" or not isinstance(args.get("milestones"), list):
        return args
    with ctx.db.connection() as conn:
        top = roadmap.root(conn, ctx.scope)
    if top is None:
        return args
    return {
        **args,
        "milestones": [
            m if not isinstance(m, dict) or m.get("parent") else {**m, "parent": f"#{top['id']}"}
            for m in args["milestones"]
        ],
    }
