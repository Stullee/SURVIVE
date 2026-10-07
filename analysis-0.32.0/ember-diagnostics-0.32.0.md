# Ember 0.32.0: what the diagnostics of 2026-10-07 show

*Your diagnostics report of 2026-10-07 15:43 UTC (version 0.31.0, cycles #118 to #129, 2026-10-06 11:53 to 2026-10-07
14:45 UTC), read against `fe16cd2` (0.31.0, the head of the tracked branch). The fixes are released as 0.32.0.*

**Evidence.** I read the whole report (6,658 lines):
- the 12 newest cycles, with every call, reply and tool;
- the planner's context as the next plan would see it;
- the records: projects, ventures, milestones, approvals, messages, bets, cases, playbook, listings, posts, pins,
  Printify products and the workspace's text files;
- the integrations, the meta keys and the newest 200 events.

I checked every finding against the code. Labels:
- **live**: it is in your report.
- **code**: found by reading the code.
- **reproduced**: a test or script against the real code showed it.

Each fix has a regression test in `ember/tests/test_fixes_0320.py`. The 10 tests for the live findings fail on 0.31.0
and pass on 0.32.0; an 11th guards the new release-notes code when an older version is restored. The full suite and
ruff are green.

**Paths** are relative to `ember/app/` unless they start with `tests/` or the repository root.

---

## 1. The short answer

- **The business has no traction yet.** 10 views on 9 listings in 9 days, no favorites, no orders and $0 revenue.
  $45.32 of your $200 is spent, at about $5 a day: 31 days of runway. Ember's own daily review names the bottleneck
  correctly: reach. Code can't fix that (section 4).
- **One tool call in five was refused**: 52 of 279 in the 12 cycles. Most were Ember's code's fault, not the agent's:
  - listings it couldn't link;
  - a length limit 19 characters stricter than needed;
  - a refusal that threw away good work, so the agent sent it again and again;
  - writes that ran out halfway through a file and left it broken.
  
  0.32.0 removes 11 of the 52 outright and makes most of the rest rarer (section 2).
- **Ember sent you Pinterest steps from its research as facts, and two didn't hold** (messages #150 and #152, your
  reply #153). Code can't check another site's menus; section 3 says what to do.
- **Its release notes restarted with every update.** 18 versions in two days meant Ember never read what changed in
  0.21 to 0.28; it read the newest 2 KB again and again. Fixed.

---

## 2. Findings and what 0.32.0 does

### 2.1 Posts and pins couldn't link the posters Printify made: live, code, reproduced

**What happened.** Three Bluesky proposals linking the two Bauhaus posters were refused with "#4587046876 isn't one of
your live listings" (cycle #123 tool #2060; cycle #126 tools #2122 and #2125). In the same cycles, `etsy_listing`
showed both posters live with views.

**Why.** Every check of a post's or a pin's listing read only Ember's own listings (`etsy_listings`):
- `_post_link` and `_propose_pin` in `agent/tools.py`;
- `_link` in `integrations/bluesky_publisher.py` and `_listing` in `integrations/pinterest_publisher.py`.

The posters are in `printify_products`, which the Etsy sync keeps alike (state, end, views:
`etsy_publisher.LISTING_TABLES`).

**What it cost.** Line #8 (the print-on-demand venture you backed) got one reach action in a week. The marketing
READY put it first for exactly that reason, and the marketing cycle it got couldn't link its listings, so it linked
the blog instead.

**0.32.0.** A post or a pin may link a listing Printify made of Ember's products, checked live when it is proposed and
again when Ember's code carries it out (`etsy_publisher.shop_listing_row`, `printify_publisher.live_listing`).
- Without a picture, its card shows the product's title only: Printify keeps the photos, and they aren't Ember's
  files. The Bluesky guide tells the agent to show its design as the picture.
- A line whose listings are all Printify's is marketable when the blog, Bluesky or Pinterest is on (it needed the blog
  before): `lines.marketing`'s `printify_links`, `loop._printify_links`.

