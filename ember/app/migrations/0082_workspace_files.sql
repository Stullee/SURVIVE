-- 0.26.0: which project or venture each file of the agent's workspace was written for, so the owner's Workspace tab
-- groups the files by them (agent/workfiles.py). Ember's code records it as a tool writes a file: the focus of the
-- cycle that wrote it (its plan's project, or the venture it studies). A file keeps the first focus it was written
-- under; one written without a focus takes the next one's. The cycle and the tool of its last write are kept too. A
-- file deleted goes from here as well. The files written before 0.26.0 are filled in once from the tool calls that
-- named them.
CREATE TABLE workspace_files (
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    path       TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 200),
    project_id INTEGER REFERENCES projects (id),
    venture_id INTEGER REFERENCES ventures (id),
    cycle_id   INTEGER REFERENCES cycles (id),
    tool       TEXT CHECK (tool IS NULL OR length(tool) <= 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (mode, session, path)
) WITHOUT ROWID;
CREATE INDEX workspace_files_by_project ON workspace_files (project_id) WHERE project_id IS NOT NULL;
CREATE INDEX workspace_files_by_venture ON workspace_files (venture_id) WHERE venture_id IS NOT NULL;
