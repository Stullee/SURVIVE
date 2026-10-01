-- 0.15.0: venture gates that can't be gamed, and an expected net that counts the months before the first sale.
--
-- A business case gives the days to its first sale (whole months couldn't tell 10 days from 25, and 0 was a
-- loophole): cases saved before keep their months, in days too. The expected net now counts the fixed costs of the
-- months before the first sale (they cost nothing, so a slow, losing case showed a profit): the stored cases' and
-- critiques' expected nets are worked out again the way agent/econ.py does (at least 14 days to the first sale, over
-- six months), from the nets they kept. The no-update triggers go only for this and come back unchanged.
ALTER TABLE venture_cases ADD COLUMN first_sale_days INTEGER CHECK (first_sale_days BETWEEN 0 AND 730);
DROP TRIGGER venture_cases_no_update;
UPDATE venture_cases SET first_sale_days = CAST(ROUND(first_sale_months * 30.4) AS INTEGER);
UPDATE venture_cases SET ev_eur = ROUND(
    ((6 - MIN(MAX(first_sale_days, 14) / 30.4, 6)) * (0.3 * net_low + 0.4 * net_mid + 0.3 * net_high)
        - MIN(MAX(first_sale_days, 14) / 30.4, 6) * (monthly_costs_eur + api_usd / usd_per_eur) - setup_eur) / 6, 2);
UPDATE venture_cases SET
    ev_per_api_usd = CASE WHEN api_usd > 0 THEN ROUND(ev_eur * usd_per_eur / api_usd, 2) END,
    ev_per_hour = CASE WHEN owner_hours > 0 THEN ROUND(ev_eur / owner_hours, 2) END;
CREATE TRIGGER venture_cases_no_update BEFORE UPDATE ON venture_cases
BEGIN SELECT RAISE(ABORT, 'venture_cases: a case never changes; a new one replaces it'); END;
CREATE TRIGGER venture_cases_days BEFORE INSERT ON venture_cases
WHEN NEW.first_sale_days IS NULL
BEGIN SELECT RAISE(ABORT, 'venture_cases: a case gives the days to its first sale'); END;

DROP TRIGGER venture_critiques_no_update;
UPDATE venture_critiques SET ev_eur = (
    SELECT ROUND(
        ((6 - MIN(MAX(ROUND(venture_critiques.first_sale_months * 30.4), 14) / 30.4, 6))
            * (0.3 * ROUND(venture_critiques.sales_low * venture_critiques.net_eur - f.fixed, 2)
                + 0.4 * ROUND(venture_critiques.sales_mid * venture_critiques.net_eur - f.fixed, 2)
                + 0.3 * ROUND(venture_critiques.sales_high * venture_critiques.net_eur - f.fixed, 2))
            - MIN(MAX(ROUND(venture_critiques.first_sale_months * 30.4), 14) / 30.4, 6) * f.fixed - f.setup_eur) / 6,
        2)
    FROM (
        SELECT venture_critiques.monthly_costs_eur + c.api_usd / c.usd_per_eur AS fixed, c.setup_eur
        FROM venture_cases c WHERE c.id = venture_critiques.case_id
    ) AS f
) WHERE status = 'ok';
CREATE TRIGGER venture_critiques_no_update BEFORE UPDATE ON venture_critiques
BEGIN SELECT RAISE(ABORT, 'venture_critiques: a critique never changes'); END;

-- A venture proposed before the gates (live, dropshipping #3: no numbers, no knock-outs, no critique, no counted
-- research) goes back to researching, with a note of Ember's code. The owner's research wish stays.
UPDATE ventures SET
    stage = 'researching',
    notes = substr(ltrim(notes || char(10) || 'Back to researching by Ember''s code: it was proposed before a '
        || 'business case needed its numbers (venture_case) and passed the knock-outs.', char(10)), -2000),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE stage = 'proposed' AND NOT EXISTS (SELECT 1 FROM venture_cases c WHERE c.venture_id = ventures.id);

-- The agent can't start a venture live ("Ember earns somewhere" let any new one skip the owner's backing): a venture
-- goes live once the owner backed it and its first test is met. Only the seeded Etsy leg, planted outside any cycle,
-- starts live.
CREATE TRIGGER ventures_live_backed BEFORE INSERT ON ventures
WHEN NEW.stage = 'live' AND NEW.created_cycle_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'ventures: only the owner''s backing makes a venture live'); END;
