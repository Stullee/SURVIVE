<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->
<!-- Headings must be exactly "## <version>" so Home Assistant shows the right release notes.
     Ember reads this file after every upgrade: describe changes so the agent understands
     what it can now do differently. -->

## 0.1.0

- Phase 1 skeleton: app manifest, Ingress dashboard with preview data, SQLite database with migrations.
- The agent itself does not run yet. Nothing calls the Anthropic API and nothing costs money.
