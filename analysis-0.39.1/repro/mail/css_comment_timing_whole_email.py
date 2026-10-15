"""4.12 through the whole parser: HTML-only emails whose <style> holds a run of unclosed comments, read by
mail.parse_message as the mailbox reads them. On 0.39.1 (it still has mail._CSS_COMMENT) only the small sizes run: the
time grows with the square of the run, and 1 MB took 24 minutes. From 0.39.5 the large ones run too."""
import time
from email.message import EmailMessage

from app.integrations import mail

sizes = [10_000, 20_000, 40_000]
if not hasattr(mail, "_CSS_COMMENT"):  # the pattern 0.39.5 replaced with a linear scan
    sizes += [250_000, 990_000]
for n in sizes:
    msg = EmailMessage()
    msg["From"] = "x@example.org"
    msg["To"] = "ember@mail.example"
    msg["Subject"] = "Hi"
    msg.set_content("<html><head><style>" + "/* " * (n // 3) + "</style></head><body><p>Hello</p></body></html>",
                    subtype="html")
    raw = msg.as_bytes()
    start = time.perf_counter()
    parsed = mail.parse_message(raw, 1)
    print(f"{len(raw):>9,} bytes of HTML email: {time.perf_counter() - start:7.2f} s, text {parsed.body[:40]!r}")
