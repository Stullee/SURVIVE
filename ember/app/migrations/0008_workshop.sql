-- 0.7.0: the workshop. Each run of code in Anthropic's sandbox is one row: what the agent asked for, the script it
-- ran again (if any), the script and files it kept, the files refused and why, and what the run cost. Rows never
-- change: they are how Ember's code sees which scripts proved useful.
CREATE TABLE workshop_runs (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    life_id     INTEGER NOT NULL REFERENCES lives (id),
    cycle_id    INTEGER NOT NULL REFERENCES cycles (id),
    created_at  TEXT NOT NULL,
    task        TEXT NOT NULL CHECK (length(task) BETWEEN 1 AND 3000),
    script_used TEXT CHECK (script_used IS NULL OR length(script_used) <= 200),
    script_path TEXT CHECK (script_path IS NULL OR length(script_path) <= 200),
    inputs      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(inputs)),
    outputs     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(outputs)),
    refused     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(refused)),
    status      TEXT NOT NULL CHECK (status IN ('ok', 'nothing', 'failed')),
    cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    summary     TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 2000)
);
CREATE INDEX workshop_runs_by_scope ON workshop_runs (mode, session, id);
CREATE TRIGGER workshop_runs_no_update BEFORE UPDATE ON workshop_runs
BEGIN SELECT RAISE(ABORT, 'workshop_runs: history cannot change'); END;
CREATE TRIGGER workshop_runs_no_delete BEFORE DELETE ON workshop_runs
BEGIN SELECT RAISE(ABORT, 'workshop_runs: history cannot change'); END;

-- An upgrade request can carry a workshop script: the code that proved useful, kept as it was when the agent asked,
-- so whoever builds the upgrade has it even after the agent changed or deleted the file.
ALTER TABLE upgrades ADD COLUMN script_path TEXT CHECK (script_path IS NULL OR length(script_path) <= 200);
ALTER TABLE upgrades ADD COLUMN script_text TEXT CHECK (script_text IS NULL OR length(script_text) <= 65536);

DROP TRIGGER upgrades_request_fixed;
CREATE TRIGGER upgrades_request_fixed BEFORE UPDATE ON upgrades
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.created_at IS NOT OLD.created_at OR NEW.title IS NOT OLD.title
    OR NEW.problem IS NOT OLD.problem OR NEW.proposed_change IS NOT OLD.proposed_change
    OR NEW.expected_benefit IS NOT OLD.expected_benefit OR NEW.priority IS NOT OLD.priority
    OR NEW.script_path IS NOT OLD.script_path OR NEW.script_text IS NOT OLD.script_text
BEGIN SELECT RAISE(ABORT, 'upgrades: the request itself cannot change'); END;
