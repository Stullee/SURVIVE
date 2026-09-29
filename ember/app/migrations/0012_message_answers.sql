-- 0.9.1: an owner's message stays in the agent's plans until the agent answers it (message_owner names it in
-- answers), not only until the agent was shown it: a cycle that ended before its reply lost the owner's questions.
-- answered_by is the agent's message that answered it; once set it never changes.
ALTER TABLE messages ADD COLUMN answered_by INTEGER REFERENCES messages (id)
    CHECK (answered_by IS NULL OR sender = 'owner');

CREATE TRIGGER messages_answer_final BEFORE UPDATE OF answered_by ON messages
WHEN OLD.answered_by IS NOT NULL AND NEW.answered_by IS NOT OLD.answered_by
BEGIN SELECT RAISE(ABORT, 'messages: an answer is final'); END;

-- Messages from before: one the agent was shown counts as answered by the agent's first message in that cycle or a
-- later one, so only questions that never got a reply stay open.
UPDATE messages SET answered_by = (
    SELECT a.id FROM messages a
    WHERE a.sender = 'agent' AND a.mode = messages.mode AND a.session = messages.session
        AND a.cycle_id >= messages.seen_cycle_id
    ORDER BY a.id LIMIT 1
) WHERE sender = 'owner' AND seen_cycle_id IS NOT NULL;
