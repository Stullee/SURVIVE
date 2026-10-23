"""0.47.0 (vision/generations.md): an evaluation judges the estate from the data. Its feedback.json may propose a
verdict for each product Ember would hand on (adopt, passive, retire, with why); the owner's card shows it beside
the rule's suggestion, and the owner decides. The proposals are the owner's: nothing of them reaches Ember, and a
feedback of proposals only sends her no message."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx2")

from app import feedback  # noqa: E402
from app.agent import estate  # noqa: E402
from tests.agent_helpers import owner, rows  # noqa: E402
from tests.generation_helpers import settled  # noqa: E402

WHY = "0 views in 52 days with no reach done for it: a fair test without a buyer's signal"


def proposing(listing: int, **more: Any) -> dict[str, Any]:
    return {
        "format": 1,
        "evaluation": "2026-11-01 estate",
        "estate": [{"kind": "etsy_listing", "ref": str(listing), "verdict": "passive", "why": WHY}],
        **more,
    }


def test_an_evaluations_proposals_go_to_the_owners_card_and_not_to_her(data_dir: Path) -> None:
    agent, listing = settled(data_dir)
    messages = len(rows(agent, "SELECT id FROM messages"))
    shown = feedback.preview(owner(agent), proposing(listing))
    assert shown["message"] == "" and shown["cases"] == []
    assert shown["estate"] == [{"kind": "etsy_listing", "ref": str(listing), "verdict": "passive", "why": WHY}]
    given = feedback.give(owner(agent), proposing(listing), "Owner")
    assert given["message_id"] is None and len(rows(agent, "SELECT id FROM messages")) == messages  # nothing to her
    with agent.db.connection() as conn:
        [row] = estate.handover(conn, agent.scope())
        [listed] = feedback.given(conn, agent.scope())
    assert (row["proposed"], row["proposed_why"], row["proposed_by"]) == (
        "passive",
        WHY,
        "evaluation 2026-11-01 estate",
    )
    assert row["verdict"] is None  # the owner decides
    assert listed["estate"] == 1 and listed["message_id"] is None
    with pytest.raises(feedback.FeedbackError, match="given to Ember already"):
        feedback.give(owner(agent), proposing(listing), "Owner")


def test_her_message_carries_the_cases_and_corrections_and_never_the_proposals(data_dir: Path) -> None:
    agent, listing = settled(data_dir)
    correction = {"right": "KDP's week of new titles starts every Sunday at 00:00 UTC"}
    shown = feedback.preview(owner(agent), proposing(listing, corrections=[correction]))
    assert "Sunday" in shown["message"] and str(listing) not in shown["message"] and "passive" not in shown["message"]
    given = feedback.give(owner(agent), proposing(listing, corrections=[correction]), "Owner")
    [message] = rows(agent, f"SELECT text FROM messages WHERE id = {given['message_id']}")
    assert str(listing) not in message["text"] and WHY not in message["text"]


@pytest.mark.parametrize(
    ("proposal", "says"),
    [
        ({"kind": "etsy_listing", "ref": "1", "verdict": "passive", "why": WHY}, "no Etsy listing 1 in the shop now"),
        ({"kind": "pin", "ref": "1", "verdict": "passive", "why": WHY}, "kind is one of"),
        ({"kind": "etsy_listing", "ref": "LISTING", "verdict": "keep", "why": WHY}, "verdict is one of"),
        ({"kind": "etsy_listing", "ref": "LISTING", "verdict": "adopt", "why": ""}, "needs its why"),
        ({"kind": "etsy_listing", "ref": "LISTING", "verdict": "adopt", "why": WHY, "score": 3}, "takes only"),
    ],
)
def test_a_proposal_names_a_product_of_the_shop_a_verdict_and_why(
    data_dir: Path, proposal: dict[str, Any], says: str
) -> None:
    agent, listing = settled(data_dir)
    proposal = {k: (str(listing) if v == "LISTING" else v) for k, v in proposal.items()}
    with pytest.raises(feedback.FeedbackError, match=says):
        feedback.preview(owner(agent), {"format": 1, "evaluation": "x", "estate": [proposal]})


def test_one_proposal_for_each_product(data_dir: Path) -> None:
    agent, listing = settled(data_dir)
    twice = proposing(listing)
    twice["estate"] = twice["estate"] * 2
    with pytest.raises(feedback.FeedbackError, match="once for each product"):
        feedback.preview(owner(agent), twice)
