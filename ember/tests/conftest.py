from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.agent import plan
from app.config import LoadedSettings, Settings, load_settings
from app.main import create_app
from tests.economy_helpers import close_databases

INGRESS = ("172.30.32.2", 50000)
HA_CORE = ("172.30.32.1", 50000)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="run only the K-th of N parts of the tests (every N-th test, from the K-th on), as CI's parallel jobs do",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """With --shard K/N, the tests the other N-1 parts run are deselected. Every N-th test rather than one block of
    files: the slow tests (whole wake cycles) bunch up in some modules, and this way each part gets its share."""
    shard = config.getoption("--shard")
    if shard is None:
        return
    try:
        part, parts = (int(number) for number in shard.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--shard {shard}: expected K/N, e.g. 1/3") from None
    if not 1 <= part <= parts:
        raise pytest.UsageError(f"--shard {shard}: K must be 1 to N")
    kept = items[part - 1 :: parts]
    config.hook.pytest_deselected(items=[item for index, item in enumerate(items) if index % parts != part - 1])
    items[:] = kept


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


@pytest.fixture(scope="session")
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[int]]:
    """A database every migration was applied to, made once per worker, and the versions applied."""
    path = tmp_path_factory.mktemp("migrated") / "ember.db"
    return path, dbmod.migrate(path, backup_dir=path.parent / "backups")


@pytest.fixture(autouse=True)
def migrate_from_a_copy(migrated_database: tuple[Path, list[int]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Applying every migration to a new database takes about half a second, and most tests start an app or an economy
    on an empty data folder: there, migrate copies the database migrated_database made instead (the same file). The
    tests of the migrations themselves call it with their own list of migrations, or on a database that exists, and
    those run for real."""
    template, versions = migrated_database
    real = dbmod.migrate

    def migrate(
        db_file: Path, migrations: list[dbmod.Migration] | None = None, backup_dir: Path | None = None
    ) -> list[int]:
        if migrations is None and not db_file.exists():
            db_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, db_file)
            return list(versions)
        return real(db_file, migrations, backup_dir)

    monkeypatch.setattr(dbmod, "migrate", migrate)


@pytest.fixture(autouse=True)
def ventures_in_the_plan(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.36.0: the plan lays out its Ventures (0.37.0: each venture's node and step) only in a test marked
    ``exploring``: elsewhere the plan is the products' alone and no cycle is a venture cycle (as the venture share of 0
    made it, until 0.35.3). An exploring test explores whenever a venture's step weighs most, without the turns the
    ventures take with a new product while no product step is ready, unless it is marked ``exploring(turns=True)``."""
    marker = request.node.get_closest_marker("exploring")
    if marker is None:
        monkeypatch.setattr(plan, "_ventures", lambda conn, scope, now: [])
    elif not marker.kwargs.get("turns"):
        monkeypatch.setattr(plan, "_ventured_last", lambda conn, scope, before: False)


@pytest.fixture(autouse=True)
def helper_databases() -> Iterator[None]:
    """The databases the test helpers opened (make_economy) are closed when the test ends."""
    yield
    close_databases()


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
