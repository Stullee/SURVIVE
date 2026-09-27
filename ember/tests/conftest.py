from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import LoadedSettings, Settings, load_settings
from app.main import create_app

INGRESS = ("172.30.32.2", 50000)
HA_CORE = ("172.30.32.1", 50000)


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own empty /data."""
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setenv("EMBER_DATA_DIR", str(directory))
    monkeypatch.delenv("EMBER_OPTIONS_PATH", raising=False)
    monkeypatch.delenv("EMBER_DEV_MODE", raising=False)
    # Tests run wake cycles explicitly; the background scheduler only keeps the economy current.
    monkeypatch.setenv("EMBER_SCHEDULER", "off")
    monkeypatch.setenv("EMBER_FAKE_DELAY_MS", "0")
    return directory


@pytest.fixture
def write_options(data_dir: Path) -> Callable[[dict], Path]:
    def write(options: dict) -> Path:
        path = data_dir / "options.json"
        path.write_text(json.dumps(options), encoding="utf-8")
        return path

    return write


@pytest.fixture
def client_factory() -> Callable[..., Iterator[TestClient]]:
    @contextmanager
    def make(
        loaded: LoadedSettings | None = None, *, dev_mode: bool = False, client: tuple[str, int] = INGRESS
    ) -> Iterator[TestClient]:
        app = create_app(loaded or load_settings(), dev_mode=dev_mode)
        with TestClient(app, client=client) as test_client:
            yield test_client

    return make


@pytest.fixture
def ingress_client(client_factory: Callable[..., Iterator[TestClient]]) -> Iterator[TestClient]:
    with client_factory(LoadedSettings(Settings())) as client:
        yield client
