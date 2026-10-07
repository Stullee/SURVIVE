# Ember 0.30.0: why she felt lost, moved at random and wrote no rulebook

*A sweep of Ember's logic and tactics, 2026-10-07, against 0.29.0 (`ec2d0c6`, the head of `claude/elegant-faraday-tcl6tq`). The fixes are released as 0.30.0.*

**The question.** In moments Ember feels lost, as if she has no clear plan of what to do and where to go; her moves and cycles feel random and uncoordinated; she hasn't created a rulebook (her *playbook*) yet, and she doesn't improve and learn as much as she should.

**Evidence.**
- I read the wake cycle end to end (`agent/loop.py`, `agent/service.py`, `agent/scheduler.py`), everything the planner sees and is told (`agent/context.py`, `agent/prompts.py`, `agent/constitution.md`, `agent/knowledge.md`), how a cycle's line is chosen (`agent/lines.py`, `agent/slack.py`, `agent/desk.py`), the learning loop (`agent/bets.py`, `agent/review.py`, `agent/learning.py`, `agent/weekly.py`, `agent/memory.py`) and the roadmap, obligations and listing tests (`agent/roadmap.py`, `agent/obligations.py`, `agent/gates.py`, `agent/stages.py`).
- I ran Ember in dry run (the fake model, simulated money) with four product lines and recorded which line each cycle took, before and after the fixes.
- I measured the weekly look's request against its token budget.
- I did **not** have your live database or a diagnostics report, so what I say about your live Ember is inferred from the code; each finding is labelled **reproduced** (a run showed it), **code** (found by reading) or **inferred**. The diagnostics report (System → Diagnostics) lists the weekly looks, cases and principles if you want to check the inferred ones.

**Paths** are relative to `ember/app/` unless they start with `tests/` or the repository root. Line numbers are for `ec2d0c6`.

---

## 1. The short answer

Ember's code already had good parts for a plan (a roadmap with a goal, a daily review that judges every line, a weekly look that rewrites the strategy) and for learning (bets, retrospectives, cases, a playbook). They didn't connect:

1. **The one thing that decides which line a cycle works on ignored all of it.** READY ranked lines by what they owed, a milestone due within 7 days, and then *the line worked on longest ago*. So every cycle took another line: a strict rotation. Her review's verdicts, her weekly look's "start/stop", her strategy and the goal's pace were text in the plan; no ranking read them.
2. **Her handoff was for the wrong cycle.** Since 0.28.0 work is per line, but the journal's "next" is per cycle: the next cycle (usually another line, a venture or a marketing cycle) received a next step it couldn't do.
3. **The rulebook had one writer, and that writer could be switched off by the business growing.** Only the weekly look wrote principles, once a week, from cases. Its view was cut at 14,000 characters but had to fit 12,000 tokens, and any view over about 12,300 characters didn't fit: the look was then skipped at every cycle, with one line in the app's log and nothing on the dashboard. A principle it drew from "too early" cases was dropped without a word.

0.30.0 connects them. In the same dry run, line switches went from 11 in 13 cycles to sprints of three cycles on the week's focus line with single-cycle detours to the others.

---

## 2. Findings and what 0.30.0 does

### 2.1 READY rotated lines every cycle — reproduced

`agent/lines.py:356-357`: `rank = (not owes, due is None, waits, last)`. After what a line owes and a milestone due, the line worked on longest ago comes first, and working on a line puts it last for the next cycle. The planner rule said the same in words (`agent/prompts.py:112`: "Keep 2-3 lines going across your cycles"), and the docs promised it ("the next one takes another line"). The fake model takes READY's first line; a real model mostly does too.

Dry run, four lines, `ROOMY` settings, before:

```
13 ordinary cycles, 11 line switches: [1, 4, 3, 2, 4, 3, 4, 4, 1, 2, 3, 4, 1]
```

A product needs several cycles in a row (research, files, photos, listing, fixes). With a rotation, each line got a cycle every three or four, its context had to be rebuilt each time, and nothing was finished before the next thing started. This is the "random, not coordinated" feeling.

**0.30.0** (`agent/lines.py`, `_Facts.rank`): after what a line owes, **the line in progress** (the line the newest ordinary cycles worked on) comes first while it has work, at most 3 cycles in a row (`prompts.LINE_STREAK`); a marketing, venture, event or idle cycle in between doesn't break the run. After a full run the line *rests* for one cycle so another line with work gets its turn. A line that only waits for you, or that today's review said to stop, goes after the others. The planner rule now says "Finish what you start". After:

```
21 ordinary cycles, 10 line switches: [1, 4, 4, 4, 3, 3, 2, 2, 1, 1, 1, 4, 1, 1, 1, 3, 1, 1, 1, 2, 1]
```

