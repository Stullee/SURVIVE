-- 0.25.1: a Bluesky post's second link (propose_bluesky_post's link takes two addresses), kept as its link is: another
-- of Ember's live Etsy listings or a page of the owner's website, shown in the words. The reach of the product line it
-- links counts the post (agent/reach.py), as it counts the link's.
ALTER TABLE bluesky_posts ADD COLUMN second_link TEXT CHECK (second_link IS NULL OR length(second_link) <= 500);
