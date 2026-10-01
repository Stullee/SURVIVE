from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from app.config import DEFAULT_PRICE_TABLE, Settings, load_settings

KEY = "sk-ant-api03-supersecretvalue1234567890"


def test_defaults_without_options_file() -> None:
    loaded = load_settings()
    assert not loaded.safe_mode
    assert loaded.settings.dry_run is True
    assert loaded.settings.agent_name == "Ember"
    assert loaded.settings.price_table == DEFAULT_PRICE_TABLE
    assert "no options file" in loaded.source


def test_reads_options_json(write_options: Callable[[dict], Path]) -> None:
    write_options({"agent_name": "Spark", "daily_spend_cap_usd": 2.5, "dry_run": False, "anthropic_api_key": KEY})
    loaded = load_settings()
    assert not loaded.safe_mode
    assert loaded.settings.agent_name == "Spark"
    assert loaded.settings.daily_spend_cap_usd == 2.5
    assert loaded.settings.dry_run is False
    assert loaded.settings.api_key_set


def test_options_path_override(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "elsewhere.json"
    custom.write_text(json.dumps({"agent_name": "Override"}), encoding="utf-8")
    monkeypatch.setenv("EMBER_OPTIONS_PATH", str(custom))
    assert load_settings().settings.agent_name == "Override"


def test_api_key_never_exposed(write_options: Callable[[dict], Path]) -> None:
    write_options({"anthropic_api_key": KEY})
    settings = load_settings().settings
    for text in (repr(settings), str(settings), settings.model_dump_json(), json.dumps(settings.public_dict())):
        assert KEY not in text
    assert settings.public_dict()["anthropic_api_key_set"] is True
    assert "anthropic_api_key" not in settings.public_dict()


def test_blank_key_counts_as_not_set(write_options: Callable[[dict], Path]) -> None:
    write_options({"anthropic_api_key": "   "})
    assert load_settings().settings.api_key_set is False


def test_cycle_cap_above_daily_cap_is_corrected(write_options: Callable[[dict], Path]) -> None:
    # 0.14.0: corrected (and shown on the dashboard) instead of safe mode, which Home Assistant's schema can't prevent.
    write_options({"daily_spend_cap_usd": 0.5, "cycle_spend_cap_usd": 1.0, "dry_run": False})
    loaded = load_settings()
    assert not loaded.safe_mode
    assert any("cycle_spend_cap_usd" in c for c in loaded.corrections)
    assert loaded.settings.dry_run is False
    assert loaded.settings.cycle_spend_cap_usd == loaded.settings.daily_spend_cap_usd == 0.5


def test_sleep_bounds_are_checked(write_options: Callable[[dict], Path]) -> None:
    write_options({"min_sleep_minutes": 60, "wake_interval_minutes": 30, "max_sleep_minutes": 120})
    loaded = load_settings()
    assert not loaded.safe_mode  # 0.14.0: corrected
    assert any("wake_interval_minutes" in c for c in loaded.corrections)
    assert loaded.settings.wake_interval_minutes == 60


def test_models_must_have_prices(write_options: Callable[[dict], Path]) -> None:
    write_options({"planner_model": "claude-unknown-9"})
    loaded = load_settings()
    assert loaded.safe_mode
    assert any("planner_model" in e and "price_table" in e for e in loaded.errors)


def test_duplicate_price_entries_rejected(write_options: Callable[[dict], Path]) -> None:
    entry = DEFAULT_PRICE_TABLE[0].model_dump()
    write_options({"price_table": [entry, entry]})
    loaded = load_settings()
    assert loaded.safe_mode
    assert any("more than once" in e for e in loaded.errors)


def test_negative_price_rejected(write_options: Callable[[dict], Path]) -> None:
    bad = {**DEFAULT_PRICE_TABLE[0].model_dump(), "output": -1}
    write_options({"price_table": [bad]})
    assert load_settings().safe_mode


def test_price_lookup_is_exact() -> None:
    settings = Settings()
    assert settings.price_for("claude-sonnet-5") is not None
    # A similar-looking but different model must not borrow another model's price.
    assert settings.price_for("claude-sonnet-5-20990101") is None
    assert settings.price_for("claude-haiku-4-5") is not None  # 0.12.0: priced out of the box, for research
    assert settings.price_for("claude-haiku-4-5-20251001") is None


def test_invalid_json_starts_safe_mode(data_dir: Path) -> None:
    (data_dir / "options.json").write_text("{not json", encoding="utf-8")
    loaded = load_settings()
    assert loaded.safe_mode
    assert loaded.settings.dry_run is True


def test_non_object_json_starts_safe_mode(data_dir: Path) -> None:
    (data_dir / "options.json").write_text("[1, 2]", encoding="utf-8")
    assert load_settings().safe_mode


def test_validation_errors_do_not_echo_values(write_options: Callable[[dict], Path]) -> None:
    # The secret is itself the invalid value in each field, so echoing inputs would expose it.
    write_options({"anthropic_api_key": [KEY], "agent_name": KEY * 2, "planner_model": {"id": KEY}})
    loaded = load_settings()
    assert loaded.safe_mode
    assert len(loaded.errors) >= 3
    assert all(KEY not in e for e in loaded.errors)


def test_api_key_is_trimmed(write_options: Callable[[dict], Path]) -> None:
    write_options({"anthropic_api_key": f"  {KEY}\n"})
    assert load_settings().settings.anthropic_api_key.get_secret_value() == KEY


def test_unknown_options_are_ignored(write_options: Callable[[dict], Path]) -> None:
    write_options({"something_new": 1})
    assert not load_settings().safe_mode
