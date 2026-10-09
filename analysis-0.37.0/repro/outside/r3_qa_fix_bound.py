"""R3: what the qa_fix rule (an unlock's 'QA fixes: up to 5 distinct photos') carries.

DOCS (Your part, Autonomy): "QA fixes: up to 5 distinct photos (no copies) on a live listing". A photo change replaces
every photo of the listing (etsy.Edit's docstring). Which photo changes does policy.match call a qa_fix?
"""

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
data = Path(os.environ["EMBER_DATA_DIR"])

from app.agent import policy  # noqa: E402
from app.integrations import etsy, etsy_publisher, qa  # noqa: E402
from tests.test_etsy import listed  # noqa: E402

agent, listing_id = listed(data)
with agent.db.connection() as conn:
    current = etsy_publisher.current_listing(conn, agent.scope(), listing_id)
print("live listing photos now:", [p.path for p in current.photos], "distinct:", qa.distinct(current.photos))


def photo(n: int) -> etsy.Upload:
    return etsy.Upload(f"photos/new-{n}.png", hashlib.sha256(f"new photo {n}".encode()).hexdigest(), 1000 + n)


for count in (5, 7, 10):
    edit = etsy.Edit(listing_id=listing_id, currency=current.currency, photos=tuple(photo(n) for n in range(count)))
    row = {"executor": "etsy_edit", "action": json.dumps(edit.to_action()), "venture_id": None, "type": "sell"}
    with agent.db.connection() as conn:
        rule = policy.match(conn, agent.scope(), row)
        falls = policy.short(row)
    kept = {p.sha256 for p in current.photos} & {p.sha256 for p in edit.photos}
    print(f"{count} new photos, keeping {len(kept)} of the listing's: rule={rule!r}, QA shortfalls={falls}")
