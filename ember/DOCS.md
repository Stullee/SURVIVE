# Ember

Ember is an AI agent that lives in this app and tries to pay for itself. Every
call it makes to Anthropic's API costs real money from a balance you fund; it has
to find honest ways to earn more than it spends. You stay in control: anything
that leaves the container (publishing, contacting people, spending money) needs
your approval, and only you can record revenue.

> **Current status: phase 1 of 5.** The dashboard shows made-up preview data so
> you can review the layout. The agent does not run yet, nothing calls the
> Anthropic API, and nothing costs money.

## Getting started

1. Start the app and open **Ember** from the sidebar (or **Open web UI**).
2. Leave **Dry run** on. In dry run the agent uses a built-in fake model and
   never calls the API.
3. When the agent is ready (phase 5), add your Anthropic API key, check the
   price table against Anthropic's pricing page, and turn dry run off.

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
| Log level | info | Detail in the app log. |

Default prices (USD per million tokens, from Anthropic's pricing page on
2026-09-27; **check them before going live**):

| Model | Input | Output | Cache write 5 min | Cache write 1 h | Cache read |
|---|---|---|---|---|---|
| claude-sonnet-5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.20 |

To use another model (for example a cheaper worker), add its prices to the
table. Both the planner and the worker model must be listed there.

If the options are inconsistent (for example a cycle cap above the daily cap),
Ember starts in **safe mode**: built-in defaults, dry run forced on, and the
problem shown at the top of the dashboard.

## Home Assistant sensors (optional)

Ember never calls Home Assistant. Instead, Home Assistant can read a small JSON
document from Ember with a [RESTful sensor](https://www.home-assistant.io/integrations/sensor.rest/).
The exact URL is shown in Ember's dashboard under **System → Sensor URL**; for a
locally installed app it is `http://local-ember:8099/api/sensors`. Add this to
your `configuration.yaml` and restart Home Assistant:

```yaml
rest:
  - resource: http://local-ember:8099/api/sensors
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

While phase 1 is installed these values are preview data (`"mock": true`).

## Security

- The dashboard is only reachable through Home Assistant Ingress, so only
  logged-in Home Assistant users can open it. Direct connections are refused.
  The one exception is `/api/sensors`, which Home Assistant itself may read.
- The app has no access to the Home Assistant or Supervisor API, runs without
  host networking and maps no folders besides its own `/data`.
- The API key is kept in the app options and never written to logs, the
  database or the dashboard.

## Data and backups

Everything Ember keeps lives in the app's `/data` folder: `ember.db` (SQLite),
and later the agent's workspace and memory files. Home Assistant backups include
it. The app is stopped briefly while a backup is taken so the database is copied
in a consistent state. Before a database upgrade, Ember also keeps a copy in
`/data/backups`.
