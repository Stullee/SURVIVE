"""0.45.0 (evaluation 2026-10-22, C1): who made it. Ember's code's own AI lines name the maker, Ember, an AI agent,
and every request tool refuses a public text that says a person made her work, promises legal certainty or says "no
refunds". Live, in the week 10-12 to 10-18, 13 of her 39 requests did, and her owner approved them all."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import claims, prompts
from app.agent.fake_llm import FakeTransport
from app.config import Settings
from app.integrations import etsy, kdp, pinterest, printify
from tests.agent_helpers import rows, run
from tests.channels import test_blog as blog_tests
from tests.channels import test_pinterest as pin_tests
from tests.etsy_helpers import call
from tests.products import test_kdp as kdp_tests

# what her requests of the week said (the evaluation's finding 1 and 2), as she wrote it
LIVE_AUTHORSHIP = [
    "Designed by a human seller with AI assistance",
    "Designed by the shop owner, AI-assisted",
    "designed by a solo creator with AI help",
    "Designed by a human with AI assistance",
    "Von einem Menschen gestaltet",
    "Designed by the shop owner with AI assistance",
    "Designed by a human, with AI assistance",
    "Designed by the author with the help of an AI tool (Ember)",
    "Gestaltet von einem Menschen, mit KI-Hilfe",
    "Liebevoll von der Shop-Inhaberin entworfen",
    "Made with care by a small team",
]
LIVE_LEGAL = ["Rechtssicher abrechnen", "§ 556 BGB konform", "GoBD-konform", "DSGVO-konform", "legally compliant"]
LIVE_REFUNDS = ["Keine Rückerstattung nach Lieferung", "no refunds", "Widerruf ausgeschlossen", "Non-refundable."]
# honest words, and words that only look alike
HONEST = [
    etsy.DISCLOSURE,
    pinterest.DISCLOSURE,
    printify.DISCLOSURE,
    kdp.DISCLOSURE,
    "Made by Ember and checked by the seller",
    "🤖 Von einer KI geschrieben, von einem Menschen freigegeben.",
    "Gestaltet von Ember (KI) mit menschlicher Prüfung",
    "A content planner for solo creators and small business owners",
    "Anschreiben nach DIN 5008, DIN 5008-konform",
    "ATS-konformer Lebenslauf",
    "Track refunds and returns in one sheet",
    "Space for handwritten notes, made for busy parents",
]


@pytest.mark.parametrize("text", LIVE_AUTHORSHIP)
def test_a_text_that_says_a_person_made_it_is_refused(text: str) -> None:
    said = claims.refusal([f"A weekly planner. {text}."])
    assert said.startswith("it says a person made it (") and claims.INSTEAD in said
    assert said.endswith("Nothing went to your owner: write it again without that, and propose it again")


@pytest.mark.parametrize("text", LIVE_LEGAL)
def test_a_promise_of_legal_certainty_is_refused(text: str) -> None:
    assert claims.refusal([text]).startswith(f'it promises legal certainty ("{text.split()[0]}')


@pytest.mark.parametrize("text", LIVE_REFUNDS)
def test_a_no_refunds_line_is_refused(text: str) -> None:
    assert "the law on withdrawal and warranty decides that" in claims.refusal([text])


@pytest.mark.parametrize("text", HONEST)
def test_honest_words_pass(text: str) -> None:
    assert claims.refusal([text]) == ""


def test_the_ai_lines_name_the_maker_and_the_constitution_says_it() -> None:
    for line in (etsy.DISCLOSURE, pinterest.DISCLOSURE, printify.DISCLOSURE, kdp.DISCLOSURE):
        assert "Ember, an AI agent" in line and "help of AI" not in line
    assert pinterest.DESCRIPTION_CHARS + len(pinterest.DISCLOSURE) + 2 == pinterest.DESCRIPTION_MAX
    # a live listing's old line, copied with its description, isn't left above the new one
    assert etsy.check_description(f"Words.\n\n{etsy.EARLIER[0]}") == "Words."
    assert kdp.check_description(f"Words.\n\n{kdp.EARLIER[0]}") == "Words."
    constitution = " ".join(prompts.constitution(Settings()).split())
    assert "Your work is made by you, an AI agent, never by a person." in constitution
    assert "Designed by" not in prompts.knowledge()


def test_every_request_tool_refuses_such_a_text_before_it_reaches_the_owner(data_dir: Path) -> None:
    agent, _ = pin_tests.listed(data_dir)
    ctx = pin_tests.pin_context(agent)
    pin = pin_tests.a_pin(agent, ctx, board_name="Planners", description="Plan your week. Designed by a human seller.")
    assert not pin.ok and 'it says a person made it ("Designed by a human seller")' in pin.text
    listing_id = pin_tests.LISTING
    edit = call(
        ctx,
        "propose_etsy_edit",
        {"listing_id": listing_id, "description": "Rechtssicher, GoBD-konform. Keine Rückerstattung.", "reason": "r"},
    )
    assert not edit.ok and 'it promises legal certainty ("Rechtssicher")' in edit.text
    assert 'it says "Keine Rückerstattung"' in edit.text
    assert rows(agent, "SELECT id FROM approvals WHERE executor IN ('pinterest_pin', 'etsy_edit')") == []
    # the words a change replaces aren't its own: a change that takes the claim out goes through
    honest = call(ctx, "propose_etsy_edit", {"listing_id": listing_id, "description": "A planner.", "reason": "r"})
    assert honest.ok, honest.text
    # request_approval: the owner carries it out, and what it quotes is no claim of its own
    asked = {
        "type": "other",
        "title": "Correct pin #102's words",
        "description": "It says a person made it.",
        "expected_cost": "$0",
        "expected_benefit": "honest pins",
    }
    quoting = 'In pin #102, replace "Designed by a human seller with AI assistance" with "Made by Ember, an AI agent".'
    assert call(ctx, "request_approval", {**asked, "payload": quoting}).ok
    claiming = call(ctx, "request_approval", {**asked, "payload": "Put this on the flyer: designed by the shop owner."})
    assert not claiming.ok and 'it says a person made it ("designed by the shop owner")' in claiming.text


def test_a_book_s_words_and_its_cover_s_are_checked_first(data_dir: Path) -> None:
    _, ctx = kdp_tests.publishing(data_dir)
    jail = ctx.workspace
    interior = kdp_tests.paperback(jail)
    claimed = kdp_tests.spec(jail, interior, description="A journal. Designed by the author with an AI tool.")
    refused = call(ctx, "propose_kdp_book", {"spec": claimed, "check": True})
    assert not refused.ok and 'it says a person made it ("Designed by the author")' in refused.text
    assert jail.size_of("books/journal-cover.pdf") is None  # refused before a cover was made
    back = kdp_tests.spec(jail, interior, cover={"front": "books/front.png", "back": "Von einem Menschen gestaltet."})
    assert (
        'it says a person made it ("Von einem Menschen gestaltet")'
        in call(ctx, "propose_kdp_book", {"spec": back, "check": True}).text
    )


def test_a_blog_post_s_article_is_checked(data_dir: Path) -> None:
    agent, _ = run(data_dir, FakeTransport(), cycles=1, settings=blog_tests.BLOG)
    agent.roots()[0].write("blog/post.md", blog_tests.POST + "\nDiese Vorlage ist rechtssicher.\n")
    ctx = blog_tests.blog_context(agent)
    refused = call(ctx, "propose_blog_post", {"source": "blog/post.md", "reason": "Search traffic."})
    assert not refused.ok and 'it promises legal certainty ("rechtssicher")' in refused.text
    assert rows(agent, "SELECT id FROM approvals WHERE executor = 'site_post'") == []


def test_the_maker_is_named_once() -> None:
    """0.53.0 (evaluation 2026-10-27, C6): live, #144 and #146 named the maker twice, in her words ("Made by Ember, an
    AI agent, and checked by the seller before publication.", "This listing was made by Ember, ...") and in the line
    Ember's code adds. Her own maker sentences give way to the code's line; her other words stay."""
    hers = "Fixed layout grid, easy to fill in by hand\n\nMade by Ember, an AI agent, and checked by the seller."
    assert kdp.with_disclosure(kdp.check_description(hers)) == (
        f"Fixed layout grid, easy to fill in by hand\n\n{kdp.DISCLOSURE}"
    )
    listed = (
        "Licence terms in plain words. This listing was made by Ember, an AI agent, working for a human shop owner"
        " who reviews each listing. No legal certainty is promised.\n\nInstant digital download."
    )
    assert etsy.check_description(listed) == (
        "Licence terms in plain words. No legal certainty is promised.\n\nInstant digital download."
    )
    assert claims.without_maker("Erst die Fixkosten. Vorlage von Ember, einem KI-Agenten, erstellt.") == (
        "Erst die Fixkosten."
    )
    for kept in (
        "A planner for people who love Ember the cat.",
        "Made by Ember's owner? No: made by Ember, an AI agent, and checked by them.",  # inside a sentence: hers
        "Ember made the cover; the seller checked it.",
    ):
        assert claims.without_maker(kept) == kept
