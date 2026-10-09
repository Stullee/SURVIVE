# Reproductions for `ember-codebase-0.37.0.md`

Scripts against Ember 0.37.0's own code (`9926457`). Each builds its state in a fresh data folder, with the app's
dry-run fakes and the repository's test helpers; nothing reaches the network and nothing in the repository changes.
Outputs that aren't printed go under `/tmp/ember-repro`.

## Running them

From `ember/`, with `requirements-dev.txt` installed:

```bash
R=../analysis-0.37.0/repro
# most scripts
EMBER_DATA_DIR=$(mktemp -d) EMBER_SCHEDULER=off EMBER_FAKE_DELAY_MS=0 PYTHONPATH=.:$R/<folder> python $R/<folder>/<script>.py
# the money scripts
sh $R/economy/run.sh $R/economy/<script>.py
# the tools folder's rt_test_*.py files are pytest files
EMBER_SCHEDULER=off PYTHONPATH=.:$R/tools python -m pytest -p no:cacheprovider -p tests.conftest -q -s $R/tools/rt_test_workspace.py
# a simulated week (section 6): data folder, days, options (default or live), owner (active or passive)
PYTHONPATH=. python $R/e2e/sim.py /tmp/ember-week 7 live active
# 3.1 with no model at all: a data folder
PYTHONPATH=. python $R/e2e/stuck_step.py $(mktemp -d)
```

Some products scripts recalculate workbooks with LibreOffice (`soffice`) when it is installed.

## Which script shows what

| Finding | Script |
|---|---|
| 3.1 A step a cycle can't advance takes every cycle | `e2e/stuck_step.py`; `plan/repro_promise_floor.py`, `plan/sim_mix.py`, `plan/repro_owner_pin_promise.py`; `e2e/sim.py … 7 live active` (run B) |
| 3.2 A product stalls after its request ends | `plan/repro_stalled_release.py` |
| 3.3 The seeded Etsy venture caps Etsy research | `ventures/r8_etsy_leg.py`, `ventures/r8e_run.py` |
| 3.4 The workshop near the bottom of the balance | `economy/e13_hold_clamp_5x.py` |
| 3.5 Whole-column references count the total twice | `products/sheets_wholecol.py`, `products/share_of_total.py` |
| 4.1.1 Emails sent before a "stop" is read | `outside/r6_stop_before_send.py` |
| 4.1.2 The kill switch mid-round | `outside/r1_kill_mid_round.py` |
| 4.1.3 An expired reply sent by an unlock | `outside/r2_expired_veto.py` |
| 4.1.4 The `qa_fix` unlock's bound | `outside/r3_qa_fix_bound.py` |
| 4.1.5 Your hold and the owed steps | `plan/repro_owner_hold.py` |
| 4.1.6 Hold new things | `plan/repro_new_things.py` |
| 4.2.1 A waiting request's files overwritten | `e2e/sim.py … 7 default active` (run A) |
| 4.2.2 Undo after an in-place fix | `platforms/repro_undo_inplace.py` |
| 4.2.3 The blog's list | `platforms/repro_blog_list.py` |
| 4.2.4 restore, draft and workshop overwrites | `tools/rt_test_workspace.py`, `tools/rt_test_workshop.py` |
| 4.3.1 A channel that is off | `plan/repro_channel_off_launch.py` |
| 4.3.2 A channel's own product | `plan/repro_channel_recurring.py` |
| 4.3.3 Ideas outweigh products | `plan/sim_ventures.py` |
| 4.4.1 Holds that outlive their reason | `loop/r8_stale_holds.py` |
| 4.4.2 Event cycles' sleep | `loop/r1_reactive_sleep.py`, `loop/sim_days.py` |
| 4.4.3 Event wakes that can't pay for work | `loop/r2_event_no_room.py 0.09` |
| 4.4.4 A reply that wakes twice | `loop/r6_reply_seen_then_wakes.py` |
| 4.4.5 The reflection after a slow step | `economy/e01c_reflection_last_step_slow.py`, `economy/e01d_reflection_default_caps.py` |
| 4.4.6 Provisional charges | `economy/e09_provisional_charge_critical.py`, `economy/e09b_provisional_charge_burn.py` |
| 4.5.1 The review's "close it" | `ventures/r3_stop.py` |
| 4.5.2 One score rescored | `ventures/r6_scores.py` |
| 4.5.3 Opposite lessons | `ventures/r4_playbook.py` |
| 4.5.4 The Plan tab's knock-out | `ventures/r2_needs.py` |
| 4.5.5 Cold outreach | `ventures/r7_cold.py` |
| 4.6.1 Pictures and the file | `products/sheets_checks.py` |
| 4.6.2 The Check line | `products/quarters.py`, `products/sheets_formula_checks.py`, `products/csv_header.py` |
| 4.6.3 KDP margins | `products/kdp_margin_scan.py`, `products/kdp_ink.py`, `products/kdp_checks.py` |
| 4.6.4 KDP A4 and Letter | `products/kdp_checks.py` |
| 4.6.5 A3 print files | `products/resize_big.py` |
| 4.6.6 Words-only photos | `products/text_overflow.py` |
| 4.7.1 Printify listings removed at Etsy | `platforms/repro_printify_deleted.py` |
| 4.7.2 A blog Undo while paused | `platforms/repro_blog_undo_paused.py` |
| 4.7.3 Ordinary file mistakes | `tools/rt_test_workspace.py` |
| 4.7.4 Released scripts | `tools/rt_test_workshop.py` |

Section 5's findings use the other scripts of each folder (for example `economy/e02_factor_streak.py`,
`loop/r9_refusal_no_reflection.py`, `plan/repro_pin_wait.py`, `platforms/repro_receipt_pages.py`,
`tools/rt_netguard_coverage.py`, `ventures/r12_bets.py`, `products/one_page_photo.py`, `web/probe_body_size.py`).
Some scripts show what holds: `products/stmt_awkward.py`, `stmt_letters.py` and `stmt_random.py` (the cost
statements against LibreOffice), `web/probe_security.py`, `probe_secrets.py`, `probe_fuzz.py` and `probe_triggers.py`,
`ventures/r1_econ.py` and `r9_evidence.py`, and `economy/e10_ledger_schema.py`, `e11_midnight.py` and `e12_proxy.py`.

The secret-looking values in `web/probe_secrets.py` and `web/probe_safe_mode.py` are fakes made up for the probe, to
check that Ember never shows them.
