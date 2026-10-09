# Ember 0.37.0: does it do what it is meant to? A full codebase analysis

*The codebase at `9926457` (0.37.0, the head of `claude/elegant-faraday-tcl6tq`, which Home Assistant installs from),
2026-10-09. It measures every part of Ember against what `ember/DOCS.md`, the CHANGELOG and the READMEs promise, and the
whole against Ember's purpose. A companion analysis of the same version, on the branch
`claude/ember-codebase-plan-weights-2394w4` (`analysis-0.37.0/ember-analysis-0.37.0.md`, not merged), reads your live
diagnostics report of 2026-10-09 against the plan's weights. This one doesn't repeat it, and cites it where the live
numbers matter.*

**Evidence.**
- **Nine audits**, one per part of the code, each reading its code against its sections of DOCS.md and reproducing
  what it found with scripts against the real code:
  - the money guard;
  - the wake cycle and its scheduling;
  - the plan tree;
  - the tools, the file jail and the workshop;
  - ventures and the learning loop;
  - approvals, mail and unlocks;
  - the platform integrations;
  - the web app, security, options and the database;
  - the products buyers get.
- **My own runs:**
  - the full test suite and lint;
  - the server started as the README says;
  - three simulated weeks of dry run (548 cycles), driven the way the scheduler drives them;
  - a reproduction of the plan's loop with no model involved;
  - the caps checked on every simulated day;
  - a break-even from Ember's own fee model;
  - CI on GitHub.
- **Every high and medium finding was re-run by me, and all of them reproduced (✔).**
- **I had no live database.** Live numbers come from the companion analysis or the earlier analyses, and say so.

**Labels**, as in the earlier analyses:
- **reproduced:** a script against the real code showed it;
- **code:** found by reading the code;
- **live:** in a diagnostics report (here, the companion analysis's reading of yours);
- **inferred:** from indirect evidence.

**Paths** are relative to `ember/app/` unless they start with `tests/`, `repro/` (short for `analysis-0.37.0/repro/`,
where the reproductions are: section 10) or the repository root.

---

## 1. The short answer

- **As software, Ember is solid.**
  - 2,855 tests pass with warnings as errors, and lint is clean.
  - CI is green on the released commit.
  - The app starts and serves its dashboard.
  - 548 simulated cycles over three weeks ran without one exception.
  - Fuzzing every route with bad input gave no server error.
- **The guarantees that protect you hold, with one rare exception.**
  - No simulated day or cycle went over its cap, and no call cost more than its priced worst case.
  - Nothing leaves the container without your approval or a covering unlock, and the database itself refuses
    anything else.
  - Each send is recorded before it starts and never repeated.
  - Access only through Ingress, your owner identity, CSRF, the CSP and your secrets all hold.
  - Everything Ember publishes says that an AI wrote it.
  - The eight unlock problems and the safe-mode problem of the 0.13.0 analysis are all fixed.
  - **The exception:** near the bottom of the balance, a costly workshop run can still take Ember below zero without
    its last will (3.4).
- **What Ember works on is where it falls short.**
  - The plan tree has steered every cycle since 0.35.0, but it can't tell a step a cycle advanced from one it can't
    advance:
    - a step waiting for you, a channel's limit, or a promise Ember can't keep yet is taken again every 30 minutes
      until the daily cap is gone;
    - the other products starve (3.1);
    - live on 10-09, it had spent $5.88 of the $7 cap by 12:35.
  - A product whose listing request expires or is rejected stalls for good (3.2).
  - On a new install, Etsy research stops after $0.60 (3.3).
  - Several of your controls on the plan can be got around (4.1).
- **Buyers can get wrong numbers.**
  - A spreadsheet that sums a whole column counts its own total twice.
  - The picture Ember checks its work with, and lists, shows the right number (3.5).
  - The cost statements, by contrast, matched an independent recalculation in all 73,600 cells checked.
- **At the edges, your control has gaps, and work can be lost.**
  - After a kill-switch reset or a restart, approved emails go out before a "stop" waiting in the mailbox is read.
  - The kill switch doesn't stop a round that is already sending.
  - Ember can overwrite the files a waiting request depends on, so your approval then fails.
  - After an in-place fix, the Undo of a listing's file change can't work (4.1, 4.2).
- **As a business, it doesn't deliver yet.**
  - Live: $60 spent, $0 earned, 13 views in eleven days.
  - At its current burn, Ember needs roughly 5 to 40 sales a month to break even. That takes 4 to 100 times the reach
    it has.
  - Reach is not a code problem. But the loop in 3.1 spends the money Ember needs to find it.
- **How Ember is built is now a risk of its own.**
  - 20 minor versions in 7 days.
  - The plan tree steered 3.5 hours after 0.34.0 announced a week in the shadow to tune it.
  - The tests treat "a step taken" as "a step done".
  - Most fixes in this report are small. Slowing the releases down, and testing the steering over simulated weeks,
    matter more (8.3).

---

## 2. Scorecard: what Ember is meant to do, and whether it does

✔ delivers · ◐ delivers in part · ✘ doesn't deliver

| What Ember is meant to do | Verdict | In short | Where |
|---|---|---|---|
| Pay for itself: earn more than it spends | ✘ | $0 revenue after $60 of API calls. Reach is 4–100 times short of break-even, and the steering wastes the daily cap. | 3.1, 7 |
| Keep its spending within your caps and the balance | ✔ | Every cap held on 21 simulated days. One rare breach near the bottom; two edges. | 3.4, 4.4 |
| Do nothing outside without your approval | ✔ | Enforced by the database. Gaps in timing at the edges, and one unlock is looser than documented. | 4.1 |
| Keep the dashboard yours and your secrets secret | ✔ | Only low findings. The network guard is narrower than the README says. | 5 |
| Say that an AI wrote it, everywhere | ✔ | Ember's code adds the line on every channel. | 9 |
| Wake, plan, work and reflect on a sensible schedule | ✔ | Holds that outlive their reason; event cycles that push the plan back. | 4.4 |
| Decide what to work on (the plan tree) | ✘ | Loops on steps it can't advance, stalls products, and starves new ones. | 3.1, 3.2, 4.3 |
| Let you steer the plan | ◐ | Pin, worth, freeze and keep/drop work. Your product hold, Hold new things and a pin can be got around. | 4.1 |
| Make products buyers pay for | ◐ | Cost statements are exact and documents and KDP covers are good. Spreadsheets can be wrong without a warning. | 3.5, 4.6 |
| Sell through Etsy, Printify, Pinterest, Bluesky and the blog | ✔ | Hosts, tokens, never acting twice and the limits all hold. Four medium defects. | 4.7 |
| Grow new ways to earn (ventures) | ◐ | The numbers and the knock-outs are right. On a new install research is capped; the gates are weaker than documented. | 3.3, 4.5 |
| Learn from what it did | ◐ | Bets, cases and lessons work. The daily review asks for what the tools refuse, and the playbook can count opposites as support. | 4.5 |
| Grow new abilities in the workshop | ✔ | Sandboxed, checked and held. Small gaps. | 4.7, 5 |
| Let you try everything for free (dry run) | ◐ | The whole loop runs. Its shop is optimistic, and it never shows a business case or a pin. | 6 |
| Show you what happens (dashboard, diagnostics, sensors) | ✔ | Only low findings. | 5 |
| Install, update and run safely in Home Assistant | ✔ | Tests, CI, migrations, backups and least privilege all hold. | 9 |
| Tell you the truth in its docs | ◐ | The store text and parts of DOCS describe mechanisms that are gone. | 5 |

---

## 3. The five high findings

### 3.1 The plan has no brake for a step a cycle can't advance — high, reproduced ✔, live

**What is promised.** The plan tree decides each cycle: "A step you pinned; else the heaviest step… To leave the
product it is on, another step must weigh 25% more, for up to three cycles in a row" (DOCS, Plan). "While your plan has
a step ready, every cycle sleeps your owner's shortest sleep at most… Your owner's daily cap is the only brake on
spending" (CHANGELOG 0.35.1). For promises 0.37.0 adds one brake: "one you took three times in a day without keeping it
weighs less until the day is over".

**What happens.** Nothing in the tree notices that a cycle took a step and nothing changed.
- A step stops being ready only when Ember says it waits (`plan_step wait`, at most `WAITS_A_DAY` = 2 a day for the
  whole plan), when you hold its product, or when its channel isn't set up (`agent/plan.py`, `_waiting`). A step whose
  own requests wait for your approval is still ready: "Pin it twice" closes only when two pins are **live**
  (`agent/templates.py`, `CHECKS`), so the two pins waiting for you don't count.
- An ordinary step's weight is worth × kind × channel × (1 + urgency + age + momentum), whatever the last cycle on it
  did. Taking it resets its age; a live product's launch marketing keeps urgency 3.
- Only an owed step (a promise or your decision) has a brake (`plan._owed_urgency`), and it stops at a floor of
  5 × (1 + 2) = 15, above every product step on a real install (no product step passes about 12).
- "Up to three cycles in a row" ends momentum, not the streak: after three cycles the same step needs no margin and
  still weighs most.
- Any ready step cuts every cycle's sleep to your shortest sleep (`agent/loop.py`, `_cut_sleep`; `agent/slack.py`). So a
  stuck step runs at 48 cycles a day (30-minute sleep) until the daily cap stops it.

**Evidence.**
- **With no model at all** (`repro/e2e/stuck_step.py`): two Etsy products; #1 live, its two pins proposed and waiting
  for you; #2 new. The real `plan.keep`, `plan.steer`, `plan.record` and `slack.sleep` for 16 cycles 30 minutes apart:
  - all 16 take "Pin it twice, with different pictures" on #1, weight 8.02 = 2 × (1 + 3 + 0.01), each 240-minute sleep
    cut to 30;
  - #2's first step weighs 0.93; at 0.5 a day of age it would need about 23 days to win one cycle.
- **A promise** (`repro/plan/repro_promise_floor.py`, `sim_mix.py`): a promise due in three days took 30 of 30 hourly
  cycles, its weight rising from 15.5 to 18.0 against at most 1.3 for anything else; one Owner promise due in four days
  took all 36 cycles of a simulation that otherwise gave 30 to ventures and 6 to products.
- **A promise of pins that names no single product** is an Owner step with no channel (`plan._owner_owed` sets none),
  so its cycle is ordinary, has no line and doesn't carry `propose_pin` (`repro/plan/repro_owner_pin_promise.py`): it
  can't be kept, and by the floor above it takes every cycle.
- **In the dry run** (section 6, run B): 316 of 322 cycles took one Pinterest step for a week, $14.05, no pin made; the
  second product never left research.
- **Live** (companion analysis, sections 4.3 and 6.1): on 10-09 the sleep cut ran 21 cycles between midnight and 12:35
  Berlin time, $5.88 of the $7 cap; your messages at 12:42 then waited 5 hours 17 minutes. None of 24 recorded picks
  went to a launch pin.

**Why the tests don't see it.** `tests/test_fixes_0353.py` replays a day by removing each step once a cycle takes it
(`left = [s for s in left if s.id != pick.step.id]`): taken means done. In Ember's records it doesn't, and the test of
0.37.0's intent (`tests/test_plan_weights.py:88-98`) uses a worth of 9, which no product on a real install has
(companion, 4.3).

**0.37.1 in progress** (commit `c044261` on the companion branch, CI red) lowers promises below the launch pins. That
fixes the promise case and makes the launch-pin case more likely: the pins then win whether or not they can be made.

**Fix (M).** One rule for every step: a step a cycle took without its check moving (or with its own requests still
waiting for you) waits until something changes: your decision, the check's numbers, the next day. A step whose
requests wait for you waits on them by itself, without using one of Ember's two waits. A pick that made no progress
last time doesn't cut the sleep. Set a promise's urgency to 0 after its tries (as 0.37.1 does) and give an Owner
promise of pins, posts or a blog post its channel.

### 3.2 A product whose listing request ends without going live stalls for good — high, reproduced ✔

**What is promised.** "Live, a rejected listing left its product waiting for an approval that never came; now the
rejection itself is the next step" (CHANGELOG 0.35.0, `plan._decisions`). Stages close on their checks (DOCS, Plan).

**What happens.** An Etsy product's release stage has two steps: "Propose the listing" (its check: a request) and "You
approve it, and it goes live" (yours: a live listing) (`agent/templates.py:126-127`). The first closes once a request is
made, and a closed node never reopens (`agent/plan.py:448-459`, `929-930`). If that request then **expires** (7 or 30 days),
is **withdrawn**, or is **rejected or fails** and Ember closes your decision's step without proposing again:
- nothing reopens "Propose the listing";
- "You approve it" waits on you, though nothing waits for you (`agent/plan.py:1594`);
- the launch steps wait behind the release stage;
- so the product has no ready step, and the tree never steers a cycle to it again. You can't unstick it either: your
  pin can't take an owner step, and pinning a later step does nothing.

YOUR PLAN and the Plan tab then show "Waiting on your owner: You approve it, and it goes live", with no request
pending.

**Evidence** (`repro/plan/repro_stalled_release.py`, for expired, withdrawn and rejected alike):
`ready steps of the tracker: []`, `cycles on the tracker in the next 10: 0`, and YOUR PLAN listing the approval that
isn't there.

**Fix (M).** In `keep`, when a stage still needs a request and none of that executor is pending or approved for the
product, lay out a code step "Propose it again" whose check is a request made since then.

### 3.3 On a new install, the seeded Etsy venture caps all Etsy research at $0.60 — high (new installs), reproduced ✔

**What is promised.** The Etsy leg is "your first leg"; `agent/stages.py:9`: "the seeded Etsy leg sits in researching
until its first listing"; `agent/ventures.py:52`: it "starts live by Ember's code". Venture cycles are for new ventures
(DOCS, Ventures).

**What happens.** On a new install, and in every new dry-run session, `ventures.seed` plants "Etsy digital products" as
**researching** (no listing is live yet), and no code ever moves it on: the agent can set it live only from building
(`agent/tools.py:2661`). Then:
- every Etsy product line joins it when it asks for a listing (`agent/tools.py:4135`; Printify lines join "Print on demand"
  the same way, `agent/tools.py:4809`);
- research in a cycle on such a line is charged to that venture's $0.60 budget and refused once it is spent
  (`agent/tools.py:3645-3655`): "venture #1 has used its research budget ($0.62 of $0.60): decide it now: its business case
  (stage proposed) or parked";
- a demand note needs an independent page from research (`agent/demand.py`, `source_problem`), and a product line's
  first listing needs a demand note at most 14 days old: so after about a dozen research calls, new Etsy products can't
  reach their first listing;
- meanwhile the plan keeps a venture step "Research its next question; then its business case, or park it" for the
  shop itself, urgent for good from day 14 (`agent/desk.py:103-106`; the park it announces never comes while a line is open,
  `agent/stages.py:543-548`).

**Evidence.** `repro/ventures/r8_etsy_leg.py`: `venture #1: Etsy digital products | stage: researching`, then
`research on line #1 -> REFUSED: venture #1 has used its research budget ($0.62 of $0.60)`. In all three of my
simulated weeks venture #1 stayed researching.

**Who it affects.** New owners, and every dry run. Your own install had no venture being explored on 10-09 (companion,
2.6), so it was most likely seeded live. **Until it is fixed:** press **Research more** on venture #1's card, or add a
keyword export to the Library.

**Fix (M).** Set a channel's own venture live once one of its listings is live (or plant it live), and keep channel
ventures out of exploring, out of the line-research budget and out of the plan's Ventures.

### 3.4 Near the bottom of the balance, a workshop run can still kill Ember below zero without its last will — high (rare), reproduced ✔

**What is promised.** After live call #423 (a workshop run billed at 5.3 times its quote), 0.21.0 made a workshop or
research call need "5 times what it keeps back… left above the last will's reserve", so that Ember wouldn't "die below
zero without its last will, your Anthropic account paying the rest" (DOCS, Money; the run keeps back the most of its
cap per run, its raised estimate and 1.5 times the costliest recent run, DOCS, The workshop).

