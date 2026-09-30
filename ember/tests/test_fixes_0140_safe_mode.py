"""0.14.0 (FIX NOW 3, 4, 5 and 7): safe mode keeps the owner's identity and the kill switch; every option's bound is in
config.yaml, where Home Assistant checks it when the owner saves; the rules between options that it can't check are
corrected instead of starting safe mode; and the secret scan's allowlist covers the tests' fake passwords."""

from __future__ import annotations

import re
import tomllib
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml
from fastapi.testclient import TestClient

from app.agent.owner import KILL_RESET_KEY
from app.config import _SITE_EMAIL, _SITE_URL, ModelPrice, Settings, load_settings
from app.economy.life import KILLED_KEY
from app.security import NOT_OWNER, SAFE_MODE_LOCKED, AccessPolicy
from tests.test_owner_api import CSRF, grant

APP_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = APP_DIR.parent
SCHEMA = yaml.safe_load((APP_DIR / "config.yaml").read_text(encoding="utf-8"))["schema"]

OWNER = "8f14e45fceea167a5a36dedd4bea2543"
TABLET = "c9f0f895fb98ab9159f51fd0297e236d"
UNPRICED = {"planner_model": "claude-unknown-9"}  # a rule between options that no correction can guess


def as_user(user_id: str) -> dict[str, str]:
    return {**CSRF, "X-Remote-User-Id": user_id}


# --- FIX 3: safe mode keeps owner_user_ids ----------------------------------------------------------------------------


def test_safe_mode_keeps_the_owner_ids(client_factory: Callable[..., Iterator[TestClient]], write_options) -> None:
    """The reproduction: one invalid option, and any Home Assistant user could open the dashboard, download the
    private diagnostics and pause Ember, because safe mode's defaults have no owner_user_ids."""
    write_options({"owner_user_ids": [f" {OWNER} "], "dry_run": False, **UNPRICED})
    loaded = load_settings()
    assert loaded.safe_mode and loaded.settings.dry_run is True
    assert loaded.settings.owner_user_ids == (OWNER,) and not loaded.owner_unknown
    with client_factory(loaded) as client:
        tablet = as_user(TABLET)
        for path in ("api/dashboard", "api/diagnostics?full=1", "api/events", "api/ledger"):
            refused = client.get(path, headers=tablet)
            assert refused.status_code == 403 and refused.json()["code"] == "not_owner", path
        assert client.post("api/control/pause", json={}, headers=tablet).status_code == 403
        assert client.post("api/ledger/grant", json=grant("50"), headers=tablet).status_code == 403
        page = client.get("", headers=tablet)
        assert page.status_code == 403 and NOT_OWNER in page.text
        dashboard = client.get("api/dashboard", headers=as_user(OWNER)).json()
    assert dashboard["system"]["safe_mode"] is True and dashboard["system"]["owner"]["ids_set"] is True
    assert any("owner_user_ids and the kill switch are kept" in e["message"] for e in dashboard["events"])


@pytest.mark.parametrize(
    "options",
    [
        "{not json",  # the file can't be read at all
        {"owner_user_ids": ["x" * 101], **UNPRICED},  # the option itself is invalid
        {"owner_user_ids": OWNER, **UNPRICED},  # not a list
    ],
)
def test_safe_mode_that_cant_read_the_owner_ids_answers_no_one(
    options: Any, client_factory: Callable[..., Iterator[TestClient]], data_dir: Path, write_options
) -> None:
    """Fail closed: a page that names safe mode and the caller's user ID, and nothing else for anyone."""
    if isinstance(options, str):
        (data_dir / "options.json").write_text(options, encoding="utf-8")
    else:
        write_options(options)
    loaded = load_settings()
    assert loaded.safe_mode and loaded.owner_unknown and loaded.settings.owner_user_ids == ()
    with client_factory(loaded) as client:
        for user in (OWNER, TABLET):
            for path in ("api/dashboard", "api/diagnostics", "api/events"):
                refused = client.get(path, headers=as_user(user))
                assert refused.status_code == 403, path
                assert refused.json() == {"code": "safe_mode_locked", "error": SAFE_MODE_LOCKED}, path
            assert client.post("api/control/pause", json={}, headers=as_user(user)).status_code == 403
            page = client.get("", headers=as_user(user))
            assert page.status_code == 403 and "safe mode" in page.text and f"Your user ID: {user}" in page.text
        assert client.get("api/health").status_code == 200  # the watchdog
        assert client.get("static/js/app.js", headers=as_user(TABLET)).status_code == 200


