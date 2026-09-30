-- 0.13.0: an agenda with event wake-ups. Only the timer and the owner's messages and decisions woke the agent: a sale,
-- a reply to Ember's email or a milestone's last day waited for the next scheduled cycle, up to the longest sleep.
-- Now Ember's code notes such events as it sees them, between cycles too (agent/agenda.py): the next plan shows them,
-- and an urgent one wakes the agent for a lean reactive cycle (a few a day at most). A favorites count a listing
-- already had when the agenda began is a baseline, never shown. What an event is about never changes; none is
-- deleted.
CREATE TABLE agenda (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL,
    session       INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('order', 'reply', 'favorites', 'milestone_due')),
    key           TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 100),  -- what it is about (a receipt, an email, ...)
    text          TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
    urgent        INTEGER NOT NULL CHECK (urgent IN (0, 1)),
    baseline      INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)),
    noted_at      TEXT NOT NULL,
    woke_at       TEXT,  -- when it woke the agent (NULL: it didn't)
    seen_cycle_id INTEGER REFERENCES cycles (id),  -- the cycle whose plan showed it
    UNIQUE (mode, session, kind, key)
);
CREATE INDEX agenda_open ON agenda (mode, session, seen_cycle_id, id);
CREATE TRIGGER agenda_event_fixed BEFORE UPDATE OF mode, session, kind, key, text, urgent, baseline, noted_at ON agenda
BEGIN SELECT RAISE(ABORT, 'agenda: an event never changes'); END;
CREATE TRIGGER agenda_seen_once BEFORE UPDATE OF seen_cycle_id ON agenda
WHEN OLD.seen_cycle_id IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'agenda: an event is seen once'); END;
CREATE TRIGGER agenda_no_delete BEFORE DELETE ON agenda
BEGIN SELECT RAISE(ABORT, 'agenda: rows cannot be deleted'); END;
