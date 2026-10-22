"""The dry-run Etsy shop the tests share: a listed product (listed, started), a tool called in the running
cycle (call), the shop's tool context, an approved change, and a live shop against a mocked Etsy."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2

from app.agent import plan as plan_tree
from app.agent import tools
from app.agent.fake_llm import FakeTransport
from app.config import Settings
from app.economy.clock import Clock, to_iso
from app.integrations import etsy
from app.integrations.etsy import Listing, TokenFile, Tokens, Upload
from app.integrations.etsy_live import LiveShop
from tests.agent_helpers import owner, rows, run
from tests.roadmap_helpers import led_to_goal

LIVE = Settings(
    etsy_enabled=True,
    etsy_keystring="k3ystr1ngk3ystr1ng",
    etsy_shared_secret="sh4red-s3cret-value",
    etsy_redirect_uri="https://localhost/ember-etsy",
)


def a_listing(**changes: Any) -> Listing:
    fields: dict[str, Any] = {
        "title": "Weekly Meal Planner Printable",
        "description": "A weekly meal planner.\n\nWhat you get: a PDF.",
        "price": "4.50",
        "currency": "EUR",
        "tags": ("meal planner", "printable"),
        "taxonomy_id": 1,
        "category": "Paper & Party Supplies > Paper > Calendars & Planners",
        "files": (Upload("shop/planner.pdf", "a" * 64, 12_345),),
        "photos": (Upload("shop/planner-photo-1.png", "b" * 64, 2_000_000),),
    }
    fields.update(changes)
    return Listing(**fields)


def some_tokens(clock: Clock, *, expires: timedelta = timedelta(hours=1)) -> Tokens:
    now = clock.now()
    return Tokens(
        access_token="12345.acc3ss-t0ken-value",
        refresh_token="r3fresh-t0ken-value",
        expires_at=to_iso(now + expires),
        refresh_expires_at=to_iso(now + timedelta(days=90)),
        user_id=12345,
        shop_id=777,
        shop_name="PlannerShop",
        currency="EUR",
        connected_at=to_iso(now),
    )


class Etsy:
    """Answers requests like Etsy's API would, and records them."""

    def __init__(self, answers: dict[tuple[str, str], Any]) -> None:
        self.answers = answers
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        answer = self.answers.get((request.method, request.url.path))
        if answer is None:
            return httpx2.Response(404, json={"error": "not found"})
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, httpx2.Response) else httpx2.Response(200, json=answer)

    def form(self, index: int) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.requests[index].content.decode()).items()}


def mock(answers: dict[tuple[str, str], Any]) -> tuple[Etsy, Any]:
    server = Etsy(answers)
    return server, httpx2.MockTransport(server)


def live_shop(tmp_path: Path, answers: dict[tuple[str, str], Any], **token_changes: Any) -> tuple[LiveShop, Etsy]:
    server, transport = mock(answers)
    clock = Clock()
    store = TokenFile(tmp_path / "tokens.json")
    tokens = some_tokens(clock)
    for key, value in token_changes.items():
        setattr(tokens, key, value)
    store.save(tokens)
    return LiveShop(LIVE, clock, store, transport), server


def proposed(data_dir: Path) -> tuple[Any, FakeTransport, int]:
    """A dry-run agent whose fourth cycle proposed an Etsy listing (0.37.6: its founder routine works on line #1 by the
    cycle's number, without the plan's brake: test_loop_shapes.unbraked)."""
    fake = FakeTransport()
    agent, _ = run(data_dir, fake, cycles=4, brake=False)
    found = rows(agent, "SELECT id FROM approvals WHERE executor = 'etsy_listing'")
    assert found, "the fake proposes a listing in its fourth cycle"
    return agent, fake, found[0]["id"]


def listed(data_dir: Path) -> tuple[Any, int]:
    """A dry-run agent with one live listing in the fake shop, and the listing's number."""
    agent, _, request = proposed(data_dir)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    assert agent.execute_approved() == [(request, "active")]
    return agent, 900_000_001


def market_next(agent: Any, channel: str) -> int:
    """0.35.0: the next cycle a marketing cycle in ``channel``: the owner's pin of the plan's first open step there."""
    now = to_iso(agent.clock.now())
    with agent.db.transaction() as conn:
        plan_tree.keep(conn, agent.scope(), now, agent.clock.today(), plan_tree.channels_from(agent.settings))
        [step, *_] = plan_tree.nodes(
            conn, agent.scope(), "level = 'step' AND status = 'open' AND channel = ?", (channel,)
        )
        plan_tree.pin(conn, agent.scope(), int(step["id"]), True, "Owner", now)
    return int(step["id"])


def started(data_dir: Path) -> tuple[Any, int]:
    """A dry-run agent whose first listing is live, with its product's test begun (0.35.0: the day Ember's code saw it
    live, kept on its product in the plan tree); the project's number."""
    agent, _ = listed(data_dir)
    [project] = rows(agent, "SELECT a.project_id FROM approvals a WHERE a.executor = 'etsy_listing'")
    assert project["project_id"], "the fake's listing belongs to its focus project"
    with agent.db.transaction() as conn:
        plan_tree.keep(conn, agent.scope(), to_iso(agent.clock.now()), agent.clock.today(), {})
    [product] = rows(
        agent, f"SELECT live_since FROM plan_nodes WHERE level = 'product' AND project_id = {project['project_id']}"
    )
    assert product["live_since"] == agent.clock.today().isoformat()
    return agent, int(project["project_id"])


def shop_context(agent: Any) -> tools.ToolContext:
    """The tools' context in the agent's last cycle, with its shop (and one department among the categories)."""
    ctx = tools.ToolContext(
        db=agent.db,
        clock=agent.clock,
        scope=agent.scope(),
        cycle_id=rows(agent, "SELECT MAX(id) AS id FROM cycles")[0]["id"],
        workspace=agent.roots()[0],
        memory=agent.memory(),
        min_sleep=30,
        max_sleep=1440,
        state=tools.CycleTools(),
    )
    categories = (*etsy.FAKE_CATEGORIES, (99, "Accessories"))
    ctx.etsy = tools.EtsyAccess(shop_name="EmberTestShop", currency="EUR", daily_limit=3, categories=categories)
    return ctx


def call(ctx: tools.ToolContext, name: str, args: dict[str, Any]) -> tools.Outcome:
    """A tool as the model calls it while working: checked, recorded and limited per cycle."""
    llm_call = rows_of(ctx, "SELECT MAX(id) AS id FROM llm_calls")[0]["id"]
    return tools.run(ctx, name, led_to_goal(ctx, name, args), f"toolu_{name}", llm_call, "act")


def rows_of(ctx: tools.ToolContext, sql: str) -> list[dict[str, Any]]:
    with ctx.db.connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def a_change(agent: Any, ctx: tools.ToolContext, listing_id: int, **args: Any) -> int:
    """A proposed change (its request number); the photos and files it names are made first."""
    for name in ("photos", "files"):
        for path in [p.strip() for p in str(args.get(name) or "").split(",") if p.strip()]:
            agent.roots()[0].write_bytes(path, f"{path} data".encode())
    made = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "reason": "Fix the listing.", **args})
    assert made.ok, made.text
    return rows_of(ctx, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'etsy_edit'")[0]["id"]


def views_approval(agent: Any, approval_id: int) -> dict[str, Any]:
    """An approval as the dashboard gets it."""
    from app.agent import views  # noqa: PLC0415

    with agent.db.connection() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return views._carried_out(agent, conn, agent.scope(), row)
