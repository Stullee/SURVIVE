-- 0.12.0: a digest of every wake cycle, built by Ember's code when the cycle ends (failed, cut-off and unreflected
-- ones too): what it was aimed at, its goal, what its tools did and what they didn't (refused, failed, skipped or cut
-- off), how its work ended, whether it reflected, and what it cost. The journal was the model's last reply, so it
-- could be lost or claim work that never happened (live: cycle #33 recorded two skipped file writes as written).
-- The next plans see the last two digests, the work brief the newest one of its focus milestone and venture, and the
-- reflection what its cycle did not do. A digest is written once and never changes (a finished cycle can't be
-- updated, so it has a table of its own).
CREATE TABLE cycle_digests (
    cycle_id   INTEGER PRIMARY KEY REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    text       TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    undone     INTEGER NOT NULL DEFAULT 0 CHECK (undone >= 0)  -- its tool calls that were not done
);
CREATE TRIGGER cycle_digests_no_update BEFORE UPDATE ON cycle_digests
BEGIN SELECT RAISE(ABORT, 'cycle_digests: a digest never changes'); END;
CREATE TRIGGER cycle_digests_no_delete BEFORE DELETE ON cycle_digests
BEGIN SELECT RAISE(ABORT, 'cycle_digests: a digest never changes'); END;
