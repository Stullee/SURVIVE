-- 0.12.0: what each model call and each request to the owner worked for. A cycle's whole cost was charged to its focus
-- milestone and to the venture of its focus or of its project, its plan too, and a research call for another venture
-- counted for the focus one. Now every model call names the venture and milestone its work served (venture_id,
-- milestone_id), and plans, reviews, brainstorms, library study and the last will are marked overhead: they serve no
-- one venture or milestone. A request to the owner (approvals) names the venture and milestone of the cycle that made
-- it. The calls and requests made before are attributed the same way from their cycles; a research call made for a
-- venture (venture_research) counts for that venture.
ALTER TABLE llm_calls ADD COLUMN venture_id INTEGER REFERENCES ventures (id);
ALTER TABLE llm_calls ADD COLUMN milestone_id INTEGER REFERENCES milestones (id);
ALTER TABLE llm_calls ADD COLUMN overhead INTEGER NOT NULL DEFAULT 0 CHECK (overhead IN (0, 1));
ALTER TABLE approvals ADD COLUMN venture_id INTEGER REFERENCES ventures (id);
ALTER TABLE approvals ADD COLUMN milestone_id INTEGER REFERENCES milestones (id);

-- The history's attribution, once: a finalized call can't change, so its guard steps aside for this update only.
DROP TRIGGER llm_calls_finalize_once;
UPDATE llm_calls SET overhead = CASE WHEN purpose IN ('work', 'reflect', 'research', 'workshop') THEN 0 ELSE 1 END;
UPDATE llm_calls SET
    milestone_id = (SELECT y.milestone_id FROM cycles y WHERE y.id = llm_calls.cycle_id),
    venture_id = COALESCE(
        (SELECT MIN(r.venture_id) FROM venture_research r WHERE r.llm_call_id = llm_calls.id),
        (SELECT COALESCE(y.venture_id, p.venture_id) FROM cycles y LEFT JOIN projects p ON p.id = y.project_id
            WHERE y.id = llm_calls.cycle_id)
    )
WHERE overhead = 0;
CREATE TRIGGER llm_calls_finalize_once BEFORE UPDATE ON llm_calls
WHEN OLD.status <> 'pending'
BEGIN
    SELECT RAISE(ABORT, 'llm_calls: a finalized call cannot change');
END;
-- (0017) recreated after it, so the two guards speak in the same order as before.
DROP TRIGGER llm_calls_version_fixed;
CREATE TRIGGER llm_calls_version_fixed BEFORE UPDATE OF app_version ON llm_calls
WHEN NEW.app_version IS NOT OLD.app_version
BEGIN SELECT RAISE(ABORT, 'llm_calls: the version that made a call is fixed'); END;
UPDATE approvals SET
    milestone_id = (SELECT y.milestone_id FROM cycles y WHERE y.id = approvals.cycle_id),
    venture_id = COALESCE(
        (SELECT p.venture_id FROM projects p WHERE p.id = approvals.project_id),
        (SELECT COALESCE(y.venture_id, p.venture_id) FROM cycles y LEFT JOIN projects p ON p.id = y.project_id
            WHERE y.id = approvals.cycle_id)
    );
CREATE INDEX llm_calls_by_venture ON llm_calls (venture_id) WHERE venture_id IS NOT NULL;
CREATE INDEX llm_calls_by_milestone ON llm_calls (milestone_id) WHERE milestone_id IS NOT NULL;
CREATE TRIGGER approvals_attribution_fixed BEFORE UPDATE OF venture_id, milestone_id ON approvals
WHEN NEW.venture_id IS NOT OLD.venture_id OR NEW.milestone_id IS NOT OLD.milestone_id
BEGIN SELECT RAISE(ABORT, 'approvals: what a request worked for is fixed'); END;
