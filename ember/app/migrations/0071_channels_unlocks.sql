-- 0.16.3: bugs 1 and 5 of the 0.16.1 analysis.
--
-- Bug 1. A product line belongs to its channel's venture (agent/ventures.py, adopt): an Etsy listing, a digital
-- download, to the Etsy leg; a Printify product to the print-on-demand venture. 0068 linked an open line to the venture
-- its newest request worked for, the cycle's focus: the Nebenkosten template, a digital download, joined print on
-- demand, so Ember's code's park of that venture would have ended its listing test, and its sales counted there. An
-- open project under a print-on-demand venture whose requests made Etsy listings and never a Printify product goes to
-- the Etsy leg (to none, when the leg is parked or killed). A closed project is final.
UPDATE projects SET
    venture_id = (
        SELECT v.id FROM ventures v
        WHERE v.mode = projects.mode AND v.session = projects.session AND v.title = 'Etsy digital products'
            AND v.parent_id IS NULL AND v.stage NOT IN ('parked', 'killed')
        ORDER BY v.id LIMIT 1
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE status IN ('idea', 'active', 'waiting')
    AND venture_id IN (SELECT id FROM ventures WHERE channel = 'printify')
    AND id IN (
        SELECT COALESCE(a.project_id, y.project_id) FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE a.executor IN ('etsy_listing', 'etsy_edit') AND COALESCE(a.project_id, y.project_id) IS NOT NULL
    )
    AND id NOT IN (
        SELECT COALESCE(a.project_id, y.project_id) FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE a.executor = 'printify_product' AND COALESCE(a.project_id, y.project_id) IS NOT NULL
    );

-- Bug 5. An unlock was written into the milestone's one note of the owner's ("Unlocked for this milestone: …", each click
-- over the last), and no take-back changed it: not the upgrade to 0.15.0's (0062), nor Ember's code's later ones. The
-- planner showed it as the owner's word, and the Roadmap tab above an Autonomy box saying "taken back". What is unlocked
-- is said from the grants now (agent/policy.py, standing), and no unlock or take-back writes a note. A note that says
-- a rule is unlocked while no grant of that rule stands on its milestone (an open one) is cleared, with the owner's
-- word it posed as.
UPDATE milestones SET
    owner_action = NULL,
    owner_comment = NULL,
    owner_at = NULL,
    owner_by = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE owner_action = 'note' AND owner_comment LIKE 'Unlocked for this milestone:%'
    AND NOT EXISTS (
        SELECT 1 FROM policy_grants g
        WHERE g.milestone_id = milestones.id AND milestones.status = 'open' AND g.level <> 'manual'
            AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
            AND milestones.owner_comment LIKE 'Unlocked for this milestone: ' || CASE g.rule
                WHEN 'qa_fix' THEN 'QA fixes'
                WHEN 'price_change' THEN 'price changes'
                WHEN 'listing_variant' THEN 'new listings'
                WHEN 'deactivate' THEN 'taking a listing'
                WHEN 'email_reply' THEN 'email replies'
            END || '%'
    );

-- A change of a milestone's unlocks is news for the agent (agent/news.py): the owner's unlock or take-back, and Ember's
-- code's take-back, which it never heard of. The changes a plan showed are kept here. Those made before were heard as
-- the owner's note, except Ember's code's take-backs: the ones that stand on an open milestone (the upgrade to 0.15.0's
-- among them) are its news once now.
CREATE TABLE policy_grants_seen (
    grant_id INTEGER PRIMARY KEY REFERENCES policy_grants (id),
    cycle_id INTEGER REFERENCES cycles (id)  -- the cycle whose plan showed it (none: seen before this table came)
);
CREATE TRIGGER policy_grants_seen_no_update BEFORE UPDATE ON policy_grants_seen
BEGIN SELECT RAISE(ABORT, 'policy_grants_seen: what the agent saw stays seen'); END;
CREATE TRIGGER policy_grants_seen_no_delete BEFORE DELETE ON policy_grants_seen
BEGIN SELECT RAISE(ABORT, 'policy_grants_seen: rows cannot be deleted'); END;
INSERT INTO policy_grants_seen (grant_id)
SELECT g.id FROM policy_grants g
WHERE NOT (
    g.by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember') AND g.level = 'manual'
    AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule)
    AND EXISTS (SELECT 1 FROM milestones m WHERE m.id = g.milestone_id AND m.status = 'open')
);
