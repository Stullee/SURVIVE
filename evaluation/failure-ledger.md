# Ember's failure ledger

Each class of failure: when it was first seen, each time since, its cause (Ember, Ember's code, the owner,
measurement), the release or rule meant to fix it, and whether it came back after that fix. Its number is
**recurrence after a fix** (target 0). Kept by the weekly evaluation (`vision/evaluation.md`, "What we learn from it").
Started 2026-10-22 with the first evaluation (`evaluation/2026-10-22-0.44.0/`). Dates before 10-12 come from the
analyses (`analysis-*/`) and are as they report them.

**Recurrence after a fix, as of the week 10-12 to 10-18: 3 classes** (F1, F2, F8).

| # | Class | Cause | First seen | Seen since | Meant to fix it | Came back after the fix? | Status |
|---|---|---|---|---|---|---|---|
| F1 | The plan and the tools keep two copies of a rule: the code picks a step her rules, her review or her owner had stopped | code | before 10-14 (analysis 0.40.1, 5.2: KDP's week, the pins and posts limits, the sales limit) | 10-12 to 10-15: critic steps on listings with 0 to 4 views against her strategy; 10-13 and 10-14: a brainstorm and six triage steps in the owner's "nothing new" week; 10-16: 9 of 12 cycles on Bluesky and blog steps her weekly look had stopped; 10-16 and 10-17: pin steps after her review stopped the daily pins | 0.39.6 (critic steps wait for views; review changes become steps); 0.39.8 (channels under their floor wait; `skip`); 0.42.6 ("ventures under invest") | **yes**: after 0.39.6 (10-16), after 0.39.8 (the pins of 10-17) | open |
| F2 | A kind of ready step cuts every sleep she chose to 30 minutes | code | 10-09 | 10-12 to 10-18: 69 of 80 chosen sleeps cut (all 55 up to 0.39.2; 12 of 14 on 0.39.6; 2 of 11 on 0.39.8); 10-21 (the vision's list) | 0.39.6, 0.39.8, 0.40.2 | **yes**: after 0.39.6 and after 0.39.8 | open |
| F3 | A text over a tool's limit | both | by 10-14 (`venture_update` texts cut in 10 of 12 cycles) | 10-12 to 10-18: 32 refusals; 10-21: 6 of 11 refusals | none yet; she writes a tool-limit lesson each time | – | open |
| F4 | Brainstorm, park everything at birth, brainstorm again | both | by 10-09 (the weekly look: 24 venture cycles, 14 ideas killed) | 10-13 and 10-14: 12 ideas in two brainstorms, 11 parked, none researched (V2 0%); 10-21 | 0.39.6 (a marketplace where buyers search is a test of its own); 0.42.6 | not yet judged (the fixes came after the week) | open |
| F5 | A belief about her own records gone stale | Ember | 10-21 (the English book, the ads' "0 views", the KDP week) | 10-12 to 10-18: 6 of 24 claims (rejections "4" for 5, pins "about 32" for 21, the blog line "$2.56" for $4.57, "mostly planning", renewal fees, the message cap's day) | the corrections of `feedback.json` 2026-10-22; exam questions | – | open |
| F6 | Polishing products nobody sees (covers and files of listings under 10 views) | code | 10-12 (earlier weeks not checked) | 10-12 to 10-15: 22 cycles, $9.92 (27% of the week); the job-tracker cover three times at 1 view | 0.39.6 (the critic's steps wait for 10 views) | **no**: no cover cycle after 0.39.6 in the week | watch |
| F7 | Calls billed at their worst case when the API's usage has a field the code doesn't know | code | 10-15 | 10-15 and 10-16: 180 calls, $13.91 overbooked | 0.39.8 (knows `usage.speed`, settled the calls) | **no** | closed |
| F8 | A KDP book resubmitted without checking the defect the owner named on fresh pages of that very file | Ember | 10-08 (#77) | #81, #84, #85 (10-09, 10-10); #109 (10-13); #120 (10-14) | cases #10, #21, #24, #26, #36; principles #5–#7, #10, #12, #14; 0.40.0 (the page check is built into `propose_kdp_book`) | **yes** after the cases (#109, #120); **no** after 0.40.0 (#140 approved at the first try, 10-19) | watch |
| F9 | The owner's exact spec restated, then changed without saying so | Ember | 10-14 (#191 → v15: 14 rows for 11, no page breaks, "zero warnings" with 67 pages flagged) | – | `feedback.json` 2026-10-22; an exam question | – | open |
| F10 | Book work cut by the conversation's size limit | code | 10-12 (earlier weeks not checked) | 10-12 to 10-18: 9 of 16 KDP cycles | none yet | – | open |
| F11 | Public texts that say a person made her work, or promise legal certainty | Ember | 10-12 (#102; earlier weeks not checked) | 10-12 to 10-17: 11 texts claim human authorship (8 pins, #124, #125, the KDP book's description, then #140 and #141 on 10-19 and 10-20); 2 pins say "rechtssicher" or "§ 556 BGB konform" | proposed: a rulebook rule, the code's disclosure lines and request checks, her constitution (evaluation 2026-10-22, A1–A3, C1, T1–T2) | – | open; **the incident of the week 10-12 to 10-18** |
| F12 | One decision per cycle where one rule decides several | both | 10-14 (#269 to #274: six ideas, six cycles, $1.04) | – | proposed: a triage step takes all waiting ideas of a kind (C4) | – | open |

## Changes

- 2026-10-22: started, from the first evaluation (week 10-12 to 10-18). F1–F5 come from the vision's list; F6–F12
  are new.
