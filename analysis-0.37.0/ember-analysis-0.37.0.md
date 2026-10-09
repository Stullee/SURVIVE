# Ember 0.37.0: how it works, and whether the plan's weights choose well

*Your diagnostics report of 2026-10-09 16:06 UTC (version 0.37.0, build `bf87976452f4faf3`, cycles #165 to #176 in
full), read against `55ce9a3` (0.37.0 and the cleanup merged after it). Nothing in the app is changed by this
analysis.*

**Evidence.** I read the code that decides what Ember does (`agent/weights.py`, `agent/plan.py`, `agent/templates.py`,
the loop's steering and the diagnostics) line by line, the docs, the changelog from 0.28.0 on, and the report's
sections on the plan: the tree (147 nodes), the 24 picks it lists, the weights' settings, the ranking for the next
cycle, the planner's context and the 12 newest cycles. A survey of the rest of the code (the cycle, the economy, the
tools and integrations, the products) was split into parts; I checked every finding of it that this report repeats.

Labels, as in the earlier analyses:
- **live**: it is in your report.
- **code**: found by reading the code.
- **reproduced**: a script against the repository's own `weights.py` showed it.

**Paths** are relative to `ember/app/` unless they start with `tests/` or the repository root.

---

## 1. The short answer

- **The weights are in the report.** It lists every number of `weights.py` and the five of `plan.py` that feed it,
  each cycle's pick with its weight's parts and the steps it was chosen from, and the next cycle's eight heaviest
  steps with each weight in words (section 3).
- **The arithmetic is right.** All 14 scored picks re-score exactly from their parts. I rebuilt the whole ranking
  from your plan tree with the repository's `weights.py`: it gives the report's top 8 step for step and weight for
  weight (section 4.1).
