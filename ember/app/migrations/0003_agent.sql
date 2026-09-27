-- Phase 3: the agent's own records. Everything the agent writes is scoped to a
-- mode ('live' / 'dry_run') and a session (0 for live; a new number for every
-- dry-run session), so a dry run never mixes with the live agent's history.
-- History is append-only; rows that have a lifecycle (projects, queue items)
-- can only change the columns that lifecycle needs.

-- Wake-cycle progress, written by the runner while the cycle is running.
ALTER TABLE cycles ADD COLUMN session INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cycles ADD COLUMN phase TEXT;
ALTER TABLE cycles ADD COLUMN step INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cycles ADD COLUMN max_steps INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cycles ADD COLUMN current_action TEXT CHECK (current_action IS NULL OR length(current_action) <= 300);
ALTER TABLE cycles ADD COLUMN plan TEXT CHECK (plan IS NULL OR json_valid(plan));
ALTER TABLE cycles ADD COLUMN project_id INTEGER REFERENCES projects (id);
ALTER TABLE cycles ADD COLUMN act_end_reason TEXT;
ALTER TABLE cycles ADD COLUMN sleep_minutes INTEGER;

-- A cycle's identity and cap never change, and a finished cycle is final.
CREATE TRIGGER cycles_guard BEFORE UPDATE ON cycles
WHEN OLD.status <> 'running' OR NEW.life_id IS NOT OLD.life_id OR NEW.boot_id IS NOT OLD.boot_id
    OR NEW.started_at IS NOT OLD.started_at OR NEW.trigger IS NOT OLD.trigger
    OR NEW.simulated IS NOT OLD.simulated OR NEW.cap_micros IS NOT OLD.cap_micros OR NEW.session IS NOT OLD.session
BEGIN
    SELECT RAISE(ABORT, 'cycles: identity and cap are fixed; a finished cycle is final');
END;

CREATE TABLE projects (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER NOT NULL REFERENCES lives (id),
    created_cycle_id INTEGER NOT NULL REFERENCES cycles (id),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    title            TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 80),
    hypothesis       TEXT NOT NULL CHECK (length(hypothesis) BETWEEN 1 AND 400),
    status           TEXT NOT NULL CHECK (status IN ('idea', 'active', 'waiting', 'succeeded', 'failed', 'abandoned')),
    next_step        TEXT NOT NULL DEFAULT '' CHECK (length(next_step) <= 200),
    notes            TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 2000)
);
CREATE INDEX projects_by_scope ON projects (mode, session, status);
CREATE TRIGGER projects_fixed BEFORE UPDATE ON projects
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.created_cycle_id IS NOT OLD.created_cycle_id OR NEW.created_at IS NOT OLD.created_at
    OR NEW.title IS NOT OLD.title OR OLD.status IN ('succeeded', 'failed', 'abandoned')
BEGIN
    SELECT RAISE(ABORT, 'projects: identity is fixed and a closed project is final');
END;
CREATE TRIGGER projects_no_delete BEFORE DELETE ON projects
BEGIN
    SELECT RAISE(ABORT, 'projects: rows cannot be deleted');
END;

