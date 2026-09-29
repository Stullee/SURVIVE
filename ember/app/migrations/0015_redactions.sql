-- 0.11.2: the words the owner removed. When the owner removes the text of a message (a password sent by mistake),
-- its secret-looking words are kept here as salted SHA-256 hashes, never as text. Ember's code finds their copies
-- by them (the agent's replies, journal, memory and tool inputs quote the owner): the diagnostics report leaves them
-- out, and the memory files, open projects and workspace files are scrubbed. The history can't change, so a copy
-- there is redacted when shown. The salt is in meta; the report never shows a secret.* value.
CREATE TABLE redactions (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    message_id INTEGER NOT NULL REFERENCES messages (id),
    digest     TEXT NOT NULL UNIQUE CHECK (length(digest) = 64)
);
CREATE TRIGGER redactions_no_update BEFORE UPDATE ON redactions
BEGIN SELECT RAISE(ABORT, 'redactions: a registered word stays registered'); END;
CREATE TRIGGER redactions_no_delete BEFORE DELETE ON redactions
BEGIN SELECT RAISE(ABORT, 'redactions: a registered word stays registered'); END;

INSERT INTO meta (key, value, updated_at)
VALUES ('secret.redaction_salt', lower(hex(randomblob(16))), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
ON CONFLICT (key) DO NOTHING;
