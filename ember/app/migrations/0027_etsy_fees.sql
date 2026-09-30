-- 0.12.0: Ember's share of Etsy's fees on an order, in cents: its share of the payment's processing fee (read from the
-- order's payment) and Etsy's transaction fee on what its lines earned. NULL until the payment was read. The owner
-- records it as an expense next to the order's revenue (Etsy's fees were never booked).
ALTER TABLE etsy_orders ADD COLUMN fees_cents INTEGER CHECK (fees_cents IS NULL OR fees_cents >= 0);
