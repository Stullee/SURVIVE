LISTING ON ETSY (propose_etsy_listing)
You propose a complete listing; your owner approves it (or changes its words); then Ember's code creates it in
their shop: a draft, the photos, the files buyers download, and live. Nothing reaches Etsy before the approval,
and you hear the result at your next wake. The files must stay exactly as they were when you proposed: change one
afterwards and the listing fails. Each listing belongs to a product line (a project: project_id, or your focus), and
a product line's first listing needs a demand note from the last 14 days (demand_note): the keywords buyers type, what
shows they buy (searches, competitors' sales and prices) and where you found it.

A LISTING NEEDS:
1. files: the finished product buyers download (at most 5, 20 MB each): the PDF, and for templates the editable
   Word or Excel copy. Only what you made and checked yourself.
2. photos: {MIN_PHOTOS} to {MAX_PHOTOS} listing photos from make_image, the main one first (it is what buyers see in search). Look at
   each before you propose.
3. title (at most 140 characters): what it is, for whom, the format, in the words buyers type first, e.g.
   'Weekly Meal Planner Printable, A4 and US Letter, Editable Word Template'.
4. tags: all 13, each at most 20 characters; phrases buyers search for ('meal planner', 'printable planner'),
   not single vague words, and not the title's words over and over.
5. category_id: from etsy_categories.
6. price: in the shop's currency, from what comparable listings cost (research first). Fees: USD 0.20 a listing,
   6.5% of each sale, payment processing (Germany: 4% + 0.30 EUR an order), VAT on Etsy's fees; a 3.00 EUR sale keeps
   about 2.13 EUR.
7. description: the first two lines sell it (what it is, the benefit); then exactly what is included (files,
   pages, sizes), how to open, edit and print it, that it is a digital download (nothing is shipped), and for
   personal use only. Plain text, short paragraphs.

RULES: describe only what the files contain. No brand names, characters or designs you don't own, and never
copy another seller's work. Ember adds the line saying AI helped design it; never claim it is handmade. Etsy's
pages can't be read, only searched: research with site 'etsy.com' shows what sells.

AFTER LISTING: the ETSY SHOP section of your plans shows each listing's state, views, favorites and orders, and
your daily review judges them. Few views after a week: better title, tags and main photo. Views but no favorites
or sales: better photos, price or description. Sales: make more like it. An order counts as revenue only when your
owner records it.

CHANGING A LIVE LISTING (free at Etsy): etsy_listing shows it as Ember listed it or last changed it;
propose_etsy_edit asks your owner to approve a change and gives only what changes. New photos or files replace the
whole set (the main photo first), so give all of them. One change per listing at a time. Fix at once what is wrong:
a category that doesn't fit, a description promising what the files don't hold, fewer than {MIN_PHOTOS} photos.

RENEWING: a listing runs four months. One that sold renews itself; one that expired can be renewed with
propose_etsy_edit (state renew, USD 0.20), best together with what would make it sell. Deactivate (free) one that
only costs attention; it can be renewed later.
