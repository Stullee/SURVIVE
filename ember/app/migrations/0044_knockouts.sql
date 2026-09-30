-- 0.13.0: knock-outs in code. What rules a venture out was the agent's judgement, and a dropshipping case resting on
-- vendors' pages reached the owner. Now Ember's code checks each business case before it is proposed: cold outreach,
-- accounts Ember itself would create, more cash to start than the owner's venture budget, a first sale later than half
-- the net runway, a sale that loses money, and demand without an independent source. A knocked-out venture isn't
-- proposed until the case changes or the owner overrides that knock-out for it (reversibly): each override and each
-- restore is kept here. A case says what it needs (cold outreach, Ember's own accounts) in its new ``needs`` column.
ALTER TABLE venture_cases ADD COLUMN needs TEXT NOT NULL DEFAULT '';

CREATE TABLE knockout_overrides (
    id         INTEGER PRIMARY KEY,
    venture_id INTEGER NOT NULL REFERENCES ventures (id),
    rule       TEXT NOT NULL CHECK (rule IN ('cold_outreach', 'ember_accounts', 'cash', 'slow', 'losing', 'vendor_only')),
    overridden INTEGER NOT NULL CHECK (overridden IN (0, 1)),  -- 1: the owner lifted it; 0: restored it
    by         TEXT,
    comment    TEXT CHECK (comment IS NULL OR length(comment) <= 1000),
    created_at TEXT NOT NULL
);
CREATE INDEX knockout_overrides_by_venture ON knockout_overrides (venture_id, rule, id);
CREATE TRIGGER knockout_overrides_no_update BEFORE UPDATE ON knockout_overrides
BEGIN SELECT RAISE(ABORT, 'knockout_overrides: history cannot change'); END;
CREATE TRIGGER knockout_overrides_no_delete BEFORE DELETE ON knockout_overrides
BEGIN SELECT RAISE(ABORT, 'knockout_overrides: history cannot change'); END;
