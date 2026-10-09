"""Review probe: the tools each kind of cycle is offered, with every channel on (app/agent/tools.offered)."""

from __future__ import annotations

from app.agent import tools

ALL = dict(mail=True, workshop=True, etsy=True, library=True, pinterest=True, printify=True, site=True,
           brainstorm=True, blog=True, bluesky=True, kdp=True)
KINDS = {
    "venture": dict(venture=True),
    "marketing": dict(venture=False, marketing=True),
    "ordinary (marketing apart)": dict(venture=False, marketing_apart=True),
    "ordinary on a channel's product": dict(venture=False, marketing_apart=False),
    "reactive": dict(venture=False),
}
offered = {k: {n for n in tools.SPECS if tools.offered(n, **{**ALL, **v})} for k, v in KINDS.items()}
everything = set(tools.SPECS)
for kind, names in offered.items():
    print(f"{kind}: {len(names)} tools; missing: {sorted(everything - names)}")
print()
print("venture carries:", sorted(offered["venture"]))
print("marketing carries building/selling tools:", sorted(offered["marketing"] & {
    "make_document", "make_spreadsheet", "make_cost_statement", "propose_etsy_listing", "propose_printify_product",
    "propose_kdp_book", "propose_email", "workshop", "draft", "make_image", "resize_image", "propose_etsy_edit",
    "propose_reddit_post", "site_page"}))
print("draft offered in:", [k for k, v in offered.items() if "draft" in v])
