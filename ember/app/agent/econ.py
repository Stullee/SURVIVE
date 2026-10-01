"""A venture's numbers (0.12.0): what one sale leaves after the channel's fees, the break-even, and what a month could
earn at low, likely and high sales, in euros.

A business case was prose ("economics: price, cost per sale, margin..."), so two cases couldn't be compared, and a
margin that didn't survive the fees went unnoticed. Now the agent gives the numbers it researched (``venture_case``)
and Ember's code computes the rest the same way for every venture:

* fees per sale: on Etsy (Germany), the listing fee (USD 0.20, charged again each time a listing that renews itself
  sells), 6.5% of the sale, payment processing (4% and EUR 0.30) and 19% VAT on Etsy's own fees (a seller without a
  VAT ID, like a Kleinunternehmer, pays it); on another channel, the agent's cost per sale holds its fees;
* net per sale and the sales a month that break even (the fixed costs and Ember's API spend on it);
* the net a month at the low, likely and high sales (the agent's P10, P50 and P90), and the expected net: Swanson's
  rule (30% low, 40% likely, 30% high) over a six-month horizon, whose months before the first sale earn nothing and
  still pay the fixed costs (0.14.0: they cost nothing, so a slow, losing case showed a profit);
* the expected net per API dollar and per hour of the owner's time: what ranks ventures (the decision desk).

These are estimates of the agent's estimates: the case shows which numbers they came from.
"""

from __future__ import annotations

from dataclasses import dataclass

CHANNELS = ("etsy_digital", "etsy_physical", "other")
LISTING_FEE_USD = 0.20  # Etsy: a listing, and again at each sale of one that renews itself (quantity above 1)
TRANSACTION_SHARE = 0.065  # Etsy: of each sale (the item and its shipping)
PROCESSING_SHARE = 0.04  # Etsy Payments in Germany: of each order...
PROCESSING_EUR = 0.30  # ...and a fixed part
FEE_VAT = 0.19  # VAT on Etsy's seller fees (listing, transaction) for a seller without a VAT ID
DEFAULT_USD_PER_EUR = 1.10  # when the owner set no exchange rate (etsy_usd_per_eur): an assumption, said as one
HORIZON_MONTHS = 6  # what the expected net is judged over: the months before the first sale earn nothing
MAX_FIRST_SALE_MONTHS = 24  # the critic's months to the first sale
DAYS_A_MONTH = 30.4
# 0.14.0: the agent gives the days to the first sale (whole months couldn't tell 10 days from 25), and fewer than
# MIN_FIRST_SALE_DAYS count as that many: a new listing needs its first weeks, and 0 was a loophole.
MIN_FIRST_SALE_DAYS = 14
MAX_FIRST_SALE_DAYS = 730
WEIGHTS = (0.3, 0.4, 0.3)  # Swanson's rule for the low, likely and high estimate (P10, P50, P90)


@dataclass(frozen=True)
class Case:
    """The agent's numbers for a venture (euros; sales and hours a month)."""

    channel: str
    price_eur: float
    unit_cost_eur: float
    monthly_costs_eur: float
    sales: tuple[int, int, int]  # a month: low, likely, high (P10, P50, P90)
    setup_eur: float
    owner_hours: float  # the owner's hours a month
    first_sale_days: int  # 0.14.0: days, not months
    api_usd: float  # Ember's API spend on it a month

    @property
    def presale_months(self) -> float:
        """The months of the horizon before the first sale (at least MIN_FIRST_SALE_DAYS)."""
        return min(max(self.first_sale_days, MIN_FIRST_SALE_DAYS) / DAYS_A_MONTH, HORIZON_MONTHS)


@dataclass(frozen=True)
class Economics:
    """Ember's code's numbers from a Case (euros; a month unless said otherwise)."""

    usd_per_eur: float
    fees_eur: float  # a sale
    net_eur: float  # a sale
    break_even: float | None  # sales a month; None when a sale doesn't cover its own costs
    net: tuple[float, float, float]  # at low, likely and high sales
    ev_eur: float  # expected net a month over the horizon
    ev_per_api_usd: float | None  # expected net (in USD) per API dollar; None without API spend
    ev_per_hour: float | None  # expected net per hour of the owner's; None without the owner's hours

    def text(self, case: Case) -> str:
        """The numbers in a few lines, for the agent and FOCUS."""
        low, mid, high = case.sales
        even = (
            f"break-even at {self.break_even:.1f} sales a month"
            if self.break_even is not None
            else "no break-even: a sale doesn't cover its own costs"
        )
        per_api = f"{self.ev_per_api_usd:.1f}" if self.ev_per_api_usd is not None else "-"
        per_hour = f"EUR {self.ev_per_hour:.2f}" if self.ev_per_hour is not None else "-"
        return (
            f"A sale at EUR {case.price_eur:.2f} keeps EUR {self.net_eur:.2f} (fees EUR {self.fees_eur:.2f}, cost EUR "
            f"{case.unit_cost_eur:.2f}); {even}. A month at {low}/{mid}/{high} sales nets EUR {self.net[0]:.0f} / "
            f"{self.net[1]:.0f} / {self.net[2]:.0f}; expected EUR {self.ev_eur:.0f} a month over "
            f"{HORIZON_MONTHS} months (first sale in {case.first_sale_days} days); per API dollar {per_api}, per hour "
            f"of your owner's {per_hour}."
        )


def fees(channel: str, price_eur: float, usd_per_eur: float) -> float:
    """A sale's fees in euros: Etsy's for Germany, none on another channel (the cost per sale holds them)."""
    if channel not in ("etsy_digital", "etsy_physical"):
        return 0.0
    listing = LISTING_FEE_USD / usd_per_eur
    transaction = TRANSACTION_SHARE * price_eur
    processing = PROCESSING_SHARE * price_eur + PROCESSING_EUR
    return round(listing + transaction + processing + FEE_VAT * (listing + transaction), 4)


def compute(case: Case, usd_per_eur: float | None = None) -> Economics:
    """Ember's code's numbers for ``case``: the same arithmetic for every venture."""
    rate = usd_per_eur if usd_per_eur and usd_per_eur > 0 else DEFAULT_USD_PER_EUR
    per_sale_fees = fees(case.channel, case.price_eur, rate)
    net_eur = case.price_eur - per_sale_fees - case.unit_cost_eur
    fixed = case.monthly_costs_eur + case.api_usd / rate
    break_even = fixed / net_eur if net_eur > 0 else None
    net = tuple(round(n * net_eur - fixed, 2) for n in case.sales)
    expected = sum(w * n for w, n in zip(WEIGHTS, net, strict=True))
    before = case.presale_months  # 0.14.0: they pay the fixed costs too
    ev = round(((HORIZON_MONTHS - before) * expected - before * fixed - case.setup_eur) / HORIZON_MONTHS, 2)
    return Economics(
        usd_per_eur=rate,
        fees_eur=round(per_sale_fees, 2),
        net_eur=round(net_eur, 2),
        break_even=round(break_even, 1) if break_even is not None else None,
        net=(net[0], net[1], net[2]),
        ev_eur=ev,
        ev_per_api_usd=round(ev * rate / case.api_usd, 2) if case.api_usd > 0 else None,
        ev_per_hour=round(ev / case.owner_hours, 2) if case.owner_hours > 0 else None,
    )
