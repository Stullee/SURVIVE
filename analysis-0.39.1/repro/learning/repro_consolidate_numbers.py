"""LEARN: the consolidation's 'keep' (merge/rewrite) can replace lessons that state numbers with text that states none.

memory.consolidate refuses to *drop* a lesson with numbers, but a keep item covering it (even from a single line) is
accepted whatever its text says, so the numbers are gone.
"""
from app.agent import memory, tools

text = (
    "# Lessons\n\n"
    "- [#c10] Dropshipping ruled out: supplier margins under 5% after Etsy fees (case #12)\n"
    "- [#c11] Pinterest pins brought 1 click from 60 impressions in 14 days\n"
    "- [#c12] Etsy CV templates: 0 orders from 17 views; price 4.50 EUR is not the blocker\n"
    "- [#c13] Owner wants finished work, not questions\n"
)
pinned: set[str] = set()
names = set(tools.SPECS)

# 1) a plain drop of a numbered lesson is refused (the guard)
answer = {"keep": [], "drop": [{"line": 1, "why": "old"}, {"line": 2, "why": "old"}]}
print("drop numbered lines ->", memory.consolidate(text, answer, pinned, memory.CAPS["lessons"], names))

# 2) a 'merge' of lines 1-3 into a sentence without any number is accepted
answer = {"keep": [{"text": "Channels and products tried so far did not work out.", "from": [1, 2, 3]},
                   {"text": "Owner wants finished work, not questions", "from": [4]}], "drop": []}
new, summary = memory.consolidate(text, answer, pinned, memory.CAPS["lessons"], names)
print("merge  ->", summary)
print(new)

# 3) even a single-line 'keep' rewrites a numbered lesson into anything
answer = {"keep": [{"text": "Dropshipping looks promising.", "from": [1]}], "drop": []}
new, summary = memory.consolidate(text, answer, pinned, memory.CAPS["lessons"], names)
print("rewrite one ->", summary)
print(new)
