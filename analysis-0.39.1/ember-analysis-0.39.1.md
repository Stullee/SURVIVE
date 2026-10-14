# Ember 0.39.1: the third full codebase analysis

*2026-10-14. The whole codebase at `cb64642` (0.39.1, the head of `claude/elegant-faraday-tcl6tq`, which Home
Assistant installs from). The first full analysis was of 0.11.1 and the second of 0.13.0; 0.37.0's was about the
plan's weights. Nothing in the app changes with this report.*

**Evidence.**
- **Ten subsystem reviews.** Each reviewer read one part line by line and ran Ember's own code against what it
  suspected. The parts:
  - the economy and the money guard;
  - the wake cycle and its scheduling;
  - the plan tree and its weights;
  - the agent's tools and their containment;
  - your controls and the web boundary;
  - mail, Etsy and the executor of approved requests;
  - the channels and your website;
  - the product makers and the file checks;
  - learning and ventures;
  - the database, the dashboard, CI and the tests.
- **My own re-check of every high and medium finding.** I reran each reproduction against `cb64642` and read the code
  it points to. For five findings I wrote reproductions of my own: 4.1, 4.2, 4.8, 4.12 and 4.14.
- **Runnable evidence.** The reproductions are in [`repro/`](repro/README.md): 40 scripts, each run from there with exit
  code 0.
