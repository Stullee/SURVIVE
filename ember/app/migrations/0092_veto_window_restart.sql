-- 0.37.1: a veto window starts again once Ember runs after a time it couldn't act (agent/policy.py, restart). Paused
-- for 8 days, the owner resumed Ember, and the first round approved a reply whose window had run out during the pause,
-- before they could look. A use still never changes otherwise, and its window may only end later, and only before it
-- approved: the database refuses an earlier end, as it refuses every other change.
DROP TRIGGER policy_uses_fixed;
CREATE TRIGGER policy_uses_fixed BEFORE UPDATE OF approval_id, grant_id, level, created_at ON policy_uses
BEGIN SELECT RAISE(ABORT, 'policy_uses: a use never changes'); END;
CREATE TRIGGER policy_uses_window_later BEFORE UPDATE OF veto_until ON policy_uses
WHEN OLD.approved_at IS NOT NULL OR OLD.veto_until IS NULL OR NEW.veto_until IS NULL
    OR NEW.veto_until < OLD.veto_until
BEGIN SELECT RAISE(ABORT, 'policy_uses: a veto window only ever ends later, before it approved'); END;
