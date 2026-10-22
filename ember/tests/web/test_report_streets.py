"""0.42.3 (analysis-0.40.1 audit): the shareable report masks street addresses.

Since 0.40.3 the Masker masks phone numbers and IBANs besides email addresses, codes and link tokens, and the owner's
own address only as they wrote it in their options. A street address anywhere else went out whole: in their message
("Retouren bitte an Hauptstr. 5a, 1010 Wien"), in the agent's notes and drafts (the Impressum's lines), in a sender's
signature the agent quoted. The public live page refused "a street with its postcode" by its run of 5 digits only,
which can't be the report's rule: every listing number, order number and token count would go. Now a street is told
by its words (privacy._STREET): German and English forms, with the postcode and town that follow, and the numbers of
a report stay.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("httpx2")

from app import diagnostics, privacy  # noqa: E402
from app.integrations import etsy_publisher  # noqa: E402
from tests.agent_helpers import owner  # noqa: E402
from tests.etsy_helpers import listed  # noqa: E402
from tests.web.test_report_masking import a_kdp_request  # noqa: E402
from tests.web.test_report_privacy import LABEL, section, shared_and_full, state_of  # noqa: E402

STREET = privacy.STREET


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # the owner's message, as the report's messages table shows it
        ("Retouren bitte an Musterstraße 12, 80331 München. Danke!", f"Retouren bitte an {STREET}. Danke!"),
        ("Abholung: Hauptstr. 5a, 1010 Wien", f"Abholung: {STREET}"),
        ("Pickup at Am Markt 3 on Monday", f"Pickup at {STREET} on Monday"),
        ("Neue Adresse: Karl-Marx-Allee 90 | 2026-10-21", f"Neue Adresse: {STREET} | 2026-10-21"),
        ("Berliner Straße 5-7a, Hinterhaus", f"{STREET}, Hinterhaus"),
        # a draft of the Impressum: in a cell its lines are joined by ⏎, in a file it is indented, as HTML <br>
        ("Stefan Muster ⏎ Musterstraße 1 ⏎ 12345 Berlin ⏎ Tel.", f"Stefan Muster ⏎ {STREET} ⏎ Tel."),
        ("    Stefan Muster\n    Musterweg 7\n    12345 Berlin\n", f"    Stefan Muster\n    {STREET}\n"),
        ("Stefan Muster<br>Musterstraße 1<br>12345 Berlin", f"Stefan Muster<br>{STREET}"),
        ('{"address": "Bahnhofstrasse 1, 8001 Zürich"}', f'{{"address": "{STREET}"}}'),
        # a street of other words only with its postcode and town
        ("Unter den Linden 77, 10117 Berlin", STREET),
        ("An der Alster 5 ⏎ 20099 Hamburg", STREET),
        # English: a buyer's signature the agent quoted
        ("Jane Doe\n12 Main Street, Springfield, IL 62701\nUSA", f"Jane Doe\n{STREET}\nUSA"),
        ("221B Baker Street, London NW1 6XE.", f"{STREET}."),
        ("ship to 1600 Pennsylvania Avenue NW, Washington, DC 20500", f"ship to {STREET}"),
        ("She lives at 4 Privet Drive.", f"She lives at {STREET}."),
        ("10 Downing St, then the post office", f"{STREET}, then the post office"),
    ],
)
def test_street_addresses_are_masked(text: str, shown: str) -> None:
    assert privacy.Masker()(text) == shown
    assert privacy.Masker(full=True)(text) == text  # the owner's own report


@pytest.mark.parametrize(
    "text",
    [
        "listing #900000001 at 4.99 EUR, 12,345 views, 3 favorites",
        "KDP paperback: 120 pages, 6 x 9 in, Haushaltsbuch 2027, 4.99 USD",
        "input_tokens=12345 output_tokens=678 cost $0.0421 (cycle 12345)",
        "receipt 3456789012 | 2026-10-21 | 12.99 EUR | 2 items | Order #12345",
        "Printify product 5f2b1c3d4e5f6a7b8c9d0e1f: 3 variants of 24 Wedding Place Cards",
        "Weihnachtsmarkt 2026: Speicherplatz 5 GB, Platz 1 bei Etsy, Weg 2: Pinterest, Der Weg 2 ist kurz",
        "Am Montag 3 Pins, im Januar 12 Verkäufe, Am Ende 3 Bücher; Kapitel 3, 12000 Wörter",
        "2 Google Drive links, a set of 50 Round Circle Labels, Top 10 Best Way to plan",
        "Versand aus 80331 München, Lager in 1010 Wien",  # a postcode and town alone
        "Woche 12 Montag Bilanz | 18 | version 0.42.3, migration 0098 | 10.000 Aufrufe",
    ],
)
def test_the_numbers_of_a_report_stay_beside_a_masked_street(text: str) -> None:
    """A street goes, and the prices, ids, years, order numbers, counts, page and token counts beside it stay."""
    assert privacy.Masker()(f"{text} | Musterstraße 12") == f"{text} | {STREET}"


def test_the_public_page_names_a_street_without_its_postcode() -> None:
    """unpublishable refused a street only by its postcode's 5 digits: "Hauptstr. 5a" passed it."""
    assert privacy.unpublishable("Abholung: Hauptstr. 5a") == "a street address"
    assert privacy.unpublishable("Musterstraße 12, 12345 Berlin") == "a number of 5 digits or more"  # as before
    assert privacy.unpublishable("Speicherplatz 5 GB, Haushaltsbuch 2026-2027") is None


def test_a_whole_report_masks_the_streets_and_nothing_else(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four cycles of the fake model, a live listing, a KDP request, the owner's messages and a draft: every street
    planted goes, and the report masks nothing it didn't mask without the street pattern."""
    agent, listing_id = listed(data_dir)
    a_kdp_request(agent, "Jane Doe")
    for message in (
        "Retouren bitte an Hauptstr. 5a, 1010 Wien. Danke!",
        "Our US office: 12 Main Street, Springfield, IL 62701 (since 2019, 3 desks).",
    ):
        assert owner(agent).send_message({"text": message}, LABEL).status == 201
    workspace, _ = agent.roots()
    workspace.write("site/impressum.md", "# Impressum\n\nStefan Muster\nMusterstraße 1\n12345 Berlin\n\nSeit 2019.\n")
    shared, full = shared_and_full(agent)
    planted = ("Hauptstr. 5a", "12 Main Street", "Musterstraße 1")
    for street in planted:
        assert street in full and street not in shared, street
    assert "Hauptstr. 5a, 1010 Wien" not in shared and "Springfield, IL" not in shared and "12345 Berlin" not in shared
    assert f"Retouren bitte an {STREET}. Danke!" in section(shared, "AGENT RECORDS")
    assert f"    Stefan Muster\n    {STREET}\n    \n    Seit 2019." in shared  # the draft, indented
    # nothing else: without the street pattern, the report differs only where the planted streets are
    monkeypatch.setattr(privacy, "_STREET", re.compile(r"(?!)"))
    unmasked = diagnostics.report(state_of(agent))
    assert STREET not in unmasked
    assert shared.count(STREET) == sum(unmasked.count(street) for street in planted) > len(planted)
    # and what a report's numbers are: the listing, its price, the book, the calls' tokens
    with agent.db.connection() as conn:
        listing = etsy_publisher.current_listing(conn, agent.scope(), listing_id)
    assert listing is not None
    shown = (f"{listing_id}", f"{listing.price} {listing.currency}", "Haushaltsbuch 2027", "Price: 4.99", "since 2019")
    for number in (*shown, "input_tokens"):
        assert number in shared, number
