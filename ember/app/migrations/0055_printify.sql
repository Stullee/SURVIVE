-- 0.13.0 (Phase E4, the owner's request): Printify (integrations/printify.py). The agent proposes a physical product
-- with its design, made on order by a print provider; the owner approves; Ember's code creates it at Printify and
-- publishes it to the owner's Etsy shop through Printify's Etsy connection, if every price keeps its margin.
--
-- No approvals rebuild (0054 made the executor any short name): only the NEVER view learns the new executor. The view
-- and the triggers that read it are dropped and made again, unchanged but for Printify.
DROP TRIGGER approvals_never_on_unlock;
DROP TRIGGER policy_uses_never;
DROP VIEW approvals_never;

-- What Ember's code keeps of Printify's catalog (per mode, not per session: it is Printify's), so a proposal is
-- checked without reaching Printify: the products (blueprints), a product's print providers, and the variants a
-- provider makes (with their print area and the shipping to Germany).
CREATE TABLE printify_catalog (
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    kind       TEXT NOT NULL CHECK (kind IN ('blueprints', 'providers', 'variants')),
    key        TEXT NOT NULL CHECK (length(key) <= 40),
    data       TEXT NOT NULL CHECK (json_valid(data) AND length(data) <= 2000000),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (mode, kind, key)
);

-- Ember's products (like etsy_listings: a row committed as 'running' before anything is sent, so a crash never makes
-- one twice; 'unclear' when it can't be known what Printify made). 'publishing': published at Printify, its Etsy
-- listing not known yet; 'active': the listing is known (listing_id, whose views and favorites Etsy's sync keeps).
CREATE TABLE printify_products (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session     INTEGER NOT NULL,
    approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals (id),
    product_id  TEXT CHECK (product_id IS NULL OR length(product_id) <= 40),  -- Printify's, once it exists
    listing_id  INTEGER,  -- the Etsy listing Printify made
    title       TEXT NOT NULL CHECK (length(title) <= 140),
    currency    TEXT NOT NULL CHECK (length(currency) = 3),
    prices      TEXT CHECK (prices IS NULL OR (json_valid(prices) AND length(prices) <= 4000)),
    status      TEXT NOT NULL CHECK (status IN ('running', 'publishing', 'active', 'deleted', 'failed', 'unclear')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    state       TEXT CHECK (state IS NULL OR length(state) <= 20),  -- the listing's at Etsy, at the last sync
    views       INTEGER,
    favorites   INTEGER,
    synced_at   TEXT,
    result      TEXT CHECK (result IS NULL OR length(result) <= 1000),
    error       TEXT CHECK (error IS NULL OR length(error) <= 500),
    CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX printify_products_by_scope ON printify_products (mode, session, id);
CREATE TRIGGER printify_products_no_delete BEFORE DELETE ON printify_products
BEGIN SELECT RAISE(ABORT, 'printify_products: rows cannot be deleted'); END;

-- The Printify orders of Ember's products: what making and shipping them costs the owner at Printify.
CREATE TABLE printify_orders (
    id             INTEGER PRIMARY KEY,
    mode           TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    session        INTEGER NOT NULL,
    order_id       TEXT NOT NULL CHECK (length(order_id) BETWEEN 1 AND 40),
    product_id     TEXT NOT NULL CHECK (length(product_id) BETWEEN 1 AND 40),
    quantity       INTEGER NOT NULL CHECK (quantity >= 0),
    cost_cents     INTEGER NOT NULL CHECK (cost_cents >= 0),
    shipping_cents INTEGER NOT NULL CHECK (shipping_cents >= 0),
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    status         TEXT NOT NULL CHECK (length(status) <= 40),
    created_at     TEXT NOT NULL,
    synced_at      TEXT NOT NULL,
    UNIQUE (mode, session, order_id, product_id)
);
CREATE TRIGGER printify_orders_no_delete BEFORE DELETE ON printify_orders
BEGIN SELECT RAISE(ABORT, 'printify_orders: rows cannot be deleted'); END;

-- The seeded print-on-demand venture is the channel's (agent/stages.py: its first test is a first order).
UPDATE ventures SET channel = 'printify' WHERE title = 'Print on demand in the Etsy shop' AND created_by = 'owner';

-- NEVER (0051, 0054), again: Ember's first Printify product is a new kind of business for the owner (physical goods,
-- with duties of their own), and no rule of the owner's unlocks covers it.
CREATE VIEW approvals_never AS
SELECT
    approval_id, account, money, first_contact, first_publication, community_post, legal, owner_only,
    account OR money OR first_contact OR first_publication OR community_post OR legal OR owner_only AS never
FROM (
    SELECT
        r.id AS approval_id,
        r.type = 'create_account' AS account,
        r.type = 'spend_money' AS money,
        r.executor IS 'email' AND (
            json_type(r.action, '$.to') IS NOT 'text' OR json_extract(r.action, '$.to') = '' OR NOT EXISTS (
                SELECT 1 FROM emails e WHERE e.mode = r.mode AND e.session = r.session AND e.direction = 'in'
                    AND lower(e.from_addr) = lower(json_extract(r.action, '$.to'))
            )
        ) AS first_contact,
        (r.executor IS 'etsy_listing' AND NOT EXISTS (
            SELECT 1 FROM etsy_listings l WHERE l.mode = r.mode AND l.session = r.session AND l.status = 'active'
        )) OR (r.executor IS 'pinterest_pin' AND NOT EXISTS (
            SELECT 1 FROM pinterest_boards b WHERE b.mode = r.mode AND b.session = r.session AND b.status = 'active'
        )) OR (r.executor IS 'printify_product' AND NOT EXISTS (
            SELECT 1 FROM printify_products p WHERE p.mode = r.mode AND p.session = r.session
                AND p.status IN ('publishing', 'active')
        )) AS first_publication,
        r.executor IS 'reddit_link' AS community_post,
        r.words GLOB '*steuer*' OR r.words GLOB '*gewerbe*' OR r.words GLOB '*finanzamt*' OR r.words GLOB '*vertrag*'
            OR r.words GLOB '*verträg*' OR r.words GLOB '*vertrÄg*' OR r.words GLOB '*[^a-z]tax[^a-z]*'
            OR r.words GLOB '*[^a-z]taxes[^a-z]*' OR r.words GLOB '*[^a-z]taxed[^a-z]*' OR r.words GLOB '*[^a-z]vat[^a-z]*'
            OR r.words GLOB '*[^a-z]ust[^a-z]*' OR r.words GLOB '*[^a-z]mwst[^a-z]*'
            OR r.words GLOB '*[^a-z]contract[^a-z]*' OR r.words GLOB '*[^a-z]contracts[^a-z]*' AS legal,
        r.executor IS NULL OR r.executor NOT IN (
            'email', 'reddit_link', 'etsy_listing', 'etsy_edit', 'pinterest_pin', 'printify_product'
        ) AS owner_only
    FROM (
        SELECT id, mode, session, type, executor, action,
            ' ' || lower(title || ' ' || description || ' ' || payload) || ' ' AS words
        FROM approvals
    ) r
);
CREATE TRIGGER approvals_never_on_unlock BEFORE UPDATE OF status ON approvals
WHEN NEW.status IN ('approved', 'approved_with_changes')
    AND NEW.decided_by IN ('Ember''s code (your unlock)', 'Ember''s code', 'Ember')
    AND (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'approvals: an unlock never approves this request (NEVER)'); END;
CREATE TRIGGER policy_uses_never BEFORE INSERT ON policy_uses
WHEN (SELECT n.never FROM approvals_never n WHERE n.approval_id = NEW.approval_id)
BEGIN SELECT RAISE(ABORT, 'policy_uses: an unlock never carries this request (NEVER)'); END;
