"""0.42.4: a request that waits for its service reads as waiting in every section the agent reads.

Since 0.42.0 (Printify since 0.42.3) a listing, change, pin, post or product that Etsy, Pinterest, Bluesky or Printify
asked to wait for (429, or 503 with Retry-After, before anything changed) stays approved and is tried again (retry.py),
and its executor's row says 'failed' until then. ETSY SHOP's newest, PINTEREST, BLUESKY, PRINTIFY, the tools that show
them and the daily review's listings showed that row ("[failed] ...: HTTP 429"), so the agent could propose the same
request again while its approval was still to be tried. Now each says it waits, until when, and not to propose it
again; one its service refused after its tries still reads as failed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app.agent import review  # noqa: E402
from app.agent.fake_llm import request_kind  # noqa: E402
from app.economy.clock import to_iso  # noqa: E402
from app.integrations import (  # noqa: E402
    bluesky,
    bluesky_publisher,
    etsy,
    etsy_publisher,
    pinterest,
    pinterest_publisher,
    printify,
    printify_publisher,
    retry,
)
from app.integrations.bluesky import FakeBluesky  # noqa: E402
from app.integrations.etsy import FakeShop  # noqa: E402
from app.integrations.pinterest import FakePinterest  # noqa: E402
from app.integrations.printify import FakePrintify  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.channels.test_approval_boundary import WAIT, refuse  # noqa: E402
from tests.channels.test_bluesky import post_context, post_rows  # noqa: E402
from tests.channels.test_bluesky import proposed as post_proposed  # noqa: E402
from tests.channels.test_etsy import listing_rows  # noqa: E402
from tests.channels.test_pinterest import BOARD, OTHER_WORDS, a_pin, pin_context, pin_rows, pinned  # noqa: E402
from tests.channels.test_printify import made_rows  # noqa: E402
from tests.channels.test_printify import proposed as product_proposed  # noqa: E402
from tests.etsy_helpers import a_change, call, listed, proposed, shop_context  # noqa: E402
from tests.queue_helpers import slot_comes  # noqa: E402


def waits(agent: Any, service: str, asked: datetime) -> str:
    """What a section says of a request ``service`` asked at ``asked`` to wait WAIT seconds for."""
    local = (asked + timedelta(seconds=WAIT)).astimezone(agent.clock.tz)
    when = f"{local:%H:%M}" + ("" if local.date() == agent.clock.today() else " tomorrow")
    return f"{service} asked to come back at {when}; don't propose it again"


def shop(agent: Any) -> str:
    with agent.db.connection() as conn:
        return etsy_publisher.shop_text(conn, agent.scope(), agent.clock, "EmberTestShop", 3)


def reviewed(agent: Any) -> str:
    with agent.db.connection() as conn:
        return review._etsy(conn, agent.scope(), agent.clock, to_iso(agent.clock.now() - timedelta(days=7)))


def title_of(agent: Any, table: str, request: int) -> str:
    [row] = rows(agent, f"SELECT title FROM {table} WHERE approval_id = {request}")
    return str(row["title"])


# --- Etsy ------------------------------------------------------------------------------------------------------------


def test_a_listing_etsy_asked_to_wait_for_reads_as_waiting_until_it_fails_after_its_tries(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    asked = agent.clock.now()
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert listing_rows(agent)[0]["status"] == "failed"  # its executor's row, until it is tried again
    title = " ".join(title_of(agent, "etsy_listings", request).split())[:80]
    said = f"- request #{request} [waiting] {title}: {waits(agent, 'Etsy', asked)}"
    assert f"\nNewest:\n{said}\n" in shop(agent)  # before: "[failed] ...: HTTP 429 (too many requests; ...)"
    assert "[failed]" not in shop(agent) and "HTTP 429" not in shop(agent)
    in_review = review._one_line(title_of(agent, "etsy_listings", request), 80)  # the daily review's listings too
    assert f"- request #{request} [waiting] {in_review}: {waits(agent, 'Etsy', asked)}" in reviewed(agent).splitlines()
    for _ in range(retry.TRIES - 1):
        agent.clock.advance(seconds=WAIT)
        asked = agent.clock.now()
        assert agent.execute_approved() == [(request, retry.LATER)]
    assert f"- request #{request} [waiting] {title}: {waits(agent, 'Etsy', asked)}" in shop(agent)  # the new time
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]  # its last try: it fails as before
    assert f"\nNewest:\n- request #{request} [failed] {title}:" in shop(agent)
    assert f"- request #{request} [failed] {in_review}" in reviewed(agent).splitlines()
    assert "asked to come back" not in shop(agent) + reviewed(agent)


def test_a_time_the_service_named_that_passed_says_which_day_it_was(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # While Ember is paused (or until a pin's or post's slot) the request still waits after the time Etsy named
    agent, _, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    at = (agent.clock.now() + timedelta(seconds=WAIT)).astimezone(agent.clock.tz)
    assert agent.execute_approved() == [(request, retry.LATER)]
    agent.economy.set_paused(True)
    agent.clock.advance(days=1)
    assert agent.execute_approved() == []  # paused: not tried
    assert f": Etsy asked to come back at {at:%H:%M} yesterday; don't propose it again\n" in shop(agent)  # not tomorrow
    agent.clock.advance(days=2)
    assert f": Etsy asked to come back at {at:%H:%M} on {at:%Y-%m-%d}; don't propose it again\n" in shop(agent)


def test_the_plan_s_etsy_shop_says_a_listing_waits_for_etsy(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, fake, request = proposed(data_dir)
    refuse(monkeypatch, FakeShop, "create_draft", etsy.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    asked = agent.clock.now()
    assert agent.execute_approved() == [(request, retry.LATER)]
    before = len(fake.sent)
    agent.run_cycle("schedule")  # within Etsy's wait: the cycle's plan reads it waiting
    plan = next(r for r in list(fake.sent)[before:] if request_kind(r) == "plan")
    text = plan["messages"][0]["content"][0]["text"]
    title = " ".join(title_of(agent, "etsy_listings", request).split())[:80]
    assert "\n== ETSY SHOP ==\n" in text
    assert f"\nNewest:\n- request #{request} [waiting] {title}: {waits(agent, 'Etsy', asked)}\n" in text


def test_a_change_etsy_asked_to_wait_for_says_so_among_the_changes_not_made_yet(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, listing_id = listed(data_dir)
    request = a_change(agent, shop_context(agent), listing_id, title="Weekly Planner, A4")
    assert f"Changes not made yet: request #{request} for #{listing_id} (your owner decides)." in shop(agent)
    refuse(monkeypatch, FakeShop, "update_listing", etsy.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    asked = agent.clock.now()
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert (
        f"Changes not made yet: request #{request} for #{listing_id} (waiting: {waits(agent, 'Etsy', asked)})."
        in shop(agent)
    )  # before: "(approved)", as if Etsy hadn't been asked yet
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "done")]
    assert "Changes not made yet" not in shop(agent)


# --- Pinterest and Bluesky ------------------------------------------------------------------------------------------


def test_a_pin_pinterest_asked_to_wait_for_reads_as_waiting_in_pinterest_and_its_tool(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, _ = pinned(data_dir)
    assert a_pin(agent, pin_context(agent), board_id=BOARD, image="shop/pin-2.png", **OTHER_WORDS).ok  # no new board
    request = rows(agent, "SELECT MAX(id) AS id FROM approvals WHERE executor = 'pinterest_pin'")[0]["id"]
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    slot_comes(agent, "pinterest")
    refuse(monkeypatch, FakePinterest, "create_pin", pinterest.Busy)
    asked = agent.clock.now()
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert pin_rows(agent)[-1]["status"] == "failed"
    said = f"- pin - (waiting): {title_of(agent, 'pinterest_pins', request)[:60]} -> "
    with agent.db.connection() as conn:
        [line] = [x for x in pinterest_publisher.text(conn, agent.scope(), agent.clock).splitlines() if said in x]
    assert line.endswith(f": {waits(agent, 'Pinterest', asked)}")  # before: "- pin - (failed): ...: not read yet"
    assert line in call(pin_context(agent), "pinterest_boards", {}).text.splitlines()  # its tool's list too
    slot_comes(agent, "pinterest")
    assert agent.execute_approved() == [(request, "active")]
    with agent.db.connection() as conn:
        assert "asked to come back" not in pinterest_publisher.text(conn, agent.scope(), agent.clock)


def test_a_post_bluesky_asked_to_wait_for_reads_as_waiting_in_bluesky_and_its_tool(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = post_proposed(data_dir)
    refuse(monkeypatch, FakeBluesky, "create_post", bluesky.Busy)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    slot_comes(agent, "bluesky")
    asked = agent.clock.now()
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert post_rows(agent)[0]["status"] == "failed"
    with agent.db.connection() as conn:
        shown = bluesky_publisher.text(conn, agent.scope(), agent.clock)
    assert shown.startswith(f"- request #{request}, ") and "(waiting): " in shown  # before: "(failed)"
    assert shown.endswith(f": {waits(agent, 'Bluesky', asked)}") and "(failed)" not in shown
    listed_by_tool = call(post_context(agent), "bluesky_posts", {})
    assert f"- request #{request}, " in listed_by_tool.text and waits(agent, "Bluesky", asked) in listed_by_tool.text
    slot_comes(agent, "bluesky")
    assert agent.execute_approved() == [(request, "active")]
    with agent.db.connection() as conn:
        assert "asked to come back" not in bluesky_publisher.text(conn, agent.scope(), agent.clock)


# --- Printify -------------------------------------------------------------------------------------------------------


def test_a_product_printify_asked_to_wait_for_reads_as_waiting_until_it_fails_after_its_tries(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, request = product_proposed(data_dir)
    refuse(monkeypatch, FakePrintify, "upload", printify.Busy, times=99)
    owner(agent).decide(request, {"decision": "approve"}, "Owner")
    asked = agent.clock.now()
    assert agent.execute_approved() == [(request, retry.LATER)]
    assert made_rows(agent)[0]["status"] == "failed"
    title = title_of(agent, "printify_products", request)[:60]

    def shown() -> str:
        with agent.db.connection() as conn:
            return printify_publisher.text(conn, agent.scope(), agent.clock)

    said = f"- {title} (waiting, no Etsy listing yet): {waits(agent, 'Printify', asked)}"
    assert said in shown().splitlines() and "HTTP 429" not in shown()  # before: "(failed, ...): HTTP 429 ..."
    for _ in range(retry.TRIES - 1):
        agent.clock.advance(seconds=WAIT)
        asked = agent.clock.now()
        assert agent.execute_approved() == [(request, retry.LATER)]
    assert f"- {title} (waiting, no Etsy listing yet): {waits(agent, 'Printify', asked)}" in shown()  # the new time
    agent.clock.advance(seconds=WAIT)
    assert agent.execute_approved() == [(request, "failed")]  # its last try: it fails as before
    assert f"- {title} (failed, no Etsy listing yet): HTTP 429" in shown()
    assert "asked to come back" not in shown()
