"""4.2 after 0.39.5: a stop written below the quote is caught, unless more than 8,000 characters of the email's text
come before it (optout.READ_CHARS, and mail.BODY_CHARS, where the stored text ends). A reply below a thread quoted
three rounds deep is enough. The same layouts on 0.39.1 show the original defect: no stop below a quote counts."""
from app.integrations import mail
from app.integrations.optout import opt_out

STOP = "Bitte keine E-Mails mehr."
EMBER = ("Hallo Ann,\n\nich habe Ihren Shop gesehen und eine Idee fuer Ihre Produktbeschreibungen. " * 25)[:2500]
EMBER += '\n\n-- \nEmber, Etsy shop\nReply "stop" and I won\'t write again.\n'  # at most 2,500 characters + footer


def quote(text: str) -> str:
    return "\n".join("> " + line for line in text.splitlines())


thread = quote(EMBER)
for _ in range(3):  # each round quotes the last one whole, and adds Ember's next email
    thread = quote(f"Am 13.10.26 schrieb Ann:\n{thread}\n\nDanke, ich ueberlege es mir.\n\n{EMBER}")
cases = {
    "below one quote (Thunderbird)": f"Am 14.10.26 um 10:00 schrieb Ember:\n{quote(EMBER)}\n\n{STOP}\n",
    "below one quote, attribution on two lines (Gmail)": (
        f"On Wed, Oct 14, 2026 at 10:00 AM Ember Shop <ember@example.com>\nwrote:\n\n{quote(EMBER)}\n\n{STOP}\n"
    ),
    f"below a thread three rounds deep ({len(thread):,} characters)": (
        f"Am 14.10.26 um 10:00 schrieb Ember:\n{thread}\n\n{STOP}\n"
    ),
}
html = (
    '<div class="gmail_quote"><div class="gmail_attr">On Wed, Oct 14, 2026 at 10:00 AM Ember wrote:<br></div>'
    f'<blockquote class="gmail_quote">{EMBER.replace(chr(10), "<br>")}</blockquote></div><div>{STOP}</div>'
)
cases["below an HTML blockquote (Gmail, HTML only)"] = mail.html_to_text(html)
for name, body in cases.items():
    print(f"{name:58} -> {opt_out('Re: Idee', body)!r}")