def test_the_policy_when_locked_and_an_option_the_owner_left_out() -> None:
    assert not AccessPolicy(locked=True).owner_allows("/api/dashboard", OWNER)
    assert not AccessPolicy(owner_ids={OWNER}, locked=True).owner_allows("/api/dashboard", OWNER)
    assert AccessPolicy(locked=True).owner_allows("/api/health", None)
    assert AccessPolicy(dev_mode=True, locked=True).owner_allows("/api/dashboard", None)  # no Ingress, no users


def test_safe_mode_is_no_wider_than_the_owners_options(write_options) -> None:
    """Safe mode keeps what the options say: no owner named there means everyone, as in a valid start (the dashboard
    warns), and not "no one"."""
    write_options(UNPRICED)
    loaded = load_settings()
    assert loaded.safe_mode and not loaded.owner_unknown and loaded.settings.owner_user_ids == ()
    write_options({"owner_user_ids": [], **UNPRICED})
    assert not load_settings().owner_unknown


# --- FIX 4: safe mode never lifts a kill switch -----------------------------------------------------------------------


def _start(client_factory: Callable[..., Iterator[TestClient]], write_options, options: dict) -> dict[str, Any]:
    write_options({"owner_user_ids": [OWNER], **options})
    with client_factory(load_settings()) as client:
        db = client.app.state.ember.db
        events = client.get("api/events", headers=as_user(OWNER)).json()
        return {"killed": db.get_meta(KILLED_KEY), "marker": db.get_meta(KILL_RESET_KEY), "events": events}


def test_safe_mode_keeps_the_kill_switch(client_factory: Callable[..., Iterator[TestClient]], write_options) -> None:
    """The reproduction: the owner had once reset a kill (kill_switch_reset 1), killed Ember again, and made an option
    invalid while tightening them. The safe-mode start read kill_switch_reset as 0, lifted the kill, and the next valid
    start ran live."""
    write_options({"owner_user_ids": [OWNER], "kill_switch_reset": 1})
    with client_factory(load_settings()) as client:
        killed = client.post("api/control/kill", json={"confirm_name": "Ember"}, headers=as_user(OWNER))
        assert killed.status_code == 200 and killed.json()["state"] == "killed"

    safe = _start(client_factory, write_options, {"kill_switch_reset": 1, **UNPRICED})
    assert safe["killed"] == "1" and safe["marker"] == "1"
    assert not any("may run again" in e["message"] for e in safe["events"])
    assert _start(client_factory, write_options, {"kill_switch_reset": 1})["killed"] == "1"  # fixed: still killed

    # A reset the owner makes while the options are invalid waits for valid options; the marker isn't moved meanwhile.
    held = _start(client_factory, write_options, {"kill_switch_reset": 2, **UNPRICED})
    assert held["killed"] == "1" and held["marker"] == "1"
    assert any("Safe mode keeps the kill switch on" in e["message"] for e in held["events"])
    unreadable = _start(client_factory, write_options, {"kill_switch_reset": "two", **UNPRICED})
    assert unreadable["killed"] == "1" and unreadable["marker"] == "1"
    valid = _start(client_factory, write_options, {"kill_switch_reset": 2})
    assert valid["killed"] == "0" and valid["marker"] == "2"
    assert any("may run again" in e["message"] for e in valid["events"])


def test_a_first_start_in_safe_mode_stores_no_reset(
    client_factory: Callable[..., Iterator[TestClient]], write_options
) -> None:
    assert _start(client_factory, write_options, {"kill_switch_reset": 3, **UNPRICED})["marker"] is None
    assert _start(client_factory, write_options, {"kill_switch_reset": 3})["marker"] == "3"


# --- FIX 5: rules between options are corrected, bounds are in config.yaml ------------------------------------------


