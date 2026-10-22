-- 0.43.0 (vision/evaluation.md): what each cycle's plan saw, its planner context, so an evaluation can audit a cycle
-- from what the agent had in front of it. The diagnostics report shows the next plan's context only, and no past one
-- was kept: the owner had to copy a report just before a cycle to see what its plan saw. Kept compressed (zlib: about
-- 70,000 characters a plan) and pruned with the other texts after db.TEXT_DAYS: its length stays, the text goes.
CREATE TABLE plan_contexts (
    cycle_id   INTEGER PRIMARY KEY REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('ordinary', 'marketing', 'venture', 'reactive')),  -- the cycle's kind
    chars      INTEGER NOT NULL CHECK (chars >= 0),  -- the context's length, kept after it is pruned
    context    BLOB CHECK (context IS NULL OR length(context) <= 1000000)  -- zlib of its UTF-8; NULL once pruned
);
CREATE INDEX plan_contexts_by_time ON plan_contexts (created_at);
CREATE TRIGGER plan_contexts_no_update BEFORE UPDATE ON plan_contexts
WHEN NOT (NEW.cycle_id IS OLD.cycle_id AND NEW.created_at IS OLD.created_at AND NEW.kind IS OLD.kind
    AND NEW.chars IS OLD.chars AND NEW.context IS NULL)
BEGIN SELECT RAISE(ABORT, 'plan_contexts: history cannot change'); END;
CREATE TRIGGER plan_contexts_no_delete BEFORE DELETE ON plan_contexts
BEGIN SELECT RAISE(ABORT, 'plan_contexts: rows cannot be deleted'); END;
