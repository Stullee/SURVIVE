# Ember 0.39.5: why Ember earns nothing yet

*Your diagnostics report of 2026-10-15 11:06 UTC (13:06 Berlin, version 0.39.5), read against `938619b`, the head of
the tracked branch, which is the 0.39.5 you run. The question this analysis answers is yours: why Ember earns no
money, what holds her back most, and what has to be added or fixed. Nothing in the app changes with this report.*

**Evidence.** I read the whole report (7,960 lines):
- the economy, the ledger and the scheduler's state;
- the planner's context as the next plan would see it, and the plan tree's ranking, waits and the last 24 picks;
- cycles #288 to #299 in full (00:07 to 07:55, all under 0.39.2), and the events since 10-12 (cycles #242 to #299);
- the records: projects, ventures, approvals, messages, reviews, bets, cases, quality checks, Etsy, Pinterest,
  Bluesky, Printify, KDP and the blog.

I checked every finding against the code. Labels, as in the earlier analyses:
- **live**: it is in your report.
- **code**: found by reading the code.
- **calc**: my arithmetic on the report's numbers. Market figures I could not check from here (an Etsy click's price,
  a shop's conversion rate) are given as ranges and say so.

**Paths** are relative to `ember/app/`. **Times** are Berlin time unless they say UTC.

---

## 1. The short answer

- **Nobody sees the products, so nobody can buy them** (2, 3.1). Since 09-29, the 7 Etsy listings have had 15 views
  in all: one view a day for the whole shop. 0 favorites, 0 orders. $0 is what that traffic predicts. The products
  haven't been rejected by buyers, because buyers have never seen them.
- **The money goes to work that can't bring a buyer** (3.2). Ember spends $7 a day, the whole cap, in 14 to 23
  cycles a day, most of them at night. Most cycles choose to sleep 4 to 6 hours, and Ember's code cuts it to 30
  minutes because "your plan has steps ready". Of the last 24 cycles ($8.15), 18% went to reach (pins). 24% redesigned covers of listings with 1
  to 3 views, and 10% went to new venture ideas.
- **The one product closest to a sale is one renderer feature away from release** (3.3). The owner on KDP request
  #120 at 07:48: "finally looking good. last small change is to extend the lines for the notes … after that I will
  approve and release." Ember's `::: lines N` only draws a fixed number of lines, so the agent guesses N by test
  renders. At 07:55 it hit `make_document`'s limit of 4 a cycle, and the next cycle comes at 20:00: the night spent
  the cap.
- **What would bring buyers is decided but never done** (3.4). The daily reviews of 10-13, 10-14 and 10-15 each asked
  for a small Etsy Ads test and a German title and tag batch. Neither request exists. The plan tree has no step for
  them, so the cycles do the tree's steps instead: critic chores, then monthly blog posts for a blog the review
  stopped.
- **At today's spending even the goal loses money** (3.6, calc). $7 a day is about $210 a month. An Etsy sale nets
  about $5 and a KDP copy about $1.70 to $2. Breaking even takes about 40 Etsy sales a month, and the shop gets about
  30 views a month.

What to do, in short (4):
1. **You, this week:** finish the KDP book, turn on a small Etsy Ads test, lift the title and tag freeze, lower the
   daily cap until the first sale, and decide the six requests waiting for you.
2. **Code, for 0.39.6:** let the agent's sleep stand unless real work is ready, keep some of the day's money for your
   hours, add `::: lines fill`, make the reviews' decisions plan steps, and stop scoring a stopped channel as
   untested.

---

## 2. Where the business stands: live

