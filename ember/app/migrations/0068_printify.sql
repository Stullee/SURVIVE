-- 0.15.0: Printify's money records and metrics (integrations/printify_publisher.py).
--
-- What an order costs the owner at Printify includes the tax Printify bills on it (its share for each of Ember's lines).
ALTER TABLE printify_orders ADD COLUMN tax_cents INTEGER NOT NULL DEFAULT 0 CHECK (tax_cents >= 0);

-- The catalog also keeps what each variant costs to make ('costs', by product and provider), read from the products
-- Ember's code creates and from a cost probe, so the agent prices a product before it proposes one. The catalog is a
-- copy of Printify's: the variants are read again, now with the currency Printify states for them.
CREATE TABLE printify_catalog_new (
    mode       TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    kind       TEXT NOT NULL CHECK (kind IN ('blueprints', 'providers', 'variants', 'costs')),
    key        TEXT NOT NULL CHECK (length(key) <= 40),
    data       TEXT NOT NULL CHECK (json_valid(data) AND length(data) <= 2000000),
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (mode, kind, key)
);
INSERT INTO printify_catalog_new (mode, kind, key, data, fetched_at)
SELECT mode, kind, key, data, fetched_at FROM printify_catalog WHERE kind <> 'variants';
DROP TABLE printify_catalog;
ALTER TABLE printify_catalog_new RENAME TO printify_catalog;

-- A product line that sells in the Etsy shop belongs to a venture (agent/ventures.py, adopt): an open project without
-- one joins the venture its requests worked for, or else the channel's, never a parked or killed one. Printify's
-- products go to the print-on-demand venture, Etsy's listings to the Etsy leg. Their sales counted for no venture.
UPDATE projects SET venture_id = COALESCE(
    (
        SELECT v.id FROM approvals a JOIN ventures v ON v.id = a.venture_id
        LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE COALESCE(a.project_id, y.project_id) = projects.id
            AND a.executor IN ('printify_product', 'etsy_listing', 'etsy_edit')
            AND v.stage NOT IN ('parked', 'killed')
        ORDER BY a.id DESC LIMIT 1
    ),
    (
        SELECT v.id FROM ventures v
        WHERE v.mode = projects.mode AND v.session = projects.session AND v.channel = 'printify'
            AND v.stage NOT IN ('parked', 'killed')
        ORDER BY v.id LIMIT 1
    )
)
WHERE venture_id IS NULL AND status IN ('idea', 'active', 'waiting')
    AND id IN (
        SELECT COALESCE(a.project_id, y.project_id) FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE a.executor = 'printify_product' AND COALESCE(a.project_id, y.project_id) IS NOT NULL
    )
    AND id NOT IN (
        SELECT COALESCE(a.project_id, y.project_id) FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE a.executor IN ('etsy_listing', 'etsy_edit') AND COALESCE(a.project_id, y.project_id) IS NOT NULL
    );
UPDATE projects SET venture_id = COALESCE(
    (
        SELECT v.id FROM approvals a JOIN ventures v ON v.id = a.venture_id
        LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE COALESCE(a.project_id, y.project_id) = projects.id
            AND a.executor IN ('printify_product', 'etsy_listing', 'etsy_edit')
            AND v.stage NOT IN ('parked', 'killed')
        ORDER BY a.id DESC LIMIT 1
    ),
    (
        SELECT v.id FROM ventures v
        WHERE v.mode = projects.mode AND v.session = projects.session AND v.title = 'Etsy digital products'
            AND v.parent_id IS NULL AND v.stage NOT IN ('parked', 'killed')
        ORDER BY v.id LIMIT 1
    )
)
WHERE venture_id IS NULL AND status IN ('idea', 'active', 'waiting')
    AND id IN (
        SELECT COALESCE(a.project_id, y.project_id) FROM approvals a LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE a.executor IN ('etsy_listing', 'etsy_edit') AND COALESCE(a.project_id, y.project_id) IS NOT NULL
    );
