"""Does a mailto link with a query (cc/bcc to someone else) pass the blog's checks ("links can only be https and your
own email address")? Uses the real read_links / read_post / render / audit."""
from app.products import blog

owner = blog.Owner(name="Ember", legal_name="Max Muster", town="Berlin", email="hallo@ember-ai.de", url="https://ember-ai.de")

# 1. link page: literal second address in the query
bio, links = blog.read_links(
    "Druckvorlagen von Ember, einem KI-Agenten.",
    [{"label": "Schreib uns", "url": "mailto:hallo@ember-ai.de?bcc=collector@evil.example&subject=Hallo"},
     {"label": "Shop", "url": "https://www.etsy.com/shop/x"}],
    owner,
)
page = blog.render_links(bio, links, owner)
print("link page accepted; audit problems:", blog.audit(page, owner))
print([l for l in page.decode().splitlines() if "mailto" in l][0][:160])

# 2. blog post: percent-encoded address slips past the text scan for e-mail addresses
body = ("Ein Text über Bewerbungen, lang genug für die Prüfung. " * 8 +
        "\n\nFragen? [Schreib mir](mailto:hallo@ember-ai.de?cc=collector%40evil.example)\n")
source = ("---\nslug: test-post\ntitle: Test\ndescription: Eine Beschreibung.\nlead: Ein Lead.\n---\n" + body)
post = blog.read_post(source, owner)
html = blog.render_post(post, "2026-10-14", owner)
print("post accepted; audit problems:", blog.audit(html, owner))
print([l for l in html.decode().split("<p>") if "mailto" in l][0][:160])

# control: a plain other address is refused
try:
    blog.read_links("Bio.", [{"label": "x", "url": "mailto:collector@evil.example"}], owner)
except blog.BlogError as exc:
    print("control refused:", exc)
