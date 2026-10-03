-- 0.18.1: the quality critic judged print-on-demand product lines without their listing (only Etsy's own listings were
-- read), and live it scored one 3/10 because "no title, photo, tags, price or description was provided". Such checks
-- no longer count: they are marked failed, so the next cycle checks the product line again with its Printify product.
UPDATE quality_checks SET status = 'failed', note = '0.18.1: judged without its listing (a Printify product line)'
WHERE status = 'ok' AND project_id NOT IN (  -- the projects with a listing of Ember's own (agent/metrics.py listings)
    SELECT COALESCE(a.project_id, y.project_id) FROM etsy_listings l
    JOIN approvals a ON a.id = l.approval_id
    LEFT JOIN cycles y ON y.id = a.cycle_id
    WHERE l.listing_id IS NOT NULL AND COALESCE(a.project_id, y.project_id) IS NOT NULL
);
