# Vision: Ember learns from her own work

*Draft, 2026-10-02, written against 0.17.0 and the diagnostics of that day. Phase 1 is built in 0.18.0; phases 2
and 3 are not built yet.*

## The idea in one paragraph

The model behind Ember never changes, so Ember can only get smarter through what her code shows her when she plans.
Today that is a 4 KB lessons file of which she sees the newest 1.3 KB, a strategy she rarely touches and a daily
review whose conclusions are advice. Learning has to become a set of routines Ember's code runs around her, like a
manager's calendar: the code notices that something happened, makes her ask why, stores the answer, and shows it to
her again the moment it matters. She never has to remember to reflect. And she should fight for a first euro instead
of shrinking: money is for bets, idle time is a cost, and work already done is paid for.

## What the diagnostics of 2026-10-02 showed

- **Her memory is full of tool limits.** 12 of her 17 lessons were limits she ran into ("the reason field caps at 300
  characters"). Each one pushed a business lesson out; the lesson "your owner wants finished products only, one
  message a day, yes/no decisions" was lost that way. The planner saw the newest 6 lessons, all of them tool limits.
- **Her strategy was stale.** It still named dropshipping as the priority after she had parked it (cycle 69).
- **Her daily review's conclusions were neither kept nor followed.** Its lesson was never saved; its verdicts ("spend
  no more on design", "freeze at $0") were advice that the next cycles didn't follow.
- **She saved money nobody asked her to save.** The constitution said "think like a frugal founder… consider sleeping
  longer to save money", the review invented caps ("aim under $0.50 a day until the owner answers") and slept 12
  hours, and the burn modes would have moved her to focus (no new ideas of her own) around 10-08.
- **The listing tests would have killed products for lack of visitors.** Every listing had 0 to 2 views; the day-14
  bar parks a product line under 30 views, about 10-13, while nothing had been done to bring buyers to them.
- **She didn't drive herself.** 8 of her last 12 cycles were started by the owner, 3 by her own schedule.

## The learning loop

```
 1 health check ──> 2 a bet on the bottleneck ──> outcome (Ember's code reads the numbers)
       ^                                                   │
       │                                                   v
 5 recall <── 4 playbook (principles) <── 3 retrospective: what happened, and why
```

Plus two rhythms around it: **6** a weekly look at the whole business, and **7** a rule for waiting time.

### 1. The project health check (daily)

The owner's questions become a fixed check in the daily review, for every open project. Ember's code answers what
numbers can answer; Ember judges the rest.

| Question | Who answers | How |
|---|---|---|
| Is it performing? | code | The funnel: built → listed → seen (views) → liked (favorites) → bought (orders). Where it is stuck names the problem: not seen = reach, seen but not liked = appeal (photos, title, price), liked but not bought = price or trust. |
| Is it reaching customers? | code | The reach done for it: blog posts that link its listings, pins, listing edits. "0 views and no reach" means hoping buyers find it by accident. |
| Was there enough research? | code, then Ember | A demand note, independent evidence, a competitor comparison, what buyers complain about (the owner's standing instruction). |
| Is the quality good enough? | an independent critic (phase 3) | Scored against the top competitors and the owner's rule that it must beat a free AI chat. |
| Verdict | Ember | keep, improve (what, and how we will know), wait (too early) or stop, with its bottleneck and why. |

**Marketing before parking.** A product line that misses its day-14 views bar with less reach than
`reach.ENOUGH` is not parked: it owes a push to bring buyers, and the bar comes once more two weeks later. Only a
product line that was marketed and still wasn't seen is parked.

### 2. Bets: say what you expect, then check (phase 2)

Every change becomes a small bet with an expected result and a date: "new photos → 20 views by 10-12". Ember's code
checks it against the numbers (the metric catalogue and the prediction ledger exist; a daily views history per
listing is added, the `observations` table already allows `listing` rows). A bet must target the bottleneck the
health check found, and Ember's code checks its preconditions: no bet on views without reach on record, no bet on
sales before the quality check passed. A bet that comes out other than expected is the signal to learn: surprise.

### 3. Retrospectives: what worked, what didn't, and why (phase 2)

When something settles (a bet, a listing bar, a closed project or venture, a request the owner rejected), Ember's
code puts it on the next review's list. For each, Ember answers: what did I expect, what happened (the code gives the
numbers), why, how sure am I, what would I do differently, where else does it apply; and one cause:

| Cause | Means | Then |
|---|---|---|
| wrong idea | no demand | stop |
| weak execution | the product or listing | improve |
| not enough reach | nobody saw it | market it |
| too early | too little data | wait, with a date |
| outside | platform, API, an approval | work around it |

Each answer is kept as a case (a table, no size limit).

### 4. A playbook instead of the lessons file (phase 3)

Once a week, cases become principles: "Etsy listings without outside traffic get under 5 views in their first two
weeks (cases #3, #4, #5)". A principle keeps its evidence and the cases against it; Ember's code sets its confidence
(one case: a hypothesis; three that agree: established; contradicted: disputed, to be tested again), and principles
nobody confirms for weeks fade. Tool limits never go in: the tools state them themselves.

### 5. Recall at the right moment (phase 3)

Her cases and principles join the search the owner's library already has (`library.relevant`, `knowledge_search`).
The plan gets the established principles that touch her open projects instead of the newest lines; the work steps
get the cases closest to what they do; before a new project or venture, Ember's code shows the most similar past
attempt: "you tried something like this, and this happened".

### 6. The weekly look at the whole business (phase 2)

One call a week on the strongest model, from a view Ember's code builds: every project and venture by funnel stage
with its cost and earnings, the mix of business models (digital products, physical products, content and ads,
services), where time and money went against where results came from, and the playbook's changes. Its questions: are
we on the right path, only products or could a service earn sooner, what do we stop, what do we start, what did we
learn. It rewrites the strategy (Ember's code checks it names no parked venture) and sets 1 to 3 open questions for
the week.

### 7. Waiting time (phase 3)

When projects wait (on the owner, an API approval, views), an ordinary cycle gets a ranked list of useful work, like a
venture cycle's READY: reach owed, quality checks, research gaps, retrospectives, the weekly look if due, a new venture
of a business model she doesn't run yet. While that list isn't empty and the money allows, Ember's code keeps her
sleep short.

## Spending: fight instead of shrinking

The burn modes protected the owner's money by narrowing what Ember may do as the runway shrank: in focus no
brainstorms and no new ideas of her own. That is backwards: when what she does isn't working, she needs new ideas
more, not fewer. Phase 1 makes it the owner's choice, the option `spending_stance`:

- **invest** (the default): the owner's caps are the only limits; Ember stays in explore whatever her runway, until
  the last will (dormant). Under 15 days of net runway her STATUS says to go for the fastest path to a first euro and
  stop what has evidence against it, and the owner is warned once in the System log, so they decide (a grant, or
  another stance), not the code.
- **steady:** explore, and focus under 30 days of net runway; never maintenance.
- **conserve:** the burn modes as they were (focus under 30 days, maintenance under 15).

The guard against an agent that burns $7 a day on busywork is the loop itself: every dollar should back a bet on a
bottleneck, and the health check says which.

## The texts that steer her

- **Constitution, HOW TO THINK** (the priorities, hard rules and mindset stay as they are): "think like a frugal
  founder" and "consider sleeping longer to save money" become an investor's rules: money is for bets on a
  bottleneck, idle time is a cost, work done is paid for, a short runway means the fastest path to a first euro; and
  a learner's: say what you expect before you act, afterwards ask why (wrong idea, weak execution, not enough reach,
  too early). "Dying honestly is acceptable" stays, now "far better than surviving through deception,
  rule-breaking or shrinking to last longer".
- **Planner rules:** "your daily cap is a limit, not a target" becomes: spend on work that can earn or teach
  something measurable, up to the owner's caps; when work waits, bring it to buyers or start the next bet.
- **Review rules:** "stop what has cost money for days without a sign of demand" becomes the health check: tell no
  demand from no reach; never set caps or sleep below the owner's settings.
- **knowledge.md:** a product nobody sees hasn't been tested yet.
- **The owner's standing instruction** (theirs to paste, on the dashboard):

  > Leg 1 is my Etsy shop for German and English buyers: keep improving and adding to it. Every product must beat
  > what a free AI chat gives: know what sells and what buyers complain about.
  >
  > Money: my settings' caps are your only spending limits. Don't set yourself lower ones or sleep to save money; idle
  > time is a cost too. Invest in what gets products in front of buyers or earns sooner. Work already done is paid
  > for: market it once, then move on instead of waiting.
  >
  > Think beyond products: services, websites, matchmaking, marketing channels, physical products, anything honest
  > people pay for.
  >
  > My time: at most 10 minutes per Etsy request. Write to me at most once a day, short, with clear yes/no
  > decisions. No apologies or long explanations.
  >
  > Blog posts: write them in Markdown and propose them with propose_blog_post (guide 'blog'). Never hand me HTML
  > files.

## Guardrails

- **Small numbers.** 2 views say nothing about quality. Results below a metric's sample are "too early", never a
  lesson.
- **A lean plan.** The 0.13.0 analysis warned that more planner sections crowd out the steering. The playbook
  replaces LESSONS, the health check lives in TODAY'S REVIEW, the waiting list extends READY: no new sections.
- **Measure that it works.** Repeated mistakes, how often her bets come true (calibration), days a stuck product waits
  for a decision, the share of cycles she starts herself.

## Phases

**Phase 1 (0.18.0, built):** the spending stance; the rewritten texts; reach counted per project and the funnel
stage in the daily review, whose verdicts name a bottleneck; marketing before parking; the review's lesson kept by
Ember's code; tool limits out of the lessons (the consolidation may drop them); a strategy naming a parked or killed
venture is an obligation; an over-long journal is shortened instead of lost; LESSONS shows 2.6 KB instead of 1.3 KB.
Not yet in phase 1: the research-depth facts of the health check (demand note, evidence, competitor comparison, buyer
complaints) come with phase 2's weekly look, and the quality critic with phase 3.

The constitution's new HOW TO THINK is short on purpose: the work step's fixed prompt may not grow past 0.11.1's
45,866 bytes (`tests/test_rule_audit.py`), and it had 3 bytes to spare, so the rewrite took room from repeated prose
in the operating rules.

**Phase 2:** the weekly look at the whole business; bets with a views history; retrospectives and cases.

**Phase 3:** the playbook with confidence; recall; the independent product critic; the waiting-time list.

## The transition to 0.18.0

- **Database:** one migration (0072) rebuilds the listing tests' table so a bar can be the day-28 retry; every row
  stays. As after every migration, the app copies the database to `/data/backups` first, and a 0.17.0 refuses the
  migrated database: to go back, restore that copy.

- **Spending:** the new option `spending_stance` defaults to invest, so an installed Ember moves to explore at her
  next check (a mode below explore no longer waits for money to come in); the System log says so. An owner who wants
  the old behaviour picks conserve.
- **Listing tests running now:** the day-7 bars stay as they are. Every day-14 views bar missed from now on is judged
  by the new rule; a product line with too little reach gets one more bar instead of being parked. Obligations
  already written stay.
- **Lessons:** nothing is deleted at the upgrade. The next daily consolidation (after the next daily review) may drop
  lessons about a tool's limits; the System log names each one dropped.
- **Strategy:** a strategy naming a parked or killed venture becomes an obligation at her next plan; she rewrites it.
- **The texts:** the constitution, rules and knowledge apply from her next cycle; her release notes (CHANGELOG
  0.18.0) tell her what changed and why.
- **The owner:** pastes the standing instruction above, checks `spending_stance`, and pins the lessons that must
  never be lost (the owner's preferences among them).
