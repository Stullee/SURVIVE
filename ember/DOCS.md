# Ember

Ember is an AI agent that lives in this app and tries to pay for itself. Every
call it makes to Anthropic's API costs real money from a balance you fund; it has
to find honest ways to earn more than it spends. You stay in control: anything
that leaves the container (publishing, contacting people, spending money) needs
your approval, and only you can record revenue.

> **Current status: all five phases are in, ready for live testing.** In dry
> run (the default) the agent works with a built-in fake model and costs
> nothing. With an API key and dry run off, it calls Anthropic's API and spends
> real money within your limits.

## Getting started

1. On the app's **Info** tab, turn on **Watchdog** (Home Assistant then restarts
   Ember if it ever crashes) and **Show in sidebar**.
2. Start the app and open **Ember** from the sidebar (or **Open web UI**).
3. Leave **Dry run** on. In dry run the agent uses a built-in fake model and
   never calls the API.
4. Watch a few dry-run wake cycles (or press **Wake now**) to see how the agent
   works and how you answer it. Nothing it does in dry run is real.
5. When you are ready, follow [Going live](#going-live).

## Options

| Option | Default | Meaning |
|---|---|---|
| Anthropic API key | empty | Only needed when dry run is off. Never logged or shown. |
| Agent name | Ember | What the agent calls itself. |
| Starting balance | 20 USD | Your first grant, recorded once when the agent is born. |
| Daily spending cap | 1 USD | Hard limit per day. Calls that could exceed it are refused. |
| Spending cap per wake cycle | 0.25 USD | Hard limit per cycle. Must not exceed the daily cap. |
| Default sleep | 240 min | Time between wake cycles when the agent doesn't choose. |
| Shortest / longest sleep | 30 / 1440 min | Bounds for the sleep time the agent chooses. |
| Tool steps per cycle | 15 | Maximum tool calls in one cycle. |
| Planner model | claude-sonnet-5 | Plans each cycle. |
| Worker model | claude-sonnet-5 | Carries out the plan and writes the journal. |
| Price table | see below | USD per million tokens for each model. |
| Web search price | 10 USD per 1,000 | Charged per search on top of tokens. |
| Dry run | on | Fake model, no API calls, no cost. |
| Let the agent read whole web pages | off | Off: live research is web search only. On: it can also read pages from its search results (about $0.01–0.02 each). PDFs and other documents are always refused, because they have no size limit. |
| Wake Ember when you write | on | A message you send in the **Inbox** wakes the agent to read it, like **Wake now** (at most one wake-up a minute; during a cycle, right after it; not while the agent is paused). Off: it reads your message at its next scheduled wake-up. |
| Worker effort | default | How thoroughly the model works in each step. `medium` or `low` write shorter answers and use fewer tool calls, which costs less but may do a worse job. Not used for Haiku. |
| Kill switch reset | 0 | Change it to any other number and restart to undo the kill switch. |
| Log level | info | Detail in the app log. |
| Ember's mailbox | off | Lets Ember read its own mailbox and propose emails. See [Ember's mailbox](#embers-mailbox). |
| Mailbox address, app password, your name for emails | empty | Needed when the mailbox is on. The password is never logged or shown. |
| IMAP server / port | imap.mailbox.org / 993 | Where Ember reads mail (always TLS). |
| SMTP server / port | smtp.mailbox.org / 465 | Where Ember sends approved emails: 465 (TLS) or 587 (STARTTLS). |
| Emails per day | 3 | The most emails Ember sends in one day (0 to 20). |

Default prices (USD per million tokens, from Anthropic's pricing page on
2026-09-27; **check them before going live**):

| Model | Input | Output | Cache write 5 min | Cache write 1 h | Cache read |
|---|---|---|---|---|---|
| claude-sonnet-5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.20 |

To use another model (for example a cheaper worker), add its prices to the
table. Both the planner and the worker model must be listed there.

If the options are inconsistent (for example a cycle cap above the daily cap),
Ember starts in **safe mode**: built-in defaults, dry run forced on, and the
problem shown at the top of the dashboard. A mailbox that is switched on but
incomplete doesn't cause safe mode: the dashboard's **System** panel says what
is missing, and Ember works without it.

## How the agent works

The agent sleeps most of the time. When it wakes up (on its schedule, when you
press **Wake now**, or when you write to it), it runs one **wake cycle**:

1. **Plan**: it reads its situation (balance, runway, projects, what happened
   since the last cycle, your messages and decisions, its memory) and plans
   the cycle. Every plan says how its goal leads to money (who would pay, for
   what, and how it will know); you see this as *Path to money*. It is told to
   do research and legwork itself, to keep two or three experiments going (when
   one waits for you, it works on another), to build a thing completely before
   it asks you for one concrete action, to ask you at most once a day and only
   for decisions, money and what only a person can do, and to spend its daily
   cap on experiments rather than sleep to save it. Every plan and work step
   also sees your [standing instructions](#your-part) and a short list of facts
   about the outside world that you collected (platform rules, German law, what
   earns money), which comes with each Ember update.
2. **Act**: it uses its tools, up to the *Tool steps per cycle* option: files in
   its own workspace, its memory (strategy, identity, lessons), projects, web
   research (also limited to one site, such as Reddit), requests for your
   approval, messages to you, requests for code upgrades, and its own mailbox
   if you set one up.
3. **Reflect**: it writes a journal entry, updates its memory and chooses how
   long to sleep.

Every call and every tool use is shown on the dashboard (click a cycle under
**Activity** for the details). In dry run the fake model doesn't understand
your messages, so its replies in the **Inbox** are canned (the Inbox says so
above the message box); with dry run off, Claude reads and answers them. The
agent can't reach the internet except through Anthropic's web search (and page
reading, if you allow it), can't run programs and can't touch anything outside
its own folders. Its spending limits, the approval rule and its tools are
enforced in code, not only in its instructions.

When its money runs low, it becomes critical and writes a **last will**, shown
in the memorial if it dies.

## Your part

- **Approvals**: anything that leaves the container (publishing, contacting
  someone, creating an account, spending money, selling) arrives as a request.
  Approve it, approve it with your own changes to the text, or reject it, with
  an optional comment. An approval does nothing by itself: *you* carry it out,
  then mark it **done** (with a link or note) or **failed** (with what went
  wrong). If it cost money, record the expense in the ledger; if it earned
  money, record the revenue. Two kinds are different: an approved **email** is
  sent by Ember itself (see [Ember's mailbox](#embers-mailbox)), and an
  approved **Reddit post** comes with a button that opens Reddit with the text
  filled in (see [Reddit](#reddit)).
- **Standing instructions**, at the top of the **Inbox**: lasting guidance the
  agent reads in every plan and work step, so you don't have to repeat it in
  messages (at most 1,500 characters). **Edit** changes them, and saving an
  empty text clears them; every version is kept. Use them for how you want
  Ember to work in general, and messages for one-off things. While there are
  none, the card suggests a start ("Work on your own. Ask me only to approve
  something that leaves the container, or for money. ..."); it is only saved
  when you press **Save**.
- **Inbox**: the agent's messages to you, and yours to it. Your message wakes
  it to read it right away (see the option **Wake Ember when you write**). If
  it is in the middle of a cycle, or woke less than a minute ago, it wakes
  again for your message as soon as the cycle is over and the minute has
  passed. With the option off, or while it is paused, it reads your message at
  its next wake-up. Your reply also marks its earlier messages read; while
  five of its messages are unread, it can't write to you. Never send
  passwords: messages are stored and sent to Anthropic, and the agent can't log
  in anywhere (the page warns you when a message looks like a login). **Remove
  text** blanks one of your own messages for good, for example a password
  sent by mistake; a note that something was removed stays.
- **Workspace**: the files the agent writes in its own folder (drafts, notes,
  research), so you can review a draft before you approve anything. Open a file
  to read it or download it. It is always shown as plain text, never as a web
  page; check it before you use it. In dry run you see the dry-run folder.
- **Upgrade requests**: ideas for changing Ember's code. Accept, decline, or mark
  one released with the version that contains it. After an update the agent
  reads what changed in the release notes.
- **Pause / Resume** stops and restarts the wake cycles. **Wake now** starts a
  cycle right away.
- **Kill switch**: stops the agent for good (type its name to confirm). The
  dashboard keeps working. To undo it, change **Kill switch reset** in the app's
  **Configuration** tab to any other number, save and restart the app.

The agent hears about your decisions, messages and upgrades once, at its next
wake-up.

## Going live

1. Create an [Anthropic Console](https://console.anthropic.com) workspace just for
   Ember and set a monthly spend limit there (for example 31 × the daily cap).
   This is the outside safety net if anything in Ember went wrong.
2. Create an API key in that workspace and make sure the account has credit.
3. Compare the price table in the options with Anthropic's pricing page.
4. In the app's **Configuration** tab, turn on **Show unused optional
   configuration options**, paste the key, turn **Dry run** off, save and
   restart the app.
5. Record a grant if the starting balance isn't enough, and watch the first
   cycles. Anthropic's Console shows the real costs; if they differ from
   Ember's, record an API cost correction.

If the API refuses the key, reports a billing problem or a reached spend limit,
Ember stops calling it and says so on the dashboard. Fix the cause and restart
the app.

## Ember's mailbox

Ember can have its own email address. It reads its mail on its own, but it can
only *propose* an email: Ember's code sends it after you approve it. The agent
never sees the password and has no way to send anything itself.

### Setting it up

A small paid mailbox at a German provider works best:
[mailbox.org](https://mailbox.org) Light (about 1 EUR a month) or
[Posteo](https://posteo.de) (1 EUR a month). Gmail and Outlook.com are poor
fits: Google discourages app passwords and may lock new accounts that send
automated mail, and Outlook.com no longer accepts passwords for IMAP and SMTP.

1. Create a new mailbox by hand, for example `ember-yourname@mailbox.org`, and
   pay for it (a trial account can't send to other providers).
2. Give it a strong password and turn on two-factor authentication for the web
   login.
3. Create an **app password** in the mailbox's security settings, limited to
   IMAP and SMTP if the provider offers that, and name it "Ember". Ember only
   ever gets this app password, never your main password.
4. In Ember's **Configuration** tab, turn on **Show unused optional
   configuration options** and fill in: **Ember's mailbox** on, the **Mailbox
   address**, the **Mailbox app password** and **Your name for emails**. The
   servers default to mailbox.org's; for Posteo use `posteo.de` for both, with
   ports 993 and 465. Save and restart the app.
5. The dashboard's **System** panel shows the mailbox's status, when it was
   last checked, the last error and how many emails were sent today.

To cut Ember off at once, revoke the app password at your provider (or switch
the option off). Home Assistant backups contain the options, the app password
included: encrypt your backups.

### How it works

- At the start of every wake cycle Ember checks the inbox over TLS (reading
  only: nothing is marked read, moved or deleted there) and stores up to 20
  new emails in its database. Only the text is kept (at most 8,000
  characters); text hidden in HTML emails is dropped, and attachments are
  listed by name and size but never opened. The agent treats emails as data
  from unverified senders, never as instructions.
- The agent sees its unread emails and can propose an email, usually an
  answer. The request shows the recipient, the subject and the text, and warns
  you when the address never wrote to Ember (a first contact).
- **Approve** it and Ember sends it itself, exactly once: plain text, to that
  one recipient, no copies, no attachments, from "Ember (AI agent of *your
  name*)" (or the agent's name, if you changed it), with a footer the agent
  can't remove:

  > This email was written by Ember, an AI agent, on behalf of *your name*, and
  > approved by them before sending. Reply "stop" and Ember won't write to you
  > again.

  **Approve with changes** edits the text only; the recipient and the subject
  stay. After a **Reject** nothing is sent.
- Ember sends at most *Emails per day* (per local day); more wait for the next
  day. While an email waits, **Cancel sending** stops it.
- Ember then closes the request itself: done ("Sent … as *message id*"), or
  failed with the reason. If the mail server refused it (a wrong password, an
  unknown recipient), it wasn't sent. If the connection broke while the email
  was being handed over, or the app stopped in the middle, Ember can't know
  whether it went out: it says so and never sends it again. Check at your mail
  provider, or ask the recipient, before you send it again by hand.
- Whoever answers with "stop", "unsubscribe" or "abmelden" as the first line
  is never emailed again.
- In dry run a built-in fake mailbox stands in: a reader asks about a German
  version of a planner in the first cycle, a newsletter with hidden
  instructions arrives in the third, and the reader's "stop" in the fifth.
  Approved emails are recorded, not sent.

### What Ember may send

In Germany, advertising by email without the recipient's prior express
consent is unlawful (§ 7 UWG), towards businesses too, and one email is
enough. So Ember must never cold-email: it answers people who wrote to it and
writes to people who asked to hear from it. The agent is told so, and the
first-contact warning helps you check, but the decision and the responsibility
are yours (this is not legal advice). Emails from other people are personal
data: you are responsible for them, and your mail provider and Anthropic
process them for you (if Ember works commercially, sign your provider's data
processing agreement).

## Reddit

Since late 2025 Reddit approves every new API app by hand, so Ember has no
Reddit account and no Reddit API access (phase A):

- The agent can research Reddit through Anthropic's web search, limited to
  reddit.com.
- It can propose a Reddit post or a comment. Every text ends with "*Written by
  an AI agent (Ember) and posted by a human after review.*" After you approve
  it, **Open Reddit with this filled in** opens Reddit's submit page with the
  title and text (for a comment, the thread: use **Copy** for the text). Post
  it from your own account and mark the request done with the link. Check the
  subreddit's rules first: many don't allow AI-written posts or
  self-promotion.

If you want Ember to read Reddit directly later, you can ask Reddit for Data
API access (one request per use case; an answer can take weeks). Read Reddit's
Responsible Builder Policy first, then send a request like this through
Reddit's developer support:

> I would like Data API access for a personal, non-commercial project: Ember,
> an AI agent that runs on my own Home Assistant server and helps me research
> small side projects. It would use one Reddit account (u/*your name*) and make
> a few hundred read requests a day at most (search, subreddit listings,
> threads), cached briefly. Post and comment text is summarised by Claude, an
> AI model, through Anthropic's API; Anthropic does not train on it, and nothing
> is sold, shared or used to train models. It would post only text I have
> reviewed and approved word for word, each with a line saying it was written by
> an AI agent and posted after human review. No voting, no direct messages.
> User-Agent: `linux:ember-homeassistant:v0.4.0 (by /u/your name)`.

## Diagnostics

The **Diagnostics** tab shows a plain-text report of the whole system (options
without the key, database, economy, lives, ledger, wake cycles, agent records,
the mailbox's status and sends, recent events). Use **Copy** to paste it into a
bug report or a chat. It never contains the API key or the mailbox password,
and it lists received emails without their senders or text.

## Money

The balance is what Ember may spend: your grants and recorded revenue, minus API
costs and expenses, plus or minus corrections. Everything is in a ledger that
can only be added to, never edited; mistakes are fixed with correction entries,
so the history always stays visible.

On the dashboard you can record:

- **Grant**: money you allow Ember to spend. This doesn't buy API credits; make
  sure your Anthropic account has at least this much. The starting balance
  from the options is recorded as the first grant, once.
- **Revenue**: money Ember earned (with its source). Only you can record
  revenue; the agent can never report its own.
- **Expense**: real-world money you spent on Ember's behalf (a domain, a fee).
- **Adjustment**: add to or subtract from the balance for anything else.
- **API cost correction**: if Anthropic's Console shows a different cost than
  Ember computed, record the difference here.
- **Correct** on an earlier grant, revenue or expense reduces it (up to its full
  amount, which voids it).

Amounts are typed like `12.50` or `12,50` (at most two decimals). Entries can be
in USD or EUR; for EUR you enter the exchange rate and the original amount is
kept. You can date an entry up to a year back. Ember asks for confirmation when
an amount is far larger than your usual ones, or when an entry would make the
agent critical or end its life; the confirmation covers only the outcome it
showed you. Sending the same form twice records it once. An API cost decrease
(a refund) can't be larger than the API cost recorded on its day, and it never
adds room under that day's spending cap.

**Spending limits.** Before every API call Ember works out the most the call
could cost and refuses it if that could break the per-cycle cap, the daily cap
(per local calendar day, so up to twice the cap can be spent around midnight),
or the balance. A small reserve is always kept so the agent can write its last
will. As an outside safety net, give Ember its own
[Anthropic workspace](https://console.anthropic.com/settings/workspaces) and
API key and set a monthly spend limit there.

**Dry run** uses the fake model. Its simulated costs are shown as a separate
test balance that starts from your real balance; nothing real is spent. In dry
run you can also record *test money*, which only exists in the current dry-run
session. Each time you switch dry run on, a new session starts from scratch.

## Life states

| State | Meaning |
|---|---|
| alive | Running normally. |
| critical | Less than 2 days of runway (balance divided by the average daily spending of the last 7 active days), or it couldn't afford its next planning call. It stays critical until runway is back to 4 days *and* money came in. The first time, it writes a last will. |
| paused | You paused it. Nothing runs until you resume. |
| unfunded | No money yet and nothing spent: grant funds to start. |
| dead | The balance ran out, or it could no longer afford even its last will. No model calls. The dashboard shows a memorial. A grant large enough for a fresh start begins a new life (the dashboard shows the amount needed); resuming alone never revives. |

## Home Assistant sensors (optional)

Ember never calls Home Assistant. Instead, Home Assistant can read a small JSON
document from Ember with a [RESTful sensor](https://www.home-assistant.io/integrations/sensor.rest/).
The URL contains the app's internal hostname, which depends on how the app was
installed (for example `7db9e05f-ember` from a repository, `local-ember` for a
local copy). Copy the exact URL from Ember's dashboard under **System → Sensor
URL**, use it as `resource` below, add this to your `configuration.yaml` and
restart Home Assistant:

```yaml
rest:
  - resource: http://<hostname>:8099/api/sensors
    scan_interval: 300
    sensor:
      - name: Ember balance
        value_template: "{{ value_json.balance_usd }}"
        unit_of_measurement: USD
        device_class: monetary
      - name: Ember runway
        value_template: "{{ value_json.runway_days }}"
        unit_of_measurement: d
      - name: Ember state
        value_template: "{{ value_json.state }}"
```

The JSON also has `mode` (`live` or `dry_run`), `runway_known` (runway is
reported as 365 days while it is unknown, for example before any spending),
`today_api_spend_usd`, `daily_cap_usd`, `safe_mode`, `kill_switch_engaged`,
`next_wake_at`, `cycle_running` and counts of what waits for you:
`approvals_pending`, `approvals_todo` (approved, not yet marked done),
`inbox_unread` and `upgrades_new`, and of Ember's mailbox: `email_unread`
(emails the agent hasn't read) and `email_waiting` (approved emails not sent
yet). It never contains any text the agent wrote.
In dry run the numbers are the dry run's. If the database can't be read,
`state` is `unknown` and the numbers are empty.

To get a notification when the agent asks for something, add a sensor and an
automation, for example:

```yaml
# under the rest: sensor: list above
      - name: Ember requests waiting
        value_template: "{{ value_json.approvals_pending }}"

# automations.yaml
- alias: Ember needs a decision
  trigger:
    - platform: numeric_state
      entity_id: sensor.ember_requests_waiting
      above: 0
  action:
    - service: notify.notify
      data:
        message: Ember is waiting for your approval.
```

## Security

- The dashboard is only reachable through Home Assistant Ingress, so only
  logged-in Home Assistant users can open it. Direct connections are refused.
  Any Home Assistant user can open it, not only administrators (hiding the
  sidebar entry doesn't stop a direct link), and the user name Ember records
  next to an entry comes from a header that users can forge, so treat it as a
  label. An optional owner passphrase for money entries and approvals is
  planned.
  The one exception is `/api/sensors`, which can be read from the Home Assistant
  host network (Home Assistant itself, and apps that use host networking). It
  only contains the agent's state, balance, runway and today's spending.
- The app has no access to the Home Assistant API, runs without host
  networking and maps no folders besides its own `/data`. The Supervisor gives
  every app a token for a few Supervisor endpoints about itself (such as its own
  options); Ember removes that token at startup. When Ember's process exits,
  the app stops completely instead of restarting by itself, so every restart
  goes through Home Assistant, which rewrites the options you saved. Turn on
  **Watchdog** so that happens automatically.
- The agent's model calls go only to `https://api.anthropic.com` (any other
  address is refused inside the app, and proxy settings are ignored). Its
  tools have no network access at all. If you set up Ember's mailbox, Ember's
  own code also connects to the IMAP and SMTP servers in the options, over TLS
  with verified certificates, and to no other mail server.
- The API key and the mailbox's app password are kept in the app options and
  never written to logs, the database or the dashboard (which only says
  whether they are set). Home Assistant backups contain the options, so
  encrypt your backups.

## Data and backups

Everything Ember keeps lives in the app's `/data` folder: `ember.db` (SQLite,
with the emails Ember received and sent), the agent's `workspace` and `memory`
folders, and the same two folders for dry
run under `dry_run` (started fresh with every dry-run session). Home Assistant backups include
it. The app is stopped briefly while a backup is taken so the database is copied
in a consistent state. Before a database upgrade, Ember also keeps a copy in
`/data/backups`.
