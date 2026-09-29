-- 0.12.0: an Etsy order's status (paid, completed, partially refunded, fully refunded, canceled), kept current by
-- every sync, which now fetches the receipts changed lately rather than those created lately. From 0.12.0 on, an
-- order's total is only Ember's lines, net of tax, shipping, the coupon and refunds: the whole receipt was stored
-- (tax, shipping and the owner's own products included) and a refunded order stayed counted. An order from before
-- keeps its status empty: its total is the whole receipt's.
ALTER TABLE etsy_orders ADD COLUMN status TEXT CHECK (status IS NULL OR length(status) <= 30);
