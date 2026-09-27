<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->
<!-- Headings must be exactly "## <version>" so Home Assistant shows the right release notes.
     Ember reads this file after every upgrade: describe changes so the agent understands
     what it can now do differently. -->

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