**What happens.** 0.33.0 made a run keep back "no more than the day has left" by clamping its hold to the money room
(`agent/workshop.py:288-293`, `economy/metering.py:723`). But that room already contains the balance's share for
server tools, (balance − reserve) ÷ 5 (`economy/metering.py:910`). So near the bottom the hold shrinks to a fifth of what is
left, and the 5× check, run on the shrunken hold (`economy/metering.py:974`), passes by construction once the bare quote fits.
What recent runs cost no longer counts.

**Evidence** (`repro/economy/e13_hold_clamp_5x.py`): a run like #423 has cost $1.76, so the documented hold is $2.64,
and with $3.10 left above the reserve the rule needs $13.22: refuse. The guard alone, called as the 0.21.0 regression
test calls it (without `hold=`), refuses. The workshop's own path admits the run with a $0.62 hold; the run costs
$3.21 (5.5 times its quote); **Ember dies at −$0.08 with no last will**. The test that guards this
(`tests/test_fixes_0140_money_guard.py::test_a_run_like_423_near_the_bottom_is_refused_rather_than_kill_ember`) never
takes the production path.

**How likely.** Only near the end of a life, and only with a run that overruns its quote several times, as #423 did.
The loss beyond the balance is one run's overrun. It is the one money guarantee I found broken.

**Fix (S).** Clamp the hold by the day's room only (daily cap − today − pending − event share − keep), run the 5× check
on the unclamped hold, and make the 0.21.0 test go through `Workshop.run`.

### 3.5 A spreadsheet that sums a whole column counts its total twice, and the picture shows the right number — high when it happens, reproduced ✔

