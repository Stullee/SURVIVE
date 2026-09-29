-- 0.12.0: who parked a venture. The agent took ventures out of the owner's park (parked → researching) as it liked;
-- now only the owner's word (Research next or Back it, a new owner_version) moves a venture they parked. Ventures
-- parked before are the owner's when their last word was park, or when the owner put them on hold before ventures
-- existed (Fiverr, planted parked); the agent's otherwise.
ALTER TABLE ventures ADD COLUMN parked_by TEXT CHECK (parked_by IS NULL OR parked_by IN ('agent', 'owner'));
UPDATE ventures SET parked_by = CASE
    WHEN owner_action = 'park' OR (created_by = 'owner' AND created_cycle_id IS NULL AND notes LIKE 'Your owner %')
        THEN 'owner'
    ELSE 'agent' END
WHERE stage = 'parked';
CREATE TRIGGER ventures_owner_park BEFORE UPDATE OF stage ON ventures
WHEN OLD.stage = 'parked' AND OLD.parked_by = 'owner' AND NEW.stage IS NOT 'parked'
    AND NEW.owner_version = OLD.owner_version
BEGIN SELECT RAISE(ABORT, 'ventures: only the owner takes a venture out of their park'); END;
