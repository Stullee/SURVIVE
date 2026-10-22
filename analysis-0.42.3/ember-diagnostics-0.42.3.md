# Ember 0.42.3: what the plan weighs, what the ventures cost, and how your three fixes got lost

*Your diagnostics report of 2026-10-22 13:35 UTC (15:35 Berlin, version 0.42.3), read against `67bf8c3`, the head of
the tracked branch (0.42.4; Ember's agent code is 0.42.3's but for one line of `audit.py`). 0.42.3 started at 14:09;
the twelve cycles in the report (#366 to #377, 10-21 15:46 to 10-22 12:50) ran 0.40.1 to 0.42.2. You asked for the
weights in the plan, the ventures and Ember's reasoning about its next steps: this analysis keeps to those. Nothing in
the app changes with this report.*

**Evidence.** I read the whole report (5,871 lines):
- the planner's context as the next plan would see it, with the plan tree's waits and settings;
- cycles #366 to #377 in full, the plan picks back to #353 with each weight's parts, the journal back to #348;
- the ventures, the reviews of 10-16 to 10-22, the messages, the standing instructions and the events.

Then I read the code that turns those records into a cycle's step (`agent/weights.py`, `agent/plan_ready.py`,
`agent/plan_steer.py`, `agent/plan_keep.py`, `agent/plan_ventures.py`, `agent/knockouts.py`, `agent/review.py`,
`agent/context.py`) and recomputed every pick below from its parts.

**Reproduced.** Your three Kassenbuch fixes (message #205) on the source I rebuilt for 0.40.1's analysis (the same
30,027 bytes the report shows unchanged since 10-21 11:34), rendered with 0.40.2's code: 67 pages, the page check
finds nothing, "Placeholders: none", the column "Text" 31 mm.

Labels, as in the earlier analyses:
- **live**: it is in your report.
- **code**: found by reading the code.
- **calc**: my arithmetic on the report's numbers.
- **reproduced**: rendered and checked here with Ember's code.

**Paths** are relative to `ember/app/`. **Times** are Berlin time unless they say UTC.

---

## 1. The short answer

**What works:**
- **Ember's own reasoning is sound, cycle by cycle.** It keeps cycles cheap ($0.18 to $0.21), leaves your paid test's
  listings alone, holds stopped lines, asks you for the books' Amazon links (#207), and caught a stale note before it
  rebuilt a finished book. It chooses 8 to 36 hours of sleep when nothing can move; your longest sleep (6 hours)
  shortens it.
- **0.40.2 works where it ran** (section 6): no venture step cut a sleep, line #12 is no longer a "KDP book", KDP's
  week is counted from Sunday 00:00 UTC and Ember plans the Kassenbuch for Sunday 02:00.

**What doesn't:**
- **Your three Kassenbuch fixes are lost** (3.3). Ember promised them in #206 ("next ordinary cycle on project #14
  will apply the 3 fixes"). No cycle did. Four cycles later its handoff says the opposite: "propose_kdp_book …
  as-is. No rebuild". On Sunday the page check will refuse v4 for its placeholder. The 12 mm "Text" column and the
  18 mm name fields will go out unless someone remembers #205.
  - Your message left Ember's plan the moment a cycle had read it: "FROM YOUR OWNER" shows only what is new since the
    last plan.
  - Ember answered without `commits`, though your standing instruction #7 says every promise goes there. So no
    promise, and no step, carried the fixes.
  - A venture's research ("the category is undated, drop 2027") wrote a note that a later cycle overwrote with "do
    NOT rebuild". That sentence then stood for everything.
- **The plan's heaviest steps were notes that say "do nothing"** (3.1). The daily review turns each "change" verdict
  into a step due that day.
  - Such a step weighs 4.0: the line's worth 2.0 × (1 + 1.0 for "due today").
  - That beats your promise (3.0) and the Kassenbuch's build step (2.5).
  - Six cycles on 10-21 and 10-22 ($1.34) did nothing but close "Review: … Kept passive at zero cost".
  - The same urgency cut Ember's sleep to 30 minutes four times from 04:43 on 10-22.
