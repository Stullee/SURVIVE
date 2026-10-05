-- 0.22.0 (analysis of 0.20.1, FIX NOW 13): an unlock of email replies runs at most with the veto window. Ember's code
-- checks only a reply's thread (one the other person started) and its words, not what it may quote of other people's
-- mail, so none goes out at once. Standing "auto" ones are taken back (the owner may grant the veto window again).
CREATE TRIGGER policy_grants_replies_veto BEFORE INSERT ON policy_grants
WHEN NEW.rule = 'email_reply' AND NEW.level = 'auto'
BEGIN SELECT RAISE(ABORT, 'policy_grants: email replies run at most with a veto window'); END;

INSERT INTO policy_grants (mode, session, milestone_id, rule, level, per_day, budget, by, why, created_at)
SELECT g.mode, g.session, g.milestone_id, g.rule, 'manual', g.per_day, g.budget, 'Ember''s code',
    'email replies run at most with a veto window since 0.22.0: grant that again if you like',
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
FROM policy_grants g
WHERE g.rule = 'email_reply' AND g.level = 'auto'
    AND g.id = (SELECT MAX(h.id) FROM policy_grants h WHERE h.milestone_id = g.milestone_id AND h.rule = g.rule);
