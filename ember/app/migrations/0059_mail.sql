-- 0.14.0: only a person's email counts as someone having written to Ember. Anyone can put any address in From:, so
-- a forged or a machine's email made its sender "someone who wrote": an email to them was no first contact for NEVER,
-- and it became an inquiry that woke the agent. Each email now keeps the verdict of the receiving mail provider
-- (authenticated: the topmost Authentication-Results header says dmarc, or dkim or spf of the From: domain, pass;
-- integrations/mail.py). NULL for mail stored before, which never counts. bulk now also marks a machine's mail
-- (bounces, no-reply senders, delivery reports) and mail from Ember's own address.
ALTER TABLE emails ADD COLUMN authenticated INTEGER CHECK (authenticated IS NULL OR authenticated IN (0, 1));

-- Both are part of NEVER's guarantee below, so, like the rest of a stored email (emails_content_fixed), they never
-- change once stored.
CREATE TRIGGER emails_verdict_fixed BEFORE UPDATE OF bulk, authenticated ON emails
WHEN NEW.bulk IS NOT OLD.bulk OR NEW.authenticated IS NOT OLD.authenticated
BEGIN SELECT RAISE(ABORT, 'emails: a stored email cannot change'); END;

-- NEVER (0051, 0054, 0055), again: a first contact is an email to an address no person's email came from
-- (mailstore.person: received, authenticated = 1, bulk = 0). The view and the triggers that read it are dropped and
-- made again, unchanged but for that.
DROP TRIGGER approvals_never_on_unlock;
DROP TRIGGER policy_uses_never;
DROP VIEW approvals_never;
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
                SELECT 1 FROM emails e WHERE e.mode = r.mode AND e.session = r.session
                    AND (e.direction = 'in' AND e.authenticated = 1 AND e.bulk = 0)
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
