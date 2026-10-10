# Ember

A Home Assistant app (Home Assistant's new name for add-ons) that runs an
autonomous AI agent whose goal is to stay alive economically. Every Anthropic API
call it makes costs real money from a balance the owner funds; it has to find
honest ways to earn more than it spends. The owner approves and carries out
everything that leaves the container, and only the owner can record revenue.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton: app manifest, Dockerfile, Ingress dashboard with preview data, SQLite + migrations, docker-compose | **done** |
| 2 | Economy: ledger, cost accounting, budget guard, runway, life states, money entry, chart | **done** |
| 3 | Agent in dry run: wake cycle, fake model, local tools, memory, projects, activity | **done** |
| 4 | Owner loop: approvals, inbox, upgrade requests, controls, changelog awareness | **done** |
| 5 | Go live: real Anthropic client, web search/fetch, prompt caching, final security and cost review | **done, in live testing** |

Dry run (the default) runs the whole agent with a built-in fake model at no
cost. With an API key and dry run off it calls the Anthropic API and spends real
money within the caps. Findings from live testing come next.

0.4.0 adds the first integrations: Ember's own mailbox (it reads mail on its
own; an email it proposes is sent by Ember's code only after the owner approves
it) and Reddit, phase A (research limited to reddit.com, and approved posts
become a prefilled link the owner opens; no Reddit API yet).

0.5.0 makes Ember work on its own: the owner's standing instructions (lasting
guidance written once in the Inbox, read in every plan and work step), rules
that keep two or three experiments going instead of waiting for the owner and
ask the owner at most once a day, a prompt to rewrite outdated or overfull
lessons, and a message from the owner that wakes the agent to answer it.

0.6.0 lets Ember make finished products itself: PDF documents with an editable
Word copy, Excel spreadsheets and listing photos, made by Ember's own code from
the agent's text, which the agent can look at before anyone else sees them.

0.7.0 adds the workshop: for what its own tools can't make, the agent has code
written and run in Anthropic's code execution sandbox (no internet, nothing runs
on Home Assistant). Ember's code checks every file a run makes before keeping it
and keeps the script, and a script that proves useful becomes an upgrade request
with the script attached, so Ember grows new abilities. It also supports a
planner that always thinks first, such as Claude Opus 5.5.

0.7.1 adds the daily business review: once a day, before its first plan, the
agent judges its own numbers (spending per day and per project, revenue and its
sources, the owner's decisions and comments, its cycles and products), decides
for each project to continue, change or stop, and carries it out; the next
review checks that it did.

0.8.0 connects Etsy: the agent proposes complete listings (words, photos, the
files buyers download), and after the owner approves one, Ember's code creates
it in the owner's shop through the owner's own Etsy app (OAuth with PKCE, tokens
kept out of the database and logs, only api.etsy.com). Views, favorites and
orders come back into the agent's plans and daily reviews; the owner records the
revenue.

0.8.1 follows Etsy's API Terms of Use: research never reads Etsy's pages (it
still searches them); the shop's listings and orders are read every hour while
the app runs, not only when the agent wakes, and its categories daily; and the
dashboard and the docs show Etsy's trademark notice:

> The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.

0.8.2 labels the working phase's counter as tool steps ("tool step 3 (at most
15)"), so it isn't mistaken for the steps of the plan shown below it.

0.9.0 lets the agent change its live Etsy listings, each change approved by the
owner (words, price, category, photos, the files buyers download), made by
Ember's code without ever leaving a listing without photos or files. It also
gives a wake cycle room to finish its work (the work conversation was cut after
5 to 10 tool steps; pictures now count by their size, and it can look at 8 a
cycle), tells the reflection why the work ended, refuses Etsy's whole top-level
departments as a listing's category, and keeps the AI note out of the footer of
CVs and letters that buyers send on.

