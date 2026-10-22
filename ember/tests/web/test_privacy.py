"""What Ember shows others of the owner's and other people's data (0.11.2, app/privacy.py)."""

from __future__ import annotations

import pytest

from app.privacy import CODE, REMOVED, Masker, Redactor, _digest, leave_out_third_party, secretish


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # The live report: Etsy's verification code, relayed from a subject line, appeared 6 times.
        ("Subject: Your Etsy verification code is 482913", f"Subject: Your Etsy verification code is {CODE}"),
        ("482913 is your Etsy security code", f"{CODE} is your Etsy security code"),
        ("Ihr Bestätigungscode lautet 123 456.", f"Ihr Bestätigungscode lautet {CODE}."),
        ("Dein Anmeldecode: 7731", f"Dein Anmeldecode: {CODE}"),
        ("Use code K7XH2P to sign in", f"Use code {CODE} to sign in"),
        ("Your one-time passcode: 99812345", f"Your one-time passcode: {CODE}"),
    ],
)
def test_one_time_codes_are_masked(text: str, shown: str) -> None:
    assert Masker()(text) == shown


@pytest.mark.parametrize(
    "text",
    [
        "cap_micros=250000 cycle #39 code_execution_usd_per_hour 0.05",
        "Etsy login refreshed at 2026-09-28T01:00:00Z",
        "listing #1234567890 needs a new login",
        "The code costs $1500 per login",
        "Order 12.50 EUR, security deposit",
        "Year 2026 and 2027 planners with 4 photos",
    ],
)
def test_numbers_that_are_not_codes_stay(text: str) -> None:
    assert Masker()(text) == text


def test_addresses_are_numbered_and_embers_own_is_named() -> None:
    masker = Masker(own_address="Ember@Mailbox.org")
    shown = masker("From: Jane Doe <jane.doe@example.com>, cc ember@mailbox.org; JANE.DOE@example.com, bo@x.de")
    assert shown == "From: Jane Doe <[email 1]>, cc [Ember's address]; [email 1], [email 2]"
    assert masker("again bo@x.de") == "again [email 2]"  # the same number all through one report


def test_tokens_in_links_are_masked_but_not_what_names_a_page() -> None:
    masker = Masker()
    assert masker("Log in: https://www.etsy.com/verify?token=abcDEF123456&x=1. Done") == (
        "Log in: https://www.etsy.com/verify?[…]. Done"
    )
    assert masker("https://shop.example.com/reset/aB3dE5fG7hJ9kL1mN3pQ5 and https://x.io/a#frag") == (
        "https://shop.example.com/reset/[…] and https://x.io/a#[…]"
    )
    assert masker("https://x.io/r/123e4567-e89b-12d3-a456-426614174000/done") == "https://x.io/r/[…]/done"
    slug = "https://www.etsy.com/listing/1234567890/haushaltsbuch-2027-printable"
    assert masker(slug) == slug


def test_other_peoples_text_is_left_out_unless_the_owner_asks_for_it() -> None:
    wrapped = (
        'ok <data src="email:3" id="ab12">\nFrom: Jane\nHi, my code is below\n</data id="ab12"> then '
        '<data src="research" id="ab12">\nweb text\n</data id="ab12"> and '
        '<data src="workspace:a.md" id="ab12">\nmine\n</data id="ab12">'
    )
    shown = leave_out_third_party(wrapped)
    assert shown == (
        'ok <data src="email:3">[31 characters of other people\'s text left out]</data> then '
        '<data src="research">[8 characters of other people\'s text left out]</data> and '
        '<data src="workspace:a.md" id="ab12">\nmine\n</data id="ab12">'  # the agent's own file stays
    )
    assert Masker()(wrapped) == shown and Masker(full=True)(wrapped) == wrapped
    cut = 'cut <data src="research" id="ff00">\nno end'  # a text cut before its end tag
    assert (
        leave_out_third_party(cut) == 'cut <data src="research">[6 characters of other people\'s text left out]</data>'
    )


def test_masking_twice_changes_nothing() -> None:
    masker = Masker(own_address="ember@mailbox.org")
    text = (
        "Your verification code is 482913, order 4521. 482913 is your code. See https://x.io/v?t=abc and "
        'https://x.io/c/aB3dE5fG7hJ9kL1mN3pQ5 from jo@x.de <data src="email:1" id="a1">\nhi\n</data id="a1">'
    )
    once = masker(text)
    assert masker(once) == once and "4521" in once


@pytest.mark.parametrize(
    ("word", "secret"),
    [
        ("aaaaaa-0bbbbb-ccccCc", True),  # a generated password
        ("Passwort2024!", True),
        ("01701234567", True),  # a phone number
        ("+4917012345678", True),
        ("DE89370400440532013000", True),  # an IBAN
        ("user@example.com", True),
        ("https://example.com/x/y?z=1", True),
        ("password", False),  # the words around a secret stay readable elsewhere
        ("EmberTheHelper", False),
        ("Weihnachts-Quiz", False),
        ("2026-10-03", False),  # a date
        ("1234567890", False),  # an id (a listing's)
        ("Weihnachtsgeschenkideen", False),
    ],
)
def test_which_words_look_secret(word: str, secret: bool) -> None:
    assert secretish(word) is secret


def test_removed_words_are_found_wherever_they_were_copied() -> None:
    password = "aaaaaa-0bbbbb-ccccCc"
    redactor = Redactor("salt", frozenset({_digest("salt", password)}))
    assert not Redactor() and redactor
    assert redactor.apply(f'login: {password}, {{"text":"login:{password}\\nnext"}}') == (
        f'login: {REMOVED}, {{"text":"login:{REMOVED}\\nnext"}}'
    )
    assert redactor.apply(f"**{password}**! and '{password}'. user@{password}") == (
        f"**{REMOVED}**! and '{REMOVED}'. user@{REMOVED}"
    )
    assert redactor.apply("aaaaaa-0bbbbb-ccccCd and nothing else") == "aaaaaa-0bbbbb-ccccCd and nothing else"
    assert Redactor("other salt", redactor.digests).apply(password) == password  # the salt is part of the hash
