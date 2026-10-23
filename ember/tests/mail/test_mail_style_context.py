"""0.45.2: a style sheet's rule applies only inside the elements its selector names (Ember couldn't read KDP's notices).

Live, Ember read Amazon KDP's "Attention needed" emails #64 and #65 about books #140 and #141 as their footer and case
number only, and told its owner they said nothing. Their template colours the text of its dark bands with rules like
"#templateFooter .endrTextContent {color:#fbfbfb}", and the hidden-text filter took only a selector's last part: every
.endrTextContent, the white body's too, was near-white on white, so the notice (the cover's text too close to its
edges) was dropped as hidden. A colour or background rule's context counts now; ">" is taken as any element outside,
and a sibling ("+", "~"), a part Ember can't read and a tag a browser adds (body, tbody) aren't checked, so a rule
never selects less than in a browser. A rule that hides still hides wherever its last part is: Ember's tree of an
email's elements isn't a browser's, and hidden text must not get through where the two differ."""

from __future__ import annotations

import time
from email.message import EmailMessage

from app.integrations import mail

# The shape of KDP's notice: its template's rules and blocks, its words shortened
KDP_NOTICE = (
    "<html><head><style>"
    ".endrTextContent{word-break:break-word;padding-top:15px;text-align:left}"
    "#templatePreheader .endrTextContent,#templatePreheader .endrTextContent p{color:#fbfbfb;font-size:12px}"
    "#templateHeader .endrTextContent,#templateHeader .endrTextContent p{color:#ffffff;font-size:13px}"
    ".templateLowerBody{background-color:#455c64}"
    ".templateLowerBody .endrTextContent,.templateLowerBody .endrTextContent p{color:#ffffff;font-size:13px}"
    "#templateFooter .endrTextContent,#templateFooter .endrTextContent p{color:#fbfbfb;font-size:12px}"
    ".templateBlocks{background-color:#ffffff;font-size:15px}"
    "</style></head><body>"
    '<div style="display:none;font-size:1px;max-height:0px;opacity:0;overflow:hidden;mso-hide:all">PREVIEW</div>'
    '<table><tr><td class="templateBlocks"><table><tr><td valign="top" class="endrTextContent">'
    "<p>Hi!</p><p>We checked your files and found issues you need to fix before your book can be published on KDP:</p>"
    "<p>Text on your book cover is too close to the edges. Please move all text at least 0.375 inches away from all"
    " edges of the cover.</p><p>The Kindle Direct Publishing team</p>"
    "</td></tr></table></td></tr></table>"
    '<table><tr><td valign="top" class="templateLowerBody" style="background-color:#2a323a">'
    '<p style="color:#ffffff">©2026 Amazon.com, Inc. or its affiliates.</p><p>SPC-Case-1</p></td></tr></table>'
    '<table id="templateFooter" style="background-color:#232f3e"><tr><td class="endrTextContent">'
    "Sent by Amazon</td></tr></table>"
    "</body></html>"
)


def test_kdp_s_notice_is_read_whole() -> None:
    text = mail.html_to_text(KDP_NOTICE)
    assert text.startswith("Hi!\n\nWe checked your files and found issues")
    assert "too close to the edges. Please move all text at least 0.375 inches" in text
    assert "The Kindle Direct Publishing team" in text and text.endswith("SPC-Case-1\n\nSent by Amazon")
    assert "PREVIEW" not in text  # its preview text is hidden as it was
    notice = EmailMessage()
    notice["From"], notice["To"] = "KDP <kdp-noreply@amazon.example>", "ember@example.invalid"
    notice["Subject"] = "Attention needed: Please review your title Household Budget Book 2027"
    notice.set_content(KDP_NOTICE, subtype="html")
    stored = mail.parse_message(notice.as_bytes(), 65)
    assert "Text on your book cover is too close to the edges." in stored.body and not stored.body_cut


def test_a_colour_applies_only_inside_its_context() -> None:
    sheet = "<style>.card .t{color:#fff} #box p.h{color:#fefefe} .x .y .g{color:white}</style>"
    html = (
        f'{sheet}<div class="card"><div><p class="t">HIDDEN-1</p></div></div><p class="t">Shown 1.</p>'
        '<div id="box"><p class="h">HIDDEN-2</p></div><div id="other"><p class="h">Shown 2.</p></div>'
        '<div class="x"><div class="y"><p class="g">HIDDEN-3</p></div></div>'
        '<div class="y"><div class="x"><p class="g">Shown 3.</p></div></div>'  # its parts the other way round
    )
    text = mail.html_to_text(html)
    assert "HIDDEN" not in text and text.split() == ["Shown", "1.", "Shown", "2.", "Shown", "3."]
    # A white text in its context on a dark band stays, and the same rule doesn't make text elsewhere white
    band = '<style>#band .t{color:#fff}</style><div id="band" style="background:#000"><p class="t">On the band.</p>'
    assert mail.html_to_text(f'{band}</div><p class="t">On the page.</p>') == "On the band.\n\nOn the page."


def test_what_ember_can_t_check_selects_more_never_less() -> None:
    # ">" is taken as any element outside; a sibling before "+" or "~", a part Ember can't read and a tag a browser
    # adds where the email leaves it out aren't checked
    sheet = "<style>.a > .b{color:#fff} .s + .h{color:#fff} .s ~ .g{color:#fff} one-column .k{color:#fff}"
    html = (
        f'{sheet} body .m{{color:#fff}} table tbody tr .n{{color:#fff}}</style><div class="a"><span><i class="b">'
        'HIDDEN-1</i></span></div><p class="h">HIDDEN-2</p><p class="g">HIDDEN-3</p><p class="k">HIDDEN-4</p>'
        '<p class="m">HIDDEN-5</p><table><td class="n">HIDDEN-6</td></table><p>Shown.</p>'
    )
    assert mail.html_to_text(html) == "Shown."
    # A tag alone after a context isn't read, as before
    assert mail.html_to_text("<style>.a td{color:#fff}</style><table><tr><td>Shown.</td></tr></table>") == "Shown."


def test_a_rule_that_hides_does_so_outside_its_context_too() -> None:
    """As before 0.45.2: Ember's tree isn't a browser's. Here a browser ignores the </div> inside the cell, so the text
    stays inside .x and is hidden; Ember closes the div there."""
    stray = '<style>.x .h{display:none}</style><div class="x"><table><tr><td></div><p class="h">HIDDEN</p>'
    assert mail.html_to_text(stray + "<p>Shown.</p>") == "Shown."
    elsewhere = "<style>#nowhere .h{visibility:hidden} .y > .g{font-size:0}</style>"
    assert mail.html_to_text(f'{elsewhere}<p class="h">HIDDEN</p><p class="g">HIDDEN</p><p>Shown.</p>') == "Shown."


def test_looking_for_contexts_is_read_in_time() -> None:
    """Each rule whose context no element has looks at every element outside: that counts toward _STYLE_WORK."""
    rules = "".join(f".z{n} .h{{color:red}}" for n in range(mail._KEY_RULES))
    html = f"<style>{rules}</style>" + "<div>" * 250 + '<p class="h">x</p>' * 40_000
    started = time.monotonic()
    text = mail.html_to_text(html[: mail.MAX_MESSAGE_BYTES])
    assert time.monotonic() - started < 20
    assert text.startswith("x\n\nx") and text.endswith("has more styling than any real email, so it wasn't read.]")