| | Now (13:06) |
|---|---|
| Money | $200 granted, $99.29 spent on the API, $0.60 on fees, **$0 earned**. Balance $100.11 |
| Spending | $48.75 in the last 7 days (about $7 a day, the cap). Runway 14.4 days: money runs out about **10-29** |
| Etsy | 7 listings live since 09-29 to 10-03: **15 views in all**, 0 favorites, 0 orders. Views per listing: 4, 3, 3, 2, 2, 1, 0 |
| Etsy changes | 55 changes of the 7 listings (the 10 latest: 8 new photos, 2 file and description fixes). Covers went up to version 18 (job tracker) and version 12 (Nebenkosten) |
| Printify | 2 posters on Etsy, 1 view each, 0 orders |
| Pinterest | 16 pins. The 10 newest have 30 impressions together and **0 clicks** |
| Bluesky | 23 posts, 151 followers, 2.3 reactions a post, "0 link clicks measurable" (message #184) |
| Blog | 7 German posts on ember-ai.de. The review stopped the test on 10-14 for near-zero traffic |
| KDP | Haushaltsbuch 2027 proposed 6 times (#77, #81, #84, #85, #109, #120), all rejected. #120: "finally looking good", one fix left |
| Waiting for you | Keep or drop lines #3 to #7 (#175 to #179) and line #10 (#189) |
| Ideas | 39 ventures and 14 projects in 17 days. 10 projects open |

The goal (#23, $100 a month by 11-15) is at 0%. Ember's own review of today calls it "very unlikely" and says the
runway ends about 10-31.

---

## 3. What holds her back

### 3.1 Reach: almost nobody is shown the products (live)

Every number points the same way: no listing has had enough views to show whether its photo, price or product
works. With 15 views, and an Etsy shop's usual conversion of very roughly 1 to 3 in 100 visits (a common rule of
thumb, not a figure from your report), the expected sales so far are 0.15 to 0.45. Zero is the normal result.

Why Etsy shows them so little:
- **A new shop in the most crowded corners of Etsy.** CV and cover letter templates and budget planners are among
  Etsy's largest digital categories, full of shops with thousands of sales and reviews. Etsy's search favours
  listings that already sell. A shop with no sale and no review starts at the bottom.
- **The free channels Ember can use need an audience she doesn't have yet.** Pinterest (a new account), Bluesky
  (151 followers who came for Ember's story, not for templates: case #25) and the blog (a new domain) all take
  months to bring traffic. Ember's own cases say so (#22, #23, #25, #34), and the pins confirm it: 0 clicks.
- **The one lever that buys views now, Etsy Ads, is yours, and nobody asked you for it** (3.4). Ember has no tool
  for it, and Etsy's API has no ads.
- **Pins are throttled by one listing** (3.5): the Pinterest queue is full until 10-20 with three pins of the
  Haushaltsbuch listing, so no other listing can get a pin before then.
- **Titles and tags are frozen until 10-20** (your rule #1). Titles and tags are the main thing Etsy's search reads.
  The freeze protects a before/after measurement that one view a day can't make.

Ember's API view count can't tell "not shown" from "shown but not clicked". Etsy Ads' dashboard shows impressions
and clicks for each advertised listing. That is the first data that would say whether the covers are the problem
(shown, not clicked) or the reach is (not shown).

### 3.2 The spending: $7 a night on work that can't bring a buyer (live, code)

**The pattern** (events, Berlin time):

| Day | Cycles | Spent | When |
|---|---|---|---|
| 10-13 | 14 | $6.58 | every 30 min from 00:07 to 04:55, then 08:31, 10:23, 20:02, 20:33 |
| 10-14 | 23 | $6.66 | every 30 min from 00:07 to 10:01, then 12:24, 20:03, 20:36 |
| 10-15 | 16 | $5.59 | every 30 min from 00:07 to 07:55; nothing more until 20:00 |

Every one of these cycles ended with "chose 360 min of sleep, cut to 30 (your plan has steps ready)" (or 120, 180,
240 or 600 min). The cut is `slack.sleep` (`agent/slack.py:46`), called by `_cut_sleep` (`agent/loop.py:396`). It
cuts whenever `plan.busy` (`agent/plan.py:2566`) finds *any* ready step that wasn't already tried. The step's weight
doesn't matter, so a chore of weight 1.1 cuts the sleep like the KDP book does. The agent's reasons for the long
sleeps were right, for example #297: "Cheap, low-activity cycle: baseline recorded, frozen on edits until 10-20,
and waiting on owner's request #123. No urgent action possible."

**What the last 24 cycles bought** (#276 to #299, 10-14 06:58 to 10-15 07:55, calc from the plan picks and the
events):

| Work | Cycles | Spent | Share |
|---|---|---|---|
| The KDP book (line #14) | 5 | $2.26 | 28% |
| New covers and files for listings with 1 to 3 views ("The critic passes it") | 5 | $1.95 | 24% |
| Pins (reach) | 3 | $1.46 | 18% |
| New venture ideas: brainstorm and triage | 4 | $0.85 | 10% |
| Licence pack: demand note, checks, pin pictures | 3 | $0.78 | 10% |
| Blog, Bluesky, channel set-up | 3 | $0.68 | 8% |
| A views baseline | 1 | $0.17 | 2% |
| **All** | **24** | **$8.15** | |

- **A cycle costs at least about $0.17.** #297 did nearly nothing and cost $0.168: $0.081 for the Opus plan of a
  17,000-token context (not cached), $0.054 to open the work conversation (21,000 tokens written to the cache), and
  $0.034 for two tool steps and the reflection. Sixteen cycles a night cost about $2.20 in plans and openings
  alone.
- **The critic's chores.** Since 0.39.1 a critic step on a line with fewer than 10 views weighs as a chore
  (`weights.CRITIC_VIEWS`, `agent/plan.py:2128`). It is still taken whenever nothing heavier is ready, and it still
  cuts the sleep. So #293 to #296 and #298 made new covers and files for listings with 1 to 3 views. Ember's own
  case #35 says this can't work yet: "With 0-4 views per listing there is too little impression data to test
  thumbnails." Each of those cycles also became an Etsy request for you to approve (#121 to #125, at 07:45).
- **It works when you sleep and has no money when you are awake.** From 00:07 the night may spend four fifths of
  the cap ($5.18 by 07:29 today). Until 20:00 the last fifth is kept for event wake-ups (`event_reserve`,
  `agent/service.py:439`). Your decision at 07:48 got one cycle (#299). It found the fix but hit `make_document`'s limit. The cycle that
  could finish it waits until 20:00: "This cycle may spend up to $0.01."

Your standing instruction #6 says "Don't … sleep to save money; idle time is a cost too." That holds while a cycle
can move money closer. These cycles mostly can't: their work goes to listings nobody is shown.

### 3.3 KDP: six rejections, mostly on what the renderer couldn't do (live, code)

The Haushaltsbuch is Ember's best chance of a first euro soon. Amazon brings its own search traffic, a printed book
is something a free AI chat can't hand a buyer, Ember says the season runs from November to January, and you wrote
that you will release it after one more fix.

The six rejections were all page layout: white space, tables that spill over, "week 1-4" labels, uneven rows, and now
note lines too short. 0.38.x and 0.39.1 fixed the renderer's row heights. What is left:
- `::: lines N` takes a count (`products/markup.py:523`). `writing_lines` (`products/pdf.py:536`) draws exactly N
  lines of 8 mm. Nothing draws lines "down to the bottom margin", which is exactly what you asked for. So #299
  rendered tests with 6, 3 and 2 lines under a 13- and a 14-row table. It was still looking for the fit when its 5th
  `make_document` was refused.
- Its handoff says 14 rows and `::: lines 2` "fits exactly on 1 page". Its own render of that page "still spills by a
  hair" (call #4000). So the next cycle starts from a wrong premise, and any count it picks either spills or leaves a
  gap: a 7th rejection for "white space" is likely.
- The book check names a part that runs on past its page. It says nothing about a page whose lines end well above
  the bottom margin, though that is your complaint.

### 3.4 The reviews decide, but the plan tree does something else (live, code)

- **Etsy Ads.** The reviews of 10-13 ("plus a 7-day Etsy Ads test at the minimum daily budget on the coach bundle,
  ATS resume and Nebenkosten"), 10-14 and 10-15 each made it the plan. No request was made: none of #111 to #125 is
  one, and the plan tree has no step for it. The review says "A change … waits for its line's cycle". But the line's
  cycle takes the tree's heaviest step, which was a critic chore every time.
- **The German title and tag batch.** The same three reviews say it "goes out today" or "is in the pending batch".
  No batch exists, and your rule #1 forbids it until 10-20. The reviews keep planning around work that can't happen.
- **The blog.** The review stopped line #10 on 10-14. Yet the plan tree's heaviest steps after the KDP book are now
  four "This month's blog post for …" steps, at 2.0 each. `_channel_factors` (`agent/plan.py:1964`) scores
  Pinterest by clicks (0 so far: the floor, 0.2, once four pins are a week old) and Bluesky by reactions. It never
  scores the blog, so the blog keeps an untested channel's 1.0. The channel nobody measures outranks the one that
  was measured.
- **Bluesky is scored by reactions** (2.3 a post, factor 0.76). They come from people following Ember's story, and
  case #25 says those people don't buy. Reactions are the wrong measure for a sales channel.

### 3.5 One listing holds the Pinterest queue for five days (live, code)

The queue holds approved pins #117, #118 and #119. All three link the Haushaltsbuch listing #4584938666, so
`LINK_DAYS = 2` gives them slots on 10-16, 10-18 and 10-20 (`integrations/publish_queue.py:47`). `WAITING = 3`
(line 48) counts a pin waiting for its slot like one waiting for you. So until 10-20 Ember may propose no pin for
any other listing: the steps for lines #3, #4, #5 and #7 wait with "queue". Rule #1's first order was "pins for every
live listing". Lower priority than 3.1 to 3.4, since pins have brought no click yet.

### 3.6 The products and the money (live, calc)

- **The critic never passes a listing.** The 10 latest checks score 5 or 6, and the pass mark is 7 (`quality.py`
  MIN_PASS). Its notes are concrete: a cover with a calculation error (Nebenkosten: "1.165 € fällig" and "1.165 €
  gezahlt" giving "42 € Nachzahlung"), "a plain 4-row table: no chart, no dashboard", US number formats on a German
  sheet. The covers are drawn by Ember's code (`make_image`, or PIL in the workshop), not screenshots of the real
  file. This will matter
  once the listings are shown. Until then, more versions can't be judged.
- **Your doubt is the market's.** On venture #10 you wrote "I really don't see anyone paying for a spreadsheet if I
  can just go to some AI for free", and on #9 "that's exactly what every ai … does with one prompt". Generic CV
  templates and budget sheets are what a free AI chat makes best. The products with an edge are the ones it can't
  hand over: a printed book (KDP), a legally specific tool (Nebenkosten, with BetrKV and HeizkostenV), and a licence
  for professionals (the €39 coach bundle, which has the most views).
- **The arithmetic.** An Etsy sale at €6 keeps about €4.80 after Etsy's fees, about $5.40. A KDP copy keeps about
  $1.70 (Ember's figure in message #188; a little more or less with the page count). At $7 a day ($210 a month):

  | Target | Etsy sales a month | Views a month at 1 to 3% (rule of thumb) |
  |---|---|---|
  | Break even on the API | about 40 | about 1,300 to 4,000 |
  | Your goal, $100 a month | about 19 | about 600 to 1,900 |
  | Today | 0 | about 30 |

  The gap is 20 to 130 times the traffic. No amount of polishing closes it. More reach would, and a lower burn gives
  that reach the weeks it takes.

---

## 4. What to do

### 4.1 You, this week (no code needed)

1. **Release the KDP book.** Approve it once the notes run to the bottom margin. If Ember's next try still leaves a
   gap, a version with code change 4.2.2 can do it exactly. KDP's review takes up to 3 days, so this week means
   listed before November.
2. **Turn on Etsy Ads for 14 days at a small daily budget (about €1 to €3)**, on the 3 listings the reviews named:
   the coach bundle (€39), Nebenkosten (in season) and the ATS resume. It is the only lever that puts the listings in
   front of buyers this month. Its dashboard says whether they are shown, clicked and bought, which no change of
   cover can tell today. Etsy sets the price of a click. Look at the real cost after two or three days, and record
   it as an expense so Ember sees it. If a few hundred ad clicks sell nothing, that is the first real evidence
   against a product or its price.
3. **Lift the title and tag freeze now** (rule #1). It protects a measurement that one view a day can't make. Let
   Ember send the German keyword batch as one request.
4. **Lower the daily cap until the first sale**, from $7 to about $2 to $3, or choose the spending stance "steady".
   At $2.50 a day, the $100 left lasts about 40 days instead of 14: into the KDP season and through an ads test.
   The cap is the only brake by your design, so this is your decision. Code change 4.2.1 makes the same money go
   further at any cap.
5. **Decide the six requests that wait for you** (#175 to #179, #189). A sensible answer: keep every listing live
   (they cost nothing more), stop work on all but the three in the ads test and the KDP book, and close line #10.
   Tell Ember in one standing instruction: no new ventures and no cover redesigns until a listing has 100 views.
6. **Don't judge the lines on 10-20 by "under 10 views".** At this traffic that judges the reach, not the product.
   Judge them after the ads test.

### 4.2 Code changes for 0.39.6, by what they save or earn

1. **Let the agent's sleep stand unless real work is ready** (`agent/slack.py`, `plan.busy`). Today any ready step
   cuts it to 30 minutes.
   - These never cut it: a chore (a quiet critic step), a report or tracking step, venture triage or a brainstorm,
     and a step of a channel at its floor.
   - A step that can bring a sale nearer still does: shipping, a launch, a real fix, marketing, a promise or a
     decision due.
   - On 10-15, 10 of the 16 cycles did chores, checks, tracking or venture triage (#288 to #298, except #292).
     After the KDP request went out at 01:44, #288 chose 6 hours. That sleep would have stood until your decision
     woke Ember at 07:54.
2. **`::: lines fill`** (`products/markup.py`, `products/pdf.py`): writing lines from where the block starts down to
   the bottom margin. The Word copy gets the count the PDF drew.
   - The book check warns about any page with something to fill in that ends more than 2 lines (16 mm) above its
     bottom margin. That turns your complaint into a check.
   - The KDP guide tells the agent to end notes with `fill`.
3. **Keep part of the day's money for your hours.** Spend at most about half the cap before 08:00. Then a decision
   you make in the morning gets its follow-up the same morning, not at 20:00. Alternatively: a cycle woken by your
   decision may run `make_document` and `look` more often than usual, for the product you decided on.
4. **Make the reviews' decisions plan steps.**
   - When the review says "change" and names a request for you (an ads test, a keyword batch), that request becomes
     a step of its line, with the line's worth and the review's urgency.
   - A request your own rule forbids (titles before 10-20) waits until that date. The review stops repeating it.
   - The Etsy Ads request goes through `request_approval`, with its daily budget, and the plan tree waits for its
     result.
5. **Score channels by what they bring to the shop.**
   - A channel whose test the review or you stopped (the blog) weighs at the floor, and its recurring steps stop.
   - Bluesky is scored by the listing views or clicks that follow a post, not by likes.
   - The blog's monthly steps are not ranked second while the blog stays stopped.
6. **Free the Pinterest queue** (`integrations/publish_queue.py`). `WAITING` should count only requests that wait for
   you, not approved pins that wait for their slot. And the plan should prefer a listing that has no pin waiting.
7. **Make critic steps on unseen listings wait, not run as chores.** Below `CRITIC_VIEWS`, a critic step waits for
   views and proposes no photo change. That would have saved $1.95 and five of your approvals this morning.

Smaller, later: the plan call's 17,000 tokens are never cached ($0.08 a plan). The covers would be more convincing as
real screenshots of the file. Ember can't make those today.

### 4.3 What not to change

- The approvals. Everything that leaves still passes you. Every change above makes fewer and better requests.
- The critic's pass mark. Its findings are right (the cover's calculation error was real). Only its timing is wrong.

---

## 5. What to expect

- **$100 a month by 11-15**: very unlikely, whatever changes now. Ember's review says the same.
- **A first euro**: most likely from the KDP book in November, if it goes live this week, or from the ads test within
  two weeks, if one of the three listings converts at all.
- **Breaking even** needs both a lower burn (4.1.4, 4.2.1) and a channel that brings hundreds of visits a month. The
  ads test is the fastest way to learn whether one exists for these products. If a few hundred paid visits sell
  nothing, the products or prices are wrong, not the reach. Then the next products should be ones a free AI chat
  can't hand over: printed books, legally specific tools, licences for professionals.
