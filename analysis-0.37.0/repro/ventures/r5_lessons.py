"""memory.consolidate: (a) a lesson with numbers can lose its number through a merge; (b) lines of the file that aren't
'- ' bullets after the first bullet vanish in any consolidation (and an upgrade's re-check), numbers or not."""
import sys; sys.path.insert(0, ".")
from app.agent import memory as m
text = "# Lessons\n\n" + "".join(f"- [#c{i}] lesson number {chr(97+i)} about the shop\n" for i in range(1, 12))
text += "- [#c20] dropshipping ruled out: margins under 5%\n- [#c21] dropshipping doesn't work for us\n"
lines = m.lesson_lines(text)[1]
print("lessons:", len(lines), "consolidation due:", len(lines) >= m.CONSOLIDATE_FROM)
answer = {"keep": [{"text": "Dropshipping doesn't work for us", "from": [12, 13]}], "drop": []}
new, said = m.consolidate(text, answer, set(), m.CAPS["lessons"], ())
print(said)
print("number kept?", "5%" in new)
print("---- (b) a file the agent wrote with memory_update replace (sections and a plain line)")
text2 = ("# Lessons\n\n- [#c1] first\n## Pricing\nPrices at 4.90 EUR sold 3 of 40 views; 9.90 EUR sold none.\n"
         + "".join(f"- [#c{i}] lesson {i} about the shop\n" for i in range(2, 14)))
answer2 = {"keep": [{"text": "lesson about the shop (merged)", "from": [2, 3]}], "drop": []}
new2, said2 = m.consolidate(text2, answer2, set(), m.CAPS["lessons"], ())
print(said2)
print("plain line with numbers kept?", "4.90 EUR" in new2, "| heading kept?", "## Pricing" in new2)
print("consolidation_input shows the plain line?", "4.90" in m.consolidation_input(text2, set()))
