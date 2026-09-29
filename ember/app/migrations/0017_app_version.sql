-- 0.12.0: which version of Ember ran each wake cycle, model call and daily review, so a change in behaviour can be
-- traced to the release that brought it (releases came hours apart, and nothing recorded which one ran). Written once
-- when the row is made; rows from before stay empty.
ALTER TABLE cycles ADD COLUMN app_version TEXT CHECK (app_version IS NULL OR length(app_version) <= 40);
ALTER TABLE llm_calls ADD COLUMN app_version TEXT CHECK (app_version IS NULL OR length(app_version) <= 40);
ALTER TABLE reviews ADD COLUMN app_version TEXT CHECK (app_version IS NULL OR length(app_version) <= 40);

CREATE TRIGGER cycles_version_fixed BEFORE UPDATE OF app_version ON cycles
WHEN NEW.app_version IS NOT OLD.app_version
BEGIN SELECT RAISE(ABORT, 'cycles: the version that ran a cycle is fixed'); END;
CREATE TRIGGER llm_calls_version_fixed BEFORE UPDATE OF app_version ON llm_calls
WHEN NEW.app_version IS NOT OLD.app_version
BEGIN SELECT RAISE(ABORT, 'llm_calls: the version that made a call is fixed'); END;
