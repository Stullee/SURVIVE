-- 0.12.0: observations, the daily record of how things do, written only by Ember's code: one value a day for each
-- subject and metric. Every sync overwrote a listing's views and favorites, so nothing showed a trend and no
-- milestone could be checked against one. The shop's counts come from Ember's own records (live listings, orders,
-- units sold); a listing's views and favorites are kept only while the owner has turned on etsy_stats_history
-- (whether Etsy's API terms allow keeping them is the owner's to confirm). Counts and amounts only, never who bought.
-- The first value of a day stays: a day's value never changes, and nothing is deleted.
CREATE TABLE observations (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    day         TEXT NOT NULL CHECK (length(day) = 10),  -- the owner's local day
    observed_at TEXT NOT NULL,
    subject     TEXT NOT NULL CHECK (subject IN ('shop', 'listing', 'venture', 'milestone')),
    subject_id  INTEGER NOT NULL,  -- a listing's Etsy number, a venture's or milestone's id; 0 for the shop
    metric      TEXT NOT NULL CHECK (length(metric) BETWEEN 1 AND 40),
    value       INTEGER NOT NULL,
    UNIQUE (mode, session, day, subject, subject_id, metric)
);
CREATE INDEX observations_by_subject ON observations (mode, session, subject, subject_id, metric, day);
CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'observations: a day''s value never changes'); END;
CREATE TRIGGER observations_no_delete BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'observations: rows are kept'); END;
