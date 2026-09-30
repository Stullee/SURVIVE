-- 0.12.0: revenue from Etsy's own numbers. Revenue, grants and expenses only ever came from the owner, so a sale
-- counted nowhere (the balance, the runway, the money goal, a venture's earnings) until the owner recorded it by hand.
-- Now, once the owner turns on etsy_auto_record_revenue, Ember's code records the revenue of paid orders with Ember's
-- listings, Etsy's fees on them and the refunds of that revenue, as entries made by 'etsy': with the same request
-- keys as the owner's buttons, so no order is recorded twice, whoever records it first. Grants stay the owner's alone,
-- and Etsy's numbers correct only the entries they made: an entry the owner made stays the owner's.
--
-- The ledger is rebuilt (SQLite can't change a CHECK) with its rows and their ids; its indexes and triggers stay as
-- they were.
CREATE TABLE ledger_new (
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
    created_by      TEXT NOT NULL CHECK (created_by IN ('owner', 'system', 'etsy')),
    entered_by      TEXT,
    idempotency_key TEXT UNIQUE,
    venture_id      INTEGER REFERENCES ventures (id),
    CHECK (
        (type IN ('owner_grant', 'revenue', 'expense')
            AND ((corrects_id IS NULL AND amount_micros > 0) OR (corrects_id IS NOT NULL AND amount_micros < 0)))
        OR (type = 'api_cost' AND amount_micros >= 0 AND corrects_id IS NULL)
        OR (type IN ('api_cost_correction', 'adjustment') AND amount_micros <> 0 AND corrects_id IS NULL)
    ),
    -- Grants come only from the owner; revenue and expenses from the owner or, new, from Etsy's numbers.
    CHECK (
        type NOT IN ('owner_grant', 'revenue', 'expense') OR created_by = 'owner'
        OR (created_by = 'etsy' AND type IN ('revenue', 'expense'))
    ),
    CHECK (created_by <> 'etsy' OR type IN ('revenue', 'expense')),
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
INSERT INTO ledger_new (
    id, ts, occurred_on, type, amount_micros, simulated, source, note, llm_call_id, corrects_id, orig_amount,
    orig_currency, fx_rate, project_id, approval_id, created_by, entered_by, idempotency_key, venture_id
)
SELECT
    id, ts, occurred_on, type, amount_micros, simulated, source, note, llm_call_id, corrects_id, orig_amount,
    orig_currency, fx_rate, project_id, approval_id, created_by, entered_by, idempotency_key, venture_id
FROM ledger;
DROP TABLE ledger;
ALTER TABLE ledger_new RENAME TO ledger;

CREATE UNIQUE INDEX ledger_one_cost_per_call ON ledger (llm_call_id) WHERE type = 'api_cost';
CREATE INDEX ledger_by_day ON ledger (occurred_on);
CREATE INDEX ledger_by_ts ON ledger (ts);
CREATE INDEX ledger_by_corrects ON ledger (corrects_id) WHERE corrects_id IS NOT NULL;
CREATE INDEX ledger_by_project ON ledger (project_id) WHERE project_id IS NOT NULL;
CREATE INDEX ledger_by_venture ON ledger (venture_id) WHERE venture_id IS NOT NULL;

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger: rows are append-only');
END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger: rows are append-only');
END;

-- (0002) An API cost must be exactly what its finalized call cost, in the same mode and on its day.
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

-- (0002) A correction reduces one original row of the same type and mode, never below zero.
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

-- (0016) Only revenue and expenses belong to a project or venture.
CREATE TRIGGER ledger_attribution_fits BEFORE INSERT ON ledger
WHEN (NEW.project_id IS NOT NULL OR NEW.venture_id IS NOT NULL) AND NEW.type NOT IN ('revenue', 'expense')
BEGIN
    SELECT RAISE(ABORT, 'ledger: only revenue and expenses belong to a project or venture');
END;

-- New: Etsy's numbers correct only the entries they made.
CREATE TRIGGER ledger_etsy_corrects_its_own BEFORE INSERT ON ledger
WHEN NEW.created_by = 'etsy' AND NEW.corrects_id IS NOT NULL
    AND (SELECT o.created_by FROM ledger o WHERE o.id = NEW.corrects_id) IS NOT 'etsy'
BEGIN
    SELECT RAISE(ABORT, 'ledger: Etsy''s numbers correct only the entries they made');
END;
