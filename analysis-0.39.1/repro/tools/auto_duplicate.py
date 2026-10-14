"""TOOLS: the same listing proposed again in a later cycle under an automatic unlock becomes a second
approved request, and Ember's code creates a second listing: the duplicate check looks at *pending* requests only, and an
automatic unlock approves the first at once.

policy.match is made to say "listing_variant", as the test suite's `fits` does: in real use the owner's unlock needs a
backed venture with 5 approved, unchanged listings (policy.match); everything after the match is the real code.
"""

import io
import json

import harness  # noqa: F401 - environment first
from harness import _data, call, rows
from PIL import Image, ImageDraw

from app.agent import policy
from tests.test_etsy import listed, shop_context
from tests.test_fixes_0140_unlock_safety import unlock
from tests.test_policy import a_milestone

(_data / "dup").mkdir()
agent, first_listing = listed(_data / "dup")
goal = a_milestone(agent)
unlock(agent, goal, "listing_variant")  # the owner's automatic unlock for new listings of this line
policy.match = lambda conn, scope, row, read=None: "listing_variant"

first = rows(agent, "SELECT action, project_id FROM approvals WHERE executor = 'etsy_listing'")[0]
action = json.loads(first["action"])
photos = []
for n in range(5):  # five photos that look different, as QA wants
    picture = Image.new("RGB", (2000, 1500), (40 * n, 200 - 30 * n, 90))
    ImageDraw.Draw(picture).rectangle([100 * n, 100, 100 * n + 700, 900], fill=(250, 250 - 40 * n, 20 * n))
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    path = f"shop/variant-photo-{n}.png"
    agent.roots()[0].write_bytes(path, buffer.getvalue())
    photos.append(path)
args = {
    "title": "Weekly planner A5 printable, undated, minimalist",
    "description": action["description"],
    "price": "4.90",
    "tags": ", ".join(action["tags"]),
    "category_id": action["taxonomy_id"],
    "files": ", ".join(f["path"] for f in action["files"]),
    "photos": ", ".join(photos),
    "reason": "An A5 variant of the planner that sells.",
    "project_id": first["project_id"],
}
for n in (1, 2):  # propose_etsy_listing runs once a cycle: the same proposal in two cycles (a fresh cycle state each)
    out = call(shop_context(agent), "propose_etsy_listing", args)
    print(f"cycle {n}: ok={out.ok}:", out.text[:160].replace("\n", " "))
    if n == 1:
        print("  carried out:", agent.execute_approved())
print(rows(agent, "SELECT id, status, decided_by FROM approvals WHERE executor = 'etsy_listing' ORDER BY id"))
print("carried out:", agent.execute_approved())
print(rows(agent, "SELECT approval_id, status, listing_id FROM etsy_listings ORDER BY id"))
