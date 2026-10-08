"""0.29.0: everything on the roadmap leads to the goal at its root. A milestone a test plans without a parent leads to
that goal, as the agent's would have to; the refusal itself is tested in test_owner_goal.py. 0.35.0: the agent's own
milestones retired with milestone_plan (the plan tree takes their place); a test sets one as the tool did."""

from __future__ import annotations

from typing import Any

from app.agent import metrics, roadmap, tools
from app.economy.clock import to_iso


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


def set_milestone(
    agent: Any,
    title: str,
    due: str,
    *,
    measure: str = "",
    metric: str | None = None,
    target: str | None = None,
    **fields: Any,
) -> int:
    """A milestone as milestone_plan set one until 0.35.0 (without its checks): with a metric, its measure and (for
    what is gained) its baseline from Ember's records now; led to the goal at the root unless ``parent_id`` says."""
    now, scope = to_iso(agent.clock.now()), agent.scope()
    with agent.db.transaction() as conn:
        checked = metrics.CATALOGUE[metric] if metric else None
        value = metrics.parse_target(checked, target) if checked is not None else None
        baseline = None
        if checked is not None and checked.history:
            row = {
                "metric": metric,
                "target": value,
                "baseline": None,
                "created_at": now,
                "project_id": fields.get("project_id"),
                "venture_id": fields.get("venture_id"),
            }
            baseline = metrics.listing_counts(conn, scope, row, "views" if metric == "views_delta" else "favorites")
        if checked is not None and not measure:
            measure = metrics.measure_text(checked, value, fields.get("project_id"), fields.get("venture_id"))
        if "parent_id" not in fields:
            top = roadmap.root(conn, scope)
            fields["parent_id"] = int(top["id"]) if top is not None else None
        return roadmap.create(
            conn,
            scope,
            title=title,
            measure=measure or title,
            due=due,
            now=now,
            metric=metric,
            target=value,
            baseline=baseline,
            **fields,
        )
