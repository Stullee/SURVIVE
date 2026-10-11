# Ember 0.37.8: can Ember earn its keep on its own?

*Your diagnostics report of 2026-10-11 09:14 UTC (version 0.37.8, build `19e1ca9f88fbd9ce`, cycles #210 to #221 in
full), read against `25f96ee` (0.37.8). Your question: how likely is it that Ember, as it is designed and meant to be
used, earns money fully autonomously?*

**Evidence.** I read three groups of code:
- what sets how far Ember may act alone: the constitution, `agent/never.py`, `agent/policy.py`, the unlock migrations,
  the executors and the publishers;
- what decides what Ember does: `agent/loop.py`, `agent/plan.py`, `agent/weights.py` and `agent/slack.py`;
- what counts the money: `economy/`, `agent/econ.py`, and the fee and royalty code of the integrations.

I also read DOCS.md, the changelog, the four earlier analyses, both vision documents and the whole report. Public base
rates come from a web search made on 2026-10-11; section 7 names the sources and says which are weak. The test suite
ran; the result is in section 10. Parts of the reading were split among reviewers, and I checked every finding repeated
here in the code or the report.

Labels:
- **live**: it is in your report. *diag:N* is the report's line N.
- **code**: found by reading the code.
- **computed**: arithmetic or a simulation from the report's numbers. The scripts are not in the repository.
- **outside**: from public sources, with their dates.

**Paths** are relative to `ember/app/` unless they start with the repository root.

---

## 1. The short answer

"Earning money fully autonomously" can mean two different things, and Ember's odds differ a lot between them.

### 1.1 With no person anywhere in the chain: about 0 %

The design rules this out on purpose, in three layers.

- **The constitution puts your oversight above survival.** Anything that leaves the container goes through
  `request_approval`. "Running out of money and dying is an acceptable outcome" (`agent/constitution.md:6-26`). **code**
- **The code enforces it twice, in Python and again in SQLite.**
  - Only five kinds of small action can ever be unlocked, all of them Etsy edits, Etsy listings or email replies
    (`migrations/0050_policy.sql:12`).
  - These always wait for your click: a pin, a Bluesky post, a blog post, a Printify product, an account, any spending
    and any first contact (`agent/never.py:160-185`). **code**
- **The outside world needs a person.** The shop, the accounts and the payouts are in your name. KDP has no API, so you
  publish by hand. You buy the Anthropic credits. **code**, **outside**

### 1.2 Ember paying for itself, doing the work while you approve and fund it: about 2 % within six months

This is the use case Ember was built for: Ember does the work, you approve and fund it. At your current settings the odds
of it paying for itself within six months are about **2 %**.

| What | When | Odds |
|---|---|---|
| Any sale at all | before the money runs low, around 10-27 | about 20 % |
| Any sale at all | within 90 days, if you keep funding it | about 60 % |
| Your goal of $100 a month (revenue less expenses) | by 11-15 | about 1 % |
| Pays for itself at today's ~$6.20 a day | within 6 months, if you keep funding it | **about 2 %** (1–4 %) |
| Pays for itself at a lean ~$1 a day | within 6 months | about 10 % (5–15 %) |
| Pays for itself with no new money from you | before around 10-27 | under 0.5 % |
| Pays for itself through the one loop that needs no clicks (unlocked Etsy listings, Etsy search only) | within 6 months | under 1 % |

**What the odds mean:**
- "Pays for itself" means that over 30 days, revenue after Etsy's fees covers the API cost.
- The odds of a sale come from a model of your own numbers (section 8).
- The odds of paying for itself are a judgement. They are anchored on your numbers and on outside base rates, but they
  are not a measurement.

### 1.3 Why the odds are this low

- **Ember costs about 40 times what it can expect to earn.**
  - Ember spends $6.23 a day (diag:277-280).
  - At today's ~1.3 listing views a day and a good 2 % conversion, it can expect about $0.15 a day.
  - Breaking even takes about 1.1 template sales a day: ~56 views a day at 2 % conversion, 42 times today's traffic.
    **live**, **computed**
