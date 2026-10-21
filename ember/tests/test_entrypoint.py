from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.__main__ import bind_host

APP_DIR = Path(__file__).resolve().parent.parent
RUN_SCRIPT = APP_DIR / "rootfs/etc/s6-overlay/s6-rc.d/ember/run"
# 0.41.0: the run script starts Python as the unprivileged user ember (see the Dockerfile).
DROP = "exec s6-setuidgid ember python3 -m app"


def test_inside_home_assistant_listens_on_all_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBER_HOST", raising=False)
    monkeypatch.delenv("EMBER_DEV_MODE", raising=False)
    assert bind_host() == "0.0.0.0"


def test_dev_mode_listens_on_localhost_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBER_HOST", raising=False)
    monkeypatch.setenv("EMBER_DEV_MODE", "1")
    assert bind_host() == "127.0.0.1"


def test_bind_address_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBER_DEV_MODE", "1")
    monkeypatch.setenv("EMBER_HOST", "0.0.0.0")
    assert bind_host() == "0.0.0.0"


def test_supervisor_tokens_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.__main__ import drop_supervisor_tokens

    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setenv("HASSIO_TOKEN", "tok")
    drop_supervisor_tokens()
    import os

    assert "SUPERVISOR_TOKEN" not in os.environ
    assert "HASSIO_TOKEN" not in os.environ


def _run_code() -> list[str]:
    """The run script's commands, without comments, continuation lines joined."""
    text = RUN_SCRIPT.read_text().replace("\\\n", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def test_run_script_removes_supervisor_tokens() -> None:
    run = RUN_SCRIPT.read_text()
    before_exec = run[: run.index(DROP)]
    assert "unset SUPERVISOR_TOKEN HASSIO_TOKEN" in before_exec
    assert "/run/s6/container_environment/SUPERVISOR_TOKEN" in before_exec
    assert "/run/s6/container_environment/HASSIO_TOKEN" in before_exec
    assert "unset HTTP_PROXY HTTPS_PROXY" in before_exec
    assert "ANTHROPIC_" in before_exec
    # 0.41.0: and the rest of the s6 copy of the environment is root's only.
    assert "chmod 0700 /run/s6/container_environment" in before_exec


def test_python_runs_as_the_user_ember_not_root() -> None:
    """0.41.0: the last command drops root for good; nothing after it, nothing that runs Python as root."""
    code = _run_code()
    assert code[-1] == DROP
    assert sum("python3" in line for line in code) == 1
    # A failed drop ends the script (and the container): there is no fallback that starts Python as root.
    assert "||" not in code[-1]
    # Ember's home, not /root (which ember can't read), for any library that keeps a cache.
    assert "export HOME=/home/ember" in code


def test_data_belongs_to_ember_except_the_options() -> None:
    """0.41.0: files an earlier version wrote as root become ember's, without following links."""
    code = _run_code()
    chown = next(line for line in code if line.startswith("find /data "))
    assert "-mindepth 1" in chown  # /data itself stays root's (see below)
    assert "! -path /data/options.json" in chown
    assert "! -user ember -o ! -group ember" in chown  # only what isn't ember's yet: a large workspace stays as it is
    assert "-exec chown -h ember:ember {} +" in chown  # -h: a link is changed itself, never its target
    # /data is root's, writable by ember and sticky: ember can't remove or replace a file that isn't its own.
    assert "mkdir -p /data && chown root:ember /data && chmod 1775 /data" in " ".join(code)


def test_options_stay_read_only_for_ember() -> None:
    """0.41.0: the Supervisor writes options.json as root at each start; ember may read it, not change it."""
    code = _run_code()
    text = "\n".join(code)
    assert "chown root:ember /data/options.json && chmod 0640 /data/options.json" in text
    # After the chown of /data's files (a hard link to it there would otherwise hand it to ember).
    assert text.index("chown root:ember /data/options.json") > text.index("-exec chown -h ember:ember")
    # Never through a link (chmod would follow it), and an absent file (no options in dev) is no error.
    assert "if [[ -L /data/options.json ]]; then" in code
    assert "elif [[ -f /data/options.json ]]; then" in code
    # All of this happens as root before the drop.
    assert text.index("chmod 0640 /data/options.json") < text.index(DROP)


def test_run_script_preparation_only_warns() -> None:
    """bashio runs scripts with errexit: a chown the Supervisor's sandbox refuses must not stop an update from starting.
    Only the privilege drop itself is fatal."""
    code = _run_code()
    for needle in ("chmod 0700 /run/s6", "chmod 1775 /data", "-exec chown -h", "chmod 0640 /data/options.json"):
        line = next(line for line in code if needle in line)
        assert "|| bashio::log.warning" in line, line


def test_dockerfile_makes_the_user_and_keeps_root_for_s6() -> None:
    dockerfile = (APP_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "addgroup -S -g 1000 ember" in dockerfile
    assert "adduser -S -D -u 1000 -G ember -h /home/ember -s /sbin/nologin ember" in dockerfile
    # s6-overlay must start as root (PID 1); the run script drops privileges itself.
    assert not re.search(r"^USER\b", dockerfile, flags=re.MULTILINE)
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile  # /opt/ember isn't writable by ember
    assert "PYTHONNOUSERSITE=1" in dockerfile  # nothing from ember's writable home is imported


def test_uvicorn_runs_with_safe_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    import app.__main__ as entry

    captured: dict = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(entry, "app_factory", lambda: "app")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    entry.main()
    assert captured["server_header"] is False
    assert captured["proxy_headers"] is False  # the client address must be the real peer
    assert captured["log_config"] is None  # keeps the redacting log setup
    assert captured["access_log"] is False
    assert captured["timeout_graceful_shutdown"] <= 3  # well inside the Supervisor's 10 s stop timeout
    assert "SUPERVISOR_TOKEN" not in os.environ


def test_finish_script_always_stops_the_container() -> None:
    """Restarting in place would re-read /data/options.json, which code in the container could edit."""
    finish = (APP_DIR / "rootfs/etc/s6-overlay/s6-rc.d/ember/finish").read_text()
    code = [line.strip() for line in finish.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert code[-1] == "exec /run/s6/basedir/bin/halt"
    # The halt is not inside any if-block: every branch ends before it.
    depth = 0
    for line in code[:-1]:
        depth += line.startswith("if ") - (line == "fi")
    assert depth == 0
