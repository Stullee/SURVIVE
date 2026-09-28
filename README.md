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
  handler).
- **Outside actions.** The agent has no tool that sends or posts anything. An
  email it proposes is sent by Ember's code only after the owner approves it,
  exactly as approved, once, to one recipient, with a footer saying an AI wrote
  it and within a daily limit; a send is recorded before it starts and never
  retried (`ember/app/integrations/executor.py`). A Reddit post becomes a link
  the owner opens and posts from their own account.
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
python -m pytest                 # run the tests
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
python -m pytest
ruff check app tests && ruff format --check app tests
```

GitHub Actions runs the same checks and builds the image for both architectures
(`.github/workflows/ci.yml`).

## Releasing a new version

1. Bump `version` in `ember/config.yaml` (semantic versioning, e.g. `0.2.0`).
2. Add a section at the top of `ember/CHANGELOG.md` whose heading is exactly
   `## 0.2.0` (Home Assistant matches this heading to show release notes). Write
   it for the agent too: Ember reads new changelog entries at its first wake-up
   after an upgrade to learn what changed about itself.
3. Database changes go in a new migration, `ember/app/migrations/000N_name.sql`
   (numbered without gaps, never edit a released one). Ember backs up the
   database to `/data/backups` before applying it.
4. Run the checks, commit, and push to the branch Home Assistant tracks.
5. In Home Assistant, the app shows an update (reload the store to see it sooner).
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
    integrations/            Ember's mailbox (IMAP/SMTP), the executor of approved emails, Reddit links
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