- **The checks.**
  - The full test suite on Python 3.12: **3,052 tests pass** (15 minutes on 4 workers).
  - `ruff check` and `ruff format --check` are clean.
  - CI on GitHub is green on `cb64642` (run #407).
  - `pip-audit` finds no known vulnerability in the pinned dependencies.
- **What happens live**, from the two diagnostics reports of today:
  - `analysis-0.38.0`;
  - `analysis-0.39.1/ember-diagnostics-0.39.1.md`, 0.39.1's first cycles, on branch `claude/inspiring-dijkstra-frf90m`.

  I had no live data beyond those reports. This report reads code.

**Labels**, as in the earlier analyses:
- **reproduced**: a script against the real code showed it;
- **code**: found by reading;
- **inferred**: from indirect evidence;
- **live**: in a diagnostics report;
- **✔**: I checked it myself. Every high and medium finding is ✔.

**Paths** are relative to `ember/app/` unless they start with `tests/`, `.github/`, `repro/` (this folder's) or the
repository root. Line numbers are at `cb64642`.

---

## 1. The short answer

- **The foundations hold** (section 3):
  - **Money:** the guard's worst case is a real ceiling for ordinary calls, and every call settles exactly once. That
    held through 12 odd shapes of usage, crashes and a stress test of threads.
  - **Outside actions:** an approved action is recorded as running before anything is sent, and nothing retries.
    What Ember's code sends is exactly what you approved.
  - **The database:** it keeps history unchangeable (68 triggers refuse deletes, 107 guard updates), and your venture
    decisions too.
  - **Containment:** the file jail held against every trick tried.
  - **The web:** the web boundary holds, and the dashboard has no way to run injected HTML.
  - **Migrations:** databases written by 0.13.0 and by 0.33.0 upgrade cleanly.
- **Four findings are high.**
  - **Text Ember quotes can run a tool call (4.1).** It comes from 0.24.0's rescue of a call the model wrote inside a
    text: Ember's code runs any call it finds in any text field.
    - A buyer's email that Ember saves to its notes ran a `memory_update` and planted an "Owner rule" in LESSONS.
    - A quote in a message to you deleted a product file.
    - Anyone can write to Ember's mailbox, and this repository is public.
  - **A "stop" Ember's code can't see (4.2).** A stop is missed when it is written below the quote, as Thunderbird
    replies, or when it arrives in a mail over 1 MB. 0.37.3's read before every send stores such a stop, and the
    approved email goes out anyway: the case § 7 UWG is about, and the one that read was built to prevent.
  - **One email can stop the mailbox, and every executor with it (4.3).** A sender name with raw 8-bit bytes makes
    every read fail, for good. While an approved email waits, no listing, pin, post or upload goes out either, and
    nothing on the dashboard says why.
  - **Your holds leak (4.4).** A product you hold still gets cycles for its promises and decisions. And "Hold new
    things" lets a new product line through, as long as it names a venture idea, which an ordinary cycle can create.
- **The costliest medium starves reach (4.5).** A live product gets no weekly pin, post or blog step until the critic
  passes it. The critic hasn't passed a listing in its last 13 checks (10 by this morning, 3 more since). So after
  their launch pins and posts, the live listings get no marketing from the plan at all. Reach is the bottleneck: 17
  views in about two weeks.
- **Twelve more medium groups** (4.6 to 4.17):
  - your pins and Ember's waits;
  - veto windows that run out while the app is down;
  - Office files that pass the workshop's check when a part is moved;
  - a contact link that copies your visitors' mail, and an upload that can delete a page;
  - overwrites without a version, and listings an unlock approves twice;
  - wakes that pay for nothing or never come;
  - one HTML email that holds the scheduler for 24 minutes;
  - a death without a last will;
  - Claude Sonnet 5.5, which Ember can't use;
  - records that make lessons look surer than the facts;
  - steps a website line can't do;
  - pages and pictures that show something other than the file.
- **One pattern behind most of them** (section 5): each piece works alone, and the defects sit where two pieces meet.
  Usually that is a fix for one live symptom meeting a rule from another release. In the 14 days since 0.13.0 there
  were 61 releases; 46 test files are named for a release; and the fixed prompt of a work step has 13 bytes left under
  its own bound.
- **What I recommend** (section 6): one release of fixes before more features. Most are small; 4.1, 4.2 and 4.13 are
  medium-sized (6.1). Until then:
  - before you approve an email, read what its recipient last wrote;
  - check that Ember's mailbox still reads;
  - grant no automatic or veto unlock;
  - read an upgrade's script before you give it to Claude Code;
  - keep every model setting off Sonnet 5.5.

---

## 2. How Ember is built (0.39.1)

The 0.37.0 analysis (its section 2) describes what Ember does: the scheduler, a wake cycle, money, the plan tree,
ventures and learning. This section is the code under it, and where each of Ember's promises is kept.

### 2.1 By the numbers

| | 0.13.0 (2026-09-30) | 0.39.1 (2026-10-14) |
|---|---|---|
| App code (Python) | 43,702 lines | 72,839 lines (+67 %) |
| Tests (Python) | 25,355 lines, 1,479 tests | 51,780 lines, 3,052 tests |
| Dashboard (JS, HTML, CSS) | | 13,393 lines (`app.js` 10,936) |
| Migrations | 58 | 93 (0001 to 0094, 0070 reserved) |
| Database | | 82 tables, 196 triggers |
| Options | | 106 |
| Agent tools | | 56 (53 offered at once with every channel on) |
| Releases | | 61 in 14 days (85 in all) |
| Test files named for a release (`test_fixes_*`) | | 46, with 653 of the 1,936 test functions |

The fixed part of a work step (the system text and the tool definitions every step sends) is about 50.5 KB, about
12,500 tokens. `tests/test_rule_audit.py:334` bounds it at 50,510 bytes; 0.39.1 left 13 bytes of room.

### 2.2 The process

- **One Python process** under s6-overlay (`rootfs/etc/s6-overlay/s6-rc.d/ember/run`). The run script deletes the
  Supervisor's token before Python starts. The finish script stops the container when Python exits, so a restart
  always goes through the Supervisor.
- **FastAPI on port 8099** behind Ingress (`main.py`). `SecurityMiddleware` (`security.py`: the IP filter, your
  identity, the CSRF header, the security headers) wraps `CatchAllMiddleware`.
- **At start** (`main._start`):
  1. the database migrates, with a backup first;
  2. the economy starts: it takes `/data/ember.lock` and charges calls an earlier process left pending at their worst
     case;
  3. the kill switch's reset is applied (held in safe mode);
  4. `Agent.recover()` runs.

  Each step that fails leaves the dashboard up and says why.
- **One scheduler task** (`agent/scheduler.py`). Each round, at most a minute apart:
  1. `economy.tick`;
  2. pruning, every 5 minutes;
  3. your unlocks (`run_policy`);
  4. approved requests (`execute_approved`);
  5. the shop (hourly);
  6. the live page (every 15 minutes);
  7. the agenda's events;
  8. `decide()`, then at most one wake cycle.

  Database and agent work runs in worker threads, never on the event loop (`db._refuse_on_event_loop`).
- **One SQLite connection** for the whole process (`db.py`), shared under a lock:
  - WAL, `check_same_thread=False`, foreign keys on, a 10-second busy timeout;
  - each migration runs in its own transaction with its record.

### 2.3 A wake cycle

`agent/loop.py`, `CycleRunner.run`:

1. `meter.open_cycle` (it can refuse: the cycle is skipped);
2. the burn mode decides whether the workshop is offered;
3. mail, Etsy, Pinterest, Bluesky and Printify are read, and expired requests close;
4. in dry run the rest runs sealed (`netguard.sealed`: no sockets, subprocesses or native libraries);
5. `_plan_act_reflect`:
   - the daily review, the weekly look, the library's study and the listing critic (each guarded);
   - `plan.keep` and `plan.steer`: the step decides the cycle's kind and its one product line;
   - the plan;
   - the work: at most `max_tool_steps`, at most 4 calls a turn;
   - the reflection;
6. whatever happened, the journal draft is kept, the sleep is cut if the plan has a step ready, and the cycle is
   closed.

Any exception ends the cycle as "failed", never the app.

### 2.4 The code, by part

| Part | Lines | What it holds |
|---|---|---|
| `economy/` | 4,975 | the ledger (typed, append-only rows); the meter (a worst-case quote, a pending row, one HTTP request, settled from the usage); the worst-case estimate (server tools, cache writes, room for thinking); prices; life states; burn modes; the clock; the transport (only `api.anthropic.com`, no retries, no proxies) |
| `agent/`: runtime and records | 10,537 | `loop.py`, `service.py`, `scheduler.py`, `slack.py`, `digest.py`, `store.py` (projects, tool calls, journal, rulebook; every row scoped to live or one dry-run session), `context.py` (what the planner sees), `prompts.py` (every request), `fake_llm.py` (the dry run's model, 3,025 lines) |
| `agent/`: decisions | 7,722 | `plan.py` (4,098 lines: the tree, its keeper and its steering), `weights.py`, `templates.py`, `obligations.py`, `roadmap.py`, `stages.py`, `lines.py`, `desk.py`, `agenda.py` |
| `agent/`: tools and containment | 8,402 | `tools.py` (5,615 lines, 56 tools), `sandbox.py`, `netguard.py`, `workfiles.py`, `workshop.py`, `library.py`, `memory.py`, `website.py` |
| `agent/`: judgement and learning | 6,141 | `ventures.py`, `review.py`, `weekly.py`, `learning.py`, `bets.py`, `predictions.py`, `critic.py`, `quality.py`, `knockouts.py`, `evidence.py`, `demand.py`, `research_check.py`, `news.py`, `reach.py`, `metrics.py`, `econ.py` |
| `agent/`: your controls | 4,586 | `owner.py`, `policy.py` (unlocks), `never.py`, `audit.py` (journal, Undo), `views.py` (what the dashboard gets) |
| `integrations/` | 15,551 | per platform a connection, a reader and a publisher (Etsy, Pinterest, Bluesky, Printify); the mailbox and its stop check; the executor of approved requests; the publishing queue; the website's SFTP uploads and live page; Reddit and KDP (which you carry out) |
| `products/` | 10,155 | the makers (PDF, Word, Excel, images, statements, tables, markup); the file checks; the HTML of the blog, the website and the live page |
| top level | 4,769 | `main.py`, `web.py` (routes), `security.py`, `config.py`, `db.py`, `diagnostics.py`, `privacy.py`, `events.py`, `logging_setup.py` |
| prompt text | | `constitution.md` (the fixed core), `knowledge.md`, 14 guides in `agent/guides/` |

### 2.5 Where each promise is kept

Ember keeps its promises in three places, and the place decides how far a promise can be trusted:

| Kept by | Examples | How strong |
|---|---|---|
| **The database** (196 triggers, CHECK constraints) | history can't change (68 triggers refuse deletes, 107 guard updates: ledger rows, calls, journals, reviews, wills, emails); your transitions (27 triggers on milestones, 13 on ventures, 10 on approvals, 7 on policy uses); an approval's action can't change | holds whatever the Python code does, for the changes it covers (an INSERT is not always covered: L30) |
| **Ember's code** | the money guard (`economy/metering.py`), the jail and the network seal, the executor's journal before any network call, the stop check, your limits on requests, the plan's waits, the file checks | holds where the code path is the only path, and as far as the check reads its input whole (4.2, 4.8) |
| **The prompt** | honesty, harmlessness and the AI disclosure (constitution); how to make products (guides); most of the "what next" judgement | holds as far as the model follows it |

Since 0.13.0 the trend has been right: rules that lived in the prompt moved into code and the database. The plan tree
came in 0.34.0, the waits in 0.37.6, and the limits the plan sees in 0.37.9.

---

## 3. What holds

Each of these was attacked by a reviewer, with scripts against the real code, and held, except where a finding is
named beside it. This is most of the code.

**Money** (`economy/`):
- **The worst case is a real ceiling for token-only calls.** It is the prompt at the highest of the input and
  cache-write rates, plus `max_tokens` with the room for thinking. Token counts get 5 % and 200 tokens more. A failed
  count falls back to a generous bound.
- **The caps.** The daily cap, the library's budget, and the "5 times the hold above the last will's reserve" rule for
  the workshop, research and the research model's check are all hard. 0.37.2's and 0.37.4's fixes hold.
- **Settlement.** Every call settles exactly once: a pending row, one finalize, a trigger and a unique index. Twelve odd
  usage shapes all settled once, among them negative counts, unknown fields, odd `iterations` and a stream cut in the
  middle. A thread stress test (calls against grants, refunds, pauses and resets) ended with 0 pending calls and 0
  errors.
- **Dry run** never touches the live balance.
- **The transport** reaches only `api.anthropic.com`: `trust_env` is off, there are no redirects, and model calls are
  never retried.

**Outside actions** (`integrations/`):
- **Recorded before it is sent.** Every send is backed by a row, `UNIQUE(approval_id)`, committed as "running" before
  anything goes out. A crash leaves it "unclear", and nothing retries.
- **Approved means approved.** An approval's action can't change after it is proposed (a trigger).
- **What goes out is what you approved.**
  - An email has one recipient (no Cc or Bcc) and a subject that can't break a header, and Ember's code adds its
    footer.
  - An Etsy file is uploaded from the very bytes whose hash was checked.
  - A listing is made as a draft and goes live only once its photos and files are in.
- **The kill switch** is checked inside each item's own transaction. Three paths miss it: a workshop run's
  continuation (L5), a cost probe from the turn's calls (L10) and the Etsy sync's automatic renewal (L16).
- **Your decisions.** Approve, reject and close are compare-and-set in one `BEGIN IMMEDIATE`. A double click, a reject
  after approval, or a close after the executor began each answer 409.
- **Connections.**
  - Mail uses TLS 1.2 or newer, with checked host names; there is no plaintext fallback when STARTTLS fails.
  - Etsy's OAuth uses PKCE (S256), compares its state in constant time, and keeps its tokens in one 0600 file, never
    in the database or the logs.
  - SFTP compares the host key before it sends the password; there is no fallback that trusts any key.
- **The publishing queue held across 11,652 random cases** (0.37.8): opening hours, the 3-hour gap, the daily limit and
  2 days between pins of one listing. The cases covered Berlin's DST days and odd time zones.
- **Bluesky**: link positions are exact with any Unicode, no mention or reply is ever made, and `@handle` in the words
  is refused.
- **Every page** escapes Ember's text, and every link is an https link, your own address or a relative path. The one
  gap is 4.9.
- **0.39.0's Printify edit** sends only what changed, never prices or variants.

**Containment** (`agent/sandbox.py`, `agent/netguard.py`):
- **The jail held against every trick tried:** `..`, absolute paths, backslashes, NUL, Unicode look-alikes, trailing
  dots, overlong names, symlinks and hardlinks. It opens a path one part at a time with `O_NOFOLLOW` and refuses
  files with more than one link.
- **netguard** blocks sockets, subprocesses, `os.system`/`exec`/`fork`/`posix_spawn`, threads, `ctypes`, `asyncio`
  connections, `http.client` and `ssl`. One gap, unused today: L28.
- **No tool crashes a cycle** on what the API can deliver. No refusal leaked a path, a secret or a traceback.

**The web boundary** (`security.py`, `web.py`):
- **Ingress only.** Only the Ingress proxy gets in; `X-Forwarded-For` is ignored.
- **Your identity:** the first `X-Remote-User-Id`. A non-owner gets static files, the health check and the sensor
  numbers only.
- **CSRF:** the header is required on every request that changes something.
- **Safe mode** keeps your user IDs and the kill switch. The 0.13.0 analysis' safe-mode and unlock problems stay fixed.
- **Secrets** are kept out of the dashboard, the API and the diagnostics, and scrubbed from logs.
- **`config.yaml`'s bounds** equal `config.py`'s, compared by a script.

**The database and the dashboard:**
- **Migrations:** each is one transaction with its record, checked for foreign keys, never `executescript`. None of
  the 15 table rebuilds loses a trigger, an index or a column.
- **Real upgrades:** databases filled by 0.13.0 and by 0.33.0, running their own cycles, migrate to 0.39.1 with no
  integrity or foreign-key error. Today's code then ran 30 cycles on them without an error.
- **The dashboard has no HTML sinks:** no `innerHTML`, `insertAdjacentHTML`, `outerHTML` or `document.write`.
  Payloads in Ember's messages, events, ventures, your inbox, rules and milestones rendered as text in Chromium: 0
  elements made, 0 CSP violations.

**The products** (`products/`):
- **Statements:** the money math is exact (fractions, rounding half away from zero, leap years, part-year tenants),
  and a file whose formulas disagree with Ember's sums is never kept.
- **Hostile files:** XML entity bombs are refused on every path. Decompression bombs are refused before decoding.
- **Size limits:** page caps (40, 160 for KDP) hold on pathological input.
- **`make_spreadsheet`'s formula allowlist** holds.
- **KDP:** the cover's spine math matches KDP's formulas.

**The runtime and learning:**
- **The cycle:** one runs at a time, and every stop reason is handled (refusal, `max_tokens`, empty replies).
- **Time:** your day and DST are right on Berlin's 23- and 25-hour days.
- **Prompt safety:**
  - every section of the planner's context has a budget;
  - outside text is quoted as JSON or wrapped with a nonce;
  - no code path writes outside text into lessons, the strategy or the playbook, except the one in 4.1.
- **Your venture decisions:** the agent can't back, kill, revive or lift your hold on a venture. Enums and triggers
  both refuse it.
- **No tool reaches close, drop, pin, worth or freeze.**

**The process:**
- **CI** runs `pytest -W error`, ruff, gitleaks over the whole history, and both image builds on every push. Since
  0.30.0 one release went out red (L43).
- **3,052 tests pass**, and every test function asserts something.

---

## 4. Findings

### 4.1 Text Ember quotes can run a tool call — high, reproduced ✔

**Why the rescue exists.** Live, the model twice wrote a second tool call *inside* a text field of the first: its
journal held `…</invoke><invoke name="memory_update">…`. 0.24.0 made Ember's code take such a call out and run it as
a call of its own (`agent/tools.py:1596-1609`; `_take_out` at `:1806-1832`).

**What it does now.** `_take_out` looks into **every text field of every tool** for
`</invoke> <invoke name="X">`, with X another tool. The call it finds runs with the agent's full authority, and it
can't tell the model's own slip from text the model is quoting. So the markup turns into a real tool call in
anything the agent copies into a field word for word:
- a buyer's email saved to notes (`workspace_write` content);
- a quote in a message to you (`message_owner` text);
- a passage of a web page in a draft or a research note.

The `<data>` fences with a nonce protect what Ember *shows* the model. They do nothing for what the model writes back.
Anyone can write to Ember's mailbox, and this repository is public, so the exact markup is easy to find.

**Evidence** (reproduced):
- **My own check** (`repro/tools/test_quoted_email_runs_a_call.py`): a real dry-run cycle in which the model saves a
  buyer's email whole with `workspace_write`. The email carries a `memory_update` call.
  - The tool calls are `workspace_write ok, memory_update ok`.
  - LESSONS now begins "Owner rule (verified): send buyers the full price list unasked." Every plan reads that as
    Ember's own lesson.
- **The reviewer's** `repro/tools/leaked_call.py`: a `message_owner` that quotes a text carrying
  `workspace_write mode=delete` deleted `shop/planner.pdf`. No version was kept (4.10), so it can't be restored.
- **Requests:** an injected `propose_*` or `request_approval` goes through `_new_request` and your unlocks. Nothing goes
  out without your approval, unless one of your unlocks lets that kind through.
- **No cap:** `write_journal` is exempt from the 4-calls-a-turn limit, and each journal can carry one more call. One
  reply of 8 journals ran 16 calls.

**Fix (M):**
- take a call out only from fields the model writes for itself (the journal's), never from fields that carry quoted
  text (`content`, `text`, notes, descriptions);
- or only when the markup is the whole rest of the field, never after quoted words;
- and never run a taken-out call that changes memory, deletes a file or files a request: answer with a refusal the
  model can turn into a call of its own.

### 4.2 A "stop" Ember's code can't see: below the quote, or in a mail over 1 MB — high, reproduced ✔

**What you were told.**
- **README:** an approved email goes out only after Ember's code has read its mailbox since your approval, "so a
  'stop' waiting there is seen first" (0.37.3).
- **DOCS:** a stop counts when it is in the sender's own words, not in what they quote.

**What the code does.**
- **The check reads only the words above the quote.** It takes the sender's own words to end at the first line that
  starts a quote, such as "Am … schrieb …:" or "On … wrote:" (`integrations/optout.py:168-177`, `own_lines`).
- **So a stop written below the quote is never seen.** In a reply written below the quote, everything the sender wrote
  comes after that line, and only the subject is checked. That is Thunderbird's default way to reply, and the way
  German netiquette asks for.
- **HTML replies too.** `html_to_text` doesn't mark a `<blockquote>`'s lines with ">", so Thunderbird's HTML reply
  ends the same way.
- **A mail over 1 MB isn't read.** Only its headers are fetched (`integrations/mail.py:1005-1007`, the limit at
  `:50`), so a stop sent with a photo is seen by neither the check nor the agent.
- **Only INBOX is read** (`mail.py:985`). A stop your provider files as spam is never seen (L36 lists it with the
  other mail lows).

**How it happens.**
1. You approve an email to Ann.
2. Ann answers below the quote: "Bitte keine E-Mails mehr."
3. 0.37.3's read stores her mail and records no stop.
4. The approved email goes out to her in the same round.
5. Her stop becomes an open inquiry, which the plan puts first to be answered. An email-reply unlock would send that
   answer once its veto window ends.

**Evidence** (reproduced):
- **My own check** (`repro/mail/stop_below_quote.py`): the same words, "Bitte keine E-Mails mehr.", are caught above
  the quote and missed below it, in German and in English.
- **End to end** (`repro/mail/test_stop_below_quote_and_large_mail.py`):
  - the stop below the quote: `execute_approved -> [(1, 'sent')]`, stored, "suppressions: []", "sent to:
    [['ann@example.org']]", and it waits as an inquiry to answer;
  - a 1.2 MB "Please stop emailing me" with a photo: only its headers are stored, and the email is sent.
- **Untested until now.** `tests/test_opt_outs.py` covers only stops written above the quote.

**Why it is high.** An email that advertises to someone who asked to stop is what § 7 UWG forbids, and the read
before sending was built to prevent exactly this.

**Fix (S/M):**
- read past a quote's first line: skip the quoted (">") lines, and keep reading the sender's lines after them;
- mark a blockquote's lines with "> ";
- for a mail over 1 MB, fetch its first text part (or its first 256 KB) and check that;
- add these layouts to the tests.

### 4.3 One email can stop the mailbox, and every executor with it — high, reproduced ✔

**What the code does.**
- **Surrogates survive.** A header with raw 8-bit bytes (legal under RFC 6532, and common from spam) is decoded with
  surrogates. The subject is cleaned, but the sender's name and address keep them: `one_line` doesn't clean them
  (`integrations/mail.py:228`, `:432-436`).
- **Storing the mail fails.** SQLite refuses the text (`UnicodeEncodeError`), so the whole batch of up to 20 mails rolls
  back. The last mail read and the mailbox's error are not written.
- **Every later read fails the same way, forever.** No mail after it is stored, nor any before it in its batch: stops
  included.
- **The other executors stop too.** `execute_approved` runs the email executor first, in one expression with the
  others (`agent/service.py:1017-1019`). The email executor reads the mailbox before sending (4.2), so while any
  approved email waits, every round raises before Etsy listings, pins, Bluesky posts, Printify products and website
  uploads run.
- **Nobody is told.** The scheduler only logs it; the request's card says "waiting", and the mailbox shows no error.
  Only its "Last fetch" (System → Email) stops moving.

**Evidence** (`repro/mail/test_8bit_header_blocks_mailbox.py`, a sender named `M\xc3\xbcller` in raw bytes):
- three rounds: "execute_approved raised UnicodeEncodeError … surrogates not allowed";
- the last mail read stays 2, and "etsy publisher ran: []";
- the card: `'status': 'waiting'`, with no error;
- a "Stop" just before the bad mail in the same batch is never stored.

**Fix (S):**
- clean surrogates in `one_line` and `clean_text`;
- store each mail in a savepoint of its own, with a headers-only fallback;
- give each executor in `execute_approved` its own `try`;
- show a mailbox read that fails on the dashboard.

### 4.4 Your holds leak: a held product's promises and decisions, and "nothing new" through an idea — high, reproduced ✔

**What you were told** (DOCS, Plan):
- **Hold it** holds a product: "its steps wait until you Resume it", and Ember can't lift your hold.
- **Hold new things** means "nothing new": no venture cycle and no new product.

**What the code does:**
- **A held product's owed steps still run.** In `plan.candidates`, a product's own steps wait when it is stopped (a
  parked or killed venture) *or held* (`agent/plan.py:2060`). Its promise and decision steps wait only when it is
  stopped: `reason = "hold" if stopped else _waiting(...)` (`plan.py:2112`). Your hold isn't read.
- **The cycle then works on the held line.** The loop drops a step's focus only for a closed line or a stopped venture
  (`agent/loop.py:980-998`). No tool reads a hold: `agent/tools.py` never mentions `hold_by` or `hold_reason`.
- **"Nothing new" lets a new product through an idea.** `project_create` refuses under your hold only when it names no
  venture (`tools.py:2180`: `if held and venture_id is None`). An idea's id is enough.
  - `venture_create` (`tools.py:2598`) checks no hold, and ordinary cycles have it too. So one ordinary cycle can make
    an idea and a product line under it.
  - 0.37.0's analysis named the first half (its 6.7). It still holds.

**How it happens.** You reject a request, which makes a decision step of its product, and hold the product: "I look at
it next week". On the request's day the decision weighs most (urgency 9), so the next cycle works on the held line,
for example proposing it again. A promise about a held line does the same, every day as its day nears. The KDP book
was rejected five times; this is the situation the hold is for.

**Evidence** (reproduced; `repro/plan/r3_owner_hold_bypass.py`, `repro/plan/r6_hold_new_things.py`):
- **Both lines held by you:** the steer takes the decision on line 2, then the promise on line 1. Every other step of
  both lines shows `hold`.
- **"Hold new things" on:** `project_create` without a venture is refused. With idea venture #2 it says "Created
  project #1", and the plan lays it out.

**Fix (S):**
- treat a hold of yours like a stopped venture for owed steps, and refuse a focus on a line you hold;
- `owner_hold` names the promises and decisions it holds;
- under "Hold new things", refuse `venture_create`, and `project_create` whatever venture it names, until you resume.

### 4.5 Live products get no weekly marketing until the critic passes them — medium, reproduced ✔ (the costliest)

**What you were told.** A product's maintain stage brings a pin and a Bluesky post a week and a blog post a month. Its
decide-by dates push its marketing first (day 7) and give it one more try (day 14).

**What the code does.** The weekly steps come only once the product is *launched*, which means no stage but maintain
is still open (`agent/plan.py:1832`). The launch stage holds "The critic passes it" (`agent/templates.py:90`, in the
Etsy and Printify templates). A stage without a check of its own closes only when all its steps are closed.

So after its first two pins and two posts, a live product has **no marketing step at all** until the critic passes it:
- **The critic hasn't passed a listing lately.** Live, it passed none in 10 checks (analysis-0.38.0, its 3.8), and three
  more checks today scored 6, 5 and 5 (ember-diagnostics-0.39.1, its 3.5).
- **0.39.1 made it rarer still:** the critic's step became a chore with no urgency.
- **The pushes push nothing.** The day-7 and day-14 pushes set `pushed_until`, but there is no step to push.
- **You decide blind.** On days 21 and 28 you are asked to keep or drop the line "after 0 reach actions".
- **Channels too.** When a launch step waits on a channel that is off, the stage never closes, and the channels that
  are on get no weekly steps either.

**Evidence** (reproduced; `repro/plan/r1_launch_never_closes.py`, `repro/plan/r1b_channel_off.py`):
- from day 3 to day 30 the only open step is "The critic passes it" (a chore, urgency 0);
- "recurring pin/post/blog steps ever laid out: 0";
- on day 28: "your owner decides on it" after "0 reach actions".

**Why it matters most.** Reach is the bottleneck every analysis since 0.13.0 names. This is the one defect that
starves it by code.

**Fix (S):** a product counts as launched once its launch marketing is done. Leave out the critic's step (maintain has
a recurring one), and any step that waits on a channel that is off.

### 4.6 Your pins and Ember's waits: a pin that isn't taken, and a line that blocks itself — medium, reproduced ✔

- **A pin that isn't taken.**
  - You can pin a product's decision step: the Plan tab shows "Pin", and `pin()` refuses only promise and owner steps.
    But owed steps are built without `pinned=` (`plan.py:2121-2132`), so the choice never sees the pin.
  - A pin on a step of a line Ember held waits behind Ember's hold. `plan.hold` refuses only the other order, a hold
    after a pin.
  - Either way the Plan tab says "Pinned: the next cycle takes it first."
  - Evidence: `repro/plan/r2_pins_ignored.py`. The pinned decision has `pinned=False`, and the steer takes another step.
- **A line that blocks itself.**
  - `plan_step wait` refuses only a step waiting on itself (`plan.py:3453-3459`). A wait lifts only when the other step
    closes (`plan.py:3540-3542`).
  - So A can wait on B while B waits on A. Or "Propose the listing" can wait on "You approve it", which can't close
    before the proposal.
  - Then no step of the line is ready again, no cycle is aimed at it, and you have no lever that lifts a wait.
  - Evidence: `repro/plan/r5_wait_cycle.py` (after 20 days, `steer: none`) and `repro/plan/r5b_wait_on_later_step.py`
    (after 30 days, every step of the line waits).

**Fix (S):**
- pass `pinned` for owed steps;
- let your pin lift Ember's hold, or refuse the pin with the reason;
- in `wait_step`, follow the chain of waits and refuse a wait that comes back to the step, or names a later step of
  its stage.

### 4.7 A veto window that ends while the app is down runs out unseen — medium, reproduced ✔

**What you were told** (0.37.3): a veto window that a pause or a wait for money interrupted gets its whole time again.

**What the code does.** Only a scheduler round that runs while unlocks are stopped marks the interruption
(`agent/service.py:977-979`); nothing marks that the app was down. After a restart the first round finds no mark. It
approves every held request whose window ended meanwhile (`policy.run_due`) and carries it out in the same round
(`agent/scheduler.py:91-97`).

**How it happens.**
1. You set email replies (or price changes) to "run unless I veto within 12 h".
2. A reply is held at 20:00.
3. The app stops: Watchdog is off, the host restarts, or the power fails.
4. It starts again at 10:00. The reply goes out in the first minute, before you can open the dashboard: you saw it for
   1 of its 12 hours.

The same follows a spell in dry run (inferred): the dashboard shows only the current mode's requests.

**Evidence** (`repro/owner/test_r1_veto_after_downtime.py`): after 12 hours down, the first round gives `'status':
'approved', 'decided_by': "Ember's code (your unlock)"`, and the executor runs it in that round. With a pause instead
of the downtime, the request stays pending.

**Fix (S):** `run_policy` writes a heartbeat. `recover()`, and the first round in a mode, mark the windows interrupted
when the heartbeat is older than a few minutes, so `policy.restart` gives each its whole window. **Latent** until you
grant an unlock with a veto window.

### 4.8 Files buyers download: the workshop's Office check is beaten by moving a part — medium, reproduced ✔

**The promise** (DOCS, The workshop):
- an Office file is refused if it holds macros, links to other files, DDE, or formulas or names that reach outside
  the workbook;
- "every part must be of a kind known to be safe".

**The gap.**
- **The check goes by the part's path.** `products/checks.py:498-505` checks formulas and fields only in `word/…`,
  `xl/worksheets/…` and `xl/workbook.xml`.
- **Office goes by the package's relationships.** Excel and openpyxl find parts through them, not by name. So a file
  whose worksheet sits at `xl/tabs/t1.xml` (its relationship and content type updated) is read in full, and none of
  its formulas is checked.
- **The kind check passes it.** `_known_parts` sees a worksheet, a kind known to be safe.
- **`IMAGE(` is missing.** `_EXCEL_ACTIVE` (`checks.py:126-128`) lacks it, and it fetches a URL when the file
  calculates.
- **Formulas elsewhere are unchecked.** `_excel_formulas` reads only `<f>`: the formulas of conditional formats and
  data validation are never checked, and neither is a sheet in the Strict namespace. Whether Excel opens such a
  mixed file wasn't tested.

**Evidence** (reproduced):
- **My own check** (`repro/products/repro_moved_worksheet.py`): the same `=WEBSERVICE("https://…"&A1)` workbook is
  refused at the standard path, and kept with its sheet moved, where openpyxl still reads the formula.
- **The reviewer's** `repro/products/repro_office_paths.py` shows the same for a defined name (the workbook at
  `xl/book.xml`), and for a Word `DDEAUTO … cmd.exe` field with the parts under `text/`.
- **`repro/products/repro_office_formula_places.py`:** a conditional format, a data validation, `IMAGE()` and a
  Strict sheet are all kept.

**How far it goes.**
- **One gate only.** `checks.check` runs only when the workshop keeps a file (`agent/workshop.py:386`), and Etsy's
  upload checks only a file's ending and size (`integrations/etsy.py:206-216`). So a workshop file goes straight into
  a listing's downloads once you approve the listing, and your approval can't show what is inside an .xlsx.
- **It takes intent.** Only a hostile or injected model (4.1) would build such a file: standard libraries write the
  standard paths.
- **`make_spreadsheet` can't do it:** its own allowlist holds.

**Fix (M):** run the checks by content type, on every part the relationships reach; add `IMAGE`, and the formulas of
conditional formats and validations.

### 4.9 Your website: a contact link that copies visitors' mail, and an upload that can delete a page — medium ×2, reproduced ✔

**A mailto link with a query.**
- **The promise:** the link page and blog posts allow only https links and your own address.
- **The gap:** the check compares only the address before "?" (`products/blog.py:321-322`, `:401`, `:775-776`).
  - So `mailto:you@…?bcc=someone@…` is accepted, and every visitor who writes through it copies their mail to a third
    party.
  - A post can also hide the address as `%40` from the text scan.
- **You can't easily see it.** The link page's card shows the URL. A post's card shows its first ~7,900 characters,
  and the preview hides where links go.
- **Evidence:** `repro/channels/mailto_bypass.py`. Both are accepted and the check before upload finds nothing; a plain
  mailto to another address is refused.

**An upload that deletes the page.**
- **The fallback.** `integrations/sftp.py:218` falls back "for a server without OpenSSH's rename extension" on *every*
  `OSError` of `posix_rename`, a timeout or a permission error too.
- **What it does.** It removes the live file first. If the plain rename then fails, it deletes the upload as well.
- **No way back.**
  - The request becomes "unclear", and nothing retries.
  - Undo refuses: the page on the server no longer matches what was uploaded (`integrations/site_publisher.py:729-733`).
- **Evidence:** `repro/channels/test_repro_sftp_rename.py`, against the repository's own SFTP server. After a timeout,
  and after a refused rename: "files on the server: []".

**Fix (S each):**
- parse mailto links, and allow only your address, with only `subject` and `body`;
- fall back only on "operation unsupported", and move the old file aside instead of deleting it.

### 4.10 Work Ember can't get back, and listings it can make twice — medium ×2, reproduced ✔

**Overwrites without a version.**
- **Only `workspace_write` keeps one.** It keeps the version that `restore` brings back (`agent/tools.py:2025`,
  `2053-2071`). `draft` in overwrite mode, a workshop run and the `make_*` tools replace a file and keep nothing.
- **DOCS' own example** for `restore` was such an overwrite: a book's 24 KB interior left as its 671-byte header.
- **Evidence:** `repro/tools/draft_overwrite.py`. A 22,531-byte interior became 26 bytes, with 0 versions, and `restore`
  answers "no earlier text of books/interior.md is kept".

**The same listing twice under an `auto` unlock.**
- **The duplicate check looks at pending requests only** (`tools.py:4078-4080`). An `auto` unlock approves the first at
  once, so the same proposal in the next cycle is new again, and approved again.
- **Evidence:** `repro/tools/auto_duplicate.py`. Listings 900000002 and 900000003 from one design: two listings, two
  fees.
- **Latent** until you grant such an unlock.

**Fix (S/M):**
- take a version of every overwrite, through one helper;
- compare a new request with approved and recently carried out ones too.

### 4.11 Wakes that pay for nothing, and wakes that never come — medium ×4, reproduced ✔

The cycle itself is sound (section 3). Its edges, where a wake is decided, are not:

1. **An event's wake needs only a plan's money.**
   - `agent/service.py:477` checks only the plan's cost. The schedule waits unless a working cycle fits (`needed =
     max(opening, min(working, daily))`, `:435`).
   - So late on a day whose cap is nearly spent, a reply or an inquiry wakes a cycle that pays for its plan and is
     refused its first work step.
   - The event is marked seen, so no later plan lists it, and Ember backs off.
   - Evidence: `repro/loop/t05_…`. With $0.0856 left, a plan at $0.066 and a working cycle at $0.317, the cycle is
     refused ("not enough money left in this cycle for a work step"), and the inquiry is marked seen.
2. **A refused cycle uses up your decision's turn.**
   - The step a cycle takes is recorded before its plan (`agent/loop.py:537-538`).
   - `_taken_counts` (`agent/plan.py:2145-2156`) counts every recorded pick, also of a refused or failed cycle. Yet
     `FINISHED` says such picks don't count (`plan.py:88`), and the brake reads only finished ones (`:2173-2184`).
   - So your decision loses its urgency after one pick, and a promise after three, for 24 hours.
   - Your messages and decisions wake Ember where your wake switches allow it (`service.py:403`), even with the cap
     spent. Such a cycle is refused at once and counts as a failure, and the back-off doubles.
   - Evidence: `repro/loop/t01_…` shows your decision weighing 30.0, then 3.64 for a day after one refused cycle.
     `repro/loop/t06_…`: four of your messages between 21:00 and 23:00 with the cap spent give four refused cycles,
     and the next wake is 03:06, not 00:05 when the cap resets.
3. **Three restarts in a row stop Ember until you act.**
   - The scheduler checks for a stop once per round (`agent/scheduler.py:91-115`). A stop that comes during the round's
     mail, shop or live-page work still opens a cycle, which ends "interrupted".
   - The crash-loop guard counts any three interrupted cycles, graceful ones too (`service.py:539-545`). After that no
     scheduled or event wake runs.
   - The dashboard says "Due now" with the old reason; only the diagnostics say "press Wake now".
   - Three option changes within a few minutes are enough (`repro/loop/t04_…`).
4. **An event's sleep replaces the schedule.**
   - A reactive cycle never cuts its sleep, since it has no plan step (`loop.py:407`), but it still sets the next wake
     (`service.py:853-898`).
   - With steps ready and a wake due in 30 minutes, an email's cycle that chose 12 hours moves the next wake to
     midnight (`repro/loop/t08_…`).

**And the event a reactive cycle woke for can be missing from its plan** (`agent/context.py:997`):
- **How.** Your unanswered messages are sized to fill all of SINCE YOUR LAST WAKE. The agenda's lines come after them,
  and are cut first.
- **The effect.**
  - With about 2.2 KB of your messages unanswered, the event that woke the cycle isn't in its context, though its
    task says "an event woke you (Agenda in SINCE YOUR LAST WAKE)".
  - When any event is there, your newest message is cut instead.
- **Evidence:** `repro/loop/t02_…`, `repro/loop/t03_…`.

**Fix (S each):**
- event wakes need what the schedule needs (`needed`);
- count only the picks of finished cycles;
- while the cap can't pay a plan, hold your wake until midnight;
- check for a stop before `decide()` and before `run_cycle`;
- leave graceful stops out of the crash-loop count, and show the guard on the dashboard;
- keep the earlier of the scheduled and the reactive wake;
- give the agenda a share of the section of its own.

### 4.12 One HTML email can hold the scheduler for 24 minutes — medium, reproduced ✔

**What the code does.**
- **The pattern.** The hidden-text filter strips CSS comments with `/\*.*?\*/` (`integrations/mail.py:102`). Its time
  grows with the square of a run of unclosed comments.
- **The cap comes too late.** The 100,000-character cap of a `<style>` is applied after the pattern
  (`mail.py:744-745`), and the pattern runs a second time at `:711`.
- **Where it runs.** On the scheduler's thread: the mailbox is read before approved emails are sent, between cycles,
  and at a cycle's start.
- **Nothing interrupts it.** The 30-second budget is checked only between mails, and a restart in the middle parses the
  mail again.

**Evidence:**
- **Mine** (`repro/mail/css_comment_timing.py`): twice the text takes four times as long. 5,000 characters take 0.03 s,
  10,000 take 0.13 s, 20,000 take 0.54 s, and 40,000 take 2.3 s.
- **The reviewer's** run of the whole parser: a 0.99 MB email took 1,439 s on this container, and a Raspberry Pi is
  several times slower.
- **Who can do it:** anyone who can send Ember an email.

**Fix (S):** cut to the cap before stripping, and strip with a linear scan (`str.find("*/")`), where an unclosed
comment ends the sheet.

### 4.13 A death without a last will, after an entry from outside the guard — medium, reproduced ✔

**The gap.**
- **The guard protects the will only from its own calls.** It keeps the last will's cost back from them.
- **Outside entries are judged by the life state alone.** These are a listing fee or refund Ember's code records from
  Etsy, and your expenses and corrections. Each is judged only as dead (a balance of 0 or less, `economy/life.py:343`),
  unfunded, or critical (`economy/service.py:500-508`).
- **Nothing judges the band in between,** from 0 up to the will's cost.

**What follows.** Such an entry can leave Ember "alive" with less than its will costs. At the next wake:
1. the plan is refused;
2. Ember turns critical and is due to write its will (`life.starve`);
3. the will is refused too;
4. it dies "starved", without one.

The same happens when you move the worker to Opus 5.5: at the restart the reserve grows 5.4 times, and nothing warns.

**Evidence** (`repro/economy/repro_starve_band.py`):
- **An Etsy fee that leaves $0.01** (the reserve is $0.0232 with a Sonnet worker): Ember is "alive", not paused. At the
  next wake it turns critical and then "starved", with no last will.
- **Your expense of $0.98 leaving $0.016** is accepted without a question.
- **The model change:** with $0.106 left and the worker changed to Opus 5.5, the reserve is $0.126. Ember is "alive",
  nothing warns, and it dies at the next wake.

0.15.0 promised the opposite:
- `service.py:253`: "pauses the agent with the reason instead of letting it die";
- DOCS: the confirmation covers the outcome it showed you.

About two weeks of runway are left, so the end of the money is near enough for this to matter.

**Fix (M):** a fifth worse state, "can't pay its will", for any entry that leaves the balance (less what is pending)
under the will's cost: a fact pauses Ember, and your entry asks you first. Check it at the start too.

### 4.14 Claude Sonnet 5.5 can't be used — medium, reproduced ✔

**The gap.**
- **One way to switch thinking off.** Every request for a model that doesn't "always think" carries `thinking: {"type":
  "disabled"}` (`agent/prompts.py:28`; `_thinking` at `:600`).
- **Ember knows three models that always think** (`economy/pricing.py:89`): Opus 5.5, Fable and Mythos.
- **Claude Sonnet 5.5 is not one of them.** It is the current Sonnet, at Sonnet 5's price ($2 / $10).
- **Sonnet 5.5 rejects that with HTTP 400,** per the current API reference; its off switch is
  `{"type": "between_tools"}`.
- **Nothing stops you choosing it.** Once its row is in the price table, `config.py` accepts it for any role
  (`:401-405`), and every call of that role then fails.

**Evidence:** `repro/api/thinking_shapes.py` prints each of six models' requests: Sonnet 5.5's plan and work requests
both carry "disabled". The 400 itself is the API's documented answer; this analysis made no live call.

**The docs.** DOCS (Choosing models) mentions neither Sonnet 5.5 nor Haiku 5.5. If you try Haiku 5.5 as the research
model, enter its higher prices: it costs $0.10 / $0.50 for prompts up to 100,000 tokens and $0.50 / $2.50 above, and
one row of the price table can't express that.

**Fix (S):** a table of how each model thinks or doesn't (Sonnet 5.5: `between_tools`; Opus 5.5, Fable, Mythos:
adaptive), and a refusal at start for a model Ember doesn't know how to ask.

### 4.15 The records Ember learns from can say what didn't happen — medium ×6, reproduced ✔

The learning loop's *safety* holds: the agent can't make your venture decisions, and outside text reaches lessons,
the strategy and the playbook by no path but 4.1. Its *measurements* are weaker:

1. **A channel venture's first test is met before it starts.**
   - Where: `agent/stages.py:64-67`, `:97-98`; `agent/metrics.py:390-398`.
   - Pinterest's test, "its pins bring 10 clicks", counts the clicks of every pin ever made, from any line, also before
     you backed it. Printify's counts every order ever. There is no baseline.
   - So a venture you back with 10 earlier clicks is "met" by Ember's code minutes later, and the agent may set it
     live: its 21-day test never runs.
   - Evidence: `repro/learning/repro_channel_first_test.py`. Backed at 12:00, closed "done" at 12:10 with 12 earlier
     clicks, then set live.
2. **A live channel venture is parked for "nothing sold".**
   - Where: `stages.py:556-566`, `:327-332`.
   - `sold()` counts the venture's own listings' orders, and a channel's line has none. So at day 60 Ember's code parks
     it, whatever its pins brought, and a wrong case about Pinterest follows.
   - Evidence: the same script; an order of the pinned listing is on record, and the venture is parked.
3. **Consolidation can rewrite a lesson's numbers away.**
   - Where: `agent/memory.py:418-431`.
   - "A pinned lesson or one with numbers is never dropped" is checked for the `drop` list only (`:439`). A `keep` item
     may say anything. The model is asked to keep the numbers; nothing checks.
   - The old text stays only in `memory_versions`.
   - Evidence: `repro/learning/repro_consolidate_numbers.py`. "Dropshipping ruled out: supplier margins under 5 %" is
     kept as "Dropshipping looks promising.", reported as "0 merged, 0 dropped".
4. **One event becomes three cases and an established principle.**
   - Where: `agent/learning.py:79-119`, `:202-205`, `:339-358`.
   - Your kill closes the venture's projects, so the venture and each project settle as separate items. Each gets a
     retrospective, and `adopt` counts them as three agreeing cases.
   - That makes the principle "established" the same day, and puts it first in every plan's LESSONS for 120 days.
   - Six ideas of one brainstorm parked in a day for one reason (analysis-0.38.0, its 3.3) do the same.
   - Evidence: `repro/learning/repro_one_event_three_cases.py`.
5. **In a busy week the weekly look doesn't see the playbook it alone curates.**
   - Where: `agent/weekly.py:155-166`.
   - The week's cases come before YOUR PLAYBOOK, and the view is cut from its end until it fits.
   - The look can't merge or retire what it can't see, so the 40-principle cap fills.
   - Evidence: `repro/learning/repro_weekly_playbook_cut.py`. The playbook starts at character 17,060 of a view cut to
     16,000: 0 principles shown.
6. **Bets settle early, and on old numbers.**
   - Where: `agent/bets.py:177`, `due <= today`.
   - A bet due on 09-04 settles at 00:05 on 09-04 and loses its day; a milestone gets its whole day.
   - With the Etsy sync a week behind, a bet settles on week-old numbers. `metrics.read` refuses the same numbers for a
     milestone ("over 3 hours ago").
   - `no_reach` counts title and tag edits as reach; the listing tests stopped doing that in 0.33.0.
   - Evidence: `repro/learning/repro_bet_due_day.py`, `repro/learning/repro_bet_stale.py`.

**Why it matters.** Ember's playbook, its weekly look and its venture decisions are built on these records. Each defect
makes a lesson look surer, or a test look met, than the facts are.

**Fix (S each; the second is M):**
- a baseline when a channel test is set, and judge a channel venture by its channel;
- refuse a `keep` that loses its sources' numbers;
- count support once per event (one per review per venture);
- put the playbook before the cases;
- settle bets after their day and only on fresh numbers, with `traffic` for `no_reach`.

### 4.16 Two more plan defects: steps a website line can't do, and a sleep cut for nothing — medium, low; reproduced ✔

- **A website's line gets weekly pins and posts it can't make** (`agent/plan.py:1833-1867`).
  - 0.39.1 dropped these steps for a channel's line only. A website-content line, such as the German blog articles
    (line #10, live), still gets "This week's pin" and "This week's Bluesky post".
  - `propose_pin` refuses, because it needs a live listing of the line.
  - A post counts for nothing, because `reach.funnels` knows only Etsy listings. So the steps never close.
  - Evidence: `repro/plan/r7_site_recurring.py`.
- **A cycle whose own step closed still cuts the sleep** (`plan.py:2535-2539`). "Moved" counts as busy even when the
  move closed the step and what remains waits on you. That costs one paid plan each time. Low (L19).

### 4.17 Pages and pictures that show something other than the file — medium ×2, reproduced ✔

- **A tall table row runs off the page** (`products/pdf.py:620-621`).
  - A row taller than a page minus the table's header gets the warning "a table row is taller than a page", and is
    drawn where the cursor stands, with no page break.
  - Below a paragraph its text ends 87 pt under the page's foot, off the page, though it would fit on a fresh one.
  - On a small KDP trim, one long cell is enough.
  - Evidence: `repro/products/repro_table_row_overflow.py`.
- **A spreadsheet's picture doesn't count dates** (`products/sheets.py:1309-1311`).
  - `_numbers`, behind SUM, AVERAGE, MIN, MAX and COUNT, keeps numbers only. Its neighbour `_number` turns dates into
    Excel's day numbers.
  - So over a date column the picture shows `MAX` as 0 ("1900-01-00"), and `COUNT` and `COUNTIF` as 0, where Excel
    shows the last date, 3 and 2.
  - The file is right; the picture you approve isn't. 0.37.5 promised the pictures show "every number as Excel shows
    it".
  - Evidence: `repro/products/repro_sheet_dates.py`.

**Fix (S each):**
- break to a fresh page before an oversized row that isn't at a page's top;
- let `_numbers` take dates as `_number` does.

### 4.18 Low

Each was found by its reviewer, by a reproduction or by reading the code. I re-checked the ones marked ✔ myself; the
others stand on their reviewer's evidence.

| # | Finding | Where | Fix |
|---|---|---|---|
| L1 | The sum check of `make_image`'s table calls a rounded percentage total wrong: 3 × 33,33 % under "Summe 100,00 %" is "99,99 %", also on Ember's own statement covers ✔ | `products/tables.py:159-174` | S: a tolerance for the rows' rounding |
| L2 | `make_document` with pages "²" ends in a bare ValueError ("the tool failed") instead of a refusal: `'²'.isdigit()` is true, and `int('²')` fails ✔ | `products/make.py:172` | S |
| L3 | A spreadsheet's picture draws ✓, ★ and emoji as empty boxes, without a word; the PDF and image makers warn | `products/sheets.py` (pictures) | S: `fonts.undrawable`, as elsewhere |
| L4 | A stop inside a calling tool (research, draft) is stored as the tool's failure, with a traceback | `agent/tools.py:1720` | S: re-raise `Stopping` and `EndCycle` |
| L5 | A workshop run's continuation after `pause_turn` checks neither the stop nor the kill switch, and a call in flight finishes after Kill (a workshop stream, up to 1,800 s) | `agent/workshop.py:288-353` | S |
| L6 | The keepers before the plan (money goal, stages, grading, bets, obligations) aren't guarded: a bug there fails every cycle after its paid review and critic | `agent/loop.py:724-740` | S: `_guarded`, as `_keep_plan` is |
| L7 | One malformed month in your blog's list (2026-13-01) stops every post upload with an IndexError every minute, a fresh SFTP login each time, and the live page too | `products/blog.py:204-206`, `products/live.py:330`, `integrations/site_publisher.py:538` | S: `date.fromisoformat` in `read_index` |
| L8 | An English Bluesky post can wait forever while two German posts fill the day before 14:00 | `integrations/publish_queue.py:164` | S/M |
| L9 | An approved Printify edit doesn't check that the Etsy listing is still live | `integrations/printify_publisher.py:1168` | S |
| L10 | A cost probe whose create answer was lost stays at Printify ("Ember cost probe (not for sale)"), and a probe can still start after Kill from the turn's calls | `integrations/printify_publisher.py:198-220`, `:337` | M |
| L11 | A "new" blog post overwrites an unlisted page of yours with the same name (Undo restores it) | `agent/tools.py` (`_propose_blog_post`), `integrations/site_publisher.py:531` | S |
| L12 | The link page has no AI line; posts and the blog's list have one | `products/blog.py:592` | S |
| L13 | Website page names `live` and `links` aren't reserved | `products/site.py:29` | S |
| L14 | The shareable report hides `kdp_author` in OPTIONS but prints it in AGENT RECORDS | `diagnostics.py:415-432`, `:800-810` | S |
| L15 | Other Home Assistant users' IDs appear unmasked in the shareable report (a mirrored security log line) | `security.py:194-204`, `diagnostics.py:278-281` | S |
| L16 | Kill pressed during an Etsy sync doesn't stop that sync's `set_auto_renew` (USD 0.20 a renewal) | `integrations/etsy_publisher.py:853-854`, `:1000-1016` | S |
| L17 | Your endpoints answer 500 to an id beyond SQLite's range, or to a lone surrogate | `agent/owner.py` (`mark_read`, `add_venture`, `add_milestone`, `_link`, `_text`) | S |
| L18 | The middleware's own refusals (403) carry no CSP, nosniff or cache-control | `security.py:148-166`, `:215-235` | S |
| L19 | A step that closed in the cycle still counts as "busy" and cuts the sleep | `agent/plan.py:2535-2539` | S/M |
| L20 | `keep` and `steer` grow with products × pins: 3.1 s and 2.4 s at 100 products and 20,000 cycles, inside `BEGIN IMMEDIATE` (the busy timeout is 10 s) | `agent/quality.py:100`, `plan.py:510-512` | S/M |
| L21 | UTC where your day is meant: a decision's due day (`now[:10]`: after 22:00 UTC in Berlin it is overdue at once), the two waits a day, "Changes today", and the critic's "failed today" | `agent/obligations.py:77`, `plan.py:3437`, `:3789`, `agent/quality.py:58` | S |
| L22 | Lessons: a full file drops line 0 when line 1 isn't a lesson, and one long lesson evicts 11 of 30 others | `agent/memory.py:240-241`, `:160-175` | S |
| L23 | More than 10 items settled since the last review, or settled while it ran, are never retrospected; a retrospective's subject must match exactly | `agent/learning.py:119`, `agent/loop.py:1294` | S |
| L24 | A lesson that waits for room in a full playbook is skipped for good | `agent/learning.py:347-363` | S |
| L25 | The seeded Etsy leg stays "researching", and the plan asks to appraise or park the shop's own leg | `agent/ventures.py:1012-1013`, `plan.py:689-690` | S |
| L26 | The listing critic sees one photo and the names of the files, and the judged text isn't fenced | `agent/quality.py:137-144`, `agent/critic.py:53-75` | M |
| L27 | The worst case of a server-tool call with a 1-hour cache prices its loop's writes at the 5-minute rate (latent: no caller sets 1 h) | `economy/estimate.py:235-238` | S |
| L28 | netguard doesn't stop `multiprocessing`'s spawn context, `getnameinfo` or `gethostname` (no handler uses them) ✔ | `agent/netguard.py:23-44` | S |
| L29 | `request_upgrade` attaches any `.py` as "Code Ember ran in its workshop", and "Copy as a task for Claude Code" puts the agent's words in as plain instructions, in a fence a line of three backticks breaks | `agent/tools.py:3601-3617`, `web/static/js/app.js:3571-3595` | S |
| L30 | The database's guards on your venture and milestone decisions cover UPDATE only: an INSERT at "building", "killed" or "done" is accepted (no path does it today) | `migrations/0030_venture_stages.sql:109`, `0029_milestone_metrics.sql:20-22` | S: two BEFORE INSERT triggers |
| L31 | `/data/backups` keeps 10 whole copies by count, all inside every Home Assistant backup; a failed copy (a full SD card) leaves an empty "ok" database and a `-journal` that pruning keeps | `db.py:38`, `:251-264`; `config.yaml` | S/M: `backup_exclude`, write then rename, cap by size |
| L32 | Nothing reports the database's or the backups' size; `llm_calls`, plans, picks, digests, journals and strangers' emails are never pruned (about 33 KB a dry-run cycle) | `db.py`, `diagnostics.py` | S |
| L33 | The reserved migration 0070 would run after 0094 on upgraded databases but in order on fresh ones, and 0072's table rebuilds could make the two schemas differ | `db.py:45-49`, `:75-88` | S: give it the next number |
| L34 | The image's build inputs are pinned by tags (actions, base image), and the device's `pip` installs without hashes: a wheel added later for a pinned version isn't what CI tested | `.github/workflows/ci.yml`, `Dockerfile:5`, `:18` | S/M |
| L35 | Untested: the event-loop guard, a failed backup, an upgrade of a database an old release filled; the suite never runs on the image's musl | `tests/` | S/M |
| L36 | Mail: only INBOX is read; Authentication-Results is read to 4,000 characters, so padding can make a forged sender look verified; the hidden-text filter misses `display:/**/none`, CSS escapes and a closed `<details>`, and prefers the text/plain part, which HTML readers never show | `integrations/mail.py:985`, `:539`, `:593` | S |
| L37 | Etsy's order sync reads at most 500 receipts of 31 days, newest first, and moves on: in a busy shop an old order's refund is never read, and revenue stays too high | `integrations/etsy_live.py:363-364`, `etsy_publisher.py:855` | S: sort by update, treat a full last page as an error |
| L38 | Dry-run sessions share the ledger's keys for revenue and fees, so from the second session on fake revenue and fees aren't booked | `integrations/etsy_publisher.py:1582`, `:1663-1671` | S |
| L39 | A Disconnect of Etsy can be undone by a token refresh in flight | `integrations/etsy_connection.py:105-108`, `etsy_live.py:246` | S |
| L40 | A KDP card's download links serve the workspace's file at click time, not the approved bytes | `agent/views.py:981`, `web/static/js/app.js:2937` | S |
| L41 | The read before sending is skipped when a round starts at the daily email limit, and the round can then cross midnight and send without one; times have second precision | `integrations/executor.py:247-256` | S |
| L42 | Doc drift: `tools.py`'s module text ("no tool reaches the network"), `vision/learning.md` (READY), DOCS' dormant mode, its options table, "Data and backups", its mail section (1 MB, INBOX), README's checks (no `-W error`), `db.py`'s note on 0071 | as named | S |
| L43 | A merge reached the tracked branch with CI red (run #276, 0.30.3, 10-07): merges aren't gated by a required check | GitHub settings | S: require the CI checks, and an up-to-date branch |

---

## 5. Why: one pattern behind most findings

**The pieces work; their seams don't.** Of the 17 high and medium findings, 11 sit where two features meet:

| Finding | One side | The other side |
|---|---|---|
| 4.1 quoted text runs a call | the rescue of a call written inside a text (0.24.0) | the agent quoting outside text into files and messages |
| 4.2 a stop below the quote | the read before every send (0.37.3) | the stop check, which reads only the words above a quote |
| 4.4 holds leak | your hold (0.36.0) | promises and decisions as plan steps (0.37.0) |
| 4.5 no weekly marketing | the critic's step in the launch stage (0.35.0) | the critic as a chore (0.39.1), and a critic that hasn't passed a listing |
| 4.6 pins, waits | your pins (0.35.0) | owed steps (0.37.0), Ember's holds and waits |
| 4.7 veto windows | windows restarted after a pause (0.37.3) | a restart, which is no pause |
| 4.10 work lost, listings twice | `restore` (0.33.0), the duplicate check | `draft` and the workshop; automatic unlocks |
| 4.11 wakes | the plan's tries, the brake, the waits (0.35 to 0.38) | refused cycles, event wakes, reactive cycles |
| 4.13 the will | the guard's reserve | entries from outside the guard (0.15.0) |
| 4.15 records | settlement by kind (0.18.0 to 0.30.0) | one event touching a venture and its lines |
| 4.16 website steps | 0.39.1's fix for a channel's line | the website's template |

The other six (4.3, 4.8, 4.9, 4.12, 4.14, 4.17) are plain defects in one place: hostile input not read whole, a check
by convention, a fallback that's too broad, a new model.

**How it got this way.** Each diagnostics report finds a live symptom, and a release fixes it with a rule, often
within hours: 61 releases in 14 days.
- **The fixes are right for what they saw.** Each comes with a regression test: 46 files, a third of the test
  functions.
- **The rule is tested where it was added, not where it meets the others.** Plan weights alone are tested from 13
  files.
- **No test runs a week.** Nothing replays a week of cycles with your levers, refusals, restarts and outside mail mixed
  in, which is where 4.2, 4.4 to 4.7 and 4.11 show up.
- **Hostile input is tested by its kind, not its shape.** Stops are tested above the quote only, and mail with 8-bit
  headers, huge styles or relocated Office parts never.
- **The prompt pays too.** Each rule adds words to a fixed prompt that has 13 bytes left under its bound, so new words
  push out old ones. 0.39.0 shortened `propose_printify_edit`'s description to fit, and 0.39.1's merge trimmed
  `make_document`'s until it no longer named its own limit, which a test caught before release (their commit
  messages).

**What it costs.** About $93 of API calls in 16 days, for a business with 17 views and no sale yet.
- **Not wasted.** Ember's rules are better each day. The plan picks better steps than READY did, and the money guard is
  sound.
- **But not where the bottleneck is.** That is reach (4.5 is the one code defect in it), and products that pass the
  critic. Neither is a seam between features.

---

## 6. What I recommend

### 6.1 Fix now: one release

In order of what they protect. All are S unless marked.

1. **4.3, one email stops the mailbox.** Clean surrogates, store each mail in its own savepoint, run each executor in
   its own `try`, and show a failing read.
2. **4.2, stops below the quote and in large mails (S/M).** Read past the quote, mark blockquotes, and check a large
   mail's first text part.
3. **4.1, quoted text runs a call (M).** Take calls out of the journal's fields only, or only when the markup is the
   whole rest of a field. Never run a taken-out call that changes memory, deletes or files a request.
4. **4.4, your holds.** Hold owed steps, refuse a focus on a line you hold, and refuse `venture_create` and every
   `project_create` under "Hold new things".
5. **4.5, the launch stage.** A product is launched without the critic's step and without steps that wait on a channel
   that is off. This one change gives every live product its weekly marketing.
6. **4.12, the CSS pattern.** Cut before stripping, and strip with a linear scan.
7. **4.13, the will (M).** No entry may leave the balance under the will's cost without pausing Ember or asking you.
8. **4.11.1 and 4.11.2, wakes and tries.** Event wakes need a working cycle's money. Only finished cycles' picks count.
   Your wake waits for midnight while the cap can't pay a plan.
9. **4.9, the website.** Parse mailto links, and make the SFTP fallback safe.
10. **4.7, veto windows.** The heartbeat, before you grant any unlock with a veto window.
11. **4.14, Sonnet 5.5.** How each model thinks, and a refusal at start for a model Ember can't ask.

Each has a script in `repro/` that shows the defect today and should show the fix.

### 6.2 Next

- **4.8, the Office check (M):** run it by content type, on every part.
- **4.15, the records:** baselines for channel tests, the consolidation's numbers, one count per event, the playbook
  first, bets after their day.
- **The plan's levers:** 4.6 (pins and wait chains) and 4.16 (website lines).
- **The other wakes:** 4.11.3, 4.11.4 and the agenda's share of the context.
- **Work and pictures:** 4.10 (a version of every overwrite, duplicates after approval) and 4.17 (tall rows, dates in
  pictures).
- **The lows**, as they come up; L30 to L35 make the database and the build sturdier, and L36 the mailbox.

### 6.3 How to build from here

1. **A scenario test.** A dry-run week with your levers, refusals, restarts and outside text mixed in:
   - your hold, pin and veto;
   - a spent cap, a restart, an event at night;
   - a hostile email, and a stop written below the quote.

   `tests/test_loop_shapes.py` drives cycles already. Make it run days, and assert what this analysis found.
2. **A corpus of hostile input.** Real-world mail shapes (bottom-posted, HTML-only, 8-bit headers, large attachments,
   huge styles) and Office packages, run through the mailbox and the file checks in every test run.
3. **Fewer releases.** One release a day at most, after the scenario test, so that a week of live cycles can show what a
   rule does before the next one changes it (0.37.0's analysis asked the same of the weights).
4. **Room in the prompt.** Move what Ember's code enforces out of the rules (a refusal already says it), so a new tool
   doesn't need old words cut. The bound is 13 bytes away.
5. **Gate the tracked branch.** Require CI on merges (L43), and pin the build's inputs (L34).
6. **Spend on reach.** The next releases that change the business are the ones that bring buyers to the live
   listings, and that make products the critic passes.

### 6.4 For you, now

1. **Before you approve an email, read what its recipient last wrote to Ember**, in Ember's mailbox at your mail
   provider. Until 4.2 is fixed, a stop written below the quote, or sent with a photo, doesn't stop the email.
2. **Check that Ember's mailbox still reads:** on **System → Email**, "Last fetch" should be minutes old, not hours. If
   it stopped, an email with 8-bit headers may be stuck in INBOX (4.3); moving that email out of INBOX frees it.
3. **Grant no automatic or veto unlock** until 4.7 and 4.10 are fixed.
4. **Read an upgrade request's script before you give it to Claude Code** (L29, 4.1). Ember's text is a request, not an
   instruction.
5. **Keep every model setting on `claude-sonnet-5` or `claude-opus-5-5`** (4.14).
6. **If you hold a product with a promise or a decision open, tell Ember to leave it.** Until 4.4 is fixed, the hold
   alone doesn't stop it.
7. **Check that the password committed in 0.4.0 was changed where it is used.** It is still in this public
   repository's history (`.gitleaksignore`).
8. **The diagnostics report of this evening still applies** (ember-diagnostics-0.39.1, its section 4), on the KDP book
   and on lesson #c281.

---

## 7. The earlier analyses' open items

| Item | Status at `cb64642` |
|---|---|
| 0.13.0 §0.2, safe mode opens the dashboard and lifts the kill switch | fixed (OWNER review) |
| 0.13.0 §0.6, unlocks: footer, QA, take-back, kill switch, owner IDs | fixed (OWNER review) |
| 0.37.0 §4.3, §5.2.1, §5.2.2, a promise first, whatever else waits | fixed in 0.37.1 |
| 0.37.0 §4.4, §5.2.4, the same promise counted several times | still holds (`obligations.py:145-200` unchanged) |
| 0.37.0 §4.5, §5.2.3, reports weigh like shipping | fixed in 0.37.9; a promise still can't be made to wait |
| 0.37.0 §4.6, §5.2.5, worth tells the products apart only by type | still holds (`could_earn` is the template's) |
| 0.37.0 §4.8, the blog on for the steering, off for the keeper | fixed (0.37.8) |
| 0.37.0 §4.9, §5.2.8, the audience guessed from words | partly (German rules added) |
| 0.37.0 §5.2.10, OBLIGATIONS says "deal with them first" | still holds (`obligations.py:49`) |
| 0.37.0 §6.1, the daily cap spent by midday | partly (the brake and the waits); live, $5.71 by 12:24 today |
| 0.37.0 §6.3, the KDP book's "Propose" stays done after a rejection | fixed (`_again`) |
| 0.37.0 §6.4, a promise orphaned when its product closes | still holds (reproduced); the fix needs a migration (`obligation_id` is unique, `parent_id` can't change) |
| 0.37.0 §6.7, "Hold new things" passes a line that names a venture | still holds: now part of 4.4 |
| 0.37.0 §6.8, stale texts | still holds: L42 |
| 0.37.3, a stop is seen before an approved email goes out | holds for a stop above the quote only: 4.2 |
| 0.38.0 §3.3 to §3.8 | fixed in 0.39.1, with three caveats: 3.4 remains for website lines (4.16), 3.6 still allows chains of waits (4.6), and 3.8's chore makes 4.5 worse |
| ember-diagnostics-0.39.1 §5.1 to §5.5 (the book check, old pictures, a file elsewhere, a step's kind, a done's reason) | open: recommended this evening, not yet made |

---

## 8. How this was checked

**The reviews.**
- **Brief.** Each reviewer had one part, read-only access to the repository, the pinned dependencies on Python 3.12,
  and the rule to reproduce any high or medium finding with a script against the real code.
- **Process.** They worked in parallel, over 20 to 46 minutes each, and ran several hundred scripts in all.
- **What came back.** Each named what it checked and found sound, alongside its findings (section 3 collects those).

**My checks.**
- **Every high and medium finding:** I read the code it points to and reran its reproduction against `cb64642`.
- **Five of my own:** for 4.1, 4.2, 4.8, 4.12 and 4.14 I wrote independent reproductions, in `repro/tools/`,
  `repro/mail/`, `repro/products/` and `repro/api/`.
- **Nothing was refuted.** No finding I rechecked failed to reproduce. I dropped one claim of my own: Opus 5.5's cache
  price in the default price table ($0.20) is right, though it isn't 10 % of its input price.
- **The reproductions are kept.** They are in [`repro/`](repro/README.md), runnable from `ember/`. Each ran from there
  with exit code 0. A secret scan with the repository's own `.gitleaks.toml` (gitleaks 8.30.1, as CI runs it) found
  nothing in them or in this report.

**Not covered.**
- **Live systems.** No live data beyond the two reports of 10-14, and no call to Anthropic, Etsy, Printify, Pinterest,
  Bluesky or a mail server: the integrations were tested with the suite's fakes.
- **4.14's 400** is the API's documented answer, not a live call.
- **Excel itself.** Whether Excel opens a Strict-namespace sheet inside a transitional package (4.8) wasn't tested.
- **Speed.** Timings (4.12, L20) are from this container, not a Raspberry Pi; a Pi would be slower.
- **The growth rate** (L32) comes from dry-run cycles, which write less text than live ones.
- **Dependencies.** `pip-audit` used PyPI's advisory service. OSV was not reachable, and whether each pin is the newest
  version wasn't checked.
