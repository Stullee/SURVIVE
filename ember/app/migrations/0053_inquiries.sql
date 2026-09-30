-- 0.13.0 (Phase E1): inbound email as a channel. People who write to Ember (a buyer's question, a reader) are this
-- channel's demand: each email from a person waits for an answer (integrations/mailstore.py) until Ember answers it
-- (an email request to them that isn't rejected), the agent closes it as needing none, or they asked to stop.
-- Newsletters and automatic replies are no inquiries: their headers say so, now kept per email (bulk; NULL for mail
-- stored before, which never counts as an inquiry). A new inquiry wakes the agent like a reply to its email: the
-- agenda gains the kind 'inquiry' (rebuilt as 0010 rebuilt approvals; nothing else about it changes).
ALTER TABLE emails ADD COLUMN bulk INTEGER CHECK (bulk IS NULL OR bulk IN (0, 1));

-- An inquiry the agent closed without an answer (a thank-you, spam, mail meant for someone else), with why.
CREATE TABLE inquiry_closures (
    email_id   INTEGER PRIMARY KEY REFERENCES emails (id),
    reason     TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 300),
    by         TEXT NOT NULL CHECK (length(by) BETWEEN 1 AND 60),
    created_at TEXT NOT NULL
);
CREATE TRIGGER inquiry_closures_no_update BEFORE UPDATE ON inquiry_closures
BEGIN SELECT RAISE(ABORT, 'inquiry_closures: a closure never changes'); END;
CREATE TRIGGER inquiry_closures_no_delete BEFORE DELETE ON inquiry_closures
BEGIN SELECT RAISE(ABORT, 'inquiry_closures: rows cannot be deleted'); END;

CREATE TABLE agenda_new (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL,
    session       INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('order', 'reply', 'inquiry', 'favorites', 'milestone_due')),
    key           TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 100),  -- what it is about (a receipt, an email, ...)
    text          TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
    urgent        INTEGER NOT NULL CHECK (urgent IN (0, 1)),
    baseline      INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)),
    noted_at      TEXT NOT NULL,
    woke_at       TEXT,  -- when it woke the agent (NULL: it didn't)
    seen_cycle_id INTEGER REFERENCES cycles (id),  -- the cycle whose plan showed it
    UNIQUE (mode, session, kind, key)
);
INSERT INTO agenda_new (id, mode, session, kind, key, text, urgent, baseline, noted_at, woke_at, seen_cycle_id)
SELECT id, mode, session, kind, key, text, urgent, baseline, noted_at, woke_at, seen_cycle_id FROM agenda;
DROP TABLE agenda;
ALTER TABLE agenda_new RENAME TO agenda;
CREATE INDEX agenda_open ON agenda (mode, session, seen_cycle_id, id);
CREATE TRIGGER agenda_event_fixed BEFORE UPDATE OF mode, session, kind, key, text, urgent, baseline, noted_at ON agenda
BEGIN SELECT RAISE(ABORT, 'agenda: an event never changes'); END;
CREATE TRIGGER agenda_seen_once BEFORE UPDATE OF seen_cycle_id ON agenda
WHEN OLD.seen_cycle_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'agenda: an event is seen once'); END;
CREATE TRIGGER agenda_no_delete BEFORE DELETE ON agenda
BEGIN SELECT RAISE(ABORT, 'agenda: rows cannot be deleted'); END;
