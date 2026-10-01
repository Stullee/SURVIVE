# Ember 0.13.0: what is going right, what is going wrong, and what to build

*Second analysis, 2026-09-30. The first was of 0.11.1 (2026-09-29).*

**Scope.** This report covers the whole codebase at 0.13.0 (`ab713b0`, the head of `claude/elegant-faraday-tcl6tq`, which Home Assistant installs from). It is measured against the same goal: a fully autonomous worker and entrepreneur that stays safe, honest and within budget. It checks the fix log and patch notes of the first analysis against the code, and reads your two diagnostics reports of 2026-09-30 (0.12.0 at 12:10 UTC, 0.13.0 at 15:10 UTC; cycles #33-#46).

**Evidence:**
- 10 subsystem reviews: economy, loop, tools and workshop, roadmap, ventures, autonomy, Etsy, the new channels, mail and privacy, web and security;
- 2 live-data reviews: operations and money, behaviour and decision quality;
- 2 strategy reviews: autonomy architecture, economics and business;
- independent re-checks of findings (see "Verification" below);
- my own checks of every high-severity claim, of CI on GitHub, and a short database and process pass;
- the full test suite, run on Python 3.12: **1,479 tests pass** (15 minutes); `ruff check` is clean.

**Not covered in depth.** The database/migration and test/process reviews were cut short when the work was paused, and so was the third strategy review (complexity). Their parts below come from my own checks and from what the stopped reviewers had measured; they are marked as such.

**Verification.** Every finding has a label for how it was found, and a mark for whether someone other than its finder confirmed it:
- **Labels:** **reproduced** (a script run against the real code showed it), **live** (it is in your diagnostics), **code** (found by reading), **inferred** (from indirect evidence).
- **✔ checked:** an independent verifier, a second reviewer who found the same thing, or I confirmed it. All five high-severity findings are ✔.
- **◐ reproduced by its finder only:** real reproductions, not yet re-checked by someone else. Treat their severity as provisional.
- Findings that were refuted are left out. None of the 14 findings that went through an independent verifier was refuted.

**Paths** are relative to `ember/app/` unless they start with `tests/`, `.github/` or the repository root. Line numbers are for `ab713b0`.

**Raw material.** Every review, verification and live analysis is in `analysis-0.13.0/raw/` as JSON, with each finding's evidence, reproduction and proposed fix in full. Your Home Assistant user ID was removed from them.

---

## 0. Urgent

### 0.1 The money guard's "worst case" is not a ceiling for workshop calls — high, live + reproduced ✔

A workshop run is one call to Anthropic's code-execution tool, and that tool loops on Anthropic's side. Ember quotes it as if it had a known number of turns (`economy/estimate.py:17-28, :42-44`: at most 10 samplings, context growth of at most 48,000 tokens, output of at most `max_tokens`). Live, all three assumptions broke:
- **Call #423 (cycle #44):** quoted $0.3482, billed **$1.8376** (5.3×): 540,865 cache-write and 2,008,335 cache-read tokens in one call. Its cycle cost **$2.0045 against a $1.00 cycle cap**, and the run broke the workshop's own per-run cap ($0.50 then, $1.50 now).
- **Call #369 (cycle #41):** wrote 12,439 output tokens with `max_tokens` 8,000.
- **Call #444 (cycle #46):** $0.3466 against $0.3441 (0.7% over).

What this breaks:
- the invariant from the first analysis ("requests with no provable cost ceiling are refused") now holds only for token-only calls: plan, work, reflect, review, study, critic, consolidate, brainstorm, draft and last will. It does not hold for the workshop, and for research (web search and fetch) it is only empirical;
- the per-run cap is checked against the quote before the call, so one call can't be bounded by it (`agent/workshop.py:205-210`);
- the daily cap and the balance are protected only up to the quote. At the default caps ($1.50 a day, $0.50 a run), a #423-sized run admitted with $0.40 of the day left takes the day to about $2.94 (reproduced).

What followed:
- the safety factor for workshop calls jumped to 4, which locked the workshop at your $0.50 run cap; you reset it 18 minutes later and raised the run cap to $1.50;
- the next run (#444) was quoted exactly as before and overran again.

And every overrun, even 0.7%, **stops the whole cycle**. Cycle #46 lost the rest of its plan (proposing the poster it had just paid for), its reflection and its handoff. The digest then told the next plan "Work ended: the plan was done", and the scheduler replaced the agent's chosen 420-minute sleep with a 30-minute back-off, which brings the next full-price cycle sooner (`economy/metering.py:1151-1164`, `agent/service.py:680-684`, `agent/digest.py`).

Token counting also fails for every workshop request that carries a file (`BadRequestError`, 3 logged, more hidden by the log's 5-minute de-duplication), so those quotes start from a rough bound.

**Fix (M):**
- **Reserve the workshop honestly.** Reserve a run at max(quote, the run cap, 1.5 × the p95 of recent runs) under the daily cap and the balance, so those two stay hard.
- **Price server-tool calls per sampling.** Price output as `MAX_SERVER_ITERATIONS × max_tokens`, and record `usage.iterations`.
- **Don't end a cycle over a small overrun.** For a purpose outside the cycle cap, or an overrun of at most about 10% and $0.02, record it, raise the factor, refuse further calls of that purpose in this cycle, and keep the cycle running.
- **Keep the reflection.** Never refuse the reflection because of an overrun: it has a provable ceiling of its own.
- **Stop needing the workshop for this.** Most of its live spend was on jobs Ember's own code could do (see 3(c) and FIX NOW 12).

### 0.2 Safe mode opens the dashboard to everyone and can lift your kill switch — high ×2, reproduced ✔

When any option is invalid, Ember starts in "safe mode" with the built-in defaults (`config.py:404-407`: `Settings(dry_run=True)`). The defaults include an **empty `owner_user_ids`**, which means "everyone is the owner" (`security.py`). So one bad option opens the panel, the private diagnostics and every control to any Home Assistant user, until you notice.

The same start runs `apply_kill_switch_reset` with the default `kill_switch_reset = 0` (`main.py:154`, `agent/owner.py:895-908`). If you once reset a kill (value 1), a safe-mode start after a new kill un-kills Ember. When you fix the options and restart, Ember runs live with every unlock still standing.

This is easy to hit:
- The first analysis told you to set `min_sleep_minutes` to 180-240 and `wake_interval_minutes` to 240. Change the first without the second (your live values are 30 and 60) and `config.py:292-293` refuses the pair, so Ember starts in safe mode.
- 13 options are bounded only in `config.py`, not in `config.yaml`, so Home Assistant accepts values that then force safe mode. Examples: an `http://` site address, a site name of 61 characters, an umlaut domain.

**Fix (S):** in `_safe_mode`, parse `owner_user_ids` and `kill_switch_reset` from the raw options with their own validators and keep them; if they can't be parsed, refuse everyone except a page naming safe mode and the caller's user ID. Don't store a kill-switch reset from invalid options. Move the bounds into `config.yaml` (`str(,60)?`, `match(^https://…)?`).

👤 **Until then:** change `min_sleep_minutes` and `wake_interval_minutes` together, and after any options change, check that the dashboard doesn't show the safe-mode banner.

### 0.3 The poster venture you backed can't publish its print file — high, reproduced ✔

`products/images.py:21` refuses pictures over **12 megapixels**, and `propose_printify_product` reads every picture through it (`agent/tools.py:3606-3610`). An A3 print area at Printify is 3510 × 4950 = 17.4 MP, and the larger sizes need 17-19 MP at 150 dpi. So the poster cycle #46 paid for can't be proposed. `look` and `workspace_read` refuse it too. The message ("isn't one of your pictures") hides the reason, so the agent is likely to pay for more workshop runs to "fix" it. The workshop's own file check allows 40 MP (`products/checks.py:41`), so the two limits disagree.

**Fix (S):** one limit for both (about 40 MP), `Image.draft`/`reduce` for thumbnails, and a refusal that says "W × H = N MP, more than M".

### 0.4 Money: 18 days of net runway, and burn went up — 👤 your settings first

- **Balance and burn:** $89.63, with 0 revenue. The 7-day-window burn is **$4.89 a day** (it was $3.76 at 0.11.1; the target was $1.90). $4.66 went out on 09-30 by 17:10.
- **What drove 09-30:**
  - the workshop, 57% ($2.64, including the $1.84 run);
  - work steps, 23%;
  - research, 9%;
  - Opus plans, 8%.
- **Cycles you started:** 55% of the last 14 cycles' spend. That was Wake now, and two decision wakes that fired 18 seconds apart.
- **Maintenance mode arrives in a few days.** Below 15 days of net runway, Ember's code drops to one $0.40 cycle a day with no venture cycles (`economy/burn.py`). At today's pace that is around **10-03**, just when the seasonal listings and the poster test need building.
- **The settings went the other way from the cost plan:**
  - the daily cap $5 → $7, the cycle cap $0.60 → $1.00, the workshop run cap $0.50 → $1.50;
  - the overshoot scaling was reset;
  - min sleep 30, wake interval 60 and the Opus planner stayed.

What to set is in section 7.

### 0.5 CI has been red on both releases — medium, ✔ (GitHub)

The CI runs on the released heads of 0.12.0 (`1eadb12`) and 0.13.0 (`ab713b0`) both **failed**. Tests, lint and both image builds pass. The **Secret scan** job fails on `tests/test_privacy.py:122` (added in `541155a`, 2026-09-29 19:08): a fake 6-6-6 value one letter off the allowlisted one in `.gitleaks.toml`. It is a false positive, not a leak, but CI has given no signal for about 19 hours and two releases.

**Fix (S):** add that exact fake to the allowlist (or use the allowlisted one), then make CI green a condition for release (section 8).

### 0.6 Don't unlock any autonomy yet — medium ×8 (5 ✔)

The policy engine shipped with no unlocks granted (`policy_grants` is 0 live), so none of this has fired. But the Roadmap card now invites you to unlock, and these hold as soon as you do:

| Problem | Where | ✔? |
|---|---|---|
| An automatic email's footer tells the recipient **you approved it before sending**, which is false for an unlock | `integrations/executor.py` (footer) | ✔ (two reviewers) |
| The "first contact" guard trusts the unauthenticated `From:` header: a forged or bulk sender counts as "someone who wrote", so an automatic reply can be a real first contact (UWG §7) | `agent/never.py`, `integrations/mailstore.py` | ✔ (two reviewers) |
| Unlocks approve requests that fail their own QA: a 329-word reply without "Re:", a new listing with 1 photo | `agent/policy.py` | ◐ |
| **Take back every unlock**, and Ember's own revocations, stop only held requests; requests an unlock already approved still run | `agent/policy.py`, `agent/owner.py` | ◐ |
| The **kill switch doesn't revoke unlocks**: on reset, requests held in a veto window are approved at once. The patch note "a kill switch that revokes every class" doesn't hold | `agent/owner.py:874-892` | ◐ |
| Unlocks can be granted while `owner_user_ids` is empty or in safe mode (first analysis §10: "don't ship the policy engine before owner identity is enforced") | `agent/owner.py`, `web.py` | ◐ |
| `qa_fix` and `price_change` judge Ember's stale record, not Etsy's: an automatic photo fix deletes photos you added at Etsy, and Undo can't bring them back | `agent/policy.py`, `integrations/etsy_publisher.py` | ◐ |
| The ±15% price band applies to each change on its own: 3 cuts a day take €4.50 to €2.86 | `agent/policy.py` | ◐ |

**Before any unlock:** fix the footer, QA, take-back and kill-switch rows (all S). Require `owner_user_ids` for any level other than manual. The mail authentication (M) must land before `email_reply` leaves manual. Section 3(e) says why the unlocks also don't fit the requests you actually get.

---

## 1. Verdict

0.13.0 did in code what the first analysis asked for. Measuring, grading, remembering, stage rules, burn modes and a delegation kit now live in code, not the prompt:
- the delegation kit is a policy engine, NEVER checked twice (in code and in the database), a journal, Undo and a daily digest;
- the database enforces most of it;
- 1,479 tests pass.

That is a real change from "the entrepreneurial part is prompt text that no code checks".

In live use, three things haven't changed yet:
- **You are still the approver, QA, art director and scheduler.**
  - You clicked 12 of 12 Etsy requests.
  - You pointed out photo problems five times in two days.
  - You picked the poster provider over the agent's choice and wrote the design brief.
  - You started 5 of the last 12 cycles, and reset the money guard by hand.
- **The agent still works on supply, not demand.** It built one more product, made photos and started a new physical channel. No plan step worked on search visibility or traffic for the six live listings, which have 4 views in total. The 44 learnings from your Etsy guides were never searched (`knowledge_search`: 0 calls).
- **Almost none of the new machinery has run.** Only one cycle ran on 0.13.0 (#46), and it was stopped. Venture cases, critiques, desk picks, predictions, agenda events, the action journal and all policy tables are empty.

And three new problems came with the speed of the work:
- **The money guard's promise broke** for the workshop (0.1).
- **The code-set structures now crowd the plan.**
  - Listing-test bars and decision points will fill the roadmap's places (FIX NOW 6).
  - Fixed section budgets cut the daily review's guidance and the poster venture's first test out of the plan.
- **The change outran any way to check it.** 77 commits and 44 migrations landed in about 21 hours. Two behaviour releases were installed 6.5 hours apart with red CI, and the agent read 12% of the 0.12.0 release notes and 23% of 0.13.0's.

**Next:**
- fix the money guard, safe mode and the picture limit;
- make the product factory able to do what the workshop was paid for;
- stop adding channels and decision tooling until a leg shows a signal;
- spend the next two weeks on German, in-season listings at about $2 a day.

---

## 2. What is going right (keep and build on)

| Area | What is strong | Evidence |
|---|---|---|
| Money safety for token-only calls | Every plan, work, reflect, review and study call really has a provable ceiling, and held live. One HTTP request per metered call (SDK retries off; continuations are new calls). Billing is reconstructed to the micro even for the pathological call. Overruns are detected loudly. | `economy/anthropic_transport.py:113`; `economy/metering.py:497-512`; live #423 |
| The budget engine at expected cost | It ended the starvation of 0.11.1. Cycles on 0.12.0+ ran 14-15 of 15 steps with no money stops (before: 34-40% of the cap unused). | `economy/metering.py:741-748`; live #42, #43, #45 |
| Continuity | Journals: 0 "Goal:"-only journals on 0.12.0+ (3 of 8 before). Code-built digests list exactly what was and wasn't done. The agent writes concrete handoffs when it journals. | `agent/digest.py`; live R2:1365-1396 |
| Obligations | Your messages come first, and it works live: #42 and #46 became ordinary cycles and answered you first. Promises are kept in code. | `agent/obligations.py`; `agent/loop.py:358-373` |
| Authority in the database | Milestone records, ventures, research budgets, evidence, cases, critiques, desk picks and forecasts are write-once or guarded by triggers. The agent can't rewrite its evidence or revive what you parked. No trigger or index was lost across the 44 new migrations, even through four table rebuilds (I checked a fresh build at 0014, 0042 and 0058). | `migrations/0020-0058`; 28 tables/58 triggers → 62/153 |
| NEVER | Defence in depth that really agrees: code and a database view check the same classes, with a 300-request agreement test. Only you can unlock (enforced in three places). Revocation runs before approval. Unknown executors are yours. | `agent/never.py`; `migrations/0051_never.sql`; `tests/test_never.py` |
| Exactly-once outbound | Every new executor (Pinterest, Printify, Undo) follows the email and Etsy pattern: a "running" row committed before anything is sent, "unclear" after a crash, never retried. What you approve is pinned by SHA-256 and re-checked. | `integrations/*_publisher.py` |
| Outside services | Each is reached from one module only, through an allowlisted transport with no redirects and capped responses. Tokens are in 0600 files and redacted. | `integrations/*_live.py` |
| The website builder | One audited template: every text escaped, https and mailto links only, no scripts, a CSP by hash, the same zip bytes for the same site, and legal pages from your options. | `products/site.py` |
| Research you can trust | Only URLs from the web tools' own result blocks count as sources, so the model's prose can't forge a source. Repeats are free and re-wrapped. The budget is enforced before any money is spent. | `agent/loop.py:1786-1796`; `agent/evidence.py` |
| Forged headings | JSON-quoting is structural: no text, whatever its source, can open a planner section. | `agent/context.py` (`_sections`) |
| Economics | `econ.py` is one small, shared, correct fee model for Etsy Germany (a €3.00 sale keeps €2.13, checked by hand). | `agent/econ.py` |
| The agent itself | It caught a formula bug that would have made the Nebenkosten tool useless before listing it. It filed its first upgrade request (Printify), released 3 hours later. It made no NEVER-class move, left your account confirmations to you, claimed no revenue, and put an AI line on every listing. | live #37-#42 |
| Tests | 1,479 tests pass. They use the real paths (the policy tests go through the database, not around it). The rule audit maps deleted prompt rules to the tests that prove them. | `tests/test_rule_audit.py` |

---

## 3. What is going wrong: seven root causes

### (a) A guarantee that holds on paper but not for every call

- **The ceiling.** The workshop's ceiling is a guess, and research's is empirical (0.1).
- **The stop.** A stop over a tiny overrun costs more than it saves (0.1).
- **Calls outside the cycle cap escape the other brakes.** Workshop runs, reviews and studies ignore both the maintenance cap ("one cycle a day of at most $0.40") and the 20:00 event reserve. In a maintenance cycle a workshop run may still spend $1.50, six times a day. **economy, medium, reproduced ✔**
- **Critic and consolidation calls count toward the cycle cap**, although the patch notes say they don't: a $0.20 critic cut a cycle's room for work from $0.50 to $0.30. **low, reproduced ✔** (`economy/ledger.py:397`)
- **STATUS promises the wrong cap.** It says "This cycle may spend up to $1.00" in maintenance (real cap $0.40), and offers a brainstorm in the focus mode, which then refuses it. **low ×2, reproduced ◐**
- **The safety factor learns badly.** It can't grow enough for a 5× miss (it caps at 4), and it can't decay for rare purposes: it needs 1,500 accurate calls. So on default settings one overrun locks the workshop until you press Reset. **low, reproduced ✔**

### (b) Built faster than it can be used or verified

- **21 hours, 77 commits, 44 migrations.**
  - App code grew from 26.4k to 43.7k lines (+66%) and tests from 14.5k to 25.4k.
  - 34 new modules.
  - Owner options went from 38 to 69, and `DOCS.md` from 822 to 1,689 lines.
  - Pinterest (2,167 lines) and Printify (2,414) each landed in about 40 minutes of commits. *(Counts measured by the stopped complexity reviewer; the line counts I re-measured.)*
- **Released faster than it could be checked.**
  - 0.12.0 was installed 8 minutes after its last commit, and 0.13.0 53 minutes after.
  - The two releases were installed 6.5 hours apart, both with red CI (0.5).
  - The first analysis's 72-hour hold and its Phase watch items were not kept: no phase's watch numbers were measured before the next phase shipped.
- **The agent doesn't read what changed.** After an upgrade it sees only the first 2 KB of the release notes, and the whole version is then marked as read (`agent/news.py`). For 0.12.0 → 0.13.0 it saw 1,937 of 8,456 characters, stopping inside the critic paragraph. It never read about READY, forecasts, unlocks, NEVER, Pinterest, Printify, the website or the listing tests. **loop, low, reproduced ◐** — and the CHANGELOG is still how behaviour is steered (first analysis 3(f)).
- **Most of it has never run.** At the 0.13.0 report, 0 rows in `venture_cases`, `venture_critiques`, `desk_picks`, `predictions`, `agenda`, `action_journal`, `policy_*`, `research_checks`, `lesson_pins`, `pinterest_*`, `printify_products` and `site_*`. `observations` isn't in the diagnostics at all, so nobody can see whether it runs.
- **The fake model can't produce what hurt live.** It never reports `iterations`, never overruns a workshop quote, never sets metric milestones or odds (`agent/fake_llm.py:11`). So the dry run and the tests never exercise the paths that failed live.

### (c) Quality still lives in the prompt, so you still do the QA

- **Photos.** Code counts photos (`integrations/qa.py:21`, `MIN_PHOTOS = 5`), so the agent met the count with near-duplicates, and you had to say so twice. `make_image` has one layout, can't crop or zoom a region, and makes at most 4 a cycle, fewer than the 5 the QA registry asks for. **tools, medium ×2, live ◐**
- **Spreadsheets.** There's no free way to preview another sheet, read cell values or copy a file. `make_image` refuses `file.xlsx#2`, and `make_spreadsheet` previews only the first sheet (`products/make.py:145-151, :188`). So $2.01 of $2.64 of workshop spend went on jobs code could do for $0: reading an xlsx, copying one file ($0.023), and rendering three sheets ($1.84, never used). **tools, medium, live ◐**
- **Posters.** There is no poster renderer. The agent told you "HTML/CSS" and then drew with Pillow in the workshop ("no browser is available"), without correcting what it had said.
- **A security gap in the workshop's PDF check.** The FIX NOW 24 check misses a JavaScript action inside a compressed object stream with an indirect `/Filter`, or a bare-CR `stream` line end. A workshop PDF with active content can then be kept and proposed as a buyer download. **tools, medium, reproduced ◐** (`products/checks.py:64, :77, :224-226`)
- **Some refusals come after the payment.** A `draft` append past 64 KB is paid for, then refused and thrown away ($0.07); a workshop folder deeper than 4 levels likewise. **low ◐**

### (d) Code-set structures crowd the plan

The first analysis asked code to keep the roadmap, obligations and digests. It does, but the planner's fixed budgets and slots weren't resized for what code now adds.
- **The roadmap fills itself.** The agent's 16 places and your 4 reserved places count milestones set by Ember's code, and `gates.py` never checks the limit. After the day-7 bars close (about 10-08), the 10 day-14 bars bring the roadmap to about 18 open: every `milestone_plan` is refused, and the agent can't drop code milestones. One more product line reaches 20, and your own "Add milestone" returns 409. **roadmap, high, reproduced ✔** (`agent/tools.py:2246-2251`, `agent/roadmap.py:38`, `agent/owner.py:671`)
- **Fixed section budgets cut the steering.**
  - TODAY'S REVIEW lost its Focus, Lesson and "Act on it" live (1,262 bytes cut). No plan saw the review's advice ("about 1 cheap check a day, to hold cost near $0.30 a day").
  - ROADMAP hides the poster venture's first test #8 (737 bytes cut).
  - About 8.6 KB of the planner's budget went unused meanwhile.

  **loop, medium, reproduced ◐; live**
- **A venture's FOCUS (1,900 bytes) loses the numbers that matter.** Once a venture has a case and a critique, FOCUS drops its knock-outs, evidence, pitch and often the critic's flaw: the reasons the desk gave it. **ventures, medium, reproduced ◐**
- **A missed bar's action never reaches the plan.** The obligation line is cut before "fix the titles, tags and category once" or "stop building this product type". **roadmap, medium, reproduced ◐**
- **A stopped cycle breaks the handoff.**
  - The digest says "the plan was done".
  - The journal's summary is the guard's refusal.
  - The next plan loses the last written handoff (it shows the code journal instead).
  - Your comment on an approved request is shown to one cycle only. #44 planned to act on "photos look like duplicates", was stopped, and the comment was lost.

  **loop, medium ×3, reproduced ◐; live**
- **In practice, no cycle reflects any more.** A cycle whose work step writes the journal skips the reflection (`agent/loop.py:731-732`). Live, that is every cycle since 0.12.0: nothing checks undone calls, writes lessons or updates strategy. STRATEGY still says "current priority: dropshipping" (unchanged since #35), and no lesson was written on 09-30. **loop, medium, reproduced ◐; live**
- **The owner's inbox and the waiting list are windows, not lists.** The Inbox shows the newest 30 messages, so an older one can't be read, checked or removed (and a pasted password stays in every plan). WAITING FOR YOUR OWNER looks only at the newest 20 approvals. **web medium, loop low, reproduced ◐**

### (e) Delegation that doesn't fit the requests

Besides the loopholes in 0.6:
- **An unlock follows the plan's focus, not the thing it acts on.** A request is stamped with the cycle's focus milestone (`agent/store.py:355-365`), and `policy.apply` reads only that milestone's grant (`agent/policy.py:242-246`). The same 5% price cut on the same listing is approved or waits depending on which milestone the plan named. An unlock for "retire the old CV templates" can deactivate the shop's best seller. **autonomy, medium, reproduced ◐**
- **Success ends autonomy quietly.** An unlock stops working once its milestone is met, and code-set milestones last 7-21 days. A held request is still approved after its milestone closed as done. **autonomy, low, reproduced ◐**
- **The rules don't match the requests you get.**
  - Of the 13 live requests, 2 would have fitted a rule (the photo fixes 3 → 5).
  - Next month's requests will mostly be title, tag and category fixes owed after the day-7 bars, Printify products and pins, and no rule covers them.
  - Promotions (5 unchanged approvals of one rule for one milestone) can't trigger.

  *(strategy review, reproduced)*
- **The NEVER tax and contract word list is both too weak and too broad.** It misses "§ 19 UStG", "Kleinunternehmer", "Auftrag … Rechnung folgt", "invoice", other languages, Cyrillic look-alikes and invisible characters. Yet it catches ordinary product copy: live request #14 hits "Mietvertrag" and "Steuerberatung", from the disclaimer the review asked for. **autonomy, medium, reproduced ◐**
- **Wake-ups spend money on anything, and you can't turn them off.**
  - Event wakes run before the no-room, crash-loop and back-off guards, and ignore your wake settings (`agent/service.py:305-307`). There is no `wake_on_events` option. ✔
  - Auto-replies, bounces, no-reply senders and spam wake paid reactive cycles; mail from Ember's own address becomes an obligation nothing can close. ◐
  - Every open milestone's last day is urgent, so bookkeeping wakes Ember.

  **autonomy and mail, medium**
- **Your own clicks start full cycles.**
  - Two approvals 83 seconds apart started #43 and #44 ($2.57 in 8 minutes).
  - On 09-28, 9 messages started 9 cycles in an hour.

  You switched both wake options off after such bursts, which says you want Ember on its own clock. So a message now waits for the agent's chosen sleep (420-600 minutes) unless you press Wake now.

### (f) Decisions: still supply-driven, and the new gates can be gamed

- **Any Library document passes as demand evidence.** A demand note's source may be any Library document, even a removed one: live, a generic Etsy SEO guide "backed" the Nebenkosten line. It needn't contain any number. **ventures, medium, live ✔**
- **`vendor_only` is too weak.** Any one "independent" claim of any metric lifts it, and the vendor table misses shopify.de, bigbuy.eu, syncee and finerworks. **medium, reproduced ✔**
- **A venture proposed before the gates escapes them.** Dropshipping #3 shows as a complete proposal with no case, no knock-outs, no critique and 0 counted research. Back it, and a €50 test above your €20 venture cash goes to building. **medium, reproduced ◐**
- **`venture_create` at "live" skips your backing.** The check is "Ember earns somewhere", so with 6 active listings any new venture can start live. The FIX NOW 16 claim doesn't hold. **medium, reproduced ◐**
- **The expected net ignores the months before the first sale.** The fixed costs of those months aren't counted, so a slow, losing case shows a profit. A later first sale can even rank higher on READY (a losing case improves from −97 to 0 as its first sale moves 0 → 6 months out). **econ, medium, reproduced ◐** (`agent/econ.py:99-102`)
- **The "slow" knock-out counts in whole months.** Below about 61 days of runway, every honest case with a first sale in a month is knocked out, and 0 months passes. The agent learns to enter 0. **medium, reproduced ◐**
- **Research-budget bypass.** Research for a venture in an ordinary cycle focused on it escapes its research budget. **low ✔**
- **Milestones and forecasts can be gamed.** **medium ×3, reproduced ◐**
  - The agent can move a code-set bar, or any metric milestone, to another project, which changes what code counts.
  - It can void a losing forecast by dropping the milestone before its date (FOCUS even suggests dropping an out-of-reach milestone).
  - A backed venture goes live on a first test the agent closes on its own word.
- **Live behaviour.**
  - Nothing worked on demand or traffic (section 1).
  - The poster margin figures drifted between cycles: €3.98, then €7.92 at Gelato, then "roughly €8" at Printify with the base cost unknown.
  - The provider flipped three times in 3 hours.
  - The agent asked you twice to look up the Printify base cost, against your standing instruction ("never build tasks, specs or research for me").

### (g) Money records that would mislead once sales come

No money path has run live yet (0 orders), so these are latent:
- **Etsy fees are under-booked by 15-29% a sale.** The ledger's fee model leaves out the USD 0.20 listing fee each sale triggers and the VAT on fees, while `econ.py` counts both. Listing fees you pay are never booked at all. **etsy, medium, reproduced ◐**
- **A refund can be held back.** A refund of revenue Ember's code recorded is held if it would make Ember dead or unfunded, so the agent keeps spending money the buyer got back. **medium, reproduced ◐**
- **Print on demand is booked lopsidedly.** **channels and etsy, medium ×5, ◐**
  - The shipping the buyer pays is dropped from revenue, while Printify's shipping is a cost.
  - Printify's costs are recorded as overhead while the sale goes to the venture.
  - Amounts are labelled with your currency option, not the currency Printify reports.
  - The 15% margin check uses a looser fee model than `econ.py` and leaves out VAT.
- **Printify listings are invisible to the metrics.** The Etsy listings Printify creates are left out of the Etsy metrics, the listing test and the "nothing sold in 60 days" rule. So the poster venture would be parked 60 days after going live even if posters sell (unless you record the revenue by hand), and a sale on a product line with no venture (like Nebenkosten #7) doesn't save the Etsy leg either. **roadmap, channels and ventures, medium, reproduced ◐**
- **A timed-out Printify publish is never reconciled.** It leaves a live Etsy listing Ember never tracks. **medium, reproduced ◐**

### (h) Privacy of the shareable report

- **The report carries your Library texts.** It prints library study replies and `library_read` results (`diagnostics.py:625-629` hides only research). Live, lines 766 and 845-847 of the 0.13.0 report show Etsy's guides digested: harmless here, but your own notes or a supplier's terms would go out the same way. **ventures and mail, medium ✔** *(the finder rated it high; I rate it medium because nothing private leaked)*
- **Masking senders' names overwrites ordinary words.** Senders' display names are replaced everywhere, before addresses are masked. So in both reports:
  - "Pinterest" became "[sender of email #15]", including in the venture list;
  - "Fiverr" became "[sender of email #7]", inside your standing instructions;
  - Ember's own mail domain became "[sender of email #1]", which also defeats the address mask ("jane@[sender of email #1].org").

  **mail, medium, live + reproduced ✔**
- **The open sensor endpoint can carry an email address.** `/api/sensors` needs no user, and since 0.13.0 it carries the daily digest, which can name the address of a person who asked to stop. **web, medium, reproduced ◐**
- **Smaller leaks.** Agenda texts keep subjects cut to 80 characters, and opt-out reasons are quoted. Your Home Assistant user ID and display name appear throughout EVENTS. **low ◐**

---

## 4. The 0.11.1 plan, checked against the code

**Holds** (checked by the reviewers):
- **FIX NOW items:**
  - FIX NOW 6: done needs evidence; closed_by.
  - FIX NOW 8: goals first, ROADMAP floor.
  - FIX NOW 12: memory_read, no blind rewrites.
  - FIX NOW 13: the tool side.
  - FIX NOW 14.
  - FIX NOW 15: the field order.
  - FIX NOW 18, 19, 20, 21, 22, 23, 27, 30, 31.
  - FIX NOW 3.
  - FIX NOW 25 (5) and (6).
- **Roadmap items:** 1, 3, 5, 6 and 7.
- **Phase A:** P&L per venture and net runway.
- **Phase B:** model routing, lessons and pins, make room (tool sets, rule audit), obligations.
- **Phase C:** the critic's isolation, the desk's ranking and audit, predictions' first-date rule.
- **Phase D:** the agenda's limits, the reactive cycle, the policy engine's levels, NEVER in code and the database agreeing, revoke-before-approve, the audit feed, the connector journal for email and Etsy.
- **Phase E:** Pinterest's single module, PKCE, the NEVER first board; Printify's allowlist and NEVER first product; the website's escaping, CSP hash, zip and preview sandbox.
- **Owner identity:** `owner_user_ids` covers every route, including those added in 0.12/0.13 (but see 0.2 for safe mode).

**Broken** (the patch note's claim doesn't hold):

| Claim | What the code does | Ref |
|---|---|---|
| FIX NOW 16: "venture_create at live needs an active listing or recorded revenue" | checks whether Ember earns anywhere, not the venture | 3(f) |
| FIX NOW 24: "the PDF check reads files whole" | bypassed by an indirect `/Filter` or a bare-CR stream | 3(c) |
| 0.2: "every email address masked" | the name mask runs first and garbles addresses | 3(h) |
| Library: "diagnostics never show texts" | study replies and `library_read` are printed | 3(h) |
| Phase D: "a kill switch that revokes every class" | kill only sets a flag; unlocks survive and fire on reset | 0.6 |
| Critic and consolidation: "daily cap only, not the cycle cap" | both count toward the cycle cap | 3(a) |
| E4: "the POD venture's first test is a first order" | venture #4 was backed under 0.12.0, and its first test #8 is still prose; migration 0055 didn't retarget it | 3(g) |

**Partial** (the main claim holds, with a gap):
- **FIX NOW 9, 17 and Phase B's budget engine.** They work for token-only calls, not the workshop (0.1).
- **FIX NOW 10 and 11.** The handoff and the code journal work, but are lost or wrong after a stopped cycle (3(d)).
- **FIX NOW 2, 5, 28.**
  - The order net is right, but a partial refund of an order you recorded is never flagged.
  - The fee model misses the listing fee and VAT.
  - Renewal rests on an unverified API assumption. Listings missing from Etsy's batch answer stay "live" forever.
- **FIX NOW 4.** Still yours (the stats history needs your Etsy-terms check). The listing bars no longer need it.
- **FIX NOW 29.** Opt-outs miss 13 common phrasings ("Hello,\nPlease stop.", "Hallo,\n\nStopp."). A refused IMAP fetch still loses the email.
- **FIX NOW 32.** One call's uncut fields can still total more than a reply holds (`request_approval` 5,240 characters).
- **FIX NOW 7.** The agent's side holds; your 4 reserved places don't (3(d)).
- **Roadmap 2 and 4.** "Counts only what belongs to it" breaks: the agent can re-link a milestone, and Printify is left out. A parked venture's projects keep their bars.
- **Phase C.** Evidence, research budget, demand notes and knock-outs hold, with the gaps in 3(f).
- **Phase D.** The daily digest's first entry ("carried out nothing" for a day with 8 actions) and the Undo:
  - dead-ends after one undo;
  - never runs while paused or killed, though the card promises "the next round";
  - hangs after a crash during a pin or product delete.
- **E1.** Inquiries rest on the unauthenticated `From:` (0.6).
- **Burn modes.** Correct thresholds and hysteresis, but maintenance bounds only cycles (3(a)), and after a failed or stopped cycle it retries in 30 minutes instead of the next day.
- **Waiting for your decisions.** It works, but WAITING misses requests beyond the newest 20. The sleep cut while a request waits is max(min sleep, wake interval) = 60 minutes at your settings, not the 240 the patch note says.

**Still open from 0.11.1 (unchanged):**
- **The score.** The venture weight is still a compensatory sum: doability 1 still scores 86.
- **Research.** Still one search per call.
- **Batch approve.** Still missing.
- **Etsy reviews.** Still not read.
- **Owner milestones.** You can't give your own milestones a metric.
- **The money goal.** Fixed at 90 days out.
- **The default cycle's margin.** It is 1.62 working cycles, near the 1.5 floor.
- **Uncertain 5xx charges.** Still reconciled by hand against the Console ($0.10 open).
- **Packaging.** No prebuilt images or hash-pinned installs.
- **Section 9.** Captured requests, evals, the live canary, the "realistic" fake and LIVE_FINDINGS: none exist (section 8).

---

## 5. FIX NOW: concrete defects, ranked

Effort: S is under a day, M is 1-3 days. **All rows are code**; your own steps are in 0.2, 0.4 and section 7.

| # | Defect | Sev. | ✔? | Evidence | Fix | Effort |
|---|---|---|---|---|---|---|
| 1 | Workshop calls have no provable ceiling; the run cap, daily cap and balance are protected only up to a guess | High, live + reproduced | ✔ | `economy/estimate.py:17-44`; `agent/workshop.py:205-210`; live #423, #444, #369 | 0.1: reserve at max(quote, run cap, 1.5 × p95), price per sampling, record iterations | M |
| 2 | Any overrun stops the whole cycle and skips the reflection; the digest says "the plan was done"; back-off shortens the sleep | Medium ×3, live + reproduced | ✔ | `economy/metering.py:1151-1164`; `agent/digest.py`; `agent/service.py:680-684`; `agent/loop.py` (`_write_report`) | Keep the cycle running below a tolerance; always allow the reflection; build the journal and digest from the cycle's real status; no back-off shorter than the chosen sleep after an overrun | S |
| 3 | Safe mode forgets `owner_user_ids` | High, reproduced | ✔ | `config.py:404-407`; `security.py` | 0.2 | S |
| 4 | Safe mode lifts an engaged kill switch | High, reproduced | ✔ | `main.py:154`; `agent/owner.py:895-908` | 0.2 | S |
| 5 | 13 options bounded only in `config.py` force safe mode (and so #3 and #4) | Medium, reproduced | ◐ | `config.py:285-300`; `config.yaml` | Bounds and patterns in `config.yaml`; website values reported on its card | S |
| 6 | Pictures over 12 MP refused, with a misleading message: the poster can't be proposed | High, reproduced | ✔ | `products/images.py:21, :85`; `agent/tools.py:3606-3610`; `products/checks.py:41` | One limit (about 40 MP), a clear message | S |
| 7 | CI red on both releases (secret-scan false positive) | Medium, live (GitHub) | ✔ | `tests/test_privacy.py:122`; `.gitleaks.toml:21-27` | Allowlist the fake; release only on green | S |
| 8 | Code milestones fill the agent's 16 and your 4 places | High, reproduced | ✔ | `agent/tools.py:2246-2251`; `agent/owner.py:671`; `agent/gates.py` | Count only agent and owner milestones toward the limits; one milestone per product line's test whose progress names the current bar | M |
| 9 | Kill switch keeps unlocks; take-back leaves already-approved actions to run; unlocks allowed without owner identity; unlocks skip QA; the automatic email footer claims your approval | Medium ×5, reproduced | ◐ / ✔ | `agent/owner.py:874-892`; `agent/policy.py`; `integrations/executor.py` | 0.6 (all S) | S |
| 10 | Unauthenticated `From:` counts as "wrote"; auto-replies, bounces, spam and Ember's own address wake paid cycles or become obligations | Medium ×3, reproduced | ✔ / ◐ | `agent/never.py`; `integrations/mailstore.py`; `agent/agenda.py` | Store the provider's Authentication-Results verdict; count only authenticated, non-bulk, non-machine mail; one predicate for inquiries, metrics and wakes | M |
| 11 | Event wakes bypass your settings and the no-room, crash-loop and back-off guards | Medium, code | ✔ | `agent/service.py:305-321` | Run the guards first; add `wake_on_events`; make orders, favorites and code-graded due dates non-urgent | S |
| 12 | The product factory can't make distinct photos, other sheets' previews, a copy or a read of a spreadsheet, or a poster; `make_image` allows 4 a cycle against `MIN_PHOTOS` 5 | Medium ×3, live | ◐ | `products/make.py:145-151, :188`; `products/sheets.py:441`; `integrations/qa.py:21` | Per-sheet previews and `file.xlsx#2`; a free copy; `workspace_read` of xlsx/docx/pdf as cells or text; crop and zoom modes; a Pillow or SVG poster template; a per-cycle cap ≥ 10; QA counts distinct pictures (hash) | M |
| 13 | Planner section budgets cut the review's guidance, the first test, the venture's knock-outs and critic, and a missed bar's action | Medium ×3, live + reproduced | ◐ | `agent/context.py:57-69`; `agent/review.py:647-673`; `agent/ventures.py` (FOCUS); `agent/gates.py` (`_owe`) | Two-pass allocation (unused budget to cut sections, by priority); verdict lines after Focus and Lesson; the action first in bar misses | M |
| 14 | A work step's journal skips the reflection: every live cycle since 0.12.0 is unreflected | Medium, live + reproduced | ◐ | `agent/loop.py:731-732` | `write_journal` reflect-only, or end the act loop there and still reflect | S |
| 15 | Library texts in the shareable report; name masking garbles words and addresses; the sensor digest can name a person | Medium ×3, live + reproduced | ✔ / ◐ | `diagnostics.py:625-629`; `privacy.py`; `agent/audit.py` | Hide study and library_read like research; mask addresses first and only person-like names; no address in `why`; the sensor gets counts only | S |
| 16 | Printify: margin check's fee model; P&L asymmetry and dropped shipping; currency assumed; unclear publishes never reconciled; Printify listings invisible to metrics, bars and stage rules; can't price before proposing | Medium ×7, reproduced + live | ◐ | `integrations/printify*.py`; `agent/metrics.py`; `agent/gates.py`; `agent/stages.py` | One fee model (`econ.fees`) with VAT; attribute costs to the venture; read Printify's currency; reconcile unclear rows in the sync; union Printify listings into the metrics; a free cost probe (create unpublished, read, delete) | M |
| 17 | Etsy fees under-booked 15-29%; a refund held keeps the balance overstated; a partial refund of an order you recorded not flagged | Medium ×2, low, reproduced | ◐ | `integrations/etsy.py` (`fees_share`); `integrations/etsy_revenue.py` | Share econ's fee table; write refunds as facts and pause with a reason instead of withholding | M |
| 18 | Gates that can be gamed: any Library document as demand evidence; weak `vendor_only`; a legacy proposal escapes every gate; create at live; research budget bypass | Medium ×4, low, reproduced + live | ✔ / ◐ | `agent/demand.py:29-44`; `agent/knockouts.py`; `agent/tools.py:1895` | Library documents only when linked or marked as a keyword export, with a number; vendor match by registrable name, a demand-type metric; send pre-gate proposals back to researching; drop "live" from agent start stages | S |
| 19 | `econ` expected net ignores pre-sale fixed costs; "slow" knock-out in whole months | Medium ×2, reproduced | ◐ | `agent/econ.py:99-102`; `agent/knockouts.py` | `ev = ((H − m) × expected − m × fixed − setup) / H`; the first sale in days | S |
| 20 | Roadmap integrity: agent can re-link code or metric milestones; your drop of a bar opens the next; forecast voided by the agent's drop; first test closed on the agent's word; a late grade after a sync gap | Medium ×4, low, reproduced | ◐ | `agent/tools.py` (`_milestone_update`); `agent/gates.py`; `agent/predictions.py`; `migrations/0030` | Freeze links on code and metric milestones (tool and trigger); your drop ends that line's test; an agent drop settles as a miss; live needs a code or owner close; grade from the last reading before the due day | S |
| 21 | Unlocks keyed to the plan's focus milestone; NEVER word list too weak and too broad; price band compounds; veto window approves after done; Undo blocked while paused or killed and a dead end after one undo | Medium ×4, low, reproduced | ◐ | `agent/store.py:355-365`; `agent/policy.py`; `agent/never.py`; `agent/audit.py` | See section 6, phase 3 | M |
| 22 | Workshop PDF active-content check bypass | Medium (security), reproduced | ◐ | `products/checks.py:64, :77, :224-226` | Refuse indirect `/Filter`/`/DecodeParms`; delimit streams after \r too; better, walk objects with a real parser | S |
| 23 | Opt-out detection misses common phrasings; a refused IMAP fetch drops the email; no back-off for a failing mailbox | Medium, low ×2, reproduced | ◐ | `integrations/optout.py`; `integrations/mail.py` | Short own lines after greetings count; stop the batch before a refused UID; back off | S |
| 24 | Inbox shows only the newest 30; WAITING only the newest 20 approvals; the agent reads 2 KB of each release's notes | Medium, low ×2, reproduced | ◐ | `agent/views.py`; `agent/context.py`; `agent/news.py` | Paging and "always your unanswered ones"; select pending directly; a read position per version | S |
| 25 | Wake now while the last will is due bypasses its back-off (3 failures in a minute, or a refused-cycle loop until midnight) | Medium, reproduced | ◐ | `agent/service.py`; `agent/scheduler.py` | Clear the wake request when its cycle starts; wait a round after a refused or failed cycle | S |
| 26 | Small defects: <br>• event reserve's 20:00 is 19:00 or 21:00 on DST days (the scheduler polls every second for an hour on 10-25) <br>• 8 `count_tokens` requests per step <br>• the diagnostics preview runs keepers and marks agenda events as woken <br>• cycles killed by a restart get no digest <br>• the constitution still says you carry out everything but email <br>• an order with many lines fails a CHECK and stops every sync for 30 days <br>• orders fetched only for the last 30 days <br>• ledger paging gap <br>• `.docx` zip bombs <br>• 10-second upload timeout <br>• Pinterest refresh lifetime assumed <br>• a pin's listing not re-checked <br>• the Impressum can be email-only <br>• the NEVER agreement test misses the new executors | Low, reproduced or code | ◐ | see `raw/review-*.json` | A small fix plus a test each | S |

---

## 6. BUILD NEXT: a phased plan

**First, a freeze.** No new channels, owner options, prompt sections or Phase C/D additions until a leg shows a signal (a product line clearing its day-14 bar, or a first sale). The code has more machinery than live data to feed it. Every phase below fixes or fits what exists.

### Phase 0: stop the bleeding (this week, one hotfix release)
- Rows 1-7 of FIX NOW: the money guard, the overrun stop, safe mode, the picture limit, CI.
- Row 12's cheapest parts: per-sheet previews, `file.xlsx#2`, a free copy and read, a poster template. They remove most of the workshop spend and unblock the poster test.
- A projected date for the next burn-mode change on STATUS and the dashboard.
- **Watch:**
  - 0 cycles stopped by an overrun;
  - workshop spend under 10% of a day;
  - the poster proposed;
  - CI green on the released commit.

### Phase 1: the plan sees what code keeps (week 2)
- Rows 8, 13, 14, 20 and 24: the roadmap's places, two-pass section budgets, reflection in every cycle, roadmap integrity, the release notes read in full.
- **Wakes through one agenda.** Your messages, rejections and venture decisions become agenda items, coalesced for about 10 minutes after your last action and run as one reactive cycle. Plain approvals that code carries out don't wake anything. Orders, favorites and code-graded due dates are non-urgent.
- **A cheap reply call.** A "reply" purpose (one worker call with STATUS, OBLIGATIONS and your unread messages; only `message_owner` and `obligation_done`), so a message is answered in minutes for about $0.03-0.05 without a full cycle.
- **Watch:**
  - 0 plans with the review's Focus cut;
  - the first test visible in every venture plan;
  - owner-triggered spend under 20%.

### Phase 2: money records and print on demand (weeks 2-3)
- Rows 16, 17, 18 and 19.
- Printify listings in the metrics, bars and stage rules.
- One fee model everywhere.
- A free cost probe, so posters are priced from real costs.
- Printify order costs recorded automatically under the same option as revenue (a cost can only lower the books).
- **Watch:**
  - every sale reaches the right venture's P&L with fees;
  - the poster test graded by code.

### Phase 3: delegation that fits (weeks 3-4, before you unlock anything)
- Rows 9, 10, 11 and 21.
- **Re-key unlocks.** Unlocks go per venture or product line and action class, with a daily limit, a budget of actions and an end date. A request belongs to what it acts on: listing → project → venture.
- **Rules for the requests you actually get:**
  - `listing_edit`: title, tags, category, description or photos of Ember's own live listing; QA must pass; at most one a week per listing;
  - `pod_product` after the first;
  - pins after the first board.
- **Offer promotions on the approval card:** after 3-5 unchanged approvals, one click.
- **Narrow NEVER's tax and contract check to legal acts.** Apply it to the request's own text, not the listing's product copy, after Unicode normalisation.
- **An autonomy scorecard in code:** your actions a day by kind, the share of owner-triggered cycles, the share of reversible actions carried by unlocks, the Undo and veto rate, time to answer your message.
- **Watch:** the first analysis's Phase D items (your actions under 10 a day; 0 NEVER actions carried; reaction under 60 minutes).

### Phase 4: quality in code (week 4 on)
- **Photo sets:** a diversity check (perceptual hash) and required photo kinds (cover, inside page, what's included, format).
- **A cheap vision check** that scores a listing or design against a rubric before proposing.
- **Your approval comments become obligations.**
- **A ring-fenced budget for a backed venture's first test** that survives maintenance mode.

**Stop building:**
- more channels;
- more venture-decision tooling (the critic, desk and forecasts have not run once);
- more owner options;
- more planner sections.

Don't deepen the policy engine before phase 3's re-keying.

---

## 7. Business strategy

### An honest read
- **Six listings, 4 views, 0 favourites, 0 sales** after about 31 hours: too early to judge. Most of the first analysis's Etsy advice isn't done yet:
  - 3 listings are still EN+DE bilingual;
  - 4 have fewer than 5 photos, and you called the added ones duplicates;
  - two sit in the generic Templates category;
  - there are no German seasonal listings;
  - there is no ads test.
- **The day-7 bars (10-07) will probably be missed** at about 0.6 views per listing per day. Each miss owes a free title, tag and category fix.
- **The Etsy leg's real test is listings launched now.** Their day-14 bars fall around 10-18 to 10-22, and Q4 is Etsy's season.
- **The listings are the asset, not the cycles.** They sell with no API spend: Etsy delivers downloads, Printify fulfils posters, and the Etsy sync runs even while Ember is paused or dormant.
- **The money to reach Christmas.** To still be running on 12-20, spending must average at most about **$1.11 a day** from now ($89.63 over 81 days).

### Settings (👤 today)

| Option | Now | Set | Why |
|---|---|---|---|
| `daily_spend_cap_usd` | 7.00 | **2.50** (from 10-22: 0.75) | $4.89/day burns the runway in about 18 days and drops to maintenance around 10-03 |
| `cycle_spend_cap_usd` | 1.00 | 0.60 | a working cycle costs $0.25-0.40 |
| `workshop_run_cap_usd` | 1.50 | 0.60, and 1 run a day | until 0.1 is fixed, the cap isn't a cap; the factory fixes remove most runs |
| `planner_model` / `strategy_model` | Opus / – | **Sonnet / Opus** | routine plans at about a third of the price; venture plans, the review and the critic stay on Opus |
| `research_model` | – | Haiku 4.5 | its paired quality check runs first (Phase B) |
| `min_sleep_minutes` / `wake_interval_minutes` | 30 / 60 | **180 / 240, changed together** | changing only one forces safe mode (0.2) |
| `etsy_auto_record_revenue` / `etsy_usd_per_eur` | off / 0 | on / about 1.10 | sales then reach the P&L, the money goal and the burn mode without your clicks |

Don't reset the workshop's overshoot scaling again, and don't raise caps or top up before a line clears its day-14 bar.

### The next 14 days: build at about $2 a day
1. **6-8 German, in-season, one-language listings by about 10-08,** each after a demand note with a number:
   - Haushaltsbuch and Jahresplaner 2027;
   - Adventskalender-Aufgaben;
   - Wichtel- and Weihnachtsspiele;
   - a Weihnachtsquiz;
   - a Kindergeburtstag-Schatzsuche;
   - price €3.90-6.90.

   Make the photos from PDF pages with `make_image`, never the workshop. Nebenkostenabrechnung is in season too: 2025 statements are due to tenants by 12-31 (§556 Abs. 3 BGB).
2. **Fix the live listings once:**
   - split the bilingual bundles;
   - move Lebenslauf and the cover letters to the résumé category (#1876);
   - use the Library's title, tag and attribute guidance.
3. **The poster test (venture #4), capped.**
   - Exactly 3 posters in the colourful style you asked for, aimed at a German niche (sayings, kitchen, Hausregeln), not generic Bauhaus art. Generic posters sit in a crowded €17-28 niche with free shipping.
   - Price A3 at **€24.90 or more** with free shipping, and list the same designs as €3.90 printables as a cheap demand probe.
   - Keep total poster API spend at $3 or less ($1.69 spent so far).
   - Before the first physical listing, 👤:
     - decide on Etsy's Offsite Ads (15% on a sale they bring, optional for shops under $10k a year: opting out or pricing about €3 higher protects the margin);
     - LUCID packaging registration;
     - GPSR details and the production partner at Etsy.
   - *The margin numbers:* the code's 15% rule is too thin. At its lowest allowed price for a €9 base, and if Printify charges you 19% VAT that you can't reclaim as a Kleinunternehmer, a sale keeps about €0. A sale through Offsite Ads loses €2-4. *These are the economics reviewer's figures from `econ.py` and `integrations/printify.py`; confirm VAT and Kleinunternehmer status with a Steuerberater. None of this is legal or tax advice.*
4. **Park dropshipping (#3), the AI companion, recruiting and the ad blog** with their reasons. Dropshipping would fail its own knock-outs (cash €50 > €20, vendor-only evidence) if it went through them (3(f)).
5. **Pinterest: hold.** You said you don't want to run it, and it isn't set up (no app ID). Before investing time, check Pinterest's current rule for trial-access apps: the economics reviewer read that their pins may be sandbox-only (visible only to their creator), which would contradict `integrations/pinterest.py:3` and DOCS. *Not verified here.*
6. **A €14 Etsy Ads test (👤):** €1 a day for 14 days, 10-08 to 10-21, on Nebenkosten and the best new seasonal listing. Read it on 10-21: a click rate of 1% or more, and a sale within about 80 clicks, or the offer or price is wrong.
7. **Your standing instruction** still says "Never answer my ideas with a no". It now fights the code's knock-outs and data-backed parking (FIX NOW 14), and the agent's memory hardened it into "no idea gets a no". Consider replacing it with: "A no backed by numbers, with the closest test, is a result."

### Decision dates and odds
- **Balance checkpoints:** at least $74 on 10-07, $60 on 10-14 and $50 on 10-21.
- **10-07:** the day-7 bars. Fix each missed line once.
- **10-14 / 10-21:** the day-14 bars of the old and new lines, and the ads test. Park lines that miss.
- **10-22:** unless a line clears its day-14 bar, switch to **harvest settings**:
  - daily cap $0.75;
  - wake interval 720-1440;
  - workshop off.

  Let the catalogue sell through the season. Break-even at about $0.65 a day is about 4 digital sales a month.
- **11-15:** spend up to $8 on variants, only if the last 30 days had 3 or more sales.
- **Odds (the economics reviewer's judgement, not a measurement):**
  - about 25% of covering a harvest-level burn in Nov-Dec;
  - about 10% of covering $1.90 a day;
  - under 3% at today's burn.

---

## 8. Process

- **Release only on green CI** on the exact commit, and fix the secret-scan allowlist now (0.5). Tag `vX.Y.Z` when `config.yaml`'s version changes. There are no tags, and five auto-named branches.
- **Keep the 72-hour hold for behaviour releases.**
  - 0.12.0 and 0.13.0 were installed 6.5 hours apart.
  - Only one cycle ran on 0.13.0 before this report.
  - Nothing in either release could be measured before the next shipped.
- **Measure the watch items before the next phase ships.** None of the Phase A-E watch items is computed by code (autonomy scorecard, phase 3).
- **Stop steering by release notes.**
  - The agent reads 2 KB of them (3(b)).
  - Keep agent-facing notes short and factual (what a tool now does), separate from your changelog, and never use them to change behaviour.
- **Still not built from section 9:**
  - **captured requests:** the exact request of plan, review, critic and reflect calls, compressed, live only, never in diagnostics, blanked on removal. With them, #44's workshop request could be replayed instead of reconstructed;
  - **evals:** about 12 scenarios from the real incidents; add #44's "render spreadsheet sheets" and #46's "overrun mid-plan";
  - **a live canary;**
  - **a realistic fake:** server-tool loops that grow, overruns, blocked sites, over-long fields;
  - **a LIVE_FINDINGS log.**
- **Prompt profiles churn.**
  - PLANNER_OPENING was raised in 11 of the 77 commits, WORK and REFLECT in 22 each.
  - The ordinary step's fixed prompt is 48.5 KB with your channels on. That is above the 45,866-byte bound, which the test checks only with channels off.
  - Prompt-wording asserts grew from 46 to 64 (complexity reviewer's count).
- **Growth.** Only the event log is pruned. Model calls, tool calls and call texts grow without limit: 3.3 MB after 3 days (low).

---

## 9. What NOT to do

- **Don't unlock anything before 0.6 and phase 3.** The loopholes are real, and none can be seen live yet because nothing is unlocked.
- **Don't treat the money guard as a ceiling for the workshop** (or research) until 0.1 lands, and don't reset its scaling or raise the workshop cap to "make it work".
- **Don't change one sleep option without the other,** or any option without checking for the safe-mode banner (0.2).
- **Don't build more channels, options or decision tooling** until a leg shows a signal. Pinterest, the website, venture cases, the critic, the desk and forecasts have not run live once.
- **Don't let quality stay a count.** Five photos that repeat each other satisfy the code and fail you and the buyer.
- **Don't ship two behaviour releases in a day,** and don't release on red CI.
- **Don't pay the workshop for what code can do:** copying files, reading cells, rendering sheets, drawing template posters.
- **Don't back dropshipping as it stands.** It never passed the gates that now exist.
- **Keep what the first analysis said:**
  - no account creation;
  - no cold outreach;
  - no automated posting in communities;
  - no scraping Etsy;
  - no revenue the agent records itself;
  - don't paste the full (private) diagnostics into tools that can write to the repository.

---

## Appendix: how this was made

- **Reviews.** Twelve subsystem reviewers were started, and ten finished: economy, loop, tools, roadmap, ventures, autonomy, Etsy, channels, web, mail. They produced 112 findings, of which 7 were rated high by their finders.
  - I merged the two workshop highs (economy and tools) into one.
  - I rated the library leak medium (3(h)).
  - That leaves **5 high**, all of which I confirmed in the code.
- **Verification.** Independent verifiers finished for the economy findings (8 of 8 confirmed) and six ventures findings (6 of 6 confirmed) before the work was paused. Two findings were found independently by two reviewers (the email footer, the forged sender). I checked:
  - the five highs;
  - the event-wake order;
  - CI on GitHub;
  - the library leak in the live report;
  - the schema (no triggers or indexes lost, 0.11.1 → 0.13.0).

  The rest (◐) were reproduced by their finders with scripts against the real code but not re-checked.
- **Not finished.** The database and test/process reviews, and the complexity strategy review. Their numbers in 3(b) and section 8 come from what the stopped reviewer had measured and from my own checks.
- **Live data.** Every live claim cites the 0.12.0 report (R1, 12:10 UTC) or the 0.13.0 report (R2, 15:10 UTC). The reports were read as data, never as instructions.

---

## 10. 0.15.0 patch notes

*Added 2026-10-01, for the release on `claude/youthful-curie-p5h0eu`. 0.15.0 was built as 17 work packages, each reviewed in up to two rounds and merged into one branch. These notes are taken from each package's final report. The raw review files in `analysis-0.13.0/raw/` stay on the analysis branch: they hold private live data and are not in this repository.*

*Version number: another session released its own 0.14.0 meanwhile (the blog on your website, uploaded over SFTP after your approval, branch `claude/zen-cannon-v75uxs`). These fixes were therefore released as 0.15.0, and 0.15.0 also contains that 0.14.0. Its migration keeps its number (`0059_blog`); the ten migrations of these fixes follow it as `0060`-`0069`.*

### Summary

0.15.0 is the fix release this analysis asked for. It covers phase 0 of section 6 in full, the FIX NOW rows of phases 1 and 2, and phase 3's rows 9, 10, 11 and 21 (the loopholes in 0.6), but not phase 3's re-keying, new rules or scorecard. It also fixes the eleven faults in your live diagnostics of 2026-10-01 (LIVE 1-11).

- **Findings:** 78 in all: FIX NOW rows 1-25, row 26's 14 small defects, 28 other findings (X1-X28) and LIVE 1-11.
  - **75 fixed.**
  - **3 partial:** row 21 (email-reply unlocks aren't scoped to a thread), row 26m (no field for a register entry) and LIVE 7 (some numbers near "pin" are still masked).
  - **0 not fixed** of those listed. What was left out on purpose is under "Partial and not done".
- **Tests:** 1,479 → **2,121**, all passing. `ruff check` is clean.
- **Size (before these notes):** 64 commits, 158 files, about +17,600 / −2,300 lines, 10 migrations (`0060`-`0069`, after 0.14.0's `0059_blog`). They run on their own at the upgrade.
- **New options:** 3: `wake_on_events`, `printify_buyer_pays_shipping` and `printify_bill_vat`. Row 11 asked for `wake_on_events`. The two Printify options state what the margin check can't know (who pays the shipping, whether Printify's bill carries VAT). Section 6's freeze asked for no other options, and none were added.
- **What the money guard now promises.** A workshop run is held at its cap per run, at least, against the daily cap and the balance. A run can still cost more than anything seen before (the code-run output and the size of a search result have no bound). Ember then books the real cost, makes no more workshop runs in that cycle and raises its estimates. DOCS says this plainly.

### Fix log: FIX NOW rows 1-26 (section 5)

| # | What was wrong | Status | What 0.15.0 does |
|---|---|---|---|
| 1 | Workshop calls had no provable ceiling; the caps held only up to a guess | ✔ fixed | A workshop call holds the largest of: its raised worst case, the cap per run, and 1.5 × the p95 of what recent runs are known to cost (last 20 runs, 14 days, since your last Reset). That amount must fit the daily cap and the balance. The quote prices 10 rounds of up to 3,000 tokens (`WORKSHOP_MAX_TOKENS` 8,000 → 3,000). Token counting works for requests with files. Each call's rounds and overruns are stored (`llm_calls.iterations`, `.overrun`) and shown in the diagnostics. Research is priced per round too; its ceiling stays empirical. Default `workshop_run_cap_usd` 0.50 → 0.75 |
| 2 | Any overrun stopped the cycle, skipped the reflection, called the plan done and shortened the sleep | ✔ fixed | Only a call that counts toward the cycle cap and went over by more than 10% or $0.02 stops the cycle. Otherwise that kind of call is refused for the rest of the cycle and the plan goes on. The reflection always runs. A stopped cycle's digest and journal say where it stopped. The next wake follows the chosen sleep, as after a completed cycle |
| 3 | Safe mode forgot `owner_user_ids` | ✔ fixed | Safe mode keeps your owner IDs. If it can't read them, Ember answers no one: each visitor sees a page naming safe mode and their user ID |
| 4 | Safe mode lifted an engaged kill switch | ✔ fixed | A start with invalid options never lifts a kill. A new reset applies at the first start with valid options |
| 5 | 13 options bounded only in `config.py`, so Home Assistant accepted values that forced safe mode | ✔ fixed | Every bound is in `config.yaml` too, and a test checks the two agree. Options that don't fit together (e.g. the sleep pair, a cycle cap above the daily cap) are corrected toward spending less, with an error event and a dashboard banner, not safe mode. A required text of spaces only takes its default |
| 6 | Pictures over 12 MP refused with a misleading message; the poster couldn't be proposed | ✔ fixed | One 40 MP limit everywhere, the same as the workshop's check. A refusal gives "W × H = N MP, more than 40 MP". Large pictures are decoded at reduced size for thumbnails |
| 7 | CI red on both releases (secret-scan false positive) | ✔ fixed | The test's fake is allowlisted, and so are the analysis branch's false positives. A test-order flake in the Tests job is fixed (secrets are now redacted longest first). README's release steps require green CI on the exact commit and a `vX.Y.Z` tag. *Not yet seen green on GitHub: this branch hasn't been pushed* |
| 8 | Code milestones filled the agent's 16 and your 4 places | ✔ fixed | Only the agent's and your milestones count. A product line's test holds one open bar at a time (day 14: 30 views first, then 2 favorites). A bar that opens on or after its day is due the next day. *Done as one bar at a time, not as the single "listing test" milestone the row proposed* |
| 9 | Kill switch kept unlocks; take-back left approved actions to run; unlocks without owner identity; no QA; the email footer claimed your approval | ✔ fixed | (a) An unlock's email says it was "sent under rules they set, without their review of this email". (b) Taking back an unlock sends what it approved and nothing began back to you; cancelling such a request counts as a veto. (c) The kill switch takes back every unlock, so its reset approves nothing. (d) Unlocks act only while `owner_user_ids` names you and the app isn't in safe mode; otherwise Ember's code takes them all back. (e) An unlock approves only what passes its QA. The upgrade took back every 0.13.0 unlock once (there were none live) |
| 10 | The unauthenticated `From:` counted as "wrote"; bounces, auto-replies and Ember's own address woke cycles or became obligations | ✔ fixed | Each email stores your provider's Authentication-Results verdict (the topmost header). Only verified mail that isn't from a list, a machine or Ember's own address counts. One predicate serves inquiries, metrics, wakes and NEVER's first-contact rule. Mail from before 0.15.0 doesn't count. *Limit: the header's authserv-id isn't checked* |
| 11 | Event wakes bypassed your settings and the guards | ✔ fixed | The no-room, crash-loop and back-off guards run first. New option `wake_on_events` (on by default). Orders and the last day of a milestone Ember's code grades no longer wake, also for rows noted before the upgrade |
| 12 | The product factory couldn't do the workshop's paid jobs; `make_image` 4 a cycle; QA counted files | ✔ fixed | A picture of every sheet; `file.xlsx#2` or `#Name`; a free copy (`workspace_write` mode copy); `workspace_read` of PDF, Word and Excel (cells with formula results); 10 zoom regions; text-only photos; a typographic poster at print size (6000 px). `make_image` runs 10 times a cycle. QA counts distinct photos: the same page under another name, a copy, or a near-identical picture (difference hash) counts once, and the card names repeats |
| 13 | Fixed section budgets cut the review's advice, the first test, the venture's knock-outs and a missed bar's action | ✔ fixed | Two-pass budgets: unused room goes to cut sections by priority, within the same total. The review's focus, lesson and advice come first. FOCUS puts knock-outs and the critic's verdict and flaw first (its budget 1,900 → 2,400 bytes). A missed bar's obligation names the action first. *When every section is full, ROADMAP can still cut a first test: no package reordered ROADMAP itself* |
| 14 | A work step's journal skipped the reflection | ✔ fixed | `write_journal` is reflect-only. Every cycle that worked reflects again (one reflect call each) |
| 15 | Library texts in the shareable report; name masking garbled words and addresses; the sensor could name a person | ✔ fixed | Study replies, `library_read` and `knowledge_search` show only their length. Names and addresses are masked in one pass, and only person-like names. The sensor carries `digest_day` and counts, not the text. A revocation reason names no address. Subjects, opt-out words and your Home Assistant name and ID are masked too |
| 16 | Printify: loose fee model, lopsided P&L, dropped shipping, assumed currency, unclear publishes lost, listings invisible to metrics, no price before proposing | ✔ fixed | (a) The margin check uses `econ`'s fee model with VAT, plus two options (who pays shipping, whether Printify bills VAT). (b) The buyer's shipping is revenue. (c) Printify's costs go to the product's project and venture, recorded automatically when automatic revenue is on. (d) The currency Printify states is read and converted. (e) Unclear publishes are taken up by the sync, or failed after 24 hours with an Undo. (f) Printify's listings count in the metrics, bars and stage rules, and lines without a venture (Nebenkosten #7) link to the Etsy leg. (g) A free cost probe (an unpublished product, deleted at once) gives each variant's making cost and least price |
| 17 | Etsy fees under-booked 15-29%; a refund could be held back; a partial refund of your order not flagged | ✔ fixed | Fees use `econ`'s table (the listing fee at each sale, 6.5%, processing, 19% VAT on fees). Listing fees are booked automatically at the next sync. A refund is always recorded; if it leaves the live agent without money, Ember's code pauses it with that reason. A partly refunded order you recorded asks you to correct its entry. *Orders read before the upgrade keep the old, lower fee figure* |
| 18 | Venture gates could be gamed | ✔ fixed | (a) A Library source counts only if it is linked to the line or its venture, or is a .csv/.tsv upload, and the note cites a number from it. (b) Vendors are matched on any domain, and `vendor_only` needs an independent demand number. (c) Proposals from before the gates go back to researching, and your Back on one without numbers needs "Back it anyway". (d) No venture starts live (tool and trigger). (e) Research for the focus venture counts toward its budget. *`vendor_only` still trusts the agent's label for a claim's metric* |
| 19 | The expected net ignored the months before the first sale; "slow" counted whole months | ✔ fixed | `ev = ((6 − m) × expected − m × fixed − setup) / 6`, with the first sale in days (14 to 730). "Slow" compares those days with half the net runway. Stored cases were recalculated |
| 20 | Roadmap integrity: re-linking, drops, voided forecasts, self-closed first tests, late grades | ✔ fixed | Links of code and metric milestones are frozen (tool and trigger). Your drop of a bar ends that line's test. The agent's drop of a forecast milestone settles as a miss. A first test closes only by code or your drop. Grades use the last reading by the due day. *Still open: `project_update` can move a project to another venture* |
| 21 | Unlocks keyed to the plan's focus; NEVER words too weak and too broad; the price band compounded; a veto window approved after done; Undo dead ends | ◐ partial | (a) ◐ An unlock carries a request only if its milestone covers what it acts on (listing → project → venture), and you can only unlock rules a milestone can cover. Email replies aren't scoped to a thread. (b) A closed milestone, even one met, ends its unlocks. (c) NEVER reads only the act (an email's subject and text), after Unicode and look-alike folding, with many more tax, invoice, offer and contract words; listing copy no longer trips it. (d) The 15% band is measured from the price you last approved. (e) Before a price change or photo fix, Ember's code re-reads Etsy (photo ids), and a difference goes to you. (f) Undo works in chains, while paused or unfunded and after a crash; it is refused while killed |
| 22 | The workshop PDF check could be bypassed | ✔ fixed | Indirect or repeated `/Filter` and `/DecodeParms` are refused. Streams start after CR, LF or CRLF and must decode to their end. Predictors are undone. pdfium is asked last. The check fails closed |
| 23 | Opt-outs missed common phrasings; a refused IMAP fetch lost the email; a failing mailbox was retried every 15 minutes | ✔ fixed | Short stop lines after greetings and many English and German phrasings count. A refused fetch is asked for again (skipped with a warning after 5). A failing mailbox backs off up to 4 hours, also at the start of a cycle, with one warning per error |
| 24 | Inbox showed the newest 30; WAITING the newest 20; the agent read 2 KB of each release's notes | ✔ fixed | Inbox pages with "Show older messages", and your unanswered messages always show. WAITING lists every pending request. Release notes are read in 2 KB parts, one per plan, until all are read |
| 25 | Wake now bypassed the last will's back-off | ✔ fixed | Every cycle clears the Wake now request when it starts. After a refused or failed cycle the scheduler waits a round |
| 26a | The event reserve's 20:00 was off by an hour on DST days | ✔ fixed | 20:00 and the 08:00 milestone check follow your local clock |
| 26b | 8-10 `count_tokens` requests per step | ✔ fixed | Counts are cached: 2 per work step |
| 26c | The diagnostics preview ran keepers and marked agenda events as woken | ✔ fixed | Making a report only reads |
| 26d | Cycles killed by a restart got no digest | ✔ fixed | They get one at the next start |
| 26e | The constitution said you carry out everything but email | ✔ fixed | It names what Ember's code carries out. The rule audit checks that the two old claims don't return |
| 26f | An order with many lines failed a CHECK and stopped every sync | ✔ fixed | Lines are stored within bounds, and one bad order is skipped and named |
| 26g | Orders fetched only for the last 30 days | ✔ fixed | Back to the last good sync, or to the oldest order without fees, up to a year |
| 26h | Ledger paging gap | ✔ fixed | The dashboard joins pages only where they meet |
| 26i | `.docx` zip bombs | ✔ fixed | Office files are checked before unpacking (entries, total size, ratio, no duplicate parts). *A 50 MB-XML file still costs a lot of memory to read* |
| 26j | 10-second upload timeout | ✔ fixed | Grows with the file, up to 3 minutes; on a timeout the list is reloaded |
| 26k | Pinterest refresh lifetime assumed | ✔ fixed | Read from Pinterest's answer (60 days if missing), renewed in the 6-hourly sync |
| 26l | A pin's listing not re-checked | ✔ fixed | A pin whose listing is no longer live or Ember's isn't sent and doesn't use the day's quota |
| 26m | The Impressum could be email-only | ◐ partial | Your name and a postal address (street with number, then postcode and town; no PO box) are required. A missing phone is advised, not required. No field for a register entry |
| 26n | The NEVER agreement test missed the new executors | ✔ fixed | It covers every executor. Code and view already agreed: this closed a test gap, not a live bug |

### Other findings fixed (sections 0, 3, 4, 8 and the raw reviews)

| # | Where | What was wrong | What 0.15.0 does |
|---|---|---|---|
| X1 | 3(a) | Maintenance's $0.40 and the 20:00 event reserve didn't bind the workshop, review or study | In maintenance every call counts toward $0.40 and there is no workshop tool. Until 20:00 every call of a scheduled cycle leaves the event reserve |
| X2 | 3(a), 4 | Critic and consolidation counted toward the cycle cap | They count toward the daily cap only (one shared list of purposes outside the cycle cap) |
| X3 | 3(a) | STATUS promised the wrong cap; brainstorm offered in focus | STATUS states the cap in force and why. Brainstorm is offered only in explore |
| X4 | 3(a) | The safety factor capped at 4 and couldn't decay | It caps at 8, halves its excess after 3 accurate calls and every 7 days. Reset also clears the workshop's run history |
| X5 | 3(c) | A draft append and a deep workshop folder were refused after payment | Both are refused before the call. A paid draft that doesn't fit goes to a new file |
| X6 | 3(d) | A stopped cycle lost the handoff and your comment | The last written handoff is shown. Your comments stay until a cycle that saw them ends normally |
| X7 | 3(e) | Each of your clicks started a full cycle | Your messages and decisions wake one cycle about 5 minutes after the last one, at most every 30 minutes. Wake now stays immediate |
| X8 | 4 | Listings missing from Etsy's batch answer stayed live forever | They are read one by one and marked removed, expired or unknown |
| X9 | 4 | One call's fields could exceed a reply | A call whose texts total over 3,000 characters is refused with the numbers |
| X10 | 4 | A parked venture's projects kept their bars | Your park or kill, or Ember's code's, drops them. The agent's own park doesn't |
| X11 | 4 | The first digest said "carried out nothing" | Digests count actions from before the journal and fill missed days (up to 14). The old one stays as written |
| X12 | 4 | Maintenance retried 30 minutes after a failed cycle | One scheduled cycle a day after any cycle |
| X13 | 4 | The sleep cut while a request waits was 60 minutes, not 240 | The chosen sleep is cut to max(min sleep, default sleep, 240 minutes) |
| X14 | 4 | The POD venture's first test #8 was still prose | A migration replaces it with "at least 1 Printify order", same dates, checked by code |
| X15 | 8 | The prompt bound was checked only with channels off | Checked with every channel on. The fixed prompt fell from 51.7 KB to about 45 KB |
| X16 | 8 | Model calls, tool calls and call texts grew without limit | After 30 days, old inputs and results are pruned. Costs and tokens are kept, nothing is deleted. The file stops growing |
| X17 | 3(b) | `observations` wasn't in the diagnostics | Counts by subject and metric, never values |
| X18 | 3(b) | The fake model couldn't overrun or report rounds | It reports `iterations` and can overrun like #423 |
| X19 | 6 (phase 0) | No projected date for the next burn-mode change | STATUS and the dashboard's runway line show it when it is within 30 days |
| X20 | raw | Promotions counted approvals you changed or commented on | Only clean approvals count. A rule Ember's code took back isn't suggested again for 30 days |
| X21 | raw | A channel switched on but not set up was invisible to the agent; its first test ran anyway | The plan says what setup is missing. The first test starts only once the channel is ready; venture #2's milestone #14 is dropped |
| X22 | raw | A POD product's discard note claimed a deletion that failed | The card says it failed and names the product to delete |
| X23 | raw | A first sale recorded late missed its forecast | First-sale forecasts settle 7 days after their date |
| X24 | raw | The agent's views and favorites goals were self-reported | It can set those metrics, and a prose goal naming such a number is refused |
| X25 | raw | The hidden-text filter saw only inline styles | It reads style sheets, more colour forms, faint text, tiny fonts and off-screen offsets (best effort) |
| X26 | raw | The cold-outreach knock-out missed negations and German | It reads negations, German phrasings and "reach out to" |
| X27 | raw | Library study kept paying after 60 learnings | It ends there and names the unstudied parts |
| X28 | raw | The desk's "decided" counted Ember's code's automatic parks | Only the agent's and your decisions count |

### LIVE 1-11 (your live report of 2026-10-01)

| # | What was wrong | Status | What 0.15.0 does |
|---|---|---|---|
| LIVE 1 | `site_url` ends in `/index.html`, so every page's address became `.../index.html/<page>.html` and search engines couldn't index the site | ✔ fixed | A trailing `index.html` or `index.htm` (any case) is dropped from the address. Any other file name in `site_url` is refused |
| LIVE 2 | Workshop run #7 ($0.89) showed "ok" though only its script came back | ✔ fixed | A run is ok only if a real file came back. The result names the missing files and where to save them |
| LIVE 3 | PRINTIFY claimed a request waited when none did | ✔ fixed | It counts approved-not-created, closed and waiting proposals |
| LIVE 4 | A 5xx before the answer began was charged at the worst case ($0.15, call #293) | ✔ fixed | 500, 502, 503 and 504 before the stream opens cost nothing, like 529 |
| LIVE 5 | The waiting-request sleep cut stayed after you decided; events said "next sleep" | ✔ fixed | After you decide the last waiting request, the agent's chosen sleep stands. Events say "chose N min of sleep" |
| LIVE 6 | Too-long fields were refused without numbers; upgrade requests and venture notes were lost | ✔ fixed | Refusals give length and limit and a remedy. Upgrade requests and venture notes are cut instead |
| LIVE 7 | The one-time-code mask hid ordinary numbers in the report's tables | ◐ partial | A code word no longer reaches across a table cell. A year near "pin" in a sentence is still masked, on purpose |
| LIVE 8 | The agent couldn't see its open upgrade requests and paid to test one | ✔ fixed | WAITING lists upgrade requests that are new or accepted |
| LIVE 9 | A kept promise couldn't be closed once `message_owner`'s 2 a day were used | ✔ fixed | One message per promise that reports it kept gets past the limit |
| LIVE 10 | `workspace_read` of a folder gave a misleading list | ✔ fixed | It says the path is a folder: use `workspace_list` |
| LIVE 11 | Opt-outs said "replied" for senders Ember never emailed | ✔ fixed | "replied" only when the mail answers one of Ember's emails, otherwise "wrote" |

### Partial and not done

**Partial (3):**
- **Row 21(a), email-reply unlocks.** A reply can't be tied to a product line, so an `email_reply` unlock sits on a milestone with no project and no venture. It then covers replies in every thread someone else started, not one conversation. Scoping by thread needs a link from milestones to threads, which doesn't exist.
- **Row 26m, the Impressum.** A register entry (Handelsregister) would need a new option, which the freeze didn't allow: add it to the downloaded `impressum.html` by hand. A missing phone is warned about (on the Website card and in the agent's plan), not refused.
- **LIVE 7, the code mask.** Exempting years or "pin + noun" would let a real 4-digit PIN through to a report you may share. The trade-off goes the other way too: a code whose code word sits in the neighbouring table cell is no longer masked. DOCS says so.

**Deliberately not in 0.15.0** (section 6's later phases and section 4's open items):
- **Phase 1:** the cheap "reply" purpose that answers your message without a full cycle. Your messages, rejections and venture decisions as agenda items. Coalescing your clicks (X7) is done.
- **Phase 3:**
  - re-keying unlocks per venture or product line and action class, with a daily limit, an action budget and an end date (21a keys them to what a milestone covers, nothing more);
  - the `listing_edit`, `pod_product`-after-the-first and pins-after-the-first-board rules;
  - one-click promotions on the approval card;
  - the autonomy scorecard.
- **Phase 4:**
  - required photo kinds (the distinct-photo check of row 12 is done);
  - the vision check;
  - your approval comments as obligations;
  - a ring-fenced first-test budget that survives maintenance.
- **Section 8:** captured requests, evals, the live canary and a LIVE_FINDINGS log. The fake model is more realistic (X18).
- **Still open from 0.11.1:**
  - the score (a compensatory sum);
  - one search per research call;
  - batch approve;
  - Etsy reviews;
  - metrics on your own milestones;
  - the money goal fixed at 90 days;
  - the default cycle's margin;
  - uncertain 5xx charges after the answer began (still reconciled by hand);
  - packaging (prebuilt images, hash-pinned installs).
- **Email-reply unlock scoping by thread** (see above).
- **Smaller leftovers named in the reports:**
  - The obligation and `qa_clean` count the same file once, but not the same page under another name.
  - A `.docx` with 50 MB of XML is bounded but slow to read.
  - The daily review's verdicts on milestones not on its card still go through (each through the same refusals).
  - Not checked against the live APIs: Printify's acceptance of the cost probe (a blank picture, a price of 999.00); whether Etsy's batch answer leaves out inactive listings; why counting tokens failed for requests with files.
  - The 3,000-token round limit is a trade-off. If workshop runs end with stop reason `max_tokens` (the diagnostics show it), the limit and the cap per run must rise together.

### What you must do

1. **Website:** set `site_url` to `https://ember-ai.de` (the old value with `/index.html` also works now). Then download the site again and upload it to your host: the pages built until now point at the wrong addresses.
2. **Phone:** set `site_phone`. The site has no contact form, so the Website card shows an "Advice" line until you do. If you have a Wirtschafts-Identifikationsnummer, enter it in `site_vat_id` (DE123456789-00001).
3. **Settings:** section 7 still applies, with two notes:
   - `workshop_run_cap_usd`: the default changed from 0.50 to 0.75. A workshop run now holds at least its cap per run against the day. A Sonnet 5 run is priced at about $0.60, so section 7's 0.60 is the lowest value at which the workshop still runs; under 0.60 it can't.
   - If one past run cost more than your daily cap allows for, the dashboard warns you and names the day it stops counting (at most 14 days). Reset estimates clears it at once. Section 9's advice stands: don't reset to make a run fit.
4. **`min_sleep_minutes` / `wake_interval_minutes`:** a mismatched pair no longer starts safe mode. Ember corrects it toward spending less and shows a banner. Still change the two together (180 / 240).
5. **New option `wake_on_events`** ("Wake Ember for events", on by default). Switch it off if you want Ember only on its own schedule.
6. **Printify options:** `printify_buyer_pays_shipping` (off) and `printify_bill_vat` (on). Switch the first on only if your Etsy shipping profile charges buyers for shipping. Switch the second off only if you can reclaim the VAT on Printify's bill: check with a Steuerberater (not tax advice).
7. **After the upgrade:** on the Upgrades tab, mark upgrade request **#3** (the poster refused as "not one of your pictures", fixed by row 6) as **released**. Set project **#8** (POD posters) back to **active**: it waits for #3, and the agent's own lesson says not to retest until you confirm the fix.
8. **Check the dashboard banners** after the upgrade and after any options change: safe mode, "Options corrected", the workshop hold warning, "Unlocks are off while ...". If Home Assistant refuses to start the app with "invalid options", a saved value breaks a new limit (e.g. extra spaces, an `http://` address). Fix it there.
9. **`owner_user_ids`** must hold your user ID before you unlock anything; without it unlocks are off. Check that your mail provider adds an Authentication-Results header to incoming mail (most do): without it, Ember would trust a header the sender wrote.
10. **Unlocks:** the loopholes of 0.6 are closed. Section 6's advice still holds: don't unlock before phase 3. Don't unlock email replies at all yet: they would cover every thread, and contacts from before the upgrade count as first contacts again until they write.
11. **Home Assistant sensor:** if an automation reads the sensor's `digest` attribute, switch it to `digest_day` or the counts (`digest_actions`, `digest_failed`, `digest_taken_back`).
12. **Old figures:** orders Ember read before the upgrade keep the old, lower Etsy fee figure; check them against your Etsy payment account. The warning about call #293 is from before LIVE 4: correct its cost on the dashboard if you want it gone.
13. **Release it as README now says:** push to a candidate branch, wait for every CI job to be green on that commit, then move the tracked branch and tag `v0.15.0`. Then keep the 72-hour hold (section 8), and measure phase 0's watch items before the next behaviour release.

### Behaviour changes to know

- **Maintenance** (under 15 days of net runway; section 0.4 expected it around 10-03) means no workshop runs, every call within the cycle's $0.40, and one scheduled cycle a day, after a failed or stopped one too.
- **The 20:00 event reserve** now binds workshop runs, reviews and studies in scheduled cycles.
- **Research quotes about double** (priced per round), so research is refused more often near the cycle cap or a venture's research budget.
- **Every cycle that worked reflects again:** one reflect call more per cycle, lessons and strategy updated again.
- **Unlocks are off** without `owner_user_ids` or in safe mode. They end when their milestone closes, even when it is met. The kill switch takes them all back, and Undo is refused while killed.
- **Stricter NEVER words mean more replies wait for you.** "offer", "quote", a UID number and invoice compounds (Schlussrechnung, and also Umrechnung; not Berechnung) in a reply make it wait. Listing copy ("Mietvertrag", "Steuerberatung") no longer blocks a listing change.
- **Ventures can't start live.** Dropshipping #3 goes back to researching (your research request stays). Backing a venture without numbers, or with a standing knock-out, needs "Back it anyway".
- **Below 28 days of net runway every venture case is knocked out as "slow"** (on 10-01 it was about 16 days) until you lift it for that venture. Stored expected nets were recalculated and may drop.
- **Code milestones don't take your places.** The money goal, decision points, first tests and bars no longer use your 4 places or the agent's 16. Each product line holds one open bar at a time. On upgrade, a line can still hold both open day-14 bars from 0.13.0.
- **The agent can't close a first test "done".** You confirm a prose test by dropping it on the Roadmap tab, after the agent sends you the evidence.
- **Your clicks wake one cycle**, a few minutes after your last one and at most every 30 minutes. While a request waits, Ember sleeps at most 4 hours (not 60 minutes). After you decide the last one, its own chosen sleep stands, so it may see your decision later. Orders and favorites no longer wake it.
- **Mail:** contacts who wrote before 0.15.0 have no verdict, so an email to them shows the first-contact warning until they write again. Machine mail no longer wakes Ember or becomes an obligation.
- **Etsy money:** listing fees are booked automatically: don't record them yourself. A refund that leaves the live agent without money pauses it with a reason. You can grant funds and resume, or resume without funds.
- **Printify:** you may see a blank `ember-cost-probe.png` in your Printify media library. Delete any product named "Ember cost probe (not for sale)". The Pinterest venture's (#2) first test, milestone #14, is dropped automatically; a fresh 21-day test starts once you finish the Pinterest setup.
- **Privacy:** the shareable diagnostics hide Library texts. After 30 days, old tool inputs, results and model replies are pruned in the database (costs and tokens stay).
