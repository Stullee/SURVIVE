-- 0.22.0 (analysis of 0.20.1, FIX NOW 12): the owner's park or kill stops a venture's projects (agent/stages.py:
-- stop_projects). Their projects stayed active and the agent worked on them. A venture the owner parked or killed
-- before 0.22.0 gets the same now: its open projects close with a kill, and wait while it is parked.
UPDATE projects
SET status = CASE (SELECT v.stage FROM ventures v WHERE v.id = projects.venture_id)
        WHEN 'killed' THEN 'abandoned' ELSE 'waiting' END,
    next_step = CASE (SELECT v.stage FROM ventures v WHERE v.id = projects.venture_id)
        WHEN 'killed' THEN 'None: closed with its venture.'
        ELSE 'None until your owner takes the venture up again.' END,
    notes = substr(CASE WHEN notes = '' THEN '' ELSE notes || char(10) END || '[owner] Your owner ' ||
        (SELECT v.stage FROM ventures v WHERE v.id = projects.venture_id) || ' venture #' || venture_id || '.', -2000),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE status IN ('idea', 'active', 'waiting')
    AND venture_id IN (
        SELECT v.id FROM ventures v WHERE v.stage = 'killed' OR (v.stage = 'parked' AND v.parked_by = 'owner')
    );
