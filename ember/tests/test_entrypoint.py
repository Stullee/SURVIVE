from __future__ import annotations

import pytest

from app.__main__ import bind_host


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


def test_run_script_removes_supervisor_tokens() -> None:
    from pathlib import Path

    run = (Path(__file__).resolve().parent.parent / "rootfs/etc/s6-overlay/s6-rc.d/ember/run").read_text()
    before_exec = run[: run.index("exec python3 -m app")]
    assert "unset SUPERVISOR_TOKEN HASSIO_TOKEN" in before_exec
    assert "/run/s6/container_environment/SUPERVISOR_TOKEN" in before_exec


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
    from pathlib import Path

    finish = (Path(__file__).resolve().parent.parent / "rootfs/etc/s6-overlay/s6-rc.d/ember/finish").read_text()
    code = [line.strip() for line in finish.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert code[-1] == "exec /run/s6/basedir/bin/halt"
    # The halt is not inside any if-block: every branch ends before it.
    depth = 0
    for line in code[:-1]:
        depth += line.startswith("if ") - (line == "fi")
    assert depth == 0
