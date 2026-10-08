-- 0.33.0: a promise names the product line (project) it is about. A promise had no line, so no cycle was ever taken
-- for it: live, the Haushaltsbuch KDP book was promised three times on 2026-10-07 and every cycle went to other
-- lines' obligations. The line is set when the promise is made; a promise made without one may get it once, when the
-- agent repeats the promise naming it, and is fixed from then on. Decisions and misses have their lines from their
-- records (the request, the milestone), so only a promise names one.
ALTER TABLE obligations ADD COLUMN project_id INTEGER REFERENCES projects (id);

CREATE TRIGGER obligations_project_promise BEFORE INSERT ON obligations
WHEN NEW.project_id IS NOT NULL AND NEW.kind <> 'promise'
BEGIN SELECT RAISE(ABORT, 'obligations: only a promise names its line'); END;
CREATE TRIGGER obligations_project_fixed BEFORE UPDATE OF project_id ON obligations
WHEN OLD.project_id IS NOT NULL AND NEW.project_id IS NOT OLD.project_id
    OR NEW.project_id IS NOT NULL AND OLD.kind <> 'promise'
BEGIN SELECT RAISE(ABORT, 'obligations: the line a promise is about is fixed'); END;
