-- 0.23.2: the owner's park or kill stops a venture's projects (0.22.0, migration 0077), and now takes every open
-- milestone of those projects with it, and the open steps leading to them (agent/stages.py: drop_milestones). Before,
-- it took only the bars Ember's code set for them: the agent's own milestones stayed open, and work went on through
-- them (the plan's focus, and the unlocks they carried). What earlier parks and kills left open is dropped now.
WITH RECURSIVE stopped(id) AS (
    SELECT m.id FROM milestones m
    JOIN projects p ON p.id = m.project_id
    JOIN ventures v ON v.id = p.venture_id
    WHERE m.status = 'open' AND (v.stage = 'killed' OR (v.stage = 'parked' AND v.parked_by = 'owner'))
    UNION
    SELECT m.id FROM milestones m JOIN stopped s ON m.parent_id = s.id WHERE m.status = 'open'
)
UPDATE milestones
SET status = 'dropped',
    result = 'Your owner parked or killed the venture its project belongs to, which stops the project''s work'
        || ' (dropped at the upgrade to 0.23.2).',
    closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    closed_by = 'owner',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    proposed_due = NULL, proposed_note = NULL, proposed_at = NULL, proposed_cycle_id = NULL
WHERE id IN (SELECT id FROM stopped) AND status = 'open';