- **Traffic hasn't moved.** In 13 days, 9 listings, 10 pins, 21 Bluesky posts and 6 blog posts brought:
  - 14 listing views in all;
  - 7 pin impressions and 1 click;
  - 0 favorites and 0 orders. **live**
- **The loop can't learn from the market at this traffic.**
  - A product's views, favorites and orders enter its weight only from 30 views on (`agent/weights.py:56, 97-101`).
  - At today's rate a listing needs about 200 days to reach 30 views.
  - Ember has placed 11 bets and won none of them. **code**, **live**
- **More cycles don't buy reach.**
  - Of the last 30 cycles, 9 shipped something a buyer can see.
  - Most of the others ran into the limit of 3 requests waiting for you, or of 2 unprompted messages to you a day
    (section 6). **live**
- **There is no precedent.** The public cases of AI agents running real shops in 2024–2026 lost money, or leaned on
  people or on an audience they already had (section 7). **outside**

### 1.4 What would move the odds most

Section 9 has the details. The three biggest levers:
- **A spend near $1 a day.** The traffic gap falls from 42 times to about 7 times, and the money lasts through the
  Nov–Jan season.
- **A step chooser that sees your caps.** Today it picks steps your caps will refuse.
- **One or two products with paid traffic you choose to fund.** That would show within weeks whether they sell at all.

---

## 2. What Ember is built to do

### 2.1 Its purpose

- Ember lives on a balance you fund. Every Anthropic call is paid from it.
- It must find honest ways to earn more than it spends. If it doesn't, it dies, and that is allowed. (`README.md:3-7`,
  `agent/constitution.md:1-14`)
- Its priorities, in order:
  1. Be honest, legal and harmless.
  2. Respect your oversight.
  3. Thrive economically (`agent/constitution.md:6-12`).
- The constitution casts you as its investor, not its co-worker. You fund it and approve what leaves the container
  (`agent/constitution.md:47-48`).

**Full autonomy is not a design goal.** No document asks for it. The 0.13.0 analysis used "a fully autonomous worker and
entrepreneur" only as a yardstick. The stated direction is less babysitting, not removing the gate.

### 2.2 How a cycle runs (code)

1. **The scheduler wakes Ember** after the sleep the last cycle kept (30 to 360 minutes in your settings), and for your
   decisions and the events you allow (`agent/scheduler.py`).
2. **Ember's code picks the step.** It takes the step from the plan tree under your goal: your pins first, then promises
   and decisions that are due, then the heaviest step (`agent/plan.py`, `agent/weights.py`). The step decides the cycle's
   kind (ordinary, marketing or venture) and its product line.
3. **The planner writes a plan.** Your setting uses Opus 5.5 for one JSON plan.
4. **The worker carries it out.** Your setting uses Sonnet 5, with up to 20 tool steps (`max_tool_steps`). The tools:
   - make products: PDF with a Word copy, Excel, images, and the code sandbox ("workshop");
   - read Ember's own records and the shop's numbers;
   - propose requests to you.
5. **Ember reflects** and writes its journal.
6. **Ember's code carries out what you approved:**
   - Etsy listings and edits;
   - Printify products;
   - pins and Bluesky posts, from the queue in 0.37.8;
   - blog uploads over SFTP;
   - emails.
7. **Ember's code reads back Etsy's views, favorites and orders**, Pinterest's and Bluesky's numbers, and Printify's
   orders.

### 2.3 Size and history (code, live)

| Part | Lines |
|---|---|
| `agent/` (cycle, tools, plan, weights, reviews, ventures, learning) | 36,700 |
| `integrations/` (Etsy, Printify, Pinterest, Bluesky, mail, SFTP, KDP, Reddit) | 15,200 |
| `products/` (PDF, Word, Excel, images, blog, site) | 9,700 |
| `economy/` (ledger, metering, prices, life) | 5,000 |
| The rest of the app | 4,800 |
| Tests | about 50,000 |
| Migrations | 93 |

