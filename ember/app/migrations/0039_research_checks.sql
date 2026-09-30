-- 0.12.0: a research model is checked before it takes over. Research could only run on the worker model; a cheaper
-- one (Claude Haiku 4.5, say) may find less. Once the owner names a research model, the agent's next 10 research
-- questions also go to it (the agent reads the worker model's answer): Ember's code compares what each found, and the
-- research model takes over only if it found as much, less one. One row per compared question, written once.
CREATE TABLE research_checks (
    id                INTEGER PRIMARY KEY,
    mode              TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session           INTEGER NOT NULL,
    model             TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 100),  -- the research model checked
    created_at        TEXT NOT NULL,
    cycle_id          INTEGER REFERENCES cycles (id),
    question          TEXT NOT NULL CHECK (length(question) BETWEEN 1 AND 500),
    worker_call_id    INTEGER REFERENCES llm_calls (id),
    candidate_call_id INTEGER REFERENCES llm_calls (id),
    worker_sources    INTEGER NOT NULL CHECK (worker_sources >= 0),
    candidate_sources INTEGER NOT NULL CHECK (candidate_sources >= 0),
    worker_chars      INTEGER NOT NULL CHECK (worker_chars >= 0),
    candidate_chars   INTEGER NOT NULL CHECK (candidate_chars >= 0),
    candidate_ok      INTEGER NOT NULL CHECK (candidate_ok IN (0, 1))  -- it answered (not failed, not empty)
);
CREATE INDEX research_checks_by_model ON research_checks (mode, session, model);
CREATE TRIGGER research_checks_no_update BEFORE UPDATE ON research_checks
BEGIN SELECT RAISE(ABORT, 'research_checks: history cannot change'); END;
CREATE TRIGGER research_checks_no_delete BEFORE DELETE ON research_checks
BEGIN SELECT RAISE(ABORT, 'research_checks: history cannot change'); END;