-- Every tool the agent used: inserted as 'started', finished once.
CREATE TABLE tool_calls (
    id          INTEGER PRIMARY KEY,
    cycle_id    INTEGER NOT NULL REFERENCES cycles (id),
    llm_call_id INTEGER NOT NULL REFERENCES llm_calls (id),
    parent_id   INTEGER REFERENCES tool_calls (id),
    seq         INTEGER NOT NULL,
    phase       TEXT NOT NULL CHECK (phase IN ('act', 'reflect', 'research')),
    origin      TEXT NOT NULL CHECK (origin IN ('local', 'server')),
    tool        TEXT NOT NULL CHECK (length(tool) <= 64),
    tool_use_id TEXT NOT NULL CHECK (length(tool_use_id) <= 100),
    project_id  INTEGER REFERENCES projects (id),
    input       TEXT NOT NULL CHECK (json_valid(input) AND length(input) <= 20000),
    status      TEXT NOT NULL CHECK (status IN ('started', 'ok', 'error', 'skipped', 'interrupted')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT CHECK (summary IS NULL OR length(summary) <= 300),
    result      TEXT CHECK (result IS NULL OR length(result) <= 8000),
    UNIQUE (llm_call_id, seq)
);
CREATE INDEX tool_calls_by_cycle ON tool_calls (cycle_id, id);
CREATE INDEX tool_calls_started ON tool_calls (status) WHERE status = 'started';
CREATE TRIGGER tool_calls_finalize_once BEFORE UPDATE ON tool_calls
WHEN OLD.status <> 'started' OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.llm_call_id IS NOT OLD.llm_call_id
    OR NEW.seq IS NOT OLD.seq OR NEW.tool IS NOT OLD.tool OR NEW.input IS NOT OLD.input
    OR NEW.started_at IS NOT OLD.started_at OR NEW.parent_id IS NOT OLD.parent_id
BEGIN
    SELECT RAISE(ABORT, 'tool_calls: a finished tool call cannot change');
END;
CREATE TRIGGER tool_calls_no_delete BEFORE DELETE ON tool_calls
BEGIN
    SELECT RAISE(ABORT, 'tool_calls: rows cannot be deleted');
END;

-- The text the model wrote in a call (plans, reports, refusals), for the owner to read.
CREATE TABLE call_texts (
    llm_call_id  INTEGER PRIMARY KEY REFERENCES llm_calls (id),
    text         TEXT NOT NULL CHECK (length(text) <= 16000),
    stop_details TEXT CHECK (stop_details IS NULL OR json_valid(stop_details))
);

CREATE TABLE journal (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    life_id    INTEGER NOT NULL REFERENCES lives (id),
    cycle_id   INTEGER NOT NULL UNIQUE REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    author     TEXT NOT NULL CHECK (author IN ('agent', 'system')),
    summary    TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 240),
    entry      TEXT NOT NULL DEFAULT '' CHECK (length(entry) <= 2000)
);
CREATE INDEX journal_by_scope ON journal (mode, session, id);

-- Every version of the agent's memory files (strategy, identity, lessons).
CREATE TABLE memory_versions (
    id         INTEGER PRIMARY KEY,
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session    INTEGER NOT NULL,
    file       TEXT NOT NULL CHECK (file IN ('strategy', 'identity', 'lessons')),
    cycle_id   INTEGER REFERENCES cycles (id),
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('seed', 'agent', 'external')),
    sha256     TEXT NOT NULL,
    content    TEXT NOT NULL CHECK (length(content) <= 8000)
);
CREATE INDEX memory_versions_by_file ON memory_versions (mode, session, file, id);

CREATE TABLE last_wills (
    life_id     INTEGER PRIMARY KEY REFERENCES lives (id),
    cycle_id    INTEGER NOT NULL REFERENCES cycles (id),
    llm_call_id INTEGER NOT NULL REFERENCES llm_calls (id),
    created_at  TEXT NOT NULL,
    text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 6000),
    cut_off     INTEGER NOT NULL DEFAULT 0 CHECK (cut_off IN (0, 1))
);

-- Requests from the agent to its owner. The owner's side (decisions, replies) arrives in phase 4.
CREATE TABLE approvals (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER NOT NULL REFERENCES lives (id),
    cycle_id         INTEGER NOT NULL REFERENCES cycles (id),
    project_id       INTEGER REFERENCES projects (id),
    created_at       TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('publish', 'contact', 'create_account', 'spend_money', 'sell', 'other')),
    title            TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    description      TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
    payload          TEXT NOT NULL CHECK (length(payload) BETWEEN 1 AND 8000),
    payload_sha256   TEXT NOT NULL,
    -- Words only: never parsed as money and never used to prefill an owner form.
    expected_cost    TEXT NOT NULL CHECK (length(expected_cost) BETWEEN 1 AND 300),
    expected_benefit TEXT NOT NULL CHECK (length(expected_benefit) BETWEEN 1 AND 300),
    status           TEXT NOT NULL DEFAULT 'pending',
    seen_cycle_id    INTEGER REFERENCES cycles (id)
);
CREATE UNIQUE INDEX approvals_one_pending ON approvals (mode, session, payload_sha256) WHERE status = 'pending';
CREATE INDEX approvals_by_scope ON approvals (mode, session, status);

