-- 0.13.0: a prediction ledger. The agent's estimates were never checked against what happened, so neither it nor the
-- owner knew whether its odds and its business cases could be trusted. Now two kinds of prediction are kept, and
-- Ember's code settles each against its records (the Phase A metrics), with no model call:
--   milestone:  a metric milestone the agent gave a likelihood (milestone_plan's likely): met by its first date?
--   first_sale: a backed venture's business case (its months to the first sale, as a 50% call): on time?
-- Hit, miss or void (what it was about went away first), with the evidence. Their record is the calibration line
-- that triage (the decision desk's READY), the critic and the daily review read. A claim never changes, a settled
-- prediction is final, and none is deleted.
CREATE TABLE predictions (
    id           INTEGER PRIMARY KEY,
    mode         TEXT NOT NULL,
    session      INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('milestone', 'first_sale')),
    milestone_id INTEGER UNIQUE REFERENCES milestones (id),
    venture_id   INTEGER REFERENCES ventures (id),
    case_id      INTEGER UNIQUE REFERENCES venture_cases (id),
    claim        TEXT NOT NULL CHECK (length(claim) BETWEEN 1 AND 300),
    probability  REAL NOT NULL CHECK (probability >= 0.05 AND probability <= 0.95),
    due          TEXT NOT NULL,  -- the owner's local date by which it comes true
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'hit', 'miss', 'void')),
    settled_at   TEXT,
    result       TEXT CHECK (length(result) <= 300),
    CHECK ((kind = 'milestone') = (milestone_id IS NOT NULL)),
    CHECK (kind <> 'first_sale' OR (case_id IS NOT NULL AND venture_id IS NOT NULL)),
    CHECK ((status = 'open') = (settled_at IS NULL))
);
CREATE INDEX predictions_by_status ON predictions (mode, session, status, id);
CREATE TRIGGER predictions_claim_fixed BEFORE UPDATE OF
    mode, session, kind, milestone_id, venture_id, case_id, claim, probability, due, created_at ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions: a prediction never changes'); END;
CREATE TRIGGER predictions_settled_once BEFORE UPDATE ON predictions
WHEN OLD.status <> 'open'
BEGIN SELECT RAISE(ABORT, 'predictions: a settled prediction is final'); END;
CREATE TRIGGER predictions_no_delete BEFORE DELETE ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions: rows cannot be deleted'); END;
