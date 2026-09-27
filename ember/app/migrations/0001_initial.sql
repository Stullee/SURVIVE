-- Phase 1: the foundation every later phase builds on.
-- Migrations must not contain BEGIN/COMMIT; the runner wraps each file in a transaction.

-- Small key/value facts about the agent's life: birth time, last start, ...
CREATE TABLE meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The system log shown in the dashboard: startups, config problems, errors.
-- Later phases add dedicated tables for cycles, model calls and tool calls.
CREATE TABLE events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
    kind    TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT CHECK (details IS NULL OR json_valid(details))
);

CREATE INDEX events_by_level ON events (level, id);
