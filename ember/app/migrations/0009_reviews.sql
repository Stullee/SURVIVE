-- 0.7.1: the daily business review. Once a day, before the first plan, the agent judges its own numbers (the
-- scorecard Ember's code builds from its records) and decides for each project: continue, change or stop. Each
-- review is one row, kept as it was: the next review holds the agent to its verdicts.
CREATE TABLE reviews (
    id             INTEGER PRIMARY KEY,
    mode           TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session        INTEGER NOT NULL,
    life_id        INTEGER NOT NULL REFERENCES lives (id),
    cycle_id       INTEGER NOT NULL REFERENCES cycles (id),
    created_at     TEXT NOT NULL,
    day            TEXT NOT NULL CHECK (length(day) = 10),  -- the local date it reviews up to, YYYY-MM-DD
    status         TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    scorecard      TEXT NOT NULL CHECK (length(scorecard) <= 12000),
    verdicts       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(verdicts)),
    working        TEXT NOT NULL DEFAULT '' CHECK (length(working) <= 600),
    not_working    TEXT NOT NULL DEFAULT '' CHECK (length(not_working) <= 600),
    owner_feedback TEXT NOT NULL DEFAULT '' CHECK (length(owner_feedback) <= 600),
    lesson         TEXT NOT NULL DEFAULT '' CHECK (length(lesson) <= 400),
    focus          TEXT NOT NULL DEFAULT '' CHECK (length(focus) <= 400),
    note           TEXT CHECK (note IS NULL OR length(note) <= 300)  -- why a review failed
);
CREATE INDEX reviews_by_scope ON reviews (mode, session, day);
CREATE TRIGGER reviews_no_update BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews: history cannot change'); END;
CREATE TRIGGER reviews_no_delete BEFORE DELETE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews: history cannot change'); END;
