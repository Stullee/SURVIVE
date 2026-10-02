-- 0.18.0: marketing before parking (agent/gates.py, agent/reach.py). A product line that misses its day-14 views bar
-- with less reach than reach.ENOUGH (blog posts, pins, listing edits for its listings) owes a push to bring buyers
-- instead of a park, and gets one more views bar two weeks later: 'retry_views'. The table is rebuilt (SQLite can't
-- change a CHECK) with its rows and their ids; its trigger stays as it was.
CREATE TABLE listing_gates_new (
    id           INTEGER PRIMARY KEY,
    mode         TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session      INTEGER NOT NULL,
    project_id   INTEGER NOT NULL REFERENCES projects (id),
    gate         TEXT NOT NULL CHECK (
        gate IN ('day7_views', 'day14_views', 'retry_views', 'day14_favorites', 'day21_sale', 'scale')
    ),
    milestone_id INTEGER NOT NULL UNIQUE REFERENCES milestones (id),
    started_on   TEXT NOT NULL CHECK (started_on GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
    created_at   TEXT NOT NULL,
    UNIQUE (mode, session, project_id, gate)
);
INSERT INTO listing_gates_new (id, mode, session, project_id, gate, milestone_id, started_on, created_at)
SELECT id, mode, session, project_id, gate, milestone_id, started_on, created_at FROM listing_gates;
DROP TABLE listing_gates;
ALTER TABLE listing_gates_new RENAME TO listing_gates;
CREATE TRIGGER listing_gates_no_delete BEFORE DELETE ON listing_gates
BEGIN SELECT RAISE(ABORT, 'listing_gates: rows cannot be deleted'); END;
