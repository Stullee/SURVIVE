-- 0.48.0 (vision/generations.md, "The inheritance"): what passes from one generation of Ember to the next besides the
-- world and the estate. An evaluation drafts it as an inheritance.json from the whole-life export; Ember's code checks
-- it, the owner reads its preview on the System tab, strikes what shouldn't pass and gives it; it waits there until
-- the change writes it (agent/inheritance.py): the principles as inherited hypotheses of the new mind's playbook, the
-- craft files, the seed strategy and identity. One given inheritance at a time for the mind that hands on; a newer one
-- replaces it. Final once passed.
CREATE TABLE inheritances (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,  -- the world's
    from_session  INTEGER NOT NULL,  -- the mind that hands it on
    to_session    INTEGER,  -- the mind it passed to (the change sets it)
    created_at    TEXT NOT NULL,
    entered_by    TEXT CHECK (entered_by IS NULL OR length(entered_by) <= 60),
    evaluation    TEXT NOT NULL CHECK (length(evaluation) BETWEEN 1 AND 40),  -- its name, as the file gives it
    sha256        TEXT NOT NULL CHECK (length(sha256) = 64),  -- the file's content, as JSON with sorted keys
    payload       TEXT NOT NULL CHECK (json_valid(payload) AND length(payload) <= 64000),  -- what passes: strikes applied
    struck        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(struck) AND json_type(struck) = 'object'),
    status        TEXT NOT NULL DEFAULT 'given' CHECK (status IN ('given', 'replaced', 'passed')),
    replaced_at   TEXT,
    passed_at     TEXT,
    written       TEXT CHECK (written IS NULL OR json_valid(written)),  -- what the change wrote: principles, files
    CHECK (status <> 'passed' OR (to_session IS NOT NULL AND passed_at IS NOT NULL)),
    CHECK (status <> 'replaced' OR replaced_at IS NOT NULL)
);
CREATE UNIQUE INDEX inheritance_given ON inheritances (mode, from_session) WHERE status = 'given';
CREATE TRIGGER inheritance_content_fixed BEFORE UPDATE OF mode, session, from_session, created_at, entered_by,
    evaluation, sha256, payload, struck ON inheritances
BEGIN SELECT RAISE(ABORT, 'inheritances: what was given is final'); END;
CREATE TRIGGER inheritance_passed_final BEFORE UPDATE ON inheritances WHEN OLD.status <> 'given'
BEGIN SELECT RAISE(ABORT, 'inheritances: a replaced or passed inheritance is final'); END;
CREATE TRIGGER inheritance_no_delete BEFORE DELETE ON inheritances
BEGIN SELECT RAISE(ABORT, 'inheritances: rows cannot be deleted'); END;

-- Her letter to the next generation (vision/generations.md): the owner asks for it on the System tab, and one call of
-- her own writes it, as the last will is written (agent/loop.py, _letter). The newest of the mind that hands on passes
-- as inheritance/letter.md unless the owner struck it. Her view, not evidence.
CREATE TABLE letters (
    id           INTEGER PRIMARY KEY,
    mode         TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session      INTEGER NOT NULL,  -- the mind's
    life_id      INTEGER NOT NULL REFERENCES lives (id),
    cycle_id     INTEGER REFERENCES cycles (id),
    llm_call_id  INTEGER REFERENCES llm_calls (id),
    created_at   TEXT NOT NULL,
    text         TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 6000),
    cut_off      INTEGER NOT NULL DEFAULT 0 CHECK (cut_off IN (0, 1))
);
CREATE INDEX letters_by_mind ON letters (mode, session, id);
CREATE TRIGGER letters_no_update BEFORE UPDATE ON letters
BEGIN SELECT RAISE(ABORT, 'letters: history cannot change'); END;
CREATE TRIGGER letters_no_delete BEFORE DELETE ON letters
BEGIN SELECT RAISE(ABORT, 'letters: history cannot change'); END;

-- An inherited principle: the generation it came from and its evidence there (the cases for and against it, its
-- principle), as JSON {"generation", "principle", "for", "against", "inheritance"}. Its predecessor's cases count as
-- one case of the new mind's (agent/learning.py, confidence): two of her own establish it, one against disputes it,
-- and INHERITED_FADE_DAYS without a case of hers retire it.
ALTER TABLE principles ADD COLUMN inherited TEXT CHECK (inherited IS NULL OR json_valid(inherited));
