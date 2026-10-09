"""R8: a KDP description at the limit Ember's code checks, with the AI line Ember's code adds (what the card's Copy
button copies)."""
import sys
sys.path.insert(0, ".")
from app.integrations import kdp

text = "A planner for busy parents. " * 142  # 3,976 characters
checked = kdp.check_description(text[:4000])
shown = kdp.with_disclosure(checked)
print("DESCRIPTION_CHARS:", kdp.DESCRIPTION_CHARS, "| checked:", len(checked), "| with the AI line:", len(shown))
try:
    kdp.check_description(shown)
    print("check_description(with the AI line) passes")
except kdp.KdpError as exc:
    print("check_description(with the AI line):", exc)
print("printing cost, 6x9 white: 108 pages", kdp.printing_cost(108, "white", "6x9"), "| 110 pages",
      kdp.printing_cost(110, "white", "6x9"), "| 112 pages", kdp.printing_cost(112, "white", "6x9"))
print("least price at 110 pages:", kdp.least_price(110, "white", "6x9"))
print("color 40/42 pages:", kdp.printing_cost(40, "color", "6x9"), kdp.printing_cost(42, "color", "6x9"))
print("spine 100 white:", kdp.spine_width(100, "white"), "cover 6x9 100 white:", kdp.cover_size("6x9", 100, "white"))
