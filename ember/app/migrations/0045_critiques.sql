-- 0.13.0: an independent critic. The agent argued its own business cases, so a case's weak point reached the owner
-- only if the agent itself saw it. Now, before the next plan after a venture is proposed, a separate call on the
-- strategy model reviews the newest case: its fatal flaw, its own numbers for the same case (Ember's code computes their
-- economics like the agent's, econ.py), a verdict (back, test: only a cheaper first test, park) and what would change
-- its mind. The owner sees it with the case; ranking uses the lower of the two expected nets. A failed critique is kept
-- too (why in its note), so a case is tried at most critic.MAX_ATTEMPTS times. Never changed.
CREATE TABLE venture_critiques (
    id                INTEGER PRIMARY KEY,
    venture_id        INTEGER NOT NULL REFERENCES ventures (id),
    case_id           INTEGER NOT NULL REFERENCES venture_cases (id),
    llm_call_id       INTEGER REFERENCES llm_calls (id),
    created_at        TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    note              TEXT CHECK (length(note) <= 300),  -- why a failed one failed
    verdict           TEXT CHECK (verdict IN ('back', 'test', 'park')),
    fatal_flaw        TEXT CHECK (length(fatal_flaw) BETWEEN 1 AND 300),
    change_mind       TEXT CHECK (length(change_mind) BETWEEN 1 AND 300),
    -- the critic's numbers for the same case (its setup, hours and API spend are the agent's)
    price_eur         REAL CHECK (price_eur > 0),
    unit_cost_eur     REAL CHECK (unit_cost_eur >= 0),
    monthly_costs_eur REAL CHECK (monthly_costs_eur >= 0),
    sales_low         INTEGER CHECK (sales_low >= 0),
    sales_mid         INTEGER CHECK (sales_mid >= sales_low),
    sales_high        INTEGER CHECK (sales_high >= sales_mid),
    first_sale_months INTEGER CHECK (first_sale_months BETWEEN 0 AND 24),
    -- Ember's code's economics of them
    net_eur           REAL,
    break_even        REAL,
    ev_eur            REAL,
    CHECK (status = 'failed' OR (verdict IS NOT NULL AND fatal_flaw IS NOT NULL AND change_mind IS NOT NULL
        AND price_eur IS NOT NULL AND unit_cost_eur IS NOT NULL AND monthly_costs_eur IS NOT NULL
        AND sales_low IS NOT NULL AND sales_mid IS NOT NULL AND sales_high IS NOT NULL
        AND first_sale_months IS NOT NULL AND net_eur IS NOT NULL AND ev_eur IS NOT NULL)),
    CHECK (status = 'ok' OR (note IS NOT NULL AND verdict IS NULL))
);
CREATE INDEX venture_critiques_by_case ON venture_critiques (case_id, id);
CREATE INDEX venture_critiques_by_venture ON venture_critiques (venture_id, id);
CREATE TRIGGER venture_critiques_no_update BEFORE UPDATE ON venture_critiques
BEGIN SELECT RAISE(ABORT, 'venture_critiques: a critique never changes'); END;
CREATE TRIGGER venture_critiques_no_delete BEFORE DELETE ON venture_critiques
BEGIN SELECT RAISE(ABORT, 'venture_critiques: rows cannot be deleted'); END;