def test_the_first_analysis_advice_no_longer_starts_safe_mode(
    client_factory: Callable[..., Iterator[TestClient]], write_options
) -> None:
    """The owner raised the shortest sleep to 180 and left the default sleep at 60: config.py refused the pair, which
    Home Assistant can't check, and Ember started in safe mode. Now the default sleep follows the shortest sleep."""
    write_options({"owner_user_ids": [OWNER], "min_sleep_minutes": 180, "wake_interval_minutes": 60, "dry_run": False})
    loaded = load_settings()
    assert not loaded.safe_mode and loaded.settings.dry_run is False
    assert (loaded.settings.min_sleep_minutes, loaded.settings.wake_interval_minutes) == (180, 180)
    assert loaded.corrections == [
        "wake_interval_minutes (60) must be between min_sleep_minutes and max_sleep_minutes: the default sleep is 180"
    ]
    with client_factory(loaded) as client:
        dashboard = client.get("api/dashboard", headers=as_user(OWNER)).json()
    assert dashboard["system"]["config_corrections"] == loaded.corrections
    assert dashboard["system"]["safe_mode"] is False and dashboard["system"]["config_errors"] == []
    assert any(e["level"] == "error" and e["message"].startswith("Options corrected: ") for e in dashboard["events"])


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"daily_spend_cap_usd": 0.4, "cycle_spend_cap_usd": 0.5}, {"cycle_spend_cap_usd": 0.4}),
        (
            {"min_sleep_minutes": 300, "max_sleep_minutes": 240, "wake_interval_minutes": 240},
            {"max_sleep_minutes": 300, "wake_interval_minutes": 300},
        ),
        ({"max_sleep_minutes": 120, "wake_interval_minutes": 240}, {"wake_interval_minutes": 120}),
        ({"etsy_usd_per_eur": 0.3}, {"etsy_usd_per_eur": 0}),
    ],
)
def test_each_rule_is_corrected_toward_spending_less(write_options, options: dict, expected: dict) -> None:
    write_options(options)
    loaded = load_settings()
    assert not loaded.safe_mode and len(loaded.corrections) == len(expected)
    for key, value in expected.items():
        assert getattr(loaded.settings, key) == value, key


def test_consistent_options_are_not_corrected(write_options) -> None:
    write_options({"daily_spend_cap_usd": 1, "cycle_spend_cap_usd": 1, "etsy_usd_per_eur": 0, "min_sleep_minutes": 60})
    loaded = load_settings()
    assert not loaded.safe_mode and loaded.corrections == []
    write_options({"min_sleep_minutes": "late", "wake_interval_minutes": 60})  # a wrong type is still an error
    assert load_settings().safe_mode


# The Supervisor's grammar for a schema rule (supervisor/addons/options.py, RE_SCHEMA_ELEMENT). A rule outside it
# makes Home Assistant refuse the whole app.
SUPERVISOR_RULE = re.compile(
    r"^(?:|bool|email|url|port|device(?:\((?P<filter>subsystem=[a-z]+)\))?"
    r"|str(?:\((?P<s_min>\d+)?,(?P<s_max>\d+)?\))?"
    r"|password(?:\((?P<p_min>\d+)?,(?P<p_max>\d+)?\))?"
    r"|int(?:\((?P<i_min>\d+)?,(?P<i_max>\d+)?\))?"
    r"|float(?:\((?P<f_min>[\d\.]+)?,(?P<f_max>[\d\.]+)?\))?"
    r"|match\((?P<match>.*)\)|list\((?P<list>.+)\))\??$"
)


def _rules() -> Iterator[tuple[str, str, pydantic.fields.FieldInfo]]:
    """(name, Home Assistant's rule, config.py's field) for every option, the price table's columns and the owner IDs'
    items included."""
    for key, field in Settings.model_fields.items():
        rule = SCHEMA[key]
        if key == "price_table":
            for column, price in ModelPrice.model_fields.items():
                yield f"price_table.{column}", rule[0][column], price
        elif key == "owner_user_ids":
            yield "owner_user_ids[]", rule[0], pydantic.fields.FieldInfo(annotation=str, max_length=100)
        else:
            yield key, rule, field


def _ha_accepts(rule: str, value: Any) -> bool:
    """How the Supervisor checks a value against a rule, for the kinds used here."""
    match = SUPERVISOR_RULE.match(rule)
    assert match, rule
    if match.group("match") is not None:
        return re.match(match.group("match"), str(value)) is not None
    low, high = (match.group(f"s_{end}") for end in ("min", "max"))
    return int(low or 0) <= len(value) <= int(high or 10**9)


