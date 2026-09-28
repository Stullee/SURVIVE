-- The owner can remove the text of their own inbox message (for example a password sent by mistake). The row
-- stays, so the history shows that something was removed, when and by whom; any other change is still refused.

ALTER TABLE messages ADD COLUMN removed_at TEXT;
ALTER TABLE messages ADD COLUMN removed_by TEXT CHECK (removed_by IS NULL OR length(removed_by) <= 60);

DROP TRIGGER messages_fixed;
CREATE TRIGGER messages_fixed BEFORE UPDATE ON messages
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.created_at IS NOT OLD.created_at OR NEW.sender IS NOT OLD.sender OR NEW.cycle_id IS NOT OLD.cycle_id
    OR (NEW.text IS NOT OLD.text AND NOT (
        OLD.sender = 'owner' AND OLD.removed_at IS NULL AND NEW.removed_at IS NOT NULL
        AND NEW.text = '[removed by the owner]'))
    OR (NEW.removed_at IS NOT OLD.removed_at AND NOT (OLD.removed_at IS NULL AND NEW.text = '[removed by the owner]'))
    OR (OLD.removed_at IS NOT NULL AND NEW.removed_by IS NOT OLD.removed_by)
BEGIN SELECT RAISE(ABORT, 'messages: a message cannot change'); END;
