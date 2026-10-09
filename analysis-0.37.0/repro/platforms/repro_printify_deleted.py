"""A listing Printify made of Ember's product disappears at Etsy (deleted there, or removed by Etsy), while the product
is still at Printify. Ember's own listing in the same situation becomes 'removed'; the Printify one stays 'active', and
pins and Bluesky posts linking it are still proposed and carried out."""

from __future__ import annotations

import harness  # noqa: F401  (sets up the environment first)

from app.integrations import etsy, printify_publisher
from app.integrations.etsy import RemoteListing
from tests.test_fixes_0320 import a_pin, a_post, approve, channels, poster

agent, pod_listing = poster(harness.DATA)
own_listing = 900_000_001
fake = agent.etsy.shop()
print("Ember's own listing:", own_listing, " Printify's listing:", pod_listing)


class Shop:
    """The fake shop, as a live shop would answer: first it reports Printify's listing live; then Etsy no longer has
    it (deleted at Etsy: the batch leaves it out and getListing says 404). Ember's own listing is deleted too."""

    def __init__(self, inner, gone: bool) -> None:
        self.inner, self.gone = inner, gone
        self.simulated = inner.simulated

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def listings(self, ids):
        found = [r for r in self.inner.listings(ids) if not (self.gone and r.listing_id == own_listing)]
        if not self.gone and pod_listing in ids:
            ends = "2027-02-01T00:00:00Z"
            found.append(RemoteListing(pod_listing, "active", "poster", etsy.listing_url(pod_listing), 5, 1, ends, True))
        return found

    def listing(self, listing_id):
        if self.gone and listing_id in (own_listing, pod_listing):
            return None  # HTTP 404: there is no such listing
        return self.inner.listing(listing_id)


agent.publisher.shop = lambda: Shop(fake, gone=False)
print("sync 1:", agent.publisher.sync(force=True))
print(harness.rows(agent, "SELECT listing_id, status, state, synced_at FROM printify_products"))
agent.clock.advance(hours=2)
agent.publisher.shop = lambda: Shop(fake, gone=True)
print("sync 2 (both listings gone at Etsy):", agent.publisher.sync(force=True))
print("etsy_listings     :", harness.rows(agent, "SELECT listing_id, status, state FROM etsy_listings"))
print("printify_products :", harness.rows(agent, "SELECT listing_id, status, state FROM printify_products"))
print("Printify's own sync:", agent.pod.sync(force=True))
print("printify_products after Printify's sync:", harness.rows(agent, "SELECT listing_id, status, state FROM printify_products"))
with agent.db.connection() as conn:
    print("live_listing(pod) :", dict(printify_publisher.live_listing(conn, agent.scope(), pod_listing) or {}).get("state"))
    print("pod_products_live :", printify_publisher.totals(conn, agent.scope()))
ctx = channels(agent)
post = a_post(ctx, etsy.listing_url(pod_listing))
print("propose_bluesky_post ->", post.ok, post.text[:120])
pin = a_pin(agent, ctx, pod_listing)
print("propose_pin          ->", pin.ok, pin.text[:120])
own = a_post(ctx, etsy.listing_url(own_listing), text="Plan your week of meals on one page, with a shopping list.")
print("post linking Ember's own (removed) listing ->", own.ok, own.text[:120])
requests = harness.rows(agent, "SELECT id, executor FROM approvals WHERE executor IN ('bluesky_post', 'pinterest_pin')")
for r in requests:
    approve(agent, r["id"])
agent.publisher.shop = lambda: Shop(fake, gone=True)
print("execute_approved ->", agent.execute_approved())
print(harness.rows(agent, "SELECT status, link, result FROM bluesky_posts"))
print(harness.rows(agent, "SELECT status, link, result FROM pinterest_pins"))
