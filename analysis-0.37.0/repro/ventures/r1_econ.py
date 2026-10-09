"""Economics by hand vs econ.compute (Etsy Germany fees, break-even, P10/P50/P90, expected net)."""
import sys; sys.path.insert(0, ".")
from app.agent import econ
c = econ.Case(channel="etsy_digital", price_eur=4.90, unit_cost_eur=0.0, monthly_costs_eur=5.0, sales=(2, 10, 30),
              setup_eur=10.0, owner_hours=2.0, first_sale_days=30, api_usd=3.0)
r = econ.compute(c)  # no owner rate: 1.10 assumed
listing = 0.20 / 1.10; trans = 0.065 * 4.90; proc = 0.04 * 4.90 + 0.30; vat = 0.19 * (listing + trans)
fees = listing + trans + proc + vat; net = 4.90 - fees
fixed = 5.0 + 3.0 / 1.10
nets = [n * net - fixed for n in (2, 10, 30)]
exp = 0.3 * nets[0] + 0.4 * nets[1] + 0.3 * nets[2]
before = 30 / 30.4
ev = ((6 - before) * exp - before * fixed - 10.0) / 6
print("hand  fees %.4f net %.4f break-even %.2f nets %s ev %.2f per$ %.2f per h %.2f" % (
    fees, net, fixed / net, [round(x, 2) for x in nets], ev, ev * 1.10 / 3.0, ev / 2.0))
print("code ", r)
# a first sale under 14 days counts as 14
c2 = econ.Case(**{**c.__dict__, "first_sale_days": 1})
c14 = econ.Case(**{**c.__dict__, "first_sale_days": 14})
print("first_sale 1 vs 14 days ev:", econ.compute(c2).ev_eur, econ.compute(c14).ev_eur)
# owner's rate used when given
print("rate 1.00:", econ.compute(c, 1.00).fees_eur, " rate 0 -> assumed:", econ.compute(c, 0).usd_per_eur)