- **The ventures took a third of two days' money for nothing that can pass** (3.2). There were 9 venture cycles
  ($2.48): 3 brainstorms, 18 ideas, 17 parked.
  - Three of these cycles came right after you woke Ember following an upgrade.
  - One brainstorm ran after you had written "No brainstorm and no new ideas".
  - With a runway of 24 days, every new venture fails its "slow" knock-out (a first sale in 14 days or more, later
    than half the runway). No venture could have passed.
- **The Kassenbuch is in four places, none of them its line** (3.1): two promises (#67, #68) for the same thing; a
  step on line #4 (cover letters); and a venture (#49) still being "researched". Line #14, the KDP line, has no step
  for it at all.
- **The daily review can't see your messages** (3.3). Its focus on 10-22, "Promote KDP books #140 and #141 … linking
  to Amazon. Then finish Fahrtenbuch 2027", went against your #205 and against the KDP section ("no link at Amazon
  yet").

What to do, in short (4):
1. **You, today:**
   - send Ember the message in 4.1: the three fixes again, the handoff corrected, venture #49 parked;
   - add the Amazon links to #140 and #141;
   - hold the Ventures on the Plan tab.
2. **You, this week:**
   - after the paid test ends (10-23), keep or drop lines #3, #4 and #6, and drop #5, #7, #8 and #10;
   - tell Ember KDP's numbers by 10-27 (milestone #21 decides whether the KDP venture is parked on 11-03).
3. **Code, for the next release** (4.3):
   - keep your words in the plan until they are done;
   - let the review read your messages;
   - no review step on a line waiting for your keep-or-drop;
   - one promise in one place, waiting on KDP's week;
   - the ventures rest while none can pass;
   - plan_step names the step it changed.

---

## 2. Where things stand: live

| | Now (15:35) |
|---|---|
| Money | $200 granted, $121.99 for the API, $0.60 fees, **$0 earned**. Balance $77.41 (10-21 14:57: $80.73) |
| Spending | 10-21 **$5.88** (20 cycles), 10-22 **$1.59** by 15:35 (7 cycles). Last 7 days $22.70: a runway of 23.9 days |
| Next wake | 18:50, "Ember chose 360 min: Nothing actionable until the 10-23 paid-test check or the 10-25 KDP slot reset" |
| The plan | **No step ready**: 18 on a hold, 7 on a channel at its floor, 5 on a date, 3 on you, 2 on views, 1 on your paid test, 1 on an upgrade, 1 tried today |
| KDP | #140 and #141 published, **no Amazon link on either**. Kassenbuch 2027: the spec still names **v4** (the placeholder, the 12 mm column). This week's two paperbacks are used; the next week begins Sunday 10-25 at 02:00 |
| Etsy | 7 listings, 22 views, 0 favorites, 0 orders (the same since 10-18). Your paid test ends 10-23: +5 views, 0 favorites |
| Ventures | 57 in all: 1 live, 6 building, 1 researching (#49), 1 idea (#35), **48 parked or killed**. Since 10-21, 18 parked: 17 ideas of three brainstorms, and venture #5 by Ember's code |
| Waiting for you | Keep or drop lines #3 to #8 (#175 to #179, #215) and #10 (#189). The books' links (#207) |
| What you did since 0.40.1's analysis | Sent #205 (10-21 16:40), installed 0.40.2, 0.40.3, 0.41.0, 0.42.0, 0.42.2 and 0.42.3, and woke Ember after three of them |

---

## 3. What happened

### 3.1 The plan's weights: what each cycle took, and why (live, code, calc)

**The rule** (`agent/weights.py`): a step weighs

> worth × kind × channel × (1 + urgency + age + momentum)

- **worth:** a line's worth is what it could earn, damped (`prior`: $5 a month → 2.0, $10 → 3.17), × its chance by
  stage and sales. A promise is worth at least 3.0. A venture's step is worth 2.0 × (0.5 + its score / 100), or what
  its business case expects.
- **kind:** ship, fix, market and launch weigh 1.0, create 0.8, a chore 0.5, a report 0.3.
- **urgency:**
  - a promise: from two days before its day (2 at least, 12 at most);
  - a defect: 5;
  - a missed views bar: 4;
  - a launch's first marketing: 3;
  - **a step of Ember's code due today: 1**.
- **age:** 0.5 a day ready and untouched.
- **momentum:** 1 for the line the last cycles worked on (3 cycles at most). Another line's step must then be 25%
  heavier to take its place.
- **The sleep:** any ready step with urgency above 0 cuts Ember's sleep to your shortest, 30 minutes
  (`plan_steer._works`).

**What the cycles took** (`plan_picks`; each weight recomputed from its parts):

| Cycle | At | Its step | Weight, and why | What came of it | Cost |
|---|---|---|---|---|---|
| #366 | 10-21 15:46 | triage of ventures #46 and #49 | 2.42 × 0.8 = 1.94 | 2 research calls; #46 parked, #49 researching | $0.34 |
| #367 | 16:17 | research #49 (Kassenbuch) | 2.0 × 0.8 = 1.6 | "the category is undated: drop 2027"; a case of €40 a month; knocked out "slow" | $0.40 |
| #368 | 20:00 | #49's next question | 6.45 (the case's €42 a month) × 0.8 × (1 + age 0.08) = 5.56 | **read your #205** and promised the fixes for "the next ordinary cycle" | $0.22 |
| #369 | 21:23 | brainstorm | 2.0 × 0.8 × (1 + age 0.1) = 1.77 | you woke Ember after 0.40.3: 6 ideas, 4 parked at once | $0.31 |
| #370 | 22:42 | triage #52, #54 | 2.28 × 0.8 = 1.82 | you woke Ember after 0.42.0: #52 parked | $0.19 |
| #371 | 10-22 04:43 | review change, line #6 | **2.0 × 1.0 × (1 + 1) = 4.0** | the daily review, then "stays passive"; wrote the "go undated" note | $0.32 |
| #372 | 05:14 | review change, line #4 | 4.0 | "stays passive"; overwrote the note: "do NOT rebuild"; added the Kassenbuch's proposal to **line #4** | $0.30 |
| #373 | 05:46 | #272, that proposal (line #4) | 2.0 × (1 + momentum 1) = 4.0, kept by the 25% margin | made it wait for 10-25; message #207; the handoff "as-is" | $0.19 |
| #374 | 06:16 | #273, promise #68 (line #4) | 3.0 × (1 + momentum 1) = 6.0 | made it wait for 10-23: the 2nd and last wait of the day | $0.18 |
| #375 | 06:47 | review change, line #3 | 4.09 | "stays passive" | $0.19 |
| #376 | 12:48 | #243, promise #67 | 3.0 × (1 + age 0.17) = 3.5 | "ready"; its wait refused: 2 a day | $0.20 |
| #377 | 12:50 | triage #54 | 2.28 × 0.8 × (1 + age 0.29) = 2.36 | you woke Ember after 0.42.2: #54 parked | $0.21 |

**What the two days paid for** (10-21 00:03 to 10-22 12:50, cycles #351 to #377, calc):

| What the cycle's step was | Cycles | Cost | Share |
|---|---|---|---|
| Ventures: 3 brainstorms, triages, research | 9 | $2.48 | 33% |
| The Kassenbuch: build and check (#355 to #359) | 5 | $1.77 | 24% |
| A review's change: "stays passive" | 6 | $1.34 | 18% |
| The Kassenbuch: waiting, promises, waits (#361, #373, #374, #376) | 4 | $1.14 | 15% |
| Your messages and obligations (#351) | 1 | $0.37 | 5% |
| Holding a stopped line (#360, #362; fixed by 0.40.2) | 2 | $0.36 | 5% |
| **In all** | **27** | **$7.46** | |

What the numbers show:
1. **A review's change outweighs everything else** (`plan_keep._review_changes`, code). Each "change" verdict of the
   daily review becomes a step of its line, kind fix, due that day: worth 2.0 × (1 + 1) = 4.0.
   - The verdicts of 10-21 and 10-22 for lines #3, #4 and #6 all said to do nothing: "Kept passive at zero cost. The
     owner decides at renewal", "No more Etsy work".
   - Each still took a cycle to be closed: three a night ($0.52 on 10-21, $0.81 on 10-22).
   - Being urgent, each cut the sleep: #371 to #374 slept 30 minutes where Ember had chosen 6 hours.
   - All three lines wait for your keep-or-drop (#175, #176, #178): the review's change has nothing to change there.
2. **A line nobody sees keeps its full worth.** Lines #3, #4 and #6 had 5, 4 and 2 views in 23 days, yet each is
   worth 2.0. A line's chance counts its sales only from 30 views on (`EVIDENCE_VIEWS`): "unseen isn't unwanted".
   The KDP line, the only one with a buyer search, is worth 3.17. Its build step weighs 3.17 × 0.8 = 2.5: less than any
   review note.
3. **The Kassenbuch has no step on its own line.**
   - Line #14's last step for it (#236, "Build title #3: Kassenbuch") was closed in #359 when the book was ready.
   - Since then the Kassenbuch lives in:
     - promise #67's step #243 (under "Your owner", no line);
     - promise #68's step #273 on **line #4**;
     - step #272, also on line #4;
     - venture #49's step #251.
   - How they got to line #4: #372's cycle was on line #4 (a review note), and `plan_step add` puts a step on the
     cycle's line. Ember noticed and wrote it down as lesson #c372. #373 then promised it again in #207 with
     `project_id: 4`.
   - The weights then counted line #4's momentum for the KDP book (#373, #374).
4. **A promise to propose a book isn't blocked by KDP's week** (`plan_ready._calls`, code).
   - 0.42.0 makes a step wait while its tool would refuse it ('blocked'). A promise step's tools are only pins, posts
     and blog posts.
   - From 10-23, two days before 10-25, promises #67 and #68 are urgent: 3.0 × (1 + 2) = 9 each, then 12 on 10-24.
     Each takes a cycle a day that `propose_kdp_book` refuses.
5. **Ember may say two steps wait, a day** (`WAITS_A_DAY`).
   - #373 and #374 used both on 10-22 before 06:20.
   - At 12:48 #376 couldn't make promise #67's step wait. It read the refusal ("2 steps were said to wait today
     already: do what you can of this one") as "Ember's code already has it tracked as waiting".
   - At 20:00 the day before, #368 asked to wait "promise #67" without naming its step. The code waited the cycle's
     own step instead: #251, venture #49's research. The result named only the number ("Step #251 waits on
     2026-10-23"), and the mix-up went unnoticed.

### 3.2 The ventures (live, code)

**What they cost:** 9 of the 27 cycles, $2.48 (calc).
- Three brainstorms (#363, #365, #369) made 18 ideas.
- Ember parked 17 of them, mostly from their pitch alone: "needs a new account", "duplicates a live book", "no new
  triage until a book sells".
- One (#49) went to research. It is the Kassenbuch itself.

**Why they came** (code):
- A venture's step weighs 1.6 to 2.4 (worth 2.0 to 2.4 × create 0.8). That is below every product step, but above
  nothing.
- On the afternoon of 10-21 every product step waited: the Kassenbuch was done, promise #67 'tried', the stopped
  lines held. So the ventures had every cycle.
- Since 0.40.2 a venture's step no longer cuts the sleep: #368, #369, #370 and #377 each kept Ember's 6 hours.
- **But you woke Ember after three upgrades** (19:23, 22:42 and 12:50, each within seconds of the restart). With
  nothing else ready, each of those cycles went to a venture: the brainstorm #369, and the triages #370 and #377.

**What Ember was told:**

| Who | What | When |
|---|---|---|
| You, #205 | "No brainstorm and no new ideas: I hold the Ventures until the Kassenbuch and the Fahrtenbuch are out" | 10-21 16:40 |
| Ember, #206 | "No brainstorm or new ideas until the Kassenbuch is out, understood" | 20:00 |
| Ember's strategy | "5. No new ventures, platforms or idea triage" | since 10-16 |
| The weekly look | "Stop: Triaging or researching new venture ideas while live listings get under 5 views" | 10-16 |
| The review of 10-22 | "Park the brainstorming: 10 ideas parked this week. No new ideas until a book sells" | 04:43 |

None of it reached the plan:
- "I hold the Ventures" was meant as your action on the Plan tab. The Ventures node has no hold.
- Ember tried to make the brainstorm wait on your word in #368: `plan_step wait, on owner, ref 205`. It was refused
  (a wait on "owner" needs a request of Ember's, and the day's two waits were used).
- At 21:23 the brainstorm was the only step ready. Ember reasoned: "The honest compromise is one cheap brainstorm".

**Why nothing could pass** (code, calc):
- A venture's case is knocked out "slow" when its first sale comes later than half the runway (`knockouts.py`). The
  first sale counts at least 14 days (`econ.MIN_FIRST_SALE_DAYS`).
- With 23.9 days of runway, half is 12. **Every new venture fails.** #49's case (€42 a month expected) failed on
  exactly that, as Ember wrote: "Honest knock-out, not faked".
- So, below 28 days of runway, the desk's brainstorms, triages and research spend money on ventures that its own
  knock-outs will stop.

**The ventures and the lines disagree about the same book:**
- Venture #49 ("Kassenbuch 2027 for Kleinunternehmer") is the Kassenbuch line #14 has built. Its research (#366,
  #367) concluded "drop 2027, make it undated". Line #14's book, which you approved in #205, is "Kassenbuch 2027".
- #371 turned the research into a note: "Decision: go undated … build the interior when the KDP week resets".
- #372 read the note against the finished book and overwrote it: "SUPERSEDED … Do NOT rebuild".
- #49 is still "researching". Its step (#251) waits until 10-23. Then it weighs 5.2 and takes a venture cycle to
  research a book that is already made.

**The ventures' own weights** (the VENTURES section's "weight 57", "64", "71"):
- They come from six scores Ember gives a pitch: revenue, doability, difficulty, risk, speed, cost.
- 33 of the 40 the report lists (#18 to #57) are marked "(guess)": scored from the pitch alone.
- They order the ideas for triage, and an idea's step worth: 2.0 × (0.5 + weight / 100), 2.1 to 2.5.
- They don't decide much: every idea weighs about the same, and all wait behind any product step.

### 3.3 Ember's next steps: the handoffs (live)

Each cycle hands the next one a "next", and the plan's steps carry what is owed. Your #205 needed both: the fixes
before Sunday, the proposal on Sunday.

| Cycle | Its "next" for the Kassenbuch |
|---|---|
| #368 (20:00) | "Next ordinary cycle on project #14 will: (1) apply the 3 exact text/table fixes … (2) make_document to … v5 … (3) run the check on pages 2, 5, 67" (message #206) |
| #369 (21:23) | "Handoff: next ordinary cycle does the Kassenbuch v5 fixes, make_document, the page 2/5/67 check and 'Placeholders: none'" |
| #370 (22:42) | "Next ordinary cycle: Kassenbuch interior v5 fixes, then prep the KDP listing proposal for promise #67" |
| #371 (04:43) | "When KDP week resets: **build Kassenbuch undated interior** … then propose_kdp_book" (from #49's research) |
| #372 (05:14) | "Do NOT rebuild as 'undated' — that would discard a working, QA-passed book"; **lesson: "trust the rendered evidence"** |
| #373 (05:46) | handoff file: "Call propose_kdp_book with spec = books/kassenbuch-2027-kdp-spec.json, **as-is. No rebuild**" |
| #374 to #377 | "On 10-25 after 02:00, propose_kdp_book for Kassenbuch 2027 using the QA-passed spec … don't rebuild" |

**Where #205 got lost:**
1. **No step carried it.**
   - The next ordinary cycle took the review's notes, which weighed 4.0. Line #14 had no ready step to bring a cycle
     to it.
   - "Next ordinary cycle on project #14" depended on a cycle choosing line #14. The plan never chose it.
2. **#206 had no `commits`.** Your standing instruction #7 (10-07): "Every promise to me goes in commits with a due
   date". With one, a promise "Apply #205's three fixes", due 10-24, would have been a step worth 3.0 with
   your words in its title, urgent from 10-22.
3. **Your words left the plan after one cycle** (`context.owner_text`, code). "FROM YOUR OWNER" holds "what the owner
   wrote or decided since the last plan". From #369 on, no plan showed #205. What Ember knew of it was its own
   handoffs.
4. **A venture's finding replaced it.**
   - #371 wrote the "undated" note and named it the next step: a different job.
   - #372 corrected the note, but against the wrong thing. It defended v4 ("QA-passed") against the rebuild, and so
     against your fixes too.
   - Its new lessons, "trust the rendered/QA'd files over any stray note suggesting changes", now read as a reason to
     ignore any change to v4.
5. **The review saw none of it** (`review.scorecard`, code). The scorecard carries your decisions on requests, not
   your messages. On 10-22 it planned the Fahrtenbuch you had put off and pins "linking to Amazon" that have no link.
   - Ember followed you, not the review, on the Fahrtenbuch. But the review's "change" steps were the plan's
     heaviest.

**What Ember got right:**
- It asked you for the links instead of making pins it couldn't link (#207).
- It left the paid test's listings alone.
- It parked ideas without research money, and kept its own sleep long.

Its "nothing actionable until 10-23" was wrong only because #205 was gone: the fixes were actionable all along.

**What happens next, by the code** (calc, from the waits and weights):

| When | What the plan will do |
|---|---|
| 10-22 18:50 | No step ready: a cycle plans and ends (about $0.20), sleeps 6 hours |
| 10-23 00:50 | The daily review. If it says "change" again for lines #3, #4, #6: three steps of 4.0, each a cycle, the sleep cut to 30 minutes |
| 10-23 | Promises #67 and #68 urgent (9 each): two cycles, both refused by KDP's week. Venture #49's research (5.2): a venture cycle on a finished book. The paid test ends: the coach bundle's fix package (#254) is ready to propose |
| 10-24 | The same promises at 12. Milestone #18 (the coach bundle) misses: venture #12 parked on 10-31 |
| 10-25 02:00 | KDP's week. The promises take the cycle: `propose_kdp_book` checks v4 and **refuses it for "[Verlag/Herausgeber]"**. The refusal names the placeholder. The narrow "Text" column and the name fields are in no refusal, only in your #205 |
| 10-27 | Milestone #21 (KDP): "a small launch whose result shows whether it can earn", Ember's to judge. Unmet by 11-03: Ember's code parks venture #17, the KDP line |
| 10-28 | #16 (print on demand) and #29 (Pinterest): ventures #4 and #2 parked on 10-28 and 11-04 |

From midnight to Sunday that is about 10 cycles (about $2) on steps that can't move, and none on your fixes, unless
something changes.

---

## 4. What to do

### 4.1 You, today

1. **Add the Amazon links** to #140 and #141 (the request's "Add its Amazon link"). Ember asked in #207. Until then no
   pin or post can name the books, and the KDP section says so every plan.
2. **Hold the Ventures** (Plan tab, the Ventures project, hold: "until the Kassenbuch is out and a book sells"). No
   brainstorm, triage or research until you resume them, also when you wake Ember after an upgrade.
3. **Send Ember this message** (Inbox):

   > My message #205 still stands, and none of it is done: books/kassenbuch-2027-kdp-spec.json still names v4, which
   > prints "Gestaltet von [Verlag/Herausgeber] … GoBD-konform" on page 67 and has a 12 mm "Text" column.
   > books/kassenbuch-handoff.md and your lessons #c372/#c373 say "propose as-is, no rebuild": that was about the
   > undated idea, which I don't want. The title stays "Kassenbuch 2027". Do this in your next cycle, on line #14:
   > the three edits of #205 in books/kassenbuch-2027-interior-v1.md, make_document to
   > books/kassenbuch-2027-interior-v5.pdf, set the spec's manuscript to it, check it with pages "2, 5, 67" and
   > look at those three pages ("Placeholders: none"). Then rewrite the handoff: on 10-25 from 02:00, propose v5.
   > Commit to it in your answer, due 10-24, as my standing instruction #7 asks: #206 had no commits, and so nothing
   > in your plan kept it.
   >
   > Promises #67 and #68 are one promise: keep it once on Sunday and close both. Park venture #49: the Kassenbuch is
   > line #14's third book, not a venture to research. No brainstorm, no new ideas and no Fahrtenbuch until I say
   > so, whatever a review says. I have added the Amazon links of #140 and #141. Milestone #21 asks for a small
   > launch: two books are live. I'll send KDP's numbers by 10-27.

   - Ember reads it at its next wake (18:50; your option "wake when you write" is off). **Wake now** brings it sooner.
   - The fixes are one make_document and a check: one cycle, about $0.40 (#355 to #359 cost $0.25 to $0.46 each).
   - The edits are exact: I applied them to the rebuilt source with 0.40.2's code. The book keeps 67 pages, the
     check finds nothing, "Text" is 31 mm.

### 4.2 You, this week

1. **Lines #3, #4 and #6 after the paid test (10-23):** keep or drop each (#175, #176, #178).
   - While they wait for you, the daily review makes a "change" step for each, about $0.17 a cycle.
   - Drop #5, #7, #8 and #10 (#177, #179, #215, #189) as 0.40.1's analysis said: their reviews say stop, and Ember
     holds them already.
2. **KDP's numbers by 10-27** (milestone #21): orders or royalties of #140 and #141 from KDP's reports ("0" is an
   answer). Two books launched is the "small launch" the milestone asks for. Tell Ember it is met, or Ember's code
   parks venture #17, the KDP line, on 11-03.
3. **The Kassenbuch's request on 10-25:** look at pages 2, 5 and 67 on the card.
4. **Your longest sleep** (6 hours) shortens what Ember asks: 8 to 36 hours on these days. Each wake with nothing
   ready costs about $0.20, 3 to 4 a day until Sunday. Your standing instruction #6 says idle time is a cost too: raise
   it to 12 hours only if you'd rather save about $0.40 a day.

### 4.3 Code changes for the next release, by urgency

1. **Your words stay in the plan until they are done** (`agent/context.py`, `owner_text`; the tool `message_owner`).
   - "FROM YOUR OWNER" shows your latest message for 3 days after Ember read it (or until a newer one), headed "your
     words, still to carry out".
   - `message_owner`'s description and the work prompt say, as your instruction #7 does: an answer saying what Ember
     will do is a promise, `commits` with a due date.
   - Live, #205 left every plan after #368, and #206 promised the fixes without `commits`.
2. **The daily review reads your messages and the books' links** (`agent/review.py`, `scorecard`).
   - It gets your messages of its window (quoted, cut like the planner's) and the KDP books with or without their link.
   - Live, the review of 10-22 planned the Fahrtenbuch you had put off and Amazon-linked pins without links. Its
     focus is the first thing every plan of the day reads.
3. **No review step on a line waiting for your keep-or-drop** (`agent/plan_keep.py`, `_review_changes`).
   - A "change" for a line with an open "You decide" step stays in the review's text. The review's prompt says a
     line left as it is is "continue", not "change".
   - Live, 6 cycles ($1.34) closed "stays passive" notes on lines #3, #4 and #6, all waiting for you, and they cut 4
     sleeps.
   - Test: a review that says change for a line with an open decision makes no step and cuts no sleep. Once you have
     kept the line, the next change is a step again.
4. **One promise in one place, waiting on KDP's week** (`agent/obligations.py`, `agent/plan_ready.py`).
   - A promise whose words match an open one is refused: "you promised this as #67, due 10-25".
   - A promise to propose a KDP book is a step of the KDP line: `_calls` maps it to `propose_kdp_book`. It waits
     ('blocked') while KDP's week is full, instead of taking a refused cycle a day.
   - Live, #67 and #68 are one job on two lines. From 10-23 they take about 4 refused cycles.
5. **The ventures rest while none can pass** (`agent/plan_ventures.py`, `agent/desk.py`).
   - While half the runway is shorter than the shortest first sale (`econ.MIN_FIRST_SALE_DAYS`, 14 days), no
     brainstorm, triage or research step is laid out.
   - The Ventures line of the plan says why: "the ventures rest: a new venture's first sale (14 days at least) would
     come after half your runway (12 days)".
   - A venture the owner backed, or one with an urgent decision, keeps its step.
   - Live, 9 venture cycles ($2.48) on 10-21 and 10-22. Every case Ember wrote failed "slow".
6. **plan_step says which step it changed, and when a wait can come** (`agent/plan_changes.py`).
   - The result names the step's title: "Step #251 'Research its next question…' waits on 10-23".
   - The day's limit says "2 a day: it can wait from tomorrow".
   - Live, #368's wait for promise #67 waited venture #49's step instead, and #376 read the limit as "already
     waiting".

### 4.4 What not to change

- **The weights' formula and its numbers.**
  - The two days' waste came from what became a step (a review's note, a second promise, a venture no knock-out lets
    through), not from how a step is weighed.
  - With 4.3.3 to 4.3.5, the only ready steps of these days would have been the Kassenbuch's.
  - A dead line's worth (2.0 at 22 views) is also your keep-or-drop to settle, not a formula's. Lowering it in code
    would decide for you.
- **The two waits a day.** They keep Ember from putting off everything. With 4.3.6 the refusal says what to do.
- **0.40.2's sleep rule.** Venture steps kept Ember's 6 hours four times. The cuts that remain come from the
  review's steps (4.3.3).

---

## 5. What to expect

- **Tonight (18:50, 00:50):**
  - Without changes: a cycle that plans nothing.
  - After midnight, the review's change steps and the two urgent promises, each sleep cut to 30 minutes: about $1.20
    by morning.
  - With the Ventures held and your message read at 18:50: the fixes in that cycle, the handoff corrected.
- **10-23:** the paid test ends; Ember judges it and proposes the coach bundle's fixes (books/coach-fix-ready.md).
- **10-25 02:00:** the Kassenbuch is proposed: v5 if the fixes were made, otherwise v4, which is refused at its
  check for the placeholder.
- **10-27:** milestone #21 needs your KDP numbers.
- **By 11-15:** the goal of $100 a month is out of reach (24 days, $0 so far). The KDP books' season is November to
  January: a first royalty is the realistic next milestone.

---

## 6. 0.40.2 against the live report

| Change | What happened |
|---|---|
| 4.3.1: a venture's step cuts the sleep only when urgent; a brainstorm rests while none of its ideas went on | Works: #368, #369, #370 and #377 kept Ember's 6 hours. The rest didn't hold #369's brainstorm: #49 had gone on to research in #366 |
| 4.3.2: a review's stop holds its line | Nothing new to hold: the stopped lines #5, #7, #8 and #10 were held already, by Ember |
| 4.3.3: KDP's week from Sunday 00:00 UTC | Works: the KDP section says "the next week begins Sunday 10-25 at 02:00", and every plan since #373 names Sunday 02:00 |
| 4.3.4: placeholders | Not run yet: no check since v4 (10-21). It runs on 10-25 and will name "[Verlag/Herausgeber]" |
| 4.3.5: column widths | Not used: your fixes weren't made (3.3) |
| 4.3.6: a promise of no line can wait | Not used: #368's wait landed on another step, #376's was over the day's limit (3.1) |
| 4.3.7: line #12 is no KDP book | Works: on 10-21 at 20:00, "line #12 holds an Etsy listing: it is no KDP book" |
| 4.3.8: a book's Amazon link | The button is there; the links aren't. Ember asked for them in #207 |
| 4.3.9: the books' sources in the report | Works: the report's workspace texts begin with the Kassenbuch's source and the English Haushaltsbuch's |

---

## 7. Ember's last messages to you, answered

| Ember wrote | What the report and the code say |
|---|---|
| #206: "Next ordinary cycle on project #14 will: (1) apply the 3 exact text/table fixes …" | **Not done**: no step and no promise carried it, and the handoff now says "as-is" (3.3). 4.1's message brings it back |
| #206: "No brainstorm or new ideas until the Kassenbuch is out, understood" | **Broken once**: #369 brainstormed 6 ideas the same evening, in the cycle you woke after 0.40.3 (3.2) |
| #207: "Kassenbuch 2027 is spec-ready and QA-passed" | **Not quite**: the spec names v4, with the placeholder and the 12 mm column |
| #207: "Could you add the Amazon links for KDP books #140 and #141" | **Yes**: "Add its Amazon link" on each request (4.1) |
| #207: "I'll hold off on more English titles until it gets at least one sale" | **Right**, and the same holds for the Fahrtenbuch |
