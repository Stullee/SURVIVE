-- 0.33.0: the text a workspace file held before workspace_write overwrote, edited, copied over or deleted it, so the
-- agent can restore it (workspace_write's restore). Live, cycle #134 of 2026-10-07 overwrote the whole interior of a
-- book (24 KB, all twelve months) with its 671-byte header, and nothing could bring it back. Text files only (64 KB at
-- most); Ember's code keeps the newest few of each file (tools.VERSIONS_KEPT).
CREATE TABLE workspace_versions (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    path       TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 300),
    content    TEXT NOT NULL CHECK (length(CAST(content AS BLOB)) <= 65536),
    reason     TEXT NOT NULL CHECK (reason IN ('overwrite', 'edit', 'copy', 'delete', 'restore')),
    cycle_id   INTEGER REFERENCES cycles (id),
    created_at TEXT NOT NULL
);
CREATE INDEX workspace_versions_by_path ON workspace_versions (mode, session, path, id);
