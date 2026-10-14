"""MAIL-1, independently: the same stop, written above the quote (top) and below it (bottom), as German mail clients
reply. integrations/optout.py's opt_out is what mailstore.py:117 calls on every stored mail."""
from app.integrations import optout

QUOTE = "Am 14.10.2026 um 10:12 schrieb Ember <ember@example.org>:\n> Hallo Ann,\n> darf ich Ihnen unseren Planer zeigen?\n> Ember\n"
for label, body in (
    ("top-posted", "Bitte keine E-Mails mehr.\n\n" + QUOTE),
    ("bottom-posted", QUOTE + "\nBitte keine E-Mails mehr.\n"),
    ("bottom, English", "On Tue, Oct 14, 2026 at 10:12 Ember <ember@example.org> wrote:\n> Hi Ann\n\nPlease stop emailing me.\n"),
):
    print(f"{label:16s} -> opt_out: {optout.opt_out('Re: Ihr Planer', body)!r}")
