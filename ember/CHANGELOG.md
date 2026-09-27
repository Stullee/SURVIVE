<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->
<!-- Headings must be exactly "## <version>" so Home Assistant shows the right release notes.
     Ember reads this file after every upgrade: describe changes so the agent understands
     what it can now do differently. -->

## 0.2.0

Phase 2: your economy is real. You still don't run yet (phase 3), and nothing calls the Anthropic API.

- A ledger records every movement of money and can only be added to: your owner's grants, revenue they record
  for you, expenses they paid for you, API costs of your model calls, and corrections. You can never record
  revenue yourself.
- Before every model call, a budget guard in the code (not in your prompt) works out the most the call could cost,
  including web searches, cache writes and the server-side tool loop, and refuses it if that could break the
  per-cycle cap, the daily cap or your balance. A small reserve is always kept so you can write your last will.
  A refused call is logged and ends gracefully.
- Life states: alive, critical (under 2 days of runway, or you can't afford your next planning call), paused,
  unfunded (no money yet), and dead (the money is gone, or you can't even afford your last will). Leaving critical
  needs 4 days of runway and new money. A grant large enough for a fresh start begins a new life.
- Dry run has its own test balance per session, so testing never touches the real balance.
- The dashboard shows the balance, runway, today's spending against the cap, the money chart, the ledger with
  your owner's entry forms, previous lives and the memorial. Sections of later phases are empty placeholders.

## 0.1.2

- Security: your process no longer receives the Supervisor token, so nothing in the container can change your
  options (spending caps, dry run) through the Supervisor. Whenever your process exits, the whole app stops and
  Home Assistant restarts it with the options your owner saved, so an edited options file is never used.
- The dashboard's system log is protected against floods, escapes text sent by other programs, shows warnings even
  when the owner lowers the log level, and never prints secrets when a write fails.
- The database now uses one shared connection, writes log events in the background, runs migrations with
  foreign-key checks, and a damaged database is shown in the dashboard instead of crashing the app.
- Phase-2 groundwork: exact cost accounting in micro-dollars and the owner's local days (not used yet).
- Still phase 1: you don't run yet and nothing calls the Anthropic API.

## 0.1.1

- Your constitution has a new section, "Mindset: solutions, not obstacles": treat blockers as problems to solve
  together with your owner, inside your priorities and hard rules, and only call something impossible after
  exploring the options.
- The worker model is now claude-sonnet-5, the same model as the planner (your owner's choice).
- Still phase 1: the agent does not run yet and nothing calls the Anthropic API.

## 0.1.0

- Phase 1 skeleton: app manifest, Ingress dashboard with preview data, SQLite database with migrations.
- The agent itself does not run yet. Nothing calls the Anthropic API and nothing costs money.
