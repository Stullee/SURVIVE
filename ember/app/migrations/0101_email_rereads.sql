-- 0.45.4: an email read again from the server after Ember's way of reading an email's text changed (mail.TEXT_VERSION).
-- Live, KDP's "Attention needed" emails #64 and #65 were stored as their footer and case number (0.45.2 fixed the
-- reading), and a stored email never changes (emails_content_fixed): its text as read again is kept beside it, and
-- email_read shows the newest. text_version: how the stored text was read (NULL: before 0.45.4, so version 0); fixed
-- once stored, like the rest of the email.
ALTER TABLE emails ADD COLUMN text_version INTEGER CHECK (text_version IS NULL OR text_version >= 0);
CREATE TRIGGER emails_text_version_fixed BEFORE UPDATE OF text_version ON emails
WHEN NEW.text_version IS NOT OLD.text_version
BEGIN SELECT RAISE(ABORT, 'emails: a stored email cannot change'); END;

-- One row per email and version it was read again with: 'same' (its text didn't change), 'changed' (the text it has
-- now, which the agent reads once like a new email) or 'gone' (the server didn't hand it over again, or its text
-- couldn't be stored). Final once written, but for when the agent read a changed text.
CREATE TABLE email_rereads (
    id               INTEGER PRIMARY KEY,
    email_id         INTEGER NOT NULL REFERENCES emails (id),
    text_version     INTEGER NOT NULL CHECK (text_version >= 1),
    read_at          TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('same', 'changed', 'gone')),
    body             TEXT CHECK (body IS NULL OR length(body) <= 8000),
    body_cut         INTEGER CHECK (body_cut IS NULL OR body_cut IN (0, 1)),
    attachments      TEXT CHECK (attachments IS NULL OR (json_valid(attachments) AND length(attachments) <= 2000)),
    read_by_agent_at TEXT,
    CHECK ((status = 'changed') = (body IS NOT NULL AND body_cut IS NOT NULL AND attachments IS NOT NULL)),
    CHECK (status = 'changed' OR read_by_agent_at IS NULL),
    UNIQUE (email_id, text_version)
);
CREATE TRIGGER email_rereads_fixed BEFORE UPDATE ON email_rereads
WHEN NEW.email_id IS NOT OLD.email_id OR NEW.text_version IS NOT OLD.text_version OR NEW.read_at IS NOT OLD.read_at
    OR NEW.status IS NOT OLD.status OR NEW.body IS NOT OLD.body OR NEW.body_cut IS NOT OLD.body_cut
    OR NEW.attachments IS NOT OLD.attachments
    OR (OLD.read_by_agent_at IS NOT NULL AND NEW.read_by_agent_at IS NOT OLD.read_by_agent_at)
BEGIN SELECT RAISE(ABORT, 'email_rereads: an email read again cannot change'); END;
CREATE TRIGGER email_rereads_no_delete BEFORE DELETE ON email_rereads
BEGIN SELECT RAISE(ABORT, 'email_rereads: rows cannot be deleted'); END;
