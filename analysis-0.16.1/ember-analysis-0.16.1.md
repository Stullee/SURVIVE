# Ember 0.16.1: the bugs, from major to minor

*Third analysis, 2026-10-01. The first was of 0.11.1, the second of 0.13.0 (`analysis-0.13.0/`).*

**Scope.** The whole codebase at 0.16.1 (`5a76774`, the head of `claude/elegant-faraday-tcl6tq`, which Home Assistant installs from). Your live diagnostics of 2026-10-01 16:23 UTC (0.16.0, cycles up to #65). The 0.15.0 patch notes (section 10 of the 0.13.0 analysis), checked against the code.

**How the bugs were found.**
- 12 reviews ran in parallel: economy; loop and planner; tools, workshop and products; roadmap and ventures; autonomy; Etsy, Printify and Pinterest; mail, privacy and diagnostics; web, config and database; the website code (blog, SFTP, live view); tests and process; live operations and money; live behaviour.
- Each reviewer reproduced its findings with scripts against the real code. I re-checked the High bugs and the main Medium ones myself.
- The full test suite ran once: **2,162 tests pass**. `ruff check` and `ruff format --check` are clean. CI is green on GitHub for 0.15.0, 0.16.0 and 0.16.1.

**As you asked, this is a list of real bugs, not a list for its own sake.** Every entry was reproduced or read in the code, and checked for a guard that already prevents it. Findings about the agent's behaviour rather than the code are in section 4, not in the bug list. Where a part held up, section 1 says so.

**Marks.**
- **✔**: I verified it myself, or two reviewers found it independently.
- **On your install**:
  - **now**: happening in your live data;
  - **dated**: will happen on a known day unless fixed;
  - **default**: off on your install because you switched the part off, but on by default;
  - **latent**: needs an event that hasn't happened yet.

**Paths** are relative to `ember/app/` unless they start with `tests/` or the repository root. Line numbers are for `5a76774`.

---

## 1. Verdict

**The foundation is solid.** That is not a courtesy: the parts that keep money, outside actions and files safe were attacked on purpose, and they held.

| Part | What held up |
|---|---|
| Money accounting | 185 of 189 live calls recompute to the micro-dollar from their tokens and the price table. The other 4 differ by exactly the workshop's container time. The ledger is append-only and guarded by triggers. All spending goes through one locked SQLite connection, with every limit re-checked inside the reservation. No call has gone over its estimate since 0.15.0. The 20:00 reserve is right on both DST days. |
| Outside actions | One exactly-once pattern in every publisher. A reviewer crashed 12 windows across Etsy listings, Etsy edits, Printify products and pins: nothing was ever sent twice. |
| Files the agent makes | 22 PDF attack variants were refused (object streams, incremental updates, `/JS`, `/Launch`, hex-escaped names, encryption, and more), and the check fails closed. So were the Office ones: macros, OLE, external links, DDE, a renamed `.xlsm`. The sandbox refused traversal, symlinks, hardlinks and quota bypasses. |
| Web boundary | All 56 routes were checked. Each enforces owner identity. All 32 POSTs need the CSRF header. No answer carries a secret, and no GET changes anything. The dashboard has no HTML sinks, and its previews are sandboxed. |
| Database | A fresh database and a 0.13.0 database upgraded to 0.16.1 have identical schemas: 66 tables, 65 indexes, 163 triggers. Integrity and foreign-key checks are clean. No network call runs inside a transaction. |
| Safe mode, kill switch | Safe mode keeps your owner IDs and fails closed, and never lifts a kill. 128 boundary values of the new options found no new way into safe mode. |
| Website escaping | No payload got markup through front matter, Markdown, link buttons, titles or the will. |
| Tests | They drive real code: the database with its triggers, the full loop, the real HTTP transport, a real SFTP server. They execute 93-95% of the statements 0.15.0 and 0.16.0 changed. |

**Where the bugs are.** They cluster in four places:
1. **The newest code: the live view and the blog's SFTP channel.** These came in 0.14.0-0.16.1, built in hours by parallel sessions. Three of the four High bugs are here.
2. **What the agent and you are shown.** Stale "Unlocked" notes, stale next steps and strategy, release notes that restart, lessons that drop the newest first, a wrong venture link from a migration. Nothing unsafe leaves the container because of these, but the agent plans on wrong facts, and so do you.
3. **Edges of the money guard.** Maintenance doesn't stay down. An interrupted call counts toward the caps only by its known part. Reset clears the workshop's history.
4. **Edges of the unlock engine.** These only matter once you unlock again: all 20 of your 09-30 unlocks were taken back by the 0.15.0 upgrade.

**Count:** 4 High, 22 Medium, 6 that only matter after you unlock again, 34 Low. None of the High bugs is in money accounting, the publishers' exactly-once sending, the file checks, the web boundary or the database.

**This week:**
- Keep `live_show_memorial` and `live_show_work` off until bugs 2 and 3 are fixed. They are off on your install, but both default to on.
- Don't edit `blog_sftp_folder` until bug 4 is fixed, or keep it short and plain ASCII.
- Bug 1 has a date: unless it is fixed, Ember's code parks your print-on-demand venture around 10-22, and the Nebenkosten listing test ends with it.
- Ignore the "Unlocked for this milestone" notes on milestones #2, #3, #6 and #7. Nothing is unlocked (bug 5).

---

## 2. The bugs, from major to minor

### High

| # | Bug | On your install | Where | Fix | Effort | ✔ |
|---|---|---|---|---|---|---|
| 1 | Ember's code will park the print-on-demand venture #4 around 10-22, and end the Nebenkosten listing test with it | dated | `migrations/0065_roadmap.sql:30-44`; `agent/metrics.py:534-538`; `agent/stages.py:96-121, 282-291`; `migrations/0068_printify.sql:50-63` | Link product lines by channel; re-link #7; restart a first test Ember's own refusal blocked | S | ✔ |
| 2 | The last will goes onto the public live page with no approval | default | `integrations/live_view.py:216-223`; `products/live.py:597-615`; `config.py:271` | The will becomes an owner-only approval request; memorial off by default | S | ✔ |
| 3 | Agent-written titles reach the public page checked only by the email mask | default | `integrations/live_view.py:92-108, 157-182`; `config.py:262-271` | A strict public-text filter, and your approval of each new title; parts off by default | S | ✔ |
| 4 | A typo in `blog_sftp_folder` freezes the whole app | latent | `integrations/sftp.py:35` | Check each path segment with a simple pattern | S | ✔ |

**1. The print-on-demand venture will be parked around 10-22.**
- **The test can't be met in time.** Venture #4's first test, milestone #16, needs one Printify order by 10-21. Migration 0065 replaced the old prose test #8 with this metric but kept #8's dates (`0065_roadmap.sql:30-44`). No Printify product exists yet: the poster was blocked first by the 12 MP limit (fixed in 0.15.0), and now by the print-area shape rule (cycle #65, bug 16).
- **There is no grace for it.** A metric milestone is closed "missed" at the first sync after its date (`agent/metrics.py:534-538`). The 7-day grace (`stages.py:45`) applies only to tests still open. The next keeper run then parks the venture (`stages.py:290-291`).
- **Nebenkosten goes down with it.** A park by Ember's code also drops the bars of every project linked to the venture (`stages.py:96-121`, 0.15.0's X10).
  - Migration 0068 linked project #7, the Nebenkosten Excel template (a digital download), to venture #4. It took the venture stamped on #7's newest request, and that request was made in a cycle focused on print on demand (`0068_printify.sql:50-63`).
  - Live, the projects table shows #7 under venture 4. Patch note 16(f) says such lines link to the Etsy leg.
  - So the park ends the Nebenkosten listing test too. Until then, a Nebenkosten sale counts for print on demand, not for the Etsy leg's 60-day "nothing sold" rule.