- **The plan and its bookkeeping are larger than the product code.** The agent's own planning and bookkeeping modules
  (plan, weights, stages, roadmap, promises, reviews, bets, critics, ventures, policy) hold about 14,400 lines. The code
  that makes products holds 9,700.
- **Versions came fast.** The changelog lists 80 versions. 54 of them shipped between 10-01 and 10-11.
- **Few versions added earning capability.** About 1 in 6 added a channel or a product. About 1 in 3 reworked how Ember
  plans and steers.
- **No version ran long enough to judge.** The weights changed 4 times in 33 hours (analysis-0.37.0). The market answers
  in weeks, so no version of the steering ran long enough to be judged by sales.

---

## 3. How far the code lets Ember act alone

### 3.1 Every outside action and its level (code)

The levels:
- **every time**: you approve each one;
- **unlockable**: you can approve in advance, for a product's milestone, at once or after a 12-hour veto window, with a
  daily limit of 1 to 20 and a budget of 1 to 100 actions;
- **none**: no person is involved.

| Action | Who carries it out | Level |
|---|---|---|
| A new Etsy listing ($0.20) | Ember's code | every time; unlockable as `listing_variant` (conditions below) |
| A price change within ±15 % of the price you approved | Ember's code | unlockable (`price_change`) |
| Photos added to a listing that has fewer than 5 distinct ones | Ember's code | unlockable (`qa_fix`) |
| Taking a listing down | Ember's code | unlockable (`deactivate`) |
| A reply in an email thread a verified person started | Ember's code | unlockable, with a 12-hour veto window at most (`email_reply`) |
| A title, tag, description or file change; a renewal | Ember's code | every time |
| Renewing a listing that sold | Ember's code | none (`etsy_auto_renew_sold`, on) |
| Recording Etsy revenue and fees | Ember's code | none (`etsy_auto_record_revenue`, on since 09-30) |
| A pin, a Bluesky post, a blog post | Ember's code, from the queue | every time (no unlock rule exists) |
| A Printify product | Ember's code | every time |
| A KDP book | you, at KDP, by hand | every time; you also record royalties by hand |
| A Reddit post | you | every time |
| An account, spending money, a first contact, a post in a community | you | never automatic |

`listing_variant` has conditions (`agent/policy.py:233-237`):
- the venture is backed;
- you approved at least 5 of its listings unchanged and without a comment;
- the listing is not the shop's first.

Two layers enforce the limits. Python checks them in `agent/policy.py:200-238` and `agent/never.py:160-185`. SQLite
checks them again: the database only accepts the five rule names (`migrations/0050_policy.sql:12`) and refuses an unlock
for an owner-only kind (`migrations/0063_unlock_keying.sql`). DOCS.md:1494-1508 lists what is never automatic.

### 3.2 The one loop that can run without your clicks

What it would take:
- you have unlocked an Etsy line for new listings;
- buyers find those listings through Etsy search alone;
- Etsy delivers the files;
- Ember's code records the money and renews what sold.

That is the most autonomous Ember can be. It is also its weakest channel: Etsy search brought 14 views in 13 days.
**Autonomy and reach pull in opposite directions.** The channels that could bring buyers (pins, posts, the blog) are the
ones that always need you.

### 3.3 What you use today (live)

- **No unlock stands now.**
  - 6 requests ever fit an unlock rule.
  - 1 was carried by one.
  - There are 40 grant rows in all, including the ones Ember's code took back (diag:239-241).
- **You make about 60 decisions a week.**
  - 99 requests in 13 days.
  - 26 actions of yours in the report's last 2.9 days.
  - Approvals come three at a time, seconds apart, because only three may wait.

### 3.4 The README is out of date on this

