"""The app manifest, translations, changelog and container files stay consistent.

These tests also pin down the security-relevant manifest settings from the
spec, so a later change can't quietly widen what the container may do.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from app.config import DEFAULT_PRICE_TABLE, Settings
from app.version import read_version

APP_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = APP_DIR.parent
MANIFEST = yaml.safe_load((APP_DIR / "config.yaml").read_text(encoding="utf-8"))


def test_required_keys() -> None:
    for key in ("name", "version", "slug", "description", "arch"):
        assert MANIFEST[key], key
    assert MANIFEST["slug"] == "ember"
    assert sorted(MANIFEST["arch"]) == ["aarch64", "amd64"]
    assert isinstance(MANIFEST["version"], str)


ALLOWED_KEYS = {
    "name",
    "version",
    "slug",
    "description",
    "url",
    "arch",
    "init",
    "startup",
    "boot",
    "ingress",
    "ingress_port",
    "panel_icon",
    "panel_title",
    "homeassistant_api",
    "hassio_api",
    "host_network",
    "backup",
    "watchdog",
    "options",
    "schema",
}


def test_least_privilege() -> None:
    """An allowlist: any new manifest key (a new privilege, device, mapping...) must be reviewed here."""
    assert set(MANIFEST) <= ALLOWED_KEYS, set(MANIFEST) - ALLOWED_KEYS
    assert MANIFEST["ingress"] is True
    assert MANIFEST["ingress_port"] == 8099
    assert MANIFEST["homeassistant_api"] is False
    assert MANIFEST["hassio_api"] is False
    assert MANIFEST.get("host_network", False) is False
    assert MANIFEST["init"] is False  # s6-overlay v3 must be PID 1
    assert MANIFEST.get("panel_admin", True) is True  # only admins get the control panel
    assert MANIFEST["backup"] == "cold"  # a consistent copy of the SQLite database


def test_no_build_yaml_and_local_build() -> None:
    assert not (APP_DIR / "build.yaml").exists()  # deprecated by Home Assistant
    assert "image" not in MANIFEST  # built on the device from the Dockerfile


def test_watchdog_uses_health_endpoint() -> None:
    assert MANIFEST["watchdog"] == "http://[HOST]:[PORT:8099]/api/health"


def test_options_match_settings_defaults() -> None:
    defaults = Settings()
    options = MANIFEST["options"]
    schema = MANIFEST["schema"]
    fields = set(Settings.model_fields)
    # Every schema key is a setting; every setting is in the schema.
    assert set(schema) == fields
    # The API key is the only option without a default (optional, "password?").
    assert set(options) == fields - {"anthropic_api_key"}
    assert schema["anthropic_api_key"] == "password?"
    for key, value in options.items():
        if key == "price_table":
            continue
        assert value == getattr(defaults, key), key
    assert [{k: float(v) if k != "model" else v for k, v in row.items()} for row in options["price_table"]] == [
        p.model_dump() for p in DEFAULT_PRICE_TABLE
    ]


def test_schema_bounds_match_settings() -> None:
    """The Supervisor's schema bounds and the app's own validation must agree."""
    schema = MANIFEST["schema"]
    pattern = re.compile(r"^(int|float|str)\((-?[\d.]*),(-?[\d.]*)\)$")
    for key, rule in schema.items():
        if not isinstance(rule, str):
            continue
        match = pattern.match(rule)
        if not match:
            continue
        kind, low, high = match.groups()
        metadata = Settings.model_fields[key].metadata
        bounds = {type(m).__name__: m for m in metadata}
        if kind == "str":
            assert bounds["MinLen"].min_length == int(low), key
            assert bounds["MaxLen"].max_length == int(high), key
        else:
            assert float(bounds["Ge"].ge) == float(low), key
            assert float(bounds["Le"].le) == float(high), key


def test_translations_cover_every_option() -> None:
    translations = yaml.safe_load((APP_DIR / "translations" / "en.yaml").read_text(encoding="utf-8"))
    configuration = translations["configuration"]
    assert set(configuration) == set(MANIFEST["schema"])
    for key, entry in configuration.items():
        assert entry["name"] and entry["description"], key
    assert set(configuration["price_table"]["fields"]) == set(MANIFEST["schema"]["price_table"][0])


def test_changelog_matches_version() -> None:
    changelog = (APP_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (\S+)$", changelog, flags=re.MULTILINE)
    assert headings, "CHANGELOG.md needs '## <version>' headings"
    assert headings[0] == MANIFEST["version"]
    assert read_version(APP_DIR / "config.yaml") == MANIFEST["version"]


def test_supervisor_finds_exactly_one_app_manifest() -> None:
    """The Supervisor scans the whole repository for config.yaml/.yml/.json files."""
    found = []
    for path in REPO_DIR.rglob("config.*"):
        parts = path.relative_to(REPO_DIR).parts
        if any(p.startswith(".") or p == "rootfs" for p in parts):
            continue
        if path.suffix in (".yaml", ".yml", ".json"):
            found.append(path)
    assert found == [APP_DIR / "config.yaml"]


def test_repository_file() -> None:
    repository = yaml.safe_load((REPO_DIR / "repository.yaml").read_text(encoding="utf-8"))
    assert repository["name"]


def test_service_scripts_are_executable() -> None:
    service = APP_DIR / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "ember"
    assert (service / "type").read_text().strip() == "longrun"
    for script in ("run", "finish"):
        assert os.access(service / script, os.X_OK), script
    assert (APP_DIR / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "user" / "contents.d" / "ember").exists()
    assert "exec python3 -m app" in (service / "run").read_text()


def test_dockerfile_pins_official_base_image() -> None:
    dockerfile = (APP_DIR / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG BUILD_FROM=(\S+)$", dockerfile, flags=re.MULTILINE)
    assert match
    assert re.fullmatch(r"ghcr\.io/home-assistant/base-python:3\.12-alpine[\d.]+-\d{4}\.\d{2}\.\d+", match.group(1))
    assert "COPY config.yaml CHANGELOG.md" in dockerfile


def test_requirements_are_pinned() -> None:
    for line in (APP_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[\w.]+", line), line