CREATE TABLE messages (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session       INTEGER NOT NULL,
    life_id       INTEGER NOT NULL REFERENCES lives (id),
    created_at    TEXT NOT NULL,
    sender        TEXT NOT NULL CHECK (sender IN ('agent', 'owner')),
    cycle_id      INTEGER REFERENCES cycles (id),
    text          TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    read_at       TEXT,
    seen_cycle_id INTEGER REFERENCES cycles (id),
    CHECK ((sender = 'agent') = (cycle_id IS NOT NULL))
);
CREATE INDEX messages_by_scope ON messages (mode, session, id);

CREATE TABLE upgrades (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session          INTEGER NOT NULL,
    life_id          INTEGER NOT NULL REFERENCES lives (id),
    cycle_id         INTEGER NOT NULL REFERENCES cycles (id),
    created_at       TEXT NOT NULL,
    title            TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    problem          TEXT NOT NULL CHECK (length(problem) BETWEEN 1 AND 600),
    proposed_change  TEXT NOT NULL CHECK (length(proposed_change) BETWEEN 1 AND 600),
    expected_benefit TEXT NOT NULL CHECK (length(expected_benefit) BETWEEN 1 AND 600),
    priority         TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high')),
    status           TEXT NOT NULL DEFAULT 'new',
    seen_cycle_id    INTEGER REFERENCES cycles (id)
);
CREATE INDEX upgrades_by_scope ON upgrades (mode, session, status);

-- History can't change; queue items keep what the agent wrote.
CREATE TRIGGER call_texts_no_update BEFORE UPDATE ON call_texts
BEGIN SELECT RAISE(ABORT, 'call_texts: history cannot change'); END;
CREATE TRIGGER call_texts_no_delete BEFORE DELETE ON call_texts
BEGIN SELECT RAISE(ABORT, 'call_texts: history cannot change'); END;
CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal: history cannot change'); END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal: history cannot change'); END;
CREATE TRIGGER memory_versions_no_update BEFORE UPDATE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory_versions: history cannot change'); END;
CREATE TRIGGER memory_versions_no_delete BEFORE DELETE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory_versions: history cannot change'); END;
CREATE TRIGGER last_wills_no_update BEFORE UPDATE ON last_wills
BEGIN SELECT RAISE(ABORT, 'last_wills: history cannot change'); END;
CREATE TRIGGER last_wills_no_delete BEFORE DELETE ON last_wills
BEGIN SELECT RAISE(ABORT, 'last_wills: history cannot change'); END;

CREATE TRIGGER approvals_request_fixed BEFORE UPDATE ON approvals
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.project_id IS NOT OLD.project_id OR NEW.created_at IS NOT OLD.created_at
    OR NEW.type IS NOT OLD.type OR NEW.title IS NOT OLD.title OR NEW.description IS NOT OLD.description
    OR NEW.payload IS NOT OLD.payload OR NEW.payload_sha256 IS NOT OLD.payload_sha256
    OR NEW.expected_cost IS NOT OLD.expected_cost OR NEW.expected_benefit IS NOT OLD.expected_benefit
BEGIN SELECT RAISE(ABORT, 'approvals: the request itself cannot change'); END;
CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
BEGIN SELECT RAISE(ABORT, 'approvals: rows cannot be deleted'); END;
CREATE TRIGGER messages_fixed BEFORE UPDATE ON messages
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.created_at IS NOT OLD.created_at OR NEW.sender IS NOT OLD.sender OR NEW.cycle_id IS NOT OLD.cycle_id
    OR NEW.text IS NOT OLD.text
BEGIN SELECT RAISE(ABORT, 'messages: a message cannot change'); END;
CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages: rows cannot be deleted'); END;
CREATE TRIGGER upgrades_request_fixed BEFORE UPDATE ON upgrades
WHEN NEW.mode IS NOT OLD.mode OR NEW.session IS NOT OLD.session OR NEW.life_id IS NOT OLD.life_id
    OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.created_at IS NOT OLD.created_at OR NEW.title IS NOT OLD.title
    OR NEW.problem IS NOT OLD.problem OR NEW.proposed_change IS NOT OLD.proposed_change
    OR NEW.expected_benefit IS NOT OLD.expected_benefit OR NEW.priority IS NOT OLD.priority
BEGIN SELECT RAISE(ABORT, 'upgrades: the request itself cannot change'); END;
CREATE TRIGGER upgrades_no_delete BEFORE DELETE ON upgrades
BEGIN SELECT RAISE(ABORT, 'upgrades: rows cannot be deleted'); END;
