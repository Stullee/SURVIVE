# Ember

An AI agent that has to earn more than its API calls cost.

Ember runs on your Home Assistant server, pays for every model call from a
balance you fund, and looks for honest ways to earn money. You approve anything
it wants to do in the outside world and you record its revenue. It can run out
of money and die, and that is an acceptable outcome.

Dashboard through Ingress, hard spending caps enforced in code, dry-run mode
with a fake model so you can try everything without spending money. Optionally
Ember gets its own mailbox: it reads its mail on its own and sends an email
only after you approve it.

**Status: all five phases in, live testing (dry run is the default).**
