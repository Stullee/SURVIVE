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