### 2.2 English posts were held to the German post's room: live, code

7 of 19 Bluesky proposals were refused for their length. The limit on the words was 239 characters for every post:
300, less the longest AI line (the German one, 59 characters), less a line break.
- An English post's AI line is 40 characters, so its words have 258.
- The refused English posts at 244, 248, 249 and 253 characters would have fitted.
- You asked for English posts only (#144), so every post is held to the wrong limit now.

**0.32.0.**
- The field takes 258 characters (`bluesky.WORDS_CHARS`).
- A German post's words beyond its 239 are refused with how many to cut; the refusal no longer speaks of a link when
  none is in the words.
- The Bluesky guide gives both numbers.

### 2.3 A refused venture proposal threw its case away: live, code, reproduced

`venture_update` refused 10 of its 15 calls, in two venture cycles:
- cycle #118: 4 refusals;
- cycle #128: 6 refusals, and it ended with "the conversation got too long".

Two things made it slow:
- **A refusal saved nothing.** Scores sent before research, or a stage the venture couldn't take yet, raised before
  anything was saved, so the demand, economics, setup and risk texts sent with them were lost. The agent noticed
  ("the case needs demand/economics/etc fields re-filled since the earlier error meant they weren't saved") and sent
  them again, into the 3,000-character limit of one call.
- **The gate named one requirement at a time.** First the missing research, then the business case's numbers, then
  "its scores from research", then the knock-out "no independent source for its demand", which was only checked once
  everything else was there.

**0.32.0** (`_venture_update`, `_new_stage`):
- What is refused is "Not done", and the rest of the call is saved. A call with nothing else in it is refused as
  before. (`project_update`'s bet has worked this way since 0.24.0.)
- A refused proposal names its gaps and the knock-outs that stand together.

### 2.4 Rewriting a file in parts ran out of writes and left it cut off: live, code

**What happened.** The Budget Planner's Summary had formulas on the wrong rows.
- Cycle #125 fixed both bilingual JSON specs (3.6 and 3.9 KB). Each needed 2 or 3 writes: one write holds 2,500
  characters, because a work step's reply holds 2,000 tokens.
- After a first build, the cycle rewrote both again. The 11th write hit the cap of 10 a cycle, and both files were
  left without their Year Overview sheet and closing brackets.
- Cycle #129 made 13 write calls, in 9 of its 20 steps, to rewrite them in parts. It still couldn't rebuild the
  German file: that would have been a 4th `make_spreadsheet`, and a cycle makes 3.
- The same thing had happened before: `drafts/nebenkosten_de.json` (2026-09-29) is cut off mid-file too.
- The KDP interior is 24 KB: any fix to it meant 10 parts.

**0.32.0.** `workspace_write` has a mode `edit`: `find` (a passage the file holds once, exactly) becomes `content`.
- A formula's fix is one small call, however long the file is.
- A passage that isn't there, or is there with other spaces, or is there more than once, is refused with which of
  these it is.
- The spreadsheet guide tells the agent to fix specs this way, and that a cycle makes 3 spreadsheets.

### 2.5 A formula that pointed at the header row went unnoticed: live, code, reproduced

In cycle #129 the Summary's Net was `=B3-B4` in its row 6, while its data were rows 4 to 6. B3 is the header
("Amount"). `make_spreadsheet` reported nothing, and the agent found it only by reading the built file.

The file your buyers have had since 2026-09-29 has a related fault: its Summary sums Income!C2:C9 and Expenses!C2:C21
while the data are rows 4 to 12 and 4 to 26. A buyer's entries in the rows below aren't counted. 0.19.2's check finds
those ranges, which is why Ember is fixing the files now.

**0.32.0.** `make_spreadsheet`'s Check line also names a formula's single cell that is a sheet's title, the empty row
under it, its header, or an empty cell below its data and total, on its own sheet or another
(`sheets._outside_cells`). I ran it on all of Ember's spreadsheet specs in your report:
- it finds the live mistake;
- it finds nothing in the fixed spec or the job trackers.

### 2.6 The release notes started again with every version: live, code, reproduced

**What happened.** Ember read new release notes 2 KB a plan, newest first. Its place in them was keyed to the
version it was reading toward.
- Each update started the notes again from the newest. Since 2026-10-05 there were 17 updates in 23 cycles.
- Ember read the top of the newest notes again and again. Its unread notes were 30 KB, 16 plans' worth, and it never
  got to those of 0.21 to 0.28 (23 KB), where cycles on one product line and marketing cycles came in (0.28.0).
- In your report: `changelog_seen` has stood at 0.20.1 since 2026-10-05, and `changelog_at` is
  `0.20.1>0.31.0@3658`.

**0.32.0.** Ember's code keeps how much of each version's notes was shown (`news.changelog_shown_key`). A version
installed while Ember reads adds its notes first, then the rest goes on where it stopped, marked "(continued)".
Nothing is shown twice.

Ember's place in the notes was kept in the old format, so it starts again once, from 0.32.0 down to 0.21.0: about 17
plans.

### 2.7 Smaller frictions: live, code

| What happened | 0.32.0 |
|---|---|
| `obligation_done` named one of your messages that Ember had just answered, in 4 of the 12 cycles: 4 of its 6 calls were refused. OBLIGATIONS lists your waiting messages beside the obligations it closes. | Naming an answered message says there is nothing to close, without an error. The OBLIGATIONS line says that a message naming it in answers closes it. |
| Promises #32 "Propose the Haushaltsbuch 2027 KDP book" and #33 "Send the Haushaltsbuch 2027 KDP proposal" both stand, due 2026-10-10. 0.24.0's check didn't see the repeat, because "propose" and "proposal" were two words to it. | Words are compared by their first six letters; numbers stay whole, and two promises naming different references (#…) are never one, so two listings' reports stay two promises (`obligations.repeated_promise`). |
| The caps of `make_spreadsheet` (3 a cycle), `propose_pin` and `propose_bluesky_post` (2 each) were met only in their refusals, mid-plan. The workers' fixed prompt is at its byte bound, so the tools can't say them. | Their guides say them, from Ember's code's numbers (`{CAP:tool}` in `guide_text`). |
| A `venture_case` call that was cut off was told to "write a long file in parts". | A cut-off call that writes no file hears to send it again in a reply of its own (`loop.cut_call`). |
| FOCUS asked for a bet on every live line without one, so cycle #129 bet twice on a file fix and was refused twice. | It says a change that brings no views, favorites or orders (a fix) needs none. |

**The fixed prompt.** A worker's fixed prompt (system text and tool definitions) has a bound of 50,510 bytes, and
0.31.0 was exactly at it. The `edit` field took 102 bytes. I found them where a tool's description repeated what its
field says:
- `propose_bluesky_post`, what a link may be;
- `propose_pin`, which listing it links to;
- `look`, "with your own eyes".

0.32.0 is at 50,506 bytes.

---

## 3. What you should do

1. **Approve the Budget Planner file change when it comes.** Ember's next ordinary cycle on line #6 rebuilds the
   German file and proposes replacing both files of listing #4584938666. Until then, buyers get a Summary that leaves
   rows out (2.5).
2. **Pinterest: trust the Standard access path, not the RSS steps.**
   - Ember's message #150 told you to claim "the Etsy shop domain"; #152 told you to claim ember-ai.de or the shop's
     domain and add a feed under Pinterest's settings.
   - You can't add an HTML tag to an Etsy shop's pages, and you found no "bulk create pins" in the settings (#153).
   - Ember took this from its research (cycle #128) and presented it as fact. It also stored it as lesson [#c128].
   - The sandbox test pin you made on 2026-10-07 for the Standard access video is the path that is known to work.
   - Tell Ember in your answer to drop or re-check lesson [#c128].
3. **Your messages don't wake Ember** (**Wake Ember when you write** is off). #153 waited three hours for the next
   scheduled cycle (17:45 UTC). Turn it on if you want answers sooner; each wake-up costs a cycle, about $0.44 on
   average.
4. **Two promises are doubled** from before 0.32.0: #32 and #33 (the KDP proposal), and #20 and #28 (the Bluesky
   report). Ember closes both of a pair once it has told you; nothing for you to do but expect one report, not two.

---

## 4. Not changed in 0.32.0: what I recommend next

1. **Reach is the business's problem, and no code change fixes it.** Of the channels you connected:
   - Bluesky has 73 followers and shows no view gain: 15 posts with 1 to 4 likes each.
   - Pinterest waits for Standard access.
   - The blog's 6 posts show no effect.
   - The five listing tests' Day-7 bars were due on 2026-10-07 with 2, 3, 0, 2 and 1 views against 10.
   - The three bets that settled on 2026-10-07 gained 0 views each.

   Your decision point is 2026-10-14 (milestone #24). The one channel with known traffic that needs nothing new from
   you is Pinterest with Standard access. Etsy Ads, which Ember parked as venture #13, is the paid alternative (its
   own pitch: €1 to €3 a day for 14 days), a test you can stop at any time.
2. **`make_spreadsheet`'s cap of 3 a cycle is tight for EN and DE pairs** (build both, fix, build both again). `edit`
   makes the second round cheap, and the guide says to fix every spec first. If it still bites, raise it to 4.
3. **Ember asked for 480 minutes of sleep in 5 of the 12 cycles** (the most allowed is 360). Your standing
   instruction says not to sleep to save money. Ember's code cuts the sleep when READY lists work, so the cost is
   small, but the habit contradicts your instruction. Tell Ember once more, or lower **Longest sleep (minutes)**.
4. **Ember states other sites' menus as facts.** A rule for messages to you would help, for example: "say where you
   read it and when; menus move". I didn't add it, because it changes every plan's prompt.
5. **Lessons don't hear about later corrections.** [#c93] still says the Summary's ranges were "1-3 rows short",
   though the real fault was the title rows. The lesson consolidation could re-check a lesson when a later cycle's
   journal contradicts it.

---

## 5. What changed in the code

| Area | Files |
|---|---|
| Printify listings in posts, pins and marketing cycles | `integrations/printify_publisher.py`, `integrations/etsy_publisher.py`, `integrations/bluesky_publisher.py`, `integrations/pinterest_publisher.py`, `agent/tools.py`, `agent/lines.py`, `agent/loop.py`, `agent/views.py`, guides `bluesky.md`, `pinterest.md` |
| Bluesky's room by language | `integrations/bluesky.py`, `agent/tools.py`, guide `bluesky.md` |
| `venture_update` keeps what it can | `agent/tools.py` (`_venture_update`, `_new_stage`) |
| `workspace_write`'s `edit` | `agent/tools.py` (`_edit`), guide `spreadsheets.md` |
| Formulas pointing at titles, headers or empty cells | `products/sheets.py` |
| Release notes across updates | `agent/news.py` |
| Answered messages, repeated promises, caps in guides, cut-off calls, the bet prompt | `agent/tools.py`, `agent/obligations.py`, `agent/loop.py`, `agent/lines.py`, guides |
| Tests | `tests/test_fixes_0320.py` (new). Updated where they pinned the old behaviour: `tests/test_ventures.py`, `tests/test_venture_stages.py`; and the renamed keyword `printify_links` in `tests/test_fixes_0240.py`, `tests/test_fixes_0300.py`, `tests/test_learning_loop.py` |
| Docs and notes | `ember/CHANGELOG.md` (for Ember), `ember/DOCS.md`, `README.md`, `ember/config.yaml` |

No database migration: the release notes' progress is a meta key. Nothing changes in what leaves the container, in
the money guard or in your controls.
