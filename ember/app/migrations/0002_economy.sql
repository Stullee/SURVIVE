-- Phase 2: the economy. Amounts are integer micro-USD ("micros").
-- Status-like columns that later phases may extend are validated in code, not
-- with CHECK constraints, so they can grow without rebuilding tables. The ledger's
-- own rules (types, signs, who may write what) are stable and enforced here.

-- One life of the agent per row. A life ends when the agent dies (or, for a dry
-- run, when its test session ends); an owner grant can start a new life. The
-- current life of a mode is its newest row.
CREATE TABLE lives (
    id                   INTEGER PRIMARY KEY,
    mode                 TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    born_at              TEXT NOT NULL,
    -- Highest ledger id at birth: this life's own money movements have higher ids.
    born_mark            INTEGER NOT NULL DEFAULT 0,
    started_reason       TEXT NOT NULL,
    state                TEXT NOT NULL,
    state_reason         TEXT,
    ended_at             TEXT,
    end_reason           TEXT,
    -- Highest ledger id when the life ended / turned critical: money recorded later has a higher id.
    ended_mark           INTEGER,
    critical_since       TEXT,
    critical_mark        INTEGER,
    last_will_due        INTEGER NOT NULL DEFAULT 0 CHECK (last_will_due IN (0, 1)),
    last_will_at         TEXT,
    revived_from_life_id INTEGER REFERENCES lives (id),
    revived_by_ledger_id INTEGER REFERENCES ledger (id),
    CHECK ((ended_at IS NULL) = (end_reason IS NULL)),
    CHECK (ended_at IS NULL OR state IN ('dead', 'ended'))
);
CREATE INDEX lives_by_mode ON lives (mode, id);

-- A life's identity never changes, and an ended life is history.
CREATE TRIGGER lives_ended_is_final BEFORE UPDATE ON lives
WHEN OLD.ended_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'lives: an ended life cannot change');
END;
CREATE TRIGGER lives_identity_fixed BEFORE UPDATE ON lives
WHEN NEW.mode IS NOT OLD.mode OR NEW.born_at IS NOT OLD.born_at OR NEW.born_mark IS NOT OLD.born_mark
    OR NEW.revived_from_life_id IS NOT OLD.revived_from_life_id
    OR NEW.revived_by_ledger_id IS NOT OLD.revived_by_ledger_id
BEGIN
    SELECT RAISE(ABORT, 'lives: mode, birth and revival cannot change');
END;
CREATE TRIGGER lives_no_delete BEFORE DELETE ON lives
BEGIN
    SELECT RAISE(ABORT, 'lives: rows cannot be deleted');
END;

-- Every change of a life's state; runway uses it to measure active time.
CREATE TABLE life_transitions (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    life_id    INTEGER NOT NULL REFERENCES lives (id),
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    reason     TEXT NOT NULL
);
CREATE INDEX life_transitions_by_life ON life_transitions (life_id, id);
CREATE TRIGGER life_transitions_no_update BEFORE UPDATE ON life_transitions
BEGIN
    SELECT RAISE(ABORT, 'life_transitions: history cannot change');
END;
CREATE TRIGGER life_transitions_no_delete BEFORE DELETE ON life_transitions
BEGIN
    SELECT RAISE(ABORT, 'life_transitions: history cannot change');
END;

-- One wake cycle of the agent (the runner arrives in phase 3).
CREATE TABLE cycles (
    id         INTEGER PRIMARY KEY,
    life_id    INTEGER NOT NULL REFERENCES lives (id),
    boot_id    TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL,
    trigger    TEXT NOT NULL,
    simulated  INTEGER NOT NULL CHECK (simulated IN (0, 1)),
    cap_micros INTEGER NOT NULL CHECK (cap_micros >= 0),
    note       TEXT
);
CREATE UNIQUE INDEX one_running_cycle ON cycles (status) WHERE status = 'running';
CREATE INDEX cycles_by_life ON cycles (life_id);
CREATE TRIGGER cycles_no_delete BEFORE DELETE ON cycles
BEGIN
    SELECT RAISE(ABORT, 'cycles: rows cannot be deleted');
END;

-- Every model call: reserved by the budget guard, then finalized once.
CREATE TABLE llm_calls (
    id                    INTEGER PRIMARY KEY,
    boot_id               TEXT NOT NULL,
    cycle_id              INTEGER NOT NULL REFERENCES cycles (id),
    purpose               TEXT NOT NULL,
    model                 TEXT NOT NULL,
    response_model        TEXT,
    simulated             INTEGER NOT NULL CHECK (simulated IN (0, 1)),
    status                TEXT NOT NULL,
    ts                    TEXT NOT NULL,
    local_day             TEXT NOT NULL,
    finished_at           TEXT,
    estimate_micros       INTEGER NOT NULL DEFAULT 0 CHECK (estimate_micros >= 0),
    cost_micros           INTEGER NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    floor_micros          INTEGER NOT NULL DEFAULT 0 CHECK (floor_micros >= 0),
    billing_uncertain     INTEGER NOT NULL DEFAULT 0 CHECK (billing_uncertain IN (0, 1)),
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    web_search_requests   INTEGER NOT NULL DEFAULT 0,
    web_fetch_requests    INTEGER NOT NULL DEFAULT 0,
    stop_reason           TEXT,
    guard_reason          TEXT,
    error                 TEXT,
    request_id            TEXT,
    message_id            TEXT,
    service_tier          TEXT,
    inference_geo         TEXT,
    price_snapshot        TEXT CHECK (price_snapshot IS NULL OR json_valid(price_snapshot)),
    usage_raw             TEXT CHECK (usage_raw IS NULL OR json_valid(usage_raw)),
    plan                  TEXT CHECK (plan IS NULL OR json_valid(plan)),
    CHECK (floor_micros <= cost_micros)
);
CREATE INDEX llm_calls_by_status ON llm_calls (status);
CREATE INDEX llm_calls_by_cycle ON llm_calls (cycle_id);