**What is promised.** `make_spreadsheet` makes the file "plus a picture of each sheet", and its Check line names a
formula that leaves data out or points at a title, a header or an empty cell (DOCS, Products; 0.32.0). Pictures "show
what formulas work out to (… sums of other sheets)" (0.19.2). Live, buyers already received a Summary that left rows
out (0.32.0 analysis, 2.5).

**What happens.** Ember adds a Total row under each sheet's data, in the same columns. A formula over a whole column
(`=SUM(Income!C:C)`, `=AVERAGE(B:B)`, `=COUNTA(A:A)`, a share `=B4/SUM(B:B)`) includes that Total row (and the header)
in Excel and LibreOffice. Ember's own evaluator treats `C:C` as the data rows only (`products/sheets.py:1204-1205`,
`_range` 1019-1035), so the picture make_spreadsheet makes, the one the agent looks at to check its work and puts on
the listing, shows the right number. The Check line looks only at `C4:C9`-style ranges (`products/sheets.py:289-304`) and says
nothing.

**Evidence** (`repro/products/sheets_wholecol.py`, `share_of_total.py`, recalculated by LibreOffice 24.2):

| Cell | In the buyer's file | In Ember's picture |
|---|---|---|
| Income `=SUM(Income!C:C)` | 7,141.00 € | 3,570.50 € |
| Spent `=SUM(Expenses!C:C)` | 2,773.00 € | 1,386.50 € |
| Items `=COUNTA(Expenses!A:A)` | 4 | 2 |
| Shares `=IFERROR(B4/SUM(B:B),0)`, summed | 50.0 % | 100.0 % |

No Check line in either case. Language models write whole-column references often, and Ember's own 0.19.2 tests use
them (with SUMIF, which happens to be safe; SUM, AVERAGE, MAX and COUNTA are not).

**Fix (S–M).** Evaluate whole columns as Excel does, or draw make_spreadsheet's pictures from the built file
(`sheets.picture`, which make_image already uses and which shows the doubled values); and name, or refuse, a
whole-column reference inside an aggregate on a sheet that has totals. **Until then**, add a rule to your rulebook:
"never use whole-column references such as C:C in formulas".

---

## 4. Medium findings

Each was reproduced by its audit and again by me (✔), unless it says otherwise. Fix sizes: S (a day or less), M (a
few days), L (more).

### 4.1 Your control over Ember

**4.1.1 After a kill-switch reset or a restart, approved emails go out before a "stop" waiting in the mailbox is read**
— reproduced ✔
- **Promised:** "Whoever asks not to be emailed again is never emailed again" (DOCS, Ember's mailbox); every footer
  says "Reply 'stop'".
- **What happens:** a scheduler round runs the unlocks, then the executor, and only later reads the mailbox
  (`agent/scheduler.py:93-109`). The mailbox isn't read while the kill switch is on (`service.check_events` is
  blocked), while the app is down (every update), or for up to 4 hours while reads fail. The executor checks
  suppressions only against mail already stored, and never asks how fresh the last read is.
- **Evidence** (`repro/outside/r6_stop_before_send.py`, live mode with fake IMAP and SMTP): you approve a reply to Ann
  and press the kill switch; Ann writes "Stop. Please don't email me."; you reset the switch; the first round sends to
  Ann, and only then is her stop read.
- **Why it matters:** advertising email without consent is illegal in Germany (§7 UWG), and a stop is the one signal
  Ember must never miss.
- **Fix (S):** before sending, read the mailbox if the last successful read is older than a few minutes or older than
  the approval; hold sends while reads fail.

**4.1.2 The kill switch doesn't stop a round that is already sending** — reproduced ✔
- **Promised:** the kill switch "stops the agent for good"; "that stops everything Ember's code sends" (DOCS, Your part).
- **What happens:** `Agent.execute_approved` checks once, before the round (`agent/service.py:997-1008`); the executor
  and the publishers never check again.
- **Evidence** (`repro/outside/r1_kill_mid_round.py`): three approved emails; you press the kill switch after the first
  goes; 2 of 3 are sent after the kill. The live page also keeps uploading every 15 minutes while the switch is on
  (code).
- **Fix (S):** check the killed flag inside each item's transaction and before each publisher's item; decide whether
  the live page stops too.

**4.1.3 An unlock sends a held email reply after it should have expired** — reproduced ✔
- **Promised:** "A request you don't decide expires (emails and posts after 7 days…)" (DOCS, Your part).
- **What happens:** requests expire only at a cycle's start (`agent/loop.py:367`), but each round runs the unlocks and
  the executor first, and `policy.run_due` never looks at the expiry.
- **Evidence** (`repro/outside/r2_expired_veto.py`): a reply held in its veto window, Ember paused for 8 days; on
  Resume the 8-day-old reply is approved by "Ember's code (your unlock)" and sent in the same round.
- **Fix (S):** expire requests at the start of `run_policy`, and restart a veto window after a pause.

**4.1.4 The `qa_fix` unlock can replace every photo of a listing** — reproduced ✔
- **Promised:** "QA fixes: up to 5 distinct photos (no copies)" (DOCS, Your part); the code's own docstring: a change
  that "only brings a live listing up to" 5 photos.
- **What happens:** the rule checks only that the listing has fewer than 5 and the edit at least 5
  (`agent/policy.py:228`); a photo change replaces all of a listing's photos, its search photo included.
- **Evidence** (`repro/outside/r3_qa_fix_bound.py`): against a listing with 1 photo, edits of 5, 7 and 10 new photos
  that keep none of the old ones all qualify as `qa_fix`. Your Undo can reverse it.
- **Fix (S):** require the current photos (by SHA-256) to be kept, and cap the count at the QA minimum.

**4.1.5 Your own hold on a product doesn't stop its promise and decision steps** — reproduced ✔
- **Promised:** "Hold it holds a product yourself: its steps wait until you Resume it" (DOCS, Plan).
- **What happens:** the work steps wait on the hold, the owed ones only on a stopped venture (`agent/plan.py:1688-1689`,
  `1721-1722`, `1765`).
- **Evidence** (`repro/plan/repro_owner_hold.py`): right after your hold on line #4, the next cycle is steered onto
  line #4 for its obligation step, at weight 50.
- **Fix (S):** treat your hold like a stopped venture for owed steps too.

**4.1.6 "Hold new things" lets a new product through when it names a venture, and a venture cycle can start one** —
reproduced ✔
- **Promised:** "Hold new things… no venture cycle and no new product (Ember's code refuses one)"; a new product only
  when no step is ready, in the explore burn mode; a venture cycle only researches and decides (DOCS, Plan, Ventures).
- **What happens:** `project_create` refuses only without a `venture_id` (`agent/tools.py:2140`), checks neither the
  burn mode nor ready steps, and venture cycles carry it.
- **Evidence** (`repro/plan/repro_new_things.py`): with your hold on, `venture_id=3` (an idea) gives "Created project
  #1"; in a venture cycle with steps ready, "Created project #2". (The companion analysis found the same, 6.7.)
- **Fix (S):** refuse while held whatever the venture, in venture cycles, and outside explore.

### 4.2 Work that gets lost or broken

**4.2.1 Ember can overwrite the files a waiting request will upload, so your approval fails** — reproduced (dry run) ✔
- **Promised:** an approved request is carried out exactly as approved (README, Outside actions).
- **What happens:** a request pins each photo and file by SHA-256, and Ember's code rightly refuses to list one that
  changed (`integrations/etsy_publisher.py`, `_uploads`). But nothing stops the agent from changing it while the
  request waits: the makers write any path the agent names (`agent/tools.py`, `_make_image` and the other `make_*`).
- **Evidence** (section 6, run A): for one product, requests #7, #10, #14, #15, #18 and #23 were each proposed with
  `shop/student-budget-spreadsheet-photo-1.png`; a later cycle that night made the photo again; the approval at 07:05
  failed with "changed after you approved it". 9 of 17 listing requests failed this way. The fake model reuses one
  file name; a real one that improves a photo after the critic's verdict does the same.
- **Fix (S):** refuse a write to a path a waiting or approved request holds, naming the request (or write the new
  version next to it).

**4.2.2 Undo of a listing's file or photo change fails after an in-place fix, and the old file is gone** — reproduced ✔
- **Promised:** "Undo … changes a changed listing back" (DOCS, Your part).
- **What happens:** the Undo re-uploads the old files from their workspace paths, checked against their old SHA-256
  (`agent/audit.py:209-222`), but Ember keeps no copy: the edit deleted the old file at Etsy (`integrations/etsy_publisher.py:1088`),
  and the workspace keeps old versions of text files only. The Undo is offered with no reason against it
  (`agent/audit.py:292`).
- **Evidence** (`repro/platforms/repro_undo_inplace.py`): "Undo offered: Change it back | why_not: None", then the
  Undo fails: "changed after you approved it… ask for a new request". The file buyers had before is in neither place.
