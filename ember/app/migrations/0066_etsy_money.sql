-- 0.15.0: when the Etsy listing Printify made of one of Ember's products ends, and whether it renews itself (as
-- etsy_listings keeps them since 0026), so that the sync sees Etsy's renewals of it and books their listing fees, and
-- counts it as expired once its end passed.
ALTER TABLE printify_products ADD COLUMN ends_at TEXT;
ALTER TABLE printify_products ADD COLUMN auto_renew INTEGER CHECK (auto_renew IS NULL OR auto_renew IN (0, 1));