(Before the weekly look: sprints of two or three per line. After it chose line #1 as the week's focus: three cycles on #1, one detour, three on #1.)

### 2.2 Her own judgement never reached the ranking — code

The daily review judges every line (continue, change or stop, with its bottleneck) and names a focus; the weekly look names the bottleneck, what to start and stop, and three questions. All of it went into TODAY'S REVIEW as text (`agent/review.py:774`, `agent/weekly.py`), far from READY, and READY ranked without it. So the plan saw "change #4's cover" in one section and "take line #2" (the oldest) at the top of another.

**0.30.0:**
- The weekly look picks **the week's focus lines**: up to 3 open lines that bring the goal nearest soonest (`prompts.WEEKLY_SCHEMA` `focus`, `weekly.focus`). READY ranks them right after what is owed and the line in progress, and so does a marketing cycle's READY (after a push that is owed). While a focus line has work, a detour to another line stays one cycle.
- Today's review's verdicts rank too (`lines.verdicts`): a **change** is a task of the line's own (one for reach belongs to the marketing cycles while they run); a **stop** sends the line last, with "close it".
- The rest of the order: a milestone due within 7 days, another task (the critic's fixes, a missing demand note), selling, then the one worked on longest ago.

### 2.3 The weekly look planned without the goal — code

0.29.0 put your goal at the root of the roadmap and showed it in every plan, but not in the weekly look's view (`agent/weekly.py` `view`): the step that rewrites the strategy never saw what it serves. **0.30.0:** the view shows the goal and each sub-goal with how far it got and its pace (`weekly.goal_text`), and the look chooses the week's focus toward it.

### 2.4 A bar of a listing test counted as a line's work — code

`agent/lines.py:402` counted every open milestone of a line as "due", including the bars of its listing test (10 views by day 7, 30 by day 14...), which Ember's code checks itself from Etsy's numbers (`agent/gates.py`). Every live line has one bar at a time, so most live lines were always "due": they jumped READY, counted as jobs (which cuts the sleep to 3 hours, `agent/slack.py`) and the cycle was aimed at a views bar in an ordinary cycle that, while marketing cycles run, has none of the tools that bring views. **0.30.0:** a bar (`created_by = 'code'`, `kind = 'first_test'`, with a line) is no milestone due for READY (`lines._bar`); a missed bar's work already comes as an obligation, which ranks first. A backed venture's first test (no line) still counts.

### 2.5 The handoff went to the wrong cycle — code

`write_journal`'s `next` is stored per cycle, and the plan's YOUR LAST CYCLE shows the last cycle's (`agent/context.py` `last_cycle_text`). Since 0.28.0 the tools refuse another line's work, so a handoff like "next: make the poster's cover" reached a cycle on another line, a venture cycle or a marketing cycle. **0.30.0:** each line keeps the next step its last cycle left (`lines.handoffs`): READY quotes it for the line in progress, the work steps' FOCUS shows it whole, and YOUR LAST CYCLE says where its handoff was written ("written in a venture cycle", "in a cycle on line #4").

### 2.6 The weekly look was skipped once the business grew — reproduced

`agent/weekly.py:32` cut the view at 14,000 characters; `agent/loop.py:138` required the request to fit 12,000 tokens (`context.fits` counts half a token per byte, plus framing). Measured with the real prompts:

| view | tokens | fits 12,000 |
|---|---|---|
| 8,000 characters | 9,793 | yes |
| 12,000 | 11,813 | yes |
| 14,000 (the cut) | 12,823 | **no** |

When it didn't fit, `agent/loop.py:1406` logged "The weekly look doesn't fit its budget; skipped" and returned, recording nothing, so it was due again at the next cycle and skipped again. The view lists every open project (there is no limit since 0.19.1), the venture tree and up to 30 earlier cases, so a business of Ember's size passes 12,300 characters easily. From then on no weekly look: no new strategy, no questions, and no principles, because the weekly look was the playbook's only writer. *Inferred for your live Ember:* if your add-on log shows that line, this is why the rulebook stayed empty.

**0.30.0:** the look gets a review call's room (`loop.WEEKLY_INPUT_TOKENS = REVIEW_CALL.input_tokens`, 16,000), its view holds your standing instructions and the strategy first and the older cases last, it is cut until the request fits (`weekly.VIEW_STEPS`), and a look that still can't come through is kept as failed, shown in the System log and tried the next day. (`tests/test_fixes_0300.py` measures a full view and a worst case of two-byte characters.)

### 2.7 The rulebook had one writer, once a week — code, inferred

`agent/learning.py` (0.18.0): principles come only from the weekly look, from cases; cases come only from what *settled* (a bet, a metric milestone, a closed project, a parked venture, a rejected request), retrospected by the daily review. The first weekly look after 0.18.0 came right after the first daily review, when the case table had just started (*inferred*: few or no cases), and the next was a week later (if it fitted, 2.6). A principle citing only too_early cases was dropped silently (`agent/learning.py:256`), so neither the look nor you could see why the playbook stayed empty. Meanwhile each retrospective already wrote a `lesson` ("what it teaches beyond this case"): a ready hypothesis that no code turned into a principle.

**0.30.0:**
- Each retrospective's lesson joins the playbook the day its case is kept (`learning.adopt`, before every plan): a **hypothesis** citing its case, or one more case for an active principle that says nearly the same (most of their words and word pairs in common, and the same denials, so "pins bring views" never counts for "pins bring no views"). Three agreeing cases make it **established**, as before. Not adopted: too_early cases (no evidence), low-confidence ones, cases without a lesson, and cases the weekly look already cited.
- The cases kept before 0.30.0 join it at the first cycle after the update (the same function: it weighs every case it hasn't weighed yet).
- The weekly look curates: it is told hypotheses arrive daily, and to confirm, merge, dispute and retire them; what its answer can't keep is now said, with why ("its cases are too_early, which is no evidence yet").
- **You can read it**: Mind → Playbook shows each principle with its confidence and its cases for and against, the ones retired lately with why, how many cases there are (and how many were too early to count), and this week's look with its focus lines. Before, the playbook and the weekly look were nowhere on the dashboard.

### 2.8 Few bets, so little to learn from — code

Bets are the main source of cases with real evidence, but they are optional (`project_update`'s `bet`), only for lines with live listings, and nothing asked for one outside marketing cycles. **0.30.0:** on a live line without an open bet, the work steps' FOCUS asks for one ("say what you expect this cycle's change to bring"), in ordinary cycles too.

---

## 3. Not changed in 0.30.0: what I recommend next

Ordered by how much I expect each to help. None of them is a defect like 2.6; they are design choices worth revisiting once 0.30.0 has run for a week.

1. **The plan is long, and her own direction comes late.** The planner gets 26 budgeted sections (about 35 KB) every cycle (`agent/context.py` `PLANNER_BUDGETS`); her STRATEGY and LESSONS (with the playbook) come near the end, after the roadmap, projects, READY, ventures and every channel (`agent/context.py:963-965`). The 0.13.0 analysis warned that more sections crowd out the steering. Consider moving STRATEGY and the playbook's established principles up, right after OBLIGATIONS, and measuring plan quality before and after. I didn't change the order: it moves every plan's attention, and should be tested live.
2. **Cycle kinds alternate by money, not by sequence.** A wake cycle is a venture or marketing cycle while its share of the day's spending is behind (`agent/lines.py` `kind`). Marketing doesn't follow the ordinary work on the same line: a listing can be pinned before its fixes are done. Consider letting a marketing cycle prefer the line the last ordinary cycle changed at Etsy (an edit carried out), so "fix, then bring buyers" happens in that order.
3. **"Too early" swallows most evidence while views are tiny.** With 0 to 2 views per listing, most retrospectives are too_early and teach nothing (by design: small numbers aren't lessons). Cases that don't depend on traffic would teach sooner: your rejections and their comments, the quality critic's verdicts, a request carried out. Consider making the critic's verdicts and your decisions cases of their own.
4. **Expectations outside listings.** The constitution says "Before you act, say what you expect", but only a listing's views, favorites and orders can be bet on. Consider bets on a request ("your owner approves #N by Friday") or a milestone, settled by Ember's code the same way.
5. **The daily review's focus is free text.** READY reads the review's verdicts now, not its "focus today". A `focus_project_id` field would let READY rank it too.
6. **A weekly look between weeks.** With hypotheses arriving daily, a look every 7 days may leave many unmerged. Consider a short mid-week curation when 10 or more new principles are waiting.

---

## 4. What changed in the code

| Area | Files |
|---|---|
| READY: the line in progress, rests, focus lines, review verdicts, no bars as due, per-line handoffs, the bet prompt | `agent/lines.py`, `agent/prompts.py` (`LINE_STREAK`, planner and marketing rules) |
| The weekly look: the goal in its view, the week's focus, a view that fits its budget | `agent/weekly.py`, `agent/loop.py`, `agent/prompts.py` (`WEEKLY_RULES`, `WEEKLY_SCHEMA`), `agent/fake_llm.py` |
| The playbook: daily hypotheses, no silent drops | `agent/learning.py`, `agent/loop.py` |
| YOUR LAST CYCLE says where its handoff was written | `agent/context.py` |
| Mind → Playbook | `agent/views.py`, `web/index.html`, `web/static/js/app.js`, `web/static/css/app.css` |
| Tests | `tests/test_fixes_0300.py` (new); updated where they pinned the old rotation, bars as jobs or wording: `tests/test_fixes_0280.py`, `tests/test_learning_loop.py`, `tests/test_autonomy.py`, `tests/test_web.py` |
| Docs and notes | `ember/CHANGELOG.md` (for Ember), `ember/DOCS.md`, `README.md`, `ember/config.yaml` |

No database migration: the week's focus is part of the weekly look's stored answer, and the playbook already had its table. Nothing changes in what leaves the container, in the money guard or in your controls.
