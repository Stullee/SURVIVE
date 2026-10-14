"""MAIL-4: the CSS-comment pattern behind the hidden-text filter takes time growing with the square of an unclosed
comment run: twice the text, four times the time. mail.py applies its 100,000-character cap after the pattern, so a
<style> of up to 1 MB in one email holds the reading thread for minutes (about 24 on a fast x86, more on a Pi)."""
import time

from app.integrations import mail

for n in (5_000, 10_000, 20_000, 40_000):
    css = "/* " * (n // 3)
    start = time.perf_counter()
    mail._CSS_COMMENT.sub("", css)
    print(f"{len(css):>7,} characters of unclosed comments: {time.perf_counter() - start:6.2f} s")