0.9.1 keeps each of the owner's messages in the agent's plans until one of its
messages answers it (a cycle that ended before its reply used to lose the
owner's questions); the Inbox shows which ones are answered.

0.19.0 connects Bluesky: the agent proposes posts on an account the owner made
for it (its words, a link to its listings or the owner's website, a picture),
and after the owner approves one, Ember's code posts it with a line saying an
AI wrote it, through an app password (never the account's own password; the
login stays in memory, only bsky.social and Bluesky's own servers). It never
mentions, replies to, follows, likes or messages anyone. Followers and each
post's likes, reposts, replies and quotes come back into the agent's plans.
Expect little from it: the [Bluesky section of the docs](ember/DOCS.md#bluesky)
says why.

0.25.0 adds Amazon KDP, which has no API, so it works like Reddit: the agent
writes a book (an ebook's Word manuscript, or a paperback's interior at a KDP
trim size) and describes it in a JSON spec; Ember's code makes its cover from
the agent's front picture (a paperback's full wrap, as wide as the pages make
the spine), checks it against KDP's rules (sizes, page counts, margins, the
cover's width, the words and the price), and after the owner approves it, the
owner publishes it at KDP from their own account with the card's fields and
files. See the [Amazon KDP section of the docs](ember/DOCS.md#amazon-kdp).

0.28.0 makes every wake cycle about one thing: an ordinary cycle works on one
product line it takes from a list Ember's code ranks (what a line owes, its
milestone due, whether it has work, when it was last worked on), and Ember's
code refuses another line's work for the rest of the cycle. Marketing gets
cycles of its own with a new option, **Share for marketing** (20 % of each day's
spending by default, like the ventures' 25 %): a marketing cycle brings buyers
to one line's live listings with pins, Bluesky posts, blog posts and better
titles and tags. Venture cycles are unchanged. The Ventures tab's Running view
gets a **Line desk** and the Activity list says what each cycle was about. See
[Marketing cycles](ember/DOCS.md#marketing-cycles).

0.29.0 puts your goal at the head of the roadmap: you set it on the Roadmap tab
(earn an amount in USD a month, or in total, by a date), Ember's code checks it
from the books, and everything on the roadmap leads to it. The agent splits it
into sub-goals, this month's milestones and this week's steps; every milestone
shows how far it got (by its metric, or by the steps that lead to it) and
whether it keeps its pace, on the Roadmap tab's goal tree and in every plan. Until
you set one, the money goal Ember's code keeps stands in for it. See the
[Roadmap section of the docs](ember/DOCS.md#roadmap).

0.30.0 gives Ember a plan she keeps and a playbook that grows. The list her plans
take a line from put the line worked on longest ago first, so nearly every cycle
switched lines (13 dry-run cycles on four lines switched 11 times) and no line was
finished before the next began. Now she finishes what she starts (the line in
progress first, three cycles in a row at most), then works on the week's focus lines,
which her weekly look picks toward your goal, and on the changes her daily review
asked for. Each lesson of her retrospectives joins her playbook the day it is
written, and you can read it, with this week's look, under **Mind → Playbook**. See
[The learning loop](ember/DOCS.md#the-learning-loop).

0.31.0 lets you choose one by one what wakes Ember. Under **Wake Ember when you
decide** each kind of decision has its own switch (approving a request, rejecting
one, marking one done or failed, ventures, the roadmap), and under **Wake Ember
for events** each kind of event (a reply to her email, a new email, a milestone's
last day): she can wake for your approvals but not for your messages, for example.
All are on by default, so nothing changes until you turn one off. See the
[options](ember/DOCS.md#options).

0.32.0 fixes what the diagnostics of 2026-10-07 showed: one tool call in five was
refused, most for reasons that weren't Ember's. Her posts and pins may now link
the posters Printify made (refused three times as "not one of your live
listings"), English posts get the 258 characters their AI line leaves (they were
held to the German 239), a refused venture proposal keeps the rest of its update
and names everything it still needs at once, `workspace_write` can replace one
passage of a file (rewriting the budget planner's specs in parts ran out of writes
and left them cut off), `make_spreadsheet` names a formula that points at a header
row, and her release notes no longer start again with each update. See
[the report](analysis-0.32.0/ember-diagnostics-0.32.0.md).

0.33.0 makes what Ember promised you and what you decided come before what her
code generates. A promise now names its project, and that project comes first;
missed milestones no longer force cycles onto their lines or keep marketing away
(on 2026-10-07 four of them took the next cycles for title and tag edits her
review had ruled out, while a KDP book promised three times got none), and a
missed day-7 views bar asks for buyers rather than new titles. A message that
promises work only in words comes back to her to record it; any cycle may keep
another line's record up to date; her work steps see why the plan chose the
work, the review's verdict and her strategy; the review sees which channels are
ready. `workspace_write` can change every copy of a passage at once and restore
a file's text from before an overwrite or delete, the workshop's budget hold no
longer refuses runs late in the day, and **Keep as instruction** on your
messages in the Inbox turns one into a standing instruction.

0.34.0 starts Release 2: one plan tree under your goal that will decide what
Ember works on next. Your goal sits at the top; under it each platform (Etsy,
KDP, Printify, the website, the channels), each product line as a product, and
each product's stages (research, create, release, launch, maintain) with small
steps whose checks Ember's code reads from her records, so a stage closes only
on a result. A promise that names a project becomes a step of that product. In
this version the tree runs in the shadow: every cycle records the step it would
have taken, with its weight's parts, next to what READY took, so a week of real
cycles can tune the weights before the tree steers. The new **Plan** tab shows
the tree as a floating node network, the step the tree would take now, what
waits on you and the shadow picks; there you can pin a step or set a product's
worth.

0.35.0: the plan tree steers. Before each cycle Ember's code takes its step from
the tree (what you pinned, a promise or your decision that is due, the
ventures' turn, else the heaviest step), and the step decides what the cycle is
and which product line it works on; Ember sees it as YOUR STEP, and the whole
plan in a few lines as YOUR PLAN. She changes her own steps with their reasons
(add, split, replace, done, wait on something her code can check) and can hold a
product to work elsewhere, but only you close or drop one. READY's ranking, the
marketing share, the listing test's bars, the goal's decision points and
Ember's own milestones retire: each product with a live listing has decide-by
dates (day 7, 14 and 21), where you keep it or drop it. The Plan tab takes the
Roadmap tab's place: your goal, the tree, the product you open with its test's
dates, its Autonomy box and your Close, Drop, Keep and Lift-hold buttons, what
changed today, the channels, the upgrades the plan waits on, and your
milestones.

0.35.1: a promise comes first, and Ember doesn't sleep hours while there is work.
A promise to you is a step of its product from the moment it is made, taken
before the heaviest step and the ventures' turn until it is kept; one made
without naming its product gets the one its words name (a listing's number, KDP),
and a promise of pins makes a marketing cycle. While the plan has a step ready,
any cycle, a venture cycle too, sleeps your shortest sleep: your daily cap is the
brake.

0.36.0: the plan decides when Ember explores, and your word on it is kept by
Ember's code. The venture share is gone (on 2026-10-09 it forced 10 of 24 cycles
into venture work that Ember, told "nothing new", left undone): your ventures are
one Explore step of the plan, weighed like any step at a worth you can set, and
**Hold new things** on the Plan tab means "nothing new": no venture cycle and no
new product. **Hold it** holds a product yourself (Ember can't lift your hold),
and **Freeze** keeps your listings' titles and tags as they are until a day you
choose: Ember's code refuses such edits and the critic asks for none. The
standing instructions became a **rulebook**: rules for how Ember works, each
kept until you remove it (live, a new text for one week's order of work had
replaced the rules set before it). What comes next is the plan's. A live
product's first pins now come before the critic's suggestions (0.35.3).

0.37.0: everything Ember does is a step of the plan, weighed like any other.
Each venture being explored is a node of the plan's Ventures, its next decision
(triage an idea, research it, answer the critic, your decision on its case) a
step worth what its business case expects, an idea by its scores; a venture
cycle works on that step, aimed at its venture, in place of the ranked READY
list it picked from. The Ventures tab is now part of the Plan tab: the tree with
each venture's step (**Explore next** pins one), and the product lines. Promises
to you and your decisions are weighed too: worth more than most products,
heavier as their day nears, and no longer first whatever else waits; one about
no product is a step of the plan's Owner project.

0.37.1: your promises weigh about what a product does, and press as their day
nears. On 0.37.0's first evening every promise weighed at least 15 and no step of
a product more than about 12, so they still came first: the pins you put first
for the week ranked tenth, behind nine promises and decisions, four of them one
KDP job (see [the analysis](analysis-0.37.0/ember-analysis-0.37.0.md)). A
promise is worth 3 now and urgent only from two days before its day; one taken
three times in a day without being kept weighs its worth alone until the day is
over.

0.37.2: near the bottom of the balance a workshop run can no longer end Ember's
life below zero, your Anthropic account paying the rest. Since 0.33.0 a run kept
back no more than the day had left, but that limit also held a fifth of the
balance, so the rule that a run needs 5 times its hold above the last will's
reserve always passed: in the codebase analysis of 0.37.0 (finding 3.4), a run
went ahead holding $0.62 with $3.10 left, cost $3.21, and Ember died at -$0.08
without its last will. Now only the day limits what a run keeps back, and the
balance counts all of it.

0.37.3 closes three gaps in timing at the approval boundary that the codebase
analysis of 0.37.0 found (its 4.1.1 to 4.1.3). An approved email goes out only
after Ember's code has read its mailbox since your approval: a "stop" that came
while the kill switch was on, the app was down or reading failed was read after
the first round had sent to its writer (§ 7 UWG). The kill switch stops a round
that is already sending (2 of 3 approved emails went out after it was pressed),
and the live page isn't uploaded while it is on. A request expires on its day
before any unlock acts (on Resume after 8 days paused, an unlock approved an
8-day-old reply), and a veto window that a pause interrupted gets its whole 12
hours again once Ember runs.

0.37.4: Ember can read her Bluesky posts' numbers in any cycle but a venture
cycle. Since 0.35.0 `bluesky_posts` was offered only with the marketing tools,
so an ordinary cycle (the one that answers you) couldn't say how a post did.
Now it lists each post by its request number with its likes, reposts, replies
and quotes, the account's followers and the posts made today against the daily
limit, and an ordinary cycle's plan and the daily review show the live posts'
reactions, in all and per post.

## Install in Home Assistant

1. In Home Assistant open **Settings → Apps** (called *Add-ons* before HA 2026.2),
   open the app store, and choose **⋮ → Repositories**.
2. Add `https://github.com/Stullee/SURVIVE` and close the dialog.
3. Find **Ember** in the store, install it, and start it. The Supervisor builds
   the image on your device from `ember/Dockerfile` (a few minutes on a Raspberry Pi).
4. On the app's **Info** tab turn on **Watchdog** (restarts Ember if it crashes)
   and **Show in sidebar**, then open Ember.

Requirements: a Home Assistant installation with apps (Home Assistant OS) on amd64 or aarch64.

## Configuration

All options, their defaults and the default price table are documented in
[`ember/DOCS.md`](ember/DOCS.md), which Home Assistant also shows on the app's
**Documentation** tab, including the steps for going live.

The default prices come from Anthropic's pricing page as of 2026-09-27
(Sonnet 5: $2 / $10 per million input / output tokens, Opus 5.5: $4 / $20, web
search $10 per 1,000). **Please verify them** before switching dry run off.

Both the planner and the worker default to `claude-sonnet-5`. For better
business decisions, plan with `claude-opus-5-5` and keep the worker on Sonnet 5
(see [Choosing models](ember/DOCS.md#choosing-models)). The spec suggested
Haiku 4.5 as a cheaper worker; to use it (or any other model), add its prices to
the price table and set `worker_model`. Haiku 4.5 is not deprecated as of
2026-09-27: Anthropic lists its retirement as "not sooner than 2026-10-15" and
gives at least 60 days' notice before retiring a model.

## Home Assistant sensors

Ember never calls Home Assistant's API. Home Assistant can poll
`http://<app hostname>:8099/api/sensors` with a REST sensor instead; the exact URL
and a ready-made YAML snippet are in [`ember/DOCS.md`](ember/DOCS.md#home-assistant-sensors-optional).

## Security model

- **Ingress only.** The server accepts connections from the Supervisor's Ingress
  proxy (172.30.32.2) and nothing else, except `GET /api/sensors` from the Home
  Assistant host network (172.30.32.1: Home Assistant Core, but also apps that use
  host networking). That endpoint only returns non-sensitive numbers. Enforced
  in `ember/app/security.py`.
- **Least privilege.** `homeassistant_api: false`, `hassio_api: false`, no host
  network, no mapped folders besides the app's own `/data`, no published ports.
  `ember/tests/test_manifest.py` fails if any of this is loosened.
- **Options can't be changed from inside.** The Supervisor hands every app a
  token that can change the app's own options or uninstall it, even with
  `hassio_api: false`; the s6 run script deletes it before Python starts. And
  whenever the Python process exits, the whole container stops instead of
  restarting in place, so a restart always goes through the Supervisor, which
  rewrites `/data/options.json` from the options you saved.
- **The agent's reach.** Its file tools work inside its own folders only
  (`ember/app/agent/sandbox.py`: no links, no absolute paths, size quotas). Its
  tool handlers run with sockets, subprocesses and native libraries blocked by
  an audit hook (`ember/app/agent/netguard.py`); in dry run the whole wake cycle
  runs that way. The live transport only reaches `https://api.anthropic.com`
  and ignores proxy variables (`ember/app/economy/anthropic_transport.py`).
  The container talks to `api.anthropic.com` and, if the owner sets up Ember's
  mailbox, to the configured IMAP and SMTP hosts, over verified TLS, from
  Ember's own code only (`ember/app/integrations/mail.py`, never a tool
  handler). With the blog on (0.14.0), Ember's code also uploads the pages the
  owner approved over SFTP to the owner's web host, only to a server showing the
  pinned key and only a post, the blog's list and the link page
  (`ember/app/integrations/sftp.py`, `site_publisher.py`). With the live view on
  (0.16.0), it uploads the live page, its banner and its chart there every 15
  minutes (never while the kill switch is on, 0.37.3), made from Ember's own
  numbers, with the agent's titles and last will only as the owner approved
  each one (`live_view.py`). With Bluesky on
  (0.19.0), Ember's code logs in to `bsky.social` with the account's app
  password and posts what the owner approved to the account's own server at
  Bluesky, and nowhere else (`ember/app/integrations/bluesky_live.py`).
- **Outside actions.** The agent has no tool that sends or posts anything. An
  email it proposes is sent by Ember's code only after the owner approves it,
  exactly as approved, once, to one recipient, with a footer saying an AI wrote
  it and within a daily limit, and (0.37.3) only after Ember's code read its
  mailbox since the approval, so a "stop" waiting there is seen first; a send is
  recorded before it starts and never retried
  (`ember/app/integrations/executor.py`). The kill switch stops whatever
  Ember's code hasn't begun, also in the middle of a round (0.37.3). A Reddit
  post becomes a link the owner opens and posts from their own account; a KDP
  book (0.25.0) becomes a package the owner publishes at KDP from their own
  account (Ember's code never reaches Amazon).
- **CSRF.** State-changing requests must carry an `X-Ember-Request: 1` header,
  which cross-site pages can't send.
- **Strict CSP.** No inline scripts or styles; Chart.js is vendored, nothing is
  loaded from a CDN.
- **Secrets.** The API key and the mailbox's app password live only in the app
  options. They are excluded from the dashboard, the API and the diagnostics
  (which only say whether they are set), and every log line is scrubbed of
  both and of anything that looks like an Anthropic key.
- **Never crashes on bad input.** Invalid options start *safe mode* (defaults,
  dry run forced on); a broken database is reported in the dashboard while the
  web UI keeps running.

## Local development

### Without Docker

```bash
cd ember
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -n auto         # run the tests, one worker per CPU
EMBER_DATA_DIR=../dev/data-local EMBER_DEV_MODE=1 python -m app
# open http://localhost:8099
```

`EMBER_DATA_DIR` replaces `/data` (a different folder from the one docker-compose
uses, whose files belong to root); `EMBER_DEV_MODE=1` turns off the Ingress IP
filter (there is no Ingress proxy locally). Never set it inside Home Assistant.
In dev mode the server listens on 127.0.0.1 only (override with `EMBER_HOST`)
and answers only requests addressed to `localhost`, `127.0.0.1` or `[::1]`, which
stops other machines and DNS-rebinding web pages from reaching it.

### With docker-compose

```bash
mkdir -p dev/data && cp dev/options.example.json dev/data/options.json
docker compose up --build
# open http://localhost:8099
```

The container is the same image Home Assistant builds; `dev/data` plays the role
of the app's `/data` folder (edit `dev/data/options.json` to change options). The
port is bound to localhost only, because the dashboard has no login of its own.

In the dashboard, **System → Preview** switches the preview data between the
alive, paused, critical and dead states (the dead state shows the memorial).

### Checks

```bash
cd ember
python -m pytest -n auto
ruff check app tests && ruff format --check app tests
```

GitHub Actions runs the same checks and builds the image for both architectures
(`.github/workflows/ci.yml`).

### Secret scanning

This repository is public, and everything ever committed stays in its history.
[gitleaks](https://github.com/gitleaks/gitleaks) scans every commit in CI (the
"Secret scan" job) with gitleaks' own rules plus one in `.gitleaks.toml` for
generated passwords (three groups of six letters and digits, as Apple's are),
which neither gitleaks nor GitHub's push protection knows. CI only finds a secret
after it was pushed; to stop it before the commit, install the hook:

```bash
pip install pre-commit && pre-commit install    # runs gitleaks on every commit
```

Test data that needs the shape of a secret uses an obviously fake value, listed
in the rule's allowlist. `.gitleaksignore` lists the findings in old commits that
the scan skips (by commit, file and line, never the value): the password
committed in 0.4.0, which has to be changed where it is used.

## Releasing a new version

1. Bump `version` in `ember/config.yaml` (semantic versioning, e.g. `0.2.0`).
2. Add a section at the top of `ember/CHANGELOG.md` whose heading is exactly
   `## 0.2.0` (Home Assistant matches this heading to show release notes). Write
   it for the agent too: Ember reads new changelog entries at its first wake-up
   after an upgrade to learn what changed about itself.
3. Database changes go in a new migration, `ember/app/migrations/000N_name.sql`
   (numbered without gaps, never edit a released one; a number another branch
   holds may be skipped until that branch is merged: `RESERVED` in
   `ember/app/db.py`, 0070 for Google Search Console). Ember backs up the
   database to `/data/backups` before applying it.
4. Run the checks, commit, and push the commit to a candidate branch first (for
   example `release/0.2.0`): CI runs on every push. Wait until every CI job is
   green on that exact commit (tests and lint, the secret scan, both image
   builds). The secret scan reads every branch on GitHub, so a finding on any
   branch makes it red.
5. Only then fast-forward the branch Home Assistant tracks to that commit, push
   it, and tag it: `git tag v0.2.0 <commit> && git push origin v0.2.0`. A push
   to the tracked branch is the release, so never push a commit there that CI
   hasn't passed.
6. In Home Assistant, the app shows an update (reload the store to see it sooner).
   Updating rebuilds the image on the device.

The Docker base image is pinned in `ember/Dockerfile`
(`ghcr.io/home-assistant/base-python:3.12-alpine3.24-2026.08.0`). Bump it
deliberately, in its own release, after checking the
[docker-base releases](https://github.com/home-assistant/docker-base/releases).
Python dependencies are pinned in `ember/requirements.txt`.

## Repository layout

```
repository.yaml              Home Assistant app repository metadata
docker-compose.yml           local development outside Home Assistant
dev/options.example.json     options for local development
ember/                       the app
  config.yaml                app manifest (options, schema, Ingress, permissions)
  Dockerfile                 image build (no build.yaml; see below)
  CHANGELOG.md, DOCS.md, README.md, icon.png, logo.png, translations/en.yaml
  requirements.txt           pinned runtime dependencies
  rootfs/                    s6-overlay v3 service that starts `python3 -m app`
  app/
    main.py                  FastAPI app, startup and shutdown
    config.py                options loading and validation
    db.py, migrations/       SQLite and numbered migrations
    security.py              Ingress IP filter, CSRF check, security headers
    web.py, web/             routes, dashboard HTML/CSS/JS, vendored Chart.js
    economy/                 ledger, life states, cost estimates, budget guard (metering.py)
    agent/                   the wake cycle, tools, context, fake model (constitution.md: the fixed prompt core)
    integrations/            Ember's mailbox (IMAP/SMTP), the executor of approved emails, Reddit links,
                             Etsy, Pinterest, Bluesky, Printify, the blog's SFTP uploads
  tests/
```

## Differences from the original spec

Where current Home Assistant or Anthropic documentation differs from the spec,
the documentation wins:

- **"Add-ons" are now "apps"** (Home Assistant 2026.2). The file format and keys
  are unchanged; wording in the docs follows the new name.
- **No `build.yaml`.** It is deprecated; the Supervisor stopped passing
  `BUILD_FROM` in 2026.04. The base image is set directly in the `Dockerfile`.
- **Multi-arch base image** `ghcr.io/home-assistant/base-python:3.12-alpine3.24`
  (pinned) instead of per-architecture images.
- **Sensor endpoint readable from the Home Assistant host network.** Ingress
  requires a browser session, so a REST sensor can't use it. `GET /api/sensors`
  is also accepted from the host-network gateway (172.30.32.1: Home Assistant
  Core, but also apps with host networking and host processes); everything else
  stays Ingress-only.
- **Price table shape.** Home Assistant option schemas allow nesting two levels
  deep, so `price_table` is a list of per-model entries (with separate 5-minute
  and 1-hour cache-write prices, as Anthropic bills them) and the web-search
  price is a separate option, `web_search_usd_per_1000`.
- **Extra options:** `min_sleep_minutes`, `max_sleep_minutes`, `max_tool_steps`
  (the spec calls them configurable) and `log_level`.
- **Cold backups** (`backup: cold`): the app pauses while Home Assistant takes a
  backup so the SQLite file is consistent.
- **Worst-case estimates include server tools and caching.** The spec's
  estimate (input tokens + max_tokens) would miss web-search charges, cache
  writes and the server-side tool loop, so the guard prices those too and
  refuses requests it can't bound (for example a server tool without
  `max_uses`).
- **Death and the last will.** "Dead at balance ≤ 0" alone would let the agent
  die without its last will, or linger unable to afford any call. Ember keeps a
  small reserve for the last will, dies when the balance is used up *or* when
  it can no longer afford its last will ("starved"), and turns critical when it
  can't afford a planning call. Leaving critical needs 4 days of runway and new
  money, so the state doesn't flip back and forth around 2 days.
- **Extra ledger types and states.** `api_cost_correction` (align with the
  Console) and typed corrections instead of edits; a state `unfunded` for an
  agent that has no money yet (not dead: it never lived).
- **Dry-run economy.** Simulated costs use a separate test balance per dry-run
  session, so a dry run never touches the live balance.

## License

No license has been chosen yet. Chart.js (vendored in
`ember/app/web/static/vendor/`) is MIT-licensed; its license file sits next to it.
