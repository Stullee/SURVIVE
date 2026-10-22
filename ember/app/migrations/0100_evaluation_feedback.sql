-- 0.44.0 (vision/evaluation.md): what the owner gave Ember from an evaluation of her work, made outside the app from
-- the evaluation export. Its cases join her own (cases, their subjects begin with "evaluation"), its corrections reach
-- her in one message of the owner's; this keeps what was given, so the same file isn't given twice and the next
-- export shows the next evaluation what she was told. Final once written.
CREATE TABLE evaluation_feedback (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    entered_by  TEXT CHECK (entered_by IS NULL OR length(entered_by) <= 60),
    evaluation  TEXT NOT NULL CHECK (length(evaluation) BETWEEN 1 AND 40),  -- its name, as the file gives it
    week_from   TEXT CHECK (week_from IS NULL OR length(week_from) = 10),
    week_to     TEXT CHECK (week_to IS NULL OR length(week_to) = 10),
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),  -- the file's content, as JSON with sorted keys
    cases       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(cases) AND json_type(cases) = 'array'),  -- their ids
    message_id  INTEGER REFERENCES messages (id),  -- the owner's message that carried it to her
    payload     TEXT NOT NULL CHECK (json_valid(payload) AND length(payload) <= 64000),
    UNIQUE (mode, session, sha256)
);
CREATE TRIGGER evaluation_feedback_final BEFORE UPDATE ON evaluation_feedback
BEGIN SELECT RAISE(ABORT, 'evaluation_feedback: what was given is final'); END;
CREATE TRIGGER evaluation_feedback_no_delete BEFORE DELETE ON evaluation_feedback
BEGIN SELECT RAISE(ABORT, 'evaluation_feedback: rows cannot be deleted'); END;
