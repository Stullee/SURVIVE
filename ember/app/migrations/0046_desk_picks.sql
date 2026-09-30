-- 0.13.0: the decision desk. A venture cycle's planner chose freely what to look at, so research went where the plan's
-- mood took it and the brainstorm never ran live. Now Ember's code ranks the ventures' next decisions (READY: build,
-- appraise, answer the critic, triage, brainstorm; agent/desk.py) and each venture plan takes one or says why it takes
-- none. Each pick is kept with the list it came from: what was ranked, what was taken, why none was. Never changed.
CREATE TABLE desk_picks (
    id         INTEGER PRIMARY KEY,
    cycle_id   INTEGER NOT NULL UNIQUE REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    items      TEXT NOT NULL CHECK (json_valid(items) AND json_type(items) = 'array'),  -- the READY list shown
    pick       TEXT CHECK (length(pick) BETWEEN 1 AND 40),  -- the key taken ("appraise #3"), or NULL
    venture_id INTEGER REFERENCES ventures (id),  -- the venture of the item taken
    why_not    TEXT CHECK (length(why_not) BETWEEN 1 AND 200),  -- why the plan took none
    CHECK ((pick IS NULL) <> (why_not IS NULL)),
    CHECK (pick IS NOT NULL OR venture_id IS NULL)
);
CREATE TRIGGER desk_picks_no_update BEFORE UPDATE ON desk_picks
BEGIN SELECT RAISE(ABORT, 'desk_picks: a pick never changes'); END;
CREATE TRIGGER desk_picks_no_delete BEFORE DELETE ON desk_picks
BEGIN SELECT RAISE(ABORT, 'desk_picks: rows cannot be deleted'); END;