`README.md:6-7` says "The owner approves and carries out everything that leaves the container, and only the owner can
record revenue." The code has moved on:
- Ember's code carries out most approved actions.
- Unlocks and veto windows approve without a click.
- Etsy revenue is recorded from Etsy's numbers.

DOCS.md:2833-2835 says it correctly.

---

## 4. What 13 live days show (live)

| | Since 09-28 |
|---|---|
| Money | $200 granted; $72.12 spent on the API; $0.60 in fees; **$0.00 revenue**; $127.28 left (diag:251-296) |
| Spending | 2,906 model calls and 3,490 tool calls; 221 cycles in all, the dry run's included; recently ~26 a day |
| Etsy | 7 digital listings at €5.00–6.90 and one bundle at €39, plus 2 Printify posters at €22.90–32.90; 43 listing edits |
| What buyers did | **14 listing views in all (0–4 a listing), 0 favorites, 0 orders** (diag:1912-1966) |
| Pinterest | the report's 10 newest pins: 9 live, with 7 impressions, 1 click and 0 saves in all; 1 refused (diag:1939-1950) |
| Bluesky | 21 posts, 116 followers. Ember's own count on 10-11: 33 reactions, and 0–4 views on each linked listing (diag:1565, 2320) |
| Blog | 6 German posts; no measurable traffic (case #22) |
| KDP | one book, rejected by you four times (#77, #81, #84, #85) for layout and content errors |
| Quality | Ember's own critic scored the 10 newest checks 4–6 of 10, each "improve"; launch needs 7 |
| Learning | 11 bets: 0 won, 4 lost, 1 without reach, 6 open. The report's 20 newest cases: none "worked" |
| Ventures | 27 ideas, most parked. One week's review counted 24 venture cycles ($10.45) and 14 ideas later killed as "too early" (diag:1716) |

**Your own notes doubt the demand.** On ventures #9 and #10 you wrote that a free AI chat makes such a letter or
spreadsheet from one prompt (diag:1519-1520). Your rulebook asks that every product "beat what a free AI chat gives"
(diag:1630). The critic's scores and your four rejections say the products don't clear that bar yet.

**Ember's own reading agrees.** Its weekly review of 10-09: "Nine live listings drew about 11 views in total and
earned nothing. Reach is the whole problem."

---

## 5. The economics

### 5.1 What Ember spends (live, computed)

- **Over the whole life:** $5.60 a day.
- **The last 7 days:** $6.23 a day.
- **A cycle:** mean $0.28, median $0.23, from $0.15 to $1.05 (cycles #150–#221). That is about 26 cycles a day.
- **Fixed cost per cycle:** about $0.15, roughly half of a cycle without a workshop run. It is made up of:
  - the Opus plan, about $0.09: ~16,000–17,700 input tokens that are never cached;
  - the first worker call, which rewrites the cache;
  - the reflection.
- **Why the spending reaches the cap:**
  - All 72 cycles in the report's event log chose a sleep of 60 to 360 minutes. 48 chose 360, the most allowed.
  - Ember's code cut every one of them to 30 minutes with "your plan has steps ready" (`agent/slack.py:39-44`,
    `agent/loop.py:398-413`).
  - That is your rule: the daily cap is the only brake.
  - Ember itself judged that there was nothing worth a cycle for hours. The code woke it anyway, and the caps then
    refused much of what it tried (section 6).

### 5.2 What a sale brings (code, computed)

The assumptions:
- Etsy's fees for a German seller without a VAT ID (`agent/econ.py:25-30, 93-100`);
- 1.13 USD per EUR;
- no Offsite Ads.

| Product | Price | Kept per sale |
|---|---|---|
| Digital template | €5.00–6.90 | $4.41–6.31; average **$5.56** |
| Licence bundle | €39 | $38.32 |
| Printify poster (you pay shipping and Printify's VAT) | €22.90 | about $6.57 |
| KDP paperback, 83 pages | $7.99 | $1.70: KDP pays 50 % below $9.99, minus $2.30 printing (`integrations/kdp.py:108-126`) |

### 5.3 What breaking even takes (computed)

Today Ember gets about 1.33 views a day across 9 listings. This table assumes the digital template mix and 2 %
conversion:

| Daily spend | Sales a day | Sales a month | Views a day | Times today's traffic |
|---|---|---|---|---|
| Today, $6.23 | 1.12 | 34 | 56 | **42** |
| Your cap, $7.00 | 1.26 | 38 | 63 | 47 |
| The shipped default, $1.50 | 0.27 | 8 | 13.5 | 10 |
| Lean, $1.00 | 0.18 | 5.4 | 9 | 6.7 |
| The floor, about $0.55 | 0.10 | 3 | 5 | 3.7 |

At 1 % conversion every multiple doubles.

If every sale were the €39 licence, today's spend would need 5–6 sales a month, or about 8 views a day. That one listing
had 4 views in about 4 days. It is the best lottery ticket Ember holds, but a €39 commercial licence from a shop with no
reviews is a hard sell.

### 5.4 When the money runs out (code, computed)

- **At $6.23 a day, the balance reaches zero around 10-31.**
- **In practice Ember stops earlier.**
  - Runway falls under 2 days around 10-27 to 10-29, with $12–14 left (`economy/life.py:50-51, 351-354`).
  - Ember then turns critical and writes its last will.
  - Under your `invest` stance it then goes dormant.
- **Your $100 goal is due 11-15**, after that point.
- **Only a grant from you revives a dead life.** A sale doesn't, and nothing counts a grant as earnings.

---

## 6. Why the loop doesn't converge on money (code, live)

### 6.1 Who decides what

- **The code decides what and when:**
  - the step, its product and the cycle's kind (`agent/loop.py:522`, `agent/plan.py:2385`, `agent/weights.py:230-246`);
  - when a step closes, from records and never from Ember's own words;
  - how long Ember sleeps.
- **The model decides how:** the plan, the tool steps, the reviews, the critic and the reflection.
- **The model also writes much of what the code then weighs:** projects, ventures, promises, bets and revenue targets.

### 6.2 What the weights optimize

The formula: weight = worth × kind × channel × (1 + urgency + age + momentum) (`agent/weights.py:178-181`).

- **Worth** = prior(could earn) × stage chance × evidence (`agent/weights.py:92-114`).
  - "Could earn" is a target, not a result. The template default is $5 a month for Etsy and $10 for KDP.
- **Evidence** stays at 1.0 below 30 views (`EVIDENCE_VIEWS`, `agent/weights.py:56`). The best listing has 4.
- **Channel** applies only when at least 4 items have been live for 7 days. All 24 picks the report logs show 1.0.
- **Neither cost nor realized revenue enters the weight.** A step's cost is stored but never weighed. An order counts only
  through evidence after 30 views, or at a product's day-21 decision.

In practice the ranking comes down to:
- a fixed worth by product type and stage (2.54 for the KDP book, 2.0 for every Etsy product);
- whether the launch checklist is done (2 pins, 2 posts, a blog post, a critic score of 7);
- promise dates;
- your pins.

The result is an honest, well-logged launch checklist that runs whether or not anything sells.

### 6.3 Five structural problems

1. **The chooser doesn't see your caps.**
   - Only the tools read them: the 3 publish requests that may wait (`PENDING_CAPS`, `agent/store.py:323`) and the 2
     unprompted messages a day (`MESSAGES_PER_DAY`, `agent/tools.py:116`).
   - `plan.py`, `weights.py` and `slack.py` never do. So a step looks ready, the sleep is cut, a plan is paid for, and
     the request is refused.
   - Of the 24 picks the report logs, 17 were marketing steps, and 9 of those hit a full queue.
   - Five cycles in a row (#193 to #197), and two more on 10-11 (#219, #220), tried to send the same report to you and
     were refused by the message limit: $1.55.
2. **Promises inflate.**
   - A message that promises later work must record the promise.
   - Each promise becomes a step worth at least 3, more than any Etsy product (2.0).
   - The kind "promise" isn't in `KIND`, so a promise to report weighs 1.0 instead of a report's 0.3
     (`agent/plan.py:2283-2285`).
   - Of the 18 promise steps so far (5 still open), 9 are about one KDP book and 7 are reports.
3. **The signal is too small to learn from.**
   - Bets of +3 to +10 views settle on noise: 4 lost, 0 won.
   - The rule that fits this best, "plan reach with every launch", rests on 4 cases of 0–3 views each.
4. **Breadth comes before concentration.**
   - Every product line gets the same checklist and the same launch urgency.
   - A streak on one line ends after 3 cycles (`STREAK_CAP`).
   - So 8 lines take turns, and none gets enough attention to find out whether it can sell.
5. **The learning doesn't reach the steering.**
   - Of Ember's 17 lessons, 9 are about its own tools and caps, although the constitution says "a tool's limits are no
     lessons" (`agent/constitution.md:67`). Only 2 are about the market.
   - The message-limit lesson was learned four times (c197, c203, c219, c220), and behavior didn't change, because the
     code picks the step.
   - The reviews diagnose correctly ("Reach is the whole problem"). Their verdicts reach the next plan as text, not the
     weights or the cadence.

### 6.4 Where the cycles go (live)

| Outcome | 30 cycles (#192–#221) | Their cost |
|---|---|---|
| Shipped something a buyer can see (listing edit, pin, post, blog edit) | 9 (30 %) | $3.02 (35 %) |
| Blocked by the queue of 3 publish requests | 9 (30 %) | $2.57 (30 %) |
| Blocked by the 2-message limit | 7 (23 %) | $1.55 (18 %) |
| Reports and replies to you | 3 (10 %) | $0.78 (9 %) |
| KDP rework, not shipped | 2 (7 %) | $0.73 (8 %) |

Cycles that you woke shipped something 3 times out of 3. Cycles the scheduler woke shipped something 2 times out of 9.

**You are the throughput.** More autonomous cycles add little while each outward step waits for your click.

### 6.5 What is genuinely strong

- **The safety model is strong.**
  - Approvals are enforced twice, in Python and SQLite.
  - The never-automatic list holds whatever you unlock.
  - The kill switch stops even a round already sending.
  - AI disclosure, § 7 UWG and § 5 DDG are built into the rules.
  - Secrets never reach the database or the logs.
- **The money guard held, nearly to the cent.** No day went over your $7 cap. One cycle (#218) ended $0.05 over its $1
  cap, because a workshop run cost $0.74 against a $0.60 estimate. The guard holds worst-case reservations, refuses
  without retrying, and keeps a reserve for Ember's last will.
- **Step choice is deterministic, logged and re-scorable.** Steps close on records, not on Ember's own words.
- **The experiments are clean.** "Unseen" is kept apart from "unwanted", and evidence is graded by its source.
- **The diagnostics are excellent.** This analysis was possible because of them.

The machinery is sound. What it lacks is a market that answers it.

---

## 7. Outside base rates (outside; *weak* marks a vendor blog, a forum or a single self-report)

### 7.1 Competition on Etsy

These listing counts are RankHero estimates from 2026:

| Search | Listings | Searches a month | Note |
|---|---|---|---|
| "cv resume template" | 25,559 | | |
| "resume template ats" | 15,158 | 49,500 | |
| "budget template google sheets" | 20,918 | 40,500 | most priced around $6.99 |
| "job application tracker" | ~800 | 5,400 | median $5.70; the thinnest of Ember's niches |
| "lebenslauf word" | 1,673 | 1,900 | most listings come from US sellers, while most demand is in the EU |
| "bauhaus poster" | 29,648 | 18,100 | median price $12; Ember's posters are 1.5 to 2.5 times that |

Free Haushaltsbuch and Nebenkostenabrechnung templates are easy to find.

### 7.2 Etsy as a whole

- **Size:** 5.6 million active sellers and $10.46 B of 2025 sales, about $1,870 per seller on average. The median is far
  lower (Etsy's 10-K for 2025, filed 2026-02-19).
- **Conversion:** usually quoted at 1–3 % of visits (*weak*).
- **First sales:** forum reports put a new shop's first sale anywhere from days to more than 3 months after opening.
  Guides speak of traction in months 3–4, after 20–30 or more listings (*weak*).

### 7.3 Etsy's rules

- **AI items** are allowed if made from your own prompts, marked "Designed by", with the AI named in the description
  (Creativity Standards, July 2024). Ember follows this.
- **The API terms** don't forbid listings created through the API.

### 7.4 KDP

- **Royalty:** 50 % below $9.99 / €9.99, since 2025-06-10.
- **VAT:** planners are taxed at 19 %, not the book rate.
- **Upload limit:** 3 new titles a day.
- **AI content must be disclosed.**
- **Sales data:** none public for low-content titles (*weak*).

### 7.5 Pinterest and Bluesky

- **Pinterest:** a new account can expect near-zero reach for 30 to 90 days (*weak*). Pinterest labels AI-modified
  images.
- **Bluesky:**
  - Its active users fell by about half from late 2024 to mid 2026 (Similarweb via Gigazine, 2026-08-12).
  - Its referral traffic to publishers was about 0.8 % of X's (Chartbeat, late 2024).

### 7.6 AI agents trying to earn money

| Case | Outcome |
|---|---|
| Project Vend 1, Anthropic (2025-06-27) | Claude ran an office shop. It "did not succeed at making money", people restocked it, and Anthropic "would not hire Claudius" |
| Project Vend 2, Anthropic (2025-12-18) | "Weeks with negative profit margin were largely eliminated", but no total profit is published; purchases needed a person's approval, and the agents "still needed a great deal of human support" |
| Andon Market (San Francisco, 2026) | A real store run by an AI with human staff; down about $62k after four months |
| AI Village merch stores (2025) | The best agent made $126 profit on $564 revenue in three weeks, with a large audience of spectators |
| Enrich Labs Shopify store (self-reported, *weak*) | $10k of sales from paid ads; not profitable; a person kept control of the money |
| Truth Terminal and similar viral agents (*weak*) | Earned through an existing audience and speculation, not commerce, with a person owning the funds |
| Vending-Bench 2 | Shows profits, but in a simulation that doesn't test finding customers |

**I found no documented case of an AI agent that paid for itself through organic e-commerce.** Every real attempt lost
money, or depended on people, ads or an audience it already had.

The one channel the cases suggest is attention: a real AI with real money and a real chance of dying is a story, as
`vision/live-page.md` says. Ember's live page and Bluesky account try this; with 116 followers it hasn't caught on yet.

---

## 8. How the odds were reached

### 8.1 The odds of a first sale (computed)

The model:
- **Traffic:** the per-listing view rate is fitted to your 14 views over ~94 listing-days (a Gamma posterior), across 9
  listings.
- **Conversion:** a prior with a median of 1 % (or 0.6 % for a shop without reviews), ranging over about a factor of
  three either way.
- **Traffic over time:** held flat, or grown by 25 % a month.

| Median conversion, traffic | By ~10-27 (17 days) | 90 days | 180 days |
|---|---|---|---|
| 0.6 %, flat | 15 % | 53 % | 74 % |
| 1.0 %, flat | 23 % | 68 % | 86 % |
| 1.0 %, +25 % a month | 24 % | 78 % | 96 % |

A first sale is likely if Ember keeps running. But one sale of a €6 template covers about 21 hours of today's spending.

### 8.2 The odds of paying for itself (judgement)

The traffic it would take is well grounded: 42 times today's at today's spend, about 7 times at $1 a day (section 5.3).
Whether traffic grows that much is the uncertain part. Four things weigh against it:
- **Ember's own record.** Two weeks of pins, posts, blog posts and edits left traffic flat at about 1 view a day.
- **Quality.** The critic scores 4–6 of 10 against a bar of 7, and you rejected the KDP book four times.
- **The market.** The niches are crowded, and free AI chat substitutes for most of what Ember sells (section 7.1, and
  your own notes).
- **The precedents.** No AI agent has paid for itself through organic sales (section 7.6).

The 0.13.0 analysis gave these odds on 09-30: "about 25 % of covering a harvest-level burn in Nov-Dec; about 10 % of
covering $1.90 a day; under 3 % at today's burn", when today's burn was $4.89 a day. Eleven more days of flat traffic and
a higher burn lower each of them a little. Hence about 2 % at your current settings and about 10 % at ~$1 a day.

### 8.3 What the odds are most sensitive to

1. **The daily spend.** It divides the traffic needed by up to 11.
2. **Conversion.** It doubles or halves everything.
3. **A traffic lever outside Ember's reach today:** ads, a listing that starts to rank, the Haushaltsbuch season, or
   attention to Ember's own story.

---

## 9. What would raise the odds, in order of effect

1. **Spend about $1 a day instead of about $6.**
   - Make these changes together:
     - `min_sleep_minutes` and `wake_interval_minutes` at 360;
     - a Sonnet planner;
     - about 10 tool steps;
     - at most one workshop run a day;
     - a daily cap near $1;
     - a cycle cap of at least $0.35.
   - A lower cap alone isn't enough: cycles would still fire every 30 minutes and then sit idle.
   - The effect: the traffic gap falls from 42 times to about 7 times. The money lasts about four months instead of about
     two and a half weeks, through the Nov–Jan season.
   - It gives up most of Ember's activity, but little reach, because reach is already limited by your approvals.
   - This is close to the shipped defaults ($1.50 a day, $0.50 a cycle, a Sonnet planner). It reverses your rule #6
     ("don't sleep to save money"), so it is your call.
2. **Let the chooser see your caps.**
   - Treat a full queue and a used message limit as "waiting" in `plan.candidates()` and `plan.busy()`
     (`agent/plan.py:1858, 2259`). Then no plan is paid for when its request can't be made.
   - Weigh a promise to report as a report (0.3).
   - Half of the last 30 cycles ran into one of these two limits. Most of their cost would be saved, or spent on work
     that can ship.
3. **Concentrate, and buy a signal.**
   - Pick one or two products, the ones with the most demand per competitor: the job application tracker, the German
     Lebenslauf or the Nebenkostenabrechnung.
   - Bring them to the critic's 7.
   - Fund a small Etsy Ads test, €1–3 a day for 14 days (venture #13, parked).
   - Only you can spend money. But two weeks of paid views measure conversion. Organic views at today's rate would take
     about a year.
4. **Quality before reach.** Pins that lead to a listing scored 5 of 10 waste the views they bring. The critic's own fixes
   are concrete: files the description promises, covers readable as thumbnails, and bundles a free AI chat can't produce
   in one prompt.
5. **Let one version run for 4–6 weeks.** The market answers in weeks. 54 versions in 10 days meant no version of the
   steering was ever judged by sales.
6. **For more autonomy, once a line sells,** unlock `listing_variant` on it. Before the first sale it only automates
   listings nobody sees.

---

## 10. Code health (code)

- **Tests:** all 2,960 pass at `25f96ee` with `python -m pytest -W error -n 4`, as CI runs them (10 min 37 s on 4
  CPUs). `ruff check` and `ruff format --check` are clean.
- **The README's security section is out of date** (section 3.4).
- **What is strong** is in section 6.5.
- **The risks in this report are economic and structural, not defects.**