- **The step it takes now is defensible**: your rejection of the KDP Haushaltsbuch (request #81), whose proposal you
  wanted by 10-10.
- **But the evaluation is not sensible yet, for one main reason: a promise to you, or your decision, still comes
  first whatever else waits.** Each open one weighs at least 5 × (1 + 2) = 15. On this install no product step gets
  there: the heaviest weigh 9.4 now (a live product's first pins), 12 to 14 at the very best, and a promise gains
  2.5 a day of waiting against a product step's 1. 0.37.0 meant the opposite ("until now they came first, whatever
  else waited"). Nine are open: four of them are one KDP job, two of them one report. So the pins for your live
  listings, first in your rulebook, in Ember's strategy, in today's review and in this week's look, are 10th. None of
  the 24 recorded picks went to a launch pin or post. If nothing closes, the first cycle after midnight takes report
  promise #20 (due 10-11), then its twin #28, ahead of the KDP resubmission (sections 4.3 to 4.5).
- **Worth tells the products apart only by their type.** Every Etsy and Printify product is worth exactly 2.0: the
  €39 coach bundle like the €5 CV (section 4.6).
- **The numbers have hardly met a live cycle.** 0.37.0 made one live pick (cycle #176), and 0.35.3 and 0.36.0 none.
  The week of shadow cycles 0.34.0 planned for tuning lasted about two hours on your device. The weights then
  changed four times in 33 hours (section 4.11).
- **Until the weights change: pin the "Pin it twice" steps on the Plan tab.** A pinned step is taken before any
  weight (section 5.1).
- **Beyond the weights, three live problems need you** (sections 5.1 and 6; times in Berlin time):
  - **The daily cap was gone by 12:35.** The 30-minute sleep cut ran 21 cycles between midnight and 12:35, spending
    $5.88 of your $7. Your messages #174 and #175 (12:42) then waited 5 hours 17 minutes for an answer.
  - **Message #176 told you wrong things.**
    - "ETAIShop" is the shop's real name, not a placeholder.
    - Your Bluesky handle is in Ember's options. Ordinary cycles don't show it.
    - The ventures it said it parked aren't the ones in the records.
  - **Your web host has refused the SFTP login since 14:08.** The blog uses the same login.

---

## 2. How Ember works

### 2.1 What it is for

Ember is a Home Assistant app that runs an AI agent with one economic rule: every Anthropic call is paid from a
balance you fund, and it has to find honest ways to earn more than it spends. It can run out of money and die. Its
fixed prompt core (`agent/constitution.md`) ranks honesty and harmlessness above your oversight, and both above
survival. It must say it is an AI wherever its work reaches people.

You are its investor and its hands in the outside world:
- you approve everything that leaves the container;
- you publish what has no API (KDP books, Reddit posts, website pages);
- you record revenue, or let Ember's code record it from Etsy's numbers;
- you steer it with your goal, your rulebook, your messages and the Plan tab.

### 2.2 One process, one scheduler, one cycle at a time

`python -m app` starts FastAPI on port 8099 behind Home Assistant's Ingress (`security.py` accepts only the Ingress
proxy, plus `GET /api/sensors` from the host network).

**At start** (`main.py`):
- the SQLite database opens: one connection under a lock, WAL, migrations 0001 to 0091 with a backup first;
- the economy takes `/data/ember.lock` and charges calls that an earlier process left unsettled at their worst case
  (your report's warning about $0.10 is this);
- the agent restores its state (`agent/service.py:261-321`).

**Every scheduler round** (`agent/scheduler.py`) does, in order:
1. re-evaluates the life state;
2. lets your unlocks approve what their veto window allows;
3. carries out approved requests;
4. syncs the shop and publishes the live page;
5. reads mail and events;
6. `decide()`s whether a cycle runs now (`agent/service.py:374-447`):
   - your message or decision wakes Ember where your wake switches allow it (on your install a message doesn't:
     `wake_on_message` is off). It wakes 5 minutes after your last one, at least 30 minutes after the previous such
     wake;
   - an event (a reply, an inquiry, a milestone's day) wakes it at most 4 times a day;
   - otherwise it wakes on its own schedule. Until 20:00 a fifth of the daily cap is kept for event wakes, and with
     the cap spent it waits for midnight.

Only one cycle runs at a time.

### 2.3 A wake cycle

`agent/loop.py`, `run` and `_plan_act_reflect`:

1. **Open**: the cycle's cap and burn mode are recorded.
2. **Read**: new mail; Etsy, Pinterest, Bluesky and Printify are synced.
3. **Before the plan**:
   - the daily review (the first cycle of the day);
   - the weekly look;
   - study of your library;
   - the quality critic on one live listing.

   Each is guarded: a failure never ends the cycle.
4. **Keep**: Ember's code keeps its own records — stages of ventures, bets, the playbook, obligations — then the plan
   tree (`plan.keep`, section 2.7).
5. **Steer**: the plan tree picks the cycle's step (`plan.steer`). The step decides:
   - the kind of cycle: ordinary, marketing (a marketing step) or venture (a venture's step);
   - the one product line the tools may work on (`tools._line`).
6. **Plan**: one call to the planner model (Opus 5.5 on your install). Its context (`agent/context.py`) holds status,
   obligations, your rulebook, your news, the last cycle's handoff, the release notes, today's review, YOUR PLAN, the
   open projects, YOUR STEP, the ventures, the channels, strategy, lessons, workspace, research and library. It
   answers in JSON.
7. **Work**: the worker model (Sonnet 5) calls tools, at most `max_tool_steps` (20 on your install) and 4 calls a
   turn. Before every step the guard checks that the step and a reflection fit the cycle's room.
8. **Reflect**: journal, lessons and the next sleep. Ember's code writes the cycle's digest.

An event's wake is a lean **reactive** cycle: at most 5 steps, no review, critic or venture work.

**The sleep** is Ember's choice within your minimum and maximum. While the plan has a step ready, Ember's code cuts
it to your shortest sleep (`agent/slack.py`). On your install the plan always has one, so cycles follow each other
every 30 minutes until the daily cap stops them: $6.27 of $7.00 was spent by 18:06.

### 2.4 Money (`economy/`)

**The ledger** has typed rows: grants, revenue, adjustments, API costs and their corrections, and expenses. A
correction is a row of its own, never an edit.

**Every model call is metered** (`economy/metering.py`):
1. its worst case is priced before it is sent (tokens counted, a safety factor per purpose, server tools included);
2. a pending row is written;
3. exactly one HTTP request goes to `api.anthropic.com`, with no retries;
4. it is settled from the usage Anthropic reports. A bill that can't be known is charged at its worst case.

**The caps:**
- a cycle cap on expected cost;
- a daily cap on the worst case;
- the event reserve (20 % until 20:00);
- the workshop's $0.75 a run;
- the library's $0.50 a day;
- a reserve for the last will.

**Life states.** Alive, critical (under 2 days of runway; leaving it needs 4 days and new money), paused, unfunded and
dead. Dead means the balance is used up, or the last will can no longer be paid.

**Runway** is the balance over the last 7 days' API spend per active day.

**The burn mode** (explore, focus, maintenance, dormant) follows the net runway. Your stance `invest` keeps it at
explore.

**Dry run** runs everything with a fake model, a separate ledger and the network sealed.

### 2.5 What Ember can do, and what only you do

**The model never sends anything itself.** Each tool (`agent/tools.py`, one `SPECS` entry each) does one of:
- reads or writes Ember's own records and files, in a jail (`agent/sandbox.py`);
- makes a metered call: research with Anthropic's web tools, drafts, brainstorms, the workshop's sandboxed code;
- reads an integration;
- files a request for your approval: email, Etsy listing or edit, pin, Bluesky post, Printify product, blog post or
  link page, KDP book, Reddit post, or anything else.

**After you approve a request**, Ember's code carries it out on the next scheduler round:
- it writes a journal entry before any network call;
- it re-checks the approved files' hashes;
- it never retries.

| Channel | Who carries it out | Undo |
|---|---|---|
| Email, Etsy listings and edits, pins, Bluesky posts, Printify products, blog posts over SFTP | Ember's code | yes, except an email |
| KDP books, Reddit posts, website pages | you | – |

**Unlocks** let you pre-approve narrow kinds of request with a veto window: a QA fix, a price change, a variant,
deactivating a listing, an email reply. Some kinds always need you (`agent/never.py`).

**One documented exception.** To learn what a Printify product costs to make, Ember's code creates an unpublished
product with a blank picture and deletes it at once (`DOCS.md`, Printify). The module text of `agent/tools.py` still
says no tool reaches the network.

**Products are made by Ember's own code** (`products/`): PDFs with a Word copy, Excel files, listing photos, print
files, KDP covers and Nebenkosten statements. Each file is checked before it is kept: images re-encoded, PDFs and
Office files parsed, spreadsheet formulas from an allowlist. Blog pages and the live page come from fixed templates
with a strict CSP.

**Security.**
- Ingress only, with a CSRF header and a strict CSP.
- An audit hook (`agent/netguard.py`) blocks sockets, subprocesses and native libraries in tool handlers (in a dry
  run, in the whole cycle).
- Secrets live only in the options.
- The shareable report masks addresses, tokens and other people's text.

### 2.6 Ventures

A venture moves idea → researching → proposed → building → live, or is parked or killed (`agent/stages.py`).

**Ember's side:**
- six scores of 1 to 5 make the venture's "weight" (0 to 100, revenue counting double: `agent/ventures.py:276`);
- research is budgeted at $0.60 per unbacked venture;
- a proposal needs evidence, a business case with numbers, no knock-out (`agent/knockouts.py`) and an independent
  critic's verdict (`agent/critic.py`).

**Your side:** you back, park or kill it. Backing opens a product line and a first test due in 21 days. Ember's code
parks ventures that miss their stage's dates.

Since 0.37.0, each venture being explored is a node of the plan's Ventures project, and its next decision is a
weighed step. On your install no venture is being explored: all are building, live or parked.

### 2.7 The plan tree (0.34.0 to 0.37.0)

**Shape.** Your goal sits at the top. Under it are projects: Etsy, KDP, Printify, Website, Channels, Other, Ventures
and Your owner. Under those:
- each open product line is a product, laid out from its type's template (`agent/templates.py`);
- each product has stages: research, create, release, launch, maintain;
- each stage has small steps, with checks that Ember's code reads from the records. A step or stage never closes on
  words.

**Before every plan, `plan.keep`:**
- lays out new lines;
- closes what the checks show done;
- adds a step for each promise and each decision of yours;
- adds a live product's recurring marketing: a pin and a Bluesky post a week, a blog post a month, and the critic's
  fixes;
- reads each live product's decide-by dates: day 7, 14 and 21 from its first live listing.

**What Ember may do to the tree:** add, split, replace, finish or make wait her own steps, and hold a product. She
can't touch what you, a promise or Ember's code put there.

**Your levers on the Plan tab:**
- pin a step;
- set a product's or a venture's worth;
- hold or resume a product;
- hold new things;
- freeze titles and tags;
- keep, close or drop a product.

**Each cycle's step** is a step you pinned, else the heaviest ready step. To leave the product it worked on last,
another step must weigh 25 % more, for up to 3 cycles in a row (`weights.choose`). The weight
(`agent/weights.py:169`):

> weight = worth × kind × channel × (1 + urgency + age + momentum)

| Part | How it is set on 0.37.0 |
|---|---|
| worth | What the product could earn a month, damped: 2·log2(1 + $/5), from 1 to 10. Times its stage's chance: research 0.4, create 0.6, release 0.8, launch and maintain 1. Times, once its listings have 30 views, how it sells. Your worth replaces it. A promise or a decision: at least 5. |
| kind | ship, launch, fix, market 1; create 0.8; chore 0.5; report 0.3 |
| channel | a marketing step: clicks per pin ÷ 0.5, reactions per post ÷ 3, from 0.2 to 1.5. 1 until 4 items have been live for a week. |
| urgency | the largest that applies: a promise or a decision 2, then 4.5 ÷ (days left + 0.5), 11 once past, at most 12; a defect the critic found 5; a missed views bar 4; a live product's launch marketing 3; your wish for a venture 2; the critic's suggestions 1; a recurring step on its day 1; a venture about to be parked 1 |
| age | 0.5 a day ready and untouched (a product's steps count from its last cycle) |
| momentum | 1 for the product worked on last, for up to 3 cycles in a row |

### 2.8 Learning

**The daily review** (`agent/review.py`) judges the numbers and gives each project a verdict. The verdicts are
advisory: Ember's code applies none.

**The weekly look** (`agent/weekly.py`) rewrites the strategy and picks the week's questions.

**Bets and predictions** settle against the records. Settled items become cases. Cases become playbook principles: a
hypothesis first, established after 3 cases (`agent/learning.py`).

**Memory** (`agent/memory.py`) holds three files: strategy, identity and lessons, with every version kept. The
lessons file is consolidated when it grows.

### 2.9 Where it stands (live, 2026-10-09)

| | |
|---|---|
| Live since | 2026-09-28 |
| Money | $200 granted; $59.66 of API calls and $0.60 of expenses; $0 revenue |
| Balance and runway | $139.74; 24.5 days at about $5.70 per active day |
| Your goal | $100 a month by 11-15: 0 % |
| Live | 7 Etsy listings and 2 Printify posters: 13 views in all, no favorite, no order |
| Reach | 4 pins, all on one listing, with 0 impressions; 16 Bluesky posts (the newest 10 have 1 to 4 likes each) and 107 followers; 6 German blog posts |
| KDP | the Haushaltsbuch 2027 was rejected twice: #77 for white space, #81 for tables spilling over |
| Open obligations | 9: 8 promises and your decision on #81 |
| Money left | At about $5.70 a day the balance lasts until about 11-02, 13 days before your goal's date |

---

## 3. The plan's weights in your report

### 3.1 What is there: live

- **`-- plan tree: its weights' settings`** (report line 2000): `weights.settings()` and `plan.settings()`. These are
  every constant the weights and the choice use, as the device runs them. Added in 0.35.2.
- **`-- plan_picks`** (line 1882): each cycle's pick. It gives the cycle's kind, the step, why it was taken
  (`weight`, `promise`, `margin`, `pin`, `venture`), the weight, its parts as JSON, and the six heaviest steps it was
  chosen from.
- **`-- plan tree: the ranking now`** (line 2060): the next cycle's eight heaviest steps, each weight in words. The
  report runs the keeper in a savepoint and takes it back (`agent/loop.py:490-525`). That is why it shows step 148
  (promise #46), which the stored tree doesn't have yet.
- **`-- plan_nodes`** (line 1733): the tree, with each step's kind, source, due day and the time it became ready.

### 3.2 What it can't tell you yet: live, code

1. **Which version made a pick.** `plan_picks` has no version. Of the 24 picks the report lists, 23 come from 0.35.1;
   the 12 older ones in your database come from 0.34.0's shadow, 0.35.0 and 0.35.1. In 0.35.1:
   - the numbers differ: a defect weighed 2 (now 5), a missed bar 2 (now 4), there was no reach (3) or promise worth
     (5);
   - a promise came first by a rule, not by weight.

   The settings block is the running version's only, so it can re-score one recorded pick: cycle #176's. Only the
   12 newest cycles carry their version, in their headers.
2. **The whole ranking.** The report shows the next cycle's top 8 (`diagnostics.py:1083`), and each pick keeps its
   top 6 (`weights.Pick.json`). Here all 8 are promises or your decision. Where the products' own steps stand can
   only be rebuilt by hand (section 4.2).
3. **The inputs that aren't numbers in the tree:**
   - the channels' factors;
   - each product's `live_since`, `decide_by` and `pushed_until` (a missed views bar);
   - which channels the cycle saw connected.
4. **Two settings no pick can use:**
   - `DATE_SCALE` (`weights.py:53`, with `date_urgency()` at 112): nothing outside a test calls it.
   - `OWN_DATE_CAP`: on the device only. `9c80509` removed it after this build, for the same reason.

   Neither changes a pick, but a reader re-scoring picks will look for them.

---

## 4. Is the evaluation sensible?

### 4.1 The arithmetic is right: reproduced

For each of the 14 scored picks (cycles #153 to #176), I computed worth × kind × channel × (1 + urgency + age +
momentum) from its recorded parts. In every case:
- the weight equals the recorded "own";
- the total equals the larger of "own" and "carried";
- the picked step heads its ranking, or a rule of its version explains why not.

There are 0 mismatches. The other 10 picks are 0.35.1's "ventures' turn", which has no parts.

I then rebuilt every candidate from the plan tree as `plan.candidates` would. I took the stage's chance, each step's
ready time and the product's last cycle, the promises' due days and how often cycles took them in 24 hours, the
critic's verdicts and the streak (line #14, two cycles). The repository's `weights.choose` gives the report's ranking
exactly: steps 137, 138 and 148 at 20.02, then steps 142 to 146 at 15.02, with step 147 next (cut by the top 8).

### 4.2 The whole ranking now: reproduced

The next cycle's candidates with 0.37.0's numbers. Two inputs aren't in the report, and I assumed the neutral value:
no missed views bar and a Bluesky factor of 1. A missed bar would add 2 to a product's marketing steps. Neither
changes the order between promises and products.

| Weight | Step (plan node) | Line | Parts |
|---|---|---|---|
| 20.0 | step 137: obligation #44, your rejection of the KDP book (request #81) | 14 | 5 × (1 + 2 + 0 + momentum 1) |
| 20.0 | step 138: promise #45, bio and footer text; the redesigned interior proposed | 14 | same |
| 20.0 | step 148: promise #46, redo the interior and resubmit (made in cycle #176) | 14 | same |
| 15.0 | steps 142 to 147: promises #18, #20, #21, #27, #28, #43 (the Owner project) | – | 5 × (1 + 2) |
| 9.4 | Pin it twice (steps 26, 56, 71, 87: lines #4, #6, #7, #8); Post it twice on Bluesky (steps 27, 72, 88: lines #4, #7, #8) | | 2 × (1 + reach 3 + age 0.69) |
| ≈9 | step 43: a German blog post, line #5 | 5 | 2 × (1 + 3 + age) |
| 8.3 | step 12: Pin it twice, line #3 | 3 | 2 × (1 + 3 + 0.17) |
| 8.0 | steps 28, 58: a German blog post, lines #4, #6 | | 2 × (1 + 3) |
| 3.4 to 5.4 | The critic passes it, lines #3 to #8 | | 2 × (1 + improve 1 + age) |
| 1.7 | steps 128, 129: this week's pin and post, line #10 | 10 | 1 × (1 + 0.69) |
| 1.6 | step 140: brainstorm six new ideas | – | 2 × 0.8 × 1 |
| 0.8 | step 94: set the channel up, line #9 | 9 | 0.6 × 0.8 × 1.69 |
| 0.5 | step 106: write the demand note, line #12 | 12 | 0.4 × 0.8 × 1.69 |

Waiting:
- step 122: you publish the book at KDP;
- step 124: the book's blog post waits for an upgrade;
- line #12's later stages.

(Steps are plan nodes and are written without "#" in this report; "#" is for promises, requests, lines and cycles.)

### 4.3 A promise or your decision still comes first, whatever else waits: live, code, reproduced

**The rule.** A promise of a product weighs `max(product's worth, PROMISE_WORTH)` (`plan.py:1777`). One of no product
weighs `PROMISE_WORTH` (`plan.py:1091`). Its urgency never falls below `PROMISE_FLOOR` (`plan.py:1834-1847`). Its kind
counts 1, and so does its channel. So every open promise or decision weighs at least

> 5 × 1 × 1 × (1 + 2 + age + momentum) ≥ 15.

**The products.** On your install a product's worth is 2.0 (Etsy, Printify), 2.5 (the KDP book in release), 1.0 (the
website), 0.6 (Pinterest's channel) or 0.4 (line #12). The best a ready Etsy or Printify step can do:

| Situation | Weight |
|---|---|
| A launch pin now | 2 × (1 + 3 + age) = 9.4 |
| A missed views bar, with momentum | 2 × (1 + 4 + 1) = 12 |
| A defect, with momentum | 2 × (1 + 5 + 1) = 14 |

Momentum resets a product's age, so the last two can't age past that. Without momentum a product step gains 1 a
day; a promise gains 2.5 a day (5 × 0.5).

**The result.** No product step can catch up with a promise that waits. While one is open, product work comes after
it.

**What 0.37.0 meant.** The changelog: "Your promises to your owner and their decisions are weighed too, worth at
least 5 and heavier as their day nears; until now they came first, whatever else waited." Your own words, quoted in
migration 0091: "shouldn't promises just move into the plan and be evaluated for weight?" They moved into the plan,
but their floor decides the order before any weight does.

**The guard that was meant to stop a promise from hogging cycles doesn't.** "One you took three times in a day
without keeping it weighs less until the day is over" (`plan.py:1841-1847`). It lowers such a promise to that same
floor, 15, still above every product step.

Live, it showed at once. Cycle #175 took your decision on #81. In #176 its urgency had fallen to the floor (taken
once), yet it was taken again: 5 × (1 + 2 + 0.11 + momentum 1) = 20.6. Without momentum it would still have been 15.6
against the pins' 9.4.

**The test that guards this intent** (`tests/test_plan_weights.py:88-98`) beats a promise due tomorrow (20) with a
product step of worth 9 (36). A worth of 9 means about $108 a month expected. Nothing on your install sets a worth
above the template's ($5 a month, worth 2), so the case the test protects can't happen on real data.

**What it does next** (reproduced: the rebuilt state, moved forward, with nothing closed):
- **The next cycle that can run today** (after 20:00 Berlin, when the event reserve frees about $0.73) takes
  decision #44 a third time.
- **After midnight Berlin**, the promises due 10-11 rise to urgency 3. Line #14's streak has reached 3, so its
  momentum ends. The Owner project's promises have waited longer than the KDP steps. So the first cycle takes
  **step 143, report promise #20**: "Report Bluesky reactions and Etsy view deltas for posts #43/#44", due 10-11.
  Then step 146, promise #28, the same report.
- The KDP resubmission you wanted by 10-10 waits behind them.
- The first pin step stays 10th.

**In the 24 picks the report lists:**

| Picks | Cycles | Step |
|---|---|---|
| 12 | | a promise or your decision |
| 7 | #156 to #171 | promise #31 alone: the KDP proposal, productive work |
| 2 | #173, #174 | the critic's suggestions for line #3, taken over its pins (the bug 0.35.3 fixed) |
| 10 | | 0.35.1's ventures' turn: each one declined by Ember, because you had said "nothing new" (0.36.0 retired it) |
| 0 | | a launch pin or a Bluesky post |

The only pin made in those cycles was a fourth pin for the one listing that already had three (requests #79 and #80).
Six of your seven Etsy listings and both posters have no pin.

**Your dates aren't weighed, Ember's are.** Your rulebook wants the KDP proposal by 10-10. Ember's commits put it at
10-11 (promises #45 and #46), and the urgency follows those.

Nothing gives a step a date of yours. `weights.date_urgency` ("the owner's own date") has no caller, so your 10-10
lives only in the rulebook's words. On 10-10 the KDP redo weighs as "due tomorrow" (urgency 3), like the reports.

### 4.4 The same work is weighed several times: live, code, reproduced

Your rulebook says: "Every promise to me goes in commits with a due date." `message_owner` turns each `commits` into
an obligation (`tools.py:3418`), and each obligation becomes a step worth 15 or more.

**Repeats.** A repeat is caught only when two promises are due within 2 days and share 60 % of their words
(`obligations.repeated_promise`, `obligations.py:179`):

| Promises | Words shared | Days apart | Caught? |
|---|---|---|---|
| #20 and #28: "Report Bluesky reactions and Etsy view deltas for posts #43/#44" and "Bluesky reaction + view delta report for posts #43/#44; full view re-check" | 70 % | 0 | Would be now. This is the pair the check's docstring names as its reason, but the check stops new copies, not old ones: both are open and weigh 15 each. |
| #45 and #46: "…redesigned Haushaltsbuch 2027 interior proposed" and "Redo Haushaltsbuch 2027 interior and resubmit via propose_kdp_book" | 21 % | 0 | No |
| #43 and #46 ("Resubmit full-page Haushaltsbuch interior or park it") | 30 % | 3 | No |

**The KDP redo is four steps:**
- your decision #44;
- promises #45 and #46;
- promise #43, which sits in the Owner project. Its words name no listing number and neither "KDP" nor "Printify",
  all that `obligations.promised_line` reads (`obligations.py:145-170`). So it lost line #14 and its momentum.

### 4.5 Reports weigh like shipping work, days before they mean anything: live, code

Promises #18, #20, #21, #27 and #28 are reports, mostly of view or reaction deltas over a period that ends on their
due day: 10-11 to 10-18.

- **Kind.** A promise's kind is "promise", which `weights.KIND` doesn't know, so `plan._kind` counts it as "ship",
  1.0 (`plan.py:1829-1831`). The weights do have a report kind (0.3).
- **Readiness.** It is ready the moment it is made, at the floor of 2: #18, due 10-18, weighs 15 today.
- **No waiting.** Ember can't make a promise wait: `plan_step wait` refuses an owner's step (`plan.py:2631-2632`).

The way out the rules leave her is to send the report early and close it, with numbers taken too early to show
anything.

### 4.6 Worth doesn't tell the products apart: live, code

`could_earn` comes from Ember's revenue sub-goal for the line, else its venture's split, else the template's default
(`plan.py:208-230`). Sub-goals came from `milestone_plan` and Ember's own milestones, both retired in 0.35.0. No
listing has the 30 views that let sales change the chance (the most is 4).

So every Etsy and Printify product is worth exactly 2.0, the template's $5 a month. That holds for:
- the €39 commercial licence bundle;
- the €22.90 to €32.90 posters;
- the €5 CV.

Between products, worth carries no information: the order comes from urgency, age, momentum and, in a tie, the
step's number. You set no worth (the report's `plan_words` is empty). The Owner project can't take one: migration
0091's check allows a worth on products and ventures only (`0091_everything_in_the_plan.sql:63`).

**Latent.** An idea's worth comes from its scores: 2 × (0.5 + weight/100). Only the scores given count
(`ventures.py:276-286`). So an idea scored only "revenue 5" has weight 100 and the highest idea worth, 3.0, while a
product being researched is worth 0.8. No idea is open now.

### 4.7 The coach bundle is in the plan as an unstarted product: live

The €39 "Resume Template Commercial License Bundle for Career Coaches" (#4587912058) counts under line #4: line #4
shows 4 views, and its own cover-letter listing has 0. So line #12 is still a generic product in research:
- "Commercial licence packs for career coaches", your backed venture #12, scored 71, the highest of those you backed;
- worth 2 (generic) × 0.4 = 0.4;
- one step, a demand note, at 0.5.

Its listing's launch work is done, if at all, as line #4's.

### 4.8 The blog is on for the steering and off for the keeper: live, code

The two disagree on whether the blog channel is on:

| Where | Rule | On your install |
|---|---|---|
| `plan.channels_from` (`plan.py:3274-3280`), used by `keep` and the Plan tab | `blog_enabled` and `site_enabled` | off (`site_enabled` is off) |
| The loop's `blog_on` (`loop.py:264, 532`), used to steer | `blog_enabled` only | on |

`site_enabled` is the separate website builder. The blog runs on its own with Blog and SFTP (`DOCS.md`, Blog), and
you have six posts live.

The effects:
- No recurring "This month's blog post" step is ever added.
- Every keep resets the launch blog steps' clock (steps 28, 43 and 58 have no ready time in the tree).
- The Plan tab's idea of "now" can differ from the step the cycle takes.

### 4.9 The audience is guessed from words: live, code

`plan.infer_audience` counts German and English words in the line's title and hypothesis (`plan.py:191-205`):
- **Line #10**, the German blog articles, reads as English: "and" counts twice, and "DE" counts for nothing. So its
  recurring steps are a pin and an English Bluesky post, never the German blog post the product is.
- **Line #3** reads as English, though one of its two listings is the German Lebenslauf. So its launch has no blog
  step.

### 4.10 What works: live, reproduced

- **The arithmetic, the tie-breaks and the record** are exact (section 4.1).
- **Momentum and the margin keep a job together.** The rejected KDP book got cycles #175 and #176 in a row and keeps
  the next one.
- **0.35.3's ladder works among the products.** A live product's first pins (9.4) now come before the critic's
  suggestions (5.4 at most). The morning of 10-09, when cycles #173 and #174 took line #3's critic fixes over its
  pins, can't repeat in that form.
- **Ventures no longer force cycles** (0.36.0). The brainstorm step is near the bottom at 1.6, so "nothing new" holds
  by weight. Not by code: you haven't used **Hold new things**.

### 4.11 What the numbers have been tested on: code, live

0.34.0 promised that "a week of real cycles can tune the weights before the tree steers". Git shows:

| When (UTC) | Version | What changed |
|---|---|---|
| 10-08 02:53 | 0.34.0 | the tree in the shadow |
| 10-08 06:19 | 0.35.0 | the tree steers |
| 10-08 08:14 | 0.35.1 | the weights' rules |
| 10-09 12:12 | 0.35.3 | the weights' rules |
| 10-09 13:52 | 0.36.0 | the weights' rules |
| 10-09 15:17 | 0.37.0 | the weights' rules |

On the device (the report's events):
- the shadow ran from 04:45 (0.34.0) to 06:57 (0.35.0) on 10-08, about two hours;
- cycles up to #175 (10:33 on 10-09) ran on 0.35.1;
- 0.35.2 (13:53), 0.35.3 (14:34:09) and 0.36.0 (14:34:25) were installed but ran no cycle;
- 0.37.0 started at 15:59.

So 0.37.0's numbers have one live pick behind them. Each change fixed what the day before showed, which was right,
but nobody has seen the numbers at work over a week. The test of PROMISE_WORTH uses a worth no product has (section
4.3).

---

## 5. What I recommend

### 5.1 For you, now

1. **Pin the launch pin steps on the Plan tab:** steps 26 (line #4), 56 (line #6), 71 (line #7), 87 (line #8) and 12
   (line #3).
   - A pinned step is taken before any weight, one a cycle, until its check passes (two pins live).
   - Approve the pin requests as they come: pending publish requests are capped at 3, shared with posts and blog
     posts.
   - Unpin a step to stop it.
2. **Tell Ember to fold the duplicates:**
   - #28 into #20;
   - #43 and #45 into #46;
   - close #44 once #46 stands for it.

   Each is a step worth 15 or more.
3. **Use Hold new things** if "nothing new" still holds. Your rulebook's words don't stop the brainstorm step; the
   hold does. One gap: a new product line that names an existing, unparked venture passes the hold
   (`tools.py:2137-2146`).
4. **Use Freeze until 10-20** for "no title or tag edits". The report's `plan_freezes` is empty, so only the
   rulebook's words say it. In cycle #176 the critic asked for a new title and tags on listing #4584852908.
5. **Check the SFTP login** (`blog_sftp_user`, `blog_sftp_password`). Your web host has refused it since 14:08 Berlin
   time (12:08 UTC; the live view's uploads). The next approved blog post would fail the same way.
6. **Correct Ember on message #176:**
   - the shop is ETAIShop;
   - the Bluesky handle is the one in your options (`bluesky_handle`), shown on the Bluesky card;
   - the ventures parked this week were #22 to #27 (Pinterest variants and a cross-sell page).

   It saved the wrong belief as a lesson ("[#c176] Never had a real Bluesky handle saved…").
7. **Listing #4584852908's text promises more than it delivers.** It says every file comes as Word too, and that an
   "Ausfüllanleitung" is included. The critic found neither (check #35). Fix the files or the words.
8. **Bring back what you still mean of standing instructions #5 and #6.** They dropped out when #7 replaced them:
   - "my settings' caps are your only spending limits";
   - "Write to me at most once a day … No apologies";
   - "Every product must beat what a free AI chat gives".

   Only #7 became a rule.

### 5.2 The weights

In order of what they change:

1. **Weigh a promise by what it serves, and let its urgency start near its day.** For example:
   - `PROMISE_WORTH` ≈ 3;
   - the floor of 2 only within 2 days of the due day, nothing before.

   With today's tree that gives:
   - the KDP redo (due tomorrow) 12.0;
   - then the launch pins and posts 9.4;
   - then the reports due in 2 days 9.0;
   - the later reports about 3, until their days near (reproduced).

   On a promise's day it still weighs 3 × (1 + 9) = 30. That is what 0.37.0's changelog describes.
2. **After `PROMISE_TRIES`, drop a promise below the products** (urgency 0, or the product's worth), not to the
   floor. Then a promise Ember can't keep yet stops taking every cycle.
3. **Weigh report promises as reports** (kind 0.3), or ready only from the day before they are due. Let Ember make a
   promise wait on a day up to its due day.
4. **Fold repeats by product and intent, not only by words.**
   - A new promise of the same line due within 2 days of an open one replaces it, and the old one closes as "replaced
     by #n".
   - `promised_line` should also match a product's title words ("Haushaltsbuch" → line #14).
5. **Give worth something to say.** For example:
   - `could_earn` from the listing's price × a cautious sales rate, or from the venture's case;
   - or ask you once for each product's worth.
6. **Line #12:** move listing #4587912058 (request #35) to line #12. The keeper then re-types it as an Etsy download
   with its own launch steps.
7. **`channels_from`:** the blog needs `blog_enabled` only, as in the loop.
8. **Audience:** from the listings' language, or yours to set (Release 2c planned that).
9. **The test of 0.37.0's intent** should use the worths the templates give (2.0). It fails today, which is the
   point.
10. **Docstrings:**
    - `weights.py:10-21` and `plan.py:18-23` still describe the 0.35.1 order ("taken first");
    - OBLIGATIONS still says "deal with them first" (`obligations.py:49`).

### 5.3 The report

1. Each pick's app version (a column on `plan_picks`, or the cycle's version beside it).
2. Every ready candidate, or at least the heaviest step of each line, with its parts. Keep more than 6 in
   `plan_picks.ranked`.
3. The channels' factors, and each product's `live_since`, `decide_by` and `pushed_until`.
4. Remove `date_urgency()` and `DATE_SCALE`, as `9c80509` removed `OWN_DATE_CAP`.

---

## 6. Elsewhere in the code

Found in the survey and checked against the code. None of them changes what section 4 says.

1. **The daily cap is spent by midday** (live, code). Every sleep is cut to 30 minutes while a step is ready
   (`loop._cut_sleep`, `slack.py`), and one always is: a brainstorm counts, and so does a promise Ember can't keep
   yet. So Ember worked through the night:
   - 21 cycles between midnight and 12:35 on 10-09 (#155 to #175), Berlin time;
   - 10 of them 0.35.1's venture cycles that did nothing, $1.55;
   - $5.88 of the $7 spent by 12:35.

   The 20:00 event reserve then held the rest. With `wake_on_message` off, your messages #174 and #175 (12:42)
   waited until you pressed Wake now at 17:59. Ember spends the day's money while you sleep, and has none left
   when you write during the day. (Approvals are carried out by every scheduler round, cycle or not.)
2. **Message #176 was wrong where its context was silent** (live, code).
   - It called "ETAIShop" a placeholder, though the planner's ETSY SHOP section says "Shop: ETAIShop.".
   - It said the handle was never saved. Ember's code shows the handle only in BLUESKY's first line
     (`loop.py:786-789`, `2449-2453`), and an ordinary cycle doesn't carry that section ("Pins, Bluesky posts and blog
     posts belong to marketing cycles").
   - It explained the parked ventures with ones that don't exist.

   An owner-woken cycle that answers you gets the line's context, not the facts your question is about.
3. **The KDP book's release stage keeps "Propose the book" done after both rejections** (live, code). A closed step
   never reopens. YOUR PLAN says "release, 2 of 4 steps done", and step 122 asks you to publish a book you
   rejected. By design your decision (#44) carries the reaction, but the tree reads as further along than it is.
4. **A promise orphaned when its product closes** (code). `_close_product` closes every open step under the product,
   promise steps included (`plan.py:879-892`). The obligation stays open. The keepers then skip any obligation that
   already has a node, whatever its state (`plan.py:982-984`, `1131-1133`, `1032-1034`). So it is never a step again
   and lives on in OBLIGATIONS only.
5. **The consolidation marks plain words as tools** (code). `_consolidate` passes every tool name (`loop.py:1461`).
   So a lesson with numbers that says "research" or "workshop" counts as a tool lesson and loses its protection
   (`memory.py:439`). `memory.identifiers` exists to avoid exactly that (`memory.py:471-473`). Your LESSONS already
   show "(has numbers, tool)" on workshop lessons.
6. **A website Undo waits while Ember is paused or unfunded** (code). The other undos run then
   (`service.py:997-1005`). `audit.UNDO_WHILE` promises all of them.
7. **Hold new things lets a product line through when it names a venture** (code). `project_create` refuses only
   without a `venture_id`, and `_working_venture` refuses only parked ones (`tools.py:2137-2146`, `2388-2399`). An
   idea's venture is enough.
8. **Stale texts:**
   - `tools.py`'s module text says no tool reaches the network (the Printify cost probe does, as documented);
   - `README.md:118` links `DOCS.md#roadmap`, which is now "Plan";
   - the vision documents still describe READY.

---

## 7. How I checked

**The picks.** I copied the report's `plan_picks` rows and recomputed each weight from its own parts.

**The ranking.** I rebuilt the candidates from the report's `plan_nodes`, `etsy_listings`, `pinterest_pins`,
`bluesky_posts`, `quality_checks` and the cycle headers, following `plan.candidates`. I ranked them with the
repository's `weights.py`, loaded as is. Two inputs are not in the report: whether a product missed a views bar, and
the Bluesky factor. I took the neutral value and checked that the worst case (every launch line with a missed bar)
changes no conclusion: the pins rise to 11.4, still under the promises' 15.

**The what-ifs** (the next picks, and `PROMISE_WORTH` 3 with the floor near the day only) use the same rebuilt state
with one setting changed. They show direction, not tuned numbers: the full test suite and a week of picks should
settle the numbers.
