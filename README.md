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
| 2 | Economy: ledger, cost accounting, budget guard, runway, life states, money entry, chart | next |
| 3 | Agent in dry run: wake cycle, fake model, local tools, memory, projects, activity | |
| 4 | Owner loop: approvals, inbox, upgrade requests, controls, changelog awareness | |
| 5 | Go live: real Anthropic client, web search/fetch, prompt caching, final security and cost review | |

In phase 1 the dashboard shows made-up data (with a banner saying so). Nothing
calls the Anthropic API and nothing costs money.

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
**Documentation** tab. Keep **Dry run** on until phase 5.

The default prices come from Anthropic's pricing page as of 2026-09-27
(Sonnet 5: $2 / $10 per million input / output tokens, web search $10 per
1,000). **Please verify them** before switching dry run off.

Both the planner and the worker default to `claude-sonnet-5`. The spec suggested
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
- **No Supervisor token.** The Supervisor hands every app a token that can
  change the app's own options or uninstall it, even with `hassio_api: false`.
  The s6 run script deletes it before Python starts, so nothing in the container
  can raise its own spending caps.
- **CSRF.** State-changing requests must carry an `X-Ember-Request: 1` header,
  which cross-site pages can't send.
- **Strict CSP.** No inline scripts or styles; Chart.js is vendored, nothing is
  loaded from a CDN.
- **Secrets.** The API key lives only in the app options. It is excluded from
  the dashboard and the API, and every log line is scrubbed of anything that
  looks like an Anthropic key.
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
EMBER_DATA_DIR=../dev/data EMBER_DEV_MODE=1 python -m app
# open http://localhost:8099
```

`EMBER_DATA_DIR` replaces `/data`; `EMBER_DEV_MODE=1` turns off the Ingress IP
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
   it for the agent too: from phase 4 on, Ember reads new changelog entries after
   an upgrade to learn what changed about itself.
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
    mock.py                  phase-1 preview data
    agent/constitution.md    the agent's fixed system prompt core
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
- **Sensor endpoint reachable by Home Assistant Core.** Ingress requires a
  browser session, so a REST sensor can't use it. `GET /api/sensors` is also
  accepted from Core's internal address (172.30.32.1); everything else stays
  Ingress-only.
- **Price table shape.** Home Assistant option schemas allow nesting two levels
  deep, so `price_table` is a list of per-model entries (with separate 5-minute
  and 1-hour cache-write prices, as Anthropic bills them) and the web-search
  price is a separate option, `web_search_usd_per_1000`.
- **Extra options:** `min_sleep_minutes`, `max_sleep_minutes`, `max_tool_steps`
  (the spec calls them configurable) and `log_level`.
- **Cold backups** (`backup: cold`): the app pauses while Home Assistant takes a
  backup so the SQLite file is consistent.

## License

No license has been chosen yet. Chart.js (vendored in
`ember/app/web/static/vendor/`) is MIT-licensed; its license file sits next to it.
