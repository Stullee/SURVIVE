"""evidence.grade: tracking parameters, www., trailing slash, vendors on any domain, affiliates."""
import h
from app.agent import evidence
agent = h.fresh("r9")
s = agent.scope()
returned = ["https://www.example-forum.de/thread/123/?utm_source=bing&page=2", "https://www.shopify.de/blog/etsy-umsatz",
            "https://blog.example.org/best-tools?ref=abc123", "https://www.reddit.com/r/EtsySellers/comments/xyz/",
            "https://help.printify.com/hc/en-us/articles/1"]
with agent.db.transaction() as conn:
    evidence.record_sources(conn, s, None, None, returned, h.now(agent))
tests = ["http://example-forum.de/thread/123?page=2", "https://example-forum.de/thread/123/?page=2&fbclid=Z",
         "https://example-forum.de/thread/123?page=3", "https://shopify.de/blog/etsy-umsatz",
         "https://blog.example.org/best-tools?ref=abc123", "https://blog.example.org/best-tools",
         "https://reddit.com/r/EtsySellers/comments/xyz", "https://help.printify.com/hc/en-us/articles/1",
         "https://www.reddit.com/r/EtsySellers/comments/xyz/#top"]
with agent.db.connection() as conn:
    for u in tests:
        print("%-12s %s" % (evidence.grade(conn, s, u), u))