- **Fix (M):** keep every file and photo Ember's code uploads or replaces in a store keyed by SHA-256 under /data, and
  restore from it; until then, refuse the Undo in `_why_not` when its files can't be read with their hashes.

**4.2.3 Rewriting the blog's list silently drops your own entries that aren't exactly in the template's form** —
reproduced ✔
- **Promised:** "the list as it is on your server, with the post added (posts you uploaded yourself stay listed)… If
  the list isn't in the blog template's form, nothing is uploaded and the request says why" (DOCS, Blog).
- **What happens:** only a missing `<ul class="post-list">` stops the upload. An entry with a relative or absolute
  link, a date written as text, or a file name with capitals or an underscore is dropped without a word
  (`products/blog.py:80`, `679`, `711`). The Undo re-renders the current list, so they stay gone.
- **Evidence** (`repro/platforms/repro_blog_list.py`): Ember's code reads 1 of 5 entries on the server; after the
  upload the other four are gone from your public blog, and the Undo doesn't bring them back.
- **Fix (S):** count the list's `<li>` items it couldn't read and refuse with their text; let the Undo put back the
  list's `before` bytes while the list is unchanged since.

**4.2.4 The safety net for overwritten text has holes** — reproduced ✔
- **Promised:** Ember's code keeps the text a file held before an overwrite, edit or delete ("its newest 5"), and
  `restore` brings it back (DOCS, Products; 0.33.0).
- **What happens:**
  - `restore` always takes the newest kept text, after saving the current one as a new version
    (`agent/tools.py:2037-2054`): two restores toggle between the two newest texts, and after four the original is
    pushed out of the five (`repro/tools/rt_test_workspace.py`: "after restore 4: file=V2 kept=['V1', 'V2', 'V1',
    'V2', 'V1']");
  - a draft's overwrite and a workshop output saved over an existing file keep no earlier text at all
    (`agent/tools.py:2791`, `agent/workshop.py:372-376`).
- **Fix (S):** let restore step back past the version it just restored (or take a version number), and keep versions
  for draft and workshop overwrites.

### 4.3 The plan's lifecycle

**4.3.1 A launch step of a channel you don't use never closes, so the product never gets recurring marketing** —
reproduced ✔
- **What happens:** a product's launch steps are laid out by its audience only (`agent/templates.py:337-339`); the step
  of a channel that is off waits on "channel" for good; the launch stage is done only when all its steps are
  (`agent/plan.py:940-945`), and recurring steps need every stage before maintain closed (`agent/plan.py:1522-1523`).
- **Evidence** (`repro/plan/repro_channel_off_launch.py`): with Pinterest off, the launch stays open and after four
  weeks there is no recurring step in any channel; with all channels on, the pin and Bluesky steps come. The tests
  close launch steps by force (`tests/test_fixes_0340.py`).
- **Fix (S–M):** lay out (or count) launch steps only for channels in use, and add one when a channel is switched on.

**4.3.2 A channel's own product gets recurring steps it can never pass, and they take its marketing cycles** —
reproduced ✔
- **What happens:** `_recurring` excludes only KDP (`agent/plan.py:1523`); a channel product ("Pinterest account") has no
  listings of its own to link, and linking another line's is refused.
- **Evidence** (`repro/plan/repro_channel_recurring.py`): every cycle for 21 days is a marketing cycle on "This week's
  pin for Pinterest account"; all three recurring steps stay open.
- **Fix (S):** no recurring steps for the channel template.

**4.3.3 On a new install, unresearched ideas outweigh real products** — reproduced ✔
- **What happens:** a venture's step carries no stage chance (`agent/plan.py:678-692`): an unresearched idea is worth 2 ×
  0.8, while a $5 product in research is worth 0.8 (in create 1.2).
- **Evidence** (`repro/plan/sim_ventures.py`, the seeded tree plus three new products, 7 days): the first ranking is
  seven venture steps at 2.40, then KDP at 1.27, Etsy and Printify at 0.80; 65 of 84 cycles are venture cycles; the
  Etsy and Printify products get their first cycle on days 3.2 and 3.4. (My run C: 86 of 126.)
- **Fix (S):** scale a venture's worth by a chance for its stage (idea about 0.4, researching 0.5, proposed 0.6).

**4.3.4 The keeper treats the blog as off unless Website is on** — code ✔, live (companion, 4.8)
- **What happens:** `plan.channels_from` needs `blog_enabled` **and** `site_enabled` (`agent/plan.py`, end of file); the
  loop's `blog_on`, which steers, needs `blog_enabled` only (`agent/loop.py:264`, `532`). DOCS says "Website itself can stay
  off".
- **Effect:** with the blog on and Website off (your install), no monthly blog-post step is ever added, the blog
  steps' clock is reset at every keep, and the Plan tab says they wait while cycles take them.
- **Fix (S):** drop `site_enabled` from `channels_from`.

### 4.4 When Ember wakes, and what it pays for

**4.4.1 Wake holds outlive their reason** — reproduced ✔
- **What happens:** the 20:00 reserve, "waiting for the daily cap to reset", maintenance's one cycle a day and dormant
  are stored as a bare next-wake time and reason (`agent/service.py:418-446`, `549-563`, `884-887`) and never checked
  again.
- **Evidence** (`repro/loop/r8_stale_holds.py`):
  - you switch event wakes off (no reserve any more): Ember still waits until 20:00;
  - you raise the daily cap from $5 to $10: it still waits for midnight;
  - a $100 grant moves the burn mode to explore: it still waits about 24 hours for "maintenance: one cycle a day".
- **Fix (M):** store each hold's kind, and re-check its premise in `_decide_schedule`.

**4.4.2 Event cycles don't get the sleep cut, and push the plan's next cycle back** — reproduced ✔
- **Promised:** "While your plan has a step ready, every cycle sleeps your owner's shortest sleep at most… whatever you
  choose" (0.35.1).
- **What happens:** an event (reactive) cycle isn't steered, so it never counts as busy and keeps the sleep it chose
  (`agent/loop.py:403`, `890-891`), and every cycle's sleep sets the next wake (`service._after`).
- **Evidence** (`repro/loop/r1_reactive_sleep.py`, `sim_days.py`): a scheduled cycle's 360 minutes are cut to 30, an
  event cycle's aren't; two events in the morning moved the plan's next cycle from 10:02 to 12:40.
- **Fix (S):** cut event cycles too, or keep the earlier of the pending wake and the event cycle's.

**4.4.3 An event wakes Ember when the day can pay for its plan but not for any work** — reproduced ✔
- **What happens:** `_event_wake` checks only the plan's cost (`agent/service.py:474-477`); the scheduled path needs a working
  cycle's (`430-434`), with a comment saying why.
- **Evidence** (`repro/loop/r2_event_no_room.py 0.09`): $0.088 left; no scheduled wake until midnight, but an email
  wakes an event cycle that pays $0.011 for its plan and is refused, then backs off. Up to 4 times a day.
- **Fix (S):** use the scheduled path's bound.

**4.4.4 A reply a cycle already showed wakes another cycle** — reproduced ✔
- **What happens:** the agenda's reply branch lacks the "came in after the last cycle ended" guard its inquiry branch
  has (`agent/agenda.py:130-135`); mail is fetched at a cycle's start, but the agenda notes only between cycles.
- **Evidence** (`repro/loop/r6_reply_seen_then_wakes.py`): cycle #5's MAIL shows Ann's reply; afterwards the agenda
  notes it as new and it wakes an event cycle. That costs a cycle and one of the day's four event wakes.
- **Fix (S):** apply the inquiry's rule to replies.

**4.4.5 The reflection is skipped after a slow last step** — reproduced ✔
- **Promised:** a cycle keeps enough of its cap for its reflection, which may go over by what a cache miss would add,
  "so it always runs" (DOCS, Money).
- **What happens:** the reserve is priced with a warm cache (`agent/loop.py:1907-1917`), but the cache counts as gone 240
  seconds after the last work call (`economy/metering.py:834-849`) and then the miss allowance is 0 (`economy/metering.py:1084-1094`).
  A workshop run or a few research calls in the last step take that long.
- **Evidence** (`repro/economy/e01c_reflection_last_step_slow.py`): the same cycle reflects; with a 5-minute last step
  it doesn't, and still closes "completed". At the default caps the reflection costs $0.051 warm and $0.149 cold
  against a $0.062 reserve.
- **Fix (S):** base the miss allowance on the last work call's prompt even when the cache is judged gone.

**4.4.6 A provisional worst-case charge makes Ember critical or lowers its burn mode, and your correction can't undo
it** — reproduced ✔
- **What happens:** a call cut off by a restart (every update) or a broken stream is charged at its worst case until
  you correct it. Death is judged on the settled balance, but the runway, entering critical and the burn mode use the
  charged one (`economy/life.py:311-352`); your correction is a refund, which isn't money in (`economy/ledger.py:43`), so
  neither change comes back (`economy/burn.py:200`).