- **New lines will repeat it.** `ventures.adopt` makes the same choice (the cycle's focus venture) for every new product line (`agent/ventures.py:1014-1038`).
- **Fix (S):**
  - link product lines by channel (a digital download goes to the Etsy leg);
  - re-link #7 to venture #1 in a migration;
  - restart a code-set first test whose product couldn't be made because of Ember's own refusal, or give metric tests the 7-day grace and warn you a week before.

**2. The last will goes public with no approval.**
- **What happens.** When Ember dies, `live_view.snapshot` reads the last will and, if `privacy.Masker` finds nothing in it, puts up to 6,000 characters of it on the public page every 15 minutes. No approval row exists (`live_view.py:216-223`, `products/live.py:597-615`). `live_show_memorial` defaults to on (`config.py:271`).
- **The vision forbids exactly this.** The agent writes the will while critical. The vision the live view was built from says it must be approved before it can be shown: "A will written in a panic and published without a look is the one thing that must never happen" (`vision/live-page.md`, "What may be shown, and who decides").
- **Reproduced.** A will holding a phone number, a URL and a stranger's name and street went up unchanged in the first round after death.
- **Fix (S):** make the will an owner-only approval request, like a blog post. Only an approved version is shown, and `live_show_memorial` defaults to off.

**3. Agent-written titles go public checked only by the email mask.**
- **The check.** Venture and milestone titles go onto the public page whenever `privacy.Masker` changes nothing in them (`live_view.py:92-108`). The agent writes those titles, and it reads emails and web pages. The Masker was built for the diagnostics report: it masks email addresses, codes, link tokens, removed words and email senders' full names.
- **Reproduced.** Through the real snapshot and the bytes that are uploaded, all of these went up:
  - phone numbers (`+49 30 …`, `0151…`);
  - a plain URL;
  - "Frau Schmidt" (only the sender's full name is masked);
  - an email address with a zero-width space, or with a fullwidth @;
  - a name with street and postcode;
  - an IBAN;
  - "Kauft jetzt im Shop, sonst sterbe ich in 3 Tagen!".
- **The release note overclaims.** It tells the agent "a title holding … a person's name is left out" (`CHANGELOG.md:22-24`). That holds only for the senders of emails.
- **Every part defaults to on** (`config.py:262-271`), although the vision asked for "default to off, and to the least: state and runway only". Live uploads also have no journal entry or Undo, unlike blog uploads.
- **Fix (S):**
  - a strict filter for public text: after Unicode folding, no run of 5 or more digits, no URL or domain, no @ in any form;
  - your one-time approval of each new title, or counts only;
  - the parts that show the agent's words default to off.

**4. A typo in the SFTP folder option freezes the whole app.**
- **The pattern.** `FOLDER = re.compile(r"^/?(?:[A-Za-z0-9._~ -]+/?)*$")` (`sftp.py:35`) backtracks exponentially when the folder holds a character outside its set.
- **Timed here** with an umlaut at the end: 26 characters take 1.0 s, 28 take 4.6 s, 30 take 16.9 s. Every 2 more characters multiply the time by about 4.
- **Why the whole app stops.** Python's `re` holds the GIL, so the web UI and the scheduler stop with it.
- **It comes back after a restart.** The check runs from the plan's BLOG section, the dashboard's status and every live-view round (`integrations/site_publisher.py:77, 96`, `live_view.py:77`), so the app hangs again after each restart. Home Assistant accepts up to 200 characters (`config.yaml`: `str(,200)?`).
- **Fix (S):** check each segment with `[A-Za-z0-9._~ -]+` (no nested quantifier), and add a timing test.

### Medium

| # | Bug | On your install | Where | Fix | ✔ |
|---|---|---|---|---|---|
| 5 | "Unlocked for this milestone" notes stand after the unlocks were taken back | now | `migrations/0062_unlock_safety.sql:34-39`; `agent/owner.py:642-650`; `agent/service.py:904-916`; `agent/roadmap.py:569-579` | Show unlock state from `policy_grants`, never in the owner-note slot; a migration clears the stale notes | ✔ |
| 6 | Maintenance doesn't stay down: it flips back to focus within days | dated | `economy/burn.py:103-115` | Leave a lower mode only after a grant or revenue, as critical does | ✔ |
| 7 | An interrupted call counts toward the caps only by its known part (often $0), while the balance is charged its hold | latent | `economy/ledger.py:303-327, 396`; `economy/metering.py:397, 1189` | Count interrupted calls toward the caps at their charged cost | ✔ |
| 8 | Reset estimates also clears the workshop's run history, so the hold falls back to the cap per run | now | `economy/metering.py:464-470`; `economy/pricing.py:221`; `web/static/js/app.js:772` | Reset clears only the safety factors, or its confirmation names the new hold | ✔ |
| 9 | The home page's banner can say "alive" long after Ember stopped | latent | `products/live.py:269, 747, 850-861` | The date, and "offline" after an hour, in the banner; replace files that are switched off | ✔ |
| 10 | The shareable diagnostics print the SFTP login name and host | now | `diagnostics.py:400-409` | Treat the `blog_sftp_*` options as personal and mask their values everywhere | ✔ |
| 11 | The email verdict parser can be fooled into "verified" | latent | `integrations/mail.py:350-383` | Parse per RFC 8601; trust only your provider's authserv-id; require `header.from` | ✔ |
| 12 | Release notes start over after every upgrade | now | `agent/news.py:109-114, 249-250, 309-317` | Unread sections oldest first, a read position per section | ✔ |
| 13 | Strategy and next steps go stale: the reflection can't see the strategy | now | `agent/prompts.py:159-168`; `agent/context.py:449-460, 868-928`; `agent/tools.py:335-340` | Give the reflection STRATEGY and the named projects' next steps; flag next steps naming closed requests | ✔ |
| 14 | YOUR LAST CYCLE can't use spare room and loses its "Not done" lines | now | `agent/context.py:497-507, 774, 796-808` | Build the section with its budget plus the spare; shorten "Done" first | |
| 15 | A full lessons file drops the newest lessons first | now | `agent/memory.py:210-223, 332` | Drop the oldest unpinned lesson, never one just written | ✔ |
| 16 | Print-area shapes: the catalog doesn't show them, a refusal names one pair, and a small mismatch prints with white strips | now | `integrations/printify_publisher.py:372-399`; `agent/tools.py:3819-3836`; `integrations/printify.py:53, 136-147`; `integrations/qa.py:29`; `agent/guides/printify.md:16` | Show and group shapes; refuse a picture more than ~1% off its group; fix the guide | ✔ |
| 17 | The day-7 bars: six separate requests fill the queue Printify shares, and day 14 asks to park the shop | dated | `agent/gates.py:176-213` | One batched request a day for bar fixes, with room of its own | |
| 18 | The daily review can't see your backings or channel readiness, and the agent can carry out its "park" | now | `agent/review.py:160-190, 364-441`; `agent/prompts.py:574-584`; `agent/tools.py:2026, 2070` | Give the scorecard those facts; the agent's park of a venture you backed becomes a request to you | ✔ |
| 19 | A first test without a metric can never close "met", and your Drop means opposite things | now | `agent/stages.py:47-93`; `agent/tools.py:2550-2554` | Two owner actions, Met and Not met; Drop means stop | |
| 20 | The "slow" knock-out rules out nearly every honest case and flips with each grant | now | `agent/knockouts.py:185-187`; `agent/tools.py:421-423` | Compare with a fixed horizon, or make it a warning | |
| 21 | A cancelled print-on-demand order keeps its booked cost | latent | `integrations/printify_publisher.py:488-545` | Book once Printify has charged; correct automatically on cancellation | ✔ |
| 22 | An Etsy listing Ember recorded as draft or unclear is never taken up when it is live | latent | `integrations/etsy_publisher.py:330-345, 428-442, 1484-1502` | Adopt such rows in the sync, as 0.15.0 does for Printify | |
| 23 | The blog's list on the server loses entries that aren't in the exact template form | latent | `products/blog.py:642-743`; `integrations/site_publisher.py:460-472` | Refuse to upload a list with a `<li>` that doesn't parse, and tell you | ✔ |
| 24 | Blog Undo never runs while Ember is paused or unfunded, or the blog is off | latent | `agent/service.py:948-957`; `integrations/site_publisher.py:369` | Run blog Undos in the blocked branch, as the other publishers do | |
| 25 | A connection dropped mid-transfer isn't handled | latent | `integrations/sftp.py:164-231`; `integrations/live_view.py:332-335` | Map `paramiko.SSHException` to NotSent or Unclear; catch-alls in the live view and the check route | ✔ |
| 26 | A test fails in CI from 2026-10-28 01:00 UTC, which blocks every release | dated | `tests/test_diagnostics.py:21, 176-201`; `agent/scheduler.py:79-87` | Date the fixture rows from the app's clock | |

**5. The unlock notes are stale.**
- **The take-back worked.** The 20 unlocks you set on 09-30 (5 rules on each of #2, #3, #6 and #7) were taken back by the 0.15.0 upgrade. `policy_grants` holds 40 rows: those 20 plus 20 take-backs. A reviewer reproduced this by running 0.13.0's own code and then upgrading.
- **Why the notes stayed.** The unlock was written into the milestone's single owner-note slot. The take-back (0062) writes no note, and neither do Ember's code's other take-backs.
- **What the agent is told.** The planner says, as your word, that it may take listings off Etsy (#6) or answer threads (#7, #2 and #3) under a veto window. The dashboard shows the same note above an Autonomy box that says "taken back".
- **Each click overwrote the last.** The note names only the last of your 5 clicks per milestone, and it would erase a real note of yours.
- **Nothing is sent because of it:** the code checks `policy_grants`, not the note.
- **The patch note is wrong here.** Its "(there were none live)" doesn't hold: there were 20.

**6. Maintenance doesn't stay down.**
- **Why it flips.** `settle` moves a mode up whenever the net runway is 20% past its threshold (`burn.py:103-115`). In maintenance, spending drops to $0.40 a day, so the 7-day window's burn falls, and the net runway passes 18 days within 2-5 days. Then it is back to focus and full spending, then back to maintenance.
- **On your ledger.** Running the real code: maintenance on 10-18, focus again on 10-20, then 11 more flips. At the $7 cap it reaches critical around 11-22 to 11-26.
- **The docs promise otherwise.** The docstring says a mode "moves up only when money comes in" (`burn.py:13-16`), and DOCS that it "doesn't flicker" (`DOCS.md:1911`).
- **Fix (S):** leave a lower mode only after a grant or revenue, as critical already does, and add a week of maintenance spending to the tests.

**7. Interrupted calls slip past the caps.**
- **Which calls are still "uncertain".** Since LIVE 4, a 5xx before any reply costs $0 and isn't uncertain any more. The ones left are calls interrupted mid-answer: by a restart (Ember restarted 7 times on 10-01), a broken stream or a timeout. They most likely did cost money.
- **What the caps count.** `cap_spend_on` subtracts `cost − floor` for these calls. A workshop run interrupted by a restart therefore counts $0 toward the day, while the balance is charged its $1.50 hold, and the day can spend its whole cap again.
- **Reproduced.** With $5.30 of $7 spent, an interrupted $1.50 hold still leaves $1.70 of room.
- **The docs are out of date.** `DOCS.md:1959` still describes these calls as "the API failed before any reply".

**8. Reset weakens the workshop hold.**
- **What Reset clears.** It clears every safety factor in the mode and also the workshop's run history (`_workshop_tail` reads "since the owner's last reset"). It is one click on the banner, with no confirmation.
- **The effect.** After a #423-sized run the next hold is $2.76. After a Reset it is $1.50, and the same run goes over the daily cap by $0.34.
- **Live.** Your Reset at 14:06 UTC on 10-01 dropped the hold from $1.74 to $1.50. It was your third in two days.

**9. A stopped Ember can look alive.**
- **The banner has no date.** It states only the time ("Stand 16:20 Uhr"). When uploads stop (the app is down, SFTP fails, a dry run, safe mode), the last banner stays up with "alive", the old balance and a time that looks current.
- **Switched-off files stay.** A file you switch off, such as the banner itself (`live_banner`), is never replaced. Only switching the whole live view off uploads the "off" pages.

**10. The shareable report shows the SFTP login.**
- **Where it appears.** OPTIONS and INTEGRATIONS show `blog_sftp_user` and `blog_sftp_host` in clear. The host also appears in META (`integrations.site.live.host_key`) and in an EVENTS line, and its first label looks like a hosting customer number.
- **Why it matters.** That is two of the three login factors to the server that hosts your site and the live view. Your mailbox address and Impressum fields are masked in the same report.
- **In this file.** The values are left out. If you have shared the report elsewhere, keep that in mind.

**11. The email verdict parser can be fooled into "verified".**
- **Comments and quoted strings.** The parser removes comments with a regular expression and splits on ";" without honouring quoted strings.
  - Your provider may repeat the envelope sender inside a comment of its own header; the project's mailbox.org sample does (`tests/test_fixes_0140_mail.py:96-97`).
  - An envelope local part such as `"x) ; dmarc=pass header.from=victim.example ; (y"` then reads as a verified email from victim.example.
- **dmarc without header.from.** A `dmarc=pass` without `header.from` passes for any From: domain.
- **The impact.** A forged email counts as a verified person:
  - an inquiry, and a paid event wake;
  - the first-contact rule lifted for that address;
  - the warning gone from your card.
- **Not checked live.** All 23 stored emails predate 0.15.0, so the header mailbox.org really puts on top is still unverified.

**12. The release notes start over.**
- **Why.** META holds the read position `0.13.0>0.15.0@3644`. The running version is now 0.16.0, so the position no longer matches, and the next plan starts again at "## 0.16.0", then 0.15.0 from the top.
- **How long a full read takes.** Reading from 0.13.0 to 0.16.x takes 6 uninterrupted plans, or 11 with the upgrades in between.
- **What the agent misses.** With a release every few plans, the agent never reaches the later 0.15.0 notes or 0.14.0's blog notes.

**13. Strategy and next steps go stale.**
- **Why.** The reflection can't see STRATEGY, and `memory_read` is refused while reflecting, so it never updates it. STRATEGY still says "Current priority: dropshipping", unchanged since cycle #35.
- **Stale next steps, live:**
  - #7 still waits for request #14, done on 09-30;
  - #8 waits for upgrade #3, which is released;
  - #10 waits for request #20, done at 14:24;
  - milestone #15 waits for a "first article", and 4 posts are live.
- **An advised change was never made.** The review's "change" verdict on #7 was never applied: verdicts are text the agent must carry out (`agent/review.py:648-660`).

**14. The last-cycle summary is cut while room goes unused.** The digests are cut before the second pass hands out unused room. Live, "[945 bytes cut]" removed #65's "Not done" line, which named the Printify shape refusal and the too-long write. Meanwhile 8-9 KB of the plan's budget went unused.

**15. New lessons are dropped first.**
- **The rule.** `trim` first drops a lesson without a digit, which can be the one just written, before older ones. The consolidation never drops a lesson with a digit.
- **Live.** 17 of the 20 lessons contain a digit, and the file is at 3,988 of 4,000 bytes. New advice without numbers vanishes, while stale lessons stay ("make_image is capped at 4 calls/cycle": it is 10).

**16. Print-area shapes.**
- **The tolerance.** The shape rule allows 3%. The live pair, 11×14 and 9×11, differs by 4%.
- **Placement.** The picture is centred and shrunk to fit, never cropped. The agent's A-ratio poster (0.707) on a 3:4 group (0.75) would print with white side strips of 0.34-0.51 inches. QA notes a mismatch only above 10%.
- **The guide is wrong.** It says 2:3 fits A sizes; it doesn't.
- **No free way to re-fit.** No free tool re-fits existing artwork to a ratio: only a new text poster, or a paid workshop run. The A-series EU blueprint the agent used before (#443, provider 30) fits the picture as it is.

**17. The day-7 bars.**
- **On 10-08.** All five day-7 bars stand at 0-2 of 10 views. On 10-08 the night sync grades them missed. The next plans create 5 obligations and 6 listing-edit requests, one per listing.
- **The queue fills up.** Those 6 requests fill the 6 places for waiting sell requests, which `propose_printify_product` shares, so no poster can be proposed until you decide them.
- **On 10-15.** The day-14 bars owe 5 "park the product line" obligations, against your standing instruction to keep improving the shop.
- **Bad timing.** Decision points #6 and #7 fall the day before each grading.

**18. The review can recommend what it shouldn't.** The review told the agent to "Park #5" an hour after you backed it, and to "start Pinterest now" while Pinterest isn't set up. Its scorecard has no owner word, first test, channel state or standing instructions. `venture_update` can park a venture you backed and drop its first test. Only your Back undoes that, and without numbers it needs "Back it anyway".

**19. First tests without a metric.**
- **#15 can't close met.** For #15, the blog's first test, neither the agent nor the review can close it done, and you have no "met" button.
- **Drop means two things.** Your Drop counts as met for Ember's code (the venture can go live), but tells the agent "stop working toward it".
- **The risk.** Dropping metric test #16 to stop it would let the agent set print on demand live with no product.

**20. The "slow" knock-out.**
- **The rule.** "Slow" means a first sale later than half the net runway.
- **What passes.** At 33 days of net runway, only first sales within 16 days pass. At the morning's 15 days, none passed.
- **It flips.** A 21-day case is knocked out at 33 days and passes at 43.
- **A silent reversal.** Your Back on a proposed venture sends it back to researching, silently, if the runway dipped since the card loaded.

**21. Cancelled print-on-demand orders.**
- **When the cost is booked.** For any order not cancelled at the sync, including one still on hold. A later cancellation is never revisited.
- **The effect.** Etsy's cancellation is booked as a refund, so in a simulation the ledger showed about −$40 for an order that really cost $0.20-2.40 in fees. This phantom cost paused the live agent for money it still had.

**22. Etsy listings left as draft or unclear.**
- **When it happens.** After a timeout on the final "make it live" request, or after you finish a draft at Etsy as its note asks.
- **What goes wrong.** The sync reads the listing's views and books its fee. But the agent sees no live listing, can't edit or pin it, and `listings_live` leaves it out.

**23. The blog's list.**
- **What it drops.** `read_index` keeps an entry only in the template's exact form. These are dropped:
  - a relative or absolute link;
  - a missing `</li>`, which is valid HTML;
  - a missing `<time>`.
- **The effect.** The next upload replaces your list without those posts, and Undo returns the shortened list. Today your list parses (4 posts).

**24. Blog Undo while paused.** Your Undo is accepted and shown as pending, but nothing happens until Ember runs again with the blog on. 0.15.0 row 21(f) fixed this for the other publishers, not for the blog, which came from the parallel 0.14.0.

**25. A dropped SFTP connection.** `sftp.py` catches `OSError` and `EOFError` but not `paramiko.SSHException`. Against a real paramiko server, the exception escapes. The live view then logs in again on every scheduler round with no back-off, and "Check the connection" answers 500.

**26. The CI time bomb.**
- **The cause.** The test's rows are dated 2026-09-28 01:00 UTC. The test app's scheduler prunes texts older than 30 days using the real clock.
- **Reproduced.** With the clock shifted past 10-28 01:00 UTC, the test fails in 9 of 10 runs.
- **The effect.** Under README's green-CI rule, no release can go out after that date.

### Only after you unlock again

These need a standing unlock. None stands now. Together with bug 11, fix them before you unlock anything.

| # | Bug | Where | Fix | ✔ |
|---|---|---|---|---|
| 27 | The veto window runs while the app is down: a held request is approved the moment it restarts | `agent/policy.py:450-490`; `agent/scheduler.py:88-95` | Approve only when now ≥ max(window's end, start + 1 h), and the same after Resume | ✔ |
| 28 | The agent can widen your unlock with its own tools | `migrations/0063_unlock_keying.sql:10-25`; `agent/tools.py:2614-2627, 1829-1833` | Keep the covered project or venture on the grant, or refuse link and venture changes while a grant stands | ✔ |
| 29 | NEVER lets realistic offers, discounts and refund promises through an email-reply unlock | `agent/never.py:59-73`; `migrations/0063_unlock_keying.sql:76-95` | No unlock carries a reply with an amount, a percentage, a currency or a link | |
| 30 | "Threads the other person started" also matches threads Ember started | `agent/policy.py:122-135`; `integrations/mailstore.py:416-423` | Walk the stored chain to its root; refuse if any id in it is one of Ember's | |
| 31 | The kill switch doesn't stop a sending round already under way | `agent/service.py:948-958`; `integrations/executor.py:214-256` | Check the kill flag before each item | |
| 32 | The promotions card can offer a one-click email-reply unlock that covers every thread | `agent/policy.py:562-600`; `web/static/js/app.js:6460-6462, 6682-6686` | Don't offer it, or say on the card what it covers | |

- **27, the veto window.** `run_due` approves every request whose window has passed, in the first scheduler round after a start. `BOOT_GRACE` delays only the agent's wake. Reproduced: a request held at T, the app down from T+1 h to T+30 h, approved 0.0 s after the start, while you couldn't veto because the dashboard was down too.
- **28, re-linking.** Links are frozen only on milestones Ember's code set or a metric measures. Reproduced:
  - You unlock price changes on the agent's milestone for project #3. The agent re-links the milestone to #7, and a price change on #7's listing is carried.
  - Moving a project into a venture widens that venture's unlock the same way. It also shields a researching venture from its research park.
- **29, NEVER's words.** 21 of 29 realistic phrasings passed, for example "Preisangebot", "Kostenvoranschlag", "individuelle Version für 15 €", "20 % Rabatt", "I'll refund you", "money-back guarantee" and "Nutzungsrechte". Meanwhile 13 of 15 ordinary support replies for this shop are blocked; "Nebenkostenabrechnung" itself trips it. An email unlock would be both unsafe and mostly useless.
- **30, thread origin.** `thread_headers` ignores In-Reply-To and keeps only the last 4 References. A reply carrying only In-Reply-To, or one in a thread 6 or more messages deep, counts as theirs. Reproduced: both such replies were approved automatically and sent.
- **31, the kill switch.** Kill was pressed during the first of two emails you had approved, and the second was still sent. Unlock-approved requests are correctly skipped.

### Low

| # | Bug | Where |
|---|---|---|
| 33 | The reflection can be refused after a call cost more than its hold: the daily cap and the 20:00 reserve have no allowance for it. 0.15.0's "the reflection always runs" fails in that case | `economy/metering.py:988, 1043, 1052`; `agent/loop.py:1448` |
| 34 | A grant that lifts Ember out of maintenance leaves the 24-hour maintenance sleep in place | `agent/service.py:507, 836` |
| 35 | In maintenance with an Opus planner, the daily review, consolidation, critic and study can never run: each keeps a whole working cycle out of $0.40 | `agent/loop.py:945, 989, 1038, 1109` |
| 36 | The workshop's cost history leaves out interrupted runs, and with 20 calls its p95 drops the costliest one | `economy/metering.py:450, 471` |
| 37 | The planner preview says the next cycle "may spend up to $0.00"; the scheduler actually waits for 20:00 and gives it $1.00 | `economy/metering.py:578`; `agent/service.py:399-404` |
| 38 | The $0.10 "charged at the worst case" warning (call #293, a 503 from before LIVE 4) still needs your manual correction | `economy/service.py:314` |
| 39 | After 7 or more days paused, the net runway reads as unknown, which means explore | `economy/life.py:399-401` |
| 40 | The prompt-size test leaves out the blog. With it on, as live, the fixed work prompt is 47.5 KB against the 45.9 KB bound | `tests/test_rule_audit.py:286-308` |
| 41 | Wake now pressed just as a cycle starts can queue a second cycle (a window of milliseconds) | `agent/service.py:535-543, 630-637` |
| 42 | The shareable report shows your name and town inside the agent's tool texts (#59-#61). Phone numbers, IBANs and streets are masked nowhere | `diagnostics.py:222-275` |
| 43 | The live view's text check skips `redact()`: a registered secret in a title would be uploaded | `integrations/live_view.py:103-108` |
| 44 | MAIL lists opted-out and unverified mail as plain unread. The 10 unread stay forever, and the newest 3 are from the sender who opted out | `agent/context.py:667-676` |
| 45 | The 09-30 opt-out still says "replied" for a sender Ember never emailed: LIVE 11 fixed new rows only, and a trigger forbids correcting the old one | `integrations/mailstore.py:114-117` |
| 46 | Opt-out detection misses 8 of 44 phrasings ("Bitte von der Liste nehmen", "Kein Interesse", "Please cease all communication") | `integrations/optout.py:71-129` |
| 47 | The hidden-text filter lets 7 hiding methods through (`@media`, attribute selectors, `transform`, `clip-path`, `filter:opacity(0)`, …) | `integrations/mail.py:571-622` |
| 48 | A bad date in the server's blog list (day and month swapped, or a trailing newline) raises on every round, or leaves the upload rows "running" | `products/blog.py:78`; `integrations/site_publisher.py:471` |
| 49 | The first SFTP login trusts the first key it sees, and can happen unattended in a live-view round. A pinned key of a type paramiko doesn't negotiate refuses the real server for good | `integrations/sftp.py:245-285` |
| 50 | Blog links: a `mailto:` may carry `?bcc=…`, and `product_text` links skip the link check | `products/blog.py:309-328` |
| 51 | The page check before upload lets `xlink:href` (in the svg 0.16.1 allows), a meta refresh to another site and external stylesheets through. Defence in depth only: the escaping held | `products/blog.py:82-106, 746-797` |
| 52 | Etsy's 6-hour display rule is applied only when the page is rendered: a page that stops updating keeps showing the listings and their numbers | `integrations/live_view.py:183-198` |
| 53 | A new blog post silently overwrites an unlisted page with the same slug | `agent/tools.py:3993-4003` |
| 54 | A Printify order without a currency is labelled with your option's (EUR), which may book USD amounts as EUR, 13% too high. Inferred: check the first real order | `integrations/printify_live.py:226-240` |
| 55 | X8's check for listings missing at Etsy skips the ones Printify made: an expired one keeps counting as live | `integrations/etsy_publisher.py:784-792, 890-900` |
| 56 | Each failed cost probe uploads another blank picture; a failed delete blocks probes until a sync | `integrations/printify_publisher.py:284-334` |
| 57 | Unclear outcomes are reconciled only for Printify products: a pin, a board or an Etsy edit made just before a crash stays unclear | `integrations/pinterest_publisher.py:255-327`; `integrations/etsy_publisher.py:436-441` |
| 58 | The word checks behind X24, X26 and 18(a) are easy to pass ("20+ views", "Aufrufe", "Telefonakquise") | `agent/tools.py:2338-2342`; `agent/demand.py:39-86` |
| 59 | Research for a venture done in a cycle not aimed at it escapes that venture's research budget | `agent/tools.py:3008-3022` |
| 60 | Printify and Pinterest metrics are graded on the last sync, labelled as read now | `agent/metrics.py:364-369` |
| 61 | A bar with no Etsy reading by its date is graded missed, even when a later reading shows it was met | `agent/metrics.py:515-569` |
| 62 | A forecast is voided, not missed, when Ember's code parks the venture it was about | `agent/stages.py:114`; `agent/predictions.py:97-100` |
| 63 | Deletes count toward the 10 writes a cycle. Live, #64 couldn't finish the cleanup you asked for | `agent/tools.py:319, 1486` |
| 64 | The constitution's list of what Ember's code carries out leaves out blog and live uploads. README describes the live page as "made from Ember's own numbers", and the live view's docstring as numbers and public items, though it also shows agent-written titles and the will | `agent/constitution.md:17-22`; `README.md:145-147`; `integrations/live_view.py:5-10` |
| 65 | An unpriced optional model (strategy, research or workshop) still forces safe mode: silently a dry run, with every unlock taken back. A correction would do | `config.py:355-364` |
| 66 | The scheduler tests use stubs missing every method added since 0.13.0: one passing test swallows 529 AttributeErrors | `tests/test_fixes_0140_wakes.py:235-262` |

---

## 3. The 0.15.0 patch notes, checked

**Hold.** Most rows hold:
- **FIX NOW rows:** 3, 4, 6, 7 (CI is green), 8, 9a-9e (9c with the gap of bug 31), 11, 12, 14 (one reflect call per working cycle, live in #64 and #65), 16a, 16b, 16c, 16e, 16g, 17, 21b, 21d, 21e, 22, 23, 25, 26a-d, 26f-l, 26n.
- **X items:** X1-X6, X7 (built as described, but it never ran live: section 4), X9-X14, X16, X17, X20-X23, X25-X28.
- **LIVE items:** LIVE 1-6, 8-10.
- **Declared partial by the notes themselves, and still so:** 21(a)'s email scope, 26m, LIVE 7.

**Broken:**

| Claim | What the code does | Bug |
|---|---|---|
| 16(f): lines without a venture (Nebenkosten #7) link to the Etsy leg | 0068 linked #7 to print on demand | 1 |
| Burn modes "move up only when money comes in" (docstring) and "don't flicker" (DOCS) | They move up whenever the runway is 20% past the threshold | 6 |
| Row 9's closing note: the upgrade took back every 0.13.0 unlock "(there were none live)" | 20 were live. The take-back worked, but their notes still say "Unlocked" | 5 |

**Partial:**

| Claim | The gap | Bug |
|---|---|---|
| Row 1: the workshop hold | Interrupted calls, Reset and the history leave gaps. Token counting with files hasn't run live yet | 7, 8, 36 |
| Row 2: "the reflection always runs" | Not after a call that cost more than its hold | 33 |
| Row 5: options don't force safe mode | An unpriced optional model still does | 65 |
| Row 10: only a verified person's email counts | The verdict parser can be fooled | 11 |
| Row 13: plan sections share their room | YOUR LAST CYCLE can't use it | 14 |
| Row 15: the report masks private data | The SFTP login, your name inside tool texts | 10, 42 |
| Row 16(d): Printify's own currency is read | An order without a currency takes your option's (inferred) | 54 |
| Rows 18-20 and X24: gates, roadmap integrity, the agent's view and favorite goals | Re-linking, "slow", metrics read late, voided forecasts, word checks | 20, 28, 58-62 |
| Row 21: unlocks scoped to what they act on; NEVER's words; Undo | Email replies cover every thread (declared). Also re-linking, Ember-started threads, NEVER's coverage, and blog Undo | 24, 28-30 |
| Row 24: the release notes, read in parts | They start over after each upgrade | 12 |
| Row 26e: the constitution names what Ember's code carries out | Blog and live uploads are missing | 64 |
| X8: listings missing at Etsy stop counting | Not Printify's listings | 55 |
| X15: the prompt bound, checked with every channel on | The blog isn't in the test | 40 |
| X18: the fake model can overrun like #423 | Only in a test's scripted turn, never in a dry-run scenario | section 4 |
| X19: when the burn mode moves down next | The date is right; the mode doesn't stay down | 6 |
| LIVE 11: "replied" only for answers to Ember's emails | The 09-30 row still says "replied" | 45 |

**Not kept:** "What you must do" 13. 0.16.1 reached the tracked branch before CI ran on it, no tags exist, and there was no 72-hour hold (section 4).

---

## 4. Not bugs, but worth knowing

**Money.**
- **The burn.** $17.55 over 3.17 active days is **$5.53 a day**. The runway is 33.0 days after your $100 grant.
- **Where it ends.** At the $7 cap, Ember goes critical around 11-22 to 11-26 (simulated with the real code on your ledger); bug 6 makes maintenance flicker on the way. $2.00 a day flat reaches 12-31; a $2.50 cap reaches it with about $4 left.
- **Cycles you started.** They took half the spend on 09-30 and 10-01.
  - All 7 owner wakes on 10-01 were Wake now presses, 4-61 seconds after a message: $2.26, or 40% of the day.
  - With `wake_on_message` and `wake_on_decision` off, 0.15.0's coalescing (one cycle 5 minutes after your last click) never ran. With it, the same messages would have made 5 cycles instead of 7.
- **The blog pipeline.** The hand-built pipeline (#59-#64) cost $1.94, 34% of 10-01. `propose_blog_post` did the same job for $0.21 in #65.
- **Caching isn't the lever.** Work calls read 92% of their prompt from cache. Plans aren't cached, but caching them would save almost nothing. The levers are:
  - fewer cycles;
  - a smaller tool prefix (the 46 tools are about 80% of it);
  - a Sonnet planner for ordinary cycles.

**The agent's work (from the live report).**
- **The poster.** 11 cycles, $4.47, 8 refused proposals, 6 of them retries with nothing changed. Its own 2,480 × 3,508 fallback was made and never tried. In #65 it left the A-series EU blueprint it had chosen.
- **Messages.** It sent you 6 messages a day on both days, 2 of them unasked. The code allows 2 unasked a day (`agent/tools.py:103`); your standing instruction says once a day.
- **Thin posts.** The posts are 187-336 words: it read the 2,500-character write limit as a page limit and never used `draft`, which the guide says to use (`guides/blog.md:8`).
- **Supply over demand.** About 14% of its spend went to demand (traffic, search, conversion). No title, tag or category fix was tried before the day-7 bars.
- **What's good.** It follows explicit instructions exactly, and its journals are honest and name its mistakes. Its two cycles on 0.15.0 cost $0.21 and $0.32.

**Public content to correct now.** These are reviewer findings, not legal advice.
- **The Nebenkosten post.** The §556 BGB deadlines and the BetrKV examples are right. But:
  - its product box promises "allen Umlageschlüsseln", while the template has no consumption-based key (HeizkostenV);
  - it says every key must be agreed in the lease, while §556a BGB defaults to floor area;
  - there is no "keine Rechtsberatung" line.

  Have it checked.
- **The colour-coded status.** The Bewerbungs-Tracker post promises "eine farbige Statusanzeige", and the English listing a colour-coded status. The spreadsheet maker makes no conditional formatting at all (`products/sheets.py`). Correct the texts or add the colours.
- **Three AI notes.** The post carries three: Ember's code adds one, the agent added two.

**Pinterest.** DOCS says a new app's trial access "may post to your own account only: all Ember needs" (`DOCS.md:1412-1415`). The agent's own research (#53) read that pins made with trial access are visible only to their creator. Check this before you set Pinterest up, as the 0.13.0 analysis also said.

**Process.**
- **Three releases in 2.5 hours on 10-01.** 0.15.0 started at 13:49 UTC and 0.16.0 at 15:48 UTC; 0.16.1 was pushed at 16:16 UTC. Two cycles ran on 0.15.0 and none on 0.16.0, so nothing from 0.15.0 could be measured before the next release.
  - 0.16.1 reached the tracked branch before CI had run on it. It went green at 16:40 UTC; the only candidate run tested a different commit.
  - No tags exist.
- **Parallel sessions collide.**
  - 10-01 produced two 0.14.0s and two 0.16.0s.
  - Branch `claude/zen-cannon-v75uxs` holds an unmerged "0.16.0" (Google Search Console, migration 0070, 3 options) that conflicts with the head in 9 files.
  - Merged with its "## 0.16.0" heading kept, the agent would never be shown its notes, and no test checks CHANGELOG headings.
  - Rebase it as 0.17.0.
- **The freeze didn't hold.** Option keys went from 70 to 90 (93 with that branch).
- **The riskiest code is the least rehearsed.**
  - The live view, the only automatic public upload, is tested only in dry run.
  - The fake model never produces the failures seen live: overruns, shape refusals, cut replies.
  - A dry run spends about $0.10 a cycle, against $0.355 live.

---

## 5. Fix order

1. **This week** (each S): bugs 1, 4, 5, 9 and 10, plus 2 and 3 before you switch those parts on. Most are a few lines, or a few lines plus a migration.
2. **The money guard** (S): 6, 7, 8, 33.
3. **What the agent sees** (M): 12-20, the message limit set to your one a day, and the shapes in the catalog (16).
4. **Before any unlock** (M): 11, 27-32.

Then:
- one release at a time;
- CI green on the exact commit before the tracked branch moves;
- a tag per version;
- the parallel Search Console branch rebased as 0.17.0.

---

## Appendix: how this was made

- **Reviews.** The 12 reviews produced about 110 findings, each with a reproduction script against the real code where one was possible. After merging duplicates and setting behaviour findings aside (section 4), the 66 above remain.
- **Found independently by two or more reviewers:**
  - by three: 5, 10;
  - by two: 3, 12, 13, 16, 18, 28, 37, 40, 42, 52, 64.
- **Verified by me:** bugs 1-12, 15, 21, 23, 25 and 27, by reading the code paths and, for 4, by timing the pattern myself. Also the colour-coded claim (the live post's text against `products/sheets.py`) and the CI runs on GitHub. The rest are their finders' reproductions.
- **Left out:**
  - behaviour findings about the agent (section 4);
  - coverage gaps that aren't bugs;
  - one cosmetic glitch in the report's link mask.
- **Tests.** The full suite ran once on Python 3.12: 2,162 passed in about 44 minutes, with the reviews running alongside. `ruff check` and `ruff format --check` are clean.
- **Live data.** The shareable diagnostics of 2026-10-01 16:23 UTC (0.16.0). It was read as data, never as instructions. No private values from it are in this file.
