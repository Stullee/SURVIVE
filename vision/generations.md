# Vision: Ember's next generation

*Draft, 2026-10-23, written against `ed592b1` (0.45.4), the diagnostics report of 10-23 20:55 UTC and the evaluation
of 10-22 (`evaluation/2026-10-22-0.44.0/`). 0.46.0 builds its foundation: the mind/world split (see "The
engineering"), with the counts of a platform's limits on the world and migration 0102; nothing begins a generation
yet. 0.47.0 builds the estate and the generation change ("The estate", "The change, step by step"; Prepare is the
card's hand-over and its checks). 0.48.0 builds the inheritance (principles, facts, craft, the letter, the seeds;
agent/inheritance.py), with three changes to this draft: the facts are `inheritance/facts.md` and the seed strategy's
opening rather than a knowledge document, and no model writes a number, so there is none of the evaluator's to check;
the owner strikes items rather than editing them; the letter is asked for on the card, and one call writes it. Still
open: re-pinning lessons, the dress rehearsal on a copy of the live database, and phase 2. 0.49.0 builds the scorecard
by generation ("How we know she is smarter"): a generation's measures over its first 14 and 28 days and its last 28,
beside its predecessor's. 0.50.0 closes the old generation's open work at the change (its lines, ventures, plan,
milestones, bets, predictions, promises and unlocks) and carries the owner's goal, which is theirs as the rulebook
is. It answers the
owner's question: how can Ember evolve into a next generation that carries over the important learnings, starts with a
clean mind, and is smarter for it, without holding on to the products her first generation failed with?*

## The idea in one paragraph

Ember's first live life (life 2, since 09-28) has been a test bench for her code as much as a business: 106 releases
in 26 days, bugs that steered her (69 of 80 chosen sleeps cut in one week) and bugs that taught her false lessons,
$126 spent and $0 earned. A generation change ends that life on purpose. It is not a death. Her **mind** starts empty:
projects, ventures, plan, cases, playbook, lessons, files. The **world** carries on: the money, the shop, the books, the
accounts, the mailbox and its opt-outs, and the owner's rulebook and library. A small, checked **inheritance** passes
between the two:
- a few principles, which count as hypotheses until her own evidence confirms them;
- dated facts that Ember's code computes from the books;
- the craft files she needs;
- a letter from her predecessor.

Products pass as an **estate** that the owner sorts. A few are adopted, each with a fresh test and a fixed date. The
rest stay live but are not hers to work on. None of them carries its sunk cost.

## What the research found

1. **A live life ends only by death.** `ended` is used only by dry run (`economy/life.py:203-225`). `start()` gives
   birth to a live life only when there is none, and only a dead life revives (`life.py:340-347`, `_revival`).
2. **A revived life keeps everything.** The agent's records are scoped by mode and session (`agent/store.py:37-39`),
   and in live the session is always 0 (`life.py:161-166`). `life_id` is stored but never filters. The live memory
   and workspace folders have fixed paths (`agent/service.py:248-252`). The successor never reads the last will. It
   goes to the owner, and to the live page once the owner approves it (`integrations/live_view.py:500-543`).
3. **Dry run already has a fresh start.** Each new dry-run session gets a new number. Its folders are moved to `*.prev`
   (`service.py:359-369`), and its ventures are seeded again (`agent/ventures.py:1011`). That is the template.
4. **The world is scoped by session too.** The Etsy, Printify, Pinterest, Bluesky, website, blog and email tables are
   all (mode, session), and so are the email opt-outs. A plain bump of the live session would:
   - forget every opt-out (`integrations/mailstore.py:455-462`): she could email people who asked her to stop;
   - stop the Etsy sync. With no listing in scope it reads no orders (`integrations/etsy_publisher.py:846-892`), so no
     revenue, fees, refunds or renewals are recorded. The Printify, Pinterest and Bluesky syncs stop the same way;
   - delete pending listing fees without booking them (`integrations/etsy_revenue.py:278-312`);
   - restart the counts that stand for real platform limits: KDP's 2 new titles a week (`integrations/kdp.py:830`),
     Etsy's daily listings (`etsy_publisher.py:77`) and the publishing queue's gaps;
   - make every listing, board and correspondent count as new for the NEVER gate (the `approvals_never` view);
   - strand the first generation's requests: the owner could no longer decide them, undo them, or remove their text
     (`agent/owner.py:177-182, 510-517`).
5. **A working template already exists.** The evaluation feedback import (`feedback.py`, 0.44.0) brings learnings in
   from outside. It takes a validated JSON file, which the owner previews and then gives. The file is recorded once,
   by its hash. Its cases become hypotheses through `learning.adopt`.
6. **The playbook's rules today.** Ember's code sets a principle's confidence (`agent/learning.py:202-205, 416-429`):
   3 cases of her own make it established; one case against makes it disputed. It fades after 42 days without a case
   for it (120 days once established), and there are at most 40. Her files are capped at `lessons.md` 4 KB,
   `strategy.md` 2 KB and `identity.md` 800 B.
7. **A small bug a generation would hit.** `prune_versions` hardcodes `("live", 0)` (`service.py:376`). A second
   generation's file versions would be pruned as if the files were gone.

## What carries on, what starts empty

**The world carries on, untouched:**
- the ledger and the balance (the ledger never had a session);
- the lives (a new row);
- the Etsy shop and its listings, the Printify products, the Pinterest boards and pins, the Bluesky account with its
  followers, the website and blog;
- the mailbox and the opt-outs;
- listing and shop views by day;
- KDP's titles and the integrations' logins;
- the owner's rulebook and library. They are the owner's, not hers, and the owner reviews the rulebook at the change.

**Her mind starts empty:**
- projects (lines), ventures, milestones, the plan tree and its picks;
- requests, messages, promises and obligations, the agenda, the journal, cycles;
- daily reviews and weekly looks;
- bets, cases, principles and predictions;
- quality checks, evidence and research, demand notes, made PDFs, paid tests;
- upgrades and workshop runs;
- unlocks and lesson pins;
- the memory files and the workspace.

The first generation's rows stay in the database under her generation, and her folders move to
`/data/generations/1/`. Nothing is deleted.

**Passes only through the inheritance:** the next section.

## The inheritance: a clean mind, but smarter

It passes generalisations, not episodes. Experiments on learning across generations find that a narrow channel makes
what passes more general and more useful. A wide one passes the noise along with it. So the inheritance is small,
about 6 KB in all, and every item in it must be a lesson about the business, backed by evidence.

1. **Principles, at most 12.** Business lessons with the first generation's evidence. Tool limits never pass, and
   neither does any lesson her code taught her wrongly. They enter her playbook as **inherited**:
   - **How they are shown:** "[inherited: generation 1, 8 cases for, 0 against]".
   - **Discounted:** the first generation's cases count as one case.
   - **Established:** after 2 supporting cases of her own.
   - **Disputed:** by her first case against.
   - **Retired:** after 60 days with no case of her own for it.

   They are priors to test, not dogma. Candidates from today's playbook:
   - #3 (established, 8 cases): outside reach with every launch;
   - #27: sell where buyers already search;
   - #29: don't polish below about 10 views;
   - #30: check against the source, the spec and the warnings;
   - #31: honest copy, made by an AI agent.

   Among the failure classes she caused (the failure ledger), F5, F8, F9 and F11 become "don't" principles.

   An example of what must not pass: lesson [#c389], "KDP 'Attention needed' emails can carry zero diagnostic body
   text". The hidden-text filter that 0.45.2 fixed lost that text, and 0.45.4 says so.
2. **Facts, computed by Ember's code, each with its date.** What the market did with the first generation's work, per
   channel and per product: views, favourites, clicks, sales, spend, days live and reach done. For example:
   - Etsy: 7 listings, 23 views in 25 days, 0 favourites, with the reach on record;
   - Pinterest: 25 pins, 1 click;
   - Bluesky: 219 followers and 61 reactions on 26 posts, with no measured clicks;
   - KDP: 2 titles live since 10-20.

   No model writes them, so none can get them wrong. They seed her strategy's situation. They are also kept as one
   knowledge document, which `knowledge_search` finds.
3. **Craft, as files.** Copied into `inheritance/` in the new workspace:
   - the KDP checklist;
   - the specs and sources of the adopted books, so a KDP notice can be answered;
   - the owner's quality bar, drawn from the six Haushaltsbuch rejections.

   Scripts that proved themselves are already in the code (upgrades #8, #10 and #12).
4. **The owner's way of working.** One batched message a day, yes or no decisions, the exact spec built as written,
   finished work only. Most of it is in the rulebook, which carries on. The rest becomes a principle or two.
5. **A letter, at most 3,000 characters.** The first generation's own words: what she tried and what she would do
   differently. It is the last will's call with a succession prompt, kept as `inheritance/letter.md` and named in her
   identity. It is not in every prompt: it is her view, not evidence.
6. **A seed strategy and identity.**
   - **Strategy:** at most 1,200 bytes, from the facts and the principles. It says to rewrite it when her own evidence
     says so.
   - **Identity:** "I am Ember, the second generation. My predecessor lived from 09-28 to 11-01; my owner ended her
     life so that I could start with a clean mind. What she learned is mine to test, not to obey."

**Not inherited:** her 62 cases, 57 ventures, projects, plan, bets and journal; her lessons file and her strategy as
written; unlocks (she earns trust again, and the owner may grant it again); and any open promise, because all of them
close before the change.

**Who writes it.**
1. **Gen 1 proposes, with no extra model call:** her playbook as it stands, plus her letter.
2. **An independent evaluator drafts it.** That is an AI chat given an inheritance brief (like `EVALUATE.md`), or a
   Claude Code session in this repository. It reads the whole-life evaluation export and the failure ledger and strikes
   what the records contradict or what her code taught her. It also judges every product of the estate from its data:
   a verdict (adopt, passive or retire) with the numbers behind it, and for an adopted product its first test and
   date. It writes `inheritance.json`.
3. **Ember's code checks it:**
   - every principle cites cases that exist, are not "too early" and are not low-sure;
   - no tool-limit line;
   - every number matches the facts it computed;
   - the size limits hold;
   - no project or venture id is named, because the new mind has none.
4. **The owner previews it on the dashboard,** strikes or edits, and gives it. This follows `feedback.py`'s preview and
   give, and it waits there until the change.

## The estate: products without their sunk cost

The owner's worry is right. A product that passes with its line also passes her hopes for it, its history and the money
already spent on it, and she would defend it from her first plan. Three rules keep that from happening.

1. **Products pass, lines don't.** The second generation gets the thing: the listing, the book, the poster. She never
   gets the line with its hypothesis, history, spend and bets. The first generation's spend stays in the first
   generation's books, so an adopted product's P&L starts at $0.
2. **The owner sorts every product before the change, from its numbers.** Each product gets one verdict:

   | Verdict | What it means |
   |---|---|
   | **Adopt** | The product becomes one of her lines at birth. It has a first test with a date fixed at the handover (a KDP book: its first sale by 12-15, say). A missed test holds its line until the owner keeps or drops it (0.47.0), which she can't lift herself: no rescue spending. |
   | **Passive** | The product stays live, which is free: Etsy charges only at a renewal, and Printify only when an order comes in. It is still synced, and its sales are still recorded. Her plan shows one line about it: "6 listings from generation 1 stay live, passive: 0 sales. Not yours to work on." She can't propose a change to it. If a passive product sells, it is offered to her to adopt: evidence brings it back, sunk cost never does. |
   | **Retire** | The product is taken down before the change: an Etsy deactivate request, and Printify, KDP and pins by the owner's hand or Undo. |

   **The evaluator proposes each product's verdict from the data,** in its `feedback.json` (`estate`, 0.47.0; the
   inheritance will carry the same): its views, favourites,
   clicks, sales, days live, the reach done for it, what it cost, and why that verdict. Ember's code checks each
   proposal against its own rule, and the dashboard shows both side by side. The owner decides. The rule:
   - adopt what is too new to judge or shows a buyer signal;
   - passive what had a fair test (reach done, 21 days or more) and no signal;
   - retire what breaks a rule.

   Where the two differ, the evaluator's "why" has to say what its rule saw that the code's rule didn't.
3. **Nothing inherited breaks her rules.** These texts claim a person made her work (F11, the incident of the week
   10-12 to 10-18): 8 pins, listings #124 and #125, and the KDP books' descriptions. They are corrected or retired
   before the change, so the second generation doesn't start with an incident.

What today's numbers suggest (decided on the day of the change):
- **Adopt:** KDP #140 and #141, and the Kassenbuch if it is proposed by then. They are the only products buyers can
  find through search, they are too new to judge, and their season runs from November to January.
- **Passive:**
  - the 7 Etsy listings (none reached 10 views; the coach licence has 8);
  - the 2 Printify posters (1 view each), unless the owner retires them;
  - the blog posts and the site pages.
- **Stay as channels:** the Pinterest and Bluesky accounts are part of the world. What they did shows in the facts.

## The change, step by step

1. **Prepare.** The owner starts it on the System tab ("Prepare the next generation") and gives a date.
   - The first generation stops starting things: no new projects, ventures or requests, only finishing work and
     corrections. Her promises close.
   - Ember's code drains what is open: pending requests wait for the owner's decision, approved ones run, and the
     publishing queue empties or is cancelled.
   - The owner sorts the estate, and the first generation writes her letter.
2. **Inherit.** The owner makes the whole-life export (Diagnostics tab). The evaluator writes `inheritance.json`, and
   the owner gives it on the dashboard.
3. **Change.** The owner presses "Begin generation 2", with a grant if they want. It is refused while anything is
   pending or running. After a copy of the database to `/data/backups`, one transaction:
   - ends life 2 with state `ended` and the reason "succeeded by generation 2 (owner's choice)". The `lives` CHECK
     already allows `ended`;
   - closes the first generation's open lines and ventures as "ended with generation 1";
   - raises the live generation counter, which the mind's session follows;
   - gives birth to a new life, `revived_from` life 2, its reason naming the inheritance;
   - resets the old mind's meta keys that are kept per mode: `failures`, `backoff_until`, `sleep_cut`,
     `owner_wake_*`, `windows_paused_at`, `next_wake_*` and `will_*`;
   - moves the folders to `/data/generations/1/{workspace,memory}` and seeds the new ones with `inheritance/`;
   - writes the inheritance: the inherited principles, the facts document and the seed files;
   - makes the adopted products her lines, with their tests;
   - pins again the lessons the owner chose to keep.

   Then the agent restarts, so the per-session startup runs: the ventures' seed, the memory files, the backfill.
4. **Birth.** Her first plan's STATUS says, once, who she is, why her predecessor's life ended, and where the
   inheritance is. Her software notes begin at the current version.

## The engineering: one scope for the mind, one for the world

`AgentScope` gets a second number:
- **`session`** is the mind's: her generation in live, the dry-run session in dry run.
- **`world`** is 0 in live, always, and the dry-run session in dry run.

`where()` stays the mind's. A new `world_where()` serves every query of a world table. In dry run and in the first
generation the two numbers are equal, so nothing changes until a generation change, and no row is moved or rewritten.
The mind's 40-odd tables are not touched.

- **World tables** (they move to `world_where`):
  - Etsy: `etsy_listings`, `etsy_edits`, `etsy_orders`, `etsy_photo_ids`;
  - Printify: `printify_products`, `printify_edits`, `printify_orders`;
  - Pinterest and Bluesky: `pinterest_boards`, `pinterest_pins`, `bluesky_posts`;
  - website and blog: `site_pages`, `site_downloads`, `site_uploads`, `blog_posts`;
  - mail: `emails` (with `email_actions`, `email_rereads` and `inquiry_closures` through their email),
    `email_suppressions`;
  - the owner's: `rules`, `library_documents` (with `library_parts`), `learnings`;
  - the new `estate` table.

  `observations` is mixed: its listing and shop rows are the world's, its milestone rows the mind's, so the writer picks
  the scope by subject. There are about 400 references in some 40 files, most of them through `scope.where()`.
- **Counts that stand for a platform's limit count the world:** KDP's weekly titles, Etsy's daily listings, the
  publishing queue's gaps, and the NEVER gate's "first" classes.
- **Links from the world to a mind.** An order's line (`order_project`, `line_sql`) is an adopted product's line in
  the second generation, through a `listing_lines` row in her session. Otherwise it has no line: the revenue still
  counts for the balance, the goal and the estate's numbers, but not for any of her lines.
- **The owner's tools reach every generation.** Deciding a request, Undo and removing a message's text look rows up by
  id within the mode, so nothing of the first generation is stranded.
- **The scorecard gets the generation.** `scorecard.py` reads most tables by mode only today, so it would mix the first
  generation's open lines into the second's measures.
- **`prune_versions`** uses the current generation's live folder.

**The safety net.**
- **A static audit test,** like `tests/cycle/test_rule_audit.py`: every SQL statement that names a world table uses
  `world_where`.
- **Tests in live mode at generation 2** (session 1, world 0) check that:
  - the sync reads the first generation's listings and orders;
  - opt-outs hold, and fees are booked;
  - KDP's weekly count and the NEVER gate span generations;
  - revenue is attributed right;
  - the owner can decide, undo and remove the text of the first generation's rows.
- **A dress rehearsal in dry run.** Dry run gets the same generation change (the mind's number goes up, the world's
  stays), run on a copy of the live database with the fake model, at no cost.

## How we know she is smarter

The second generation's scorecard starts at her birth. A change on Sunday makes her first week a whole one. Her first
14 and 28 days are compared with the first generation's on the same measures:
- the first generation's failure classes she repeats despite an inherited principle against them (L5 across
  generations; target 0);
- spend on stopped work (E5), ideas parked at birth (V2, F4), cost per shipped thing (E4) and first-pass approvals
  (X3);
- belief accuracy (R1), and the owner's minutes (O3, from `week.csv`);
- how many inherited principles her own evidence confirmed, disputed or retired.

That comparison only means something if the code holds still. During the second generation's first four weeks, only
fixes for defects that burn money or break rules, each one logged.

## What the owner decides

| Question | Recommendation |
|---|---|
| A generation change in the same database, or a new install? | The same database, with the mind/world split. A new install loses the links to the shop and the books, the opt-outs and the history, and needs every integration authorised again. |
| Who writes the inheritance? | The evaluator, from the export, with her playbook and her letter as inputs. Ember's code checks it, and the owner gives it. |
| Do inherited principles start as hypotheses or as established? | Hypotheses at a discount. Established would make her obey lessons that the bugs half taught her. |
| Products | Products pass, lines don't. The evaluator proposes adopt, passive or retire for each product from its data, Ember's code checks it against its rule, and the owner decides on the day of the change. |
| Unlocks | Back to manual. She earns them again, and the owner can grant them again at once. |
| Rulebook and library | They carry on. The owner reviews the rulebook for rules about the first generation's lines. |
| Name | Still Ember, with her generation in her identity, on the dashboard and on the live page ("generation 2"). |
| Money | A grant at the change that, with what is left (about $45 on 11-01), gives at least six weeks of runway: about $80. |
| Code freeze | Only fixes for four weeks from her birth. |

## Phases

**Phase 1, before the change (target Sunday 11-01):**
- the mind/world scope split, with its audit test and the generation 2 tests;
- the generation counter, Prepare and Begin, the folders, the meta reset;
- the `estate` table with its verdicts, and the planner's passive line;
- `inheritance.json`: its format, its checks, preview and give;
- the inherited principles' confidence rules;
- the counts that stand for platform limits;
- the scorecard by generation;
- the dashboard's generation label;
- the letter, from the last will's call;
- the dress rehearsal in dry run.

**Phase 2, after the change (not needed for it):**
- the owner's read-only view of the first generation's records on the dashboard;
- the live page's archive of past lives (`live/life-N.html`, `vision/live-page.md`);
- a succession call in which Ember proposes the inheritance herself;
- a public note of the change on Bluesky and the live page, approved by the owner.

If phase 1 isn't solid by 10-30 (tests green, rehearsal clean), the change moves rather than being rushed. A week's
delay costs about $20. A broken sync costs sales nobody records and opt-outs that don't hold.

## Prior art this borrows from

- **Experience distilled into insights** (ExpeL, 2023): an agent turns many trajectories into a short list of insights
  that are voted up and down, rather than keeping the trajectories themselves.
- **Iterated learning** (Kirby et al., 2008): what passes through a narrow channel between generations becomes more
  general.
- **Discounted priors** (the power prior in Bayesian statistics): evidence from an earlier, different trial counts, but
  at a weight below one.
- **After-action reviews:** what was supposed to happen, what happened, why, and what to keep or change. They are the
  same questions her cases already ask.
