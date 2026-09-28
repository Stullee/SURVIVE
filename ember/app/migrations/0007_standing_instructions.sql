-- 0.5.0: the owner's standing instructions, shown in every plan and work step. Each save adds a row (an empty text
-- clears them); the current instructions of a mode and session are its newest row. Rows never change and are never
-- deleted, so the history shows what the owner told the agent, and when.

CREATE TABLE standing_instructions (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    entered_by TEXT CHECK (entered_by IS NULL OR length(entered_by) <= 60),
    text       TEXT NOT NULL CHECK (length(text) <= 1500)
);
CREATE INDEX standing_instructions_by_scope ON standing_instructions (mode, session, id);
CREATE TRIGGER standing_instructions_no_update BEFORE UPDATE ON standing_instructions
BEGIN SELECT RAISE(ABORT, 'standing_instructions: history cannot change'); END;
CREATE TRIGGER standing_instructions_no_delete BEFORE DELETE ON standing_instructions
BEGIN SELECT RAISE(ABORT, 'standing_instructions: history cannot change'); END;