-- A finalized call is history: only a pending reservation may change, and nothing is deleted.
CREATE TRIGGER llm_calls_finalize_once BEFORE UPDATE ON llm_calls
WHEN OLD.status <> 'pending'
BEGIN
    SELECT RAISE(ABORT, 'llm_calls: a finalized call cannot change');
END;
CREATE TRIGGER llm_calls_no_delete BEFORE DELETE ON llm_calls
BEGIN
    SELECT RAISE(ABORT, 'llm_calls: rows cannot be deleted');
END;

-- The money. Append-only: mistakes are fixed with correction rows.
--   owner_grant, revenue, expense   entered by the owner; a correction of one of
--                                   these has the same type, a negative amount and
--                                   corrects_id pointing at the original row
--   api_cost                        written by the budget guard for a model call
--   api_cost_correction             signed; positive means the calls cost more
--   adjustment                      signed; positive adds to the balance
-- occurred_on is the owner-local calendar day the money belongs to.
CREATE TABLE ledger (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    occurred_on     TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (
        type IN ('owner_grant', 'revenue', 'api_cost', 'api_cost_correction', 'expense', 'adjustment')
    ),
    amount_micros   INTEGER NOT NULL,
    simulated       INTEGER NOT NULL DEFAULT 0 CHECK (simulated IN (0, 1)),
    source          TEXT,
    note            TEXT,
    llm_call_id     INTEGER REFERENCES llm_calls (id),
    corrects_id     INTEGER REFERENCES ledger (id),
    orig_amount     TEXT,
    orig_currency   TEXT,
    fx_rate         TEXT,
    project_id      INTEGER,
    approval_id     INTEGER,
    created_by      TEXT NOT NULL CHECK (created_by IN ('owner', 'system')),
    entered_by      TEXT,
    idempotency_key TEXT UNIQUE,
    CHECK (
        (type IN ('owner_grant', 'revenue', 'expense')
            AND ((corrects_id IS NULL AND amount_micros > 0) OR (corrects_id IS NOT NULL AND amount_micros < 0)))
        OR (type = 'api_cost' AND amount_micros >= 0 AND corrects_id IS NULL)
        OR (type IN ('api_cost_correction', 'adjustment') AND amount_micros <> 0 AND corrects_id IS NULL)
    ),
    -- Revenue, grants and expenses only ever come from the owner.
    CHECK (type NOT IN ('owner_grant', 'revenue', 'expense') OR created_by = 'owner'),
    CHECK (type <> 'api_cost' OR (created_by = 'system' AND llm_call_id IS NOT NULL)),
    CHECK (llm_call_id IS NULL OR type IN ('api_cost', 'api_cost_correction')),
    CHECK (created_by = 'system' OR idempotency_key IS NOT NULL),
    -- Test money only exists for the dry run's own economy.
    CHECK (simulated = 0 OR type <> 'api_cost_correction'),
    CHECK (
        (orig_currency IS NULL AND orig_amount IS NULL AND fx_rate IS NULL)
        OR (orig_currency = 'EUR' AND orig_amount IS NOT NULL AND fx_rate IS NOT NULL)
    )
);
CREATE UNIQUE INDEX ledger_one_cost_per_call ON ledger (llm_call_id) WHERE type = 'api_cost';
CREATE INDEX ledger_by_day ON ledger (occurred_on);
CREATE INDEX ledger_by_ts ON ledger (ts);
CREATE INDEX ledger_by_corrects ON ledger (corrects_id) WHERE corrects_id IS NOT NULL;

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger: rows are append-only');
END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger: rows are append-only');
END;

-- An API cost must be exactly what its finalized call cost, in the same mode and on its day.
CREATE TRIGGER ledger_api_cost_matches_call BEFORE INSERT ON ledger
WHEN NEW.type = 'api_cost'
BEGIN
    SELECT RAISE(ABORT, 'ledger: api_cost does not match its llm_calls row')
    WHERE NOT EXISTS (
        SELECT 1 FROM llm_calls c
        WHERE c.id = NEW.llm_call_id
          AND c.simulated = NEW.simulated
          AND c.cost_micros = NEW.amount_micros
          AND c.local_day = NEW.occurred_on
          AND c.status IN ('ok', 'interrupted')
    );
END;

-- A correction reduces one original row of the same type and mode, never below zero.
CREATE TRIGGER ledger_correction_fits BEFORE INSERT ON ledger
WHEN NEW.corrects_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'ledger: a correction must reduce an original entry of the same type')
    WHERE NOT EXISTS (
        SELECT 1 FROM ledger o
        WHERE o.id = NEW.corrects_id
          AND o.corrects_id IS NULL
          AND o.type = NEW.type
          AND o.simulated = NEW.simulated
    );
    SELECT RAISE(ABORT, 'ledger: corrections would exceed the original amount')
    WHERE (SELECT o.amount_micros FROM ledger o WHERE o.id = NEW.corrects_id)
        + (SELECT COALESCE(SUM(k.amount_micros), 0) FROM ledger k WHERE k.corrects_id = NEW.corrects_id)
        + NEW.amount_micros < 0;
END;
