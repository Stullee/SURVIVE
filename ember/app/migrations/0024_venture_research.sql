-- 0.12.0: research bound to a venture. Scores were labelled research without any research, and a venture went from
-- idea to proposed with one-letter fields. Now each research call made for a venture (its venture_id; in a venture
-- cycle, the cycle's focus venture unless it names another) is recorded here by Ember's code, with how many web pages
-- it found or read and what it cost. A call that found something counts: scores from research need one, a business
-- case (stage proposed) two, and the database refuses them otherwise. Written by code only; never changed.
CREATE TABLE venture_research (
    id          INTEGER PRIMARY KEY,
    venture_id  INTEGER NOT NULL REFERENCES ventures (id),
    cycle_id    INTEGER NOT NULL REFERENCES cycles (id),
    llm_call_id INTEGER REFERENCES llm_calls (id),  -- the research call (the first, when it paused)
    question    TEXT NOT NULL CHECK (length(question) BETWEEN 1 AND 500),
    url         TEXT CHECK (url IS NULL OR length(url) <= 250),  -- the page it read, when it read one
    sources     INTEGER NOT NULL CHECK (sources >= 0),  -- the web pages it found or read
    cost_micros INTEGER NOT NULL CHECK (cost_micros >= 0),
    created_at  TEXT NOT NULL
);
CREATE INDEX venture_research_by_venture ON venture_research (venture_id, sources);
CREATE TRIGGER venture_research_no_update BEFORE UPDATE ON venture_research
BEGIN SELECT RAISE(ABORT, 'venture_research: rows are never changed'); END;
CREATE TRIGGER venture_research_no_delete BEFORE DELETE ON venture_research
BEGIN SELECT RAISE(ABORT, 'venture_research: rows cannot be deleted'); END;

-- A new venture has no research yet: it starts unscored or with a brainstorm's guesses, and never as a business case.
CREATE TRIGGER ventures_start_unresearched BEFORE INSERT ON ventures
WHEN NEW.stage = 'proposed' OR NEW.scores_by = 'research'
BEGIN SELECT RAISE(ABORT, 'ventures: a new venture has no research yet'); END;
-- Scores given as research need a research call for the venture that found something.
CREATE TRIGGER ventures_scores_researched BEFORE UPDATE ON ventures
WHEN NEW.scores_by = 'research'
    AND (OLD.scores_by IS NOT 'research' OR NEW.revenue IS NOT OLD.revenue OR NEW.doability IS NOT OLD.doability
        OR NEW.difficulty IS NOT OLD.difficulty OR NEW.risk IS NOT OLD.risk OR NEW.speed IS NOT OLD.speed
        OR NEW.cost IS NOT OLD.cost)
    AND NOT EXISTS (SELECT 1 FROM venture_research WHERE venture_id = NEW.id AND sources > 0)
BEGIN SELECT RAISE(ABORT, 'ventures: scores from research need research that found something'); END;
-- A business case needs two research calls for the venture that found something.
CREATE TRIGGER ventures_proposed_researched BEFORE UPDATE OF stage ON ventures
WHEN NEW.stage = 'proposed' AND OLD.stage IS NOT 'proposed'
    AND (SELECT COUNT(*) FROM venture_research WHERE venture_id = NEW.id AND sources > 0) < 2
BEGIN SELECT RAISE(ABORT, 'ventures: a business case needs two research calls that found something'); END;
