# Ember 0.13.0 analysis: paused work (2026-09-30)

The second full codebase analysis (after the 0.11.1 one) was paused at the owner's request because the session's usage limit was close. This folder holds everything that had finished, so the work can continue without redoing it.

## What is done

- **Baseline:**
  - `ruff check` is clean.
  - The full test suite passes: 1,479 tests on Python 3.12 in 15 minutes.
- **Size of the change since 0.11.1** (`4f31ccc`, 2026-09-29 17:22 UTC → `ab713b0`, 2026-09-30 13:59 UTC, about 21 hours):
  - 77 commits;
  - app Python 26.4k → 43.7k lines (+66%);
  - tests 14.5k → 25.4k lines;
  - migrations 14 → 58.
- **Subsystem reviews (10 of 12)** are in `raw/review-*.json`: economy, loop, tools, roadmap, ventures, autonomy, etsy, channels, web, mail. Each lists findings (severity, files, evidence, failure scenario, label, fix), checks of the patch notes' claims (`fix_checks`) and strengths.
- **Adversarial verification** covers only three batches: `raw/verify-economy-1.json`, `raw/verify-economy-2.json` and `raw/verify-ventures-1.json`. All 14 verdicts there are "confirmed".
- **Live analysis:** `raw/live-ops.json` (cycles, costs, overruns, owner actions, which features engaged) and `raw/live-behaviour.json` (decision quality).
- **Strategy reviews:** `raw/strategy-autonomy.json` and `raw/strategy-economics.json`.

## What is missing

1. Reviews of **db** (migrations and triggers) and **tests** (test suite, fake model, CI, process).
2. **Verification** of the unverified findings: loop, tools, roadmap, ventures (the second half), autonomy, etsy, channels, web, mail. A high-severity finding also gets a second, independent skeptic.
3. The **complexity and process** strategy review.
4. **Synthesis:** the report `ember-analysis-0.13.0.md`, in the same structure as the 0.11.1 one (verdict, what's going right, root causes, FIX NOW ranked, BUILD NEXT, business, process, what not to do), plus a final accuracy check.

## How to resume

- **In the same Claude Code session** (if it is still available), each workflow resumes with its completed agents cached:
  - `Workflow({scriptPath: <ember-review script>, resumeFromRunId: "wf_d4022367-5ae", args: {areas: ["economy","loop","tools","roadmap"]}})`
  - the same for `wf_cff6dc19-a37` (`ventures, autonomy, etsy, channels`) and `wf_c4cc603d-035` (`mail, web, db, tests`);
  - the live and strategy script with `wf_13f0cac0-950`.
- **In a new session:** give it this folder and the three inputs (the 0.11.1 analysis and the two diagnostics reports of 2026-09-30). Then run only what is missing above, using the `raw/*.json` files as input.

## Headline findings so far (unverified unless noted)

- **Money safety (high, confirmed):** a workshop run's "worst case" is not an upper bound.
  - Live, call #423 cost $1.84 against a $0.35 estimate. It broke the $1.00 cycle cap and the $1.50 workshop cap, and made cycle #44 cost $2.00.
  - Code-execution calls loop on Anthropic's side with no provable ceiling.
  - Any overrun, even 0.7% (#444), stops the whole cycle and skips its reflection.
- **Owner authority (high, reproduced):**
  - Safe mode forgets `owner_user_ids`, so any option error opens the dashboard and its controls to every Home Assistant user.
  - A safe-mode start can lift an engaged kill switch.
- **Roadmap (high, reproduced):** milestones set by Ember's code (listing-test bars, money goal, decision points) use up the agent's 16 places and the owner's 4 reserved places.
- **Printify (high, reproduced):** pictures over 12 MP, which means every print-resolution poster, are refused by `propose_printify_product`, so the backed POD venture can't publish.
- **Privacy (high, reproduced):**
  - The shareable diagnostics report carries the owner's library texts.
  - Masking senders' display names overwrites ordinary words ("Pinterest", "Fiverr", the mail domain).
- **Autonomy (medium, reproduced), none unlocked live yet:**
  - The unlocks have loopholes: a spoofed From address counts as "someone who wrote", unlocks approve requests that fail their own QA, and the ±15% price band compounds.
  - Take-back and the kill switch don't stop everything already approved.
- **Economics (strategy review):** burn rose to about $4.89 a day (7-day window), against $3.76 at 0.11.1 and a $1.90 target. The runway is 18.3 days, and nothing has sold yet.