def test_every_bound_in_config_py_is_in_the_schema() -> None:
    """The reproduction: 13 options were bounded only in config.py (a 61-character site name, an http:// address), so
    Home Assistant saved them and Ember started in safe mode. Each bound of config.py must be in the schema too."""
    for name, rule, field in _rules():
        match = SUPERVISOR_RULE.match(rule)
        assert match, f"{name}: Home Assistant can't read {rule!r}"
        bounds = {type(m).__name__: m for m in field.metadata}
        if match.group("match") is not None:
            pattern = {"site_url": _SITE_URL, "site_email": _SITE_EMAIL}[name].pattern
            assert match.group("match") == pattern, name
            assert f"(?=.{{0,{bounds['MaxLen'].max_length}}}$)" in pattern, name  # the length is in the pattern
        elif rule.startswith("str"):
            assert match.group("s_min") == (str(bounds["MinLen"].min_length) if "MinLen" in bounds else None), name
            assert match.group("s_max") == (str(bounds["MaxLen"].max_length) if "MaxLen" in bounds else None), name
        elif rule.startswith(("int", "float")):
            kind = rule[0]
            assert float(match.group(f"{kind}_min")) == float(bounds["Ge"].ge), name
            assert float(match.group(f"{kind}_max")) == float(bounds["Le"].le), name
        elif rule.startswith("list"):
            assert match.group("list").split("|") == list(typing.get_args(field.annotation)), name
        else:
            assert not bounds, f"{name}: {rule!r} has no bounds for {bounds}"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("site_url", "http://my-shop.example"),
        ("site_url", "https://www.müller-shop.de"),
        ("site_url", "https://example.org/?page=1"),
        ("site_url", "https://example.org/#top"),
        ("site_url", "https://" + "a" * 146 + ".de/" + "b" * 42),  # 200 characters
        ("site_url", "https://" + "a" * 146 + ".de/" + "b" * 43),
        ("site_url", "https://example.org/shop"),
        ("site_url", "https://xn--mller-shop-q9a.de"),
        ("site_url", ""),
        ("site_email", "shop@example.org"),
        ("site_email", "shop@example"),
        ("site_email", '"a b"@example.org'),
        ("site_email", "a@b.123"),
        ("site_email", "x" * 64 + "@" + "y" * 186 + ".org"),
        ("site_name", "x" * 60),
        ("site_name", "x" * 61),
        ("site_vat_id", "DE123456789"),
        ("site_vat_id", "DE 123 456 789 000 000 0"),
        ("owner_user_ids[]", "x" * 100),
        ("owner_user_ids[]", "x" * 101),
        ("workshop_model", "m" * 101),
    ],
)
def test_what_home_assistant_saves_ember_accepts(key: str, value: str) -> None:
    """The same answer from both: a value Home Assistant saves never starts safe mode, and one Ember would refuse is
    refused when the owner saves it."""
    rule = dict((name, rule) for name, rule, _ in _rules())[key]
    options = {"owner_user_ids": [value]} if key == "owner_user_ids[]" else {key: value}
    try:
        Settings.model_validate(options)
        ember = True
    except pydantic.ValidationError:
        ember = False
    assert _ha_accepts(rule, value) == ember


# --- FIX 7: the secret scan passes over the tests' fake passwords ----------------------------------------------------


def test_the_secret_scan_allows_every_fake_password_in_the_repository() -> None:
    """CI's Secret scan failed on both releases over tests/test_privacy.py's "aaaaaa-0bbbbb-ccccCd", a fake one letter
    off the allowlisted one. The rule's allowlist must cover every value of its shape in the files."""
    config = tomllib.loads((REPO_DIR / ".gitleaks.toml").read_text(encoding="utf-8"))
    (rule,) = [r for r in config["rules"] if r["id"] == "generated-password-6-6-6"]
    shape = re.compile(rule["regex"])
    allowed = [re.compile(regex) for allowlist in rule["allowlists"] for regex in allowlist["regexes"]]
    found = set()
    for path in REPO_DIR.rglob("*"):
        parts = path.relative_to(REPO_DIR).parts
        if path.is_file() and ".git" not in parts and path.suffix in (".py", ".js", ".md", ".yaml", ".toml", ".json"):
            found |= {m.group(0) for m in shape.finditer(path.read_text(encoding="utf-8", errors="ignore"))}
    assert "aaaaaa-0bbbbb-ccccCd" in found
    assert [value for value in found if not any(a.search(value) for a in allowed)] == []
    real_shape = "-".join(("Kx7mQp", "2vRtNw", "hB4zLs"))  # joined here, so this file has none of the shape
    assert shape.search(real_shape) and not any(a.search(real_shape) for a in allowed)  # still found
