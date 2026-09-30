-- 0.12.0: when each listing ends at Etsy and whether Etsy renews it, as the last sync read them (listings were
-- created without automatic renewal and expired unseen after four months), and when Ember's code turned automatic
-- renewal on for a listing that sold: once, so the owner's own choice at Etsy stands after it.
ALTER TABLE etsy_listings ADD COLUMN ends_at TEXT;
ALTER TABLE etsy_listings ADD COLUMN auto_renew INTEGER CHECK (auto_renew IS NULL OR auto_renew IN (0, 1));
ALTER TABLE etsy_listings ADD COLUMN renew_set_at TEXT;