- **Evidence** (`repro/economy/e09_provisional_charge_critical.py`): $5.80 and 2.3 days of runway; the app restarts
  during a workshop run (hold $0.75); Ember is critical, its last will falls due; your correction to the real $0.10
  leaves it critical; once the will is written, it goes dormant. With *conserve*, a correction left it in maintenance.
- **Fix (M):** judge critical and the burn mode on the settled figures, as death is.

### 4.5 Learning and ventures

**4.5.1 The daily review tells Ember to close what it stopped, which its tools have refused since 0.35.0** — reproduced ✔
- `agent/review.py:794-795` says "carry out each stop now (project_update closes any line)"; `project_update` refuses every
  closing status (`agent/tools.py:2206-2208`); the next review marks the stop "still active: you haven't carried it out",
  every day. Holding the product, which the dry-run fake does, is never mentioned, and nothing asks you to close it.
  (`repro/ventures/r3_stop.py`.) **Fix (S):** a stop means hold the product and tell you; count a held product as
  carried out.

**4.5.2 Rescoring one score marks all six "from research"** — reproduced ✔
- `venture_update` sets `scores_by="research"` for any subset (`agent/tools.py:2614-2623`), and the proposal gate checks only
  that flag (`agent/ventures.py:261-265`). A brainstorm idea with one score rescored passes "all six scores from research"
  and its card stops saying "a first guess" (`repro/ventures/r6_scores.py`: proposed at weight 93, five of its six
  scores the brainstorm's optimistic guesses). **Fix (S–M):** keep a source per score.

**4.5.3 The playbook can count a lesson that says the opposite as support** — reproduced ✔
- `learning._same` compares word overlap and negations only (`agent/learning.py:395-405`): "priced **below** 10 EUR get
  more…" counted for "priced **above** 10 EUR get fewer…", and the principle became *established* on it
  (`repro/ventures/r4_playbook.py`). Negations ("no views") work. **Fix (M):** don't count words that differ (antonyms,
  numbers, comparatives) as overlap, or require near-identity and leave merges to the weekly look.

**4.5.4 Your Plan tab says "knocked out (cash beyond the budget)" for ventures that aren't** — reproduced ✔
- The Plan tab calls `desk.item` without your cash budget or the runway (`agent/plan.py:718`, `806`), so the defaults (€0)
  apply; YOUR STEP passes them (`agent/plan.py:2165`). A venture that Ember is told is "ready to propose" shows you a false
  knock-out (`repro/ventures/r2_needs.py`). **Fix (S):** pass the settings, or drop the defaults.

**4.5.5 The cold-outreach knock-out misses most phrasings** — reproduced ✔
- Only listed nouns right after the verb are caught (`agent/knockouts.py:44-76`): "Send emails to Etsy shop owners…",
  "Pitch our planners to teachers by email", "Wir kontaktieren Kunden per WhatsApp" pass; "Customers can contact shops
  through Etsy's messages" is flagged (`repro/ventures/r7_cold.py`: 9 of 12 cold plans missed). Every email still
  needs your approval, so this is the first of two locks, not the only one. **Fix (M):** match a contact verb with any
  person noun in the clause, skip clauses whose subject is the buyer, and have the critic look too.

### 4.6 What buyers get

Besides 3.5:

**4.6.1 make_spreadsheet's pictures show other numbers and dates than the file** — reproduced ✔
- The pictures come from a second evaluator that reads the spec, not the file (`products/sheets.py:570-625`): empty
  rows are never evaluated, totals cover the given rows only, "general" numbers are printed with `%g`, and dates as the
  spec's ISO text. For one budget: an average of 307.95 € in the file is 821.21 € in the picture; 1234567 is
  "1.23457e+06"; 15.01.2026 is "2026-01-15"; a count total is the text "=COUNTA(…)" (`repro/products/sheets_checks.py`).
  make_image's own sheet pictures read the file and disagree with these. **Fix (M):** draw every picture from the
  built workbook, with Excel's General format.

**4.6.2 The Check line flags correct formulas and misses promised ones** — reproduced ✔
- Four correct quarterly sums (`Months!B4:B6` … `B13:B15`) each get "leaves out some of the data… B4:B15 takes them
  all", and the guide says "fix those before you sell it" (`repro/products/quarters.py`).
- Not flagged: an own-sheet Total `=SUM(B4:B5)` over rows 4-7, a two-column range, `=COUNTA(A1:A11)` that counts the
  header, and a template with no sample rows whose column formula points at a header
  (`repro/products/sheets_formula_checks.py`).
- `rows_csv` turns a CSV's header line into a data row (its formula shows #VALUE!, and so does the Total) and skips the
  column formula where a CSV field is empty (`Rent,950,950,`) (`repro/products/csv_header.py`).
- **Fix (S–M):** flag only ranges that start at the first data row and stop short; extend the checks to own-sheet and
  multi-column ranges and to template formulas; treat "" as empty and drop a header line.

**4.6.3 KDP: the guide's margins make Ember's own check refuse the book, and the refusal repeats the failing margin** —
reproduced ✔
- The kdp guide says 10 mm (13 mm from 151 pages). A 159-page journal at 13 mm prints 0.505 in from the inside edge
  (KDP needs 0.5) but Ember's 36-dpi check reads 0.472 and refuses with "a margin of at least 13 mm"; at 10 mm a
  "Page {page} of {pages}" footer is refused for the bottom margin with "at least 10 mm" (`integrations/kdp.py:87`, `498`,
  `509-511`; `products/pdf.py:196`; `repro/products/kdp_margin_scan.py`). The agent can't learn its way out of advice that
  repeats the failing value. **Fix (S):** footer inside the text area on KDP trims, advice from the measured
  shortfall, measure at 150 dpi or more, guide 13/14 mm.

**4.6.4 KDP: A4 and Letter books stop at 40 pages** — reproduced ✔
- DOCS: "A4 and Letter are KDP sizes too, up to 160 pages". `page: A4` or `page: Letter` with 45 pages is refused
  ("longer than 40 pages"), while `8.27x11.69` and `8.5x11` make 44 (`products/pdf.py:766-768`;
  `repro/products/kdp_checks.py`). **Fix (S):** grant 160 pages to A4 and Letter.

**4.6.5 resize_image can't make the documented A3 print file of a photo-like picture** — reproduced ✔
- DOCS gives "3508 x 4961 for A3 at 300 dpi" as the example; the output must be PNG, and a photo-like A3 PNG is over
  the 15 MB product limit: "QuotaError: a product file can hold at most 15 MB" (`repro/products/resize_big.py`). Flat
  posters fit. **Fix (S):** allow JPEG output, or fall back to it and say so.

**4.6.6 A words-only listing photo can overflow and silently cut items** — reproduced ✔
- With four items of about 40 characters (within the 160-character limit) and a badge, the fourth item runs under the
  "Instant download" badge and "PDF guide" is cut at the bottom edge, with no warning (`products/images.py:501-512`;
  `repro/products/text_overflow.py`, `t-square.png`). **Fix (S):** shrink the font until the wrapped items fit, as the
  listing layout already does.

### 4.7 Integrations and tools

