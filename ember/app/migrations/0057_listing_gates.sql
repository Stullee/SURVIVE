-- 0.13.0: a product line's listing test (agent/gates.py): the bars of the analysis' section 8 (10 views by day 7,
-- 30 views and 2 favorites by day 14, a first order by day 21), as milestones Ember's code sets for a project once
-- its first listing is live, and checks from Etsy's numbers. This records which milestone is which bar (milestones of
-- the kind 'first_test': a product line's first test); 'scale' is the decision point a first order by day 21 sets
-- (kind 'decision').
CREATE TABLE listing_gates (
    id           INTEGER PRIMARY KEY,
    mode         TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session      INTEGER NOT NULL,
    project_id   INTEGER NOT NULL REFERENCES projects (id),
    gate         TEXT NOT NULL CHECK (gate IN ('day7_views', 'day14_views', 'day14_favorites', 'day21_sale', 'scale')),
    milestone_id INTEGER NOT NULL UNIQUE REFERENCES milestones (id),
    started_on   TEXT NOT NULL CHECK (started_on GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    created_at   TEXT NOT NULL,
    UNIQUE (mode, session, project_id, gate)
);
CREATE TRIGGER listing_gates_no_delete BEFORE DELETE ON listing_gates
BEGIN SELECT RAISE(ABORT, 'listing_gates: rows cannot be deleted'); END;
