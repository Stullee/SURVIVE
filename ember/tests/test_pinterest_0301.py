"""0.30.1: a Pinterest app with Trial access connects, but Pinterest refuses its pins (HTTP 403, "Apps with Trial access
may not create Pins in production - use API Sandbox instead"). An approved pin failed with those words, which point to
a sandbox whose pins nobody but their maker sees (Ember doesn't use it), and nothing said what the owner can do. Such a
refusal of a pin or a board now says that the app needs Standard access: on the request, in what Ember's code did and
in the System log, and the agent hears it at its next wake."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from app.agent.fake_llm import request_kind  # noqa: E402
from app.integrations import pinterest  # noqa: E402
from app.integrations.pinterest import NotSent, Pin, TokenFile  # noqa: E402
from app.integrations.pinterest_live import NEEDS_STANDARD, LiveAccount  # noqa: E402
from tests.test_agent import rows  # noqa: E402
from tests.test_etsy import views_approval  # noqa: E402
from tests.test_owner_loop import owner  # noqa: E402
from tests.test_pinterest import LIVE, live_account, mock, proposed, some_tokens  # noqa: E402

BOARD = "5550001"


def trial(what: str = "Pins") -> Any:
    """Pinterest's answer to an app with Trial access."""
    message = f"Apps with Trial access may not create {what} in production - use API Sandbox instead"
    return httpx2.Response(403, json={"code": 29, "message": message})


def a_live_pin() -> Pin:
    upload = pinterest.image("shop/pin.png", b"png")
    return Pin("Planner", "Plan your week.", "https://www.etsy.com/listing/1", "A planner", upload, 10, 15)


def test_a_refusal_of_trial_access_says_the_app_needs_standard_access(tmp_path: Path) -> None:
    account, _ = live_account(tmp_path, {("POST", "/v5/pins"): trial(), ("POST", "/v5/boards"): trial("Boards")})
    with pytest.raises(NotSent) as refused:
        account.create_pin(BOARD, a_live_pin(), b"png")
    assert str(refused.value) == NEEDS_STANDARD
    assert NEEDS_STANDARD.startswith("Standard access needed: the Pinterest app has Trial access")
    assert "developers.pinterest.com" in NEEDS_STANDARD and "Sandbox" not in NEEDS_STANDARD
    with pytest.raises(NotSent, match="^Standard access needed: "):
        account.create_board("Meal planning printables", "")
    # Any other refusal keeps Pinterest's words.
    other, _ = live_account(
        tmp_path / "other",
        {("POST", "/v5/pins"): httpx2.Response(403, json={"message": "Not authorized to access board or pin."})},
    )
    with pytest.raises(NotSent, match=r"^HTTP 403: Not authorized to access board or pin\.$"):
        other.create_pin(BOARD, a_live_pin(), b"png")


def trial_account(agent: Any, tmp_path: Path, refused: str) -> None:
    """The owner's live account (connected, Trial access) in place of the dry run's fake one."""
    answers: dict[tuple[str, str], Any] = {
        ("POST", "/v5/boards"): {"id": BOARD, "name": "Meal planning printables"},
        ("POST", "/v5/pins"): trial(),
    }
    if refused == "board":
        answers[("POST", "/v5/boards")] = trial("Boards")
    store = TokenFile(tmp_path / "tokens.json")
    store.save(some_tokens(agent.clock))
    _, transport = mock(answers)
    live = LiveAccount(LIVE, agent.clock, store, transport)
    agent.pins.account = lambda: live


@pytest.mark.parametrize(
    ("refused", "note", "journal"),
    [
        (
            "pin",
            f"Not pinned: Pinterest refused it ({NEEDS_STANDARD})",
            [("pinterest.create_board", "done"), ("pinterest.create_pin", "failed")],
        ),
        (
            "board",
            f"Not pinned: the new board couldn't be made ({NEEDS_STANDARD})",
            [("pinterest.create_board", "failed"), ("pinterest.create_pin", "failed")],
        ),
    ],
    ids=["pin", "board"],
)
def test_the_owner_and_the_agent_hear_that_the_app_needs_standard_access(
    data_dir: Path, tmp_path: Path, refused: str, note: str, journal: list[tuple[str, str]]
) -> None:
    agent, fake, request = proposed(data_dir)  # on a new board
    trial_account(agent, tmp_path, refused)
    assert owner(agent).decide(request, {"decision": "approve"}, "Owner").status == 200
    assert agent.execute_approved() == [(request, "failed")]
    closed = rows(agent, f"SELECT status, closed_by, result_note FROM approvals WHERE id = {request}")[0]
    assert (closed["status"], closed["closed_by"], closed["result_note"]) == ("failed", "Ember", note)
    shown = views_approval(agent, request)["execution"]
    assert (shown["status"], shown["result"], shown["error"]) == ("failed", note, NEEDS_STANDARD)
    done = rows(agent, f"SELECT class, status, note FROM action_journal WHERE approval_id = {request} ORDER BY id")
    assert [(j["class"], j["status"]) for j in done] == journal
    assert NEEDS_STANDARD in done[-1]["note"]  # what Ember's code did
    assert any(e["message"].startswith(f"Request #{request}: {note[:60]}") for e in agent.db.recent_events(limit=20))
    assert agent.execute_approved() == []  # never tried again
    before = len(fake.sent)
    agent.run_cycle("schedule")
    plan = next(r for r in list(fake.sent)[before:] if request_kind(r) == "plan")
    assert NEEDS_STANDARD in plan["messages"][0]["content"][0]["text"]