**4.7.1 A listing Printify made never stops counting as live when Etsy no longer has it** — reproduced ✔
- Etsy's sync updates only Ember's own listings to removed or expired (`integrations/etsy_publisher.py:801-807`, `913-920`), and
  Printify's sync ignores active rows. A deleted poster listing stays "active" for good: pins and posts proposed for it
  are accepted and made, linking a dead page under your name, and the plan counts the product live
  (`repro/platforms/repro_printify_deleted.py`: the same post for Ember's own removed listing is refused). **Fix (M):**
  bring Printify's listing ids into the removed/expired update.

**4.7.2 A blog Undo is accepted while Ember is paused, but carried out only after Resume** — reproduced ✔
- The undos-only branch for a paused or unfunded Ember runs the listings, pins, products and posts, never the blog
  (`agent/service.py:1001-1005`); DOCS: "An Undo is carried out while Ember is paused or waits for money too". You pause
  because of a bad post and press Undo, and the post stays up (`repro/platforms/repro_blog_undo_paused.py`; also the
  companion, 6.6). **Fix (S):** an undos-only run of the blog publisher.

**4.7.3 Ordinary file mistakes end the cycle's work after three** — reproduced ✔
- Any jail error but a full workspace counts as a "refused file operation" (`agent/tools.py:1679`), including a file that
  doesn't exist yet or already exists; three end the work phase (`agent/loop.py:1756-1758`), and the agent is never told the
  rule (`repro/tools/rt_test_workspace.py`). **Fix (S):** count only path-safety refusals (links, bad names,
  traversal).

**4.7.4 A released workshop script still runs when it is handed over as a file** — reproduced ✔
- The released-script check runs only for `script` (`agent/workshop.py:153-154`), and the refusal itself says "describe the
  task without script" (`repro/tools/rt_test_workshop.py`: two paid runs of the built-in script). **Fix (S):** check
  every `.py` handed over, by path and content.

---

## 5. Low findings

Small defects, edge cases and documentation drift. Each is reproduced (r) or found in the code (c) by its audit; I
re-ran a sample, not all. Fix sizes are S unless marked.

**Money**

| Finding | Where |
|---|---|
| A refund sent twice gets 422 and points you to "Other correction", an adjustment that counts as money in (r) | `economy/ledger.py:626-634` |
| A raised estimate comes down after 3 accurate calls in total, not 3 in a row (r) | `economy/metering.py:1512-1525` |
| Research-check calls (the research model's trial) skip the server-tool hold and the 5× rule (r) | `economy/metering.py:130`, `agent/loop.py:2170-2197` |
| Wake now on a dormant Ember that can't afford a plan ends its life; DOCS don't say so (r) | `economy/life.py:452-470` |
| The dashboard says the workshop can't run when its hold exceeds the daily cap; since 0.33.0 it runs (r) | `economy/service.py:357-361` |
| DOCS cost figures are stale: a working cycle $0.317 (not about $0.25), an Opus plan $0.211, an Opus workshop call $2.14 (r) | DOCS 38, 164-171 |
| Research's cost tail forgets a costly call after 20 newer research calls, not after 14 days (r) | `economy/metering.py:497-521` |
| Maintenance's "one scheduled cycle a day" is min(24 h, Longest sleep): 3 a day at 480 minutes (c) | `agent/service.py:558`, `884` |
| "The last 7 active days" is the active time within the last 7 calendar days; after a pause of a week the runway is unknown (c) | `economy/life.py:402-421` |
| A per-call correction clause never matches, so every refund nets against uncertain charges (c) | `economy/ledger.py:289`, `333` |

**Wake cycle**

| Finding | Where |
|---|---|
| After a cycle stopped for an overrun there is no steps-ready cut, and events are held for the whole chosen sleep (r) | `agent/loop.py:399-400`, `agent/service.py:900` |
| A wake for one kind of your news fires for news of a kind you switched off (r) | `agent/service.py:636-639` |
| A cycle that worked gets no reflection after a model refusal (r) | `agent/loop.py:1052-1053` |
| An unlock's approval of the last waiting request doesn't restore the sleep Ember chose (c) | `web.py:505` only caller |
| The keepers before the plan aren't guarded: a bug in one ends every cycle after the day's paid review (c, risk) | `loop._keep` |

**Plan tree**

| Finding | Where |
|---|---|
| Ember can make your pinned step wait, which defeats the pin (r) | `agent/plan.py:2614-2632` |
| YOUR PLAN's "Waiting on your owner" lists approvals that aren't waiting (r) | `agent/plan.py:2260-2266` |
| The day-28 retry checks views only, not the 2 favorites (r) | `agent/plan.py:1401` |
| A promise is orphaned when its product closes: the obligation stays open and is no step any more (r; companion 6.4) | `agent/plan.py:879-892`, `984` |
| The day-21 decision falls inside the day-14 retry, and you are asked again at day 28 (r) | `agent/plan.py:1390-1409` |
| Two steps can wait on each other for ever (r) | `agent/plan.py:2654` |
| The scale step can't be split, though its docstring says it is hers to split (r) | `agent/plan.py:1478-1499`, `2409` |
| The promise brake counts a rolling 24 hours, not "until that day is over" (c) | `agent/plan.py:64`, `1681` |
| A venture cycle records the model's `focus_project_id`, which feeds the streak, momentum and ages (c) | `agent/loop.py:940-1005` |
| `burn.Burn.marketing_cycles` is dead since 0.35.0 (c) | `economy/burn.py` |

**Approvals, mail, Reddit and KDP**

| Finding | Where |
|---|---|
| A KDP description can reach 4,068 characters with the AI line (KDP allows 4,000), and the AI line is what would be cut (r) | `integrations/kdp.py:235`, `248` |
| The email-reply unlock reads a thread's start from a trimmed chain; in a thread Ember started, a third answer counts as theirs (r; still no first contact) | `integrations/mailstore.py:419`, `agent/policy.py:140` |
| A Reddit request approved with changes through the API loses the AI line in its link (the dashboard has no such button) (r) | `owner.decide`, `agent/views.py:981` |
| 0.35.0's one-time move gave each of a venture's products the whole remaining unlock budget (c) | `agent/plan.py:1256`, `1332` |
| An SMTP refusal after DATA is reported as unclear, and takes back the unlock that approved it (c) | `integrations/mail.py` |
| KDP's flat printing band ends at 108 pages, not 110: a minimum price a few cents low (inferred) | `integrations/kdp.py:117` |

**Platforms**

| Finding | Where |
|---|---|
| An order with no payment at Etsy pins every sync's window to its day, for up to a year (r; trigger inferred) | `integrations/etsy_publisher.py:796-818`, `969-987` |
| A sync reads at most 500 receipts, newest created first; a later refund of an older order isn't seen (r) | `integrations/etsy_live.py:363-383` |
| The memorial shows total spending with "balance and spending" switched off (r) | `products/live.py:637` |
| The blog Undo uploads a re-rendered list without the second check (c) | `integrations/site_publisher.py:735-736` |
| A cycle's own Etsy sync is skipped when the category fetch fails (c) | `agent/loop.py:569-571` |
| At execution, a Bluesky link that is neither a listing nor your site passes unchecked (only the proposal checks it) (c, risk) | `integrations/bluesky_publisher.py:266-267` |
| A post Bluesky's app view hasn't indexed yet may be marked "Deleted at Bluesky" (inferred, risk) | `integrations/bluesky_publisher.py:585-590` |

**Tools and workshop**

| Finding | Where |
|---|---|
| Six files can be handed to the workshop, not five (r) | `agent/workshop.py:241` |
| Runs refused for their price, with nothing sent, use up your runs per day (r) | `agent/workshop.py:171`, `405` |
| A task naming a workspace file it doesn't hand over is missed for root files, `./` paths and some text kinds (r) | `agent/workshop.py:81-83` |
| A paused run refused on its continuation leaves its files at Anthropic and goes unrecorded (c) | `agent/workshop.py:319-320` |
| The network guard misses `multiprocessing`'s spawn, C-extension imports and `getnameinfo`; in live mode six handlers run unsealed, though the README says every handler is (r, risk) | `agent/netguard.py:23-44` |
| Harmless Excel formulas (a `\|` inside text, an https HYPERLINK) are refused in workshop files; Word HYPERLINK fields aren't checked for their target (r, c) | `products/checks.py:125-128` |
| A Bluesky post is kept on the cycle's line only when it links a listing (c) | `agent/tools.py:4565-4581` |
| An upgrade request's "workshop script" can be any `.py` the agent wrote (c) | `agent/tools.py:3527-3529` |
| `workspace_write`'s edit can't delete a passage (r) | `agent/tools.py:1990` |

**Learning and ventures**

| Finding | Where |
|---|---|
| A researching venture with an open project is announced "parked by Ember's code on X" and stays urgent for good (r) | `agent/desk.py:67-69`, `103-107` |
| A channel venture you parked and backed again while its channel is off keeps a dead first test; Ember can set it live (r) | `agent/stages.py:552-560` |
| A met first test leaves a venture "building" until the agent acts, so the 60-day rule and Scale it never apply (c) | `agent/stages.py` |
| Consolidating lessons can drop a lesson's number in a merge, and loses lines that aren't bullets (r) | `agent/memory.py:418-431` |
| The weekly look's strategy is refused for naming a parked idea whose title is a generic category (r) | `agent/obligations.py:331-343` |
| Ember can restart the 21-day research clock by moving a venture back to idea and on again (r) | `agent/ventures.py:305-307` |
| Bets settle at the start of their due day (r) | `agent/bets.py:177` |
| A research call continued after `pause_turn` may lose the first part's pages (inferred) | `agent/loop.py:2095-2121` |
| "Answer the critic with evidence" can't close its step: the critic runs again only for a new case (c) | `agent/critic.py` |

**Products**

| Finding | Where |
|---|---|
| The listing photo's "soft shadow" is a hard-edged band (r) | `products/images.py:349-352` |
| A table row slightly shorter than a page runs off the page (r) | `products/pdf.py:557-563` |
| An empty table becomes a Word table with no rows (r; Word's reaction inferred) | `products/markup.py:635-636`, `products/word.py:447` |
| `check_formula` refuses `" \| "` inside text and unquoted non-ASCII sheet names such as `Übersicht!B2` (r) | `products/sheets.py:441`, `68-72` |

**Web, security and the database** (none weakens a guarantee)

| Finding | Where |
|---|---|
| A database that breaks while Ember runs isn't reported: sensors answer 500, health says "ok" (r) | `web.py:120-123`, `189-216` |
| No request-size limit: a 300 MB body is read whole before the 8 MB check (r) | every `Body()` route |
| `GET /api/site/download` records a download without the CSRF header (r; practically unexploitable) | `web.py:997-1017` |
| The `unlocks` sensor counts unlocks of closed milestones while Ember is paused (r) | `agent/service.py:1225`, `agent/audit.py:645` |
| Event texts are stored unredacted, and secrets under 8 characters aren't registered: they hold only because no caller passes one (c, risk) | `events.py:150-166`, `logging_setup.py:30-34` |
| The shareable diagnostics keep a refused household member's user ID (r) | `diagnostics._owners` |

**Workflow, dry run and documentation**

| Finding | Where |
|---|---|
| Nothing stops a second listing of a product that is already live: in run A, 6 live copies of one product (r) | `tools._propose_etsy_listing` |
| The store text (`ember/README.md`) still promises spending shares for ventures and marketing and "a roadmap it keeps itself", all retired in 0.35.0–0.36.0 (c) | `ember/README.md:16-22` |
| DOCS: the shop isn't read while paused (it is); the SFTP server is used "only after you approved a page" (the live page uploads every 15 minutes); "paused: nothing runs" (renewals and the live page do); `draft` is "ordinary cycles only" (marketing and event cycles carry it); another line's obligations are refused (allowed since 0.33.0) (c) | DOCS 261, 554, 1897, 2822, 2941 |
| DOCS' sensor paragraph understates what host-network apps can read (about 30 fields); its options table omits 14 options (c) | DOCS 30-127, 2918 |
| README's "System → Preview" switcher is gone; `translations/en.yaml` points to the Ventures tab, gone in 0.37.0; README links `DOCS.md#roadmap` (c) | README 118, 346; en.yaml 32-37 |
| The fake model's docstring still describes the decision desk's READY list (c) | `agent/fake_llm.py` |

---

## 6. Three simulated weeks: what the whole system does

The unit tests check each rule on its own. To see the rules together, I ran Ember's real wake cycles for three
simulated weeks with the built-in fake model, the fake shop and the fake channels, driven the way
`agent/scheduler.py` drives them: economy, unlocks, executor, shop, live page, agenda, then decide and run
(`repro/e2e/sim.py`). The simulated owner either approved every request at 09:00 and 18:00 (and marked the ones they
carry out done), or never answered. Nothing reached the network.

| Run | Options | Owner | Cycles | Spent | What happened |
|---|---|---|---|---|---|
| A | a new install's ($20, $1.50 a day, $0.50 a cycle) | approves twice a day | 100 | $9.64 of API, $1.60 of listing fees | 2 products; 17 listing requests, 9 of them failed because a photo was made again while they waited (4.2.1); 8 live listings for 2 products, 6 of them copies of one; both products then held by Ember after its review said stop |
| B | yours, as the analyses report them ($200, $5 a day, $1 a cycle, 30-minute shortest sleep, channels on) | approves twice a day | 322 | $14.68 | **316 cycles took one step, "Pin it twice", and made no pin** (3.1); the second product never left research |
| C | a new install's | never answers | 126 | $9.32 | 86 venture cycles; the step "Research its next question; then its business case, or park it" taken 79 times; no business case reached the owner |

**What held, every simulated day:**
- 0 exceptions in 548 cycles; every cycle completed.
- **The money caps:** the costliest day was $1.29 of a $1.50 cap (A) and $2.15 of $5 (B, where the 30-minute sleep,
  not the cap, set the pace); no cycle spent more than its cap; no call cost more than its priced worst case.
- **The approval boundary:** nothing reached the fake shop, accounts or mailbox without the simulated owner's approval,
  and the executor refused every listing whose approved files had changed.

**Two workflow defects the runs showed.** Approvals that fail because a waiting request's files were made again
(4.2.1), and nothing to stop a second identical listing of a product that is already live (section 5): run A listed
"Proofreading notes for non-native writers" six times. Live, each costs $0.20 and splits the product's views, and only
your attention stops it.

**What the fake model can't show.** It proposes only Bluesky posts in a marketing cycle and never a pin, never writes
a business case, and asks the same research question again (answered free from memory). So runs B and C loop partly
because of the fake; 3.1 shows that the plan tree's part of it is real with no model at all. The dry run's shop is
optimistic by design: each listing gets 1 to 5 views an hour and every third one an order on its second day
(`integrations/etsy.py`, `FakeShop._interest`, `orders`), while your live shop had 13 views in eleven days. **A dry run
shows how Ember works, not what it will earn**, and it never shows you a business case or a pin.

---

## 7. Does Ember deliver its purpose?

The purpose is the README's first sentence: an agent "whose goal is to stay alive economically… it has to find honest
ways to earn more than it spends".

**Where it stands** (live, from the companion analysis, 2026-10-09):
- $200 granted; $59.66 of API calls and $0.60 of expenses in eleven days; $0 revenue;
- 7 Etsy listings and 2 Printify posters: 13 views, no favorite, no order;
- about $5.70 per active day, so the money lasts until about 11-02;
- your goal, $100 a month by 11-15: 0 %.

**What it would take.** At $5.70 a day the API costs about $173 a month. With Ember's own fee model (`agent/econ.py`:
Etsy Germany, USD 1.10 per EUR), a sale keeps:

| Price | Kept per sale | Sales a month to cover the API | Views a month, if 1–3 % of views order |
|---|---|---|---|
| €5 | $4.29 | 40 | 1,350–4,050 |
| €9.90 | $9.04 | 19 | 640–1,920 |
| €22.90 (a poster, before Printify's cost) | $21.67 | 8 | 270–800 |
| €39 | $37.30 | 5 | 155–465 |

The view-to-order rate is my assumption, a common range on Etsy. Observed reach is about 35 views a month across all
listings: between 4 and 100 times too few. The 0.32.0 analysis said it first: "Reach is the business's problem, and
no code change fixes it."

**What the code can still change:**
1. **Stop paying for cycles that can't move anything** (3.1). With a step that can't advance, the sleep cut turns the
   day into 48 cycles; live on 10-09 that spent $5.88 by 12:35. Fewer, productive cycles stretch the runway more than
   cheaper ones would.
2. **Keep the products moving.** A product that stalls after a rejection (3.2) or never gets past its launch because
   a channel is off (4.3.1) is a listing that never gets its marketing.
3. **Put the cycles where buyers come from.** Since 0.35.3 the weights rank a live product's launch marketing first,
   which is right; it needs the brake in 3.1 so that one blocked channel doesn't take every cycle.
4. **Don't ship wrong files** (3.5, 4.6). A bad review on a €5 product costs more than the sale.

**What only you can change:**
- **Reach that costs money:** Etsy Ads, which Ember parked as venture #13 (0.32.0 analysis), is a test you can stop
  any day.
- **Prices that need fewer sales:** at €39, five sales a month cover the API; at €5, forty.
- **A slower pace while nothing sells:** a higher shortest sleep, or the steady stance, until there is something to
  sell into.

---

## 8. What I recommend

### 8.1 For you, now

1. **Keep a stuck step from spending your cap** (3.1), until the code has a brake:
   - pin the step you want worked on (a pinned step is taken first, one a cycle, until its check passes); the
     companion analysis lists the launch pin steps to pin (its 5.1);
   - decide pins and posts soon: a step whose requests wait for you is taken again every 30 minutes;
   - when you'll be away for hours, raise **Shortest sleep** (120 minutes makes a stuck step cost 12 cycles a day, not
     48).
2. **Look for a product that "waits on you" with nothing in your approvals** (3.2): it is stuck. Ask Ember in a message
   to propose it again; its promise, made with the product's project, becomes a step of that product and gets it a
   cycle.
3. **Add three rules to your rulebook:**
   - "Never use whole-column references such as C:C in a formula; refer to the data rows, such as C4:C20" (3.5);
   - "For A4 or Letter books use page 8.27x11.69 or 8.5x11" (4.6.4);
   - "From 151 pages on, give a book a margin of 14 mm" (4.6.3).
4. **Before you approve a spreadsheet listing, open the file, not its picture,** and check the totals (3.5, 4.6.1).
5. **Mail:**
   - before you reset the kill switch, cancel the approved emails that haven't gone out, and approve them again once
     Ember has read its mail (4.1.1);
   - take your email-reply unlock back before you pause Ember for more than a few days (4.1.3);
   - the kill switch stops the next round, not one already sending (4.1.2).
6. **Keep your own copy of a listing's files before you approve a change to them** (4.2.2), and add entries to the
   blog's list by hand only in the template's exact form (4.2.3).
7. **On a new install, or a new dry-run session,** press **Research more** on venture #1 once its budget is spent (3.3).
8. **Reach** (section 7): decide whether to test Etsy Ads, and whether to steer Ember toward fewer, dearer products.

### 8.2 In the code, in this order

| # | What | Where | Size |
|---|---|---|---|
| 1 | One brake for every step: a step taken without progress, or whose requests wait for you, waits until something changes; no sleep cut for it; an Owner promise of pins gets its channel | 3.1 | M |
| 2 | "Propose it again" when a product's request ends without going live | 3.2 | M |
| 3 | The workshop's hold near the bottom, with a test on the production path | 3.4 | S |
| 4 | Spreadsheets: pictures and checks from the built file; whole-column references; the Check line's false alarms and gaps; `rows_csv` | 3.5, 4.6.1, 4.6.2 | M |
| 5 | Mail and the kill switch: read before sending, check the switch per item, expire before the unlocks run | 4.1.1–4.1.3 | S |
| 6 | Files a request depends on: refuse overwrites; keep uploaded bytes so Undo works | 4.2.1, 4.2.2 | S–M |
| 7 | The seeded channel ventures: live once listed, out of exploring and the research budget | 3.3 | M |
| 8 | Your controls: your hold stops owed steps; Hold new things refuses with a venture and in venture cycles; a wait can't take a pinned step; `qa_fix` keeps the photos | 4.1.4–4.1.6, 5 | S |
| 9 | The plan's lifecycle: launch steps for channels in use, no recurring steps on channel products, a chance for ventures, the blog channel | 4.3 | S each |
| 10 | Wake timing and money edges: holds that re-check, event cycles' sleep and room, replies once, the reflection after a slow step, provisional charges | 4.4 | S–M |
| 11 | Learning and ventures: the review's stop, scores per source, the playbook's opposites, the Plan tab's knock-outs, cold outreach | 4.5 | S–M |
| 12 | KDP margins and sizes, print files, text photos | 4.6.3–4.6.6 | S |
| 13 | The blog list and Undo; Printify listings removed at Etsy; ordinary file mistakes; released scripts | 4.2.3, 4.2.4, 4.7 | S–M |
| 14 | Section 5, and the documentation drift | 5 | S |

0.37.1, in progress on the companion branch (CI red when I looked), covers part of item 1 for promises.

### 8.3 In how Ember is built

- **Test the steering over weeks, not single picks.** The tests replay picks as if taking a step completed it (3.1),
  and the fake model never pins or writes a business case (section 6). A simulated week in CI, with a fake that does
  what YOUR STEP asks and assertions such as "no step is taken more than N times without progress", "every product
  gets a cycle within D days" and "no cycle runs on a step whose requests all wait", would have caught 3.1, 3.2, 3.3,
  4.3.1 and 4.3.2. Section 6's harness (`repro/e2e/sim.py`) runs a simulated week in under two minutes.
- **One source of truth for buyers' files.** Pictures and checks that read the built file, not the spec (3.5, 4.6.1).
  The products audit recalculated every workbook with LibreOffice; CI could do the same.
- **Keep the promises the changelog makes.** 0.34.0's week in the shadow lasted 3.5 hours, and the weights then
  changed four times in 33 hours (companion, 4.11). "A change to the weights steers only after N live cycles in the
  shadow" makes the next change measurable before it costs money.
- **Release less often.** 20 minor versions in 7 days (10-02 to 10-09; four on 10-09 alone). Each is a restart, which
  4.4.6 shows can put Ember into a critical state, and each adds release notes Ember spends plans reading.
- **Remove what a release replaces, everywhere at once.** Three generations of "what to work on" in ten days (READY's
  rotation, READY with focus lines, the plan tree) left words behind in the store text, DOCS, docstrings, the fake model
  and the daily review's instructions (4.5.1). DOCS is 2,972 lines, and the workers' fixed prompt was at its byte bound
  in 0.32.0.

---

## 9. What holds

What each audit checked against its promise and found true, in brief.

**Money**
- Each call is refused if its worst case could break the daily cap (per local day, across a DST night too) or the
  balance, or its expected cost the cycle cap; the database ties each cost to its day.
- The last will's reserve, the reflection's reserve and its bounded allowances, and the 20:00 share for event wakes.
- Overruns scale that kind of call's estimate (×8 at most) and stop only what they must.
- Calls cut off are charged at their worst case; 500, 502, 503, 504 and 529 before the answer cost nothing.
- The ledger is append-only, corrections are bounded by their entry, refunds by their day; the same form twice is
  recorded once.
- Burn modes and life states follow DOCS, with their hysteresis; dry run has its own balance per session.
- The transport reaches only `https://api.anthropic.com`, ignores proxies and never retries.

**Approvals and outside actions**
- Nothing is sent without your approval or a standing, covering unlock, and the database refuses anything else; an
  approved action can't change.
- A send is recorded before it starts, happens at most once (also with two executors at once), to one recipient, with
  no header injection, over verified TLS, with a truthful footer, within the day's limit.
- First contact needs a verified person; opt-outs are kept and checked at send.
- Unlocks: price bands from the price you approved, QA checked twice, take-backs and the kill switch stop what an unlock
  approved; no unlock without your owner ID or in safe mode. All 8 problems of analysis-0.13.0's §0.6 are fixed.
- Reddit links and KDP packages (trims, spine and cover maths, prices) are right.

**Security**
- Only the Ingress proxy may connect (and host-network apps to `/api/sensors` only); dev mode only from localhost.
- Your identity comes from the first `X-Remote-User-Id`; every route refuses anyone else; safe mode keeps your IDs and
  the kill switch (0.13.0's §0.2 is fixed).
- Every POST needs the CSRF header; the CSP allows nothing inline and nothing from a CDN; all text reaches the page as
  text; products download, never open.
- With every secret set, none appeared in the log, the database, any response or the diagnostics.
- The Supervisor's token is removed, and the container stops when Ember's process ends.
- The file jail refused every path, link and special-file trick tried; quotas hold.

**The wake cycle**
- Your wakes (5 minutes after your last word, at most 15 after the first, 30 apart), the switches, Wake now; event
  wakes (at most 4 a day, 30 minutes apart, never while dormant or backing off); the 20:00 reserve, DST-correct.
- The step's kind and line reach the cycle; tools stay on the line.
- The reflection hears what wasn't done; a work step's journal is its draft; calls written inside text are run.
- A digest however a cycle ends, also after a restart; your decisions stay in the news until a cycle that saw them
  ends normally; maintenance's one cycle a day; crash-loop protection.

**The plan tree's mechanics**
- Layout from templates, stages that close only on their checks, retyping, the weight's arithmetic and constants,
  decide-by dates, the money goal and your goal, freeze, only you closing or dropping a product.

**Products**
- Cost statements are exact: a hand-checked leap-year statement with odd areas and a 17-day tenant, the letter for
  every tenant, and 80 random statements (73,600 cells) match LibreOffice's recalculation.
- Documents: every layout line in the PDF and the Word copy, fonts embedded, 40 pages (160 for KDP inch trims).
- KDP covers: trims, bleed, spine width from the page count, the barcode space, the ebook size, price floors.
- Listing photos, posters and print files at their documented sizes; 40 megapixels at most.

**Integrations**
- Each talks only to its hosts over verified TLS; SFTP refuses another key before the login.
- Tokens stay out of the database, the logs and the diagnostics; OAuth uses PKCE and a state.
- Per-day limits; nothing done twice after a crash; a listing created complete, an edit never leaving it without
  photos or files.
- Automatic revenue only for Ember's paid orders from the day you allowed it, never twice; Etsy's API terms kept.
- Bluesky's AI line, length limits and links; the blog's files and checks; the live page only with what you approved.

**Learning and ventures**
- Ember's code works out the economics exactly (checked by hand to the cent); evidence grading, the research budget,
  the stage rules and your park or kill reaching projects, milestones and unlocks.
- Bets, retrospectives, the playbook's confidence, the daily review's timing and budget, the lessons file's limits and
  pins, the weekly look, the quality critic, forecasts and the library.

**Everything Ember publishes says that an AI wrote it**: Etsy listings, pins, Printify products, Bluesky posts, emails,
blog posts and website pages each get the line from Ember's code, not from the model.

---

## 10. How to re-run the reproductions

The scripts are in `analysis-0.37.0/repro/`, one folder per audit (`e2e`, `economy`, `loop`, `plan`, `tools`,
`ventures`, `outside`, `platforms`, `web`, `products`). Each builds its state in a fresh data folder with the
repository's own code and test helpers, and changes nothing in the repository. From `ember/`, with the development
requirements installed:

```bash
R=../analysis-0.37.0/repro
EMBER_DATA_DIR=$(mktemp -d) EMBER_SCHEDULER=off EMBER_FAKE_DELAY_MS=0 PYTHONPATH=.:$R/<folder> \
    python $R/<folder>/<script>.py
# the tools folder's rt_test_*.py files are pytest files:
EMBER_SCHEDULER=off PYTHONPATH=.:$R/tools python -m pytest -p no:cacheprovider -p tests.conftest -q -s $R/tools/rt_test_workspace.py
# a simulated week (section 6): data folder, days, options (default or live), owner (active or passive)
PYTHONPATH=. python $R/e2e/sim.py /tmp/ember-week 7 live active
```

`repro/README.md` lists which script reproduces which finding. Some products scripts recalculate with LibreOffice
(`soffice`) when it is installed.
