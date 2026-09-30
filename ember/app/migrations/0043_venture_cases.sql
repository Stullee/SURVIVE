-- 0.13.0: a numeric business case. A venture's economics were prose ("price, cost per sale, margin..."), so two cases
-- couldn't be compared, and a margin that didn't survive the fees went unnoticed. Now the agent gives a venture's
-- numbers (venture_case: price, cost per sale, fixed costs, sales a month as a low, likely and high estimate, setup
-- cash, the owner's hours, the months to the first sale, Ember's API spend on it) and Ember's code computes the rest
-- (agent/econ.py): the fees (Etsy's for Germany), net per sale, break-even, the net a month at each estimate, the
-- expected net and what it earns per API dollar and per hour of the owner's. Each case is kept (the newest counts), and
-- a venture is proposed only with one.
CREATE TABLE venture_cases (
    id                INTEGER PRIMARY KEY,
    venture_id        INTEGER NOT NULL REFERENCES ventures (id),
    cycle_id          INTEGER REFERENCES cycles (id),
    created_at        TEXT NOT NULL,
    channel           TEXT NOT NULL CHECK (channel IN ('etsy_digital', 'etsy_physical', 'other')),
    price_eur         REAL NOT NULL CHECK (price_eur > 0),
    unit_cost_eur     REAL NOT NULL CHECK (unit_cost_eur >= 0),
    monthly_costs_eur REAL NOT NULL CHECK (monthly_costs_eur >= 0),
    sales_low         INTEGER NOT NULL CHECK (sales_low >= 0),
    sales_mid         INTEGER NOT NULL CHECK (sales_mid >= sales_low),
    sales_high        INTEGER NOT NULL CHECK (sales_high >= sales_mid),
    setup_eur         REAL NOT NULL CHECK (setup_eur >= 0),
    owner_hours       REAL NOT NULL CHECK (owner_hours >= 0),  -- a month
    first_sale_months INTEGER NOT NULL CHECK (first_sale_months BETWEEN 0 AND 24),
    api_usd           REAL NOT NULL CHECK (api_usd >= 0),  -- a month
    -- Ember's code's numbers (econ.py), as computed when the case was saved
    usd_per_eur       REAL NOT NULL CHECK (usd_per_eur > 0),
    fees_eur          REAL NOT NULL,  -- a sale
    net_eur           REAL NOT NULL,  -- a sale
    break_even        REAL,  -- sales a month (NULL: a sale doesn't cover its own costs)
    net_low           REAL NOT NULL,  -- a month, at each estimate
    net_mid           REAL NOT NULL,
    net_high          REAL NOT NULL,
    ev_eur            REAL NOT NULL,  -- the expected net a month
    ev_per_api_usd    REAL,
    ev_per_hour       REAL
);
CREATE INDEX venture_cases_by_venture ON venture_cases (venture_id, id);
CREATE TRIGGER venture_cases_no_update BEFORE UPDATE ON venture_cases
BEGIN SELECT RAISE(ABORT, 'venture_cases: a case never changes; a new one replaces it'); END;
CREATE TRIGGER venture_cases_no_delete BEFORE DELETE ON venture_cases
BEGIN SELECT RAISE(ABORT, 'venture_cases: rows cannot be deleted'); END;

-- A business case (stage proposed) needs its numbers.
CREATE TRIGGER ventures_proposed_numbers BEFORE UPDATE OF stage ON ventures
WHEN NEW.stage = 'proposed' AND OLD.stage IS NOT 'proposed'
    AND NOT EXISTS (SELECT 1 FROM venture_cases WHERE venture_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'ventures: a business case needs its numbers (venture_case)'); END;
