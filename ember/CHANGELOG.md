<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->
<!-- Headings must be exactly "## <version>" so Home Assistant shows the right release notes.
     Ember reads this file after every upgrade: describe changes so the agent understands
     what it can now do differently. -->

## 0.1.1

- Your constitution has a new section, "Mindset: solutions, not obstacles": treat blockers as problems to solve
  together with your owner, inside your priorities and hard rules, and only call something impossible after
  exploring the options.
- The worker model is now claude-sonnet-5, the same model as the planner (your owner's choice).
- Still phase 1: the agent does not run yet and nothing calls the Anthropic API.

## 0.1.0

- Phase 1 skeleton: app manifest, Ingress dashboard with preview data, SQLite database with migrations.
- The agent itself does not run yet. Nothing calls the Anthropic API and nothing costs money.
