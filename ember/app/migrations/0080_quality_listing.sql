-- 0.24.0: the quality critic checks each live listing of a product line, and keeps which one it judged (agent/quality.py).
-- It judged only a line's newest listing and kept no listing, so live, three verdicts on a €39 licence bundle were read
-- as verdicts on its line's cover letter listing for three days. A check made before now judged its project's newest
-- listing live by then (its own listing first, as the critic took it, then one Printify made): that one is filled in.
ALTER TABLE quality_checks ADD COLUMN listing_id INTEGER;

UPDATE quality_checks
SET listing_id = COALESCE(
    (
        SELECT l.listing_id FROM etsy_listings l
        JOIN approvals a ON a.id = l.approval_id LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE l.mode = quality_checks.mode AND l.session = quality_checks.session AND l.listing_id IS NOT NULL
            AND l.status = 'active' AND COALESCE(a.project_id, y.project_id) = quality_checks.project_id
            AND COALESCE(l.finished_at, l.started_at) <= quality_checks.created_at
        ORDER BY l.id DESC LIMIT 1
    ),
    (
        SELECT p.listing_id FROM printify_products p
        JOIN approvals a ON a.id = p.approval_id LEFT JOIN cycles y ON y.id = a.cycle_id
        WHERE p.mode = quality_checks.mode AND p.session = quality_checks.session AND p.listing_id IS NOT NULL
            AND p.status = 'active' AND COALESCE(a.project_id, y.project_id) = quality_checks.project_id
            AND COALESCE(p.finished_at, p.started_at) <= quality_checks.created_at
        ORDER BY p.id DESC LIMIT 1
    )
)
WHERE listing_id IS NULL;

CREATE INDEX quality_checks_by_listing ON quality_checks (mode, session, listing_id, id);
