"""0.42.0: a 401 is Home Assistant's Ingress, never Ember: the dashboard retries quietly while the session renews.

Live, after the page sat idle, the dashboard said "Ember refused the request (HTTP 401: 401: Unauthorized). Reload the
page; ...". Ember's server answers 403 to what it refuses; "401: Unauthorized" is the Supervisor's Ingress proxy, whose
session for the page ran out while the tab slept and which Home Assistant's frontend renews within a minute.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
JS = (APP / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_embers_server_never_answers_401() -> None:
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"status_code\s*=\s*401|HTTP_401", text), path


def test_the_dashboard_retries_a_401_quietly_before_it_asks_for_a_reload() -> None:
    assert re.search(r"var SESSION_RETRY_MS = \d+;", JS) and re.search(r"var SESSION_GRACE_MS = \d+;", JS)
    retry = int(re.search(r"var SESSION_RETRY_MS = (\d+);", JS)[1])
    grace = int(re.search(r"var SESSION_GRACE_MS = (\d+);", JS)[1])
    assert retry <= 10_000 and grace >= 60_000  # Home Assistant renews the session about once a minute
    assert "err.status === 401) {\n        // 0.42.0: Ingress" in JS  # retried at SESSION_RETRY_MS, no system log fetch
    assert "Home Assistant renews it within a minute; retrying." in JS  # a warning first
    assert "ran out and wasn't renewed (HTTP 401). Reload the page" in JS  # the reload advice only after the grace
    assert "ui.sessionLostSince = 0;" in JS  # a good answer ends it
