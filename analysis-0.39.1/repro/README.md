# Reproductions for the analysis of 0.39.1

Each script shows one finding of [`../ember-analysis-0.39.1.md`](../ember-analysis-0.39.1.md) against Ember's own code
at `cb64642` (0.39.1). They are evidence, not part of the test suite: on 0.39.1 each prints the defect. Once it is
fixed, the same script shows the fixed behaviour, and it is a starting point for the fix's regression test.

**Run** them from the repository's `ember/` folder, with `requirements-dev.txt` installed (Python 3.12):

```bash
cd ember
PYTHONPATH=. python ../analysis-0.39.1/repro/<part>/<script>.py
PYTHONPATH=. python -m pytest ../analysis-0.39.1/repro/<part>/test_<name>.py -q -s
```

They keep their data in the system's temporary folder, never in the repository, and reach no network (the mail tests
use the suite's fake IMAP and SMTP servers, the SFTP test its own server on 127.0.0.1). Most use the test suite's
helpers (`tests/…`), as the suite does.

| Finding | Script | What it prints on 0.39.1 |
|---|---|---|
| 4.1 Quoted text runs a tool call | `tools/test_quoted_email_runs_a_call.py` | a real dry-run cycle saves a buyer's email; tool calls `workspace_write ok, memory_update ok`; LESSONS begins "Owner rule (verified): …" |
| | `tools/leaked_call.py` | the same through `tools.run`; and a quote in `message_owner` deletes `shop/planner.pdf` |
| 4.2 A stop the code can't see | `mail/stop_below_quote.py` | "Bitte keine E-Mails mehr." caught above the quote, `None` below it (German and English) |
| | `mail/test_stop_below_quote_and_large_mail.py` | the stop below the quote, and a 1.2 MB stop with a photo: stored, no suppression, the approved email "sent to: [['ann@example.org']]" |
| 4.3 One email stops the mailbox | `mail/test_8bit_header_blocks_mailbox.py` | `UnicodeEncodeError` every round; the last mail read stays 2; "etsy publisher ran: []"; the card says "waiting"; a stop in the same batch is lost |
| 4.4 Your holds leak | `plan/r3_owner_hold_bypass.py` | both lines held by you; the steer takes the held lines' decision, then their promise |
| | `plan/r6_hold_new_things.py` | "Hold new things" on: `project_create` refused without a venture, made with an idea's |
| 4.5 No weekly marketing until the critic passes | `plan/r1_launch_never_closes.py` | day 15 to 30: only "The critic passes it" open; 0 weekly steps; day 28: your decision "after 0 reach actions" |
| | `plan/r1b_channel_off.py` | Bluesky off: the launch never closes, no weekly steps for the channels that are on |
| 4.6 Pins and waits | `plan/r2_pins_ignored.py` | a pinned decision has `pinned=False`; a pin on a line Ember held waits `hold` |
| | `plan/r5_wait_cycle.py`, `plan/r5b_wait_on_later_step.py` | after 20 or 30 days: `steer: none` |
| 4.7 Veto window during downtime | `owner/test_r1_veto_after_downtime.py` | after 12 hours down the first round approves the held reply and carries it out; with a pause it stays pending |
| 4.8 Office files past the check | `products/repro_moved_worksheet.py` | the same `WEBSERVICE` workbook: refused at the standard path, kept with its sheet moved |
| | `products/repro_office_paths.py` | the same for a defined name and for a Word `DDEAUTO` field |
| | `products/repro_office_formula_places.py` | conditional format, data validation, `IMAGE()`, a Strict-namespace sheet: all kept |
| 4.9 Your website | `channels/mailto_bypass.py` | `mailto:…?bcc=…` and `?cc=…%40…` accepted, the audit finds nothing |
| | `channels/test_repro_sftp_rename.py` | after a slow or refused rename: "files on the server: []" |
| 4.10 Work lost, listings twice | `tools/draft_overwrite.py` | 22,531 bytes → 26 bytes, 0 versions, `restore` refuses |
| | `tools/auto_duplicate.py` | two cycles, one design, an automatic unlock: listings 900000002 and 900000003 |
| 4.11 Wakes | `loop/t05_event_wake_without_room_for_work.py` | an event's cycle pays its plan, is refused its first work step, the event is seen |
| | `loop/t01_refused_pick_uses_decision_try.py` | your decision weighs 30.0, then 3.64 for a day after one refused cycle |
| | `loop/t06_owner_wakes_with_cap_spent.py` | four of your messages with the cap spent: next wake 03:06 |
| | `loop/t04_stop_during_round_opens_cycle.py` | three stops during rounds: "press Wake now", while the dashboard shows an old "first wake-up" |
| | `loop/t08_reactive_sleep_pushes_schedule.py` | a reactive 720-minute sleep replaces a 30-minute scheduled wake |
| | `loop/t02_event_cut_from_reactive_plan.py`, `loop/t03_agenda_line_hides_owner_message.py` | the waking event isn't in the plan; with an event, your second message is cut |
| 4.12 One HTML email holds the scheduler | `mail/css_comment_timing.py` | twice the text, four times the time: 0.03 s, 0.13 s, 0.54 s, 2.3 s |
| 4.13 Death without a will | `economy/repro_starve_band.py` | a fee leaving $0.01: "alive", then "starved", no will; an Opus worker: the same after a restart |
| 4.14 Sonnet 5.5 | `api/thinking_shapes.py` | the `thinking` each model gets: Sonnet 5.5's plan and work requests carry "disabled" |
| 4.15 Records | `learning/repro_channel_first_test.py` | the Pinterest test "done" ten minutes after you back it; parked at day 60 though an order came |
| | `learning/repro_consolidate_numbers.py` | "Dropshipping ruled out: supplier margins under 5 %" kept as "Dropshipping looks promising." |
| | `learning/repro_one_event_three_cases.py` | one kill, three cases, "established" |
| | `learning/repro_weekly_playbook_cut.py` | the playbook at character 17,060 of a view cut to 16,000 |
| | `learning/repro_bet_due_day.py`, `learning/repro_bet_stale.py` | settled at 00:05 of its day; settled on week-old numbers a milestone refuses |
| 4.16 Website steps | `plan/r7_site_recurring.py` | the blog line's weekly pin and post steps still open 10 days on |
| 4.17 Pages and pictures | `products/repro_table_row_overflow.py` | the long row's text 87 pt below the page's foot |
| | `products/repro_sheet_dates.py` | `COUNT`, `MAX`, `MIN`, `COUNTIF` over dates: 0, where Excel shows 3, the last date, the first, 2 |
| L28 | `tools/mp_probe.py` | the `spawn` context runs a child while sealed; `fork`, `forkserver`, `os.fork`, `os.forkpty` are blocked |

The reviewers wrote most of them. Five are the analysis' own independent checks:
- `api/thinking_shapes.py`
- `mail/stop_below_quote.py`
- `mail/css_comment_timing.py`
- `products/repro_moved_worksheet.py`
- `tools/test_quoted_email_runs_a_call.py`

Each ran from this folder on 2026-10-14 with exit code 0, and a secret scan with the repository's `.gitleaks.toml`
found nothing.
